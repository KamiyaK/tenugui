"""検証・診断・下書き生成など、変換本体の周辺機能（CLI/UI/agent 共通で使う）。"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .core import (Config, ConfigError, FieldRule, Issue, _apply_field, _is_reserved, check_header, compile_pattern, is_null,
                   convert_rows, select_case, shape_of, survey as _survey, uniquify)
from .corrections import KIND_LABEL, Provenance, summarize, unused_entries
from .units import UnitError, UnitTable, fold_key, normalize_text, parse_quantity

# ---------------------------------------------------------------- 修正のヒント
HINTS = {
    "C001": "入力CSVに無い列です。列名の綴り・chozu側の列名変更を確認し、field の src / vars を直す",
    "C002": "出力列名が既存列と衝突。out を別名にする",
    "C101": "数値+単位として読めない。複合表記なら match/parts か extract/sub で数値部分だけ取り出す",
    "C102": "単位が無い値。default_unit を指定するか、入力側に単位を付ける",
    "C103": "未定義の単位か次元違い。[units.*] / [unit_alias] に追加するか、to を見直す",
    "C104": "strip_units に無い単位。単位を換算するなら to、捨ててよいなら strip_units に足す",
    "C111": "書式が外れた。tenugui suggest で追加すべき case の案を得るか、tenugui try で値を試す",
    "C131": "変換はできたが形が少数派（単位なし・桁区切り・書き方違いなど）。正しければそのままでよい。"
            "別の書式なら case を足す。警告が不要なら [settings] rare_shape = 0",
    "L401": "正規表現の繰り返しを入れ子にしない。(\\d+)+ は \\d+ と同じ意味で、ずっと速い",
    "C121": "同義語表に無い値。[synonyms.*] に追加するか on_unknown を見直す（survey の candidates 参照）",
    "C141": "訂正ファイルの [[hold]]（保留）に当たった値。変換せず元の値のまま通している。"
            "判断がついたら [[hold]] を消し、誤記なら [[correct]] に、正しい値ならルールを足す",
    "C201": "必須項目が空。入力データの欠落か、match の該当部分が無い",
    "C301": "派生の入力が数値として読めない。上流の field で数値化する",
    "C302": "入力がchozuの抽出NG印のため計算しない（情報）",
    "C303": "式が計算できない（0除算など）",
    "C304": "単位が無いので vars の単位とみなした。入力側に単位があるか確認",
    "C305": "派生の換算ができない。unit / to / vars の単位の次元を揃える",
    "C311": "単位付きの生値は vars に '列名@単位' で指定する",
}


def issue_dict(i: Issue) -> dict:
    return {"row": i.row, "key": i.key, "column": i.column, "out": i.out, "code": i.code,
            "severity": i.severity, "message": i.message, "raw": i.raw, "hint": HINTS.get(i.code, "")}


# ---------------------------------------------------------------- lint（設定だけで分かる注意）
_NESTED_REPEAT = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d+,\}?)")


def lint(cfg: Config) -> list[dict]:
    out: list[dict] = []
    for fr in cfg.fields:
        if fr.out == fr.src and fr.to and fr.unit_out == "strip" and not fr.default_unit:
            out.append({"code": "L101", "where": fr.src,
                        "message": "出力は単位なしの数値になるため、変換済みCSVをもう一度通すとC102になる。"
                                   "default_unit を指定すると再実行できる"})
    seen_rx: set[str] = set()
    for fr in cfg.fields:
        cands = [(fr, "match / extract")] if fr.extract is not None and fr.label in ("", "match") else []
        cands += [(alt, "match") for alt in fr.alts.values() if alt.label == "match"]
        for x, what in cands:
            pat = x.extract.pattern
            if pat not in seen_rx and _NESTED_REPEAT.search(pat):
                seen_rx.add(pat)
                out.append({"code": "L401", "where": fr.src,
                            "message": f"{what} の正規表現 {pat!r} に、繰り返しの中の繰り返し（(a+)+ の形）があります。"
                                       "一致しない値で処理が止まったように遅くなることがあります。内側か外側の繰り返しを外してください"})
    used = {fr.synonyms for fr in cfg.fields if fr.synonyms}
    for name in cfg.synonyms:
        if name not in used:
            out.append({"code": "L201", "where": f"synonyms.{name}", "message": "どの field からも使われていない同義語表"})
    return out


# ---------------------------------------------------------------- 変更差分・冪等性
def diff_changes(header: list[str], rows: list[list[str]], out_header: list[str],
                 out_rows: list[list[str]]) -> list[dict]:
    """入力にも出力にもある列で、値が変わったセルの一覧（前→後）。"""
    uh, _ = uniquify(header)
    iidx = {h: i for i, h in enumerate(uh)}
    common = [(c, iidx[c], j) for j, c in enumerate(out_header) if c in iidx]
    res = []
    for n, (r_in, r_out) in enumerate(zip(rows, out_rows), start=1):
        key = r_in[0] if r_in else ""
        for c, i, j in common:
            before = r_in[i] if i < len(r_in) else ""
            if before != r_out[j]:
                res.append({"row": n, "key": key, "column": c, "before": before, "after": r_out[j]})
    return res


def idempotency_report(cfg: Config, header: list[str], rows: list[list[str]]) -> list[dict]:
    """変換結果をもう一度通して値が変わる列。置換列(out==src)だけが対象。"""
    sub = replace(cfg, fields=[f for f in cfg.fields if f.out == f.src], derives=[])
    if not sub.fields:
        return []
    h1, r1, iss1 = convert_rows(cfg, header, rows)
    h2, r2, iss2 = convert_rows(sub, h1, r1)
    cols = {f.out for f in sub.fields}
    i1 = {c: i for i, c in enumerate(h1)}
    i2 = {c: i for i, c in enumerate(h2)}
    bad: dict[str, dict] = {}
    for n, (a, b) in enumerate(zip(r1, r2), start=1):
        for c in cols:
            if c in i1 and c in i2 and a[i1[c]] != b[i2[c]] and c not in bad:
                bad[c] = {"column": c, "row": n, "first": a[i1[c]], "second": b[i2[c]]}
    first = {(i.row, i.column) for i in iss1}
    for i in iss2:   # 2回目に「新たに」失敗するもの（単位なしの出力にdefault_unitが無い等）。1回目から失敗の値は対象外
        if i.severity == "E" and i.column not in bad and (i.row, i.column) not in first:
            bad[i.column] = {"column": i.column, "row": i.row, "first": r1[i.row - 1][i1[i.column]],
                             "second": f"(再実行でエラー {i.code})"}
    return list(bad.values())


# ---------------------------------------------------------------- ヘッダのドリフト（lockファイル）
def header_sha(uh: list[str]) -> str:
    return hashlib.sha256("\n".join(uh).encode("utf-8")).hexdigest()[:12]


def lock_path(config_path: str | Path) -> Path:
    return Path(config_path).with_suffix(".lock.json")


def write_lock(config_path: str | Path, header: list[str]) -> Path:
    uh, _ = uniquify(header)
    p = lock_path(config_path)
    p.write_text(json.dumps({"sha": header_sha(uh), "columns": uh}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


def header_drift(config_path: str | Path, header: list[str]) -> dict | None:
    """lockがあれば、前回のヘッダと比べて増えた列・消えた列を返す。lockが無ければ None。"""
    p = lock_path(config_path)
    if not p.exists():
        return None
    try:
        old = json.loads(p.read_text(encoding="utf-8-sig"))
        cols, sha = old["columns"], old.get("sha", "")
        if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols) or not isinstance(sha, str):
            raise ValueError("columns")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ConfigError([f"{p}: 列構成の記録（lock）が壊れていて読めません。`tenugui lock 入力CSV -c 設定` で"
                           "作り直すか、このファイルを消してください"]) from None
    uh, _ = uniquify(header)
    added = [c for c in uh if c not in cols]
    removed = [c for c in cols if c not in uh]
    return {"changed": bool(added or removed or uh != cols), "added": added,
            "removed": removed, "sha": header_sha(uh), "locked_sha": sha}


# ---------------------------------------------------------------- survey / 同義語候補
def synonym_candidates(values: Counter, max_distinct: int = 300) -> list[dict]:
    """表記ゆれの候補をまとめる。exact=NFKC・大小・空白・ハイフン違いのみ / similar=包含・類似。"""
    vals = [v for v in values if v][:max_distinct]
    groups: dict[str, list[str]] = {}
    for v in vals:
        groups.setdefault(fold_key(v), []).append(v)
    keys = list(groups)
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k

    similar: set[str] = set()
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if re.sub(r"\d", "", a) == re.sub(r"\d", "", b):
                continue  # 数字だけ違う（型番・番号・寸法）は別物。ゆれとして束ねない
            if len(a) >= 2 and len(b) >= 2 and (a in b or b in a or
                                                difflib.SequenceMatcher(None, a, b).ratio() >= 0.8):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
                similar.update((a, b))
    merged: dict[str, list[str]] = {}
    for k in keys:
        merged.setdefault(find(k), []).extend(groups[k])
    out = []
    for r, vs in merged.items():
        if len(vs) < 2:
            continue
        kind = "exact" if len({fold_key(v) for v in vs}) == 1 else "similar"
        canon = max(vs, key=lambda v: (values[v], -len(v)))
        out.append({"canonical": canon, "variants": [v for v in vs if v != canon], "kind": kind,
                    "count": sum(values[v] for v in vs)})
    return sorted(out, key=lambda d: -d["count"])


def survey_full(cfg: Config, header: list[str], rows: list[list[str]], column: str, top: int = 30) -> dict:
    s = _survey(cfg, header, rows, column, top)
    uh, _ = uniquify(header)
    i = uh.index(column)
    vals = Counter(normalize_text(r[i]) if i < len(r) else "" for r in rows)
    vals = Counter({v: n for v, n in vals.items() if v and not _is_reserved(cfg, v)})
    textual = Counter()
    for v, n in vals.items():
        try:
            parse_quantity(v, cfg.units)
        except UnitError:
            textual[v] = n
    s["shapes"] = shape_counts(vals)
    s["candidates"] = synonym_candidates(textual)
    s["composite_examples"] = [v for v in vals if any(ch in v for ch in "±×～~") and any(c.isdigit() for c in v)][:3]
    return s


# ---------------------------------------------------------------- 形の集計
def shape_counts(vals: Counter, top: int = 20, examples: int = 3) -> list[dict]:
    """値の「形」（数値を # にしたもの）ごとの件数と例。件数の多い順。"""
    by: dict[str, Counter] = {}
    for v, n in vals.items():
        by.setdefault(shape_of(v), Counter())[v] += n
    res = [{"shape": sh, "count": sum(c.values()), "distinct": len(c),
            "examples": [v for v, _ in c.most_common(examples)]} for sh, c in by.items()]
    return sorted(res, key=lambda d: (-d["count"], d["shape"]))[:top]


def survey_overview(cfg: Config, header: list[str], rows: list[list[str]], top: int = 5) -> list[dict]:
    """全列の概況: 値の種類数と形の種類数。形が複数ある列が、ばらつきのある列。"""
    uh, _ = uniquify(header)
    res = []
    for i, col in enumerate(uh):
        vals = Counter(normalize_text(r[i]) for r in rows if i < len(r))
        ng = sum(n for v, n in vals.items() if v and _is_reserved(cfg, v))
        vals = Counter({v: n for v, n in vals.items() if v and not _is_reserved(cfg, v)})
        allsh = shape_counts(vals, top=10 ** 6)
        res.append({"column": col, "filled": sum(vals.values()), "ng": ng, "distinct": len(vals),
                    "shape_count": len(allsh), "shapes": allsh[:top]})
    return res


# ---------------------------------------------------------------- 問題のグループ化
def group_issues(issues: list[Issue], examples: int = 3, rows: int = 5) -> list[dict]:
    """問題を「列 × コード × 値の形」でまとめる。件数の多い順。行ごとの一覧より桁違いに短い。"""
    by: dict[tuple, dict] = {}
    for i in issues:
        sh = shape_of(i.raw) if i.raw else ""
        k = (i.column, i.out if i.code != "C111" else "", i.code, sh if len(sh) <= 120 else sh[:120] + "…")
        g = by.setdefault(k, {"column": i.column, "out": k[1], "code": i.code, "severity": i.severity,
                              "shape": k[3], "count": 0, "examples": [], "rows": [],
                              "message": i.message, "hint": HINTS.get(i.code, "")})
        g["count"] += 1
        if i.raw and i.raw not in g["examples"] and len(g["examples"]) < examples:
            g["examples"].append(i.raw)
        if len(g["rows"]) < rows:
            g["rows"].append(i.row)
    return sorted(by.values(), key=lambda d: (-d["count"], d["column"], d["code"], d["shape"]))


def case_coverage(cfg: Config, header: list[str], rows: list[list[str]]) -> list[dict]:
    """case 形式の列ごとに、どの書式で何件通ったか。"""
    uh, _ = uniquify(header)
    idx = {h: i for i, h in enumerate(uh)}
    res, done = [], set()
    for fr in cfg.fields:
        if not fr.case_rx or fr.src in done or fr.src not in idx:
            continue
        done.add(fr.src)
        cnt: Counter = Counter()
        i = idx[fr.src]
        for r in rows:
            raw = r[i] if i < len(r) else ""
            if _is_reserved(cfg, raw):
                cnt["ng"] += 1
                continue
            k = select_case(fr, raw)
            cnt["empty" if k is None else k] += 1
        res.append({"column": fr.src,
                    "cases": [{"case": k + 1, "label": lb, "count": cnt[k]} for k, lb in enumerate(fr.case_labels)],
                    "unmatched": cnt[-1], "empty": cnt["empty"], "ng": cnt["ng"]})
    return res


# ---------------------------------------------------------------- 回帰テスト（[[test]]）
def run_tests(cfg: Config) -> dict:
    """設定内の [[test]] を実行。期待と違う出力・報告コードを failures に入れる。"""
    results = []
    for t in cfg.tests:
        r = try_row(cfg, t["in"])
        diffs = [{"column": c, "expected": exp, "actual": r["outputs"].get(c)}
                 for c, exp in t["out"].items() if r["outputs"].get(c) != exp]
        codes = sorted({i["code"] for i in r["issues"]})
        if codes != sorted(set(t["codes"])):
            diffs.append({"column": "(codes)", "expected": ",".join(sorted(set(t["codes"]))) or "(なし)",
                          "actual": ",".join(codes) or "(なし)"})
        results.append({"name": t["name"], "ok": not diffs, "in": t["in"], "diffs": diffs})
    failed = [r for r in results if not r["ok"]]
    return {"total": len(results), "failed": len(failed), "ok": not failed, "failures": failed}


def as_test_block(values: dict[str, str], res: dict, name: str = "") -> str:
    """try の結果を、そのまま設定に貼れる [[test]] にする。"""
    outs = {c: v for c, v in res["outputs"].items() if c not in values or values[c] != v}
    L = ["[[test]]"]
    if name:
        L.append(f"name = {_q(name)}")
    L.append("in = { " + ", ".join(f"{_q(k)} = {_q(v)}" for k, v in values.items()) + " }")
    codes = sorted({i["code"] for i in res["issues"]})
    if outs or not codes:
        L.append("out = { " + ", ".join(f"{_q(k)} = {_q(v)}" for k, v in outs.items()) + " }" if outs else "out = {}")
    if codes:
        L.append("codes = [" + ", ".join(_q(c) for c in codes) + "]")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- ルール変更の影響（出力同士の比較）
def compare_outputs(h1: list[str], r1: list[list[str]], h2: list[str], r2: list[list[str]],
                    samples: int = 5) -> dict:
    """2つの出力（変更前 / 変更後）を行番号で突き合わせ、列の増減と値が変わったセルを数える。"""
    i1 = {c: i for i, c in enumerate(h1)}
    i2 = {c: i for i, c in enumerate(h2)}
    cols = []
    for c in h1:
        if c not in i2:
            continue
        a, b, n, sm = i1[c], i2[c], 0, []
        for row, (x, y) in enumerate(zip(r1, r2), start=1):
            vx = x[a] if a < len(x) else ""
            vy = y[b] if b < len(y) else ""
            if vx != vy:
                n += 1
                if len(sm) < samples:
                    sm.append({"row": row, "key": x[0] if x else "", "before": vx, "after": vy})
        if n:
            cols.append({"column": c, "changed": n, "samples": sm})
    added = [c for c in h2 if c not in i1]
    removed = [c for c in h1 if c not in i2]
    changed = sum(c["changed"] for c in cols)
    return {"same": not (added or removed or changed or len(r1) != len(r2)),
            "rows_before": len(r1), "rows_after": len(r2), "columns_added": added,
            "columns_removed": removed, "changed_cells": changed,
            "columns": sorted(cols, key=lambda d: -d["changed"])}


def impact(base: Config, new: Config, header: list[str], rows: list[list[str]]) -> dict:
    """同じ入力を 変更前の設定 / 変更後の設定 で変換し、出力と問題件数の違いを返す。"""
    h1, r1, s1 = convert_rows(base, header, rows)
    h2, r2, s2 = convert_rows(new, header, rows)
    res = compare_outputs(h1, r1, h2, r2)

    def cnt(iss):
        e = sum(1 for i in iss if i.severity == "E")
        return {"error": e, "warning": len(iss) - e}

    res["issues_before"], res["issues_after"] = cnt(s1), cnt(s2)
    k1 = Counter((i.column, i.out, i.code) for i in s1)
    k2 = Counter((i.column, i.out, i.code) for i in s2)
    res["issue_changes"] = [{"column": k[0], "out": k[1], "code": k[2], "before": k1[k], "after": k2[k]}
                            for k in sorted(set(k1) | set(k2)) if k1[k] != k2[k]]
    return res


# ---------------------------------------------------------------- suggest（修正案）
def _part_toml(fr: FieldRule | None, out: str) -> str:
    items = [f"out = {_q(out)}"]
    if fr is not None and fr.alts:
        fr = next(iter(fr.alts.values()))
    if fr is not None:
        if fr.number_only:
            items += ["number_only = true",
                      "strip_units = [" + ", ".join(_q(u) for u in sorted(fr.strip_units)) + "]"]
        if fr.to:
            items.append(f"to = {_q(fr.to)}")
            if fr.default_unit:
                items.append(f"default_unit = {_q(fr.default_unit)}")
            if fr.unit_out == "add":
                items.append('unit_out = "add"')
        if fr.decimals is not None:
            items.append(f"decimals = {fr.decimals}")
    return "{ " + ", ".join(items) + " }"


def _pattern_for(shape: str, examples: list[str]) -> tuple[str, list[str]] | None:
    """形をそのまま pattern として使えるか確かめる。使えれば (pattern, 数値名)。"""
    if any(ch in normalize_text(e) for e in examples for ch in "#[]{}"):
        return None
    try:
        rx, order = compile_pattern(shape)
    except (ValueError, re.error):
        return None
    c = re.compile(rx)
    if not all(c.search(normalize_text(e)) for e in examples):
        return None
    return shape, list(order)


def suggest(cfg: Config, header: list[str], rows: list[list[str]]) -> list[dict]:
    """問題のグループごとに、設定の修正案（TOML断片）を付ける。案は推測なので、
    適用したら check と test で確かめること。"""
    _, _, issues = convert_rows(cfg, header, rows)
    by_src: dict[str, list[FieldRule]] = {}
    for fr in cfg.fields:
        by_src.setdefault(fr.src, []).append(fr)
    out = []
    for g in group_issues([i for i in issues if i.code in
                           ("C101", "C102", "C103", "C104", "C111", "C121")]):
        frs = by_src.get(g["column"], [])
        target = next((f for f in frs if f.out == g["out"]), frs[0] if frs else None)
        sug = {"kind": "none", "toml": "", "note": "自動の案はありません。hint を参照", "confidence": "none"}
        multi = g["code"] == "C104" and g["shape"].count("#") > 1     # 単位違いではなく、数値が複数ある値
        if (g["code"] in ("C111", "C101") or multi) and target is not None:
            pat = _pattern_for(g["shape"], g["examples"])
            if pat:
                pattern, names = pat
                outs = list(dict.fromkeys(f.out for f in frs))
                matcher = bool(target.case_rx or target.group)
                parts = []
                for k, n in enumerate(names):
                    if matcher and k < len(outs):
                        parts.append(f"{n} = {_part_toml(next(f for f in frs if f.out == outs[k]), outs[k])}")
                    elif matcher:
                        parts.append(f"{n} = {_part_toml(None, g['column'] + '_' + n)}")
                    else:
                        parts.append(f"{n} = {_part_toml(target, g['column'] + '_' + n)}")
                body = f"pattern = {_q(pattern)}\nparts = {{ {', '.join(parts)} }}\n"
                conf = "medium" if len(names) == 1 else "low"   # 数値が複数あると、どれがどの列かは推測
                if target.case_rx:
                    sug = {"kind": "add_case", "toml": "[[field.case]]\n" + body, "confidence": conf,
                           "note": f"src = {g['column']!r} の [[field]] の最後に、この case を足す。"
                                   "部品と出力列の対応は左からの順で当てた推測なので確認する"}
                elif matcher:
                    sug = {"kind": "to_case", "toml": "[[field.case]]\n" + body, "confidence": conf,
                           "note": f"src = {g['column']!r} の [[field]] を case 形式に直す: 今の "
                                   "preset / match / pattern と parts を1つ目の [[field.case]] に移し、"
                                   "この case を2つ目に足す。部品と出力列の対応は推測なので確認する"}
                else:
                    sug = {"kind": "to_pattern", "toml": f"[[field]]\nsrc = {_q(g['column'])}\n" + body,
                           "confidence": "low",
                           "note": "1つの値に数値が複数ある。この列を pattern と parts で分ける案。"
                                   "出力列名は仮なので付け直す。他の形の値もあるなら case 形式にする"}
        elif g["code"] == "C104" and target is not None:
            m = re.search(r"指定外の単位 '(.+?)'", g["message"])
            fr = next(iter(target.alts.values())) if target.alts else target
            have = sorted(u for u in fr.strip_units if u != "*")
            if m and have:
                u = m.group(1)
                if cfg.units.known(u) and cfg.units.known(have[0]) and cfg.units.same_dim(u, have[0]):
                    sug = {"kind": "use_to", "confidence": "medium",
                           "toml": f"to = {_q(have[0])}\ndefault_unit = {_q(have[0])}\n",
                           "note": f"{u} は {have[0]} に換算できる。number_only / strip_units をやめて "
                                   f"to で {have[0]} に揃える（{u} の値は換算される）"}
                else:
                    sug = {"kind": "add_strip_unit", "confidence": "low",
                           "toml": "strip_units = [" + ", ".join(_q(x) for x in [*have, u]) + "]\n",
                           "note": f"{u} を捨ててよい単位に足す案。{have[0]} と {u} の数値が同じ列に混ざるので、"
                                   "意味が同じ場合だけ使う"}
        elif g["code"] == "C102" and target is not None:
            fr = next(iter(target.alts.values())) if target.alts else target
            if fr.to:
                sug = {"kind": "add_default_unit", "toml": f"default_unit = {_q(fr.to)}\n", "confidence": "low",
                       "note": f"単位なしの値を {fr.to} とみなす案。本当に {fr.to} の値か確認する"}
        elif g["code"] == "C121" and target is not None and target.synonyms:
            table = cfg.synonyms.get(target.synonyms, {})
            canon = sorted(set(table.values()))
            ex = g["examples"][0] if g["examples"] else ""
            near = difflib.get_close_matches(fold_key(ex), [fold_key(c) for c in canon], n=1, cutoff=0.6)
            if near:
                c = next(c for c in canon if fold_key(c) == near[0])
                sug = {"kind": "add_synonym", "toml": f"# [synonyms.{target.synonyms}] の {_q(c)} の配列に足す\n"
                                                      f"{_q(c)} = [..., {_q(ex)}]\n",
                       "confidence": "low", "note": f"{ex!r} は {c!r} の表記ゆれに見える。同じものか確認する"}
        out.append({**g, "suggestion": sug})
    return out


# ---------------------------------------------------------------- draft（ルールの下書き）
def _q(s: str) -> str:
    """TOML の文字列。JSON の書き方がそのまま使えるが、DEL(0x7f) だけは TOML では生で書けない。"""
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _c(s) -> str:
    """TOML のコメントに入れる文字列。改行・制御文字があると、コメントが途中で切れて設定として読めなくなる。"""
    return re.sub(r"[\x00-\x1f\x7f\u0085\u2028\u2029]", " ", str(s))


def draft_toml(cfg: Config, header: list[str], rows: list[list[str]]) -> str:
    """CSVの実態からルールの下書きを作る。必ず人/agentが見直し、check で検証すること。"""
    uh, _ = uniquify(header)
    syn_blocks: list[str] = []
    field_blocks: list[str] = []
    notes: list[str] = []
    for i, col in enumerate(uh):
        if i == 0:
            continue
        vals = Counter(normalize_text(r[i]) for r in rows if i < len(r))
        vals = Counter({v: n for v, n in vals.items() if v and not _is_reserved(cfg, v)})
        if not vals:
            continue
        total = sum(vals.values())
        units: Counter = Counter()
        parsed = 0
        for v, n in vals.items():
            try:
                _, u = parse_quantity(v, cfg.units)
                parsed += n
                if u:
                    units[u] += n
            except UnitError:
                pass
        known = {u: n for u, n in units.items() if cfg.units.known(u)}
        if parsed / total >= 0.8 and known:
            to = max(known, key=known.get)
            field_blocks.append(
                f"[[field]]\nsrc = {_q(col)}\nto = {_q(to)}\ndefault_unit = {_q(to)}   # 単位なしの値は{_c(to)}とみなす\n"
                f"# 検出された単位: {_c(', '.join(f'{u}×{n}' for u, n in units.most_common()))}\n")
            continue
        allc = synonym_candidates(vals)
        cands = [c for c in allc if c["kind"] == "exact"]   # 下書きに載せるのは機械的に同一視できるものだけ
        for c in allc:
            if c["kind"] != "exact":
                notes.append(_c(f"# {col}: 表記ゆれの疑い（要確認）{c['canonical']!r} ← {c['variants']}"))
        if cands:
            name = col
            body = "".join(f"{_q(c['canonical'])} = [{', '.join(_q(v) for v in c['variants'])}]"
                           f"   # {_c(c['kind'])}・{c['count']}件\n" for c in cands)
            syn_blocks.append(f"[synonyms.{_q(name)}]\n{body}")
            field_blocks.append(f"[[field]]\nsrc = {_q(col)}\nsynonyms = {_q(name)}\non_unknown = \"warn\"\n")
            continue
        comp = [v for v in vals if any(ch in v for ch in "±×～~") and any(c.isdigit() for c in v)]
        if comp:
            notes.append(_c(f"# {col}: 複合表記（例 {comp[0]!r}）。match と parts で分解する"))
    head = ("# tenugui draft — 自動生成の下書き。そのまま使わず、内容を確認して check で検証すること\n"
            "schema_version = 1\n\n")
    return head + "".join(b + "\n" for b in syn_blocks) + "".join(b + "\n" for b in field_blocks) \
        + ("\n".join(notes) + "\n" if notes else "")


# ---------------------------------------------------------------- try（1行だけ試す）
def try_row(cfg: Config, values: dict[str, str]) -> dict:
    """CSVなしで値を試す。関係するルールだけ実行し、各段階の途中経過を返す。"""
    header = list(values)
    kept_f = [f for f in cfg.fields if f.src in values]
    produced = {f.out for f in kept_f} | set(header)
    kept_d, skipped_d = [], []
    for d in cfg.derives:
        if all(col in produced for col, _ in d.vars.values()):
            kept_d.append(d)
            produced.add(d.out)
        else:
            skipped_d.append(d.out)
    sub = replace(cfg, fields=kept_f, derives=kept_d)
    prov = Provenance()
    cols, rows, issues = convert_rows(sub, header, [[values[h] for h in header]], prov)
    traces = {}
    for f in kept_f:
        t: list = []
        raw = values[f.src]
        if (1, f.src) in prov.held:
            t.append(("hold", f"保留（{prov.held[(1, f.src)][1].reason}）"))
        else:
            if (1, f.src) in prov.corrected:
                e = prov.corrected[(1, f.src)][1]
                t.append(("correct", f"{raw} → {e.to}（{e.reason}）"))
                raw = e.to
            _apply_field(cfg, f, raw, {}, (1, ""), [], trace=t)
        traces[f.out] = [{"stage": a, "value": b} for a, b in t]
    return {"outputs": dict(zip(cols, rows[0])), "issues": [issue_dict(i) for i in issues],
            "trace": traces, "skipped_fields": [f.out for f in cfg.fields if f.src not in values],
            "skipped_derives": skipped_d}


# ---------------------------------------------------------------- 変換前後チェック
def verify_before_after(cfg: Config, header: list[str], rows: list[list[str]],
                        out_header: list[str], out_rows: list[list[str]],
                        issues: list[Issue], corrected: set | None = None) -> dict:
    """変換の前後を突き合わせて「意図しない変化」を探す。

    violations（あってはならない）:
      row_count        行数が変わった
      untouched_changed ルール対象外の列（変換しない列・out違いの元列）の値が変わった
                       （corrected = 訂正ファイルで訂正した (行, 列) は意図した変更なので除く）
      ng_altered       chozuの抽出NG印が変わった
      column_missing   入力列が出力から消えた（drop_src / passthrough=false で意図したものを除く）
    warnings:
      lost             値があったセルが空になった（置換列）
    """
    uh, _ = uniquify(header)
    iidx = {h: i for i, h in enumerate(uh)}
    oidx = {h: i for i, h in enumerate(out_header)}
    replaced = {f.src for f in cfg.fields if f.out == f.src}
    corrected = corrected or set()
    dropped = {f.src for f in cfg.fields if f.drop_src}
    derived_or_new = {f.out for f in cfg.fields if f.out != f.src} | {d.out for d in cfg.derives}
    err_cells: Counter = Counter()
    for i in issues:
        if i.severity == "E":
            err_cells[(i.column if i.out in ("", i.column) else i.column, i.row)] += 1
    violations: list[dict] = []
    if len(rows) != len(out_rows):
        violations.append({"kind": "row_count", "column": "", "row": 0,
                           "before": str(len(rows)), "after": str(len(out_rows))})
    for c in uh:
        gone = c not in oidx
        intended = c in dropped or (not cfg.passthrough and c not in cfg.keep and c not in replaced)
        if gone and not intended:
            violations.append({"kind": "column_missing", "column": c, "row": 0, "before": "", "after": ""})
    cols = []
    for c in uh:
        if c not in oidx:
            continue
        i, j = iidx[c], oidx[c]
        st = {"column": c, "ruled": c in replaced, "changed": 0, "lost": 0, "errors": 0, "sample": []}
        for n, (a, b) in enumerate(zip(rows, out_rows), start=1):
            before = a[i] if i < len(a) else ""
            after = b[j]
            st["errors"] += 1 if err_cells.get((c, n)) else 0
            if before == after:
                continue
            st["changed"] += 1
            if _is_reserved(cfg, before):
                violations.append({"kind": "ng_altered", "column": c, "row": n, "before": before, "after": after})
            elif c not in replaced and (n, c) not in corrected:
                violations.append({"kind": "untouched_changed", "column": c, "row": n,
                                   "before": before, "after": after})
            if before.strip() and not after.strip() and not is_null(cfg, before):   # null_values は意図した空
                st["lost"] += 1
            if len(st["sample"]) < 3:
                st["sample"].append([before, after])
        cols.append(st)
    new_cols = [{"column": c, "filled": sum(1 for r in out_rows if r[oidx[c]].strip()),
                 "rows": len(out_rows)} for c in out_header if c in derived_or_new]
    dropped_cols = []
    for c in uh:
        if c not in oidx and (c in dropped or (not cfg.passthrough and c not in cfg.keep and c not in replaced)):
            failed = len({i.row for i in issues if i.column == c and i.severity == "E"})
            dropped_cols.append({"column": c, "failed_rows": failed})
    summary = {"rows_in": len(rows), "rows_out": len(out_rows),
               "violations": len(violations),
               "lost_cells": sum(c["lost"] for c in cols),
               "changed_cells": sum(c["changed"] for c in cols),
               "error_cells": sum(c["errors"] for c in cols)}
    summary["dropped_columns"] = len(dropped_cols)
    return {"ok": not violations, "summary": summary, "violations": violations,
            "columns": cols, "new_columns": new_cols, "dropped_columns": dropped_cols}


# ---------------------------------------------------------------- 実行ログ
def write_log(path: str | Path, info: dict) -> None:
    """人が読めるプレーンテキストの実行ログ（何を、どの設定で、どう処理したか）。"""
    import datetime
    from . import __version__
    L = [f"tenugui {__version__}  {datetime.datetime.now().isoformat(timespec='seconds')}",
         f"コマンド: {info.get('command', '')}",
         f"入力: {info['input']}",
         f"設定: {info['config']}  (sha256-12: {info.get('config_sha', '')})",
         f"列構成sha: {info.get('header_sha', '')}",
         f"行数: {info['rows']}   エラー {info['errors']} 件 / 警告 {info['warnings']} 件",
         "出力ファイル:"]
    L += [f"  {k}: {v}" for k, v in info["files"].items()]
    v = info.get("verification")
    if v:
        s = v["summary"]
        L += ["", f"[変換前後チェック] {'OK' if v['ok'] else 'NG'}  行 {s['rows_in']}→{s['rows_out']} / "
                  f"変更セル {s['changed_cells']} / 欠落 {s['lost_cells']} / 違反 {s['violations']}"]
        L += [f"  違反 {x['kind']} {x['column']} {x['row']}行: {x['before']!r} → {x['after']!r}"
              for x in v["violations"][:200]]
    for d in (v or {}).get("dropped_columns", []):
        L.append(f"削除した元の列: {d['column']}"
                 + (f"（変換に失敗した行 {d['failed_rows']} 件。元の値はレポートの問題一覧 raw に記録）"
                    if d["failed_rows"] else ""))
    pv = info.get("provenance")
    if pv and pv["corrections_file"]:
        L += ["", f"[訂正・保留] {pv['corrections_file']}  (sha256-12: {pv['corrections_sha']})  "
                  f"訂正 {pv['counts']['correct']} セル / 保留 {pv['counts']['hold']} セル"]
        L += [f"  {KIND_LABEL[g['kind']]} {g['column']}: {g['value']!r}" + (f" → {g['to']!r}" if g["to"] else "")
              + f"  {g['count']} 件  理由: {g['reason']}" for g in pv["fix_requests"]]
        L += [f"  注意 {u['code']} {u['where']}: {u['message']}" for u in pv["unused"]]
    for b in info.get("not_idempotent", []):
        L.append(f"注意 L102 再実行で値が変わる列 {b['column']}: {b['first']!r} → {b['second']!r}")
    for w in info.get("lint", []):
        L.append(f"注意 {w['code']} {w['where']}: {w['message']}")
    d = info.get("drift")
    if d and d["changed"]:
        L.append(f"注意 C003 前回(lock)から列が変わっています 増:{d['added']} 減:{d['removed']}")
    if info.get("groups"):
        L += ["", "[問題の内訳（列 × コード × 形）]"]
        L += [f"  {g['count']} 件 {g['code']} {g['out'] or g['column']} 形 {g['shape']} 例 {g['examples']}"
              for g in info["groups"][:100]]
    for c in info.get("coverage", []):
        L.append(f"書式の内訳 {c['column']}: " + " / ".join(f"{x['label']} {x['count']}件" for x in c["cases"])
                 + f" / 不一致 {c['unmatched']}件")
    L += ["", "[問題一覧]"]
    L += [f"  {i['severity']} {i['code']} {i['row']}行 {i['key']} {i['out'] or i['column']}: "
          f"{i['message']} (値: {i['raw']!r})" for i in info["issues"]] or ["  なし"]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def verify_files(cfg: Config, header: list[str], rows: list[list[str]], out_path: str | Path) -> dict:
    """ディスク上の出力CSVを、入力と設定から作り直した結果と突き合わせて検証する。
    出力が古い・手で編集された場合は output_differs を報告する。"""
    from .core import read_csv
    oh, orows = read_csv(out_path, cfg.encoding_out)
    prov = Provenance()
    ph, prows, issues = convert_rows(cfg, header, rows, prov)
    v = verify_before_after(cfg, header, rows, oh, orows, issues, set(prov.corrected))
    diff = None
    if oh != ph:
        diff = {"column": "(ヘッダ)", "row": 0, "before": ",".join(ph)[:60], "after": ",".join(oh)[:60]}
    elif orows != prows:
        for n, (a, b) in enumerate(zip(prows, orows), start=1):
            for c, x, y in zip(ph, a, b):
                if x != y and diff is None:
                    diff = {"column": c, "row": n, "before": x, "after": y}
        diff = diff or {"column": "", "row": 0, "before": f"{len(prows)}行", "after": f"{len(orows)}行"}
    if diff:
        v["violations"].append({"kind": "output_differs", **diff})
        v["summary"]["violations"] = len(v["violations"])
        v["ok"] = False
    return v


# ---------------------------------------------------------------- 変換の実行（CLI/UI共通）
def _write_diff(path: Path, changes: list[dict], enc: str) -> None:
    import csv
    with open(path, "w", newline="", encoding=enc) as f:
        w = csv.writer(f)
        w.writerow(["row", "key", "column", "before", "after"])
        for c in changes:
            w.writerow([c["row"], c["key"], c["column"], c["before"], c["after"]])


def _write_fix(path: Path, cells: list[dict], enc: str) -> None:
    """元データの修正依頼リスト: 訂正・保留のかかったセルを1行ずつ。"""
    import csv
    with open(path, "w", newline="", encoding=enc) as f:
        w = csv.writer(f)
        w.writerow(["種類", "row", "key", "column", "元の値", "訂正後", "理由", "根拠"])
        for c in cells:
            w.writerow([KIND_LABEL[c["kind"]], c["row"], c["key"], c["column"], c["before"], c["after"],
                        c["reason"], c["ref"]])


class _Staged:
    """出力ファイルをいったん仮の名前で書き、全部書けてから本当の名前にする。
    途中で失敗したとき（書けない文字、ディスクの空き）に、書きかけのファイルや古い結果との混在を残さない。"""

    def __init__(self) -> None:
        self.pairs: list[tuple[Path, Path]] = []

    def path(self, final: Path) -> Path:
        tmp = final.with_name(final.name + ".writing")
        self.pairs.append((tmp, final))
        return tmp

    def commit(self) -> None:
        try:
            for tmp, final in self.pairs:
                os.replace(tmp, final)
        finally:
            self.discard()

    def discard(self) -> None:
        for tmp, _ in self.pairs:
            try:
                tmp.unlink()
            except OSError:
                pass


def output_files(out: Path) -> list[Path]:
    """出力CSV out と一緒に書く（ことがある）ファイル。上書きしてはいけない相手との照合に使う。"""
    return [out, out.with_suffix(".html"), out.with_suffix(".json"), out.with_suffix(".log"),
            *(out.with_name(out.stem + x) for x in ("_fix.csv", "_conv.csv", "_diff.csv"))]


def convert_to_files(cfg: Config, config_path: str, input_path: str, header: list[str],
                     rows: list[list[str]], out: Path, *, no_diff: bool = False,
                     verify: bool = False, command: str = "", detail_files: bool = False,
                     report_unused: bool = True) -> dict:
    """変換して、出力CSV・HTMLレポート（人が見る）・JSONレポート（機械が読む）を out と同じフォルダに書く。
    訂正ファイルを使っているときだけ、修正依頼リスト <名>_fix.csv も書く。
    detail_files=True で、従来の 変換レポートCSV・差分CSV・ログ も書く。印字はしない。
    report_unused=False は、複数ファイルの一括変換用（当たらなかった訂正・保留は全ファイルを見てから判断する）。"""
    from . import __version__
    from .core import outputs_from, write_csv, write_report
    from .report import build_report, render_html, write_json
    uh, dups = uniquify(header)
    drift = header_drift(config_path, header) if config_path else None
    prov = Provenance()
    out_header, out_rows, issues = convert_rows(cfg, header, rows, prov)
    out.parent.mkdir(parents=True, exist_ok=True)
    st = _Staged()
    try:
        res = _write_outputs(st, cfg, config_path, input_path, header, rows, out, out_header, out_rows, issues,
                             prov, uh, dups, drift, no_diff, verify, command, detail_files, report_unused)
        st.commit()
        return res
    except UnicodeEncodeError as e:
        st.discard()
        raise ConfigError([f"[settings] encoding_out = {cfg.encoding_out!r} では書けない文字 "
                           f"{e.object[e.start:e.end]!r} があります。出力は書いていません。encoding_out を \"utf-8-sig\"（既定）に"
                           "すると、どの文字も書けます"]) from None
    except BaseException:
        st.discard()
        raise


def _write_outputs(st: _Staged, cfg: Config, config_path: str, input_path: str, header: list[str],
                   rows: list[list[str]], out: Path, out_header: list[str], out_rows: list[list[str]],
                   issues: list[Issue], prov: Provenance, uh: list[str], dups: list[str], drift: dict | None,
                   no_diff: bool, verify: bool, command: str, detail_files: bool, report_unused: bool) -> dict:
    from . import __version__
    from .core import outputs_from, write_csv, write_report
    from .report import build_report, render_html, write_json
    write_csv(st.path(out), out_header, out_rows, cfg.encoding_out)
    htmlp, jsonp = out.with_suffix(".html"), out.with_suffix(".json")
    files = {"output": str(out), "html": str(htmlp), "json": str(jsonp)}
    changes = diff_changes(header, rows, out_header, out_rows)
    pv = summarize(cfg.corrections, prov, changes, uh, report_unused, lambda c: outputs_from(cfg, c))
    cells = pv["cells"]
    fixp = out.with_name(out.stem + "_fix.csv")
    if cfg.corrections is not None:     # 訂正ファイルを使っているときだけ書く（当たりが無ければ見出しだけ）
        _write_fix(st.path(fixp), cells, cfg.encoding_out)
        files["fix"] = str(fixp)
    ver = verify_before_after(cfg, header, rows, out_header, out_rows, issues, set(prov.corrected))
    idem = idempotency_report(cfg, header, rows) if verify else []
    idicts = [issue_dict(i) for i in issues]
    e = sum(1 for i in issues if i.severity == "E")
    sha = ""
    if config_path and Path(config_path).exists():
        sha = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()[:12]
    groups, cover = group_issues(issues), case_coverage(cfg, header, rows)
    if detail_files:
        rep = out.with_name(out.stem + "_conv.csv")
        write_report(st.path(rep), issues, cfg.encoding_out)
        files["report"] = str(rep)
        if not no_diff:
            dp = out.with_name(out.stem + "_diff.csv")
            _write_diff(st.path(dp), changes, cfg.encoding_out)
            files["diff"] = str(dp)
        files["log"] = str(out.with_suffix(".log"))
        write_log(st.path(Path(files["log"])), {"command": command, "input": input_path, "config": config_path or "(設定なし)",
                                 "config_sha": sha, "header_sha": header_sha(uh), "rows": len(out_rows),
                                 "errors": e, "warnings": len(issues) - e, "files": files, "verification": ver,
                                 "not_idempotent": idem, "lint": lint(cfg), "drift": drift, "issues": idicts,
                                 "groups": groups, "coverage": cover, "provenance": pv})
    report = build_report(version=__version__, command=command, input_path=input_path,
                          config_path=config_path or "(設定なし)", config_sha=sha, header_sha=header_sha(uh),
                          rows=len(out_rows), errors=e, warnings=len(issues) - e, files=files, groups=groups,
                          coverage=cover, issues=idicts, changes=changes, verification=ver,
                          not_idempotent=idem, lint=lint(cfg), drift=drift, provenance=pv)
    write_json(st.path(jsonp), report)
    st.path(htmlp).write_text(render_html(report, cfg, header, rows, out_header, out_rows, issues, prov), encoding="utf-8")
    return {"out_header": out_header, "out_rows": out_rows, "issues": issues, "idicts": idicts,
            "changes": changes, "verification": ver, "not_idempotent": idem, "drift": drift,
            "files": files, "errors": e, "warnings": len(issues) - e, "duplicates": dups,
            "header_sha": header_sha(uh), "groups": groups, "coverage": cover, "provenance": pv,
            "unused": unused_entries(cfg.corrections, prov, uh)}


SHAPE_HEADER = ["列", "形の種類", "件数", "形", "例"]


def shape_table(cfg: Config, header: list[str], rows: list[list[str]], all_columns: bool = False,
                top: int = 50) -> list[tuple]:
    """UI「形」タブの表。行 = (列, 形の種類数, 件数, 形, 例)。列名・種類数は各列の先頭行だけに入る。

    all_columns=False は「数値を含み、形が2種類以上ある列」だけ（survey の既定と同じ絞り込み）。
    """
    out = []
    for o in survey_overview(cfg, header, rows, top=top):
        if not all_columns and not (o["shape_count"] > 1 and any("#" in x["shape"] for x in o["shapes"])):
            continue
        for k, sh in enumerate(o["shapes"]):
            out.append((o["column"] if k == 0 else "", o["shape_count"] if k == 0 else "", sh["count"],
                        sh["shape"], " / ".join(sh["examples"])))
    return out

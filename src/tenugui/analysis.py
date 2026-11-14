"""検証・診断・下書き生成など、変換本体の周辺機能（CLI/UI/agent 共通で使う）。"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .core import (Config, Issue, _apply_field, _is_reserved, check_header, convert_rows,
                   survey as _survey, uniquify)
from .units import UnitError, UnitTable, fold_key, normalize_text, parse_quantity

# ---------------------------------------------------------------- 修正のヒント
HINTS = {
    "C001": "入力CSVに無い列です。列名の綴り・chozu側の列名変更を確認し、field の src / vars を直す",
    "C002": "出力列名が既存列と衝突。out を別名にする",
    "C101": "数値+単位として読めない。複合表記なら match/parts か extract/sub で数値部分だけ取り出す",
    "C102": "単位が無い値。default_unit を指定するか、入力側に単位を付ける",
    "C103": "未定義の単位か次元違い。[units.*] / [unit_alias] に追加するか、to を見直す",
    "C111": "match/extract が外れた。tenugui try で値を試し、正規表現を直す",
    "C121": "同義語表に無い値。[synonyms.*] に追加するか on_unknown を見直す（survey の candidates 参照）",
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
def lint(cfg: Config) -> list[dict]:
    out: list[dict] = []
    for fr in cfg.fields:
        if fr.out == fr.src and fr.to and fr.unit_out == "strip" and not fr.default_unit:
            out.append({"code": "L101", "where": fr.src,
                        "message": "出力は単位なしの数値になるため、変換済みCSVをもう一度通すとC102になる。"
                                   "default_unit を指定すると再実行できる"})
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
    old = json.loads(p.read_text(encoding="utf-8"))
    uh, _ = uniquify(header)
    added = [c for c in uh if c not in old["columns"]]
    removed = [c for c in old["columns"] if c not in uh]
    return {"changed": bool(added or removed or uh != old["columns"]), "added": added,
            "removed": removed, "sha": header_sha(uh), "locked_sha": old["sha"]}


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
    s["candidates"] = synonym_candidates(textual)
    s["composite_examples"] = [v for v in vals if any(ch in v for ch in "±×～~") and any(c.isdigit() for c in v)][:3]
    return s


# ---------------------------------------------------------------- draft（ルールの下書き）
def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


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
                f"[[field]]\nsrc = {_q(col)}\nto = {_q(to)}\ndefault_unit = {_q(to)}   # 単位なしの値は{to}とみなす\n"
                f"# 検出された単位: {', '.join(f'{u}×{n}' for u, n in units.most_common())}\n")
            continue
        allc = synonym_candidates(vals)
        cands = [c for c in allc if c["kind"] == "exact"]   # 下書きに載せるのは機械的に同一視できるものだけ
        for c in allc:
            if c["kind"] != "exact":
                notes.append(f"# {col}: 表記ゆれの疑い（要確認）{c['canonical']!r} ← {c['variants']}")
        if cands:
            name = col
            body = "".join(f"{_q(c['canonical'])} = [{', '.join(_q(v) for v in c['variants'])}]"
                           f"   # {c['kind']}・{c['count']}件\n" for c in cands)
            syn_blocks.append(f"[synonyms.{_q(name)}]\n{body}")
            field_blocks.append(f"[[field]]\nsrc = {_q(col)}\nsynonyms = {_q(name)}\non_unknown = \"warn\"\n")
            continue
        comp = [v for v in vals if any(ch in v for ch in "±×～~") and any(c.isdigit() for c in v)]
        if comp:
            notes.append(f"# {col}: 複合表記（例 {comp[0]!r}）。match と parts で分解する")
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
    cols, rows, issues = convert_rows(sub, header, [[values[h] for h in header]])
    traces = {}
    for f in kept_f:
        t: list = []
        _apply_field(cfg, f, values[f.src], {}, (1, ""), [], trace=t)
        traces[f.out] = [{"stage": a, "value": b} for a, b in t]
    return {"outputs": dict(zip(cols, rows[0])), "issues": [issue_dict(i) for i in issues],
            "trace": traces, "skipped_fields": [f.out for f in cfg.fields if f.src not in values],
            "skipped_derives": skipped_d}


# ---------------------------------------------------------------- 変換前後チェック
def verify_before_after(cfg: Config, header: list[str], rows: list[list[str]],
                        out_header: list[str], out_rows: list[list[str]],
                        issues: list[Issue]) -> dict:
    """変換の前後を突き合わせて「意図しない変化」を探す。

    violations（あってはならない）:
      row_count        行数が変わった
      untouched_changed ルール対象外の列（変換しない列・out違いの元列）の値が変わった
      ng_altered       chozuの抽出NG印が変わった
      column_missing   入力列が出力から消えた（drop_src / passthrough=false で意図したものを除く）
    warnings:
      lost             値があったセルが空になった（置換列）
    """
    uh, _ = uniquify(header)
    iidx = {h: i for i, h in enumerate(uh)}
    oidx = {h: i for i, h in enumerate(out_header)}
    replaced = {f.src for f in cfg.fields if f.out == f.src}
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
            elif c not in replaced:
                violations.append({"kind": "untouched_changed", "column": c, "row": n,
                                   "before": before, "after": after})
            if before.strip() and not after.strip():
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
                 + (f"（変換に失敗した行 {d['failed_rows']} 件。元の値は *_conv.csv の raw に記録）"
                    if d["failed_rows"] else ""))
    for b in info.get("not_idempotent", []):
        L.append(f"注意 L102 再実行で値が変わる列 {b['column']}: {b['first']!r} → {b['second']!r}")
    for w in info.get("lint", []):
        L.append(f"注意 {w['code']} {w['where']}: {w['message']}")
    d = info.get("drift")
    if d and d["changed"]:
        L.append(f"注意 C003 前回(lock)から列が変わっています 増:{d['added']} 減:{d['removed']}")
    L += ["", "[問題一覧]"]
    L += [f"  {i['severity']} {i['code']} {i['row']}行 {i['key']} {i['out'] or i['column']}: "
          f"{i['message']} (値: {i['raw']!r})" for i in info["issues"]] or ["  なし"]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def verify_files(cfg: Config, header: list[str], rows: list[list[str]], out_path: str | Path) -> dict:
    """ディスク上の出力CSVを、入力と設定から作り直した結果と突き合わせて検証する。
    出力が古い・手で編集された場合は output_differs を報告する。"""
    from .core import read_csv
    oh, orows = read_csv(out_path, cfg.encoding_out)
    ph, prows, issues = convert_rows(cfg, header, rows)
    v = verify_before_after(cfg, header, rows, oh, orows, issues)
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


def convert_to_files(cfg: Config, config_path: str, input_path: str, header: list[str],
                     rows: list[list[str]], out: Path, *, no_diff: bool = False,
                     verify: bool = False, command: str = "") -> dict:
    """変換して 出力CSV・変換レポート・差分・ログ を out と同じフォルダに書く。印字はしない。"""
    from .core import write_csv, write_report
    uh, dups = uniquify(header)
    drift = header_drift(config_path, header) if config_path else None
    out_header, out_rows, issues = convert_rows(cfg, header, rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(out, out_header, out_rows, cfg.encoding_out)
    rep = out.with_name(out.stem + "_conv.csv")
    write_report(rep, issues, cfg.encoding_out)
    logp = out.with_suffix(".log")
    files = {"output": str(out), "report": str(rep), "log": str(logp)}
    changes = diff_changes(header, rows, out_header, out_rows)
    if not no_diff:
        dp = out.with_name(out.stem + "_diff.csv")
        _write_diff(dp, changes, cfg.encoding_out)
        files["diff"] = str(dp)
    ver = verify_before_after(cfg, header, rows, out_header, out_rows, issues)
    idem = idempotency_report(cfg, header, rows) if verify else []
    idicts = [issue_dict(i) for i in issues]
    e = sum(1 for i in issues if i.severity == "E")
    sha = ""
    if config_path and Path(config_path).exists():
        sha = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()[:12]
    write_log(logp, {"command": command, "input": input_path, "config": config_path or "(設定なし)",
                     "config_sha": sha, "header_sha": header_sha(uh), "rows": len(out_rows),
                     "errors": e, "warnings": len(issues) - e, "files": files, "verification": ver,
                     "not_idempotent": idem, "lint": lint(cfg), "drift": drift, "issues": idicts})
    return {"out_header": out_header, "out_rows": out_rows, "issues": issues, "idicts": idicts,
            "changes": changes, "verification": ver, "not_idempotent": idem, "drift": drift,
            "files": files, "errors": e, "warnings": len(issues) - e, "duplicates": dups,
            "header_sha": header_sha(uh)}

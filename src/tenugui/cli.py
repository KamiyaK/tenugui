"""コマンドライン: convert / check / survey / draft / try / test / suggest / impact / compare / verify / glossary / schema / lock / ui

人向けは既定のテキスト、agent向けは --format json（スキーマ名 tenugui.*/1）。
終了コード: 0=OK  1=--fail-on の基準に該当  2=設定・使い方のエラー
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

from .analysis import (SHAPE_HEADER, case_coverage, compare_outputs, convert_to_files, diff_changes, draft_toml,
                       group_issues, header_drift, header_sha, idempotency_report, impact, issue_dict, lint, shape_table,
                       lock_path, run_tests, suggest, survey_full, survey_overview, as_test_block, try_row,
                       verify_before_after, verify_files, write_lock, write_log)
from .core import (ConfigError, check_header, config_from_dict, convert_rows, did_you_mean, load_config,
                   read_csv, uniquify, write_csv, write_report)
from .corrections import KIND_LABEL, Provenance
from .export import export_tables

SCHEMA = "tenugui.{}/1"


def _cfg(args):
    return _load(args) if args.config else config_from_dict({"schema_version": 1})


def _load(args):
    """ルールTOMLを読む。--corrections があればその訂正ファイル、無ければ <ルール名>.corrections.toml（あれば）。"""
    return load_config(args.config, getattr(args, "corrections", None))


def _json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _failing(fail_on: str, errors: int, warnings: int, holds: int = 0) -> bool:
    """hold = エラーか、保留（判断待ちで変換していない値）が1つでもあれば該当。"""
    return ((fail_on in ("error", "hold") and errors > 0) or (fail_on == "hold" and holds > 0)
            or (fail_on == "warn" and errors + warnings > 0))


def _where(msg: str) -> dict:
    head, sep, rest = msg.partition(": ")
    if sep and len(head) < 80 and (head.startswith("[") or head.startswith("トップ")):
        return {"where": head, "message": rest}
    return {"where": "", "message": msg}


# ---------------------------------------------------------------- check
def cmd_check(args) -> int:
    cfg = _load(args)
    probs, dups, drift, idem = [], [], None, []
    if args.input:
        header, rows = read_csv(args.input, cfg.encoding_in)
        uh, dups = uniquify(header)
        probs = check_header(cfg, uh)
        drift = header_drift(args.config, header)
        if args.idempotent:
            idem = idempotency_report(cfg, header, rows)
    warns = lint(cfg)
    if args.input and cfg.corrections:
        warns = warns + [{"code": "L303", "where": e.where,
                          "message": f"訂正・保留の列 {e.column!r} が入力にありません{did_you_mean(e.column, uh)}"}
                         for e in cfg.corrections.entries if e.column not in uh]
    tests = run_tests(cfg)
    errors = len(probs) + tests["failed"]
    nwarn = len(warns) + len(idem) + (1 if drift and drift["changed"] else 0)
    bad = _failing(args.fail_on, errors, nwarn)
    if args.format == "json":
        _json({"schema": SCHEMA.format("check"), "ok": not bad, "fields": len(cfg.fields),
               "derives": len(cfg.derives), "corrections": _cor_info(cfg), "input_problems": probs, "lint": warns,
               "duplicate_columns": dups, "header_drift": drift, "not_idempotent": idem, "tests": tests})
    else:
        print(f"設定OK: field {len(cfg.fields)} 件 / derive {len(cfg.derives)} 件")
        ci = _cor_info(cfg)
        if ci:
            print(f"訂正ファイル: {ci['file']}（訂正 {ci['correct']} 件 / 保留 {ci['hold']} 件）")
        if tests["total"]:
            print(f"テスト: {tests['total'] - tests['failed']}/{tests['total']} 件 合格")
        _print_failures(tests)
        for d in dups:
            print(f"注意: 列名 {d!r} が重複しているため #2, #3... を付けて区別します")
        for p in probs:
            print(f"NG {p}")
        for w in warns:
            print(f"注意 {w['code']} {w['where']}: {w['message']}")
        if drift and drift["changed"]:
            print(f"注意 C003 前回(lock)から列が変わっています 増:{drift['added']} 減:{drift['removed']}")
        for b in idem:
            print(f"注意 L102 再実行で値が変わる列 {b['column']}: {b['first']!r} → {b['second']!r}（{b['row']}行目）")
    return 1 if bad else 0


def _cor_info(cfg) -> dict | None:
    c = cfg.corrections
    if c is None:
        return None
    return {"file": c.path, "sha": c.sha, "correct": sum(1 for e in c.entries if e.kind == "correct"),
            "hold": sum(1 for e in c.entries if e.kind == "hold")}


def _print_failures(tests: dict) -> None:
    for f in tests["failures"]:
        print(f"NG テスト {f['name']}: 入力 {f['in']}")
        for d in f["diffs"]:
            print(f"     {d['column']}: 期待 {d['expected']!r} / 実際 {d['actual']!r}")


def _group_lines(groups: list[dict], limit: int = 10) -> list[str]:
    L = [f"  {g['count']:5d} 件  {g['code']} {g['column']}"
         + (f" → {g['out']}" if g["out"] and g["out"] != g["column"] else "")
         + (f"  形 {g['shape']}" if g["shape"] else "") + (f"  例 {g['examples']}" if g["examples"] else "")
         for g in groups[:limit]]
    if len(groups) > limit:
        L.append(f"  …ほか {len(groups) - limit} グループ")
    return L


# ---------------------------------------------------------------- convert
OUT_DIR_NAME = "tenugui_out"


def default_out_dir(input_path: str | Path) -> Path:
    return Path(input_path).resolve().parent / OUT_DIR_NAME


def resolve_output(input_path: str, output: str | None, out_dir: str | None, stamp: bool) -> Path:
    """出力CSVのパス。既定は 入力CSVのあるフォルダの tenugui_out/ 。無ければ作る。"""
    d = Path(out_dir) if out_dir else default_out_dir(input_path)
    if output:
        out = Path(output)
        if out.parent == Path("."):
            out = d / out.name
    else:
        out = d / f"{Path(input_path).stem}_tenugui.csv"
    if stamp:
        out = out.with_name(f"{out.stem}_{datetime.datetime.now():%Y%m%d_%H%M%S}{out.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _inputs(args) -> list[str]:
    """convert の入力。フォルダを渡すと、その中の *.csv（tenugui_out は除く）。"""
    res: list[str] = []
    for a in args.input:
        pa = Path(a)
        if pa.is_dir():
            res += [str(x) for x in sorted(pa.glob("*.csv"))]
        else:
            res.append(a)
    if not res:
        raise ConfigError([f"入力CSVがありません: {', '.join(args.input)}"])
    return res


def _convert_one(args, cfg, input_path: str, batch: bool = False) -> tuple[int, dict, list[str]]:
    """1ファイルを変換。戻り値 (終了コード, JSON用の辞書, 人向けの表示行)。"""
    header, rows = read_csv(input_path, cfg.encoding_in)
    uh, dups = uniquify(header)
    probs = check_header(cfg, uh)
    if probs:
        return 2, {"schema": SCHEMA.format("convert"), "ok": False, "input": input_path,
                   "input_problems": probs}, [f"NG {x}" for x in probs]
    out = resolve_output(input_path, args.output, args.out_dir, args.timestamp)
    r = convert_to_files(cfg, args.config, input_path, header, rows, out, no_diff=args.no_diff,
                         verify=args.verify, command="tenugui " + " ".join(sys.argv[1:]),
                         detail_files=args.detail_files, report_unused=not batch)
    out_rows, changes, ver, idem = r["out_rows"], r["changes"], r["verification"], r["not_idempotent"]
    drift, files = r["drift"], r["files"]
    e, w = r["errors"], r["warnings"]
    nviol = len(ver["violations"])
    pv = r["provenance"]
    nwarn = w + len(idem) + (1 if drift and drift["changed"] else 0) + len(pv["unused"])
    bad = _failing("error" if args.strict and args.fail_on == "none" else args.fail_on, e + nviol, nwarn,
                   pv["counts"]["hold"])
    idicts, groups, cover = r["idicts"], r["groups"], r["coverage"]
    shown = idicts if args.max_issues < 0 else idicts[:args.max_issues]
    j = {"schema": SCHEMA.format("convert"), "ok": not bad, "input": input_path, "rows": len(out_rows),
         "counts": {"error": e, "warning": w}, "files": files,
         "issue_groups": groups, "case_coverage": cover,
         "issues": shown, "issues_truncated": len(shown) < len(idicts),
         "changed_cells": len(changes),
         "verification": {"ok": ver["ok"], "summary": ver["summary"], "violations": ver["violations"][:50]},
         "not_idempotent": idem, "lint": lint(cfg), "provenance": pv,
         "header": {"sha": r["header_sha"], "duplicates": dups, "drift": drift}}
    if batch:
        j["_unused"] = [u["where"] for u in r["unused"]]
    L = [f"注意: 列名 {d!r} が重複していたため #2, #3... を付けました" for d in dups]
    L += [f"{len(out_rows)} 行を変換 → {files['output']}", f"エラー {e} 件 / 警告 {w} 件"]
    L += _group_lines(groups)
    L += [f"  書式の内訳 {c['column']}: " + " / ".join(f"{x['label']} {x['count']}件" for x in c["cases"])
          + f" / 不一致 {c['unmatched']}件" for c in cover]
    s = ver["summary"]
    if pv["corrections_file"]:
        L.append(f"訂正 {pv['counts']['correct']} セル / 保留 {pv['counts']['hold']} セル"
                 f"（修正依頼リスト → {Path(files['fix']).name}）")
        L += [f"  {KIND_LABEL[g['kind']]} {g['count']:4d} 件  {g['column']}: {g['value']!r}"
              + (f" → {g['to']!r}" if g["to"] else "") + f"  理由: {g['reason']}" for g in pv["fix_requests"][:10]]
        L += [f"注意 {u['code']} {u['where']}: {u['message']}" for u in pv["unused"]]
    L += [f"値が変わったセル {len(changes)} 件",
          f"変換前後チェック: {'OK' if ver['ok'] else 'NG'}（違反 {s['violations']} / 欠落 {s['lost_cells']}）",
          f"レポート → {files['html']}（機械用: {Path(files['json']).name}）"]
    L += [f"  {k} → {files[k]}" for k in ("report", "diff", "log") if k in files]
    L += [f"  違反 {v['kind']} {v['column']} {v['row']}行: {v['before']!r} → {v['after']!r}" for v in ver["violations"][:5]]
    L += [f"注意 L102 再実行で値が変わる列 {b['column']}: {b['first']!r} → {b['second']!r}" for b in idem]
    if drift and drift["changed"]:
        L.append(f"注意 C003 前回(lock)から列が変わっています 増:{drift['added']} 減:{drift['removed']}")
    return (1 if bad else 0), j, L


def cmd_convert(args) -> int:
    cfg = _load(args)
    inputs = _inputs(args)
    if len(inputs) == 1:
        code, j, lines = _convert_one(args, cfg, inputs[0])
        if args.format == "json":
            _json(j)
        else:
            for x in lines:
                print(x, file=sys.stderr if code == 2 else sys.stdout)
        return code
    if args.output:
        raise ConfigError(["-o は入力が1つのときだけ使えます（複数のときは --out-dir か既定の tenugui_out/）"])
    results, worst = [], 0
    for inp in inputs:
        try:
            code, j, lines = _convert_one(args, cfg, inp, batch=True)
        except (ConfigError, OSError, UnicodeError) as ex:
            code, j, lines = 2, {"schema": SCHEMA.format("convert"), "ok": False, "input": inp,
                                 "input_problems": [str(ex)]}, [f"NG {ex}"]
        results.append(j)
        worst = max(worst, code)
        if args.format != "json":
            print(f"== {inp}")
            for x in lines:
                print("  " + x)
    tot = {"files": len(results), "failed": sum(1 for r in results if "input_problems" in r),
           "rows": sum(r.get("rows", 0) for r in results),
           "error": sum(r.get("counts", {}).get("error", 0) for r in results),
           "warning": sum(r.get("counts", {}).get("warning", 0) for r in results)}
    if cfg.corrections:      # 訂正ファイルを使っているときだけ（使っていない人の出力は変えない）
        tot["corrected"] = sum(r.get("provenance", {}).get("counts", {}).get("correct", 0) for r in results)
        tot["held"] = sum(r.get("provenance", {}).get("counts", {}).get("hold", 0) for r in results)
    # 当たらなかった訂正・保留は、全ファイルで一度も当たらなかったものだけ（1つのファイルにしか無い値が普通にあるため）
    sets = [set(r.pop("_unused")) for r in results if "_unused" in r]
    never = set.intersection(*sets) if sets else set()
    unused = [{"code": "L301" if e.kind == "correct" else "L302", "kind": e.kind, "where": e.where, "column": e.column,
               "value": e.value, "to": e.to, "key": list(e.keys), "reason": e.reason,
               "message": f"どのファイルの {e.column} にも {e.value!r} はありません。元データが直ったなら、この{KIND_LABEL[e.kind]}は消せます"}
              for e in (cfg.corrections.entries if cfg.corrections else []) if e.where in never]
    if unused and args.fail_on == "warn":
        worst = max(worst, 1)
    if args.format == "json":
        _json({"schema": SCHEMA.format("batch"), "ok": worst == 0, "totals": tot,
               **({"unused": unused} if cfg.corrections else {}), "results": results})
    else:
        print(f"合計: {tot['files']} ファイル（変換できず {tot['failed']}）/ {tot['rows']} 行 / "
              f"エラー {tot['error']} 件 / 警告 {tot['warning']} 件"
              + (f" / 訂正 {tot['corrected']} セル / 保留 {tot['held']} セル" if cfg.corrections else ""))
        for u in unused:
            print(f"注意 {u['code']} {u['where']}: {u['message']}")
    return worst


def cmd_verify(args) -> int:
    """入力CSVと出力CSV（ディスク上）を突き合わせて検証する。"""
    cfg = _load(args)
    header, rows = read_csv(args.input, cfg.encoding_in)
    ver = verify_files(cfg, header, rows, args.output_csv)
    if args.format == "json":
        _json({"schema": SCHEMA.format("verify"), **ver})
    else:
        s = ver["summary"]
        print(f"変換前後チェック: {'OK' if ver['ok'] else 'NG'}  行 {s['rows_in']}→{s['rows_out']} / "
              f"変更 {s['changed_cells']} / 欠落 {s['lost_cells']} / 違反 {s['violations']}")
        for v in ver["violations"][:20]:
            print(f"  違反 {v['kind']} {v['column']} {v['row']}行: {v['before']!r} → {v['after']!r}")
    return 1 if (not ver["ok"] and args.fail_on != "none") else 0


# ---------------------------------------------------------------- survey / draft / try / lock
def cmd_survey(args) -> int:
    cfg = _cfg(args)
    header, rows = read_csv(args.input, cfg.encoding_in)
    if args.output:
        if args.column:
            print("survey -o は概況（-k なし）だけで使えます", file=sys.stderr)
            return 2
        files = export_tables(args.output, [("形", SHAPE_HEADER, shape_table(cfg, header, rows, args.all))])
        if args.format == "json":
            _json({"schema": SCHEMA.format("export"), "files": [str(f) for f in files]})
        else:
            print("形の集計を書き出しました → " + ", ".join(map(str, files)))
        return 0
    if not args.column:
        ov = survey_overview(cfg, header, rows)
        if not args.all:
            ov = [o for o in ov if o["shape_count"] > 1 and any("#" in x["shape"] for x in o["shapes"])]
        if args.format == "json":
            _json({"schema": SCHEMA.format("survey_overview"), "rows": len(rows), "columns": ov})
            return 0
        print(f"{len(rows)} 行。" + ("全列" if args.all else "数値を含み、形が2種類以上ある列（ばらつきのある列）") + f": {len(ov)} 列")
        for o in ov:
            print(f"== {o['column']}: 値あり {o['filled']} / 種類 {o['distinct']} / 形 {o['shape_count']} 種"
                  + (f" / NG {o['ng']}" if o["ng"] else ""))
            for sh in o["shapes"]:
                print(f"  {sh['count']:6d}  {sh['shape']}    例 {sh['examples'][0]!r}")
        return 0
    res = [survey_full(cfg, header, rows, c, args.top) for c in args.column]
    if args.format == "json":
        _json({"schema": SCHEMA.format("survey"), "columns": [
            {**r, "top_values": [{"value": v, "count": n} for v, n in r["top_values"]]} for r in res]})
        return 0
    for s in res:
        print(f"== {s['column']}: 種類 {s['distinct']} / 単位 {s['units']} / 数値+単位として読めない {s['unparsable']}")
        for v, n in s["top_values"]:
            print(f"  {n:6d}  {v!r}")
        print("  形（数値を # にしたもの。pattern にそのまま使える）:")
        for sh in s["shapes"]:
            print(f"  {sh['count']:6d}  {sh['shape']}    例 {sh['examples'][0]!r}")
        for c in s["candidates"]:
            print(f"  表記ゆれ候補({c['kind']}): {c['canonical']!r} ← {c['variants']}  計{c['count']}件")
        if s["composite_examples"]:
            print(f"  複合表記の例（match/parts で分解）: {s['composite_examples']}")
    return 0


def cmd_draft(args) -> int:
    cfg = _cfg(args)
    header, rows = read_csv(args.input, cfg.encoding_in)
    text = draft_toml(cfg, header, rows)
    if args.stdout:
        print(text)
        return 0
    out = Path(args.output) if args.output else default_out_dir(args.input) / "draft.toml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"下書きを書きました → {out}（check で検証してから使ってください）")
    return 0


def cmd_try(args) -> int:
    cfg = _load(args)
    values: dict[str, str] = {}
    for s in args.set or []:
        k, eq, v = s.partition("=")
        if not eq:
            print(f"--set は 列名=値 の形で: {s!r}", file=sys.stderr)
            return 2
        values[k] = v
    for k, v in (args.field or []):
        values[k] = v
    if not values:
        print("--field 列名 値 か --set 列名=値 を指定してください", file=sys.stderr)
        return 2
    res = try_row(cfg, values)
    if args.as_test:
        print(as_test_block(values, res, args.name or ""))
        return 0
    errors = sum(1 for i in res["issues"] if i["severity"] == "E")
    bad = _failing(args.fail_on, errors, len(res["issues"]) - errors)
    if args.format == "json":
        _json({"schema": SCHEMA.format("try"), "ok": not bad, **res})
    else:
        for col, tr in res["trace"].items():
            print(f"[{col}]")
            for t in tr:
                print(f"  {t['stage']:10s} {t['value']}")
        print("出力:", {k: v for k, v in res["outputs"].items()})
        for i in res["issues"]:
            print(f"  {i['severity']} {i['code']} {i['column']}: {i['message']}")
        if res["skipped_derives"]:
            print(f"  （入力が足りず実行しなかった派生: {res['skipped_derives']}）")
    return 1 if bad else 0


def cmd_test(args) -> int:
    cfg = _load(args)
    res = run_tests(cfg)
    if args.format == "json":
        _json({"schema": SCHEMA.format("test"), **res})
    else:
        if not res["total"]:
            print("[[test]] がありません（tenugui try ... --as-test で作れます）")
        else:
            print(f"テスト: {res['total'] - res['failed']}/{res['total']} 件 合格")
        _print_failures(res)
    return 0 if res["ok"] else 1


def cmd_suggest(args) -> int:
    cfg = _load(args)
    header, rows = read_csv(args.input, cfg.encoding_in)
    uh, _ = uniquify(header)
    probs = check_header(cfg, uh)
    if probs:
        raise ConfigError(probs)
    groups = suggest(cfg, header, rows)
    total = len(groups)
    if args.max_groups >= 0:
        groups = groups[:args.max_groups]
    if args.format == "json":
        _json({"schema": SCHEMA.format("suggest"), "rows": len(rows), "groups": groups,
               "groups_truncated": len(groups) < total})
        return 0
    if not groups:
        print("修正が必要な問題はありません")
    for g in groups:
        print(f"== {g['count']} 件  {g['code']} {g['column']}"
              + (f" → {g['out']}" if g["out"] and g["out"] != g["column"] else "")
              + (f"  形 {g['shape']}" if g["shape"] else ""))
        print(f"   例 {g['examples']}  行 {g['rows']}")
        sg = g["suggestion"]
        print(f"   案（確度 {sg['confidence']}）: {sg['note']}")
        for line in sg["toml"].splitlines():
            print(f"     {line}")
    return 0


def cmd_impact(args) -> int:
    new = _load(args)
    base = load_config(args.base)
    header, rows = read_csv(args.input, new.encoding_in)
    uh, _ = uniquify(header)
    probs = check_header(new, uh) + [f"(base) {x}" for x in check_header(base, uh)]
    if probs:
        raise ConfigError(probs)
    res = impact(base, new, header, rows)
    return _show_compare(args, "impact", res)


def cmd_compare(args) -> int:
    h1, r1 = read_csv(args.before, args.encoding)
    h2, r2 = read_csv(args.after, args.encoding)
    return _show_compare(args, "compare", compare_outputs(h1, r1, h2, r2))


def _show_compare(args, name: str, res: dict) -> int:
    if args.format == "json":
        _json({"schema": SCHEMA.format(name), **res})
    else:
        if res["same"]:
            print("出力に違いはありません")
        else:
            print(f"行 {res['rows_before']}→{res['rows_after']} / 値が変わったセル {res['changed_cells']} 件")
        if res["columns_added"]:
            print(f"増えた列: {res['columns_added']}")
        if res["columns_removed"]:
            print(f"消えた列: {res['columns_removed']}")
        for c in res["columns"]:
            print(f"== {c['column']}: {c['changed']} 件")
            for x in c["samples"]:
                print(f"   {x['row']}行 {x['key']}: {x['before']!r} → {x['after']!r}")
        if "issues_before" in res:
            b, a = res["issues_before"], res["issues_after"]
            print(f"問題: エラー {b['error']}→{a['error']} / 警告 {b['warning']}→{a['warning']}")
            for x in res["issue_changes"]:
                print(f"   {x['code']} {x['out'] or x['column']}: {x['before']}→{x['after']}")
    return 1 if (not res["same"] and args.fail_on != "none") else 0


def cmd_glossary(args) -> int:
    """用語対応表: 同義語表を「正式名 ← 別名」の一覧にする。CSVを渡すと、実際に使われた件数も付ける。"""
    cfg = _load(args)
    used: dict[tuple[str, str], dict[str, int]] = {}
    cols: dict[str, list[str]] = {}
    for fr in cfg.fields:
        if fr.synonyms and fr.src not in cols.setdefault(fr.synonyms, []):
            cols[fr.synonyms].append(fr.src)
    rows_n = None
    if args.input:
        header, rows = read_csv(args.input, cfg.encoding_in)
        probs = check_header(cfg, uniquify(header)[0])
        if probs:
            raise ConfigError(probs)
        prov = Provenance()
        convert_rows(cfg, header, rows, prov)
        rows_n = len(rows)
        for (sname, canon, variant), n in prov.syn.items():
            used.setdefault((sname, canon), {})[variant] = n
    terms = []
    for sname, tbl in cfg.synonym_tables.items():
        for canon, variants in tbl.items():
            t = {"set": sname, "canonical": canon, "variants": variants, "columns": cols.get(sname, [])}
            if rows_n is not None:
                u = used.get((sname, canon), {})
                t["count"] = sum(u.values())
                t["unified"] = sum(n for v, n in u.items() if v != canon)
                t["used"] = [{"value": v, "count": n} for v, n in sorted(u.items(), key=lambda x: -x[1])]
            terms.append(t)
    if args.format == "json":
        _json({"schema": SCHEMA.format("glossary"), "rows": rows_n, "terms": terms})
        return 0
    if args.format == "csv":
        import csv
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(["同義語表", "正式名", "別名", "列"] + (["件数", "統一した件数"] if rows_n is not None else []))
        for t in terms:
            w.writerow([t["set"], t["canonical"], " / ".join(t["variants"]), " / ".join(t["columns"])]
                       + ([t["count"], t["unified"]] if rows_n is not None else []))
        return 0
    if not terms:
        print("同義語表（[synonyms.<名前>]）がありません")
    cur = None
    for t in terms:
        if t["set"] != cur:
            cur = t["set"]
            print(f"== {cur}" + (f"（列: {', '.join(t['columns'])}）" if t["columns"] else "（どの列からも使われていません）"))
        print(f"  {t['canonical']}  ←  {' / '.join(t['variants']) or '（別名なし）'}"
              + (f"    {t['count']} 件（うち別の表記 {t['unified']} 件）" if rows_n is not None else ""))
    return 0


def cmd_schema(args) -> int:
    from .schema import schema, schema_text
    sc = schema()
    if args.format == "json":
        _json(sc)
    else:
        print(schema_text(sc))
    return 0


def cmd_lock(args) -> int:
    cfg = _load(args)
    header, _ = read_csv(args.input, cfg.encoding_in)
    p = write_lock(args.config, header)
    print(f"列構成を記録しました → {p}（次回から列の増減を検知します）")
    return 0


def cmd_ui(args) -> int:
    from .ui import main as ui_main
    return ui_main()


# ---------------------------------------------------------------- main
def _fmt(p: argparse.ArgumentParser, fail_default: str) -> None:
    p.add_argument("--format", choices=["text", "json"], default="text", help="json は agent 向け（1行のJSON）")
    p.add_argument("--fail-on", choices=["none", "error", "hold", "warn"], default=fail_default,
                   help="終了コードを1にする基準（hold = エラーか、保留の値が残っているとき）")


def _cor(p: argparse.ArgumentParser) -> None:
    p.add_argument("--corrections", metavar="TOML",
                   help="訂正・保留のファイル（既定: ルールTOMLと同じ場所の <ルール名>.corrections.toml があれば使う）")


def main(argv: list[str] | None = None) -> int:
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    from . import __version__
    ap = argparse.ArgumentParser(prog="tenugui",
                                 description=f"tenugui {__version__} — chozu出力CSVの変換（表記ゆれ・単位・計算）")
    ap.add_argument("-V", "--version", action="version", version=f"tenugui {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="変換して 出力CSV・HTMLレポート・JSONレポート を書く")
    c.add_argument("input", nargs="+", help="入力CSV。複数、またはフォルダ（中の *.csv すべて）も可")
    c.add_argument("-c", "--config", required=True)
    c.add_argument("-o", "--output", help="出力CSV名（既定: 入力CSVのあるフォルダの tenugui_out/<入力名>_tenugui.csv）")
    c.add_argument("--out-dir", help=f"出力フォルダ（既定: 入力CSVのあるフォルダの {OUT_DIR_NAME}/。無ければ作る）")
    c.add_argument("--timestamp", action="store_true", help="出力ファイル名に日時を付ける")
    c.add_argument("--detail-files", action="store_true",
                   help="従来の *_conv.csv（問題一覧）・*_diff.csv（変更セル）・*.log も書く（同じ内容は JSON レポートにある）")
    c.add_argument("--no-diff", action="store_true", help="--detail-files のとき *_diff.csv を出さない")
    c.add_argument("--diff", action="store_true", help=argparse.SUPPRESS)   # 互換（既定で出力する）
    c.add_argument("--verify", action="store_true", help="変換結果を再度通して値が変わる列を検出（冪等性）")
    c.add_argument("--strict", action="store_true", help="--fail-on error と同じ（互換）")
    c.add_argument("--max-issues", type=int, default=-1, help="json に載せる問題の最大数（-1=全部）")
    _cor(c)
    _fmt(c, "none")
    c.set_defaults(fn=cmd_convert)

    k = sub.add_parser("check", help="設定の検証（CSVを渡すと列の存在・ヘッダ変化も確認）")
    k.add_argument("-c", "--config", required=True)
    k.add_argument("input", nargs="?")
    k.add_argument("--idempotent", action="store_true", help="CSVを渡した場合に冪等性も確認")
    _cor(k)
    _fmt(k, "error")
    k.set_defaults(fn=cmd_check)

    s = sub.add_parser("survey", help="ルールを書く前に列の値の実態・表記ゆれ候補を数える")
    s.add_argument("input")
    s.add_argument("-c", "--config")
    s.add_argument("--column", "-k", action="append",
                   help="調べる列（複数可）。省略すると全列の概況（形が2種類以上ある列）")
    s.add_argument("--all", action="store_true", help="概況で、形が1種類の列も出す")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("-o", "--output", metavar="FILE",
                   help="概況（UIの「形」タブと同じ表）を書き出す。.xlsx なら Excel、それ以外は CSV（utf-8-sig）")
    _cor(s)
    s.set_defaults(fn=cmd_survey)

    d = sub.add_parser("draft", help="CSVの実態からルールTOMLの下書きを作る")
    d.add_argument("input")
    d.add_argument("-c", "--config", help="単位表の追加などだけ入れた既存設定（任意）")
    d.add_argument("-o", "--output", help=f"既定: 入力CSVのあるフォルダの {OUT_DIR_NAME}/draft.toml")
    d.add_argument("--stdout", action="store_true", help="ファイルでなく標準出力に出す")
    _cor(d)
    d.set_defaults(fn=cmd_draft)

    vf = sub.add_parser("verify", help="入力CSVと出力CSVを突き合わせて変換前後を検証する")
    vf.add_argument("input")
    vf.add_argument("output_csv")
    vf.add_argument("-c", "--config", required=True)
    _cor(vf)
    _fmt(vf, "error")
    vf.set_defaults(fn=cmd_verify)

    t = sub.add_parser("try", help="CSVなしで値を試す（各段階の途中経過つき）")
    t.add_argument("-c", "--config", required=True)
    t.add_argument("--field", nargs=2, action="append", metavar=("列名", "値"))
    t.add_argument("--set", action="append", metavar="列名=値")
    t.add_argument("--as-test", action="store_true", help="結果を、設定に貼れる [[test]] として出す")
    t.add_argument("--name", help="--as-test で付けるテスト名")
    _cor(t)
    _fmt(t, "none")
    t.set_defaults(fn=cmd_try)

    ts = sub.add_parser("test", help="設定内の [[test]]（入力→期待値）を実行する")
    ts.add_argument("-c", "--config", required=True)
    ts.add_argument("--format", choices=["text", "json"], default="text")
    _cor(ts)
    ts.set_defaults(fn=cmd_test)

    sg = sub.add_parser("suggest", help="問題を 列×形 でまとめ、設定の修正案（TOML断片）を出す")
    sg.add_argument("input")
    sg.add_argument("-c", "--config", required=True)
    sg.add_argument("--max-groups", type=int, default=-1, help="出すグループの最大数（-1=全部）")
    sg.add_argument("--format", choices=["text", "json"], default="text")
    _cor(sg)
    sg.set_defaults(fn=cmd_suggest)

    im = sub.add_parser("impact", help="設定の変更前後で、同じ入力の出力がどう変わるかを出す")
    im.add_argument("input")
    im.add_argument("-c", "--config", required=True, help="変更後の設定")
    im.add_argument("--base", required=True, help="変更前の設定")
    _cor(im)
    _fmt(im, "none")
    im.set_defaults(fn=cmd_impact)

    cp = sub.add_parser("compare", help="2つの出力CSV（前・後）を比べる")
    cp.add_argument("before")
    cp.add_argument("after")
    cp.add_argument("--encoding", default="utf-8-sig")
    _fmt(cp, "none")
    cp.set_defaults(fn=cmd_compare)

    gl = sub.add_parser("glossary", help="用語対応表（正式名 ← 別名）を出す。CSVを渡すと使われた件数つき")
    gl.add_argument("-c", "--config", required=True)
    gl.add_argument("input", nargs="?")
    gl.add_argument("--format", choices=["text", "json", "csv"], default="text")
    _cor(gl)
    gl.set_defaults(fn=cmd_glossary)

    sc = sub.add_parser("schema", help="設定ファイルの書き方（キー・型・制約・preset・報告コード）を出す")
    sc.add_argument("--format", choices=["text", "json"], default="text")
    sc.set_defaults(fn=cmd_schema)

    lk = sub.add_parser("lock", help="いまのCSVの列構成を記録（以後の列の増減を検知）")
    lk.add_argument("input")
    lk.add_argument("-c", "--config", required=True)
    _cor(lk)
    lk.set_defaults(fn=cmd_lock)

    u = sub.add_parser("ui", help="簡易UIを開く")
    u.set_defaults(fn=cmd_ui)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (OSError, UnicodeError) as e:
        msg = (f"ファイルを開けません: {e}" if isinstance(e, OSError) else
               f"文字コードが合いません: {e}（[settings] encoding_in = \"cp932\" などを指定します）")
        if getattr(args, "format", "text") == "json":
            _json({"schema": SCHEMA.format("error"), "ok": False, "config_errors": [{"where": "", "message": msg}]})
        else:
            print(msg, file=sys.stderr)
        return 2
    except ConfigError as e:
        if getattr(args, "format", "text") == "json":
            _json({"schema": SCHEMA.format("error"), "ok": False,
                   "config_errors": [_where(m) for m in e.problems]})
        else:
            print("設定エラー:", file=sys.stderr)
            for line in e.problems:
                print(f"  - {line}", file=sys.stderr)
            print("（設定の書き方は tenugui schema、値の試し打ちは tenugui try）", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""コマンドライン: convert / check / survey / draft / try / lock / ui

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

from .analysis import (convert_to_files, diff_changes, draft_toml, header_drift, header_sha, idempotency_report,
                       issue_dict, lint, lock_path, survey_full, try_row, verify_before_after,
                       verify_files, write_lock, write_log)
from .core import (ConfigError, check_header, config_from_dict, convert_rows, load_config,
                   read_csv, uniquify, write_csv, write_report)

SCHEMA = "tenugui.{}/1"


def _cfg(args):
    return load_config(args.config) if args.config else config_from_dict({"schema_version": 1})


def _json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _failing(fail_on: str, errors: int, warnings: int) -> bool:
    return (fail_on == "error" and errors > 0) or (fail_on == "warn" and errors + warnings > 0)


def _where(msg: str) -> dict:
    head, sep, rest = msg.partition(": ")
    if sep and len(head) < 80 and (head.startswith("[") or head.startswith("トップ")):
        return {"where": head, "message": rest}
    return {"where": "", "message": msg}


# ---------------------------------------------------------------- check
def cmd_check(args) -> int:
    cfg = load_config(args.config)
    probs, dups, drift, idem = [], [], None, []
    if args.input:
        header, rows = read_csv(args.input, cfg.encoding_in)
        uh, dups = uniquify(header)
        probs = check_header(cfg, uh)
        drift = header_drift(args.config, header)
        if args.idempotent:
            idem = idempotency_report(cfg, header, rows)
    warns = lint(cfg)
    errors = len(probs)
    nwarn = len(warns) + len(idem) + (1 if drift and drift["changed"] else 0)
    bad = _failing(args.fail_on, errors, nwarn)
    if args.format == "json":
        _json({"schema": SCHEMA.format("check"), "ok": not bad, "fields": len(cfg.fields),
               "derives": len(cfg.derives), "input_problems": probs, "lint": warns,
               "duplicate_columns": dups, "header_drift": drift, "not_idempotent": idem})
    else:
        print(f"設定OK: field {len(cfg.fields)} 件 / derive {len(cfg.derives)} 件")
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


def cmd_convert(args) -> int:
    cfg = load_config(args.config)
    header, rows = read_csv(args.input, cfg.encoding_in)
    uh, dups = uniquify(header)
    probs = check_header(cfg, uh)
    if probs:
        if args.format == "json":
            _json({"schema": SCHEMA.format("convert"), "ok": False, "input_problems": probs})
        else:
            for p in probs:
                print(f"NG {p}", file=sys.stderr)
        return 2
    out = resolve_output(args.input, args.output, args.out_dir, args.timestamp)
    r = convert_to_files(cfg, args.config, args.input, header, rows, out, no_diff=args.no_diff,
                         verify=args.verify, command="tenugui " + " ".join(sys.argv[1:]))
    out_rows, issues, changes, ver, idem = r["out_rows"], r["issues"], r["changes"], r["verification"], r["not_idempotent"]
    drift, files, logp, rep = r["drift"], r["files"], r["files"]["log"], r["files"]["report"]
    e, w = r["errors"], r["warnings"]
    nviol = len(ver["violations"])
    nwarn = w + len(idem) + (1 if drift and drift["changed"] else 0)
    bad = _failing("error" if args.strict and args.fail_on == "none" else args.fail_on, e + nviol, nwarn)
    idicts = r["idicts"]
    if args.format == "json":
        shown = idicts if args.max_issues < 0 else idicts[:args.max_issues]
        _json({"schema": SCHEMA.format("convert"), "ok": not bad, "input": args.input, "rows": len(out_rows),
               "counts": {"error": e, "warning": w}, "files": files,
               "issues": shown, "issues_truncated": len(shown) < len(idicts),
               "changed_cells": len(changes),
               "verification": {"ok": ver["ok"], "summary": ver["summary"],
                                "violations": ver["violations"][:50]},
               "not_idempotent": idem, "lint": lint(cfg),
               "header": {"sha": header_sha(uh), "duplicates": dups, "drift": drift}})
    else:
        for d in dups:
            print(f"注意: 列名 {d!r} が重複していたため #2, #3... を付けました")
        print(f"{len(out_rows)} 行を変換 → {files['output']}")
        print(f"エラー {e} 件 / 警告 {w} 件 → {rep}")
        print(f"値が変わったセル {len(changes)} 件" + ("" if args.no_diff else f" → {files['diff']}"))
        s = ver["summary"]
        print(f"変換前後チェック: {'OK' if ver['ok'] else 'NG'}（違反 {s['violations']} / 欠落 {s['lost_cells']}）")
        print(f"ログ → {logp}")
        for v in ver["violations"][:5]:
            print(f"  違反 {v['kind']} {v['column']} {v['row']}行: {v['before']!r} → {v['after']!r}")
        for b in idem:
            print(f"注意 L102 再実行で値が変わる列 {b['column']}: {b['first']!r} → {b['second']!r}")
        if drift and drift["changed"]:
            print(f"注意 C003 前回(lock)から列が変わっています 増:{drift['added']} 減:{drift['removed']}")
    return 1 if bad else 0


def cmd_verify(args) -> int:
    """入力CSVと出力CSV（ディスク上）を突き合わせて検証する。"""
    cfg = load_config(args.config)
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
    res = [survey_full(cfg, header, rows, c, args.top) for c in args.column]
    if args.format == "json":
        _json({"schema": SCHEMA.format("survey"), "columns": [
            {**r, "top_values": [{"value": v, "count": n} for v, n in r["top_values"]]} for r in res]})
        return 0
    for s in res:
        print(f"== {s['column']}: 種類 {s['distinct']} / 単位 {s['units']} / 数値+単位として読めない {s['unparsable']}")
        for v, n in s["top_values"]:
            print(f"  {n:6d}  {v!r}")
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
    cfg = load_config(args.config)
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


def cmd_lock(args) -> int:
    cfg = load_config(args.config)
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
    p.add_argument("--fail-on", choices=["none", "error", "warn"], default=fail_default,
                   help="終了コードを1にする基準")


def main(argv: list[str] | None = None) -> int:
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="tenugui", description="chozu出力CSVの変換（表記ゆれ・単位・計算）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("convert", help="変換して出力CSVと変換レポートを書く")
    c.add_argument("input")
    c.add_argument("-c", "--config", required=True)
    c.add_argument("-o", "--output", help="出力CSV名（既定: 入力CSVのあるフォルダの tenugui_out/<入力名>_tenugui.csv）")
    c.add_argument("--out-dir", help=f"出力フォルダ（既定: 入力CSVのあるフォルダの {OUT_DIR_NAME}/。無ければ作る）")
    c.add_argument("--timestamp", action="store_true", help="出力ファイル名に日時を付ける")
    c.add_argument("--no-diff", action="store_true", help="*_diff.csv を出さない")
    c.add_argument("--diff", action="store_true", help=argparse.SUPPRESS)   # 互換（既定で出力する）
    c.add_argument("--verify", action="store_true", help="変換結果を再度通して値が変わる列を検出（冪等性）")
    c.add_argument("--strict", action="store_true", help="--fail-on error と同じ（互換）")
    c.add_argument("--max-issues", type=int, default=-1, help="json に載せる問題の最大数（-1=全部）")
    _fmt(c, "none")
    c.set_defaults(fn=cmd_convert)

    k = sub.add_parser("check", help="設定の検証（CSVを渡すと列の存在・ヘッダ変化も確認）")
    k.add_argument("-c", "--config", required=True)
    k.add_argument("input", nargs="?")
    k.add_argument("--idempotent", action="store_true", help="CSVを渡した場合に冪等性も確認")
    _fmt(k, "error")
    k.set_defaults(fn=cmd_check)

    s = sub.add_parser("survey", help="ルールを書く前に列の値の実態・表記ゆれ候補を数える")
    s.add_argument("input")
    s.add_argument("-c", "--config")
    s.add_argument("--column", "-k", action="append", required=True)
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.set_defaults(fn=cmd_survey)

    d = sub.add_parser("draft", help="CSVの実態からルールTOMLの下書きを作る")
    d.add_argument("input")
    d.add_argument("-c", "--config", help="単位表の追加などだけ入れた既存設定（任意）")
    d.add_argument("-o", "--output", help=f"既定: 入力CSVのあるフォルダの {OUT_DIR_NAME}/draft.toml")
    d.add_argument("--stdout", action="store_true", help="ファイルでなく標準出力に出す")
    d.set_defaults(fn=cmd_draft)

    vf = sub.add_parser("verify", help="入力CSVと出力CSVを突き合わせて変換前後を検証する")
    vf.add_argument("input")
    vf.add_argument("output_csv")
    vf.add_argument("-c", "--config", required=True)
    _fmt(vf, "error")
    vf.set_defaults(fn=cmd_verify)

    t = sub.add_parser("try", help="CSVなしで値を試す（各段階の途中経過つき）")
    t.add_argument("-c", "--config", required=True)
    t.add_argument("--field", nargs=2, action="append", metavar=("列名", "値"))
    t.add_argument("--set", action="append", metavar="列名=値")
    _fmt(t, "none")
    t.set_defaults(fn=cmd_try)

    lk = sub.add_parser("lock", help="いまのCSVの列構成を記録（以後の列の増減を検知）")
    lk.add_argument("input")
    lk.add_argument("-c", "--config", required=True)
    lk.set_defaults(fn=cmd_lock)

    u = sub.add_parser("ui", help="簡易UIを開く")
    u.set_defaults(fn=cmd_ui)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as e:
        if getattr(args, "format", "text") == "json":
            _json({"schema": SCHEMA.format("error"), "ok": False,
                   "config_errors": [_where(m) for m in e.problems]})
        else:
            print("設定エラー:", file=sys.stderr)
            for line in e.problems:
                print(f"  - {line}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""変換レポート: 人が見る HTML（1ページ・単体で開ける）と、機械が読む JSON。"""
from __future__ import annotations

import datetime
import html
import json
from pathlib import Path

from .core import Config, Issue, _is_reserved, uniquify

MAX_HTML_ROWS = 2000      # これを超える入力は、問題のある行だけを表に載せる

_CSS = """
:root{--bg:#fff;--fg:#1c1c1c;--mut:#666;--line:#d8d8d8;--head:#f3f3f1;
--err:#fbd5d5;--errb:#c0392b;--warn:#fdf0b8;--warnb:#a67c00;--chg:#dcefdc;--ng:#e4e4e4;--src:#fafaf7}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1b;--fg:#e8e8e8;--mut:#a0a0a0;--line:#444;--head:#2a2a2a;
--err:#5a2323;--errb:#ff8a80;--warn:#57480f;--warnb:#ffd54f;--chg:#1f3d24;--ng:#3a3a3a;--src:#222}}
*{box-sizing:border-box}
body{margin:0;padding:20px 24px 60px;background:var(--bg);color:var(--fg);
font:14px/1.5 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--mut);font-size:12px;word-break:break-all}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}
.card{border:1px solid var(--line);border-radius:6px;padding:8px 14px;min-width:110px}
.card b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.card span{font-size:12px;color:var(--mut)}
.card.err{border-color:var(--errb)}.card.err b{color:var(--errb)}
.card.warn{border-color:var(--warnb)}.card.warn b{color:var(--warnb)}
.wrap{overflow:auto;max-height:75vh;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;font-size:13px;width:max-content;min-width:100%}
th,td{border-bottom:1px solid var(--line);border-right:1px solid var(--line);padding:4px 8px;text-align:left;
vertical-align:top;white-space:nowrap;max-width:340px;overflow:hidden;text-overflow:ellipsis}
thead th{position:sticky;top:0;background:var(--head);z-index:1;font-weight:600}
th.g{text-align:center}td.num{text-align:right;color:var(--mut);font-variant-numeric:tabular-nums}
td.src{background:var(--src);color:var(--mut)}
td.err{background:var(--err)}td.warn{background:var(--warn)}td.chg{background:var(--chg)}td.ng{background:var(--ng)}
.tag{font-size:10px;font-weight:700;margin-left:6px;padding:0 4px;border-radius:3px;border:1px solid currentColor}
td.err .tag{color:var(--errb)}td.warn .tag{color:var(--warnb)}
.legend span{display:inline-block;padding:1px 8px;margin-right:8px;border-radius:3px;font-size:12px}
.ok{color:#2e7d32;font-weight:700}.bad{color:var(--errb);font-weight:700}
code{font-family:ui-monospace,Consolas,monospace;font-size:12px}
label.f{font-size:13px;margin-right:16px}
body.only tr.clean{display:none}
ul{margin:6px 0;padding-left:20px}
"""

_JS = "document.getElementById('only').addEventListener('change',function(e){document.body.classList.toggle('only',e.target.checked)});"


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def build_report(*, version: str, command: str, input_path: str, config_path: str, config_sha: str,
                 header_sha: str, rows: int, errors: int, warnings: int, files: dict, groups: list[dict],
                 coverage: list[dict], issues: list[dict], changes: list[dict], verification: dict,
                 not_idempotent: list[dict], lint: list[dict], drift: dict | None) -> dict:
    """機械が読むレポート（tenugui.report/1）。変換1回ぶんの結果をすべて持つ。"""
    return {"schema": "tenugui.report/1", "version": version,
            "time": datetime.datetime.now().isoformat(timespec="seconds"), "command": command,
            "input": input_path, "config": config_path, "config_sha": config_sha, "header_sha": header_sha,
            "rows": rows, "counts": {"error": errors, "warning": warnings}, "files": files,
            "issue_groups": groups, "case_coverage": coverage, "issues": issues, "changes": changes,
            "verification": verification, "not_idempotent": not_idempotent, "lint": lint,
            "header_drift": drift}


def write_json(path: str | Path, rep: dict) -> None:
    Path(path).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")


def _layout(cfg: Config, uh: list[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """表の列構成: (元の列, その出力列...) の並びと、派生列。"""
    groups: dict[str, list[str]] = {}
    for fr in cfg.fields:
        if fr.src in uh:
            outs = groups.setdefault(fr.src, [])
            if fr.out not in outs:
                outs.append(fr.out)
    return list(groups.items()), [d.out for d in cfg.derives]


def render_html(rep: dict, cfg: Config, header: list[str], rows: list[list[str]],
                out_header: list[str], out_rows: list[list[str]], issues: list[Issue]) -> str:
    """人が見るレポート。ルールのかかった列について 元の値 → 変換後 を並べ、問題のあるセルに色を付ける。"""
    uh, _ = uniquify(header)
    iidx = {h: i for i, h in enumerate(uh)}
    oidx = {h: i for i, h in enumerate(out_header)}
    groups, derives = _layout(cfg, uh)
    outs_of = dict(groups)

    # セルごとの状態: (行, 列ID) -> [重大度, [メッセージ...], [コード...]]。列IDは 元の列=("src",名前) / 出力列=名前
    marks: dict[tuple, list] = {}

    def mark(row: int, col, sev: str, code: str, msg: str) -> None:
        m = marks.setdefault((row, col), ["W", [], []])
        if sev == "E":
            m[0] = "E"
        m[1].append(f"{code} {msg}")
        if code not in m[2]:
            m[2].append(code)

    for i in issues:
        if i.column in outs_of:
            mark(i.row, ("src", i.column), i.severity, i.code, i.message)
            targets = outs_of[i.column] if (i.code in ("C111", "C131") or not i.out) else [i.out]
            for t in targets:
                mark(i.row, t, i.severity, i.code, i.message)
        else:
            mark(i.row, i.out or i.column, i.severity, i.code, i.message)
    bad_rows = {r for r, _ in marks}

    c = rep["counts"]
    ver = rep["verification"]
    s = ver["summary"]
    P: list[str] = []
    P.append(f"<h1>Tenugui 変換レポート</h1><div class='meta'>入力: {_e(rep['input'])}<br>設定: {_e(rep['config'])}"
             f"（sha {_e(rep['config_sha'])}）<br>{_e(rep['time'])} / tenugui {_e(rep['version'])}</div>")
    P.append("<div class='cards'>"
             f"<div class='card'><b>{rep['rows']}</b><span>行</span></div>"
             f"<div class='card{' err' if c['error'] else ''}'><b>{c['error']}</b><span>エラー</span></div>"
             f"<div class='card{' warn' if c['warning'] else ''}'><b>{c['warning']}</b><span>警告</span></div>"
             f"<div class='card'><b>{len(rep['changes'])}</b><span>値が変わったセル</span></div>"
             f"<div class='card{'' if ver['ok'] else ' err'}'><b>{'OK' if ver['ok'] else 'NG'}</b>"
             f"<span>変換前後チェック（違反 {s['violations']} / 欠落 {s['lost_cells']}）</span></div></div>")

    if rep["issue_groups"]:
        P.append("<h2>問題の内訳（列 × コード × 形）</h2><div class='wrap'><table><thead><tr><th>件数</th><th>コード</th>"
                 "<th>列</th><th>形</th><th>例</th><th>行</th><th>直し方</th></tr></thead><tbody>")
        for g in rep["issue_groups"]:
            cls = "err" if g["severity"] == "E" else "warn"
            col = g["column"] + (f" → {g['out']}" if g["out"] and g["out"] != g["column"] else "")
            P.append(f"<tr><td class='num'>{g['count']}</td><td class='{cls}'>{_e(g['code'])}</td><td>{_e(col)}</td>"
                     f"<td><code>{_e(g['shape'])}</code></td><td>{_e(' / '.join(g['examples']))}</td>"
                     f"<td>{_e(', '.join(map(str, g['rows'])))}</td>"
                     f"<td style='white-space:normal;min-width:260px'>{_e(g['hint'])}</td></tr>")
        P.append("</tbody></table></div>")
    else:
        P.append("<h2>問題の内訳</h2><p class='ok'>問題はありません</p>")

    notes: list[str] = []
    for cv in rep["case_coverage"]:
        notes.append(f"書式の内訳 <b>{_e(cv['column'])}</b>: "
                     + " / ".join(f"<code>{_e(x['label'])}</code> {x['count']}件" for x in cv["cases"])
                     + f" / 不一致 {cv['unmatched']}件")
    for v in ver["violations"][:50]:
        notes.append(f"<span class='bad'>違反 {_e(v['kind'])}</span> {_e(v['column'])} {v['row']}行: "
                     f"{_e(v['before'])} → {_e(v['after'])}")
    for d in ver.get("dropped_columns", []):
        notes.append(f"削除した元の列 <b>{_e(d['column'])}</b>"
                     + (f"（変換に失敗した行 {d['failed_rows']} 件。元の値は下の表と JSON レポートに残っています）"
                        if d["failed_rows"] else ""))
    for b in rep["not_idempotent"]:
        notes.append(f"L102 再実行で値が変わる列 {_e(b['column'])}: {_e(b['first'])} → {_e(b['second'])}")
    for w in rep["lint"]:
        notes.append(f"{_e(w['code'])} {_e(w['where'])}: {_e(w['message'])}")
    d = rep["header_drift"]
    if d and d["changed"]:
        notes.append(f"C003 前回(lock)から列が変わっています 増: {_e(d['added'])} 減: {_e(d['removed'])}")
    if notes:
        P.append("<h2>確認事項</h2><ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>")

    # ---- 変換前後の表
    limited = len(rows) > MAX_HTML_ROWS
    show = [n for n in range(1, len(rows) + 1) if (not limited or n in bad_rows)][:MAX_HTML_ROWS]
    P.append("<h2>変換前後（ルールのかかった列）</h2>")
    P.append("<p class='legend'><span style='background:var(--err)'>エラー（変換できず）</span>"
             "<span style='background:var(--warn)'>警告（要確認）</span>"
             "<span style='background:var(--chg)'>値を変換した</span>"
             "<span style='background:var(--ng)'>抽出NG印（そのまま）</span>"
             "<span style='background:var(--src)'>元の値</span></p>")
    P.append("<p><label class='f'><input type='checkbox' id='only'> 問題のある行だけ表示</label>"
             + (f"<span class='meta'>行数が多いため、問題のある行だけを載せています（{len(show)} / {len(rows)} 行）</span>"
                if limited else f"<span class='meta'>{len(rows)} 行。色の付いたセルにカーソルを合わせると理由が出ます</span>")
             + "</p>")
    if not groups and not derives:
        P.append("<p class='meta'>ルールのかかった列はありません</p>")
    else:
        key_name = uh[0] if uh else ""
        P.append("<div class='wrap'><table><thead><tr><th rowspan='2'>行</th>"
                 f"<th rowspan='2'>{_e(key_name)}</th>")
        for src, outs in groups:
            P.append(f"<th class='g' colspan='{1 + len(outs)}'>{_e(src)}</th>")
        if derives:
            P.append(f"<th class='g' colspan='{len(derives)}'>計算した列</th>")
        P.append("</tr><tr>")
        for src, outs in groups:
            P.append("<th>元の値</th>" + "".join(
                f"<th>{'変換後' if o == src else _e(o)}</th>" for o in outs))
        P.append("".join(f"<th>{_e(o)}</th>" for o in derives) + "</tr></thead><tbody>")

        def cell(n: int, col, text: str, base: str) -> str:
            m = marks.get((n, col))
            if m:
                cls = "err" if m[0] == "E" else "warn"
                tags = "".join(f"<span class='tag'>{_e(x)}</span>" for x in m[2])
                return f"<td class='{cls}' title='{_e(' / '.join(m[1]))}'>{_e(text)}{tags}</td>"
            return f"<td{f' class={base!r}' if base else ''}>{_e(text)}</td>"

        for n in show:
            r_in, r_out = rows[n - 1], out_rows[n - 1]
            P.append(f"<tr class='{'dirty' if n in bad_rows else 'clean'}'><td class='num'>{n}</td>"
                     f"<td>{_e(r_in[0] if r_in else '')}</td>")
            for src, outs in groups:
                i = iidx[src]
                raw = r_in[i] if i < len(r_in) else ""
                ng = _is_reserved(cfg, raw)
                P.append(cell(n, ("src", src), raw, "ng" if ng else "src"))
                for o in outs:
                    val = r_out[oidx[o]] if o in oidx else ""
                    base = "ng" if ng else ("chg" if val != "" and val != raw else "")
                    P.append(cell(n, o, val, base))
            for o in derives:
                P.append(cell(n, o, r_out[oidx[o]] if o in oidx else "", ""))
            P.append("</tr>")
        P.append("</tbody></table></div>")

    return ("<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Tenugui レポート — {_e(Path(rep['input']).name)}</title><style>{_CSS}</style></head><body>"
            + "".join(P) + f"<script>{_JS}</script></body></html>")

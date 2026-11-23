"""変換レポート: 人が見る HTML（1ページ・単体で開ける）と、機械が読む JSON。"""
from __future__ import annotations

import base64
import datetime
import gzip
import html
import json
import re
import unicodedata
from pathlib import Path

from .core import Config, Issue, _is_reserved, uniquify
from .corrections import KIND_LABEL, Provenance

MAX_HTML_ROWS = 2000      # これを超える入力は、問題のある行だけを表に載せる
DISPLAY_CHARS = 80        # 表のセルに出す文字数。長い値は省略し、全文はカーソルを合わせると出る

_CSS = """
:root{--bg:#fff;--fg:#1c1c1c;--mut:#666;--line:#d8d8d8;--head:#f3f3f1;
--err:#fbd5d5;--errb:#c0392b;--warn:#fdf0b8;--warnb:#a67c00;--chg:#dcefdc;--ng:#e4e4e4;--src:#fafaf7;
--cor:#d6e6fb;--corb:#1f5fae;--hold:#ead9f7;--holdb:#6b3fa0}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b1b;--fg:#e8e8e8;--mut:#a0a0a0;--line:#444;--head:#2a2a2a;
--err:#5a2323;--errb:#ff8a80;--warn:#57480f;--warnb:#ffd54f;--chg:#1f3d24;--ng:#3a3a3a;--src:#222;
--cor:#1d3557;--corb:#8ab4f8;--hold:#40295c;--holdb:#c9a4f5}}
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
.card.cor{border-color:var(--corb)}.card.cor b{color:var(--corb)}
.card.hold{border-color:var(--holdb)}.card.hold b{color:var(--holdb)}
td.cor{background:var(--cor)}td.hold{background:var(--hold)}
td.cor .tag{color:var(--corb)}td.hold .tag{color:var(--holdb)}
td.wrapc{white-space:normal;min-width:220px;max-width:420px}
details{margin:8px 0}summary{cursor:pointer;font-weight:600}
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

_CSS += """
.vt{height:75vh}
.vt table{table-layout:fixed;min-width:0;border-collapse:separate;border-spacing:0}
.vt th,.vt td{height:26px;line-height:17px;max-width:none;overflow:hidden;text-overflow:clip}
.vt thead th{height:26px;z-index:2}.vt thead th[rowspan]{height:52px}.vt tr.h2 th{top:26px}
.vt .sl{position:sticky;left:0;z-index:1;background:var(--bg)}.vt .sl1{left:var(--l1)}
.vt thead .sl{z-index:3;background:var(--head)}
.vt td.sl1{border-right:2px solid var(--line)}.vt td.sl1.cor{background:var(--cor)}
.vt tr.sp td{padding:0;border:0;height:auto}.vt td.gap{padding:0;border:0;background:none}
.tools{margin:4px 0}#q{font:inherit;padding:2px 6px;width:16em}
"""

# 変換前後の表: 埋め込んだデータ D から、見えている行（と前後の少し）だけを描く。
# 行の高さを固定にして、描かない行の分は上下の空き行で高さを埋める。列の幅は Python 側で全行から決めてある
# 列も、見えている範囲（と前後の少し）だけを描く。描かない列は colspan の空きセル1つで幅を埋める。
# 行は BLK 行ずつのまとまりで描き、見えている範囲の前後1まとまりまで描いておき、外れたときだけ入れ替える
# （DOM を変えると表全体を描き直すので、変える回数を減らす）。出力にはコメントを入れない。
# 圧縮したとき（Z）は、ブラウザの DecompressionStream で戻してから描く。
_JS = r"""(function(){function T(D){
var only=document.getElementById('only'),W=document.getElementById('vt');
if(!W||!D){if(only)only.addEventListener('change',function(e){document.body.classList.toggle('only',e.target.checked)});return}
var H=26,BLK=20,LIM=80,tb=W.querySelector('tbody'),q=document.getElementById('q'),cnt=document.getElementById('cnt');
W.querySelector('table').style.setProperty('--l1',D.widths[0]+'px');
var E={'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'};
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return E[c]})}
function td(c,j){var t,cl='',ti='',tg='';
 if(typeof c!=='object'){t=String(c)}else{t=c[0];cl=c[1]||'';
  if(c.length>3){ti=D.titles[c[2]];tg=D.tags[c[3]].map(function(x){return "<span class='tag'>"+esc(x)+'</span>'}).join('')}}
 if(j===0)cl='num sl';else if(j===1)cl+=(cl?' ':'')+'sl sl1';
 var cut=t.length>LIM;if(cut&&!ti)ti=t;
 return '<td'+(cl?" class='"+cl+"'":'')+(ti?" title='"+esc(ti)+"'":'')+'>'+esc(cut?t.slice(0,LIM)+'…':t)+tg+'</td>'}
var NC=D.widths.length,X=[0],c0=NC,c1=2,CB=40;
for(var j=0;j<NC;j++)X.push(X[j]+D.widths[j]);
function cols(){var left=W.scrollLeft+X[2],right=W.scrollLeft+W.clientWidth,p=2,q=NC;
 while(p<NC&&X[p+1]<=left)p++;q=p;while(q<NC&&X[q]<right)q++;return [p,q]}
function gap(n){return n>0?"<td class='gap' colspan='"+n+"'></td>":''}
function tr(i){var r=D.rows[i],h="<tr class='"+(r[1]?'dirty':'clean')+"'>"+td(r[0],0)+td(r[2],1)+gap(c0-2);
 for(var j=c0;j<c1;j++)h+=td(r[j+1],j);return h+gap(NC-c1)+'</tr>'}
function sp(){var e=document.createElement('tr');e.className='sp';e.innerHTML="<td colspan='"+D.widths.length+"'></td>";return e}
var top=sp(),bot=sp(),idx=[],a=0,b=0,txt=[];
function html(x,y){var h='';for(var k=x;k<y;k++)h+=tr(idx[k]);return h}
function fill(){top.firstChild.style.height=(a*H)+'px';bot.firstChild.style.height=((idx.length-b)*H)+'px'}
function draw(full){
 var v=cols();
 if(v[0]<c0||v[1]>c1){c0=Math.max(2,v[0]-CB);c1=Math.min(NC,v[1]+CB);full=true}
 var f=Math.floor(Math.max(0,W.scrollTop-2*H)/H),l=f+Math.ceil(W.clientHeight/H);
 if(!full&&b>a&&(f>=a||a===0)&&(l<=b||b===idx.length)&&(f-a>=0)&&(b-l>=0))return;
 var na=Math.max(0,(Math.floor(f/BLK)-1)*BLK),nb=Math.min(idx.length,(Math.floor(l/BLK)+2)*BLK);
 if(full||nb<=a||na>=b){tb.textContent='';tb.appendChild(top);tb.insertAdjacentHTML('beforeend',html(na,nb));tb.appendChild(bot)}
 else{var k;
  if(na>a){for(k=a;k<na;k++)tb.removeChild(top.nextSibling)}else if(na<a){top.insertAdjacentHTML('afterend',html(na,a))}
  if(nb<b){for(k=nb;k<b;k++)tb.removeChild(bot.previousSibling)}else if(nb>b){bot.insertAdjacentHTML('beforebegin',html(b,nb))}}
 a=na;b=nb;fill()}
function norm(s){return String(s).normalize('NFKC').toLowerCase()}
function text(i){if(txt[i]===undefined){var r=D.rows[i],p=[r[0],r[2]];
 for(var j=3;j<r.length;j++){var c=r[j];p.push(typeof c!=='object'?c:c[0]+(c.length>3?' '+D.tags[c[3]].join(' '):''))}
 txt[i]=norm(p.join('\t'))}return txt[i]}
function filter(){var o=only&&only.checked,s=q?norm(q.value.trim()):'';idx=[];
 for(var i=0;i<D.rows.length;i++)if((!o||D.rows[i][1])&&(!s||text(i).indexOf(s)>=0))idx.push(i);
 cnt.textContent=(o||s)?' '+idx.length+' / '+D.rows.length+' 行':'';W.scrollTop=0;draw(true)}
var busy=false;
W.addEventListener('scroll',function(){if(!busy){busy=true;requestAnimationFrame(function(){busy=false;draw(false)})}},{passive:true});
window.addEventListener('resize',function(){draw(false)});
if(only)only.addEventListener('change',filter);
var timer;if(q)q.addEventListener('input',function(){clearTimeout(timer);timer=setTimeout(filter,150)});
filter()}
if(typeof Z==='undefined'){T(typeof D==='undefined'?null:D);return}
function fail(){var c=document.getElementById('cnt');if(c)c.textContent=' このブラウザでは表を表示できません。新しい Edge / Chrome で開いてください';T(null)}
if(typeof DecompressionStream==='undefined'){fail();return}
try{var b=atob(Z),u=new Uint8Array(b.length);for(var i=0;i<b.length;i++)u[i]=b.charCodeAt(i);
new Response(new Blob([u]).stream().pipeThrough(new DecompressionStream('gzip'))).text().then(function(t){T(JSON.parse(t))},fail)}catch(e){fail()}})();"""


def _row_list(rows: list[int], limit: int = 30) -> str:
    """問題の内訳の「行」欄。行が多いと HTML が大きくなるので先頭だけ書く（全部は JSON レポートにある）。"""
    head = ", ".join(map(str, rows[:limit]))
    return head + (f" ほか {len(rows) - limit} 行" if len(rows) > limit else "")


def embedded_table(page: str) -> dict | None:
    """HTML レポートに埋め込んだ「変換前後」の表のデータを取り出す（テスト・agent 向け）。

    rows の各行 = [行番号, 問題あり(0/1), キー, セル...]。セル = 文字列 | [文字列, 色] | [文字列, 色, titles の番号, tags の番号]。
    """
    m = re.search(r"<script>const D=(.*?);\(function\(\)", page, re.S)
    if m:
        return json.loads(m.group(1))
    m = re.search(r'<script>const Z="([A-Za-z0-9+/=]*)";', page)
    return json.loads(gzip.decompress(base64.b64decode(m.group(1))).decode("utf-8")) if m else None


def _embed(table_data, compress: bool) -> str:
    """表のデータを <script> の先頭に置く。compress=True なら gzip + base64（メールに添付できる大きさにする）。"""
    if table_data is None:
        return "const D=null;"
    if not compress:
        return f"const D={_json_for_script(table_data)};"
    raw = json.dumps(table_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f'const Z="{base64.b64encode(gzip.compress(raw, 9, mtime=0)).decode("ascii")}";'


def _json_for_script(obj) -> str:
    """<script> の中に埋め込む JSON。値に </script> などがあってもタグにならないよう < > & を逃がす。"""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _text_width(s: str) -> float:
    w = 0.0
    for ch in s[:DISPLAY_CHARS]:
        w += 13 if unicodedata.east_asian_width(ch) in "WF" else 7.4
    return w


def _widths(labels: list[str], data_rows: list[list], tagsets: list[tuple]) -> list[int]:
    """列の幅(px)。行を差し替えても幅が変わらないよう、載せる全行から先に決める。"""
    tag_w = [sum(len(t) * 6.5 + 14 for t in ts) for ts in tagsets]
    best = [_text_width(x) * 0.95 + 4 for x in labels]       # 見出しは太字
    for r in data_rows:
        best[0] = max(best[0], _text_width(str(r[0])))
        for j, c in enumerate(r[2:], start=1):
            if isinstance(c, str):
                w = _text_width(c)
            else:
                w = _text_width(c[0]) + (tag_w[c[3]] if len(c) > 3 else 0)
            if w > best[j]:
                best[j] = w
    return [int(min(max(w + 18, 44), 420)) for w in best]


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def build_report(*, version: str, command: str, input_path: str, config_path: str, config_sha: str,
                 header_sha: str, rows: int, errors: int, warnings: int, files: dict, groups: list[dict],
                 coverage: list[dict], issues: list[dict], changes: list[dict], verification: dict,
                 not_idempotent: list[dict], lint: list[dict], drift: dict | None,
                 provenance: dict | None = None) -> dict:
    """機械が読むレポート（tenugui.report/1）。変換1回ぶんの結果をすべて持つ。"""
    return {"schema": "tenugui.report/1", "version": version,
            "time": datetime.datetime.now().isoformat(timespec="seconds"), "command": command,
            "input": input_path, "config": config_path, "config_sha": config_sha, "header_sha": header_sha,
            "rows": rows, "counts": {"error": errors, "warning": warnings}, "files": files,
            "issue_groups": groups, "case_coverage": coverage, "issues": issues, "changes": changes,
            "verification": verification, "not_idempotent": not_idempotent, "lint": lint,
            "header_drift": drift,
            "provenance": provenance or {"corrections_file": None, "corrections_sha": "",
                                         "counts": {"normalize": len(changes), "correct": 0, "hold": 0},
                                         "fix_requests": [], "cells": [], "unused": [], "glossary": []}}


def write_json(path: str | Path, rep: dict) -> None:
    Path(path).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")


def _layout(cfg: Config, uh: list[str], extra: list[str] = ()) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """表の列構成: (元の列, その出力列...) の並びと、派生列。
    extra = ルールは無いが訂正・保留のかかった列（元の値 → 出力 を並べて見せる）。"""
    groups: dict[str, list[str]] = {}
    for fr in cfg.fields:
        if fr.src in uh:
            outs = groups.setdefault(fr.src, [])
            if fr.out not in outs:
                outs.append(fr.out)
    for c in extra:
        if c in uh and c not in groups:
            groups[c] = [c]
    return list(groups.items()), [d.out for d in cfg.derives]


def _provenance_html(pv: dict) -> list[str]:
    """修正依頼リスト（訂正・保留）と、用語対応表（この入力で使われた表記）。"""
    P: list[str] = []
    if pv["corrections_file"]:
        P.append("<h2>元データの修正依頼リスト（訂正・保留）</h2>"
                 f"<div class='meta'>訂正ファイル: {_e(pv['corrections_file'])}（sha {_e(pv['corrections_sha'])}）。"
                 "訂正 = 正解が自明な誤記を直して変換した値 / 保留 = 判断が要るため変換せず元の値のまま通した値。"
                 "どちらも元データ側で直すと、この一覧から消えます</div>")
        if pv["fix_requests"]:
            P.append("<div class='wrap'><table><thead><tr><th>種類</th><th>件数</th><th>列</th><th>元の値</th>"
                     "<th>訂正後</th><th>理由</th><th>根拠</th><th>該当（先頭列の値）</th></tr></thead><tbody>")
            for g in pv["fix_requests"]:
                keys = ", ".join(g["keys"]) + (" …" if g["keys_truncated"] else "")
                P.append(f"<tr><td class='{'cor' if g['kind'] == 'correct' else 'hold'}'>{KIND_LABEL[g['kind']]}</td>"
                         f"<td class='num'>{g['count']}</td><td>{_e(g['column'])}</td><td>{_e(g['value'])}</td>"
                         f"<td>{_e(g['to']) if g['kind'] == 'correct' else '（変換していません）'}</td>"
                         f"<td class='wrapc'>{_e(g['reason'])}</td><td class='wrapc'>{_e(g['ref'])}</td>"
                         f"<td class='wrapc'>{_e(keys)}</td></tr>")
            P.append("</tbody></table></div>")
        else:
            P.append("<p class='ok'>訂正・保留に当たった値はありません</p>")
        if pv["unused"]:
            P.append("<p><b>当たらなかった訂正・保留</b>（元データが直ったなら消せます）</p><ul>"
                     + "".join(f"<li>{_e(u['code'])} {_e(u['where'])}: {_e(u['message'])}</li>" for u in pv["unused"])
                     + "</ul>")
    gl = [g for g in pv["glossary"] if g["unified"]]
    if gl:
        total = sum(g["unified"] for g in gl)
        P.append(f"<h2>用語対応表（この入力で使われた表記）</h2><details><summary>正式名と違う表記 {total} 件を統一しました"
                 f"（{len(gl)} 語）</summary><div class='wrap'><table><thead><tr><th>同義語表</th><th>正式名</th>"
                 "<th>統一した件数</th><th>使われていた表記（件数）</th><th>列</th></tr></thead><tbody>")
        for g in gl[:500]:
            vs = " / ".join(f"{_e(v['value'])}（{v['count']}）" for v in g["variants"] if not v["canonical"])
            P.append(f"<tr><td>{_e(g['set'])}</td><td><b>{_e(g['canonical'])}</b></td><td class='num'>{g['unified']}</td>"
                     f"<td class='wrapc'>{vs}</td><td>{_e(', '.join(g['columns']))}</td></tr>")
        P.append("</tbody></table></div></details>")
    return P


def render_html(rep: dict, cfg: Config, header: list[str], rows: list[list[str]],
                out_header: list[str], out_rows: list[list[str]], issues: list[Issue],
                prov: Provenance | None = None, compress: bool = True) -> str:
    """人が見るレポート。ルールのかかった列について 元の値 → 変換後 を並べ、問題のあるセルに色を付ける。"""
    uh, _ = uniquify(header)
    iidx = {h: i for i, h in enumerate(uh)}
    oidx = {h: i for i, h in enumerate(out_header)}
    prov = prov or Provenance()
    pv = rep["provenance"]
    groups, derives = _layout(cfg, uh, sorted({c for _, c in [*prov.corrected, *prov.held]}, key=uh.index))
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
    bad_rows = {r for r, _ in marks} | {r for r, _ in prov.corrected}

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
             + (f"<div class='card{' cor' if pv['counts']['correct'] else ''}'><b>{pv['counts']['correct']}</b><span>訂正</span></div>"
                f"<div class='card{' hold' if pv['counts']['hold'] else ''}'><b>{pv['counts']['hold']}</b><span>保留</span></div>"
                if pv["corrections_file"] else "") +
             f"<div class='card{'' if ver['ok'] else ' err'}'><b>{'OK' if ver['ok'] else 'NG'}</b>"
             f"<span>変換前後チェック（違反 {s['violations']} / 欠落 {s['lost_cells']}）</span></div></div>")

    if rep["issue_groups"]:
        P.append("<h2>問題の内訳（列 × コード × 形）</h2><div class='wrap'><table><thead><tr><th>件数</th><th>コード</th>"
                 "<th>列</th><th>形</th><th>例</th><th>行</th><th>直し方</th></tr></thead><tbody>")
        for g in rep["issue_groups"]:
            cls = "err" if g["severity"] == "E" else ("hold" if g["code"] == "C141" else "warn")
            col = g["column"] + (f" → {g['out']}" if g["out"] and g["out"] != g["column"] else "")
            P.append(f"<tr><td class='num'>{g['count']}</td><td class='{cls}'>{_e(g['code'])}</td><td>{_e(col)}</td>"
                     f"<td><code>{_e(g['shape'])}</code></td><td>{_e(' / '.join(g['examples']))}</td>"
                     f"<td>{_e(_row_list(g['rows']))}</td>"
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

    P += _provenance_html(pv)

    # ---- 変換前後の表
    limited = len(rows) > MAX_HTML_ROWS
    show = [n for n in range(1, len(rows) + 1) if (not limited or n in bad_rows)][:MAX_HTML_ROWS]
    P.append("<h2>変換前後（ルールのかかった列）</h2>")
    P.append("<p class='legend'><span style='background:var(--err)'>エラー（変換できず）</span>"
             "<span style='background:var(--warn)'>警告（要確認）</span>"
             f"<span style='background:var(--chg)'>値を変換した{'（表記統一・単位）' if pv['corrections_file'] else ''}</span>"
             + ("<span style='background:var(--cor)'>訂正（誤記を直して変換）</span>"
                "<span style='background:var(--hold)'>保留（変換していない）</span>" if pv["corrections_file"] else "") +
             "<span style='background:var(--ng)'>抽出NG印（そのまま）</span>"
             "<span style='background:var(--src)'>元の値</span></p>")
    P.append("<p><label class='f'><input type='checkbox' id='only'> 問題のある行だけ表示</label>"
             + (f"<span class='meta'>行数が多いため、問題のある行だけを載せています（{len(show)} / {len(rows)} 行）</span>"
                if limited else f"<span class='meta'>{len(rows)} 行。色の付いたセルにカーソルを合わせると理由が出ます</span>")
             + "</p>")
    table_data = None
    if not groups and not derives:
        P.append("<p class='meta'>ルールのかかった列はありません</p>")
    else:
        key_name = uh[0] if uh else ""
        # 表は行ごとのデータを埋め込み、見えている行だけをブラウザで描く（行数・列数が多くてもスクロールが重くならない）。
        # セル = 文字列 | [文字列, 色] | [文字列, 色, 理由の番号, 印の番号]。理由と印は重複が多いので表にまとめる
        titles: dict[str, int] = {}
        tagsets: dict[tuple, int] = {}

        def cell(n: int, col, text: str, base: str):
            m = marks.get((n, col))
            if m:
                cls = "err" if m[0] == "E" else ("hold" if m[2] == ["C141"] else "warn")
                tags = tuple("保留" if x == "C141" else x for x in m[2])
                return [text, cls, titles.setdefault(" / ".join(m[1]), len(titles)), tagsets.setdefault(tags, len(tagsets))]
            return [text, base] if base else text

        h1 = [["行", 1, 2], [key_name, 1, 2]]          # [見出し, 列数, 行数]
        h2: list[str] = []
        for src, outs in groups:
            h1.append([src, 1 + len(outs), 1])
            h2 += ["元の値", *("変換後" if o == src else o for o in outs)]
        if derives:
            h1.append(["計算した列", len(derives), 1])
            h2 += list(derives)
        data_rows = []
        for n in show:
            r_in, r_out = rows[n - 1], out_rows[n - 1]
            r = [n, 1 if n in bad_rows else 0, r_in[0] if r_in else ""]
            for src, outs in groups:
                i = iidx[src]
                raw = r_in[i] if i < len(r_in) else ""
                ng = _is_reserved(cfg, raw)
                cor = prov.corrected.get((n, src))
                if cor and (n, ("src", src)) not in marks:
                    r.append([raw, "cor", titles.setdefault(f"訂正 {raw} → {cor[1].to}（理由: {cor[1].reason}）", len(titles)),
                              tagsets.setdefault(("訂正",), len(tagsets))])
                else:
                    r.append(cell(n, ("src", src), raw, "ng" if ng else "src"))
                for o in outs:
                    val = r_out[oidx[o]] if o in oidx else ""
                    base = "ng" if ng else ("cor" if cor and val != "" else ("chg" if val != "" and val != raw else ""))
                    r.append(cell(n, o, val, base))
            for o in derives:
                r.append(cell(n, o, r_out[oidx[o]] if o in oidx else "", ""))
            data_rows.append(r)
        widths = _widths(["行", key_name, *h2], data_rows, list(tagsets))
        table_data = {"h1": h1, "h2": h2, "widths": widths, "rows": data_rows,
                      "titles": list(titles), "tags": [list(t) for t in tagsets]}
        P.append("<p class='tools'><label class='f'>行を探す <input type='search' id='q' "
                 "placeholder='品番・値・コードなど'></label><span class='meta' id='cnt'></span></p>"
                 f"<div class='wrap vt' id='vt'><table style='width:{sum(widths)}px'><colgroup>"
                 + "".join(f"<col style='width:{w}px'>" for w in widths) + "</colgroup><thead><tr>")
        for k, (label, span, rs) in enumerate(h1):
            cls = "sl sl0" if k == 0 else ("sl sl1" if k == 1 else "g")
            P.append(f"<th class='{cls}'" + (f" colspan='{span}'" if span > 1 else "")
                     + (f" rowspan='{rs}'" if rs > 1 else "") + f">{_e(label)}</th>")
        P.append("</tr><tr class='h2'>" + "".join(f"<th>{_e(x)}</th>" for x in h2)
                 + "</tr></thead><tbody></tbody></table></div>")

    return ("<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Tenugui レポート — {_e(Path(rep['input']).name)}</title><style>{_CSS}</style></head><body>"
            + "".join(P) + f"<script>{_embed(table_data, compress)}{_JS}</script></body></html>")

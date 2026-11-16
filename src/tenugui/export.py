"""表を CSV / Excel(.xlsx) に書き出す（標準ライブラリのみ）。

- 拡張子で形式を決める: .xlsx → Excel、それ以外 → CSV（utf-8-sig。Excel でそのまま開ける）
- 表が複数あるとき: xlsx は1ファイルに複数シート、CSV は <名>_<表名>.csv に分けて書く
- 値は文字列のまま書く（"35mm" や "001" を Excel が勝手に数値・日付にしないように）
  数値として読めるもの（int/float 型の値）だけ数値セルにする
"""
from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

Table = tuple[str, list[str], list[list]]      # (表名, 見出し, 行)

_BAD_SHEET = re.compile(r"[\[\]:*?/\\]")
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def export_tables(path: str | Path, tables: list[Table]) -> list[Path]:
    """表を書き出し、書いたファイルの一覧を返す。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() == ".xlsx":
        write_xlsx(p, tables)
        return [p]
    if len(tables) == 1:
        write_csv_table(p, tables[0][1], tables[0][2])
        return [p]
    out = []
    for name, header, rows in tables:
        q = p.with_name(f"{p.stem}_{_safe_name(name)}{p.suffix or '.csv'}")
        write_csv_table(q, header, rows)
        out.append(q)
    return out


def write_csv_table(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(["" if v is None else v for v in r] for r in rows)


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "table"


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f'<c r="{ref}"><v>{v}</v></c>'
    t = escape(_XML_BAD.sub("", str(v)))
    sp = ' xml:space="preserve"' if t != t.strip() else ""
    return f'<c r="{ref}" t="inlineStr"><is><t{sp}>{t}</t></is></c>'


def _sheet_xml(header: list[str], rows: list[list]) -> str:
    out = []
    for i, r in enumerate([header] + [list(x) for x in rows]):
        cells = "".join(_cell(f"{_col(j)}{i + 1}", v) for j, v in enumerate(r))
        out.append(f'<row r="{i + 1}">{cells}</row>')
    freeze = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
              'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{freeze}<sheetData>{"".join(out)}</sheetData></worksheet>')


def _sheet_names(tables: list[Table]) -> list[str]:
    names, seen = [], set()
    for name, _, _ in tables:
        base = (_BAD_SHEET.sub("_", name).strip("'") or "Sheet")[:31]
        n, k = base, 2
        while n.lower() in seen:
            suf = f"_{k}"
            n, k = base[:31 - len(suf)] + suf, k + 1
        seen.add(n.lower())
        names.append(n)
    return names


def write_xlsx(path: Path, tables: list[Table]) -> None:
    names = _sheet_names(tables)
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(tables)))
                   + '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{ns_r}"><sheets>'
                   + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                             for i, n in enumerate(names))
                   + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                             for i in range(len(tables)))
                   + '</Relationships>')
        for i, (_, header, rows) in enumerate(tables):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(header, rows))

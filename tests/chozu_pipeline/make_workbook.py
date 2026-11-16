"""chozu → tenugui 連携テスト用の架空帳票（xlsx）を作る。社内情報は含まない説明用のダミー。

    python tests/chozu_pipeline/make_workbook.py OUT.xlsx

わざと意地悪な内容にしてある: 書き方の混在、全角、セル内改行、カンマや引用符、
表示形式つきの数値、抜けた項目、紛らわしいシート名。
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from openpyxl import Workbook

# シート名 → 各項目の値。None はセルを空に、MISSING は見出しごと書かない（chozu では NG1）
MISSING = object()
SHEETS: list[tuple[str, dict]] = [
    ("A-001", dict(part="SAMPLE-001", kind="標準", width="8.5mm", length="330±15mm",
                   rating="1250W±100W(11.5～13.5A)", time=30, note=["通常品"])),
    ("A-002", dict(part="SAMPLE-002", kind="標準型", width="８．５ｍｍ", length="330mm±15",
                   rating="1250W±100W（11.5〜13.5A）", time="30分", note=["全角で記入"])),
    ("A-003", dict(part="SAMPLE-003", kind="ｽﾀﾝﾀﾞｰﾄﾞ", width=("fmt", 8.5, '0.0"mm"'), length="330mm±15mm",
                   rating="3400W＋238－272(15.64～18.19A)", time="0.5h", note=["表示形式つきの数値"])),
    ("#004", dict(part="SAMPLE-004", kind="特殊", width="0.85cm", length=("fmt", 590, '0"±"\\1"mm"'),
                  rating="900W", time=30.0, note=["シート名が # で始まる"])),
    ("S,005", dict(part="SAMPLE,005", kind="特殊型", width="8.5 mm", length="330 ± 15 mm",
                   rating="1250W ± 100W (11.5 ～ 13.5A)", time="３０", note=["シート名と品番にカンマ", "2行目, カンマ入り"])),
    ('S"006', dict(part='SAMPLE"006"', kind="標準", width="8.5", length="330",
                   rating="1250", time="30", note=['引用符 " を含む', "単位なしの値"])),
    ("NG1", dict(part="SAMPLE-007", kind="標準", width=None, length=MISSING,
                 rating="NG", time=None, note=["シート名が抽出NG印と同じ"])),
    ("S 008", dict(part=" SAMPLE-008 ", kind="不明な種別", width="約8.5mm", length="330～345mm",
                   rating="1,250W±100W(11.5～13.5A)", time="-", note=["セル内\n改行あり", "=1+1", "<script>alert(1)</script>"])),
    ("試験🙂009", dict(part="SAMPLE-009", kind="標準", width="8.5ｍｍ", length="φ330±15",
                     rating="1.25kW±0.1kW(11.5～13.5A)", time=1e-3, note=["絵文字のシート名"])),
    ("EMPTY", dict(part=MISSING, kind=MISSING, width=MISSING, length=MISSING, rating=MISSING, time=MISSING,
                   note=MISSING)),
]

LABELS = [("part", "品番", 2), ("kind", "種別", 3), ("width", "幅", 6), ("length", "全長", 7),
          ("rating", "定格", 10), ("time", "試験時間", 11)]


def put(ws, row: int, label: str, value) -> None:
    if value is MISSING:
        return
    ws.cell(row, 1, label)
    if value is None:
        return
    if isinstance(value, tuple) and value[0] == "fmt":
        c = ws.cell(row, 3, value[1])
        c.number_format = value[2]
    else:
        ws.cell(row, 3, value)


def build(path: str | Path) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    for name, v in SHEETS:
        ws = wb.create_sheet(name)
        if v["part"] is not MISSING:
            ws.cell(1, 1, "発 行")
            ws.cell(1, 3, dt.datetime(2026, 4, 1))
        ws.cell(5, 1, "1.寸法")
        ws.cell(9, 1, "2.試験")
        for key, label, row in LABELS:
            put(ws, row, label, v[key])
        if v["note"] is not MISSING:
            ws.cell(12, 1, "備考")
            for k, line in enumerate(v["note"]):
                ws.cell(13 + k, 1, line)
    path = Path(path)
    wb.save(path)
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "pipeline.xlsx"))

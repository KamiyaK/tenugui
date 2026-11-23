"""HTML レポートの「変換前後」の表（0.16.0）: データを埋め込み、見えている行・列だけを JS で描く。"""
import re
import shutil
import subprocess

import pytest

from tenugui.cli import main
from tenugui.report import _row_list, _widths, embedded_table

RULES = """schema_version = 1
[[field]]
src = "len"
to = "mm"
default_unit = "mm"
[[field]]
src = "tol"
preset = "tolerance"
parts = { nom = "t_nom", tol = "t_tol" }
"""


def report(tmp_path, rows):
    import csv
    with open(tmp_path / "in.csv", "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows([["key", "len", "tol"], *rows])
    (tmp_path / "r.toml").write_text(RULES, encoding="utf-8")
    main(["convert", str(tmp_path / "in.csv"), "-c", str(tmp_path / "r.toml")])
    return (tmp_path / "tenugui_out" / "in_tenugui.html").read_text(encoding="utf-8")


def test_data_shape_matches_the_header(tmp_path):
    page = report(tmp_path, [["A", "40mm", "10±0.2"], ["B", "4cm", "x"], ["C", "41", "12±0.1"]])
    t = embedded_table(page)
    assert t["h1"] == [["行", 1, 2], ["key", 1, 2], ["len", 2, 1], ["tol", 3, 1]]
    assert t["h2"] == ["元の値", "変換後", "元の値", "t_nom", "t_tol"]
    assert len(t["widths"]) == 2 + len(t["h2"]) and all(44 <= w <= 420 for w in t["widths"])
    assert all(len(r) == 1 + len(t["widths"]) for r in t["rows"])
    assert [(r[0], r[1], r[2]) for r in t["rows"]] == [(1, 0, "A"), (2, 1, "B"), (3, 0, "C")]
    bad = t["rows"][1][5]
    assert bad[1] == "err" and t["tags"][bad[3]] == ["C111"] and "C111" in t["titles"][bad[2]]
    assert page.count("<col ") == len(t["widths"])


def test_hostile_values_never_become_tags(tmp_path):
    page = report(tmp_path, [["</script><script>alert(1)</script>", "<img src=x onerror=1>", "&amp; "]])
    data = re.search(r"const D=(.*?);\(function", page, re.S).group(1)
    assert "<" not in data and ">" not in data and "&" not in data and " " not in data
    assert page.count("<script") == 1 and "<img" not in page
    assert embedded_table(page)["rows"][0][2] == "</script><script>alert(1)</script>"


def test_widths_are_fixed_from_all_rows_and_capped():
    rows = [[1, 0, "k", "短い"], [2, 0, "k", "とても長い値" * 40], [3, 0, "k", ["x", "err", 0, 0]]]
    w = _widths(["行", "key", "列"], rows, [("C111", "C131")])
    assert w[2] == 420                         # 長い値は上限で切る（全文はカーソルを合わせると出る）
    assert _widths(["行", "key", "列"], rows[:1], [])[2] < w[2]


def test_issue_row_list_is_shortened():
    assert _row_list(list(range(1, 6))) == "1, 2, 3, 4, 5"
    assert _row_list(list(range(1, 101))).endswith("30 ほか 70 行")


@pytest.mark.skipif(not shutil.which("node"), reason="node がない")
def test_script_is_valid_javascript(tmp_path):
    page = report(tmp_path, [["A", "40mm", "10±0.2"]])
    js = tmp_path / "r.js"
    js.write_text(re.search(r"<script>(.*?)</script>", page, re.S).group(1), encoding="utf-8")
    subprocess.run(["node", "--check", str(js)], check=True)

"""0.7.0: 形の集計 / pattern 記法 / case 形式 / 問題のグループ化 / [[test]] / impact / suggest"""
import json
import re

import pytest

from tenugui.analysis import (case_coverage, compare_outputs, group_issues, impact, run_tests,
                              shape_counts, suggest, survey_full, survey_overview, as_test_block, try_row)
from tenugui.cli import main
from tenugui.core import ConfigError, compile_pattern, config_from_dict, convert_rows, shape_of
from collections import Counter


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


MM = {"number_only": True, "strip_units": ["mm"]}
CASES = {"src": "len", "drop_src": True, "case": [
    {"pattern": "#±#mm", "parts": {"n1": {"out": "nom", **MM}, "n2": {"out": "tol", **MM}}},
    {"pattern": "{nom}mm", "parts": {"nom": {"out": "nom", **MM}}}]}
H = ["sheet", "len"]
ROWS = [["a", "330±15mm"], ["b", "330mm"], ["c", "330～345mm"], ["d", "NG"], ["e", ""], ["f", "340～350mm"]]


# ---- 形 -----------------------------------------------------------------
@pytest.mark.parametrize("raw,shape", [
    ("330±15mm", "#±#mm"), ("３３０±１５ｍｍ", "#±#mm"), ("1250W±100W(11.5～13.5A)", "#W±#W(#~#A)"),
    ("φ13±0.2", "φ#±#"), ("1,200V", "#V"), ("標準", "標準"), ("7  mm", "# mm")])
def test_shape_of(raw, shape):
    assert shape_of(raw) == shape


def test_shape_counts_and_survey():
    sc = shape_counts(Counter({"330±15mm": 3, "1040±15mm": 2, "330mm": 1}))
    assert [(s["shape"], s["count"], s["distinct"]) for s in sc] == [("#±#mm", 5, 2), ("#mm", 1, 1)]
    assert sc[0]["examples"][0] == "330±15mm"
    s = survey_full(cfg(), H, ROWS, "len")
    assert {x["shape"] for x in s["shapes"]} == {"#±#mm", "#mm", "#~#mm"}     # NG・空は数えない
    ov = survey_overview(cfg(), H, ROWS)
    assert ov[1]["shape_count"] == 3 and ov[1]["ng"] == 1 and ov[1]["filled"] == 4


# ---- pattern ------------------------------------------------------------
def test_pattern_basic_and_unit_borrow():
    c = cfg(field=[{"src": "len", "pattern": "#±#mm",
                    "parts": {"n1": {"out": "nom", **MM}, "n2": {"out": "tol", **MM}}}])
    cols, rows, issues = convert_rows(c, H, [["a", "7±2mm"], ["b", "７ ± ２ ㎜"], ["c", "7±2cm"], ["d", "7mm"]])
    d = [dict(zip(cols, r)) for r in rows]
    assert (d[0]["nom"], d[0]["tol"]) == ("7", "2") and (d[1]["nom"], d[1]["tol"]) == ("7", "2")
    assert [(i.row, i.code) for i in issues] == [(3, "C111"), (4, "C111")]    # 単位は書いた通りにだけ一致


def test_pattern_named_optional_and_convert():
    c = cfg(field=[{"src": "pw", "pattern": "{w}W[±{tol}W][({lo}~{hi}A)]", "parts": {
        "w": {"out": "w", "to": "W"}, "tol": {"out": "w_tol", "to": "W"},
        "lo": {"out": "a_min", "to": "mA"}, "hi": {"out": "a_max", "to": "A"}}}])
    cols, rows, issues = convert_rows(c, ["s", "pw"], [["a", "1250W±100W(11.5～13.5A)"], ["b", "900W"]])
    d = [dict(zip(cols, r)) for r in rows]
    assert (d[0]["w"], d[0]["w_tol"], d[0]["a_min"], d[0]["a_max"]) == ("1250", "100", "11500", "13.5")
    assert (d[1]["w"], d[1]["w_tol"], d[1]["a_min"]) == ("900", "", "") and not issues


def test_pattern_unit_order_stays_inside_parentheses():
    _, order = compile_pattern("#W±#(#~#A)")
    assert order == {"n1": ["u_n1"], "n2": ["u_n1"], "n3": ["u_n4"], "n4": ["u_n4"]}


@pytest.mark.parametrize("pat,msg", [("mm", "数値"), ("{a", "閉じて"), ("#[±#", "閉じて"), ("#]", "対応"),
                                     ("{a}{a}", "重複"), ("{u_x}", "名前")])
def test_pattern_errors(pat, msg):
    with pytest.raises(ConfigError) as e:
        cfg(field=[{"src": "a", "pattern": pat, "parts": {"n1": {"out": "o"}}}])
    assert msg in str(e.value)


def test_pattern_part_name_error_lists_names():
    with pytest.raises(ConfigError) as e:
        cfg(field=[{"src": "a", "pattern": "#±#mm", "parts": {"nom": {"out": "o"}}}])
    assert "n1, n2" in str(e.value)


def test_only_one_matcher_kind():
    with pytest.raises(ConfigError) as e:
        cfg(field=[{"src": "a", "pattern": "#mm", "preset": "tolerance", "parts": {"n1": {"out": "o"}}}])
    assert "どれか1つ" in str(e.value)


# ---- case ---------------------------------------------------------------
def test_case_first_match_wins_and_missing_part_is_empty():
    cols, rows, issues = convert_rows(cfg(field=[CASES]), H, ROWS)
    assert cols == ["sheet", "nom", "tol"]
    assert rows[0][1:] == ["330", "15"] and rows[1][1:] == ["330", ""]
    assert rows[2][1:] == ["", ""] and rows[3][1:] == ["NG", "NG"] and rows[4][1:] == ["", ""]
    assert [(i.row, i.code) for i in issues] == [(3, "C111"), (6, "C111")]     # 列ごとに重複しない
    assert "#~#mm" in issues[0].message


def test_case_coverage_and_trace():
    c = cfg(field=[CASES])
    cov = case_coverage(c, H, ROWS)[0]
    assert [x["count"] for x in cov["cases"]] == [1, 1] and (cov["unmatched"], cov["empty"], cov["ng"]) == (2, 1, 1)
    r = try_row(c, {"len": "330mm"})
    assert r["trace"]["nom"][0] == {"stage": "case", "value": "#2 {nom}mm"}
    assert r["trace"]["tol"][0]["stage"] == "case" and r["outputs"]["tol"] == ""


def test_case_mixes_preset_and_match():
    c = cfg(field=[{"src": "len", "case": [
        {"preset": "tolerance", "parts": {"nom": {"out": "nom", "to": "mm"}, "tol": {"out": "tol", "to": "mm"}}},
        {"match": r"^約(?P<v>\d+)mm$", "parts": {"v": {"out": "nom"}}}]}])
    cols, rows, issues = convert_rows(c, H, [["a", "33±1.5cm"], ["b", "約330mm"]])
    assert rows[0][2:] == ["330", "15"] and rows[1][2:] == ["330", ""] and not issues


def test_case_config_errors():
    def bad(f):
        with pytest.raises(ConfigError) as e:
            cfg(field=[f])
        return str(e.value)
    assert "1つ以上" in bad({"src": "a", "case": []})
    assert "未知のキー" in bad({"src": "a", "to": "mm", "case": [{"pattern": "#", "parts": {"n1": {"out": "o"}}}]})
    assert "sub は使えません" in bad({"src": "a", "case": [
        {"pattern": "#", "parts": {"n1": {"out": "o", "sub": [["x", "y"]]}}}]})
    assert "重複" in bad({"src": "a", "case": [
        {"pattern": "#±#", "parts": {"n1": {"out": "o"}, "n2": {"out": "o"}}}]})


# ---- 問題のグループ化 ------------------------------------------------------
def test_group_issues_by_shape():
    _, _, issues = convert_rows(cfg(field=[CASES]), H, ROWS)
    g = group_issues(issues)
    assert len(g) == 1 and g[0]["count"] == 2 and g[0]["shape"] == "#~#mm" and g[0]["rows"] == [3, 6]
    assert g[0]["examples"] == ["330～345mm", "340～350mm"] and g[0]["hint"]


# ---- [[test]] -----------------------------------------------------------
def test_run_tests_pass_and_fail():
    c = cfg(field=[CASES], test=[
        {"name": "ok", "in": {"len": "330±15mm"}, "out": {"nom": "330", "tol": "15"}},
        {"name": "ng値", "in": {"len": "330mm"}, "out": {"nom": "331"}},
        {"name": "外れ", "in": {"len": "x"}, "codes": ["C111"]},
        {"name": "コード違い", "in": {"len": "x"}, "out": {"nom": ""}}])
    r = run_tests(c)
    assert (r["total"], r["failed"]) == (4, 2)
    assert [f["name"] for f in r["failures"]] == ["ng値", "コード違い"]
    assert r["failures"][0]["diffs"] == [{"column": "nom", "expected": "331", "actual": "330"}]
    assert r["failures"][1]["diffs"][0]["column"] == "(codes)"


def test_test_config_errors():
    for t, msg in [({"out": {"a": "1"}}, "in ="), ({"in": {"a": "1"}}, "out（期待"),
                   ({"in": {"a": "1"}, "out": {}, "zz": 1}, "未知のキー")]:
        with pytest.raises(ConfigError) as e:
            cfg(test=[t])
        assert msg in str(e.value)


def test_test_block_roundtrip():
    import tomllib
    c = cfg(field=[CASES])
    v = {"len": "330±15mm"}
    blk = tomllib.loads(as_test_block(v, try_row(c, v), "基本"))["test"][0]
    assert blk == {"name": "基本", "in": v, "out": {"nom": "330", "tol": "15"}}
    assert run_tests(cfg(field=[CASES], test=[blk]))["ok"]
    bad = tomllib.loads(as_test_block({"len": "x"}, try_row(c, {"len": "x"})))["test"][0]
    assert bad["codes"] == ["C111"]


# ---- impact / compare ---------------------------------------------------
def test_impact_and_compare():
    base = cfg(field=[{**CASES, "case": CASES["case"][:1]}])
    new = cfg(field=[CASES])
    r = impact(base, new, H, ROWS)
    assert not r["same"] and r["changed_cells"] == 1
    assert r["columns"][0]["column"] == "nom" and r["columns"][0]["samples"][0] == {
        "row": 2, "key": "b", "before": "", "after": "330"}
    assert (r["issues_before"]["error"], r["issues_after"]["error"]) == (3, 2)
    assert impact(new, new, H, ROWS)["same"]
    c = compare_outputs(["k", "a", "b"], [["1", "x", "y"]], ["k", "a", "c"], [["1", "x", "z"]])
    assert c["columns_added"] == ["c"] and c["columns_removed"] == ["b"] and c["changed_cells"] == 0


# ---- suggest ------------------------------------------------------------
def test_suggest_add_case_applies_cleanly():
    import tomllib
    c = cfg(field=[CASES])
    g = suggest(c, H, ROWS)
    assert len(g) == 1 and g[0]["suggestion"]["kind"] == "add_case" and g[0]["suggestion"]["confidence"] == "low"
    frag = tomllib.loads(g[0]["suggestion"]["toml"])["field"]["case"][0]
    assert frag["pattern"] == "#~#mm" and frag["parts"]["n1"]["out"] == "nom"
    c2 = cfg(field=[{**CASES, "case": [*CASES["case"], frag]}])
    assert not convert_rows(c2, H, ROWS)[2]


def test_suggest_c104_and_plain_field():
    c = cfg(field=[{"src": "len", **MM}])
    g = suggest(c, H, [["a", "0.7cm"], ["b", "7inch"], ["c", "7±2mm"]])
    kinds = {x["code"] + ":" + x["shape"]: x["suggestion"]["kind"] for x in g}
    assert kinds == {"C104:#cm": "use_to", "C104:#inch": "add_strip_unit", "C104:#±#mm": "to_pattern"}
    c = cfg(field=[{"src": "len", "to": "mm"}])
    g = suggest(c, H, [["a", "7"]])
    assert g[0]["suggestion"]["kind"] == "add_default_unit"


def test_suggest_skips_ambiguous_literals():
    c = cfg(field=[{"src": "len", "pattern": "#mm", "parts": {"n1": {"out": "o"}}}])
    g = suggest(c, H, [["a", "7[mm]"]])
    assert g[0]["suggestion"]["kind"] == "none"


# ---- CLI ----------------------------------------------------------------
TOML = '''schema_version = 1
[[field]]
src = "len"
drop_src = true
[[field.case]]
pattern = "#±#mm"
parts = { n1 = { out = "nom", number_only = true, strip_units = ["mm"] }, n2 = { out = "tol", number_only = true, strip_units = ["mm"] } }
%s
[[test]]
in = { len = "330±15mm" }
out = { nom = "330", tol = "%s" }
'''
CASE2 = '''[[field.case]]
pattern = "#mm"
parts = { n1 = { out = "nom", number_only = true, strip_units = ["mm"] } }
'''


@pytest.fixture
def files(tmp_path):
    inp = tmp_path / "in.csv"
    inp.write_text("sheet,len\n" + "\n".join(",".join(r) for r in ROWS) + "\n", encoding="utf-8-sig")
    old, new, bad = tmp_path / "old.toml", tmp_path / "new.toml", tmp_path / "bad.toml"
    old.write_text(TOML % ("", "15"), encoding="utf-8")
    new.write_text(TOML % (CASE2, "15"), encoding="utf-8")
    bad.write_text(TOML % ("", "99"), encoding="utf-8")
    return inp, old, new, bad


def _j(capsys):
    return json.loads(capsys.readouterr().out)


def test_cli_test_and_check(files, capsys):
    inp, old, new, bad = files
    assert main(["test", "-c", str(new), "--format", "json"]) == 0
    assert _j(capsys) == {"schema": "tenugui.test/1", "total": 1, "failed": 0, "ok": True, "failures": []}
    assert main(["test", "-c", str(bad)]) == 1
    assert "期待 '99' / 実際 '15'" in capsys.readouterr().out
    assert main(["check", "-c", str(bad), "--format", "json"]) == 1
    assert _j(capsys)["tests"]["failed"] == 1


def test_cli_convert_groups_and_coverage(files, capsys):
    inp, old, new, bad = files
    assert main(["convert", str(inp), "-c", str(new), "--format", "json", "--max-issues", "0"]) == 0
    j = _j(capsys)
    assert j["issues"] == [] and j["issues_truncated"]
    assert [(g["code"], g["shape"], g["count"]) for g in j["issue_groups"]] == [("C111", "#~#mm", 2)]
    assert j["case_coverage"][0]["unmatched"] == 2
    assert "問題の内訳" in open(j["files"]["log"], encoding="utf-8").read()


def test_cli_survey_overview_suggest_impact_compare(files, capsys, tmp_path):
    inp, old, new, bad = files
    assert main(["survey", str(inp), "--format", "json"]) == 0
    j = _j(capsys)
    assert j["schema"] == "tenugui.survey_overview/1" and [c["column"] for c in j["columns"]] == ["len"]
    assert main(["suggest", str(inp), "-c", str(old), "--format", "json"]) == 0
    j = _j(capsys)
    assert {g["shape"] for g in j["groups"]} == {"#mm", "#~#mm"} and not j["groups_truncated"]
    assert main(["impact", str(inp), "-c", str(new), "--base", str(old), "--format", "json"]) == 0
    j = _j(capsys)
    assert j["schema"] == "tenugui.impact/1" and j["changed_cells"] == 1
    assert main(["impact", str(inp), "-c", str(new), "--base", str(old), "--fail-on", "error"]) == 1
    capsys.readouterr()
    for name, c in (("a", old), ("b", new)):
        assert main(["convert", str(inp), "-c", str(c), "--out-dir", str(tmp_path / name)]) == 0
    capsys.readouterr()
    assert main(["compare", str(tmp_path / "a" / "in_tenugui.csv"), str(tmp_path / "b" / "in_tenugui.csv"),
                 "--format", "json"]) == 0
    assert _j(capsys)["changed_cells"] == 1


def test_cli_try_as_test(files, capsys):
    inp, old, new, bad = files
    assert main(["try", "-c", str(new), "--field", "len", "330mm", "--as-test", "--name", "単値"]) == 0
    out = capsys.readouterr().out
    assert 'name = "単値"' in out and 'out = { "nom" = "330", "tol" = "" }' in out

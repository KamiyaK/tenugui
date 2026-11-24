"""0.18.0: case の warn（書き方は本来と違うが値は取る）と、利用者が決める報告コード [codes]（CU000〜CU999）。"""
import json

import pytest

from tenugui.analysis import lint, try_row
from tenugui.cli import main
from tenugui.core import ConfigError, config_from_dict
from tenugui.report import embedded_table

BASE = {
    "schema_version": 1,
    "codes": {"CU001": {"severity": "W", "meaning": "単位なし（記載ルール違反）", "hint": "元データの修正を依頼"},
              "CU002": {"severity": "E", "meaning": "旧書式"}},
    "field": [{"src": "len", "case": [
        {"pattern": "#±#mm", "parts": {"n1": "len_nom", "n2": "len_tol"}},
        {"pattern": "#mm", "parts": {"n1": "len_nom"}, "warn": "公差なし"},
        {"pattern": "#", "parts": {"n1": "len_nom"}, "warn": "CU001"},
        {"pattern": "#cm", "parts": {"n1": "len_nom"}, "warn": "CU002"},
    ]}],
}


def run(v, cfg=BASE):
    return try_row(config_from_dict(cfg), {"len": v})


def codes(r):
    return [(i["code"], i["severity"]) for i in r["issues"]]


def test_no_warn_case_unchanged():
    r = run("10±1mm")
    assert r["outputs"]["len_nom"] == "10" and r["outputs"]["len_tol"] == "1"
    assert r["issues"] == []


def test_message_warn_gives_c151_and_keeps_value():
    r = run("12mm")
    assert r["outputs"]["len_nom"] == "12"
    assert codes(r) == [("C151", "W")]
    assert "公差なし" in r["issues"][0]["message"]
    assert any(t["stage"] == "warn" for t in r["trace"]["len_nom"])


def test_user_code_uses_definition():
    r = run("19")
    assert r["outputs"]["len_nom"] == "19"
    assert codes(r) == [("CU001", "W")]
    i = r["issues"][0]
    assert "単位なし" in i["message"] and i["hint"] == "元データの修正を依頼"


def test_user_code_error_severity_still_keeps_value():
    r = run("3cm")
    assert r["outputs"]["len_nom"] == "3"
    assert codes(r) == [("CU002", "E")]


def test_one_issue_per_value_even_with_many_parts():
    cfg = {"schema_version": 1, "field": [{"src": "v", "case": [
        {"pattern": "#~#A", "parts": {"n1": "lo", "n2": "hi"}, "warn": "範囲の書き方違い"}]}]}
    r = try_row(config_from_dict(cfg), {"v": "1~2A"})
    assert codes(r) == [("C151", "W")]


def test_unmatched_is_still_c111():
    assert codes(run("abc")) == [("C111", "E")]


def test_warn_in_test_codes():
    cfg = dict(BASE, test=[{"in": {"len": "19"}, "out": {"len_nom": "19"}, "codes": ["CU001"]}])
    from tenugui.analysis import run_tests
    assert run_tests(config_from_dict(cfg))["ok"]


@pytest.mark.parametrize("codes_tbl, case_warn, needle", [
    ({"C999": {"meaning": "x"}}, None, "CU000〜CU999"),
    ({"CU1": {"meaning": "x"}}, None, "CU000〜CU999"),
    ({"CU001": {"severity": "X", "meaning": "x"}}, None, "severity"),
    ({"CU001": {"severity": "W"}}, None, "meaning"),
    ({"CU001": {"meaning": "x", "colour": "red"}}, None, "未知のキー"),
    ({}, "CU009", "[codes] にありません"),
    ({}, "", "空は不可"),
    ({}, 3, "warn は"),
])
def test_config_errors(codes_tbl, case_warn, needle):
    case = {"pattern": "#", "parts": {"n1": "a"}}
    if case_warn is not None:
        case["warn"] = case_warn
    cfg = {"schema_version": 1, "codes": codes_tbl, "field": [{"src": "len", "case": [case]}]}
    with pytest.raises(ConfigError) as e:
        config_from_dict(cfg)
    assert needle in str(e.value)


def test_lint_broad_warn_case():
    cfg = {"schema_version": 1, "field": [{"src": "len", "case": [
        {"pattern": "#mm", "parts": {"n1": "a"}},
        {"match": "^(?P<t>.+)$", "parts": {"t": "b"}, "warn": "何でも"}]}]}
    assert [x["code"] for x in lint(config_from_dict(cfg))] == ["L601"]
    assert [x["code"] for x in lint(config_from_dict(BASE))] == []


def test_convert_reports(tmp_path, capsys):
    rules = tmp_path / "r.toml"
    rules.write_text(
        'schema_version = 1\n[codes]\nCU001 = { meaning = "単位なし", hint = "修正依頼" }\n'
        '[[field]]\nsrc = "len"\n'
        '[[field.case]]\npattern = "#±#mm"\nparts = { n1 = "len_nom", n2 = "len_tol" }\n'
        '[[field.case]]\npattern = "#"\nparts = { n1 = "len_nom" }\nwarn = "CU001"\n', encoding="utf-8")
    inp = tmp_path / "in.csv"
    inp.write_text("sheet,len\nA,10±1mm\nB,19\n", encoding="utf-8")
    code = main(["convert", str(inp), "-c", str(rules), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    assert code == 0                              # 警告では止めない（既定）
    assert j["counts"] == {"error": 0, "warning": 1}
    rep = json.loads((tmp_path / "tenugui_out" / "in_tenugui.json").read_text(encoding="utf-8"))
    cov = rep["case_coverage"][0]["cases"]
    assert [c["warn"] for c in cov] == [None, "CU001"]
    g = [g for g in rep["issue_groups"] if g["code"] == "CU001"][0]
    assert g["count"] == 1 and g["hint"] == "修正依頼"
    page = (tmp_path / "tenugui_out" / "in_tenugui.html").read_text(encoding="utf-8")
    cells = [c for r in embedded_table(page)["rows"] for c in r[3:] if isinstance(c, list)]
    assert any(c[1] == "fmt" for c in cells)      # 書式の警告は専用の色
    out = (tmp_path / "tenugui_out" / "in_tenugui.csv").read_text(encoding="utf-8-sig")
    assert "B,19,19," in out                      # 値は取れている
    assert main(["convert", str(inp), "-c", str(rules), "--fail-on", "warn", "--format", "json"]) == 1
    capsys.readouterr()


def test_schema_lists_new_keys(capsys):
    main(["schema", "--format", "json"])
    s = json.loads(capsys.readouterr().out)
    assert "warn" in s["toml"]["field"]["forms"]["case"]["case_keys"]
    assert s["toml"]["codes"]["table"] == "[codes]"
    assert "C151" in s["codes"] and "L601" in s["codes"]

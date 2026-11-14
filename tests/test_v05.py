from pathlib import Path

import pytest

from tenugui import ConfigError, config_from_dict, convert_rows, load_config

H = ["k", "v"]


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


def run(c, text):
    cols, rows, issues = convert_rows(c, H, [["a", text]])
    return dict(zip(cols, rows[0])), issues


TR = cfg(field=[{"src": "v", "preset": "tolerance_range", "parts": {
    "nom": {"out": "w", "to": "W", "unit_out": "add"}, "tol": {"out": "wt", "to": "W", "unit_out": "add"},
    "rlo": {"out": "alo", "to": "A", "unit_out": "add"}, "rhi": {"out": "ahi", "to": "A", "unit_out": "add"}}}])


@pytest.mark.parametrize("text,expect", [
    ("1250W±100W(11.5～13.5A)", ("1250W", "100W", "11.5A", "13.5A")),     # 依頼の例（単位つきのまま4分割）
    ("1250W±100W（11.5～13.5A）", ("1250W", "100W", "11.5A", "13.5A")),   # 全角括弧
    ("1.25kW±100W(500mA～1.2A)", ("1250W", "100W", "0.5A", "1.2A")),      # 単位が違えば換算
    ("1250W(11.5～13.5A)", ("1250W", "", "11.5A", "13.5A")),               # 許容差なし
    ("1250W±100W", ("1250W", "100W", "", "")),                              # 範囲なし
    ("1250W ± 100W ( 11.5A ~ 13.5A )", ("1250W", "100W", "11.5A", "13.5A")),
])
def test_tolerance_range_preset(text, expect):
    r, issues = run(TR, text)
    assert (r["w"], r["wt"], r["alo"], r["ahi"]) == expect and issues == []


def test_tolerance_range_unmatched_reported_once():
    r, issues = run(TR, "不明")
    assert [i.code for i in issues] == ["C111"] and r["w"] == ""


@pytest.mark.parametrize("text,expect", [
    ("330mm", "330"), ("5ボルト", "5"), ("20%", "20"), ("1.25mm2", "1.25"), ("4(二重)", "4"),
    ("1,200V", "1200"), ("３５㎜", "35"), ("−3.5mm", "-3.5"), ("10±0.2", "10"), ("100", "100"),
])
def test_number_only_forces_unit_removal_without_conversion(text, expect):
    r, issues = run(cfg(field=[{"src": "v", "number_only": True, "strip_units": ["*"]}]), text)
    assert r["v"] == expect and issues == []


def test_number_only_failure_follows_on_error_and_derive_can_use_it():
    c = cfg(field=[{"src": "v", "number_only": True, "strip_units": ["*"], "decimals": 1}],
            derive=[{"out": "d", "expr": "x * 2", "vars": {"x": "v"}}])
    r, issues = run(c, "12.34V")
    assert (r["v"], r["d"]) == ("12.3", "24.68") and issues == []   # 派生は丸め前の値(12.34)で計算する
    r, issues = run(c, "abc")
    assert r["v"] == "abc" and r["d"] == "" and [i.code for i in issues] == ["C101"]   # 二重報告しない


def test_number_only_in_preset_parts_and_alias_column():
    c = cfg(field=[{"src": "v", "preset": "tolerance",
                    "parts": {"nom": {"out": "n", "number_only": True, "strip_units": ["*"]}, "tol": {"out": "t", "number_only": True, "strip_units": ["*"]}}}])
    r, _ = run(c, "100±5V")
    assert (r["n"], r["t"]) == ("100", "5")


def test_number_only_rejects_unit_options():
    for extra in ({"to": "mm"}, {"default_unit": "mm"}, {"unit_out": "add"}):
        with pytest.raises(ConfigError) as e:
            cfg(field=[{"src": "v", "number_only": True, "strip_units": ["*"], **extra}])
        assert "number_only" in str(e.value)


def test_example_abc_synonyms_and_first_of_list():
    c = load_config(Path(__file__).parents[1] / "examples" / "abc.toml")
    cols, rows, issues = convert_rows(c, ["k", "grade", "list"], [
        ["1", "A", "A,B,C"], ["2", "b", "B、C"], ["3", " Ｃ ", "E29140,E26880"], ["4", "x", "Z"]])
    got = [(r[1], r[3]) for r in rows]
    assert got == [("A", "A"), ("A", "B"), ("A", "E29140"), ("x", "Z")]
    assert [i.code for i in issues] == ["C121"] and issues[0].row == 4

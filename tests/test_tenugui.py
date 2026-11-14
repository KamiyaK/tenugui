from decimal import Decimal

import pytest

from tenugui import ConfigError, config_from_dict, convert_rows
from tenugui.expr import ExprError, compile_expr
from tenugui.units import UnitError, UnitTable, parse_quantity

T = UnitTable()


def cfg(**kw):
    base = {"schema_version": 1}
    base.update(kw)
    return config_from_dict(base)


def run(c, header, rows):
    return convert_rows(c, header, rows)


# ---- units -------------------------------------------------------------
@pytest.mark.parametrize("text,num,unit", [
    ("35mm", "35", "mm"), ("３５ｍｍ", "35", "mm"), ("35㎜", "35", "mm"),
    ("1,200 V", "1200", "V"), ("1.5 cm", "1.5", "cm"), ("−3.5mm", "-3.5", "mm"),
    ("100", "100", None), ("5kΩ", "5", "kΩ"), ("5Ω", "5", "Ω"), ("5ohm", "5", "Ω"),
    ("20µm", "20", "μm"),
])
def test_parse_quantity(text, num, unit):
    assert parse_quantity(text, T) == (Decimal(num), unit)


@pytest.mark.parametrize("bad", ["abc", "φ10±0.2", "10 20", "", "約85mm"])
def test_parse_quantity_rejects(bad):
    with pytest.raises(UnitError):
        parse_quantity(bad, T)


def test_convert_exact_and_dimension_mismatch():
    assert T.convert(Decimal("1.5"), "cm", "mm") == Decimal("15.0")
    assert T.convert(Decimal("85"), "mm", "m") == Decimal("0.085")
    with pytest.raises(UnitError):
        T.convert(Decimal(1), "V", "mm")
    with pytest.raises(UnitError):
        T.convert(Decimal(1), "furlong", "mm")


def test_custom_units_and_alias():
    t = UnitTable({"length": {"inch": "25.4"}}, {"ミリ": "mm"})
    assert t.convert(Decimal(1), "inch", "mm") == Decimal("25.4")
    assert parse_quantity("5ミリ", t) == (Decimal(5), "mm")


# ---- expr --------------------------------------------------------------
def test_expr_basic():
    e = compile_expr("pi * (d / 2) ** 2")
    assert e.names == {"d"}
    assert round(e.eval({"d": Decimal(10)}), 4) == Decimal("78.5398")


@pytest.mark.parametrize("src", ["__import__('os')", "a.b", "a[0]", "a if b else c",
                                 "x == 1", "'s'", "f(1)", "(lambda: 1)()", "a ** 0.5"])
def test_expr_rejects(src):
    with pytest.raises(ExprError):
        e = compile_expr(src)
        e.eval({"a": Decimal(1), "b": Decimal(1), "c": Decimal(1), "x": Decimal(1)})


def test_expr_zero_division():
    with pytest.raises(ExprError):
        compile_expr("1/a").eval({"a": Decimal(0)})


# ---- fields ------------------------------------------------------------
H = ["sheet", "shape", "len", "volt"]


def test_synonyms_and_units():
    c = cfg(synonyms={"shape": {"標準型": ["標準", "STD"]}},
            field=[{"src": "shape", "synonyms": "shape"},
                   {"src": "len", "to": "mm", "default_unit": "mm"},
                   {"src": "volt", "to": "V"}])
    cols, rows, issues = run(c, H, [["a", " ｓｔｄ ", "3.5cm", "1.2kV"],
                                    ["b", "標準", "８５", "5V"]])
    assert cols == H
    assert rows[0] == ["a", "標準型", "35", "1200"]
    assert rows[1] == ["b", "標準型", "85", "5"]
    assert issues == []


def test_unit_add_and_label():
    c = cfg(field=[{"src": "len", "to": "mm", "unit_out": "add", "decimals": 1}])
    _, rows, _ = run(c, H, [["a", "x", "1.5cm", ""]])
    assert rows[0][2] == "15mm"
    c = cfg(field=[{"src": "len", "to": "mm", "unit_out": "add", "unit_label": " mm"}])
    _, rows, _ = run(c, H, [["a", "x", "35", ""]]) if False else run(
        cfg(field=[{"src": "len", "to": "mm", "default_unit": "mm", "unit_out": "add",
                    "unit_label": " mm"}]), H, [["a", "x", "35", ""]])
    assert rows[0][2] == "35 mm"


def test_errors_reported_not_swallowed():
    c = cfg(field=[{"src": "len", "to": "mm"}, {"src": "volt", "to": "V", "default_unit": "V"}])
    _, rows, issues = run(c, H, [["a", "x", "35", "12mm"], ["b", "x", "abc", ""]])
    codes = [(i.row, i.column, i.code) for i in issues]
    assert (1, "len", "C102") in codes          # 単位なし・default_unitなし
    assert (1, "volt", "C103") in codes         # 次元違い
    assert (2, "len", "C101") in codes          # 読めない
    assert rows[0][2] == "35" and rows[0][3] == "12mm"   # on_error=keep で元値を残す


def test_on_error_empty():
    c = cfg(settings={"on_error": "empty"}, field=[{"src": "len", "to": "mm"}])
    _, rows, issues = run(c, H, [["a", "x", "abc", ""]])
    assert rows[0][2] == "" and issues[0].code == "C101"


def test_reserved_ng_marks_pass_through():
    c = cfg(field=[{"src": "len", "to": "mm", "required": True}])
    _, rows, issues = run(c, H, [["a", "x", "NG1", ""], ["b", "x", "NG12", ""]])
    assert rows[0][2] == "NG1" and rows[1][2] == "NG12" and issues == []


def test_required_and_empty_passthrough():
    c = cfg(field=[{"src": "len", "to": "mm", "required": True},
                   {"src": "volt", "to": "V"}])
    _, rows, issues = run(c, H, [["a", "x", "", ""]])
    assert [i.code for i in issues] == ["C201"] and rows[0][3] == ""


def test_extract_and_sub_with_alias_out():
    c = cfg(field=[{"src": "len", "out": "nom", "sub": [["^[φΦ]", ""]],
                    "extract": r"(\d+(?:\.\d+)?)", "to": "mm", "default_unit": "mm"}])
    cols, rows, issues = run(c, H, [["a", "x", "φ10±0.2", ""]])
    assert cols == ["sheet", "shape", "len", "nom", "volt"]   # 元列は残り直後に追加
    assert rows[0][2:4] == ["φ10±0.2", "10"]
    c2 = cfg(field=[{"src": "len", "out": "nom", "extract": r"(\d+)", "drop_src": True,
                     "to": "mm", "default_unit": "mm"}])
    assert run(c2, H, [["a", "x", "7", ""]])[0] == ["sheet", "shape", "nom", "volt"]


def test_passthrough_false():
    c = cfg(settings={"passthrough": False, "keep": ["sheet"]},
            field=[{"src": "len", "to": "mm", "default_unit": "mm"}])
    cols, rows, _ = run(c, H, [["a", "x", "5cm", "v"]])
    assert cols == ["sheet", "len"] and rows[0] == ["a", "50"]


def test_duplicate_headers_are_uniquified():
    c = cfg()
    cols, rows, _ = run(c, ["k", "info", "info"], [["a", "1", "2"]])
    assert cols == ["k", "info", "info#2"] and rows[0] == ["a", "1", "2"]


# ---- derive ------------------------------------------------------------
def test_derive_with_unit_conversion_and_placement():
    c = cfg(field=[{"src": "len", "to": "mm", "default_unit": "mm"}],
            derive=[{"out": "len_m", "expr": "l", "vars": {"l": "len@m"}, "unit": "m",
                     "unit_out": "add", "decimals": 3},
                    {"out": "area", "expr": "l * w", "vars": {"l": "len", "w": "volt@V"},
                     "after": "sheet"}])
    cols, rows, issues = run(c, H, [["a", "x", "8.5cm", "1.2kV"]])
    assert cols == ["sheet", "area", "shape", "len", "volt", "len_m"]
    r = dict(zip(cols, rows[0]))
    assert r["len_m"] == "0.085m" and r["area"] == "102000"
    assert issues == []


def test_derive_raw_column_unit_rules():
    c = cfg(derive=[{"out": "o", "expr": "a", "vars": {"a": "len"}}])
    _, rows, issues = run(c, H, [["a", "x", "5mm", ""]])
    assert issues[0].code == "C311" and rows[0][-1] == ""   # 単位付きは@指定を要求
    c = cfg(derive=[{"out": "o", "expr": "a", "vars": {"a": "len@mm"}}])
    _, rows, issues = run(c, H, [["a", "x", "5", ""]])
    assert issues[0].code == "C304" and issues[0].severity == "W" and rows[0][-1] == "5"


def test_derive_no_double_report_and_empty_propagation():
    c = cfg(field=[{"src": "len", "to": "mm", "default_unit": "mm"}],
            derive=[{"out": "o", "expr": "a * 2", "vars": {"a": "len"}}])
    _, rows, issues = run(c, H, [["a", "x", "abc", ""], ["b", "x", "", ""], ["c", "x", "NG2", ""]])
    # 変換失敗(C101)は派生側で重ねて報告しない。NG印入力はW(C302)で「なぜ空か」だけ残す
    assert [(i.code, i.severity) for i in issues] == [("C101", "E"), ("C302", "W")]
    assert [r[-1] for r in rows] == ["", "", ""]


def test_derive_div_zero():
    c = cfg(derive=[{"out": "o", "expr": "1 / a", "vars": {"a": "len"}}])
    _, rows, issues = run(c, H, [["a", "x", "0", ""]])
    assert issues[0].code == "C303"


# ---- config validation -------------------------------------------------
def problems(**kw):
    with pytest.raises(ConfigError) as e:
        cfg(**kw)
    return "\n".join(e.value.problems)


def test_config_catches_typos_and_inconsistencies():
    assert "未知のキー" in problems(field=[{"src": "a", "unit": "mm"}])
    assert "単位表にありません" in problems(field=[{"src": "a", "to": "furlong"}])
    assert "[synonyms.*]" in problems(field=[{"src": "a", "synonyms": "nope"}])
    assert "vars にありません" in problems(derive=[{"out": "o", "expr": "a+b", "vars": {"a": "x"}}])
    assert "式で使われていません" in problems(derive=[{"out": "o", "expr": "a", "vars": {"a": "x", "b": "y"}}])
    assert "重複" in problems(field=[{"src": "a", "out": "z"}, {"src": "b", "out": "z"}])
    assert "依存順" in problems(derive=[
        {"out": "p", "expr": "q", "vars": {"q": "q_out"}},
        {"out": "q_out", "expr": "1", "vars": {}}])
    assert "次元が違います" in problems(field=[{"src": "a", "to": "mm", "default_unit": "V"}])
    assert "両方に入って" in problems(synonyms={"s": {"A": ["x"], "B": ["X"]}})
    with pytest.raises(ConfigError):
        config_from_dict({"schema_version": 2})


# ---- 複合単位・式定義の単位 -----------------------------------------------
@pytest.mark.parametrize("src,dst,val,expect", [
    ("mm^2", "m^2", "1000000", "1"), ("mm2", "cm2", "100", "1"), ("mm*mm", "m2", "1000000", "1"),
    ("mm²", "cm^2", "250", "2.5"), ("m/s", "mm/s", "2", "2000"), ("mm/min", "m/s", "60000", "1"),
    ("cm3", "mm^3", "1", "1000"), ("kg*m/s^2", "g*mm/s2", "1", "1000000"),
    ("°C", "K", "0", "273.15"), ("K", "°C", "300", "26.85"), ("°F", "°C", "212", "100"),
    ("°C", "°F", "100", "212"), ("%", "ppm", "1", "10000"),
])
def test_compound_and_affine(src, dst, val, expect):
    assert T.convert(Decimal(val), src, dst).quantize(Decimal("0.0001")) == Decimal(expect).quantize(Decimal("0.0001"))


def test_compound_rejections():
    for a, b in [("mm^2", "mm"), ("mm2", "V"), ("W/°C", "W/K")]:
        with pytest.raises(UnitError):
            T.convert(Decimal(1), a, b)
    with pytest.raises(UnitError):
        T.resolve("mm/")


def test_parse_compound_and_temperature_text():
    assert parse_quantity("100 mm^2", T) == (Decimal(100), "mm^2")
    assert parse_quantity("1350℃", T) == (Decimal(1350), "°C")
    assert parse_quantity("1.25mm2", T) == (Decimal("1.25"), "mm2")


def test_user_defined_affine_unit():
    t = UnitTable({"temperature": {"°R": {"to_base": "x * 5 / 9", "from_base": "x * 9 / 5"}}})
    assert t.convert(Decimal(491.67).quantize(Decimal("0.01")), "°R", "K").quantize(Decimal("0.01")) == Decimal("273.15")
    with pytest.raises(UnitError):
        UnitTable({"temperature": {"bad": {"to_base": "x + y", "from_base": "x"}}})


def test_field_and_derive_with_compound_and_temperature():
    c = cfg(field=[{"src": "len", "to": "°C", "default_unit": "K"},
                   {"src": "volt", "to": "m2", "default_unit": "cm^2", "decimals": 4}],
            derive=[{"out": "a", "expr": "x * y", "vars": {"x": "sheet@mm", "y": "sheet@mm"},
                     "unit": "mm^2", "to": "cm2", "unit_out": "add"}])
    _, rows, issues = run(c, H, [["30", "x", "300", "50"]])
    assert rows[0][2] == "26.85" and rows[0][3] == "0.005" and rows[0][4] == "9cm2"
    assert [i.code for i in issues] == ["C304", "C304"] or issues[0].severity == "W"


def test_config_rejects_bad_derive_units():
    assert "次元が違います" in problems(derive=[{"out": "o", "expr": "a", "vars": {"a": "x@mm"},
                                                  "unit": "mm2", "to": "mm"}])
    assert "unit が必要" in problems(derive=[{"out": "o", "expr": "a", "vars": {"a": "x@mm"}, "to": "mm"}])


def test_failed_alias_column_is_empty_not_raw_copy():
    c = cfg(field=[{"src": "len", "out": "n", "extract": r"(\d+)", "to": "mm"}])
    _, rows, issues = run(c, H, [["a", "x", "abc", ""], ["b", "x", "7W", ""]])
    assert rows[0][3] == "" and rows[1][3] == ""     # C111 / C102 とも空
    assert [i.code for i in issues] == ["C111", "C102"]

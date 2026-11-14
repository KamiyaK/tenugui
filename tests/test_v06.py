import json

import pytest

from tenugui import ConfigError, config_from_dict, convert_rows
from tenugui.cli import main

H = ["k", "v", "other"]


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


def run(c, text, header=H):
    cols, rows, issues = convert_rows(c, header, [["a", text, "x"]])
    return dict(zip(cols, rows[0])), issues


# ---- number_only は指定した単位だけ処理する -------------------------------------------
N = cfg(field=[{"src": "v", "number_only": True, "strip_units": ["mm", "cm"]}])


@pytest.mark.parametrize("text", ["330mm", "12 cm", "３５㎜", "100"])        # 指定した単位、または単位なし
def test_number_only_strips_only_listed_units(text):
    r, issues = run(N, text)
    assert issues == [] and r["v"] == {"330mm": "330", "12 cm": "12", "３５㎜": "35", "100": "100"}[text]


@pytest.mark.parametrize("text,unit", [("5V", "V"), ("20%", "%"), ("4(二重)", "(二重)"), ("10±0.2", "±0.2")])
def test_number_only_rejects_unlisted_units_instead_of_dropping(text, unit):
    r, issues = run(N, text)
    assert [i.code for i in issues] == ["C104"] and unit in issues[0].message
    assert r["v"] == text                      # 元の値を残す（黙って数値にしない）


def test_number_only_does_not_convert():
    r, _ = run(N, "12cm")
    assert r["v"] == "12"                      # 120 にはならない（換算しない）


def test_strip_units_uses_aliases_and_any_text_units():
    c = cfg(unit_alias={"ボルト": "V"}, field=[{"src": "v", "number_only": True, "strip_units": ["V", "個"]}])
    assert run(c, "5ボルト")[0]["v"] == "5" and run(c, "5v")[0]["v"] == "5" and run(c, "3個")[0]["v"] == "3"
    assert run(c, "3本")[1][0].code == "C104"


def test_strip_units_wildcard_is_explicit_opt_in():
    c = cfg(field=[{"src": "v", "number_only": True, "strip_units": ["*"]}])
    assert run(c, "20%")[0]["v"] == "20" and run(c, "4(二重)")[0]["v"] == "4"


def test_number_only_config_errors():
    def bad(**f):
        with pytest.raises(ConfigError) as e:
            cfg(field=[{"src": "v", **f}])
        return str(e.value)
    assert "strip_units" in bad(number_only=True)                                    # 単位の指定が必須
    assert "strip_units" in bad(number_only=True, strip_units=[])
    assert "number_only = true と一緒" in bad(strip_units=["mm"])
    assert "number_only は to" in bad(number_only=True, strip_units=["mm"], to="mm")


def test_number_only_in_preset_parts_with_units():
    c = cfg(field=[{"src": "v", "preset": "tolerance", "parts": {
        "nom": {"out": "n", "number_only": True, "strip_units": ["V"]},
        "tol": {"out": "t", "number_only": True, "strip_units": ["V"]}}}])
    r, issues = run(c, "100±5V")
    assert (r["n"], r["t"]) == ("100", "5") and issues == []


# ---- 元のデータを消す drop_src -------------------------------------------------------------
def tr(**extra):
    return cfg(field=[{"src": "v", "preset": "tolerance_range", **extra, "parts": {
        "nom": {"out": "w", "to": "W"}, "rlo": {"out": "lo", "to": "A"}}}])


def test_drop_src_removes_original_column_for_preset_and_plain_fields():
    cols, rows, _ = convert_rows(tr(drop_src=True), H, [["a", "1250W(11.5～13.5A)", "x"]])
    assert cols == ["k", "w", "lo", "other"] and rows[0] == ["a", "1250", "11.5", "x"]
    c = cfg(field=[{"src": "v", "out": "n", "number_only": True, "strip_units": ["*"], "drop_src": True}])
    assert convert_rows(c, H, [["a", "5V", "x"]])[0] == ["k", "n", "other"]


def test_drop_src_with_out_equal_src_is_a_config_error_not_silent_loss():
    with pytest.raises(ConfigError) as e:
        cfg(field=[{"src": "v", "preset": "tolerance", "drop_src": True,
                    "parts": {"nom": {"out": "v", "to": "V", "default_unit": "V"}}}])
    assert "drop_src" in str(e.value) and "out" in str(e.value)


def test_derive_can_still_read_a_dropped_source_column():
    c = cfg(field=[{"src": "v", "out": "n", "number_only": True, "strip_units": ["*"], "drop_src": True}],
            derive=[{"out": "d", "expr": "x * 2", "vars": {"x": "n"}}])
    cols, rows, _ = convert_rows(c, H, [["a", "5V", "x"]])
    assert dict(zip(cols, rows[0]))["d"] == "10" and "v" not in cols


@pytest.fixture
def work(tmp_path):
    (tmp_path / "in.csv").write_text("k,v,other\na,1250W(11.5~13.5A),x\nb,不明,y\n", encoding="utf-8")
    (tmp_path / "r.toml").write_text(
        'schema_version = 1\n[[field]]\nsrc = "v"\npreset = "tolerance_range"\ndrop_src = true\n'
        'parts = { nom = { out = "w", to = "W" }, rlo = { out = "lo", to = "A" } }\n', encoding="utf-8")
    return tmp_path


def test_dropped_columns_are_reported_with_failed_rows_and_not_a_violation(work, capsys):
    code = main(["convert", str(work / "in.csv"), "-c", str(work / "r.toml"), "--format", "json"])
    d = json.loads(capsys.readouterr().out)
    v = d["verification"]
    assert v["ok"] and v["summary"]["violations"] == 0                       # 意図した削除は違反にしない
    log = (work / "tenugui_out" / "in_tenugui.log").read_text(encoding="utf-8")
    assert "削除した元の列: v（変換に失敗した行 1 件" in log                      # 失敗行は raw に残る
    rep = (work / "tenugui_out" / "in_tenugui_conv.csv").read_text(encoding="utf-8-sig")
    assert "不明" in rep
    code = main(["verify", str(work / "in.csv"), str(work / "tenugui_out" / "in_tenugui.csv"),
                 "-c", str(work / "r.toml"), "--format", "json"])
    assert json.loads(capsys.readouterr().out)["dropped_columns"] == [{"column": "v", "failed_rows": 1}]


def test_number_only_parts_in_preset_check_units_too():
    """preset の部品でも number_only は単位を照合する（以前は単位が無視され、7±2cm が通ってしまった）"""
    c = cfg(field=[{"src": "v", "preset": "tolerance", "parts": {
        "nom": {"out": "n", "number_only": True, "strip_units": ["mm"]},
        "tol": {"out": "t", "number_only": True, "strip_units": ["mm"]}}}])
    for text, ok in [("7±2mm", True), ("7mm±2mm", True), ("7±2", True), ("７±２㎜", True),
                     ("7±2cm", False), ("7cm±2mm", False)]:
        r, issues = run(c, text)
        assert (issues == []) is ok, text
        if ok:
            assert (r["n"], r["t"]) == ("7", "2")
        else:
            assert {i.code for i in issues} == {"C104"}

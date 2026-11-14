"""0.9.0: 列のまとめ指定(for / src配列) / each / parts短縮形 / null_values / schema / フォルダ一括 / エラーの候補表示"""
import json

import pytest

from tenugui import core
from tenugui.analysis import verify_before_after
from tenugui.cli import main
from tenugui.core import ConfigError, check_header, config_from_dict, convert_rows


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


def bad(**kw):
    with pytest.raises(ConfigError) as e:
        cfg(**kw)
    return str(e.value)


MM = {"number_only": True, "strip_units": ["mm"]}


# ---- 列のまとめ指定 --------------------------------------------------------
def test_src_list_applies_same_rule():
    c = cfg(field=[{"src": ["a", "b"], **MM}])
    cols, rows, issues = convert_rows(c, ["k", "a", "b"], [["x", "7mm", "14mm"]])
    assert rows[0] == ["x", "7", "14"] and not issues and [f.src for f in c.fields] == ["a", "b"]


def test_for_expands_product_and_fills_out():
    c = cfg(field=[{"for": {"n": [0, 1], "side": ["l", "r"]}, "src": "w{n}_strip{side}", **MM}])
    assert [f.src for f in c.fields] == ["w0_stripl", "w0_stripr", "w1_stripl", "w1_stripr"]
    c = cfg(field=[{"for": {"n": ["0", "1"]}, "src": "w{n}_len", "preset": "tolerance", "drop_src": True,
                    "each": MM, "parts": {"nom": "len{n}_nom", "tol": {"out": "{src}_tol", "decimals": 1}}}])
    assert [(f.src, f.out, f.number_only, f.decimals, f.drop_src) for f in c.fields] == [
        ("w0_len", "len0_nom", True, None, True), ("w0_len", "w0_len_tol", True, 1, True),
        ("w1_len", "len1_nom", True, None, True), ("w1_len", "w1_len_tol", True, 1, True)]
    cols, rows, _ = convert_rows(c, ["k", "w0_len", "w1_len"], [["x", "330±15mm", "1040±15.26mm"]])
    assert dict(zip(cols, rows[0])) == {"k": "x", "len0_nom": "330", "w0_len_tol": "15",
                                        "len1_nom": "1040", "w1_len_tol": "15.3"}


def test_each_and_shorthand_in_case_with_override():
    c = cfg(field=[{"src": "len", "each": MM, "case": [
        {"pattern": "#±#mm", "parts": {"n1": "nom", "n2": "tol"}},
        {"pattern": "#cm", "each": {"to": "mm"}, "parts": {"n1": "nom"}}]}])
    cols, rows, issues = convert_rows(c, ["k", "len"], [["a", "330±15mm"], ["b", "33cm"]])
    assert rows[0][2:] == ["330", "15"] and rows[1][2:] == ["330", ""] and not issues


def test_grouping_errors():
    assert "入れる値がありません" in bad(field=[{"src": "a", "preset": "tolerance", "parts": {"nom": "x{n}"}}])
    assert "重複" in bad(field=[{"for": {"n": [0, 1]}, "src": "a", **MM}])
    assert "名前 = 値の配列" in bad(field=[{"for": {"n": "01"}, "src": "a{n}"}])
    assert "src は使えません" in bad(field=[{"for": {"src": ["a"]}, "src": "{src}"}])
    assert "each は parts" in bad(field=[{"src": "a", "each": MM}])
    assert "出力列名 'o' が重複" in bad(field=[{"src": ["a", "b"], "preset": "tolerance", "parts": {"nom": "o"}}])
    assert "src は列名" in bad(field=[{"src": 3}])


# ---- null_values ---------------------------------------------------------
def test_null_values_become_empty_only_in_ruled_columns():
    c = cfg(settings={"null_values": ["-", "なし", "N/A"]},
            field=[{"src": "a", **MM}, {"src": "b", "preset": "tolerance", "parts": {"nom": "b_nom"}}],
            derive=[{"out": "d", "expr": "x * 2", "vars": {"x": "c"}}])
    H = ["k", "a", "b", "c", "free"]
    rows = [["1", "－", "なし", "n/a", "-"], ["2", "7mm", "5±1", "3", "-"]]
    cols, out, issues = convert_rows(c, H, rows)
    d = [dict(zip(cols, r)) for r in out]
    assert d[0]["a"] == "" and d[0]["b_nom"] == "" and d[0]["d"] == "" and not issues
    assert d[0]["free"] == "-" and d[0]["b"] == "なし" and d[0]["c"] == "n/a"      # ルールのない列・元の列はそのまま
    assert d[1]["a"] == "7" and d[1]["d"] == "6"
    v = verify_before_after(c, H, rows, cols, out, issues)
    assert v["ok"] and v["summary"]["lost_cells"] == 0                              # 意図した空は「欠落」にしない
    assert "null_values は" in bad(settings={"null_values": "-"})


def test_null_values_with_required_and_case():
    c = cfg(settings={"null_values": ["-"]}, field=[
        {"src": "a", "required": True, **MM},
        {"src": "b", "case": [{"pattern": "#mm", "parts": {"n1": "o"}}]}])
    _, out, issues = convert_rows(c, ["k", "a", "b"], [["1", "-", "-"]])
    assert [(i.code, i.column) for i in issues] == [("C201", "a")] and out[0][3] == ""


# ---- エラーメッセージ ------------------------------------------------------
def test_messages_suggest_spelling():
    assert "もしかして: strip_units" in bad(field=[{"src": "a", "number_only": True, "strip_unit": ["mm"]}])
    assert "もしかして: tolerance" in bad(field=[{"src": "a", "preset": "tolerence", "parts": {"nom": "o"}}])
    m = bad(field=[{"src": "a", "to": "mmm"}])
    assert "もしかして" in m and "mm" in m and "[units.<次元>]" in m
    assert "もしかして: grade" in bad(synonyms={"grade": {"A": []}}, field=[{"src": "a", "synonyms": "grad"}])
    probs = check_header(cfg(field=[{"src": "wire_lenght", **MM}]), ["sheet", "wire_length"])
    assert probs == ["C001 入力に列 'wire_lenght' がありません（もしかして: wire_length）"]


def test_toml_syntax_error_has_causes(tmp_path, capsys):
    p = tmp_path / "r.toml"
    p.write_text('schema_version = 1\n[[field]]\nsrc = "a"\nextract = "(\\d+)"\n', encoding="utf-8")
    assert main(["check", "-c", str(p)]) == 2
    err = capsys.readouterr().err
    assert "'...' で書く" in err and "tenugui schema" in err


# ---- schema --------------------------------------------------------------
def test_schema_matches_validator_keys():
    from tenugui import schema as S
    assert set(S.SETTINGS) == core._SETTINGS_KEYS
    assert set(S.VALUE_KEYS) | {"src", "extract", "drop_src"} == core._FIELD_KEYS
    assert set(S.VALUE_KEYS) | set(S.PART_ONLY) == core._PART_KEYS
    assert set(S.DERIVE) == core._DERIVE_KEYS and set(S.TEST) == core._TEST_KEYS
    assert set(S.schema()["presets"]) == set(core.PRESETS)
    from tenugui.analysis import HINTS
    assert set(HINTS) <= set(S.CODES)


def test_schema_defaults_are_the_real_defaults():
    from tenugui import schema as S
    c = cfg()
    assert S.SETTINGS["rare_shape"]["default"] == c.rare_shape and S.SETTINGS["on_error"]["default"] == c.on_error
    assert S.SETTINGS["passthrough"]["default"] == c.passthrough
    assert S.SETTINGS["reserved"]["default"] == [r.pattern for r in c.reserved]


def test_cli_schema(capsys):
    assert main(["schema", "--format", "json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["schema"] == "tenugui.schema/1" and j["presets"]["range"]["parts"] == ["lo", "hi"]
    assert "for" in j["toml"]["field"]["forms"]["split"]["keys"] and j["codes"]["C131"]["hint"]
    assert main(["schema"]) == 0
    assert "[[field]] case" in capsys.readouterr().out


# ---- フォルダ一括 ----------------------------------------------------------
@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "a.csv").write_text("sheet,len\ns1,7mm\ns2,8mm\n", encoding="utf-8-sig")
    (d / "b.csv").write_text("sheet,len\ns1,7cm\n", encoding="utf-8-sig")
    (d / "c.csv").write_text("sheet,other\ns1,1\n", encoding="utf-8-sig")
    (d / "note.txt").write_text("x", encoding="utf-8")
    (tmp_path / "r.toml").write_text('schema_version = 1\n[[field]]\nsrc = "len"\nnumber_only = true\n'
                                     'strip_units = ["mm"]\n', encoding="utf-8")
    return tmp_path


def test_convert_folder_batch(folder, capsys):
    code = main(["convert", str(folder / "data"), "-c", str(folder / "r.toml"), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    assert code == 2 and j["schema"] == "tenugui.batch/1" and not j["ok"]           # c.csv は列が無い
    assert j["totals"] == {"files": 3, "failed": 1, "rows": 3, "error": 1, "warning": 0}
    assert [r["ok"] for r in j["results"]] == [True, True, False]
    assert "input_problems" in j["results"][2]
    out = folder / "data" / "tenugui_out"
    assert sorted(p.name for p in out.iterdir()) == [
        "a_tenugui.csv", "a_tenugui.html", "a_tenugui.json", "b_tenugui.csv", "b_tenugui.html", "b_tenugui.json"]
    code = main(["convert", str(folder / "data"), "-c", str(folder / "r.toml")])     # 2回目: tenugui_out の中は拾わない
    text = capsys.readouterr().out
    assert "合計: 3 ファイル（変換できず 1）/ 3 行 / エラー 1 件" in text


def test_convert_multiple_files_and_output_option(folder, capsys):
    a, b = str(folder / "data" / "a.csv"), str(folder / "data" / "b.csv")
    assert main(["convert", a, b, "-c", str(folder / "r.toml"), "--fail-on", "error"]) == 1
    capsys.readouterr()
    assert main(["convert", a, b, "-c", str(folder / "r.toml"), "-o", "x.csv"]) == 2
    assert "-o は入力が1つ" in capsys.readouterr().err
    assert main(["convert", str(folder / "nothing.csv"), "-c", str(folder / "r.toml")]) == 2
    assert "ファイルを開けません" in capsys.readouterr().err
    assert main(["convert", str(folder / "nothing.csv"), "-c", str(folder / "r.toml"), "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().out)["schema"] == "tenugui.error/1"

import json
from pathlib import Path

import pytest

from tenugui import ConfigError, config_from_dict, convert_rows
from tenugui.analysis import verify_before_after, verify_files
from tenugui.cli import main


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


H = ["sheet", "v"]


def tol(**parts):
    return cfg(field=[{"src": "v", "preset": "tolerance", "parts": parts}])


# ---- preset: tolerance / range ------------------------------------------------
@pytest.mark.parametrize("text,expect", [
    ("100±5V", {"n": "100", "t": "5"}),
    ("100V±5V", {"n": "100", "t": "5"}),
    ("100V±5", {"n": "100", "t": "5"}),
    ("1.2kV±50V", {"n": "1200", "t": "50"}),          # 単位が違っても各部で換算
    ("φ100±5V", {"n": "100", "t": "5"}),
    ("100V", {"n": "100", "t": ""}),                    # 許容差なしでも nom は取れる
    ("100+5/-3V", {"n": "100", "hi": "5", "lo": "3", "t": ""}),
])
def test_tolerance_preset(text, expect):
    c = tol(nom={"out": "n", "to": "V"}, tol={"out": "t", "to": "V"},
            hi={"out": "hi", "to": "V"}, lo={"out": "lo", "to": "V"})
    cols, rows, issues = convert_rows(c, H, [["a", text]])
    r = dict(zip(cols, rows[0]))
    for k, v in expect.items():
        assert r[k] == v, (text, k, r)
    assert issues == []


def test_tolerance_without_to_drops_unit_text_only():
    c = tol(nom={"out": "n"}, tol={"out": "t"})
    cols, rows, _ = convert_rows(c, H, [["a", "100±5V"]])
    assert dict(zip(cols, rows[0]))["n"] == "100" and dict(zip(cols, rows[0]))["t"] == "5"


def test_tolerance_no_unit_needs_default_unit_and_unmatched_reports_once():
    c = tol(nom={"out": "n", "to": "V"}, tol={"out": "t", "to": "V"})
    _, _, issues = convert_rows(c, H, [["a", "100±5"]])
    assert {i.code for i in issues} == {"C102"}
    c2 = tol(nom={"out": "n", "to": "V", "default_unit": "V"}, tol={"out": "t", "to": "V", "default_unit": "V"})
    cols, rows, issues = convert_rows(c2, H, [["a", "100±5"], ["b", "abc"]])
    assert dict(zip(cols, rows[0]))["t"] == "5"
    assert [i.code for i in issues] == ["C111"]


def test_range_preset_shares_unit_either_side():
    c = cfg(field=[{"src": "v", "preset": "range",
                    "parts": {"lo": {"out": "lo", "to": "A"}, "hi": {"out": "hi", "to": "A"}}}])
    for text in ["11.5～13.5A", "11.5A~13.5A", "500mA~1.2A"]:
        cols, rows, issues = convert_rows(c, H, [["a", text]])
        assert issues == [], text
    cols, rows, _ = convert_rows(c, H, [["a", "500mA～1.2A"]])
    assert dict(zip(cols, rows[0]))["lo"] == "0.5" and dict(zip(cols, rows[0]))["hi"] == "1.2"


def test_preset_config_errors():
    def bad(**f):
        with pytest.raises(ConfigError) as e:
            cfg(field=[f])
        return str(e.value)
    assert "未対応" in bad(src="v", preset="nope", parts={"x": {"out": "o"}})
    assert "部品名" in bad(src="v", preset="tolerance", parts={"zzz": {"out": "o"}})
    assert "parts が必要" in bad(src="v", preset="tolerance")
    assert "out が必要" in bad(src="v", preset="tolerance", parts={"nom": {}})


# ---- 出力フォルダ・ログ ------------------------------------------------------------
@pytest.fixture
def work(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("sheet,v\na,100±5V\nb,NG1\nc,abc\n", encoding="utf-8")
    (tmp_path / "r.toml").write_text(
        'schema_version = 1\n[[field]]\nsrc = "v"\npreset = "tolerance"\n'
        'parts = { nom = { out = "n", to = "V" }, tol = { out = "t", to = "V" } }\n', encoding="utf-8")
    return tmp_path


def run(capsys, *a):
    code = main([str(x) for x in a])
    return code, capsys.readouterr().out


def test_default_output_goes_to_tenugui_out_next_to_input_and_is_created(work, capsys):
    out_dir = work / "data" / "tenugui_out"
    assert not out_dir.exists()
    code, out = run(capsys, "convert", work / "data" / "in.csv", "-c", work / "r.toml")
    assert code == 0 and out_dir.is_dir()
    assert sorted(p.name for p in out_dir.iterdir()) == ["in_tenugui.csv", "in_tenugui.html", "in_tenugui.json"]
    code, out = run(capsys, "convert", work / "data" / "in.csv", "-c", work / "r.toml", "--detail-files")
    names = sorted(p.name for p in out_dir.iterdir())
    assert names == ["in_tenugui.csv", "in_tenugui.html", "in_tenugui.json", "in_tenugui.log",
                     "in_tenugui_conv.csv", "in_tenugui_diff.csv"]
    log = (out_dir / "in_tenugui.log").read_text(encoding="utf-8")
    assert "C111" in log and "[変換前後チェック] OK" in log and "sha256-12" in log
    assert (out_dir / "in_tenugui.csv").read_text(encoding="utf-8-sig").splitlines()[1].endswith(",100,5")


def test_out_dir_option_timestamp_and_no_diff(work, capsys):
    code, out = run(capsys, "convert", work / "data" / "in.csv", "-c", work / "r.toml", "--out-dir",
                    work / "deep" / "x", "--timestamp", "--no-diff", "--detail-files", "--format", "json")
    d = json.loads(out)
    files = d["files"]
    assert "diff" not in files and Path(files["output"]).parent == work / "deep" / "x"
    assert Path(files["log"]).exists() and "_20" in Path(files["output"]).name


def test_draft_default_goes_to_tenugui_out(work, capsys):
    run(capsys, "draft", work / "data" / "in.csv")
    assert (work / "data" / "tenugui_out" / "draft.toml").exists()


# ---- 変換前後チェック -----------------------------------------------------------------
def test_verify_ok_on_normal_conversion(work, capsys):
    run(capsys, "convert", work / "data" / "in.csv", "-c", work / "r.toml")
    code, out = run(capsys, "verify", work / "data" / "in.csv", work / "data" / "tenugui_out" / "in_tenugui.csv",
                    "-c", work / "r.toml", "--format", "json")
    d = json.loads(out)
    assert code == 0 and d["ok"] and d["summary"]["violations"] == 0
    assert {n["column"] for n in d["new_columns"]} == {"n", "t"}


def test_verify_detects_edited_output_and_untouched_changes(work, capsys):
    run(capsys, "convert", work / "data" / "in.csv", "-c", work / "r.toml")
    p = work / "data" / "tenugui_out" / "in_tenugui.csv"
    p.write_text(p.read_text(encoding="utf-8-sig").replace("abc", "ABC", 1), encoding="utf-8-sig")
    code, out = run(capsys, "verify", work / "data" / "in.csv", p, "-c", work / "r.toml", "--format", "json")
    d = json.loads(out)
    kinds = {v["kind"] for v in d["violations"]}
    assert code == 1 and not d["ok"] and {"untouched_changed", "output_differs"} <= kinds   # src列が変わった/設定と食い違う


def test_verify_before_after_flags_ng_alteration_row_count_and_lost():
    c = config_from_dict({"schema_version": 1, "field": [{"src": "v", "to": "V", "default_unit": "V"}]})
    rows = [["a", "5V"], ["b", "NG1"], ["c", "7V"]]
    out_h, out_r = ["sheet", "v"], [["a", "5"], ["b", ""], ["c", ""]]
    v = verify_before_after(c, ["sheet", "v"], rows, out_h, out_r, [])
    assert [x["kind"] for x in v["violations"]] == ["ng_altered"] and v["summary"]["lost_cells"] == 2
    v2 = verify_before_after(c, ["sheet", "v"], rows, out_h, out_r[:2], [])
    assert v2["violations"][0]["kind"] == "row_count"
    v3 = verify_before_after(c, ["sheet", "v"], rows, ["sheet"], [["a"], ["b"], ["c"]], [])
    assert any(x["kind"] == "column_missing" for x in v3["violations"])


def test_verify_intended_drop_and_passthrough_false_are_not_violations():
    c = config_from_dict({"schema_version": 1, "settings": {"passthrough": False, "keep": ["sheet"]},
                          "field": [{"src": "v", "out": "n", "to": "V", "default_unit": "V"}]})
    rows = [["a", "5V"]]
    h, r, iss = convert_rows(c, ["sheet", "v"], rows)
    assert verify_before_after(c, ["sheet", "v"], rows, h, r, iss)["ok"]


def test_ui_model_outputs_folder_verification_and_row_view(work):
    from tenugui.ui import Model
    m = Model()
    inp = str(work / "data" / "in.csv")
    assert m.default_output(inp).endswith("tenugui_out/in_tenugui.csv") or m.default_output(inp).endswith("tenugui_out\\in_tenugui.csv")
    lines = m.convert(inp, str(work / "r.toml"), "", False)
    assert (work / "data" / "tenugui_out" / "in_tenugui.html").exists()
    assert "前後チェック: OK" in lines[3]
    view = m.row_view(1)
    assert any("＋ n: '100'" in x for x in view)
    assert m.verify_disk(inp, str(work / "r.toml"), m.out_path)["ok"]

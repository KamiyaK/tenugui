"""0.8.0: 出力の整理（CSV + HTML + JSON）/ HTMLレポート / 少数派の形の警告 C131 / --version"""
import json

import pytest

from tenugui import __version__
from tenugui.cli import main
from tenugui.core import ConfigError, config_from_dict, convert_rows
from tenugui.report import embedded_table


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


MM = {"number_only": True, "strip_units": ["mm"]}
H = ["sheet", "len"]


def many(n, v="7mm"):
    return [[f"s{i}", v] for i in range(n)]


# ---- C131 少数派の形 ------------------------------------------------------
def test_rare_shape_warns_but_still_converts():
    rows = many(30) + [["x", "7"], ["y", "1,200mm"]]
    cols, out, issues = convert_rows(cfg(field=[{"src": "len", **MM}]), H, rows)
    assert [(i.row, i.code, i.severity, i.raw) for i in issues] == [(31, "C131", "W", "7"), (32, "C131", "W", "1,200mm")]
    assert out[30][1] == "7" and out[31][1] == "1200"            # 値は変換する。警告だけ
    assert "主な形は #mm" in issues[0].message


def test_rare_shape_needs_enough_rows_and_can_be_disabled():
    rows = many(10) + [["x", "7"]]
    assert not convert_rows(cfg(field=[{"src": "len", **MM}]), H, rows)[2]         # 行が少ないと判断しない
    rows = many(30) + [["x", "7"]]
    assert not convert_rows(cfg(settings={"rare_shape": 0}, field=[{"src": "len", **MM}]), H, rows)[2]
    with pytest.raises(ConfigError):
        cfg(settings={"rare_shape": 0.9})


def test_rare_shape_skips_errors_ng_and_explicit_formats():
    rows = many(30) + [["x", "7cm"], ["y", "NG"], ["z", ""]]
    issues = convert_rows(cfg(field=[{"src": "len", **MM}]), H, rows)[2]
    assert [(i.row, i.code) for i in issues] == [(31, "C104")]                     # エラーの値に重ねて警告しない
    case = {"src": "len", "case": [{"pattern": "#mm", "parts": {"n1": {"out": "o", **MM}}},
                                   {"pattern": "#", "parts": {"n1": {"out": "o", **MM}}}]}
    assert not convert_rows(cfg(field=[case]), H, many(30) + [["x", "7"]])[2]      # case は形を明示しているので対象外
    text = {"src": "len", "synonyms": "s"}
    assert not convert_rows(cfg(synonyms={"s": {"a": []}}, field=[{**text, "on_unknown": "keep"}]),
                            H, many(30, "abc") + [["x", "zz9"]])[2]                # 数値として読まない列は対象外


def test_rare_shape_applies_to_preset():
    f = {"src": "len", "preset": "tolerance", "parts": {"nom": {"out": "nom", **MM}, "tol": {"out": "tol", **MM}}}
    issues = convert_rows(cfg(field=[f]), H, many(30, "330±15mm") + [["x", "330mm"]])[2]
    assert [(i.row, i.code) for i in issues] == [(31, "C131")]


# ---- 出力ファイルとレポート -------------------------------------------------
@pytest.fixture
def work(tmp_path):
    rows = [["a", "330±15mm"], ["b", "330mm"], ["c", "<b>約</b>330"], ["d", "NG"], ["e", ""]]
    (tmp_path / "in.csv").write_text("sheet,len\n" + "\n".join(",".join(r) for r in rows) + "\n",
                                     encoding="utf-8-sig")
    (tmp_path / "r.toml").write_text(
        'schema_version = 1\n[[field]]\nsrc = "len"\ndrop_src = true\npreset = "tolerance"\n'
        'parts = { nom = { out = "nom", number_only = true, strip_units = ["mm"] }, '
        'tol = { out = "tol", number_only = true, strip_units = ["mm"] } }\n', encoding="utf-8")
    return tmp_path


def test_convert_writes_csv_html_json_only(work, capsys):
    assert main(["convert", str(work / "in.csv"), "-c", str(work / "r.toml"), "--format", "json"]) == 0
    d = json.loads(capsys.readouterr().out)
    out = work / "tenugui_out"
    assert sorted(p.name for p in out.iterdir()) == ["in_tenugui.csv", "in_tenugui.html", "in_tenugui.json"]
    assert sorted(d["files"]) == ["html", "json", "output"]
    rep = json.loads((out / "in_tenugui.json").read_text(encoding="utf-8"))
    assert rep["schema"] == "tenugui.report/1" and rep["version"] == __version__
    assert rep["counts"] == {"error": 1, "warning": 0} and rep["issues"][0]["raw"] == "<b>約</b>330"
    assert rep["issue_groups"][0]["code"] == "C111" and rep["verification"]["ok"]
    assert rep["verification"]["dropped_columns"] == [{"column": "len", "failed_rows": 1}]
    assert rep["files"] == d["files"] and len(rep["config_sha"]) == 12


def test_html_report_marks_cells_and_escapes(work, capsys):
    main(["convert", str(work / "in.csv"), "-c", str(work / "r.toml")])
    text = capsys.readouterr().out
    assert "in_tenugui.html" in text
    h = (work / "tenugui_out" / "in_tenugui.html").read_text(encoding="utf-8")
    assert h.startswith("<!doctype html>") and "<script src" not in h and "http" not in h      # 単体で開ける
    assert "<b>約</b>330" not in h                                # 値はタグにならない形で埋め込む
    t = embedded_table(h)
    cells = [c for r in t["rows"] for c in r[3:]]
    assert any(isinstance(c, list) and c[0] == "<b>約</b>330" for c in cells)   # 元の値（表示は JS がエスケープする）
    errs = [c for c in cells if isinstance(c, list) and c[1] == "err"]
    assert len(errs) == 3                                        # 元の値 + nom + tol の3セル
    assert all(t["tags"][c[3]] == ["C111"] and "C111" in t["titles"][c[2]] for c in errs)
    assert ["NG", "ng"] in cells and ["330", "chg"] in cells
    assert {r[1] for r in t["rows"]} == {0, 1}                   # 問題のある行とない行
    assert "削除した元の列 <b>len</b>（変換に失敗した行 1 件" in h


def test_html_report_limits_rows_to_problem_rows(tmp_path, capsys, monkeypatch):
    import tenugui.report as R
    monkeypatch.setattr(R, "MAX_HTML_ROWS", 5)
    (tmp_path / "in.csv").write_text("sheet,len\n" + "\n".join(f"s{i},7mm" for i in range(8)) + "\nbad,7cm\n",
                                     encoding="utf-8-sig")
    (tmp_path / "r.toml").write_text('schema_version = 1\n[[field]]\nsrc = "len"\nnumber_only = true\n'
                                     'strip_units = ["mm"]\n', encoding="utf-8")
    main(["convert", str(tmp_path / "in.csv"), "-c", str(tmp_path / "r.toml")])
    h = (tmp_path / "tenugui_out" / "in_tenugui.html").read_text(encoding="utf-8")
    assert "問題のある行だけを載せています（1 / 9 行）" in h
    assert [(r[0], r[1]) for r in embedded_table(h)["rows"]] == [(9, 1)]


def test_version_flag(capsys):
    for flag in ("--version", "-V"):
        with pytest.raises(SystemExit) as e:
            main([flag])
        assert e.value.code == 0
        assert capsys.readouterr().out.strip() == f"tenugui {__version__}"

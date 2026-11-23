"""chozu → tenugui の連携テスト。

2段構えにしてある:
  1. 固定の入力（chozu_output.csv。本物の chozu 0.23.0 が make_workbook.py の帳票から出したCSV）
     を tenugui にかけ、結果を1セルずつ確かめる。chozu が無くても走る（CI はこちら）
  2. chozu が import できる環境では、帳票を作り直して実際に chozu で抽出し、
     固定の入力と一致すること（＝ chozu の出力の形が変わっていないこと）と、
     その先の tenugui まで通ることを確かめる

  chozu を入れずに手元で 2 を走らせるには:
      PYTHONPATH=src:../chozu/src python -m pytest tests/chozu_pipeline
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tenugui.cli import main
from tenugui.core import read_csv
from tenugui.report import embedded_table

HERE = Path(__file__).parent
FROZEN = HERE / "chozu_output.csv"
RULES = HERE / "tenugui_pipeline.toml"
SHEETS = ["A-001", "A-002", "A-003", "#004", "S,005", 'S"006', "NG1", "S 008", "試験🙂009", "EMPTY"]

# シート名 → (kind, width, length_nom, length_tol, length_min, length_max,
#             rating_w, rating_w_tol, rating_a_min, rating_a_max, rating_w_plus, rating_w_minus, test_time)
EXPECTED = {
    "A-001": ("標準", "8.5", "330", "15", "", "", "1250", "100", "11.5", "13.5", "", "", "30"),
    "A-002": ("標準", "8.5", "330", "15", "", "", "1250", "100", "11.5", "13.5", "", "", "30"),        # 全角・書き方違い
    "A-003": ("標準", "8.5", "330", "15", "", "", "3400", "", "15.64", "18.19", "238", "272", "30"),   # ＋/－、0.5h
    "#004": ("特殊", "8.5", "590", "1", "", "", "900", "", "", "", "", "", "30"),                       # cm、表示形式
    "S,005": ("特殊", "8.5", "330", "15", "", "", "1250", "100", "11.5", "13.5", "", "", "30"),        # 空白入り
    'S"006': ("標準", "8.5", "330", "", "", "", "", "", "", "", "", "", "30"),                          # 単位なし → C102
    "NG1": ("標準", "NG2", "NG1", "NG1", "NG1", "NG1", "NG", "NG", "NG", "NG", "NG", "NG", ""),         # 抽出NG印は通す
    "S 008": ("不明な種別", "約8.5mm", "", "", "330", "345", "1250", "100", "11.5", "13.5", "", "", ""),
    "試験🙂009": ("標準", "8.5", "330", "15", "", "", "1250", "100", "11.5", "13.5", "", "", "0.001"),
    "EMPTY": ("", "NG1", "NG1", "NG1", "NG1", "NG1", "NG1", "NG1", "NG1", "NG1", "NG1", "NG1", ""),
}
COLS = ["kind", "width", "length_nom", "length_tol", "length_min", "length_max", "rating_w", "rating_w_tol",
        "rating_a_min", "rating_a_max", "rating_w_plus", "rating_w_minus", "test_time"]


def convert(inp: Path, out_dir: Path, capsys, *extra) -> tuple[int, dict]:
    code = main(["convert", str(inp), "-c", str(RULES), "--out-dir", str(out_dir), "--format", "json",
                 "--verify", *extra])
    return code, json.loads(capsys.readouterr().out)


def table(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]


def check_result(j: dict, src: Path) -> None:
    """tenugui の結果を、入力（chozu の出力）と突き合わせて確かめる。"""
    h_in, rows_in = read_csv(src, "utf-8-sig")
    h, rows = table(j["files"]["output"])
    idx = {c: i for i, c in enumerate(h)}
    # 行を1つも落とさない・足さない（シート名が # で始まる行、カンマ・引用符・絵文字入りの行も）
    assert [r[0] for r in rows] == SHEETS == [r[0] for r in rows_in]
    assert j["rows"] == len(SHEETS)
    # 1セルずつ
    for r in rows:
        assert tuple(r[idx[c]] for c in COLS) == EXPECTED[r[0]], r[0]
    # ルールのかかっていない列は1文字も変えない（セル内改行・カンマ・引用符・タグを含む備考、missing_info）
    for name in ("issue_date", "part_no", "note"):
        i_in = h_in.index(name)
        assert [r[idx[name]] for r in rows] == [r[i_in] for r in rows_in], name
    assert [c for c in h if c.startswith("missing_info")] == [
        "missing_info0", "missing_info1", "missing_info0#2", "missing_info1#2", "missing_info0#3", "missing_info1#3"]
    mi = [i for i, c in enumerate(h_in) if c.startswith("missing_info")]
    mo = [i for i, c in enumerate(h) if c.startswith("missing_info")]
    assert [[r[i] for i in mo] for r in rows] == [[r[i] for i in mi] for r in rows_in]
    # 元の列を消したもの
    assert "length" not in h and "rating" not in h
    # 報告: 何がどこで起きたか
    got = sorted((i["key"], i["code"], i["column"]) for i in j["issues"])
    assert got == [("S 008", "C101", "width"), ("S 008", "C121", "kind"),
                   ('S"006', "C102", "rating"), ('S"006', "C102", "width")]
    assert j["counts"] == {"error": 3, "warning": 1}
    assert j["verification"]["ok"] and j["verification"]["summary"]["violations"] == 0
    cov = {c["column"]: ([x["count"] for x in c["cases"]], c["unmatched"], c["ng"]) for c in j["case_coverage"]}
    assert cov == {"length": ([7, 1], 0, 2), "rating": ([7, 1], 0, 2)}
    assert j["header"]["duplicates"] == ["missing_info0", "missing_info1"]


def test_frozen_chozu_output_through_tenugui(tmp_path, capsys):
    code, j = convert(FROZEN, tmp_path, capsys)
    assert code == 0
    check_result(j, FROZEN)


def test_reports_are_wellformed_and_keep_evidence(tmp_path, capsys):
    _, j = convert(FROZEN, tmp_path, capsys)
    rep = json.loads(Path(j["files"]["json"]).read_text(encoding="utf-8"))
    assert rep["rows"] == 10 and len(rep["issues"]) == 4
    # 消した列の、変換できなかった元の値が報告に残っている
    assert any(i["raw"] == "1250" and i["column"] == "rating" for i in rep["issues"])
    html = Path(j["files"]["html"]).read_text(encoding="utf-8")
    assert html.count("<script") == 1                       # 自前の1つだけ。値に入っていたタグは実行されない形になっている
    keys = {r[2] for r in embedded_table(html)["rows"]}
    for name in ("#004", "S,005", 'S"006', "試験🙂009"):
        assert name in keys


def test_fail_on_error_stops_the_pipeline(tmp_path, capsys):
    code, j = convert(FROZEN, tmp_path, capsys, "--fail-on", "error")
    assert code == 1 and not j["ok"]


def test_second_stage_output_is_stable_for_split_columns(tmp_path, capsys):
    """tenugui の出力をもう一度 tenugui にかけても、数値にした列は変わらない（後段が再実行しても壊れない）。"""
    _, j = convert(FROZEN, tmp_path, capsys)
    h, rows = table(j["files"]["output"])
    rerun = tmp_path / "again.toml"
    rerun.write_text('schema_version = 1\n[[field]]\nsrc = "test_time"\nto = "min"\ndefault_unit = "min"\n'
                     '[[field]]\nsrc = ["length_nom", "length_tol", "rating_w"]\nnumber_only = true\nstrip_units = ["*"]\n',
                     encoding="utf-8")
    assert main(["convert", j["files"]["output"], "-c", str(rerun), "--out-dir", str(tmp_path / "2"),
                 "--format", "json"]) == 0
    j2 = json.loads(capsys.readouterr().out)
    assert j2["counts"] == {"error": 0, "warning": 0} and j2["changed_cells"] == 0
    assert table(j2["files"]["output"]) == (h, rows)


def test_chozu_ng_report_is_not_mistaken_for_data(tmp_path, capsys):
    """chozu の NG 一覧（1行目が `# chozu: ...`）を渡しても、コメント行をヘッダと取り違えない。"""
    ng = tmp_path / "output_PIPE_ng.csv"
    ng.write_text("# chozu: /x/y.toml (更新 2026-11-16 22:55:18 / sha256:0ce385ed7f6d)\n"
                  "シート名,セクション,項目名,NG種別,検索パターン,必須項目\n#004,1.寸法,width,NG2,幅,必須\n",
                  encoding="utf-8-sig")
    h, rows = read_csv(ng, "utf-8-sig")
    assert h[0] == "シート名" and rows == [["#004", "1.寸法", "width", "NG2", "幅", "必須"]]
    assert main(["convert", str(ng), "-c", str(RULES)]) == 2            # 列が違うので止まる（黙って空の結果を出さない）
    assert "C001" in capsys.readouterr().err


# ---------------------------------------------------------------- 本物の chozu を動かす
def _chozu_env() -> dict:
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


@pytest.fixture(scope="module")
def chozu_run(tmp_path_factory):
    pytest.importorskip("chozu", reason="chozu が import できない環境では、固定の入力を使うテストだけを走らせる")
    pytest.importorskip("openpyxl")
    spec = importlib.util.spec_from_file_location("make_workbook", HERE / "make_workbook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path_factory.mktemp("chozu")
    xlsx = mod.build(d / "pipeline.xlsx")

    def run(*extra: str, out: str = "out") -> Path:
        r = subprocess.run([sys.executable, "-m", "chozu.cli", "extract", str(xlsx), "PIPE", "--toml",
                            str(HERE / "chozu_pipeline.toml"), "--out-dir", str(d / out), *extra],
                           capture_output=True, encoding="utf-8", env=_chozu_env())
        assert r.returncode == 0, r.stderr
        return d / out
    return run


def test_live_chozu_output_matches_frozen_fixture(chozu_run):
    """chozu の出力の形が変わったら、ここで気づく（固定の入力を作り直す合図）。"""
    out = chozu_run()
    assert (out / "output_PIPE.csv").read_bytes() == FROZEN.read_bytes()


def test_live_pipeline_end_to_end(chozu_run, tmp_path, capsys):
    out = chozu_run()
    code, j = convert(out / "output_PIPE.csv", tmp_path, capsys)
    assert code == 0
    check_result(j, out / "output_PIPE.csv")


def test_live_chozu_ng_csv_and_merge_mode(chozu_run, tmp_path, capsys):
    out = chozu_run()
    h, rows = read_csv(out / "output_PIPE_ng.csv", "utf-8-sig")
    assert h[0] == "シート名" and len(rows) == 13 and {r[0] for r in rows} == {"NG1", "EMPTY"}
    merged = chozu_run("--merge", out="merge") / "output_PIPE_merged.csv"
    code, j = convert(merged, tmp_path, capsys)
    assert code == 0 and j["rows"] == 10
    h, rows = table(j["files"]["output"])
    assert h[:2] == ["元ファイル", "シート名"] and [r[1] for r in rows] == SHEETS
    assert {i["key"] for i in j["issues"]} == {"pipeline.xlsx"}      # --merge では先頭列がファイル名になる


def test_live_without_ng_marks(chozu_run, tmp_path, capsys):
    """chozu --no-ng-in-csv（NG を空にする）でも、行と値は変わらない。"""
    out = chozu_run("--no-ng-in-csv", out="nong")
    code, j = convert(out / "output_PIPE.csv", tmp_path, capsys)
    h, rows = table(j["files"]["output"])
    idx = {c: i for i, c in enumerate(h)}
    assert code == 0 and [r[0] for r in rows] == SHEETS
    assert rows[9][idx["width"]] == "" and rows[0][idx["width"]] == "8.5"

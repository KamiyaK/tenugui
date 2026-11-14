import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tenugui import ConfigError, config_from_dict, convert_rows
from tenugui.analysis import (diff_changes, draft_toml, header_drift, idempotency_report, lint,
                              synonym_candidates, try_row, write_lock)
from tenugui.cli import main


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


H = ["sheet", "spec", "note"]


# ---- parts / match -------------------------------------------------------
PARTS = dict(field=[{"src": "spec",
                     "match": r"^(?P<w>[\d.]+[kK]?W)(?:±(?P<t>[\d.]+W))?(?:\((?P<lo>[\d.]+)~(?P<hi>[\d.]+)(?P<u>[A-Za-z]+)\))?",
                     "parts": {"w": {"out": "w", "to": "W", "default_unit": "W"},
                               "t": {"out": "tol", "to": "W", "default_unit": "W"},
                               "lo": {"out": "amin", "to": "A", "unit_group": "u"},
                               "hi": {"out": "amax", "to": "A", "unit_group": "u"}}}])


def test_parts_split_one_rule_into_columns():
    c = cfg(**PARTS)
    cols, rows, issues = convert_rows(c, H, [["a", "1250W±100W(11.5～13.5A)", ""]])
    r = dict(zip(cols, rows[0]))
    assert (r["w"], r["tol"], r["amin"], r["amax"]) == ("1250", "100", "11.5", "13.5")
    assert r["spec"] == "1250W±100W(11.5～13.5A)" and issues == []     # 元列は残る


def test_parts_optional_groups_and_unit_conversion_via_group():
    c = cfg(**PARTS)
    cols, rows, issues = convert_rows(c, H, [["a", "1.2kW", ""], ["b", "5W(500~900mA)", ""]])
    r0, r1 = (dict(zip(cols, r)) for r in rows)
    assert (r0["w"], r0["tol"], r0["amin"]) == ("1200", "", "") and issues == []
    assert (r1["amin"], r1["amax"]) == ("0.5", "0.9")            # mA → A


def test_parts_no_match_reports_once():
    c = cfg(**PARTS)
    _, rows, issues = convert_rows(c, H, [["a", "不明", ""]])
    assert [i.code for i in issues] == ["C111"] and rows[0][-4:] == ["", "", "", ""]


def test_parts_note_and_required():
    c = cfg(field=[{"src": "spec", "match": r"^(?P<n>\d+)(?:\((?P<note>[^)]*)\))?$",
                    "parts": {"n": {"out": "n"}, "note": {"out": "note2", "required": True}}}])
    cols, rows, issues = convert_rows(c, H, [["a", "4(二重)", ""], ["b", "4", ""]])
    assert dict(zip(cols, rows[0]))["note2"] == "二重"
    assert [(i.row, i.code) for i in issues] == [(2, "C201")]


def test_parts_config_errors():
    def bad(**kw):
        with pytest.raises(ConfigError) as e:
            cfg(**kw)
        return str(e.value)
    assert "名前付きグループ" in bad(field=[{"src": "a", "match": r"(?P<x>\d)", "parts": {"y": {"out": "o"}}}])
    assert "out が必要" in bad(field=[{"src": "a", "match": r"(?P<x>\d)", "parts": {"x": {}}}])
    assert "unit_group" in bad(field=[{"src": "a", "match": r"(?P<x>\d)",
                                       "parts": {"x": {"out": "o", "to": "A", "unit_group": "zz"}}}])
    assert "一緒に書きます" in bad(field=[{"src": "a", "match": r"(?P<x>\d)"}])
    assert "未知のキー" in bad(field=[{"src": "a", "match": r"(?P<x>\d)", "extract": "(\\d)",
                                    "parts": {"x": {"out": "o"}}}])


# ---- diff / 冪等性 / lint --------------------------------------------------
def test_diff_changes_only_changed_existing_columns():
    c = cfg(synonyms={"s": {"標準型": ["STD"]}}, field=[{"src": "spec", "synonyms": "s"}])
    rows = [["a", "STD", "x"], ["b", "標準型", "x"]]
    h, o, _ = convert_rows(c, H, rows)
    assert diff_changes(H, rows, h, o) == [
        {"row": 1, "key": "a", "column": "spec", "before": "STD", "after": "標準型"}]


def test_idempotency_detects_unit_loss_and_passes_with_default_unit():
    bad = cfg(field=[{"src": "spec", "to": "mm"}])
    rep = idempotency_report(bad, H, [["a", "5cm", ""]])
    assert rep and rep[0]["column"] == "spec" and "C102" in rep[0]["second"]
    assert any(w["code"] == "L101" for w in lint(bad))
    good = cfg(field=[{"src": "spec", "to": "mm", "default_unit": "mm"}])
    assert idempotency_report(good, H, [["a", "5cm", ""], ["b", "30", ""]]) == []
    assert not any(w["code"] == "L101" for w in lint(good))


def test_idempotent_sample_config_runs_twice_same():
    c = cfg(synonyms={"s": {"標準型": ["STD", "標準"]}},
            field=[{"src": "spec", "synonyms": "s"}, {"src": "note", "to": "V", "default_unit": "V"}])
    rows = [["a", "std", "1.2kV"], ["b", "標準", "300"]]
    h1, r1, _ = convert_rows(c, H, rows)
    h2, r2, _ = convert_rows(c, h1, r1)
    assert (h1, r1) == (h2, r2)


def test_lint_unused_synonym_set():
    assert any(w["code"] == "L201" for w in lint(cfg(synonyms={"zz": {"A": ["a"]}})))


# ---- lock ------------------------------------------------------------------
def test_lock_detects_added_and_removed_columns(tmp_path):
    toml = tmp_path / "r.toml"
    toml.write_text("schema_version = 1\n", encoding="utf-8")
    assert header_drift(toml, ["a", "b"]) is None
    write_lock(toml, ["a", "b"])
    assert header_drift(toml, ["a", "b"])["changed"] is False
    d = header_drift(toml, ["a", "c"])
    assert d["changed"] and d["added"] == ["c"] and d["removed"] == ["b"]


# ---- survey / draft --------------------------------------------------------
def test_synonym_candidates_exact_similar_and_digit_guard():
    v = Counter({"標準型": 5, "標準": 2, "ＳＴＤ": 1, "std": 3, "P-100": 4, "P-101": 4})
    cands = synonym_candidates(v)
    kinds = {c["canonical"]: c for c in cands}
    assert kinds["標準型"]["kind"] == "similar" and "標準" in kinds["標準型"]["variants"]
    assert kinds["std"]["kind"] == "exact" and kinds["std"]["variants"] == ["ＳＴＤ"]
    assert not any("P-100" in [c["canonical"], *c["variants"]] for c in cands)   # 数字違いは別物


def test_draft_loads_and_only_emits_exact_synonyms():
    rows = [["a", "35mm", "std"], ["b", "3.5cm", "ＳＴＤ"], ["c", "40", "標準"]]
    text = draft_toml(cfg(), H, rows)
    c = config_from_dict(__import__("tomllib").loads(text))     # 下書きが読み込める
    assert any(f.src == "spec" and f.to == "mm" for f in c.fields)
    assert any(f.src == "note" and f.synonyms for f in c.fields)
    assert "標準" not in text.split("[[field]]")[0] or "要確認" in text


# ---- try / CLI(JSON) -----------------------------------------------------------
def test_try_row_runs_relevant_rules_only_with_trace():
    c = cfg(field=[{"src": "spec", "to": "mm", "default_unit": "mm"}, {"src": "note", "to": "V"}],
            derive=[{"out": "d", "expr": "x * 2", "vars": {"x": "spec"}}])
    res = try_row(c, {"spec": "3.5cm"})
    assert res["outputs"]["spec"] == "35" and res["outputs"]["d"] == "70"
    assert res["skipped_fields"] == ["note"]
    assert [t["stage"] for t in res["trace"]["spec"]] == ["normalize", "quantity", "format"]


@pytest.fixture
def files(tmp_path):
    (tmp_path / "r.toml").write_text(
        'schema_version = 1\n[[field]]\nsrc = "spec"\nto = "mm"\ndefault_unit = "mm"\n', encoding="utf-8")
    (tmp_path / "in.csv").write_text("sheet,spec,note\na,3.5cm,x\nb,abc,y\nc,NG1,z\n", encoding="utf-8")
    return tmp_path


def run_cli(capsys, *argv):
    code = main(list(map(str, argv)))
    return code, capsys.readouterr().out


def test_cli_convert_json_diff_and_exit_codes(files, capsys):
    base = ["convert", files / "in.csv", "-c", files / "r.toml", "-o", files / "out.csv", "--format", "json", "--detail-files"]
    code, out = run_cli(capsys, *base)
    d = json.loads(out)
    assert code == 0 and d["schema"] == "tenugui.convert/1" and d["counts"] == {"error": 1, "warning": 0}
    assert d["issues"][0]["code"] == "C101" and d["issues"][0]["hint"]
    assert d["changed_cells"] == 1 and (files / "out_diff.csv").exists()
    code, _ = run_cli(capsys, *base, "--fail-on", "error")
    assert code == 1
    code, out = run_cli(capsys, *base, "--max-issues", "0")
    assert json.loads(out)["issues_truncated"] is True


def test_cli_config_error_is_json_and_exit_2(files, capsys):
    (files / "bad.toml").write_text('schema_version = 1\n[[field]]\nsrc="a"\nunit="mm"\n', encoding="utf-8")
    code, out = run_cli(capsys, "check", "-c", files / "bad.toml", "--format", "json")
    d = json.loads(out)
    assert code == 2 and d["ok"] is False and d["config_errors"][0]["where"].startswith("[[field]]")


def test_cli_try_and_survey_and_draft_json(files, capsys):
    code, out = run_cli(capsys, "try", "-c", files / "r.toml", "--field", "spec", "3.5cm", "--format", "json")
    d = json.loads(out)
    assert code == 0 and d["outputs"]["spec"] == "35"
    code, out = run_cli(capsys, "survey", files / "in.csv", "-k", "spec", "--format", "json")
    assert json.loads(out)["columns"][0]["column"] == "spec"
    code, _ = run_cli(capsys, "draft", files / "in.csv", "-o", files / "d.toml")
    assert code == 0 and (files / "d.toml").exists()
    code, out = run_cli(capsys, "check", "-c", files / "d.toml", files / "in.csv", "--format", "json")
    assert code == 0 and json.loads(out)["ok"]


def test_cli_lock_then_drift_warning(files, capsys):
    run_cli(capsys, "lock", files / "in.csv", "-c", files / "r.toml")
    (files / "in2.csv").write_text("sheet,spec,note,new\na,1,x,y\n", encoding="utf-8")
    code, out = run_cli(capsys, "convert", files / "in2.csv", "-c", files / "r.toml", "-o", files / "o2.csv",
                        "--format", "json")
    d = json.loads(out)
    assert d["header"]["drift"]["added"] == ["new"]


def test_python_dash_m_works(files):
    r = subprocess.run([sys.executable, "-m", "tenugui", "check", "-c", str(files / "r.toml")],
                       capture_output=True, text=True, env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")})
    assert r.returncode == 0 and "設定OK" in r.stdout


def test_ui_model_headless(files):
    from tenugui.ui import Model
    m = Model()
    lines = m.convert(str(files / "in.csv"), str(files / "r.toml"), str(files / "ui.csv"), True)
    assert "エラー 1 件" in lines[1] and m.changes and m.example_value("spec") == "3.5cm"
    assert m.try_value(str(files / "r.toml"), "spec", "2cm")["outputs"]["spec"] == "20"


def test_idempotency_ignores_values_that_already_failed_in_first_pass():
    c = cfg(field=[{"src": "spec", "to": "mm", "default_unit": "mm"}])
    assert idempotency_report(c, H, [["a", "abc", ""], ["b", "5cm", ""]]) == []

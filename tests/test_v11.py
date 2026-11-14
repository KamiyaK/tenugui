"""0.11.0: 訂正・保留（別ファイル）、来歴、修正依頼リスト、用語対応表、--fail-on hold。"""
import csv
import json
from pathlib import Path

import pytest

from tenugui import ConfigError, config_from_dict, convert_rows, load_config
from tenugui.analysis import idempotency_report, try_row, verify_before_after
from tenugui.cli import main
from tenugui.corrections import CORRECT_KEYS, HOLD_KEYS, Provenance, corrections_path

RULES = '''schema_version = 1
[synonyms.shape]
"標準型" = ["標準", "STD"]
[[field]]
src = "shape"
synonyms = "shape"
[[field]]
src = "dia"
out = "dia_mm"
to = "mm"
default_unit = "mm"
[[field]]
src = "volt"
to = "V"
default_unit = "V"
[[derive]]
out = "half"
expr = "d / 2"
vars = { d = "dia_mm" }
'''
COR = '''schema_version = 1
[[correct]]
column = "dia"
value = "35nm"
to = "35mm"
reason = "外径が nm になることはない"
[[hold]]
column = "volt"
value = "100/200V"
reason = "併記か範囲か決められない"
ref = "照会中"
'''
CSV = "sheet,shape,dia,volt,memo\nP1,標準,35mm,100V,a\nP2,STD,35nm,100V,b\nP3,標準型,3.5cm,100/200V,c\nP4,標準型,35,NG1,d\n"


def rules_dict():
    import tomllib
    return tomllib.loads(RULES)


def cor_dict(text=COR):
    import tomllib
    return tomllib.loads(text)


@pytest.fixture
def work(tmp_path):
    (tmp_path / "in.csv").write_text(CSV, encoding="utf-8-sig")
    (tmp_path / "r.toml").write_text(RULES, encoding="utf-8")
    (tmp_path / "r.corrections.toml").write_text(COR, encoding="utf-8")
    (tmp_path / "plain.toml").write_text(RULES, encoding="utf-8")        # 訂正ファイルなし
    return tmp_path


def rows():
    r = list(csv.reader(CSV.splitlines()))
    return r[0], r[1:]


# ---- 変換エンジン -------------------------------------------------------------
def test_correct_is_applied_before_rules_and_hold_passes_raw():
    cfg = config_from_dict(rules_dict(), cor_dict())
    h, rs = rows()
    prov = Provenance()
    oh, out, issues = convert_rows(cfg, h, rs, prov)
    o = [dict(zip(oh, r)) for r in out]
    assert o[1]["dia"] == "35mm" and o[1]["dia_mm"] == "35" and o[1]["half"] == "17.5"   # 訂正してからルールへ
    assert o[2]["volt"] == "100/200V"                                                   # 保留: 元の値のまま
    assert [(i.row, i.column, i.code, i.severity) for i in issues] == [(3, "volt", "C141", "W")]
    assert set(prov.corrected) == {(2, "dia")} and set(prov.held) == {(3, "volt")}
    assert prov.syn[("shape", "標準型", "STD")] == 1 and prov.syn_cols["shape"] == {"shape"}


def test_without_corrections_nothing_changes():
    h, rs = rows()
    oh, out, issues = convert_rows(config_from_dict(rules_dict()), h, rs)
    o = [dict(zip(oh, r)) for r in out]
    assert o[1]["dia"] == "35nm" and o[1]["dia_mm"] == ""               # 訂正が無ければ、誤記はエラーのまま残る
    assert [(i.row, i.column, i.code) for i in issues] == [(2, "dia", "C103"), (3, "volt", "C103")]


def test_hold_blanks_alias_outputs_and_derives_without_extra_reports():
    cor = cor_dict('schema_version = 1\n[[hold]]\ncolumn = "dia"\nvalue = "3.5cm"\nreason = "単位が怪しい"\n')
    h, rs = rows()
    oh, out, issues = convert_rows(config_from_dict(rules_dict(), cor), h, rs)
    o = dict(zip(oh, out[2]))
    assert o["dia"] == "3.5cm" and o["dia_mm"] == "" and o["half"] == ""
    assert [i.code for i in issues if i.row == 3] == ["C141", "C103"]   # 派生は二重に報告しない（C103 は volt）


def test_hold_on_raw_column_used_by_derive():
    raw = {"schema_version": 1, "derive": [{"out": "x2", "expr": "a * 2", "vars": {"a": "n"}}]}
    cor = {"schema_version": 1, "hold": [{"column": "n", "value": "1か2", "reason": "不明"}]}
    oh, out, issues = convert_rows(config_from_dict(raw, cor), ["k", "n"], [["a", "3"], ["b", "1か2"]])
    assert [r[oh.index("x2")] for r in out] == ["6", ""]
    assert [i.code for i in issues] == ["C141"]


def test_key_scoped_entry_wins_over_column_wide():
    cor = {"schema_version": 1,
           "correct": [{"column": "c", "value": "x", "to": "y", "reason": "全体"},
                       {"column": "c", "value": "x", "to": "z", "reason": "このシートだけ", "key": ["B", "C"]}]}
    cfg = config_from_dict({"schema_version": 1}, cor)
    _, out, _ = convert_rows(cfg, ["k", "c"], [["A", "x"], ["B", "ｘ "], ["C", "x"], ["D", "w"]])
    assert [r[1] for r in out] == ["y", "z", "z", "w"]                  # 全角・空白の違いは無視して当たる


def test_reserved_and_empty_are_never_touched():
    cor = {"schema_version": 1, "hold": [{"column": "c", "value": "x", "reason": "r"}]}
    cfg = config_from_dict({"schema_version": 1}, cor)
    _, out, issues = convert_rows(cfg, ["k", "c"], [["A", "NG1"], ["B", ""]])
    assert [r[1] for r in out] == ["NG1", ""] and not issues


@pytest.mark.parametrize("body, needle", [
    ('[[correct]]\ncolumn = "c"\nvalue = "a"\nto = "b"\n', "reason が必要"),
    ('[[hold]]\ncolumn = "c"\nvalue = "a"\nreason = " "\n', "reason が必要"),
    ('[[correct]]\ncolumn = "c"\nvalue = "a"\nto = "ａ"\nreason = "r"\n', "value と to が同じ"),
    ('[[correct]]\ncolumn = "c"\nvalue = "a"\nto = "b"\nreason = "r"\nwhy = "x"\n', "未知のキー 'why'"),
    ('[[hold]]\ncolumn = "c"\nvalue = "a"\nreason = "r"\nto = "b"\n', "未知のキー 'to'"),
    ('[[hold]]\ncolumn = "c"\nvalue = "NG2"\nreason = "r"\n', "抽出NG印"),
    ('[[hold]]\ncolumn = "c"\nvalue = "a"\nreason = "r"\nkey = 3\n', "key は"),
    ('[[correct]]\ncolumn = "c"\nvalue = "a"\nto = "b"\nreason = "r"\n[[hold]]\ncolumn = "c"\nvalue = "a"\nreason = "r"\n',
     "にもあります"),
    ('[[correct]]\ncolumn = "c"\nvalue = "a"\nto = "b"\nreason = "r"\n[[correct]]\ncolumn = "c"\nvalue = "b"\nto = "d"\n'
     'reason = "r"\n', "訂正後の値 'b'"),
    ('[[fix]]\ncolumn = "c"\n', "未知のキー 'fix'"),
])
def test_corrections_file_errors(body, needle):
    with pytest.raises(ConfigError) as e:
        config_from_dict({"schema_version": 1}, cor_dict("schema_version = 1\n" + body))
    assert needle in str(e.value)


def test_corrections_schema_version_required():
    with pytest.raises(ConfigError) as e:
        config_from_dict({"schema_version": 1}, {"hold": []})
    assert "schema_version = 1" in str(e.value)


def test_same_value_different_keys_is_allowed():
    cor = {"schema_version": 1, "correct": [
        {"column": "c", "value": "a", "to": "b", "reason": "r", "key": "A"},
        {"column": "c", "value": "a", "to": "d", "reason": "r", "key": "B"}]}
    assert len(config_from_dict({"schema_version": 1}, cor).corrections.entries) == 2


# ---- 検証・冪等性・try --------------------------------------------------------
def test_verify_treats_correction_of_unruled_column_as_intended():
    cor = {"schema_version": 1, "correct": [{"column": "memo", "value": "b", "to": "B", "reason": "r"}]}
    cfg = config_from_dict(rules_dict(), cor)
    h, rs = rows()
    prov = Provenance()
    oh, out, issues = convert_rows(cfg, h, rs, prov)
    assert verify_before_after(cfg, h, rs, oh, out, issues, set(prov.corrected))["ok"]
    bad = verify_before_after(cfg, h, rs, oh, out, issues)             # 訂正だと知らなければ違反
    assert [v["kind"] for v in bad["violations"]] == ["untouched_changed"]


def test_idempotent_with_corrections():
    cfg = config_from_dict(rules_dict(), cor_dict())
    h, rs = rows()
    assert idempotency_report(cfg, h, rs) == []


def test_try_trace_shows_correct_and_hold():
    cfg = config_from_dict(rules_dict(), cor_dict())
    r = try_row(cfg, {"dia": "35nm", "volt": "100/200V"})
    assert r["trace"]["dia_mm"][0]["stage"] == "correct" and r["outputs"]["dia_mm"] == "35"
    assert [t["stage"] for t in r["trace"]["volt"]] == ["hold"]
    assert [i["code"] for i in r["issues"]] == ["C141"]


# ---- ファイルの読み込み ------------------------------------------------------
def test_sibling_file_is_loaded_automatically(work):
    assert corrections_path(work / "r.toml") == work / "r.corrections.toml"
    cfg = load_config(work / "r.toml")
    assert cfg.corrections and len(cfg.corrections.entries) == 2 and len(cfg.corrections.sha) == 12
    assert load_config(work / "plain.toml").corrections is None
    assert load_config(work / "plain.toml", work / "r.corrections.toml").corrections is not None


def test_explicit_corrections_file_missing_is_config_error(work, capsys):
    code = main(["check", "-c", str(work / "plain.toml"), "--corrections", str(work / "nope.toml"), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    assert code == 2 and "訂正ファイルが見つかりません" in j["config_errors"][0]["message"]


# ---- convert の出力 -----------------------------------------------------------
def run_convert(work, *extra, toml="r.toml"):
    return main(["convert", str(work / "in.csv"), "-c", str(work / toml), "--format", "json", *extra])


def test_convert_reports_provenance_and_writes_fix_list(work, capsys):
    code = run_convert(work)
    j = json.loads(capsys.readouterr().out)
    pv = j["provenance"]
    assert code == 0 and pv["counts"] == {"normalize": 4, "correct": 1, "hold": 1}
    assert [(g["kind"], g["column"], g["value"], g["to"], g["keys"]) for g in pv["fix_requests"]] == [
        ("hold", "volt", "100/200V", "", ["P3"]), ("correct", "dia", "35nm", "35mm", ["P2"])]
    assert pv["unused"] == [] and j["verification"]["ok"]
    assert [(c["kind"], c["row"], c["column"], c["outputs"]) for c in pv["cells"]] == [
        ("correct", 2, "dia", ["dia_mm", "half"]), ("hold", 3, "volt", ["volt"])]      # 後段が未検査にする列
    assert pv["glossary"][0]["canonical"] == "標準型" and pv["glossary"][0]["unified"] == 2
    fix = list(csv.reader(Path(j["files"]["fix"]).read_text(encoding="utf-8-sig").splitlines()))
    assert fix[0][:6] == ["種類", "row", "key", "column", "元の値", "訂正後"]
    assert [r[:6] for r in fix[1:]] == [["訂正", "2", "P2", "dia", "35nm", "35mm"],
                                        ["保留", "3", "P3", "volt", "100/200V", ""]]
    rep = json.loads(Path(j["files"]["json"]).read_text(encoding="utf-8"))
    assert {c["kind"] for c in rep["changes"]} == {"normalize", "correct"}
    assert rep["provenance"]["corrections_sha"] == pv["corrections_sha"]
    html = Path(j["files"]["html"]).read_text(encoding="utf-8")
    for needle in ("元データの修正依頼リスト", "用語対応表", "class='cor'", "class='hold'", "外径が nm になることはない"):
        assert needle in html


def test_convert_without_corrections_file_keeps_old_outputs(work, capsys):
    code = run_convert(work, toml="plain.toml")
    j = json.loads(capsys.readouterr().out)
    assert code == 0 and "fix" not in j["files"]
    assert j["provenance"]["corrections_file"] is None and j["provenance"]["fix_requests"] == []
    assert sorted(p.name for p in (work / "tenugui_out").iterdir()) == [
        "in_tenugui.csv", "in_tenugui.html", "in_tenugui.json"]
    html = (work / "tenugui_out" / "in_tenugui.html").read_text(encoding="utf-8")
    assert "修正依頼リスト" not in html and "保留（変換していない）" not in html


def test_fail_on_hold(work, capsys):
    assert run_convert(work, "--fail-on", "hold") == 1                  # 保留が残っている
    assert run_convert(work, "--fail-on", "error") == 0
    (work / "r.corrections.toml").write_text(COR.split("[[hold]]")[0], encoding="utf-8")
    assert run_convert(work, "--fail-on", "hold") == 1                  # 保留を外すと C103 のエラーになる
    (work / "in.csv").write_text(CSV.replace("100/200V", "100V"), encoding="utf-8-sig")
    assert run_convert(work, "--fail-on", "hold") == 0
    capsys.readouterr()


def test_unused_entries_are_reported_when_source_is_fixed(work, capsys):
    (work / "in.csv").write_text(CSV.replace("35nm", "35mm"), encoding="utf-8-sig")   # 元データが直った
    (work / "r.corrections.toml").write_text(
        COR + '[[hold]]\ncolumn = "nocol"\nvalue = "x"\nreason = "r"\n', encoding="utf-8")
    code = run_convert(work, "--fail-on", "warn")
    j = json.loads(capsys.readouterr().out)
    un = j["provenance"]["unused"]
    assert code == 1 and [(u["code"], u["where"], u["column_missing"]) for u in un] == [
        ("L301", "[[correct]]#1", False), ("L302", "[[hold]]#2", True)]
    assert "消せます" in un[0]["message"] and "入力にありません" in un[1]["message"]
    code = main(["check", "-c", str(work / "r.toml"), str(work / "in.csv"), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    assert code == 0 and [w["code"] for w in j["lint"]] == ["L303"]
    assert j["corrections"]["correct"] == 1 and j["corrections"]["hold"] == 2


def test_batch_reports_unused_only_when_no_file_hits(work, capsys):
    d = work / "data"
    d.mkdir()
    (d / "a.csv").write_text(CSV, encoding="utf-8-sig")
    (d / "b.csv").write_text("sheet,shape,dia,volt,memo\nQ1,標準,35mm,100V,a\n", encoding="utf-8-sig")
    (work / "r.corrections.toml").write_text(
        COR + '[[correct]]\ncolumn = "memo"\nvalue = "zzz"\nto = "y"\nreason = "r"\n', encoding="utf-8")
    code = main(["convert", str(d), "-c", str(work / "r.toml"), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    assert code == 0 and j["totals"]["corrected"] == 1 and j["totals"]["held"] == 1
    assert [u["where"] for u in j["unused"]] == ["[[correct]]#2"]       # b.csv に 35nm が無いのは普通なので出さない
    assert all(r["provenance"]["unused"] == [] and "_unused" not in r for r in j["results"])


def test_verify_command_accepts_corrected_output(work, capsys):
    run_convert(work)
    capsys.readouterr()
    code = main(["verify", str(work / "in.csv"), str(work / "tenugui_out" / "in_tenugui.csv"),
                 "-c", str(work / "r.toml"), "--format", "json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["ok"]


def test_ui_model_lines_mention_corrections(work):
    from tenugui.ui import Model
    lines = Model().convert(str(work / "in.csv"), str(work / "r.toml"), "")
    assert any("訂正 1 セル / 保留 1 セル" in x for x in lines)
    assert not any("訂正" in x for x in Model().convert(str(work / "in.csv"), str(work / "plain.toml"), ""))


# ---- glossary / schema --------------------------------------------------------
def test_glossary_command(work, capsys):
    assert main(["glossary", "-c", str(work / "r.toml"), "--format", "json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["schema"] == "tenugui.glossary/1" and j["rows"] is None
    assert j["terms"] == [{"set": "shape", "canonical": "標準型", "variants": ["標準", "STD"], "columns": ["shape"]}]
    assert main(["glossary", "-c", str(work / "r.toml"), str(work / "in.csv"), "--format", "json"]) == 0
    t = json.loads(capsys.readouterr().out)["terms"][0]
    assert t["count"] == 4 and t["unified"] == 2 and {u["value"] for u in t["used"]} == {"標準型", "標準", "STD"}
    assert main(["glossary", "-c", str(work / "r.toml"), "--format", "csv"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "同義語表,正式名,別名,列" and out[1] == "shape,標準型,標準 / STD,shape"
    assert main(["glossary", "-c", str(work / "r.toml")]) == 0
    assert "標準型  ←  標準 / STD" in capsys.readouterr().out


def test_schema_describes_corrections():
    from tenugui import schema as S
    sc = S.schema()
    assert set(sc["corrections"]["correct"]["keys"]) == CORRECT_KEYS
    assert set(sc["corrections"]["hold"]["keys"]) == HOLD_KEYS
    assert {"C141", "L301", "L302", "L303"} <= set(sc["codes"])
    assert "[[hold]]" in S.schema_text(sc)


def test_example_files_work(capsys):
    ex = Path(__file__).resolve().parent.parent / "examples"
    assert main(["check", "-c", str(ex / "corrections_demo.toml"), str(ex / "corrections_demo_input.csv")]) == 0
    assert "訂正 1 件 / 保留 1 件" in capsys.readouterr().out

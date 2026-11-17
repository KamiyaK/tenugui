"""いじわる試験 第2弾: 落ちはしないが、黙って違う値になる / 黙ってファイルを壊す・失う ものを確かめる。

第1弾（test_torture.py）は「落ちない・止まらない・行を捨てない」。ここは次の4つ。
  1. 数値の読み違い（丸数字、桁区切り、許容差の単位、符号）
  2. 出力先が大事なファイルに重なる（設定・入力・レポート同士・一括処理の同名）
  3. 周辺のファイルや指定が壊れている（lock、文字コード、置換文字列、訂正ファイル）
  4. 自分で作ったもの（[[test]]、下書き）が、自分で読める
"""
from __future__ import annotations

import hashlib
import json
import random
import tomllib
from pathlib import Path

import pytest

from tenugui.analysis import as_test_block, draft_toml, run_tests, try_row, verify_files
from tenugui.cli import main
from tenugui.core import (ConfigError, check_header, compile_pattern, config_from_dict, convert_rows, plan_columns,
                          read_csv, shape_of, uniquify, write_csv)
from tenugui.units import format_decimal, normalize_text
from decimal import Decimal

RULES = 'schema_version = 1\n[[field]]\nsrc = "v"\nto = "mm"\n'
CSV = "k,v\nA,5cm\nB,7mm\n"


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


def one(field, value, **kw):
    """1つの値を変換。戻り値 (出力列 → 値, 報告コードのリスト)。"""
    c = cfg(field=[field] if isinstance(field, dict) else field, **kw)
    cols, rows, issues = convert_rows(c, ["k", "v"], [["K1", value]])
    return dict(zip(cols, rows[0])), [i.code for i in issues]


def write(path: Path, text: str, enc: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=enc, newline="")
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def work(tmp_path):
    write(tmp_path / "in.csv", CSV)
    write(tmp_path / "r.toml", RULES)
    return tmp_path


def run(capsys, *args) -> tuple[int, str, str]:
    code = main([str(a) for a in args])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


TO_MM = {"src": "v", "to": "mm", "default_unit": "mm"}
ONLY_MM = {"src": "v", "number_only": True, "strip_units": ["mm"]}
ANY = {"src": "v", "number_only": True, "strip_units": ["*"]}
TOL_BARE = {"src": "v", "preset": "tolerance", "parts": {"nom": "n", "tol": "t", "hi": "hi", "lo": "lo"}}
TOL_V = {"src": "v", "preset": "tolerance", "parts": {"nom": {"out": "n", "to": "V"}, "tol": {"out": "t", "to": "V"}}}
RNG_BARE = {"src": "v", "preset": "range", "parts": {"lo": "lo", "hi": "hi"}}


# ================================================================ 1. 数値の読み違い
@pytest.mark.parametrize("value", ["5①", "5⑳", "5⒈", "100①mm", "5²", "5¹", "5₂", "5³mm"])
@pytest.mark.parametrize("field", [TO_MM, ONLY_MM])
def test_note_marks_are_not_fused_into_the_number(field, value):
    """丸数字や上付き数字（注記）が、数値の続きとして読まれていた（5① → 51、5² → 52）。"""
    out, codes = one(field, value)
    assert out["v"] == value and codes, (out, codes)        # 読めない値として元のまま残し、必ず報告する


def test_note_marks_in_presets_and_patterns_do_not_match():
    out, codes = one(TOL_BARE, "100①±5")
    assert (out["n"], out["t"], codes) == ("", "", ["C111"])        # 1001±5 と読んでいた
    out, codes = one(TOL_BARE, "100±5①")
    assert (out["n"], out["t"], codes) == ("", "", ["C111"])        # 100±51 と読んでいた
    out, codes = one({"src": "v", "pattern": "#±#mm", "parts": {"n1": "a", "n2": "b"}}, "5①±1mm")
    assert (out["a"], codes) == ("", ["C111"])


def test_units_written_with_superscripts_still_work():
    """単位の後ろの上付き数字（mm²、min⁻¹）は今までどおり単位の指数。"""
    assert one({"src": "v", "to": "mm2"}, "5cm²") == ({"k": "K1", "v": "500"}, [])
    assert one({"src": "v", "to": "mm2"}, "5㎠") == ({"k": "K1", "v": "500"}, [])
    assert normalize_text("mm²") == "mm2" and normalize_text("５ｍｍ") == "5mm"
    assert shape_of("5①") == "#(#)"                     # 形でも、注記つきの値が # にまとめられない


@pytest.mark.parametrize("value, out", [("1,200", "1200"), ("12,345", "12345"), ("1,234,567", "1234567"),
                                        ("1,234.5", "1234.5"), ("-1,200", "-1200"), ("５，０００", "5000")])
def test_thousands_separators_accepted(value, out):
    assert one(TO_MM, value) == ({"k": "K1", "v": out}, [])


@pytest.mark.parametrize("value", ["0,500", "01,234", "12345,678", "1,2345", "1,23", "3,4", "1,234,56"])
@pytest.mark.parametrize("field", [TO_MM, ONLY_MM, ANY])
def test_misplaced_commas_are_not_read_as_thousands(field, value):
    """0,500 → 500、12345,678 → 12345678、（何でも捨てる指定で）3,4 → 3 と読んでいた。"""
    out, codes = one(field, value)
    assert out["v"] == value and codes == ["C101"], (out, codes)


def test_number_only_any_does_not_cut_a_number_in_the_middle():
    assert one(ANY, "1.2.3") == ({"k": "K1", "v": "1.2.3"}, ["C101"])      # 1.2 と読んでいた
    assert one(ANY, "5mm 注") == ({"k": "K1", "v": "5"}, [])               # 単位・注記を捨てるのは今までどおり
    assert one(ANY, "100±5") == ({"k": "K1", "v": "100"}, [])


@pytest.mark.parametrize("value", ["5cm^9999999", "5cm^99999999999999999999", "5cm^-9999999", "5mm^25", "5km^999"])
def test_absurd_unit_exponent_is_reported_not_a_crash(value):
    """単位の指数が大きいと、換算の計算（10 の 9999999 乗）で落ちていた。"""
    assert one({"src": "v", "to": "mm"}, value) == ({"k": "K1", "v": value}, ["C103"])


def test_unit_alias_containing_choon():
    """別名に長音「ー」が入ると（メートル、オーム）、マイナス記号に置き換えられて決して一致しなかった。"""
    kw = {"unit_alias": {"メートル": "m", "オーム": "Ω"}}
    assert one({"src": "v", "to": "mm"}, "5メートル", **kw) == ({"k": "K1", "v": "5000"}, [])
    assert one({"src": "v", "to": "Ω"}, "5 オーム", **kw) == ({"k": "K1", "v": "5"}, [])
    assert one(TO_MM, "ー5") == ({"k": "K1", "v": "-5"}, [])           # 値の先頭の ー は今までどおりマイナス
    assert one({"src": "v", "to": "mm"}, "−5㎜")[0]["v"] == "-5"


def test_negative_zero_is_not_printed():
    assert format_decimal(Decimal("-0.001"), 2, False) == "0.00"       # -0.00 と出ていた
    assert format_decimal(Decimal("-0.001"), 2, True) == "0"
    assert format_decimal(Decimal("-0.01"), 2, False) == "-0.01"
    out, _ = one({"src": "v", "to": "mm", "default_unit": "mm", "decimals": 1, "trim": False}, "-0.04")
    assert out["v"] == "0.0"


# ---------------------------------------------------------------- preset
@pytest.mark.parametrize("value, nom, tol", [("1,200±50W", "1200", "50"), ("12,500W±1,000W", "12500", "1000"),
                                             ("1,200W", "1200", "")])
def test_preset_reads_thousands_separators(value, nom, tol):
    """許容差の preset で、1,200±50W の基準値が 1 になっていた（,200 が単位として読まれた）。"""
    out, codes = one(TOL_BARE, value)
    assert (out["n"], out["t"], codes) == (nom, tol, [])
    out, codes = one({"src": "v", "preset": "tolerance", "parts": {"nom": {"out": "n", "to": "W"},
                                                                   "tol": {"out": "t", "to": "W"}}}, value)
    assert (out["n"], out["t"], codes) == (nom, tol, [])


@pytest.mark.parametrize("value", ["100V-5V", "100mm,200mm", "100mm、200mm", "100mm;200mm", "1,2345±5"])
def test_preset_does_not_swallow_a_second_value_as_unit(value):
    """100V-5V や 100mm,200mm の後半を「単位」として読み、基準値 100 だけを黙って出していた。"""
    out, codes = one(TOL_BARE, value)
    assert (out["n"], out["t"], codes) == ("", "", ["C111"])


def test_range_preset_does_not_swallow_a_second_value():
    out, codes = one(RNG_BARE, "5~10A,20A")
    assert (out["lo"], out["hi"], codes) == ("", "", ["C111"])


@pytest.mark.parametrize("value", ["100V±5%", "100kV±5V", "100mm±5cm"])
def test_bare_parts_refuse_mixed_units(value):
    """単位を見ない部品（to なし）で、100V±5% の 5% を 5 として出していた。"""
    out, codes = one(TOL_BARE, value)
    assert (out["n"], out["t"]) == ("", "") and set(codes) == {"C103"}, (out, codes)


def test_bare_parts_refuse_mixed_units_in_range():
    out, codes = one(RNG_BARE, "500mA~1.2A")
    assert (out["lo"], out["hi"]) == ("", "") and set(codes) == {"C103"}
    out, codes = one({"src": "v", "preset": "range", "parts": {"lo": {"out": "lo", "to": "A"},
                                                               "hi": {"out": "hi", "to": "A"}}}, "500mA~1.2A")
    assert (out["lo"], out["hi"], codes) == ("0.5", "1.2", [])         # to を付ければ換算できる


@pytest.mark.parametrize("value, nom, tol", [("100±5V", "100", "5"), ("100V±5V", "100", "5"), ("100V±5v", "100", "5"),
                                             ("100mm±5㎜", "100", "5"), ("100±5%", "100", "5"), ("1500±50min-1", "1500", "50"),
                                             ("φ13±0.2", "13", "0.2"), ("100mm2±5", "100", "5"), ("100mm※1", "100", "")])
def test_bare_parts_same_unit_unchanged(value, nom, tol):
    out, codes = one(TOL_BARE, value)
    assert (out["n"], out["t"], codes) == (nom, tol, [])


@pytest.mark.parametrize("value, lo, hi", [("5∼10", "5", "10"), ("5〜10A", "5", "10"), ("−5~−1", "-5", "-1")])
def test_range_accepts_sign_and_tilde_variants(value, lo, hi):
    out, codes = one(RNG_BARE, value)
    assert (out["lo"], out["hi"], codes) == (lo, hi, [])


def test_presets_accept_unicode_minus():
    """U+2212 のマイナス（−100±5）が、通常の field では読めるのに preset / pattern では不一致だった。"""
    out, codes = one(TOL_BARE, "−100±5")
    assert (out["n"], out["t"], codes) == ("-100", "5", [])
    out, codes = one({"src": "v", "pattern": "#±#mm", "parts": {"n1": "a", "n2": "b"}}, "−5±1mm")
    assert (out["a"], out["b"], codes) == ("-5", "1", [])
    out, codes = one({"src": "v", "case": [{"pattern": "#mm", "parts": {"n1": "a"}}]}, "‐5mm")
    assert (out["a"], codes) == ("-5", [])


def test_mismatch_message_shows_the_shape_not_the_internal_regex():
    c = cfg(field=[TOL_BARE])
    _, _, issues = convert_rows(c, ["k", "v"], [["K1", "100V/200V"]])
    assert len(issues) == 1 and "preset = tolerance" in issues[0].message and "#V/#V" in issues[0].message
    assert "(?P<" not in issues[0].message


# ---------------------------------------------------------------- pattern の符号
@pytest.mark.parametrize("pattern, value, expect", [
    ("-#mm", "-5mm", {"n1": "-5"}),                        # 5 になっていた
    ("#~-#mm", "-10~-5mm", {"n1": "-10", "n2": "-5"}),     # 2つ目が 5 になっていた
    ("#(-#%)", "100(-5%)", {"n1": "100", "n2": "-5"}),
    ("{a}~-{b}mm", "1~-2mm", {"a": "1", "b": "-2"}),
    # 区切りとしての + と -（許容差の上下）は今までどおり、符号なしの大きさを取る
    ("#+#-#W", "100+5-3W", {"n1": "100", "n2": "5", "n3": "3"}),
    ("+#-#mm", "+5-3mm", {"n1": "5", "n2": "3"}),
    ("#±#mm", "-5±1mm", {"n1": "-5", "n2": "1"}),
])
def test_pattern_keeps_the_sign(pattern, value, expect):
    """形（-5mm → -#mm）をそのまま pattern に書くと、符号が落ちて 5 になっていた。"""
    rx, _ = compile_pattern(pattern)
    out, codes = one({"src": "v", "pattern": pattern, "parts": {k: f"o_{k}" for k in expect}}, value)
    assert ({k: out[f"o_{k}"] for k in expect}, codes) == (expect, [])


def test_shape_pasted_as_pattern_reproduces_the_value():
    """survey が返す形は、そのまま pattern に書ける（AGENT_REFERENCE 2b）。符号つきの値でも成り立つ。"""
    for value in ["-5mm", "-10~-5A", "5~-10A", "-5±1mm", "100W(-5~5A)", "-0.5mm"]:
        shape = shape_of(value)
        n = shape.count("#")
        out, codes = one({"src": "v", "pattern": shape, "parts": {f"n{i}": f"o{i}" for i in range(1, n + 1)}}, value)
        nums = [out[f"o{i}"] for i in range(1, n + 1)]
        text = value
        for x in nums:                      # 取り出した数値を順に消していくと、数字が残らない
            assert x in text, (value, shape, nums)
            text = text.replace(x, "", 1)
        assert codes == [] and not any(ch.isdigit() for ch in text) and "-" not in text, (value, shape, nums)


# ================================================================ 2. 出力先が大事なファイルに重なる
@pytest.mark.parametrize("target", ["r.toml", "r.corrections.toml", "r.lock.json", "in.csv", "sub/../in.csv"])
def test_convert_output_never_overwrites_config_or_input(work, capsys, target):
    """-o にルールTOMLを指定すると、変換結果のCSVで上書きしていた。"""
    write(work / "r.corrections.toml", 'schema_version = 1\n[[hold]]\ncolumn = "v"\nvalue = "zz"\nreason = "r"\n')
    assert main(["lock", str(work / "in.csv"), "-c", str(work / "r.toml")]) == 0
    capsys.readouterr()
    before = {p.name: sha(p) for p in work.iterdir() if p.is_file()}
    code, out, err = run(capsys, "convert", work / "in.csv", "-c", work / "r.toml", "-o", work / target)
    assert code == 2 and ("同じ" in err or "使えません" in err)
    assert {p.name: sha(p) for p in work.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("name", ["out.html", "out.json", "out.log", "OUT.JSON"])
def test_convert_output_name_cannot_collide_with_its_own_reports(work, capsys, name):
    """-o out.json とすると、CSV と JSON レポートが同じ名前になり、CSV が消えていた。"""
    code, out, err = run(capsys, "convert", work / "in.csv", "-c", work / "r.toml", "-o", work / name)
    assert code == 2 and "使えません" in err and not (work / name).exists()


def test_convert_report_files_never_overwrite_the_input(work, capsys):
    """入力が x_fix.csv で -o x.csv のとき、修正依頼リスト（x_fix.csv）が入力を上書きする。"""
    write(work / "x_fix.csv", CSV)
    code, out, err = run(capsys, "convert", work / "x_fix.csv", "-c", work / "r.toml", "-o", work / "x.csv")
    assert code == 2 and (work / "x_fix.csv").read_text(encoding="utf-8") == CSV


@pytest.mark.parametrize("cmd", ["draft", "survey"])
@pytest.mark.parametrize("target", ["in.csv", "r.toml"])
def test_draft_and_survey_output_never_overwrite_config_or_input(work, capsys, cmd, target):
    """draft -o / survey -o に入力CSVやルールTOMLを指定すると、下書きや集計表で上書きしていた。"""
    before = (sha(work / "in.csv"), sha(work / "r.toml"))
    extra = ["--all"] if cmd == "survey" else []
    code, out, err = run(capsys, cmd, work / "in.csv", "-c", work / "r.toml", *extra, "-o", work / target)
    assert code == 2 and "同じ" in err
    assert (sha(work / "in.csv"), sha(work / "r.toml")) == before


def test_draft_and_survey_still_write_elsewhere(work, capsys):
    assert run(capsys, "draft", work / "in.csv", "-o", work / "d.toml")[0] == 0
    assert run(capsys, "survey", work / "in.csv", "--all", "-o", work / "s.csv")[0] == 0
    assert (work / "d.toml").exists() and (work / "s.csv").exists()


def test_batch_same_name_outputs_do_not_overwrite_each_other(tmp_path, capsys):
    """別フォルダの同名CSVを --out-dir つきで一括変換すると、後の結果が先の結果を上書きしていた。"""
    write(tmp_path / "a/x.csv", "k,v\nA,1cm\n")
    write(tmp_path / "b/x.csv", "k,v\nB,2cm\nC,3cm\n")
    write(tmp_path / "r.toml", RULES)
    code = main(["convert", str(tmp_path / "a/x.csv"), str(tmp_path / "b/x.csv"), "-c", str(tmp_path / "r.toml"),
                 "--out-dir", str(tmp_path / "out"), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    outs = [Path(r["files"]["output"]) for r in j["results"]]
    assert code == 0 and len(set(outs)) == 2 and outs[1].name == "x_tenugui_2.csv"
    assert [len(read_csv(o, "utf-8-sig")[1]) for o in outs] == [1, 2]
    assert Path(j["results"][1]["files"]["json"]).name == "x_tenugui_2.json"


def test_folder_input_skips_previous_outputs(tmp_path, capsys):
    """出力先を入力フォルダにして2回流すと、前回の出力（x_tenugui.csv）も入力にして x_tenugui_tenugui.csv を作っていた。"""
    write(tmp_path / "x.csv", CSV)
    write(tmp_path / "y.csv", CSV)
    write(tmp_path / "r.toml", RULES)
    write(tmp_path / "r.corrections.toml", 'schema_version = 1\n[[hold]]\ncolumn = "v"\nvalue = "7mm"\nreason = "r"\n')
    names = []
    for _ in range(3):
        code = main(["convert", str(tmp_path), "-c", str(tmp_path / "r.toml"), "--out-dir", str(tmp_path),
                     "--detail-files", "--format", "json"])
        j = json.loads(capsys.readouterr().out)
        assert code == 0 and j["totals"]["files"] == 2
        names.append(sorted(p.name for p in tmp_path.glob("*.csv")))
    assert names[0] == names[1] == names[2]
    assert len(j["skipped_outputs"]) == 8          # 2 入力 × (出力・_fix・_conv・_diff)
    # 名前が似ているだけで、隣にレポートの無い CSV は入力として扱う
    write(tmp_path / "z_tenugui.csv", CSV)
    main(["convert", str(tmp_path), "-c", str(tmp_path / "r.toml"), "--out-dir", str(tmp_path / "o2"), "--format", "json"])
    j = json.loads(capsys.readouterr().out)
    assert sorted(Path(r["input"]).name for r in j["results"]) == ["x.csv", "y.csv", "z_tenugui.csv"]


def test_folder_with_only_previous_outputs_says_so(tmp_path, capsys):
    write(tmp_path / "in/x.csv", CSV)
    write(tmp_path / "r.toml", RULES)
    assert main(["convert", str(tmp_path / "in/x.csv"), "-c", str(tmp_path / "r.toml")]) == 0
    capsys.readouterr()
    code, out, err = run(capsys, "convert", tmp_path / "in/tenugui_out", "-c", tmp_path / "r.toml")
    assert code == 2 and "前回の出力" in err


@pytest.mark.parametrize("enc, text", [("cp932", "k,v\nA,5cm\nB😀,7mm\n"), ("ascii", "k,v\nあ,5cm\n")])
def test_unencodable_output_leaves_no_partial_files(tmp_path, capsys, enc, text):
    """encoding_out で書けない文字があると、途中まで書いたCSVを残し、原因を encoding_in と案内していた。"""
    write(tmp_path / "in.csv", text)
    write(tmp_path / "r.toml", f'schema_version = 1\n[settings]\nencoding_out = "{enc}"\n[[field]]\nsrc = "v"\nto = "mm"\n')
    code, out, err = run(capsys, "convert", tmp_path / "in.csv", "-c", tmp_path / "r.toml")
    assert code == 2 and "encoding_out" in err and "encoding_in" not in err
    assert list((tmp_path / "tenugui_out").iterdir()) == []


def test_failed_run_keeps_the_previous_outputs_intact(tmp_path, capsys):
    """2回目の変換が途中で失敗したとき、1回目の出力が半分だけ新しい内容になることがない。"""
    write(tmp_path / "in.csv", "k,v\nA,5cm\n")
    write(tmp_path / "r.toml", 'schema_version = 1\n[settings]\nencoding_out = "cp932"\n[[field]]\nsrc = "v"\nto = "mm"\n')
    assert main(["convert", str(tmp_path / "in.csv"), "-c", str(tmp_path / "r.toml")]) == 0
    capsys.readouterr()
    before = {p.name: sha(p) for p in (tmp_path / "tenugui_out").iterdir()}
    write(tmp_path / "in.csv", "k,v\nA,5cm\nB😀,6cm\n")
    assert main(["convert", str(tmp_path / "in.csv"), "-c", str(tmp_path / "r.toml")]) == 2
    capsys.readouterr()
    assert {p.name: sha(p) for p in (tmp_path / "tenugui_out").iterdir()} == before


# ================================================================ 3. 周辺のファイル・指定が壊れている
@pytest.mark.parametrize("body", ["{", "[]", '{"columns": 5}', '{"header": "x"}', "", "null", '{"columns": [1]}'])
@pytest.mark.parametrize("cmd", ["check", "convert"])
def test_broken_lock_file_is_a_message_not_a_traceback(work, capsys, body, cmd):
    write(work / "r.lock.json", body)
    args = ["check", "-c", work / "r.toml", work / "in.csv"] if cmd == "check" else \
           ["convert", work / "in.csv", "-c", work / "r.toml"]
    code, out, err = run(capsys, *args)
    assert code == 2 and "lock" in err and "tenugui lock" in err
    assert not (work / "tenugui_out").exists() or not list((work / "tenugui_out").iterdir())


def test_lock_roundtrip_still_works(work, capsys):
    assert run(capsys, "lock", work / "in.csv", "-c", work / "r.toml")[0] == 0
    assert run(capsys, "check", "-c", work / "r.toml", work / "in.csv")[0] == 0
    write(work / "in.csv", "k,v,extra\nA,5cm,1\n")
    code, out, err = run(capsys, "check", "-c", work / "r.toml", work / "in.csv", "--fail-on", "warn")
    assert code == 1 and "C003" in out


@pytest.mark.parametrize("key", ["encoding_in", "encoding_out"])
def test_unknown_encoding_name_is_a_config_error(tmp_path, capsys, key):
    """文字コード名の間違いが、check を通り、convert でトレースバック（encoding_out は空の出力つき）になっていた。"""
    write(tmp_path / "in.csv", CSV)
    write(tmp_path / "r.toml", f'schema_version = 1\n[settings]\n{key} = "sjis-win"\n[[field]]\nsrc = "v"\nto = "mm"\n')
    for args in (["check", "-c", tmp_path / "r.toml"], ["convert", tmp_path / "in.csv", "-c", tmp_path / "r.toml"]):
        code, out, err = run(capsys, *args)
        assert code == 2 and key in err and "文字コード" in err
    assert not (tmp_path / "tenugui_out").exists()


def test_rules_toml_with_bom_is_read(tmp_path, capsys):
    """メモ帳で保存した BOM 付き UTF-8 の設定が「TOMLの構文エラー」になっていた。"""
    write(tmp_path / "r.toml", RULES, "utf-8-sig")
    assert run(capsys, "check", "-c", tmp_path / "r.toml")[0] == 0


@pytest.mark.parametrize("enc", ["utf-16", "cp932"])
def test_rules_toml_in_other_encoding_says_utf8(tmp_path, capsys, enc):
    write(tmp_path / "r.toml", RULES + '# 日本語のコメント\n', enc)
    code, out, err = run(capsys, "check", "-c", tmp_path / "r.toml")
    assert code == 2 and "UTF-8" in err and "encoding_in" not in err
    write(tmp_path / "ok.toml", RULES)
    write(tmp_path / "c.toml", 'schema_version = 1\n[[hold]]\ncolumn = "v"\nvalue = "あ"\nreason = "い"\n', enc)
    code, out, err = run(capsys, "check", "-c", tmp_path / "ok.toml", "--corrections", tmp_path / "c.toml")
    assert code == 2 and "UTF-8" in err


@pytest.mark.parametrize("repl", [r"\1", r"\g<x>", r"\9", "\\"])
def test_bad_sub_replacement_is_caught_at_load(repl):
    """sub の置換後に、無いグループ（\\1）を書くと、変換の途中でトレースバックになっていた。"""
    with pytest.raises(ConfigError, match="sub"):
        cfg(field=[{"src": "v", "sub": [["a", repl]]}])
    with pytest.raises(ConfigError, match="sub"):
        cfg(field=[{"src": "v", "match": "(?P<a>.+)", "parts": {"a": {"out": "o", "sub": [["a", repl]]}}}])
    assert one({"src": "v", "sub": [["(a)(b)", r"\2\1"]]}, "abc")[0]["v"] == "bac"


@pytest.mark.parametrize("pat", ["", ".*", "x?", "^$", "(NG)?"])
def test_reserved_pattern_matching_everything_is_rejected(pat):
    """reserved が空の文字列に一致すると、すべての値が抽出NG印扱いになり、黙って何も変換されなかった。"""
    with pytest.raises(ConfigError, match="reserved"):
        cfg(settings={"reserved": [pat]}, field=[{"src": "v", "to": "mm"}])
    assert one({"src": "v", "to": "mm"}, "5cm", settings={"reserved": [r"^NG\d*$", "^未抽出$"]})[0]["v"] == "50"


def test_derive_after_itself_is_rejected():
    """after に自分の列名を書くと、その派生列が出力から黙って消えていた。"""
    with pytest.raises(ConfigError, match="after"):
        cfg(derive=[{"out": "d", "expr": "a*2", "vars": {"a": "v"}, "after": "d"}])
    c = cfg(derive=[{"out": "d", "expr": "a*2", "vars": {"a": "v"}, "after": "v"}])
    assert plan_columns(c, ["k", "v", "w"]) == ["k", "v", "d", "w"]


def test_correction_cannot_produce_an_ng_mark():
    """訂正後の値に NG1 を書くと、chozu が読めなかった印と区別のつかない値を作れた。"""
    with pytest.raises(ConfigError, match="NG"):
        config_from_dict({"schema_version": 1}, {"schema_version": 1, "correct": [
            {"column": "v", "value": "35nm", "to": "NG1", "reason": "r"}]})


# ================================================================ 4. 自分で作ったものを自分で読める
NASTY = ['"', "\\", "\n", "\r", "\t", "'", "#", "=", "{", "}", "[", "]", ",", " ", "　", "\x01", "\x7f", "​",
         " ", "é", "①", "㎜", "±", "～", "😀", "$", "%", "\\n", '"""', "'''", "\\u0041", "NG1"]
BASE_TOML = '''schema_version = 1
[synonyms.k]
"カーボン" = ["carbon"]
[[field]]
src = "a"
to = "mm"
[[field]]
src = "b"
preset = "tolerance"
parts = { nom = { out = "b_n", to = "mm" }, tol = "b_t" }
[[field]]
src = "c"
synonyms = "k"
[[field]]
src = "d"
number_only = true
strip_units = ["*"]
[[derive]]
out = "z"
expr = "x*2"
vars = { x = "a" }
'''
VALS = ["330±15mm", "5cm", "100V", "0.1kV", "カーボン", "carbon", "NG1", "", "約5mm", "1,200V", "5~10A", "−5", "5①"]


def _nasty(rng: random.Random, n: int = 4) -> str:
    return "".join(rng.choice(NASTY + list("abc015mV")) for _ in range(rng.randint(1, n)))


@pytest.mark.parametrize("seed", range(8))
def test_as_test_block_roundtrips(seed):
    """try --as-test の出力は、どんな値でも、設定に貼ればそのまま通る（DEL 文字で TOML として読めなくなっていた）。"""
    rng = random.Random(seed)
    base = config_from_dict(tomllib.loads(BASE_TOML))
    for _ in range(120):
        val = lambda: rng.choice(VALS) + (_nasty(rng) if rng.random() < 0.5 else "")
        values = {"k": _nasty(rng), "a": val(), "b": val(), "c": val(), "d": val()}
        block = as_test_block(values, try_row(base, values), _nasty(rng))
        res = run_tests(config_from_dict(tomllib.loads(BASE_TOML + block)))
        assert res["total"] == 1 and res["ok"], (values, res["failures"])


@pytest.mark.parametrize("seed", range(8))
def test_draft_is_always_loadable_and_csv_roundtrips(seed, tmp_path):
    """draft の下書きは、列名や値に何が入っていても、設定として読める（コメントに改行や制御文字が入り壊れていた）。
    あわせて、書いたCSVを読み直すと同じ内容になり、自分の出力を verify すると必ず OK になる。"""
    rng = random.Random(100 + seed)
    base = config_from_dict(tomllib.loads(BASE_TOML))
    empty = config_from_dict({"schema_version": 1})
    for n in range(25):
        val = lambda: rng.choice(VALS) + (_nasty(rng) if rng.random() < 0.5 else "")
        hdr = uniquify(["k", "a", "b", "c", "d", _nasty(rng), _nasty(rng)])[0]
        rows = [[_nasty(rng), val(), val(), val(), val(), _nasty(rng), ""] for _ in range(6)]
        text = draft_toml(empty, hdr, rows * 3)
        c = config_from_dict(tomllib.loads(text))
        assert check_header(c, hdr) == []
        src, dst = tmp_path / f"in{n}.csv", tmp_path / f"out{n}.csv"
        write_csv(src, hdr, rows, "utf-8-sig")
        assert read_csv(src, "utf-8-sig") == (hdr, rows)
        cols, out_rows, _ = convert_rows(base, hdr, rows)
        write_csv(dst, cols, out_rows, "utf-8-sig")
        v = verify_files(base, hdr, rows, dst)
        assert v["ok"], v["violations"][:2]


def test_no_command_tracebacks_on_awkward_arguments(work, capsys):
    """周辺コマンドの、変な引数の総当たり。終了コードは 0 / 1 / 2 のどれかで、例外は外へ出ない。"""
    w = work
    cases = [
        ["survey", w / "in.csv", "-k", "v", "--top", "-1", "--format", "json"],
        ["suggest", w / "in.csv", "-c", w / "r.toml", "--max-groups", "-5", "--format", "json"],
        ["convert", w / "in.csv", "-c", w / "r.toml", "--max-issues", "-5", "--format", "json"],
        ["verify", w / "in.csv", w / "nope.csv", "-c", w / "r.toml"],
        ["verify", w / "in.csv", w / "in.csv", "-c", w / "r.toml"],
        ["compare", w / "in.csv", w / "r.toml", "--format", "json"],
        ["impact", w / "in.csv", "-c", w / "r.toml", "--base", w / "zz.toml", "--format", "json"],
        ["try", "-c", w / "r.toml", "--set", "=5", "--format", "json"],
        ["try", "-c", w / "r.toml", "--field", "v", '5"\\\n\tcm\x01\x7f', "--as-test", "--name", 'a"b\nc'],
        ["convert", w, "-c", w],
        ["convert", w / "r.toml", "-c", w / "r.toml", "--format", "json"],
        ["convert", w / "in.csv", "-c", w / "r.toml", "--out-dir", w / "r.toml", "--format", "json"],
        ["check", "-c", w / "r.toml", w],
        ["check", "-c", w / "r.toml", "--corrections", w],
        ["glossary", "-c", w / "r.toml", w / "in.csv", "--format", "csv"],
        ["lock", w / "nope.csv", "-c", w / "r.toml"],
    ]
    for args in cases:
        code = main([str(a) for a in args])
        cap = capsys.readouterr()
        assert code in (0, 1, 2), args
        assert "Traceback" not in cap.out + cap.err, args
    assert (w / "r.toml").read_text(encoding="utf-8") == RULES and (w / "in.csv").read_text(encoding="utf-8") == CSV

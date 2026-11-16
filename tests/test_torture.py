"""いじわる試験: 壊れた入力・変な値・間違った設定を与えても、
落ちない / 黙ってデータを捨てない / 変な値を黙って通さない ことを確かめる。

ここにあるのは、実際に見つかって直した不具合の再発防止と、乱数による総当たり。
"""
from __future__ import annotations

import csv
import datetime
import json
import random
import signal
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tenugui.analysis import (case_coverage, diff_changes, group_issues, idempotency_report, lint, run_tests,
                              shape_table, suggest, try_row, verify_before_after)
from tenugui.cli import main
from tenugui.core import ConfigError, compile_pattern, config_from_dict, convert_rows, read_csv, uniquify
from tenugui.corrections import Provenance
from tenugui.units import normalize_text

MM = {"number_only": True, "strip_units": ["mm"]}


def cfg(**kw):
    return config_from_dict({"schema_version": 1, **kw})


def conv(field, values, **kw):
    c = cfg(field=[field] if isinstance(field, dict) else field, **kw)
    cols, rows, issues = convert_rows(c, ["k", "a"], [[f"r{i}", v] for i, v in enumerate(values, start=1)])
    return [r[1] for r in rows], [(i.row, i.code) for i in issues]


def write(path: Path, text: str, enc: str = "utf-8-sig") -> Path:
    path.write_text(text, encoding=enc, newline="")
    return path


@pytest.fixture
def rules(tmp_path):
    return write(tmp_path / "r.toml", 'schema_version = 1\n[[field]]\nsrc = "a"\nnumber_only = true\n'
                                      'strip_units = ["mm"]\n', "utf-8")


def run_json(capsys, *args) -> tuple[int, dict]:
    code = main([str(a) for a in args] + ["--format", "json"])
    return code, json.loads(capsys.readouterr().out)


# ================================================================ CSV: 行を捨てない
def test_rows_starting_with_hash_are_data_not_comments(tmp_path, rules, capsys):
    """シート名が # で始まる行を、コメントとして黙って捨てていた。"""
    p = write(tmp_path / "in.csv", "k,a\n#004,7mm\n#,8mm\nr3,9mm\n")
    code, j = run_json(capsys, "convert", p, "-c", rules)
    assert code == 0 and j["rows"] == 3
    assert Path(j["files"]["output"]).read_text(encoding="utf-8-sig") == "k,a\n#004,7\n#,8\nr3,9\n"


def test_only_leading_comment_lines_are_skipped(tmp_path):
    p = write(tmp_path / "in.csv", "# chozu: /x.toml (更新 2026-01-01 / sha256:abc), with, commas\n# 2行目\nk,a\n# data,1\n")
    assert read_csv(p, "utf-8-sig") == (["k", "a"], [["# data", "1"]])
    p = write(tmp_path / "h.csv", "#,a\n1,7mm\n")                       # 「#」という名前の列はヘッダ
    assert read_csv(p, "utf-8-sig") == (["#", "a"], [["1", "7mm"]])


def test_empty_cell_rows_are_kept_and_blank_lines_skipped(tmp_path, rules, capsys):
    p = write(tmp_path / "in.csv", "k,a\n\nr1,7mm\n,\n\n\nr3,8mm\n")
    code, j = run_json(capsys, "convert", p, "-c", rules)
    assert j["rows"] == 3 and Path(j["files"]["output"]).read_text(encoding="utf-8-sig") == "k,a\nr1,7\n,\nr3,8\n"


def test_row_wider_than_header_stops_instead_of_dropping_cells(tmp_path, rules, capsys):
    p = write(tmp_path / "in.csv", "k,a\nr1,7mm,はみ出し,もう1つ\nr2,8mm\n")
    assert main(["convert", str(p), "-c", str(rules)]) == 2
    assert "ヘッダ（2列）より列の多い行" in capsys.readouterr().err
    p = write(tmp_path / "ok.csv", "k,a\nr1,7mm,,\nr2\n")               # 余分が空だけなら通す。短い行は空で埋める
    code, j = run_json(capsys, "convert", p, "-c", rules)
    assert code == 0 and Path(j["files"]["output"]).read_text(encoding="utf-8-sig") == "k,a\nr1,7\nr2,\n"


def test_duplicate_header_names_never_collide():
    """a, a, a#2 という列があると、2つ目の a に a#2 を付けて既存の a#2 と重なり、値が上書きされていた。"""
    assert uniquify(["k", "a", "a", "a#2"])[0] == ["k", "a", "a#3", "a#2"]
    assert uniquify(["a#2", "a", "a", "a"])[0] == ["a#2", "a", "a#3", "a#4"]
    h = ["x"] * 5 + ["x#3", "", ""]
    assert len(set(uniquify(h)[0])) == len(h)
    cols, rows, _ = convert_rows(cfg(field=[{"src": "a", **MM}]), ["k", "a", "a", "a#2"], [["r", "7mm", "8mm", "9mm"]])
    assert cols == ["k", "a", "a#3", "a#2"] and rows == [["r", "7", "8mm", "9mm"]]


def test_oversized_cell_does_not_crash(tmp_path, rules, capsys):
    p = write(tmp_path / "in.csv", "k,a\nr1," + "7" * 200_000 + "mm\nr2,8mm\n")
    code, j = run_json(capsys, "convert", p, "-c", rules)
    assert code == 0 and j["rows"] == 2 and [(i["row"], i["code"]) for i in j["issues"]] == [(1, "C101")]
    assert len(j["issues"][0]["message"]) < 200


@pytest.mark.parametrize("text,enc,msg", [
    ("k\ta\nr1\t7mm\n", "utf-8-sig", "タブ区切り"), ("k;a\nr1;7mm\n", "utf-8-sig", "セミコロン区切り"),
    ("k,a\nあ,7mm\n", "cp932", "文字コード"), ("k,a\nr1,7mm\n", "utf-16", "文字コード"), ("", "utf-8", "空のCSV")])
def test_wrong_file_kinds_get_a_plain_message(tmp_path, rules, capsys, text, enc, msg):
    p = write(tmp_path / "in.csv", text, enc)
    assert main(["convert", str(p), "-c", str(rules)]) == 2
    assert msg in capsys.readouterr().err


def test_header_variants(tmp_path, rules, capsys):
    p = write(tmp_path / "bom.csv", "﻿﻿k,a\nr1,7mm\n", "utf-8")       # BOM が二重
    code, j = run_json(capsys, "convert", p, "-c", rules)
    assert code == 0 and Path(j["files"]["output"]).read_text(encoding="utf-8-sig").startswith("k,a\n")
    for hdr, shown in (("k, a ", "' a '"), ("k,ａ", "'ａ'"), ("k,A", "'A'")):
        p = write(tmp_path / "h.csv", hdr + "\nr1,7mm\n")
        assert main(["convert", str(p), "-c", str(rules)]) == 2
        assert f"入力の列名は {shown} です" in capsys.readouterr().err


def test_nasty_but_valid_csv_roundtrips(tmp_path, rules, capsys):
    rows = [["k", "a", "free"], ['q"uote', "7mm", 'a "b" c'], ["new\nline", "８ｍｍ", "x\r\ny"], ["com,ma", "9mm", ",,,"],
            ["=1+1", "7mm", "@SUM(A1)"], ["🙂", "7mm", "\t tab "], ["", "7mm", ""]]
    p = tmp_path / "in.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    code, j = run_json(capsys, "convert", p, "-c", rules)
    with open(j["files"]["output"], encoding="utf-8-sig", newline="") as f:
        out = list(csv.reader(f))
    assert code == 0 and [r[0] for r in out] == [r[0] for r in rows] and [r[2] for r in out] == [r[2] for r in rows]
    assert [r[1] for r in out[1:]] == ["7", "8", "9", "7", "7", "7"]


def test_output_never_overwrites_input_or_writes_into_a_file(tmp_path, rules, capsys):
    p = write(tmp_path / "in.csv", "k,a\nr1,7mm\n")
    before = p.read_bytes()
    assert main(["convert", str(p), "-c", str(rules), "-o", str(p)]) == 2
    assert "出力先が入力ファイルと同じ" in capsys.readouterr().err and p.read_bytes() == before
    write(tmp_path / "afile", "x", "utf-8")
    assert main(["convert", str(p), "-c", str(rules), "--out-dir", str(tmp_path / "afile")]) == 2
    assert "ファイルを開けません" in capsys.readouterr().err


def test_cli_bad_arguments_give_messages_not_tracebacks(tmp_path, rules, capsys):
    p = write(tmp_path / "in.csv", "k,a\nr1,7mm\n")
    for args, msg in ((["survey", str(p), "-k", "zzz"], "入力に列 'zzz' がありません"),
                      (["convert", str(p), "-c", str(p)], "TOMLの構文エラー"),
                      (["convert", str(p), "-c", str(tmp_path)], "ファイルを開けません"),
                      (["convert", str(tmp_path / "none.csv"), "-c", str(rules)], "ファイルを開けません"),
                      (["convert", "/bin/sh", "-c", str(rules)], "")):
        assert main(args) == 2, args
        assert msg in capsys.readouterr().err


# ================================================================ 数値の読み取り
@pytest.mark.parametrize("raw,out", [
    ("7mm", "7"), ("７ｍｍ", "7"), ("7 mm", "7"), ("7　mm", "7"), ("7\tmm", "7"), ("7\nmm", "7"), ("7 mm", "7"),
    ("7​mm", "7"), ("﻿7mm", "7"), ("7m‍m", "7"),          # 見えない文字（ゼロ幅スペースなど）
    ("-5mm", "-5"), ("−5mm", "-5"), ("－5mm", "-5"), ("+5mm", "5"), (".5mm", "0.5"), ("5.mm", "5"),
    ("1,234.5mm", "1234.5"), ("12,345,678mm", "12345678"), ("1e3mm", "1000"), ("1E-3mm", "0.001"), ("㎜", None)])
def test_number_forms_accepted(raw, out):
    got, issues = conv({"src": "a", **MM}, [raw])
    assert (got[0], issues) == ((out, []) if out is not None else (raw, [(1, "C101")]))


@pytest.mark.parametrize("raw", [
    "5..5mm", "1,23mm", "0x10mm", "1_000mm", "五mm", "5 m m", "5mm5", "mm5", "5mmmm", "5MM", "5Mm", "NaNmm", "infmm",
    "Infinity", "nan", "1/2mm", "7±2mm", "7mm以下", "約7mm", "7mm(参考)", "７ｍM", "1e400mm", "1e-400mm", "9" * 60 + "mm",
    "0." + "0" * 60 + "1mm", "+²E1250³mm", "1.2.3", "--5mm", "5-mm", "=7mm", "'7mm"])
def test_number_forms_rejected_not_guessed(raw):
    """読めない値は、それらしい数にせず、元のままエラーにする。"""
    got, issues = conv({"src": "a", **MM}, [raw])
    assert got == [raw] and [c for _, c in issues] and issues[0][1] in ("C101", "C104")


@pytest.mark.parametrize("raw,to,out", [
    ("5km", "mm", "5000000"), ("0.1cm", "mm", "1"), ("5μm", "mm", "0.005"), ("5µm", "mm", "0.005"), ("-0mm", "mm", "0"),
    ("1.25kW", "W", "1250"), ("25℃", "K", "298.15"), ("0.5h", "min", "30"), ("9999999999999999999999m", "mm", "9999999999999999999999000")])
def test_conversions(raw, to, out):
    assert conv({"src": "a", "to": to}, [raw]) == ([out], [])


@pytest.mark.parametrize("raw", ["5Km", "5KM", "1e400mm", "+²E1250³", "9" * 40 + "mm", "1" + "0" * 50 + "mm"])
def test_conversion_of_absurd_numbers_is_an_error_not_a_crash(raw):
    """+²E1250³ は全角半角の統一で 2E12503 になり、丸めの計算で落ちていた。"""
    got, issues = conv({"src": "a", "to": "mm", "default_unit": "mm", "decimals": 2}, [raw])
    assert got == [raw] and issues[0][1] in ("C101", "C103")


# ================================================================ 止まらない（長い値・悪い正規表現）
def _limit(seconds: int):
    def stop(*_):
        raise TimeoutError(f"{seconds}秒で終わらない")
    signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)


LONG = ["1" + " " * 20000 + "±" + " " * 20000 + "x", "1 " * 5000 + "!", "1." * 3000, "1±" * 3000, "(" * 5000,
        "1,000," * 2000 + "x", "1" + "mm" * 5000, "±" * 10000, "1e" * 3000, "-" * 10000 + "1", " " * 50000, "\n" * 9999 + "7mm"]
RULES_FOR_LONG = [
    {"src": "a", "preset": "tolerance", "parts": {"nom": {"out": "o", "to": "mm"}}},
    {"src": "a", "preset": "tolerance_range", "parts": {"nom": {"out": "o", "to": "W"}}},
    {"src": "a", "preset": "range", "parts": {"lo": {"out": "o", "to": "A"}}},
    {"src": "a", "pattern": "#[±#][±#][±#][±#]mm", "parts": {"n1": "o"}},
    {"src": "a", "case": [{"pattern": "#±#mm", "parts": {"n1": "o"}}, {"preset": "tolerance", "parts": {"nom": "o"}}]},
    {"src": "a", **MM}, {"src": "a", "to": "mm", "default_unit": "mm"}]


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="時間制限に SIGALRM を使う")
@pytest.mark.parametrize("n", range(len(RULES_FOR_LONG)))
def test_long_values_finish_quickly(n):
    """空白が2万個続く値で、preset と単位の照合が数十秒止まっていた。"""
    _limit(10)
    try:
        t = time.time()
        cols, rows, issues = convert_rows(cfg(field=[RULES_FOR_LONG[n]]), ["k", "a"], [["r", v] for v in LONG])
        assert time.time() - t < 5
    finally:
        signal.alarm(0)
    assert rows[-1][-1] in ("7", "")     # 改行だらけの後ろの 7mm: 前後の空白を除いて読む（単位が合わない規則では空）
    assert len(rows) == len(LONG) and len({i.row for i in issues}) >= len(LONG) - 2
    assert all(len(i.message) < 300 for i in issues)


def test_nested_repeat_in_user_regex_is_flagged():
    """標準の正規表現には時間制限が無いので、止まる形の正規表現は書いた時点で警告する。"""
    for rx in (r"^(a+)+$", r"(.*)*x", r"(\d+)*z", r"(?P<v>\d+)(?P<w>\d*)*z", r"(\w+\s?)+$"):
        f = {"src": "a", "extract": rx} if "?P<" not in rx else {"src": "a", "match": rx, "parts": {"v": "o"}}
        assert [w["code"] for w in lint(cfg(field=[f]))] == ["L401"], rx
    for rx in (r"(\d+)", r"^(\d+)\s*mm$", r"(a|b)+", r"(\d+(?:\.\d+)?)", r"\((\d+)\)"):
        assert not lint(cfg(field=[{"src": "a", "extract": rx}])), rx


# ================================================================ pattern
@pytest.mark.parametrize("pat,msg", [
    ("##", "隣り合って"), ("{a}{b}", "隣り合って"), ("#" * 50, "隣り合って"), ("#[]", "中が空"), ("[]#", "中が空"),
    ("#mm" * 200, "長すぎ"), ("{}", "名前"), ("{1}", "名前"), ("{a b}", "名前"), ("#{", "閉じて"), ("[#", "閉じて"),
    ("#]", "対応"), ("mm", "数値"), ("", "数値")])
def test_pattern_rejects_ambiguous_or_broken(pat, msg):
    with pytest.raises(ValueError) as e:
        compile_pattern(pat)
    assert msg in str(e.value)


def test_pattern_does_not_split_one_number_into_two():
    """「# #」が 12 を 1 と 2 に分けて一致していた。"""
    c = cfg(field=[{"src": "a", "pattern": "# #", "parts": {"n1": "x", "n2": "y"}}])
    cols, rows, issues = convert_rows(c, ["k", "a"], [["r", "12"], ["r", "1 2"], ["r", "1  2"], ["r", "1.5 2.5"]])
    assert [r[2:] for r in rows] == [["", ""], ["1", "2"], ["1", "2"], ["1.5", "2.5"]]
    assert [(i.row, i.code) for i in issues] == [(1, "C111")]


@pytest.mark.parametrize("pat", ["#(", "#)", "#\\", "#*", "#+", "#?", "#|#", "#^", "#$", "^#$", "#.*", "#.#", "}#", "#-#", "#e#"])
def test_pattern_treats_regex_characters_as_plain_text(pat):
    import re
    rx = re.compile(compile_pattern(pat)[0])
    assert not rx.search("12") and not rx.search("1.5x") and not rx.search("aaaa")


@pytest.mark.parametrize("pat,value", [("#％", "5%"), ("＃ｍｍ", "7㎜"), ("#㎜", "7mm"), ("#℃", "25°C"), ("# °C", "25℃"),
                                       ("#µm", "3μm"), ("#～#mm", "3〜5mm"), ("φ#", "Φ5"), ("#Ω", "5Ω")])
def test_pattern_width_and_symbol_variants(pat, value):
    import re
    assert re.search(compile_pattern(pat)[0], normalize_text(value)) or pat == "φ#"


# ================================================================ 式
@pytest.mark.parametrize("expr,msg", [
    ("x*0+min()", "引数の数"), ("abs(x, x)", "引数の数"), ("sqrt()", "引数の数"), ("round(x, 1, 2)", "引数の数"),
    ("x+abs", "関数です"), ("__import__('os')", "使えない"), ("x.__class__", "使えない"), ("(lambda: 1)()", "使えない"),
    ("[x for x in (1,)]", "使えない"), ("x if x else 1", "使えない"), ("x%2", "使えない"), ("x//2", "使えない"),
    ("x+True", "数値以外"), ("x+'a'", "数値以外"), ("x;x", "構文"), ("", "構文"), ("x x", "構文"), ("x+１", "構文"),
    ("x" + "+x" * 5000, "長すぎ"), ("(" * 500 + "x" + ")" * 500, ""), ("x == 1", "使えない"), ("x and 1", "使えない"),
    ("max(*[x])", ""), ("round(x, ndigits=1)", "使えない")])
def test_expression_rejected_at_load(expr, msg):
    with pytest.raises(ConfigError) as e:
        cfg(derive=[{"out": "d", "expr": expr, "vars": {"x": "a"}}])
    assert msg in str(e.value)


@pytest.mark.parametrize("expr", [
    "x*0+9**9**9", "x+10**100000", "x**1000000", "x**x**x**x", "x*0+1/0", "x/(x-7)", "sqrt(x-8)", "x+1e999", "x*0+0**-1",
    "x**0.5", "round(x, 10**9)", "x*0+pi**pi**pi**pi", "round(x, 99)"])
def test_expression_that_cannot_be_computed_reports_c303(expr):
    """0 の -1 乗や 1e999 が Infinity として、そのまま出力されていた。"""
    c = cfg(derive=[{"out": "d", "expr": expr, "vars": {"x": "a"}}])
    cols, rows, issues = convert_rows(c, ["k", "a"], [["r", "7"]])
    assert rows[0][-1] == "" and [i.code for i in issues] == ["C303"]


@pytest.mark.parametrize("expr,out", [("sqrt(x)*sqrt(x)", "7"), ("round(x/3, 2)", "2.33"), ("round(x, -1)", "10"),
                                      ("-(-(-x))", "-7"), ("x**-1*7", "1"), ("max(x, 3)+min(x, 3)", "10"), ("ｘ+1", "8"),
                                      ("x*pi/pi", "7"), ("2**10+x", "1031"), ("abs(-x)", "7")])
def test_expression_values(expr, out):
    c = cfg(derive=[{"out": "d", "expr": expr, "vars": {"x": "a"}, "decimals": 6}])
    assert convert_rows(c, ["k", "a"], [["r", "7"]])[1][0][-1] == out


# ================================================================ 設定: 型を間違えても落ちない
BAD = [0, -1, 1.5, True, "", " ", "x", "{", "(", "\\", [], [1], ["x"], [[]], [["a"]], [["a", "b", "c"]], {}, {"a": 1},
       {"out": 3}, datetime.date(2026, 1, 1), 10 ** 30, float("inf"), float("nan"), "a" * 3000, {"x": {"y": {"z": []}}}]
H3 = ["k", "a", "b"]
R3 = [["r1", "7mm", "330±15mm"], ["r2", "", "NG"], ["r3", "x", "5"]]
VALUE_KEYS = ["out", "required", "sub", "synonyms", "on_unknown", "number_only", "strip_units", "to", "default_unit",
              "unit_out", "unit_label", "decimals", "trim"]


def _survives(d: dict) -> None:
    """設定として読めるなら変換まで動く。読めないなら ConfigError（それ以外の例外は出ない）。"""
    try:
        c = config_from_dict(d)
    except ConfigError as e:
        assert e.problems and all(isinstance(x, str) and x for x in e.problems)
        return
    convert_rows(c, H3, R3)
    run_tests(c)
    suggest(c, H3, R3)
    for fr in c.fields[:2]:
        try_row(c, {fr.src: "7mm"})


def _wrong_type_configs():
    split = {"src": "b", "preset": "tolerance", "parts": {"nom": "o"}}
    for b in BAD:
        for k in VALUE_KEYS + ["src", "extract", "drop_src", "for", "zzz"]:
            yield {"field": [{"src": "a", k: b}]}
            yield {"field": [{"src": "a", "to": "mm", k: b}]}
            yield {"field": [{**split, "parts": {"nom": {"out": "o", k: b}}}]}
            yield {"field": [{**split, "each": {k: b}}]}
        for k in ["preset", "pattern", "match", "parts", "each", "case", "for", "src", "drop_src"]:
            yield {"field": [{**split, k: b}]}
            yield {"field": [{"src": "b", "case": [{"pattern": "#mm", "parts": {"n1": "o"}, k: b}]}]}
        yield {"field": [{**split, "parts": {"nom": b}}]}
        yield {"field": [{**split, "parts": {"nom": {"out": "o", "unit_group": b}}}]}
        for k in ["out", "expr", "vars", "unit", "to", "unit_out", "decimals", "trim", "after"]:
            yield {"derive": [{"out": "d", "expr": "x*2", "vars": {"x": "a"}, k: b}]}
        for k in ["name", "in", "out", "codes"]:
            yield {"field": [{"src": "a", "to": "mm"}], "test": [{"in": {"a": "7mm"}, "out": {"a": "7"}, k: b}]}
        for k in ["encoding_in", "encoding_out", "passthrough", "keep", "reserved", "on_error", "rare_shape", "null_values"]:
            yield {"settings": {k: b}, "field": [{"src": "a", "to": "mm"}]}
        for top in ["schema_version", "settings", "units", "unit_alias", "synonyms", "field", "derive", "test"]:
            yield {top: b}
        yield {"units": {"length": b}}
        yield {"units": {"zz": {"base": "q", "w": b}}}
        yield {"units": {"zz": {"base": b}}}
        yield {"unit_alias": {"q": b}}
        yield {"synonyms": {"s": b}, "field": [{"src": "a", "synonyms": "s"}]}
        yield {"synonyms": {"s": {"A": b}}, "field": [{"src": "a", "synonyms": "s"}]}
        yield {"field": [b]}
        yield {"derive": [b]}
        yield {"test": [b]}


def test_wrong_typed_config_values_never_crash():
    """設定の値の型を間違えると（to = 0、sub = 1 など）200通り以上の落ち方をしていた。"""
    n = 0
    for d in _wrong_type_configs():
        _survives({"schema_version": 1, **d} if "schema_version" not in d else d)
        n += 1
    assert n > 3000


@pytest.mark.parametrize("field,msg", [
    ({"src": "a", "to": 0}, "to は 空でない文字列"), ({"src": "a", "sub": 1}, "sub は [["),
    ({"src": "a", "to": "mm", "decimals": 1.5}, "decimals は -20〜50 の整数"),
    ({"src": "a", "to": "mm", "decimals": 10 ** 30}, "decimals は"), ({"src": "a", "synonyms": 1}, "synonyms は 文字列"),
    ({"src": "a", "extract": []}, "extract は 文字列"), ({"src": "a", "out": {}}, "out は 空でない文字列"),
    ({"src": "a", "number_only": "true", "strip_units": ["mm"]}, "number_only は true / false"),
    ({"src": "a", "number_only": True, "strip_units": "mm"}, "strip_units は 文字列の配列")])
def test_wrong_type_messages_name_the_key_and_the_expected_form(field, msg):
    with pytest.raises(ConfigError) as e:
        cfg(field=[field])
    assert msg in str(e.value) and "今は" in str(e.value)


def test_wrong_typed_corrections_never_crash():
    base = {"column": "a", "value": "7mm", "to": "8mm", "reason": "r"}
    rule = {"schema_version": 1, "field": [{"src": "a", "to": "mm"}]}
    n = 0
    for b in BAD:
        cands = [{"schema_version": 1, "correct": b}, {"schema_version": 1, "hold": b}, {"schema_version": b},
                 {"schema_version": 1, "correct": [b]}, {"schema_version": 1, "hold": [b]}]
        for k in ["column", "value", "to", "reason", "ref", "key", "zz"]:
            cands.append({"schema_version": 1, "correct": [{**base, k: b}]})
            cands.append({"schema_version": 1, "hold": [{"column": "a", "value": "x", "reason": "r", k: b}]})
        for d in cands:
            n += 1
            try:
                c = config_from_dict(rule, corrections=d)
            except ConfigError:
                continue
            convert_rows(c, ["k", "a"], [["r", "7mm"], ["r2", "x"]])
    assert n > 400


# ================================================================ レポート: 値に何が入っていても壊れない
EVIL = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>', "</td></tr></table><h1>x", "'onmouseover='alert(1)",
        "&lt;b&gt;", "</style><script>1</script>", "</script>", " ", "{{7*7}}", "${x}", "%s%n", "\\", "‮evil",
        "a\rb", "a\tb", "]]>", "<!--", "--><svg onload=1>", "=cmd|' /C calc'!A0", "a\x00b"]


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.stack, self.bad = set(), set(), [], []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attrs |= {a for a, _ in attrs}
        if tag not in ("meta", "br", "input"):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.bad.append(tag)


def test_reports_survive_hostile_values_and_column_names(tmp_path, capsys):
    hdr = ["k", "a<b>", 'x"y', "n"]
    rows = [[e, e, e, "7mm"] for e in EVIL] + [["ok", "5mm", "5mm", "7cm"]]
    with open(tmp_path / "in.csv", "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([hdr] + rows)
    write(tmp_path / "r.toml", '''schema_version = 1
[synonyms.s]
"<b>正</b>" = ["<script>alert(1)</script>"]
[[field]]
src = "a<b>"
number_only = true
strip_units = ["mm"]
[[field]]
src = 'x"y'
synonyms = "s"
[[field]]
src = "n"
out = "o<i>"
number_only = true
strip_units = ["mm"]
''', "utf-8")
    write(tmp_path / "r.corrections.toml", '''schema_version = 1
[[correct]]
column = "a<b>"
value = "&lt;b&gt;"
to = "9mm"
reason = "<script>alert('r')</script>"
ref = "</td><script>1</script>"
[[hold]]
column = "a<b>"
value = "{{7*7}}"
reason = "<img src=x onerror=1>"
''', "utf-8")
    code, j = run_json(capsys, "convert", tmp_path / "in.csv", "-c", tmp_path / "r.toml", "--detail-files")
    assert code == 0
    html = Path(j["files"]["html"]).read_text(encoding="utf-8")
    t = _Tags()
    t.feed(html)
    assert t.tags <= {"html", "head", "meta", "title", "style", "body", "h1", "h2", "div", "p", "span", "b", "code", "br",
                      "table", "thead", "tbody", "tr", "th", "td", "label", "input", "script", "ul", "li", "details", "summary"}
    assert not {a for a in t.attrs if a.startswith("on")} and not t.stack and not t.bad
    assert html.count("<script") == 1 and "<img" not in html and "<svg" not in html and html.count("<h1") == 1
    rep = json.loads(Path(j["files"]["json"]).read_text(encoding="utf-8"))
    assert rep["rows"] == len(rows) and rep["provenance"]["counts"]
    with open(j["files"]["output"], encoding="utf-8-sig", newline="") as f:
        out = list(csv.reader(f))
    assert out[0] == ["k", "a<b>", 'x"y', "n", "o<i>"] and [r[0] for r in out[1:]] == [r[0] for r in rows]
    for k in ("report", "diff", "fix"):
        with open(j["files"][k], encoding="utf-8-sig", newline="") as f:
            assert list(csv.reader(f))


# ================================================================ 乱数による総当たり
TOK = ["0", "1", "7", "15", "330", "1250", "0.2", "1,200", ".5", "5.", "-", "+", "±", "＋", "－", "~", "～", "〜", "/", "(",
       ")", "（", "）", "φ", "mm", "ｍｍ", "㎜", "cm", "m", "W", "kW", "A", "V", "℃", "°C", "%", "分", "h", "min", " ", "　",
       "\n", "\t", "​", ",", ".", "e", "E", "x", "約", "以下", "NG", "NG1", "ng", "なし", "―", "標準", "標準型", "特殊",
       "#", "=", "\"", "'", "<", ">", "&", "\\", "{", "}", "[", "]", "²", "³", "μ", "Ω", "１", "２", "∞", "NaN", ""]
FUZZ = {"schema_version": 1, "settings": {"null_values": ["-", "なし"]},
        "synonyms": {"s": {"標準": ["標準型"], "特殊": ["特殊型"]}},
        "field": [
            {"src": "a", **MM},
            {"src": "b", "to": "mm", "default_unit": "mm", "decimals": 2},
            {"src": "c", "preset": "tolerance", "drop_src": True, "each": {"to": "mm", "default_unit": "mm"},
             "parts": {"nom": "c_nom", "tol": "c_tol", "hi": "c_hi", "lo": "c_lo"}},
            {"src": "d", "each": {"to": "W"}, "case": [
                {"preset": "tolerance_range", "parts": {"nom": "d_w", "tol": "d_tol", "rlo": {"out": "d_lo", "to": "A"},
                                                         "rhi": {"out": "d_hi", "to": "A"}}},
                {"pattern": "{w}W+{p}-{m}[({lo}~{hi}A)]", "parts": {"w": "d_w", "p": "d_p", "m": "d_m",
                                                                    "lo": {"out": "d_lo", "to": "A"}, "hi": {"out": "d_hi", "to": "A"}}},
                {"match": r"^(?P<t>標準|特殊)$", "parts": {"t": "d_kind"}}]},
            {"src": "e", "synonyms": "s"},
            {"src": "f", "preset": "range", "parts": {"lo": {"out": "f_lo", "to": "A"}, "hi": {"out": "f_hi", "to": "A"}}},
            {"src": "g", "extract": r"(\d+(?:\.\d+)?)", "to": "℃", "default_unit": "℃", "out": "g_c"},
            {"src": "h", "to": "K", "required": True}],
        "derive": [{"out": "area", "expr": "x * y / 2", "vars": {"x": "a@mm", "y": "b@mm"}, "unit": "mm^2", "to": "cm^2",
                    "decimals": 3},
                   {"out": "ratio", "expr": "sqrt(abs(p)) / (q - 1)", "vars": {"p": "c_nom", "q": "c_tol"}}]}
FH = ["key", "a", "b", "c", "d", "e", "f", "g", "h", "free", "free2"]
NUMERIC_OUT = ["a", "b", "c_nom", "c_tol", "d_w", "d_lo", "f_lo", "f_hi", "g_c", "h", "area", "ratio"]


def test_random_values_keep_the_invariants():
    """でたらめな値を与えても: 落ちない / 行数が変わらない / ルールのない列は1文字も変わらない /
    数値の列に出るのは「数値・空・元の値・抽出NG印」だけ / 報告は JSON にできる / もう一度かけても変わらない。"""
    c = config_from_dict(FUZZ)
    import re
    number = re.compile(r"-?\d+(\.\d+)?")
    for seed in range(250):
        r = random.Random(seed)
        rows = [[f"k{i}"] + ["".join(r.choice(TOK) for _ in range(r.randint(0, 7))) for _ in FH[1:]]
                for i in range(r.randint(1, 25))]
        prov = Provenance()
        oh, orows, issues = convert_rows(c, FH, rows, prov)
        assert len(orows) == len(rows), seed
        oi = {col: i for i, col in enumerate(oh)}
        failed = {(i.row, i.out or i.column) for i in issues if i.severity == "E"} | {
            (i.row, col) for i in issues if i.severity == "E" for col in oh}
        for n, (a, b) in enumerate(zip(rows, orows), start=1):
            assert (b[oi["key"]], b[oi["free"]], b[oi["free2"]]) == (a[0], a[9], a[10]), seed
            for col in NUMERIC_OUT:
                v = b[oi[col]]
                if v == "" or number.fullmatch(v):
                    continue
                assert (n, col) in failed or re.fullmatch(r"NG\d*", normalize_text(v)), (seed, n, col, v)
        v = verify_before_after(c, FH, rows, oh, orows, issues, set(prov.corrected))
        assert v["ok"], (seed, v["violations"][:2])
        json.dumps({"g": group_issues(issues), "s": suggest(c, FH, rows), "t": shape_table(c, FH, rows, True),
                    "c": case_coverage(c, FH, rows), "d": diff_changes(FH, rows, oh, orows),
                    "i": idempotency_report(c, FH, rows)}, ensure_ascii=False)


# ================================================================ 既存の書き方を壊していないこと
def test_every_shipped_example_still_passes_check(capsys):
    """型の検査を足したとき、倍率を文字列で書いた既存の設定（inch = "25.4"）を弾いてしまった。"""
    ex = Path(__file__).parents[1] / "examples"
    pairs = [(t, ex / (t.stem + "_input.csv")) for t in sorted(ex.glob("*.toml")) if ".corrections" not in t.name]
    assert len(pairs) >= 3
    for toml, inp in pairs:
        args = ["check", "-c", str(toml)] + ([str(inp)] if inp.exists() else [])
        assert main(args) == 0, (toml.name, capsys.readouterr())
        capsys.readouterr()


@pytest.mark.parametrize("factor,ok", [(25.4, True), ("25.4", True), (1, True), ("1e-3", True), (0, False), (-1, False),
                                       ("abc", False), ("", False), (True, False), (float("nan"), False), ([], False)])
def test_unit_factor_forms(factor, ok):
    d = {"schema_version": 1, "units": {"length": {"zz": factor}}, "field": [{"src": "a", "to": "mm"}]}
    if ok:
        assert convert_rows(config_from_dict(d), ["k", "a"], [["r", "2zz"]])[1][0][1] not in ("", "2zz")
    else:
        with pytest.raises(ConfigError) as e:
            config_from_dict(d)
        assert "zz" in str(e.value)

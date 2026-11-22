"""draft（ルールの下書き）を、空と抽出NG印以外の列で、確からしいものをできるだけ作るようにした変更（0.14.0）。"""
import tomllib

from tenugui.analysis import draft_toml
from tenugui.core import check_header, config_from_dict, convert_rows

EMPTY = config_from_dict({"schema_version": 1})


def _draft(header, rows):
    text = draft_toml(EMPTY, header, rows)
    cfg = config_from_dict(tomllib.loads(text))            # 下書きは必ず設定として読める
    assert check_header(cfg, header) == []
    return text, cfg


def _field(cfg, src):
    return next(f for f in cfg.fields if f.src == src)


def _index(text):
    return [line for line in text.splitlines() if line.startswith("#   ")]


def test_similar_spellings_become_a_rule_marked_for_review():
    """似ている表記（similar）も同義語表に入れる。行末とまとめに「要確認」を付ける。"""
    h = ["k", "形式"]
    rows = [["a", "標準型"], ["b", "標準型"], ["c", "標準"], ["d", "特殊"]]
    text, cfg = _draft(h, rows)
    assert cfg.synonyms["形式"]["標準"] == "標準型"
    assert "要確認・似ている表記" in text
    assert any("形式: 要確認" in line for line in _index(text))
    _, out, issues = convert_rows(cfg, h, rows)
    assert [r[1] for r in out] == ["標準型", "標準型", "標準型", "特殊"] and issues == []


def test_values_without_variants_are_listed_so_only_new_values_warn():
    """値の種類が少ないときは、今のデータの値をすべて表に載せる。今のデータでは警告が出ず、新しい値だけ警告になる。"""
    h = ["k", "材質"]
    rows = [["a", "SUS304"], ["b", "sus304"], ["c", "SUS316"]]
    _, cfg = _draft(h, rows)
    assert _field(cfg, "材質").on_unknown == "warn"
    assert convert_rows(cfg, h, rows)[2] == []
    assert [i.code for i in convert_rows(cfg, h, [["d", "SS400"]])[2]] == ["C121"]


def test_alnum_only_difference_is_not_merged():
    """SUS304 と SUS304L のように英数字だけ違う値は、規格・品番の違いのことが多いので束ねない。"""
    h = ["k", "材質"]
    rows = [["a", "SUS304"], ["b", "SUS304"], ["c", "sus304"], ["d", "SUS304L"]]
    text, cfg = _draft(h, rows)
    table = cfg.synonyms["材質"]
    assert table["sus304l"] == "SUS304L" and table["sus304"] == "SUS304"
    assert "別物として扱った" in text
    assert convert_rows(cfg, h, rows)[2] == []


def test_many_distinct_values_pass_unknown_values_through():
    """値の種類が多い列は、表に無い値をそのまま通す（全部が警告になるのを避ける）。"""
    h = ["k", "メモ"]
    rows = [["x", "ＡＢＣ"], ["y", "abc"]] + [[str(n), f"自由記述{chr(0x4e00 + n)}"] for n in range(60)]
    _, cfg = _draft(h, rows)
    assert _field(cfg, "メモ").on_unknown == "keep"
    assert convert_rows(cfg, h, rows)[2] == []


def test_composite_uses_preset_when_it_fits():
    h = ["k", "寸法"]
    rows = [["a", "10±0.2"], ["b", "φ12±0.1"], ["c", "8+0.1/-0.2"]]
    text, cfg = _draft(h, rows)
    assert 'preset = "tolerance"' in text and "寸法: ルール" in text
    cols, out, issues = convert_rows(cfg, h, rows)
    assert issues == [] and dict(zip(cols, out[1]))["寸法_nom"] == "12"


def test_composite_cases_keep_numbers_of_different_meaning_apart():
    """±0.1 の 0.1 と 10±0.2 の 10 を同じ出力列に入れない。単位で区別できる数値は単位の名前にする。"""
    h = ["k", "公差", "定格"]
    rows = [["a", "10±0.2", "100V 50W"], ["b", "12±0.3", "200V 60W"], ["c", "±0.1", "100V 70W"], ["d", "なし", "110V 50W"]]
    text, cfg = _draft(h, rows)
    cols, out, issues = convert_rows(cfg, h, rows)
    assert issues == []
    got = [dict(zip(cols, r)) for r in out]
    assert got[0]["公差_n1"] == "10" and got[2]["公差_n1"] == ""
    assert got[2]["公差_1値_n1"] == "0.1" and got[3]["公差_text"] == "なし"
    assert got[1]["定格_V"] == "200" and got[1]["定格_W"] == "60"
    assert "公差: 要確認" in text and "定格: ルール" in text      # 位置で付けた仮の名前は確認してもらう


def test_every_column_appears_in_the_index_except_empty_ones():
    """ルールを作らなかった列も、理由つきで冒頭の一覧に出る。空と抽出NG印だけの列は対象外。"""
    h = ["k", "外径", "備考", "個数", "番号", "空", "NGだけ"]
    rows = [["a", "40mm", "特になし", "3", "X-1", "", "NG1"],
            ["b", "41", "赤い", "5", "Y-7", "", "NG2"],
            ["c", "4cm", "青い", "4", "Z-3", "", ""],
            ["d", "42mm", "特になし", "7", "W-9", "", "NG1"]]
    text, _ = _draft(h, rows)
    idx = _index(text)
    assert [line.split(":")[0].strip("# ") for line in idx] == ["外径", "備考", "個数", "番号"]
    assert any("個数: ルールなし — 数値だけ" in line for line in idx)
    assert any("番号: ルールなし — 自由記述または番号" in line for line in idx)
    assert "4 列: ルール 1 / 要確認 0 / ルールなし 3" in text


def test_each_rule_is_tried_on_the_data():
    """下書きは今のデータで変換してみて、結果をコメントに書く。そろえられない単位は要確認にする。"""
    h = ["k", "外径", "電圧"]
    rows = [["a", "40mm", "100V"], ["b", "4cm", "200V"], ["c", "41mm", "50W"], ["d", "42", "100V"],
            ["e", "40mm", "100V"], ["f", "40mm", "100V"]]
    text, _ = _draft(h, rows)
    assert "# 試行: このデータでは問題なし" in text
    assert "電圧: 要確認" in text and "V にそろえられない単位 W" in text and "試行: このデータでエラー 1 件" in text


def test_a_few_two_value_cells_make_the_column_composite():
    """2つの数値を持つ値が2割以上あれば、複合表記として形ごとに分ける（単位で列名を付ける）。"""
    h = ["k", "電圧"]
    rows = [["a", "100V"], ["b", "200V"], ["c", "100V 50W"], ["d", "100V"]]
    text, cfg = _draft(h, rows)
    cols, out, issues = convert_rows(cfg, h, rows)
    assert issues == [] and dict(zip(cols, out[2])) == {"k": "c", "電圧": "100V 50W", "電圧_V": "100", "電圧_W": "50"}


def test_chozu_merged_layout_skips_both_key_columns():
    """chozu --merge の出力は 元ファイル・シート名 の2列がキー。どちらにもルールを作らない。"""
    h = ["元ファイル", "シート名", "外径"]
    rows = [["a.xlsx", "P001", "40mm"], ["a.xlsx", "P002", "4cm"], ["A.XLSX", "P001", "41"]]
    text, cfg = _draft(h, rows)
    assert [f.src for f in cfg.fields] == ["外径"] and "synonyms" not in text


def test_chozu_missing_info_columns_are_left_out():
    """chozu の補助列 missing_info0 / missing_info1（重複して #2 が付いたものも）には下書きを作らない。"""
    h = ["シート名", "外径", "missing_info0", "missing_info1", "幅", "missing_info0", "missing_info1"]
    rows = [["P1", "40mm", "外径/幅", "B3", "10mm", "", "C5"],
            ["P2", "4cm", "外径", "b3", "11", "幅", "c5"],
            ["P3", "41", "外径/幅", "B3", "12mm", "", ""]]
    text, cfg = _draft(h, rows)
    assert [f.src for f in cfg.fields] == ["外径", "幅"]
    assert "missing_info" not in "".join(_index(text)) and not cfg.synonyms

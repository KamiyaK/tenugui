"""[settings] ignore_columns（0.15.0）: draft・survey の対象から外す列。既定は chozu の補助列 missing_info*。"""
import tomllib

import pytest

from tenugui.analysis import draft_toml, lint, shape_table, survey_overview
from tenugui.core import ConfigError, config_from_dict, convert_rows, is_ignored_column

H = ["シート名", "外径", "missing_info0", "missing_info1", "メモ", "missing_info0", "missing_info1"]
ROWS = [["P1", "40mm", "外径/幅", "B3", "ＡＢＣ", "", "C5"],
        ["P2", "4cm", "外径", "b3", "abc", "幅", "c5"],
        ["P3", "41", "外径/幅", "B3", "abc", "", ""]]
UH = ["シート名", "外径", "missing_info0", "missing_info1", "メモ", "missing_info0#2", "missing_info1#2"]


def cfg(**settings):
    return config_from_dict({"schema_version": 1, **({"settings": settings} if settings else {})})


def drafted(c):
    return [f.src for f in config_from_dict(tomllib.loads(draft_toml(c, H, ROWS))).fields]


def test_default_leaves_out_chozu_aux_columns_including_numbered_duplicates():
    c = cfg()
    assert [is_ignored_column(c, h) for h in UH] == [False, False, True, True, False, True, True]
    assert drafted(c) == ["外径", "メモ"]
    assert [o["column"] for o in survey_overview(c, H, ROWS)] == ["シート名", "外径", "メモ"]
    assert not any("missing_info" in str(r[0]) for r in shape_table(c, H, ROWS, all_columns=True))


def test_writing_the_setting_replaces_the_default():
    """書くと既定を置き換える（design_erc と同じ）。補助列も外すなら既定の式も書く。"""
    # missing_info0#2 は値が「幅」1つだけで表記ゆれも無く、ルールにならない
    assert drafted(cfg(ignore_columns=["メモ"])) == ["外径", "missing_info0", "missing_info1", "missing_info1#2"]
    both = cfg(ignore_columns=[r"missing_info\d*(#\d+)?", "メモ"])
    assert drafted(both) == ["外径"]
    assert "missing_info0" in [o["column"] for o in survey_overview(cfg(ignore_columns=[]), H, ROWS)]


def test_names_must_match_whole():
    c = cfg(ignore_columns=["メモ"])
    assert is_ignored_column(c, "メモ") and not is_ignored_column(c, "メモ2") and not is_ignored_column(c, "社内メモ")


def test_conversion_is_unchanged_and_aux_columns_pass_through():
    c = cfg()
    cols, out, issues = convert_rows(c, H, ROWS)
    assert cols == UH and [r[2] for r in out] == ["外径/幅", "外径", "外径/幅"] and issues == []


def test_bad_values_are_config_errors():
    with pytest.raises(ConfigError, match="ignore_columns の正規表現が不正"):
        cfg(ignore_columns=["("])
    with pytest.raises(ConfigError):
        cfg(ignore_columns="missing_info0")


def test_field_on_an_ignored_column_is_converted_but_linted():
    c = config_from_dict({"schema_version": 1, "settings": {"ignore_columns": ["外径"]},
                          "field": [{"src": "外径", "to": "mm", "default_unit": "mm"}]})
    assert [w["code"] for w in lint(c)] == ["L501"]
    _, out, _ = convert_rows(c, H, ROWS)
    assert [r[1] for r in out] == ["40", "40", "41"]

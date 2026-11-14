"""設定ファイル(TOML)の書き方の定義。`tenugui schema` が出す。agent が手引きを読まずに設定を書けるようにする。

ここに書いたキーの集合は、core の検証と一致することをテストで確かめている。
"""
from __future__ import annotations

from . import __version__
from .analysis import HINTS
from .core import PRESETS, SCHEMA_VERSION
from .units import DEFAULT_UNITS


def K(type_: str, desc: str, default=None, **extra) -> dict:
    d = {"type": type_, "desc": desc}
    if default is not None:
        d["default"] = default
    d.update(extra)
    return d


SETTINGS = {
    "encoding_in": K("string", "入力CSVの文字コード", "utf-8-sig"),
    "encoding_out": K("string", "出力CSVの文字コード", "utf-8-sig"),
    "passthrough": K("bool", "ルールのない列もそのまま出力する", True),
    "keep": K("string[]", "passthrough = false のときに残す列", []),
    "reserved": K("regex[]", "触らずに通す値（chozu の抽出NG印）", [r"^NG\d*$"]),
    "on_error": K("keep|empty", "変換に失敗した値を、元のまま残すか空にするか", "keep"),
    "rare_shape": K("number", "列の中でこの割合以下の形の値を C131 で警告する。0 で無効", 0.05),
    "null_values": K("string[]", "空として扱う値（\"-\"、\"なし\" など）。ルールのかかった列だけに効く。"
                                 "全角半角・大小の違いは無視、完全一致", []),
}

# 値の処理（通常の field と、parts の各部品で共通）
VALUE_KEYS = {
    "out": K("string", "出力列名。field では省略すると src を置き換える。部品では必須。{src} と for の {名前} を書ける"),
    "required": K("bool", "空なら C201 エラー", False),
    "sub": K("[regex, string][]", "最初に行う置換。case の部品では使えない"),
    "synonyms": K("string", "使う同義語表の名前（[synonyms.<名前>]）"),
    "on_unknown": K("keep|warn|error", "同義語表に無い値の扱い（C121）", "warn"),
    "number_only": K("bool", "数値だけを取り出す。換算しない", False, requires=["strip_units"],
                     conflicts=["to", "default_unit", "unit_out", "unit_label"]),
    "strip_units": K("string[]", "number_only で捨ててよい単位。それ以外の単位は C104。[\"*\"] は何でも",
                     requires=["number_only"]),
    "to": K("unit", "この単位に換算する"),
    "default_unit": K("unit", "単位のない値をこの単位とみなす", requires=["to"]),
    "unit_out": K("strip|add", "出力に単位を付けるか", "strip", requires=["to"]),
    "unit_label": K("string", "unit_out = add で付ける表記（省略時は to）", requires=["to"]),
    "decimals": K("int", "小数点以下の桁数（四捨五入）"),
    "trim": K("bool", "末尾の 0 を落とす", True),
}

FIELD_ONLY = {
    "src": K("string | string[]", "元の列名。配列にすると同じルールを複数の列にかける。for の {名前} を書ける", required=True),
    "for": K("{名前: (string|int)[]}", "列のまとめ指定。全ての組み合わせに展開し、src と out の {名前} を置き換える。"
                                     "例: for = { n = [\"0\", \"1\"] }, src = \"wire{n}_len\""),
    "drop_src": K("bool", "変換後に元の列を出力から消す。out = src とは併用できない", False),
    "extract": K("regex", "キャプチャグループ1つで、値の一部だけを取り出す（通常の field のみ）"),
}

SPLIT_KEYS = {
    "preset": K("|".join(PRESETS), "組み込みの分割。部品名は presets を参照"),
    "pattern": K("string", "簡易記法。# = 数値（部品名 n1, n2, …）、{名前} = 名前付きの数値、[...] = 省略可。"
                           "数値の直後の文字はその数値の単位で、書いた単位にだけ一致する。survey / suggest が返す shape をそのまま書ける"),
    "match": K("regex", "名前付きグループ (?P<名前>...) を持つ正規表現。'...'（リテラル文字列）で書く"),
    "parts": K("{部品名: string | table}", "部品 → 出力列。文字列は出力列名だけの短縮形。table は value_keys と unit_group", required=True),
    "each": K("table", "全ての部品に共通の指定（value_keys）。部品側に同じキーがあれば部品が優先"),
}

PART_ONLY = {
    "unit_group": K("string | string[]", "単位を借りるグループ名（match 用。preset / pattern は自動）", requires=["to"]),
}

DERIVE = {
    "out": K("string", "出力列名", required=True),
    "expr": K("string", "式。+ - * / **、括弧、pi、abs min max round sqrt", required=True),
    "vars": K("{変数: \"列名\" | \"列名@単位\"}", "式の変数。@単位 でその単位に換算してから使う"),
    "unit": K("unit", "式の結果の単位"),
    "to": K("unit", "結果をこの単位に換算する", requires=["unit"]),
    "unit_out": K("strip|add", "出力に単位を付けるか", "strip"),
    "decimals": K("int", "小数点以下の桁数"),
    "trim": K("bool", "末尾の 0 を落とす", True),
    "after": K("string", "この列の直後に置く（省略時は末尾）"),
}

TEST = {
    "name": K("string", "テスト名"),
    "in": K("{列名: 値}", "入力", required=True),
    "out": K("{出力列名: 期待値}", "期待する出力。out か codes のどちらかは必須"),
    "codes": K("string[]", "期待する報告コードの集合。省略時は「問題なし」を期待", []),
}

CODES = {
    "C001": ("E", "設定が参照する列が入力に無い"), "C002": ("E", "出力列名が既存の列と衝突"),
    "C003": ("W", "前回(lock)から入力の列構成が変わった"),
    "C101": ("E", "数値+単位として読めない"), "C102": ("E", "単位が無く default_unit も無い"),
    "C103": ("E", "未定義の単位、または次元が違って換算できない"), "C104": ("E", "strip_units に無い単位"),
    "C111": ("E", "preset / pattern / match / extract / case に一致しない"),
    "C121": ("W/E", "同義語表に無い値"), "C131": ("W", "変換はできたが、列の中で少数派の形"),
    "C201": ("E", "必須項目が空"),
    "C301": ("E", "派生: 入力が数値として読めない"), "C302": ("W", "派生: 入力が抽出NG印"),
    "C303": ("E", "派生: 計算できない"), "C304": ("W", "派生: 単位が無いので vars の単位とみなした"),
    "C305": ("E", "派生: 換算できない"), "C311": ("E", "派生: 単位付きの値に @単位 の指定が無い"),
    "L101": ("W", "再実行すると C102 になる設定"), "L102": ("W", "再実行すると値が変わる列"),
    "L201": ("W", "使われていない同義語表"),
}


def schema() -> dict:
    """設定ファイルの定義（tenugui.schema/1）。"""
    return {
        "schema": "tenugui.schema/1", "version": __version__, "schema_version": SCHEMA_VERSION,
        "toml": {
            "schema_version": K("int", "必須。常に 1", required=True),
            "settings": {"table": "[settings]", "keys": SETTINGS},
            "units": {"table": "[units.<次元>]",
                      "desc": "base = 基準単位、単位 = 倍率。式で定義する単位は { to_base = \"x + 273.15\", from_base = \"x - 273.15\" }",
                      "defaults": {dim: sorted(k for k in tbl if k != "base") for dim, tbl in DEFAULT_UNITS.items()}},
            "unit_alias": {"table": "[unit_alias]", "desc": "\"ボルト\" = \"V\" のように、単位の別名"},
            "synonyms": {"table": "[synonyms.<名前>]",
                         "desc": "\"正しい表記\" = [\"ゆれ1\", \"ゆれ2\"]。完全一致（全角半角・大小・空白・- _ ・ は無視）"},
            "field": {
                "table": "[[field]]",
                "forms": {
                    "plain": {"desc": "1列を1列に変換する", "keys": {**FIELD_ONLY, **VALUE_KEYS}},
                    "split": {"desc": "1つの値を複数の列に分ける。preset / pattern / match のどれか1つ + parts",
                              "keys": {k: FIELD_ONLY[k] for k in ("src", "for", "drop_src")} | SPLIT_KEYS},
                    "case": {"desc": "書式が混ざる列。[[field.case]] を上から順に試し、最初に一致したものを使う。"
                                     "一致した case に無い出力列は空。どれにも一致しなければ C111",
                             "keys": {**{k: FIELD_ONLY[k] for k in ("src", "for", "drop_src")},
                                      "each": SPLIT_KEYS["each"],
                                      "case": K("table[]", "[[field.case]]。各要素は split と同じ"
                                                           "（preset / pattern / match のどれか1つ + parts、each）", required=True)}},
                },
                "value_keys": VALUE_KEYS, "part_only_keys": PART_ONLY,
                "order": "抽出NG印はそのまま → null_values と空 → 全角半角の統一 → sub → synonyms → "
                         "preset/pattern/match/extract → number_only または to（換算） → decimals/trim/unit_out",
            },
            "derive": {"table": "[[derive]]", "desc": "他の列から計算する列。宣言順に評価する", "keys": DERIVE},
            "test": {"table": "[[test]]", "desc": "入力 → 期待値。check と test が実行する", "keys": TEST},
        },
        "presets": {name: {"parts": list(order)} for name, (_, order) in PRESETS.items()},
        "preset_examples": {"tolerance": ["100±5V", "100V±5V", "100+5/-3V", "φ13±0.2", "100V"],
                            "tolerance_range": ["1250W±100W(11.5~13.5A)", "1250W(11.5~13.5A)"],
                            "range": ["11.5~13.5A", "5A~10A"]},
        "codes": {c: {"severity": sev, "meaning": m, "hint": HINTS.get(c, "")} for c, (sev, m) in CODES.items()},
        "notes": ["正規表現（match / extract / sub / reserved）は全角半角を統一した後の文字に対して書く。TOML では '...' で書く",
                  "{ ... } のインラインテーブルは1行で書く。長くなるなら [field.parts.名前] の形にする",
                  "設定を書いたら tenugui check -c R IN --format json、変えたら tenugui impact で影響を確かめる"],
    }


def schema_text(s: dict) -> str:
    L = [f"tenugui {s['version']} 設定ファイルの書き方（schema_version = {s['schema_version']}）", ""]

    def keys(title: str, ks: dict) -> None:
        L.append(title)
        for name, k in ks.items():
            extra = []
            if k.get("required"):
                extra.append("必須")
            if "default" in k:
                extra.append(f"既定 {k['default']!r}")
            if k.get("requires"):
                extra.append("要 " + ", ".join(k["requires"]))
            if k.get("conflicts"):
                extra.append("併用不可 " + ", ".join(k["conflicts"]))
            L.append(f"  {name:13s} {k['type']:22s} {k['desc']}" + (f"  [{' / '.join(extra)}]" if extra else ""))
        L.append("")

    t = s["toml"]
    keys("[settings]", t["settings"]["keys"])
    for form, d in t["field"]["forms"].items():
        keys(f"[[field]] {form}: {d['desc']}", d["keys"])
    keys("部品（parts の table）だけのキー", t["field"]["part_only_keys"])
    L += [f"処理順: {t['field']['order']}", ""]
    keys("[[derive]]", t["derive"]["keys"])
    keys("[[test]]", t["test"]["keys"])
    L.append("preset と部品名")
    L += [f"  {n:16s} {', '.join(p['parts'])}    例 {' / '.join(s['preset_examples'].get(n, []))}"
          for n, p in s["presets"].items()]
    L += ["", "報告コード"]
    L += [f"  {c} {d['severity']:3s} {d['meaning']}" for c, d in s["codes"].items()]
    L += ["", *[f"- {n}" for n in s["notes"]]]
    return "\n".join(L)

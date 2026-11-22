"""設定(TOML)の読み込み・検証と変換エンジン。"""
from __future__ import annotations

import codecs
import csv
import difflib
import hashlib
import json
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from .corrections import Corrections, Provenance, corrections_path, parse_corrections, read_corrections
from .expr import Expr, ExprError, compile_expr
from .units import (UnitError, UnitTable, fold_key, format_decimal, squash_ws,
                    normalize_text, parse_number, parse_quantity)

SCHEMA_VERSION = 1

_SETTINGS_KEYS = {"encoding_in", "encoding_out", "passthrough", "keep", "reserved", "on_error", "rare_shape", "null_values",
                  "ignore_columns"}
# 下書き・集計の対象から外す列（列名に全体一致）。既定は chozu がセクションごとに付ける補助列（未取得の項目名・未使用セル）。
# 同じ名前が複数あると読み込み時に #2, #3 が付く。design_erc の [meta] ignore_columns の既定と同じ
DEFAULT_IGNORE_COLUMNS = [r"missing_info\d*(#\d+)?"]
MAX_VALUE_CHARS = 2000         # これより長い値は、数値や書式として照合しない（C101 / C111）
RARE_SHAPE_MIN_ROWS = 20      # 少数派の形(C131)を調べるのに必要な、値のある行数
_FIELD_KEYS = {"src", "out", "drop_src", "required", "sub", "synonyms", "on_unknown",
               "extract", "number_only", "strip_units", "to", "default_unit", "unit_out", "unit_label", "decimals",
               "trim"}
_DERIVE_KEYS = {"out", "expr", "vars", "unit", "to", "unit_out", "decimals", "trim", "after"}
_PART_KEYS = (_FIELD_KEYS - {"src", "extract", "drop_src"}) | {"unit_group"}
_TOP_KEYS = {"schema_version", "settings", "units", "unit_alias", "synonyms", "field", "derive", "test"}
_TEST_KEYS = {"name", "in", "out", "codes"}


# ---- 組み込みの分割パターン（NFKC正規化後の文字列に対して使う）
_NUM = r"[+-]?\d+(?:\.\d+)?"
_UNS = r"\d+(?:\.\d+)?"
# preset 用の数値: 桁区切り（1,200）を数値の一部として読む。pattern は今までどおり（形 #,# を書いた人に気づかせる）
_PUNS = r"(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.\d+)?"
_PNUM = rf"[+-]?{_PUNS}"
# 単位: 先頭は数字・記号以外。区切り（, ; 、）は含めない。「数字のあとに文字」が続く並び（V-5V、mm,200mm）は
# 単位ではなく別の値なので一致させない（mm2、min-1 のように数字で終わるものは単位）
_U = r"(?![\d.\-])(?:(?!\d+(?:\.\d+)?[^\W\d_])[^\s±+/()~〜,;、])+"
PRESETS: dict[str, tuple[str, dict[str, list[str]]]] = {
    # 100±5V / 100V±5V / 100±5 / 100+5/-3V / φ13±0.2 / 100V（許容差なし）
    "tolerance": (
        rf"^[φΦØ]?(?P<nom>{_PNUM})\s*(?P<u1>{_U})?\s*"
        rf"(?:±\s*(?P<tol>{_PUNS})|\+\s*(?P<hi>{_PUNS})\s*/\s*-\s*(?P<lo>{_PUNS}))?\s*(?P<u2>{_U})?$",
        {"nom": ["u1", "u2"], "tol": ["u2", "u1"], "hi": ["u2", "u1"], "lo": ["u2", "u1"]}),
    # 1250W±100W(11.5～13.5A) / 1250W(11.5～13.5A): 許容差つきの値 + 括弧内の範囲
    "tolerance_range": (
        rf"^[φΦØ]?(?P<nom>{_PNUM})\s*(?P<u1>{_U})?\s*"
        rf"(?:±\s*(?P<tol>{_PUNS})|\+\s*(?P<hi>{_PUNS})\s*/\s*-\s*(?P<lo>{_PUNS}))?\s*(?P<u2>{_U})?\s*"
        rf"(?:\(\s*(?P<rlo>{_PNUM})\s*(?P<ru1>{_U})?\s*[~〜]\s*(?P<rhi>{_PNUM})\s*(?P<ru2>{_U})?\s*\))?$",
        {"nom": ["u1", "u2"], "tol": ["u2", "u1"], "hi": ["u2", "u1"], "lo": ["u2", "u1"],
         "rlo": ["ru1", "ru2"], "rhi": ["ru2", "ru1"]}),
    # 11.5～13.5A / 5A～10A
    "range": (
        rf"^(?P<lo>{_PNUM})\s*(?P<u1>{_U})?\s*[~〜]\s*(?P<hi>{_PNUM})\s*(?P<u2>{_U})?$",
        {"lo": ["u1", "u2"], "hi": ["u2", "u1"]}),
}


_SIGNS = str.maketrans({"−": "-", "‐": "-", "–": "-", "∼": "~"})


def match_text(text: str) -> str:
    """preset / pattern の照合にかける前の整形: 空白の連続を1つに、マイナスと波線の異体字を - と ~ に。"""
    return squash_ws(text).translate(_SIGNS)


def _clip(text: str, n: int = 80) -> str:
    """報告用に長い文字列を切る。"""
    return text if len(text) <= n else text[:n] + f"…（{len(text)}文字）"


def shape_of(text: str) -> str:
    """値の「形」。数値を # に置き換えたもの（330±15mm → #±#mm）。ばらつきの把握と pattern の元になる。"""
    t = re.sub(r"\s+", " ", normalize_text(text))
    return re.sub(r"\d+(?:\.\d+)?", "#", t)      # 桁区切りのカンマは残す（1,200V → #,#V）。読み違いに気づけるように


_PAT_UNIT = re.compile(r"[^\s±+\-/()~〜\d.,\[\]#{}]+")


def compile_pattern(pattern: str) -> tuple[str, dict[str, list[str]]]:
    """簡易記法を正規表現へ。戻り値 (正規表現, {数値名: 単位グループの候補順})。

      #        数値（左から n1, n2, ... と名前が付く）
      {name}   名前付きの数値
      [ ... ]  省略可能な部分
      数値の直後の文字（mm, W など）はその数値の単位。単位のない数値は、同じ括弧区間の
      隣の数値の単位を借りる（#±#mm の1つ目は mm）。それ以外の文字はそのまま一致。空白の有無は問わない。
    """
    if len(pattern) > 300:
        raise ValueError(f"pattern が長すぎます（{len(pattern)}文字。上限 300）")
    t = re.sub(r"\s+", " ", normalize_text(pattern)).translate(_SIGNS)
    out: list[str] = ["^"]
    order: list[tuple[str, int, bool]] = []      # (名前, 括弧区間, 自前の単位があるか)
    seg, auto, i, prev_sign, depth, last = 0, 0, 0, False, 0, ""
    neg = False                 # 直前の - を数値の符号として取り込む
    while i < len(t):
        c = t[i]
        if c == "-" and t[i + 1:i + 2] in ("#", "{") and (i == 0 or t[i - 1] in "~〜("):
            # 先頭・~ の後・( の後の - は区切りではなく符号（-5mm の形 -#mm を書いたとき、5 ではなく -5 を取る）
            neg, i = True, i + 1
            continue
        if c in "#{":
            if c == "{":
                j = t.find("}", i)
                if j < 0:
                    raise ValueError("{ が閉じていません")
                name = t[i + 1:j]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name.startswith("u_"):
                    raise ValueError(f"{{{name}}} は英数字と _ の名前にしてください（u_ で始まる名前は不可）")
                i = j + 1
            else:
                auto += 1
                name, i = f"n{auto}", i + 1
            if any(name == o[0] for o in order):
                raise ValueError(f"数値の名前 {name!r} が重複しています")
            if last == "num":
                raise ValueError("数値が隣り合っています（## や {a}{b}）。どこで区切るか決まらないので、間に文字か空白を書いてください")
            if last == "num_ws":
                out[-1] = r"\s+"          # 数値 空白 数値: 空白が無いと 12 を 1 と 2 に分けてしまう
            out.append(f"(?P<{name}>-{_UNS})" if neg else f"(?P<{name}>{_UNS if prev_sign else _NUM})")
            neg = False
            m = _PAT_UNIT.match(t, i)
            if m:
                out.append(rf"\s*(?P<u_{name}>{re.escape(m.group())})")
                i = m.end()
            order.append((name, seg, bool(m)))
            prev_sign = False
            last = "unit" if m else "num"
        elif c == "[":
            out.append("(?:")
            depth += 1
            i += 1
            last = "open"
        elif c == "]":
            if depth == 0:
                raise ValueError("] に対応する [ がありません")
            if last == "open":
                raise ValueError("[ ] の中が空です")
            out.append(")?")
            depth -= 1
            i += 1
            last = "close"
        elif c == " ":
            out.append(r"\s*")
            i += 1
            last = "num_ws" if last == "num" else last
        elif c in "~〜":
            out.append(r"\s*[~〜]\s*")
            prev_sign = False
            i += 1
            last = "lit"
        else:
            if c in "()":
                seg += 1
            out.append(re.escape(c) if c.isalnum() else rf"\s*{re.escape(c)}\s*")
            prev_sign = c in "±+-"
            i += 1
            last = "lit"
    if depth:
        raise ValueError("[ が閉じていません")
    if not order:
        raise ValueError("数値（# か {名前}）が1つもありません")
    unit_order: dict[str, list[str]] = {}
    for k, (name, sg, own) in enumerate(order):
        cand = [f"u_{name}"] if own else []
        cand += [f"u_{n}" for n, s2, o in order[k + 1:] if s2 == sg and o]
        cand += [f"u_{n}" for n, s2, o in reversed(order[:k]) if s2 == sg and o]
        unit_order[name] = cand
    return "".join(out) + "$", unit_order


class ConfigError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class Issue:
    row: int          # 入力CSVのデータ行番号（1始まり、ヘッダ除く）
    key: str          # 先頭列の値（シート名など）
    column: str
    code: str
    severity: str     # "E" or "W"
    message: str
    raw: str = ""
    out: str = ""     # 出力列名（別名列のとき。報告で原因の列を特定するため）


@dataclass
class FieldRule:
    src: str
    out: str
    drop_src: bool = False
    required: bool = False
    sub: list[tuple[re.Pattern, str]] = field(default_factory=list)
    synonyms: str | None = None
    on_unknown: str = "warn"
    extract: re.Pattern | None = None
    group: str | None = None         # parts 用: extract のどの名前付きグループを取るか
    unit_group: str | list[str] | None = None    # 単位を別グループから借りる（11.5～13.5A のAなど）。リストは先頭から順に空でないもの
    number_only: bool = False        # 先頭の数値だけ取り、指定した単位(strip_units)は捨てる（換算しない）
    strip_units: frozenset = frozenset()   # number_only で捨ててよい単位。"*" は何でも
    to: str | None = None
    default_unit: str | None = None
    unit_out: str = "strip"
    unit_label: str | None = None
    decimals: int | None = None
    trim: bool = True
    label: str = ""                  # どの書式か（pattern の文字列、preset:名前、match）
    # case 形式: 上から順に試し、最初に一致した書式の部品(alts)を使う
    case_rx: list[re.Pattern] = field(default_factory=list)
    case_labels: list[str] = field(default_factory=list)
    alts: dict[int, "FieldRule"] = field(default_factory=dict)


@dataclass
class DeriveRule:
    out: str
    expr: Expr
    vars: dict[str, tuple[str, str | None]]  # 変数名 -> (列名, 単位指定)
    unit: str | None = None      # 式の結果の単位
    to: str | None = None        # 指定すると結果をこの単位へ換算して出力
    unit_out: str = "strip"
    decimals: int | None = None
    trim: bool = True
    after: str | None = None


@dataclass
class Config:
    units: UnitTable
    fields: list[FieldRule]
    derives: list[DeriveRule]
    synonyms: dict[str, dict[str, str]]  # set名 -> {fold_key(variant): canonical}
    encoding_in: str = "utf-8-sig"
    encoding_out: str = "utf-8-sig"
    passthrough: bool = True
    keep: list[str] = field(default_factory=list)
    reserved: list[re.Pattern] = field(default_factory=list)
    on_error: str = "keep"
    tests: list[dict] = field(default_factory=list)   # [[test]]: {name, in, out, codes}
    rare_shape: float = 0.05     # 列の中でこの割合以下の形は「少数派」として C131 警告。0 で無効
    null_keys: frozenset = frozenset()   # 空として扱う値（[settings] null_values を NFKC・大小無視にしたもの）
    synonym_tables: dict[str, dict[str, list[str]]] = field(default_factory=dict)   # 書かれたままの同義語表（用語対応表用）
    corrections: Corrections | None = None    # 訂正・保留（別ファイル）。無ければ None
    ignore_columns: list[re.Pattern] = field(
        default_factory=lambda: [re.compile(x) for x in DEFAULT_IGNORE_COLUMNS])   # 下書き・集計の対象外の列


def _unknown(prefix: str, d: dict, allowed: set[str], problems: list[str]) -> None:
    for k in d:
        if k not in allowed:
            problems.append(f"{prefix}: 未知のキー {k!r}{did_you_mean(str(k), allowed)}"
                            f"（使えるキー: {', '.join(sorted(allowed))}）")


def did_you_mean(word: str, candidates, n: int = 3) -> str:
    """綴り間違いの候補。あれば「（もしかして: a, b）」、無ければ空文字。"""
    cands = [str(c) for c in candidates]
    low = {c.casefold(): c for c in cands}
    hit = difflib.get_close_matches(word.casefold(), list(low), n=n, cutoff=0.6)
    return f"（もしかして: {', '.join(low[h] for h in hit)}）" if hit else ""


def _all_units(units: UnitTable) -> list[str]:
    return sorted({*units.factors, *units.affine, *units.aliases})


_PH = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _fill(text: str, env: dict[str, str], w: str, p: list[str]) -> str:
    def sub(m):
        if m.group(1) not in env:
            p.append(f"{w}: {text!r} の {{{m.group(1)}}} に入れる値がありません"
                     f"（使えるのは {', '.join('{' + k + '}' for k in env) or 'なし'}。for で定義します）")
            return m.group(0)
        return env[m.group(1)]
    return _PH.sub(sub, text)


def _expand_field(f: dict, w: str, p: list[str]) -> list[dict]:
    """列のまとめ指定を、列ごとの [[field]] に展開する。
      src = ["a", "b"]                         同じルールを複数の列に
      for = { n = ["0", "1"] } + src = "w{n}"  組み合わせの数だけ展開。out にも {n} と {src} を書ける
    あわせて each（部品共通の指定）と parts の短縮形（部品名 = "出力列名"）をほどく。"""
    f = dict(f)
    loop = f.pop("for", None)
    srcs = f.get("src")
    envs: list[dict[str, str]] = [{}]
    if loop is not None:
        if not isinstance(loop, dict) or not loop or not all(
                isinstance(v, list) and v and all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in v)
                for v in loop.values()):
            p.append(f"{w}: for は {{ n = [\"0\", \"1\"] }} のように、名前 = 値の配列 で書きます")
            return []
        for name, vals in loop.items():
            if name == "src":
                p.append(f"{w}: for の名前に src は使えません（{{src}} は元の列名を表します）")
                return []
            envs = [{**e, name: str(v)} for e in envs for v in vals]
    if isinstance(srcs, list):
        if not srcs or not all(isinstance(x, str) and x for x in srcs):
            p.append(f"{w}: src の配列は列名の文字列だけにします")
            return []
        envs = [{**e, "src": _fill(x, e, w, p)} for e in envs for x in srcs]
    elif isinstance(srcs, str):
        envs = [{**e, "src": _fill(srcs, e, w, p)} for e in envs]
    else:
        p.append(f"{w}: src は列名の文字列か、その配列で書きます")
        return []
    if len({e["src"] for e in envs}) < len(envs):
        p.append(f"{w}: 展開した列名が重複しています（for を使うなら src に {{名前}} を入れます）")
        return []

    def parts_of(node: dict, each, where: str):
        if "each" in node:
            each = node["each"]
        if each is not None and not isinstance(each, dict):
            p.append(f"{where}: each は {{ number_only = true, strip_units = [\"mm\"] }} の形で書きます")
            each = None
        ps = node.get("parts")
        if not isinstance(ps, dict):
            return ps
        out = {}
        for g, pd in ps.items():
            if isinstance(pd, str):
                pd = {"out": pd}                      # 短縮形: 部品名 = "出力列名"
            out[g] = {**(each or {}), **pd} if isinstance(pd, dict) else pd
        return out

    if "each" in f and "parts" not in f and "case" not in f:
        p.append(f"{w}: each は parts（または case）と一緒に使います。部品に共通の指定を1回で書くためのものです")
    res = []
    for env in envs:
        g = {k: v for k, v in f.items() if k != "each"}
        g["src"] = env["src"]
        if isinstance(g.get("out"), str):
            g["out"] = _fill(g["out"], env, w, p)
        if "parts" in f:
            g["parts"] = parts_of(f, None, w)
        if isinstance(f.get("case"), list):
            cases = []
            for k, cs in enumerate(f["case"]):
                if not isinstance(cs, dict):
                    cases.append(cs)
                    continue
                c2 = {x: y for x, y in cs.items() if x != "each"}
                if "parts" in cs:
                    c2["parts"] = parts_of(cs, f.get("each"), f"{w}.case#{k + 1}")
                cases.append(c2)
            g["case"] = cases
        for node in [g, *[c for c in g.get("case", []) if isinstance(c, dict)]]:
            if isinstance(node.get("parts"), dict):
                node["parts"] = {gn: ({**pd, "out": _fill(pd["out"], env, w, p)}
                                      if isinstance(pd, dict) and isinstance(pd.get("out"), str) else pd)
                                 for gn, pd in node["parts"].items()}
        res.append(g)
    return res


def _mk_field(f: dict, w: str, units: UnitTable, synonyms: dict, p: list[str]) -> FieldRule | None:
    fr = FieldRule(src=f["src"], out=f.get("out", f["src"]))
    fr.drop_src = bool(f.get("drop_src", False))
    fr.required = bool(f.get("required", False))
    for j, pair in enumerate(f.get("sub", [])):
        try:
            rx = re.compile(pair[0])
            rx.sub(pair[1], "")          # 置換後の書き方（\\1、\\g<名前>）も今のうちに確かめる。変換の途中で落とさない
            fr.sub.append((rx, pair[1]))
        except (re.error, IndexError, TypeError) as e:
            p.append(f"{w}: sub[{j}] が不正 ({e})。置換後に \\1 や \\g<名前> を書くときは、正規表現側にそのグループが必要です")
    fr.synonyms = f.get("synonyms")
    if fr.synonyms and fr.synonyms not in synonyms:
        p.append(f"{w}: synonyms = {fr.synonyms!r} に対応する [synonyms.*] がありません"
                 f"{did_you_mean(fr.synonyms, synonyms)}")
    fr.on_unknown = f.get("on_unknown", "warn")
    if fr.on_unknown not in ("keep", "warn", "error"):
        p.append(f"{w}: on_unknown は keep / warn / error")
    if "extract" in f:
        try:
            fr.extract = re.compile(f["extract"])
            if fr.extract.groups < 1:
                p.append(f"{w}: extract にはキャプチャグループ ( ) が1つ必要です")
        except re.error as e:
            p.append(f"{w}: extract の正規表現が不正 ({e})")
    fr.to = f.get("to")
    fr.default_unit = f.get("default_unit")
    for key in ("to", "default_unit"):
        u = getattr(fr, key)
        if u is not None:
            setattr(fr, key, units.canon_unit(u))
            if not units.known(u):
                p.append(f"{w}: {key} = {u!r} は単位表にありません{did_you_mean(u, _all_units(units))}"
                         "（新しい単位は [units.<次元>] に倍率を足すか、[unit_alias] で既存の単位の別名にします）")
    if fr.default_unit and fr.to and units.known(fr.default_unit) and units.known(fr.to) \
            and not units.same_dim(fr.default_unit, fr.to):
        p.append(f"{w}: default_unit と to の次元が違います")
    if (fr.default_unit or "unit_out" in f or "unit_label" in f or "unit_group" in f) and not fr.to:
        p.append(f"{w}: default_unit / unit_out / unit_label / unit_group は to と一緒に使います")
    fr.number_only = bool(f.get("number_only", False))
    su = f.get("strip_units")
    if fr.number_only:
        if fr.to or fr.default_unit or "unit_out" in f or "unit_label" in f:
            p.append(f"{w}: number_only は to / default_unit / unit_out / unit_label と一緒に使えません"
                     "（単位を換算するなら to、単位を取るだけなら number_only + strip_units）")
        if not isinstance(su, list) or not su or not all(isinstance(x, str) and x.strip() for x in su):
            p.append(f"{w}: number_only には strip_units = [\"mm\", \"cm\"] のように、捨ててよい単位を指定します"
                     "（何でも捨てるなら [\"*\"]）")
        else:
            fr.strip_units = frozenset("*" if x.strip() == "*" else units.canon_unit(x) for x in su)
    elif su is not None:
        p.append(f"{w}: strip_units は number_only = true と一緒に使います")
    fr.unit_out = f.get("unit_out", "strip")
    if fr.unit_out not in ("strip", "add"):
        p.append(f"{w}: unit_out は strip / add")
    fr.unit_label = f.get("unit_label")
    fr.decimals = f.get("decimals")
    fr.trim = bool(f.get("trim", True))
    return fr


def _matcher_parts(f: dict, src: str, drop: bool, w: str, units: UnitTable, synonyms: dict,
                   p: list[str]) -> tuple[re.Pattern, str, list[FieldRule]] | None:
    """preset / match / pattern のどれか1つ + parts を、部品ごとの FieldRule にする。"""
    kinds = [k for k in ("preset", "match", "pattern") if k in f]
    if len(kinds) != 1:
        p.append(f"{w}: preset / match / pattern のどれか1つを parts と一緒に書きます"
                 if kinds else f"{w}: match と parts は一緒に書きます")
        return None
    kind = kinds[0]
    unit_order: dict[str, list[str]] | None = None
    if kind == "preset":
        pre = PRESETS.get(f["preset"])
        if pre is None:
            p.append(f"{w}: preset = {f['preset']!r} は未対応{did_you_mean(str(f['preset']), PRESETS)}"
                     f"（使えるもの: {', '.join(PRESETS)}）")
            return None
        rx, unit_order, label = re.compile(pre[0]), pre[1], f"preset:{f['preset']}"
    elif kind == "pattern":
        try:
            src_rx, unit_order = compile_pattern(str(f["pattern"]))
            rx = re.compile(src_rx)
        except (ValueError, re.error) as e:
            p.append(f"{w}: pattern が不正 ({e})")
            return None
        label = str(f["pattern"])
    else:
        try:
            rx = re.compile(f["match"])
        except re.error as e:
            p.append(f"{w}: match の正規表現が不正 ({e})")
            return None
        label = "match"
    names = list(unit_order) if unit_order is not None else list(rx.groupindex)
    if "parts" not in f and kind == "match":
        p.append(f"{w}: match と parts は一緒に書きます")
        return None
    if not isinstance(f.get("parts"), dict) or not f["parts"]:
        if kind == "match":
            p.append(f"{w}: parts は {{ グループ名 = {{ out = ... }} }} の形で書きます")
        else:
            p.append(f"{w}: parts が必要です（例: parts = {{ {names[0]} = {{ out = \"x\", to = \"V\" }} }}、"
                     f"使える名前: {', '.join(names)}）")
        return None
    res: list[FieldRule] = []
    for gname, pd in f["parts"].items():
        pw = f"{w}.parts.{gname}"
        if not isinstance(pd, dict):
            p.append(f"{pw}: {{ out = ... }} の形で書きます")
            continue
        _unknown(pw, pd, _PART_KEYS, p)
        if gname not in names:
            if kind == "match":
                p.append(f"{pw}: match に名前付きグループ (?P<{gname}>...) がありません")
            else:
                p.append(f"{pw}: {kind} {f[kind]!r} の部品名は {', '.join(names)} です")
            continue
        if "out" not in pd:
            p.append(f"{pw}: out が必要です")
            continue
        ug = pd.get("unit_group")
        for g in ([ug] if isinstance(ug, str) else ug or []):
            if g not in rx.groupindex:
                p.append(f"{pw}: unit_group = {g!r} が {kind} にありません")
        fr = _mk_field({**pd, "src": src}, pw, units, synonyms, p)
        if fr:
            fr.extract, fr.group, fr.label = rx, gname, label
            if unit_order is None:
                fr.unit_group = ug
            else:
                fr.unit_group = ug or ((unit_order[gname] or None) if (fr.to or fr.number_only) else None)
            fr.drop_src = drop
            res.append(fr)
    return rx, label, res


def load_config(path: str | Path, corrections: str | Path | None = None) -> Config:
    """ルールTOMLを読む。同じ場所に <ルール名>.corrections.toml があれば訂正・保留も読む
    （corrections で別の場所を指定できる）。"""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ConfigError([f"設定ファイルが見つかりません: {path}"]) from None
    try:
        raw = tomllib.loads(data.decode("utf-8-sig"))       # メモ帳などが付ける BOM は読み飛ばす
    except UnicodeDecodeError:
        raise ConfigError([f"設定ファイルを UTF-8 として読めません: {path}（UTF-8 で保存し直してください。"
                           "Shift_JIS や UTF-16 では読めません）"]) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"TOMLの構文エラー: {e}（よくある原因: 正規表現を \"...\" で書いた → '...' で書く / "
                           "{ } の中で改行した → 1行で書くか [field.parts.名前] の形にする / 文字列の \" の閉じ忘れ）"]) from None
    cp = Path(corrections) if corrections else corrections_path(path)
    if not corrections and not cp.exists():
        return config_from_dict(raw)
    craw, sha, probs = read_corrections(cp)
    if probs:
        raise ConfigError(probs)
    cfg = config_from_dict(raw, craw)
    cfg.corrections.path, cfg.corrections.sha = str(cp), sha
    return cfg


def _tname(v) -> str:
    return {bool: "true/false", int: "整数", float: "小数", str: "文字列", list: "配列", dict: "テーブル"}.get(
        type(v), type(v).__name__)


def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


_T_STR = ("文字列", lambda v: isinstance(v, str))
_T_NAME = ("空でない文字列", lambda v: isinstance(v, str) and v.strip() != "")
_T_BOOL = ("true / false", lambda v: isinstance(v, bool))
_T_DEC = ("-20〜50 の整数", lambda v: isinstance(v, int) and not isinstance(v, bool) and -20 <= v <= 50)
_T_STRS = ("文字列の配列 [\"a\", \"b\"]", _strs)
_T_SUB = ("[[\"正規表現\", \"置換後\"], ...] の形", lambda v: isinstance(v, list) and all(
    isinstance(x, list) and len(x) == 2 and all(isinstance(y, str) for y in x) for x in v))
_T_TABLE = ("テーブル { ... }", lambda v: isinstance(v, dict))
_T_STR_OR_STRS = ("文字列か、文字列の配列", lambda v: isinstance(v, str) or _strs(v))
_T_STRMAP = ("{ 名前 = \"文字列\" } の形", lambda v: isinstance(v, dict) and all(isinstance(x, str) for x in v.values()))
_T_TABLES = ("テーブルの配列（[[...]] で書く）", lambda v: isinstance(v, list) and all(isinstance(x, dict) for x in v))
_VALUE_T = {"out": _T_NAME, "required": _T_BOOL, "sub": _T_SUB, "synonyms": _T_STR, "on_unknown": _T_STR,
            "number_only": _T_BOOL, "strip_units": _T_STRS, "to": _T_NAME, "default_unit": _T_NAME,
            "unit_out": _T_STR, "unit_label": _T_STR, "decimals": _T_DEC, "trim": _T_BOOL}
_FIELD_T = {**_VALUE_T, "extract": _T_STR, "drop_src": _T_BOOL, "preset": _T_STR, "pattern": _T_STR,
            "match": _T_STR, "each": _T_TABLE, "case": _T_TABLES}
_PART_T = {**_VALUE_T, "unit_group": _T_STR_OR_STRS}
_DERIVE_T = {"out": _T_NAME, "expr": _T_STR, "vars": _T_STRMAP, "unit": _T_NAME, "to": _T_NAME, "unit_out": _T_STR,
             "decimals": _T_DEC, "trim": _T_BOOL, "after": _T_STR}
_SETTINGS_T = {"encoding_in": _T_NAME, "encoding_out": _T_NAME, "passthrough": _T_BOOL, "keep": _T_STRS,
               "reserved": _T_STRS, "on_error": _T_STR, "ignore_columns": _T_STRS}


def _type_problems(raw) -> list[str]:
    """値の型が合っているか。意味の検査より先に行い、型の違う値が先へ進まないようにする。"""
    if not isinstance(raw, dict):
        return ["設定の最上位はテーブルです"]
    p: list[str] = []

    def chk(where: str, d: dict, spec: dict) -> None:
        for k, v in d.items():
            t = spec.get(k)
            if t and not t[1](v):
                p.append(f"{where}: {k} は {t[0]} で書きます（今は {_tname(v)}: {repr(v)[:40]}）")

    def parts(where: str, node: dict) -> None:
        if isinstance(node.get("each"), dict):
            chk(f"{where}.each", node["each"], _VALUE_T)
        if isinstance(node.get("parts"), dict):
            for g, pd in node["parts"].items():
                if isinstance(pd, dict):
                    chk(f"{where}.parts.{g}", pd, _PART_T)

    for key, kind in (("settings", dict), ("units", dict), ("unit_alias", dict), ("synonyms", dict),
                      ("field", list), ("derive", list), ("test", list)):
        if key in raw and not isinstance(raw[key], kind):
            head = f"[{key}]" if kind is dict else f"[[{key}]]"
            p.append(f"{head} は{'テーブル' if kind is dict else 'テーブルの配列'}で書きます（今は {_tname(raw[key])}）")
    if p:
        return p
    chk("[settings]", raw.get("settings", {}), _SETTINGS_T)
    for dim, tbl in raw.get("units", {}).items():
        if not isinstance(tbl, dict):
            p.append(f"[units.{dim}] はテーブルで書きます（base = \"基準単位\"、単位 = 倍率）")
            continue
        for u, f in tbl.items():
            if u == "base":
                ok = isinstance(f, str) and f.strip() != ""
            elif isinstance(f, dict):
                ok = set(f) == {"to_base", "from_base"} and all(isinstance(x, str) for x in f.values())
            elif isinstance(f, str):            # "25.4" のように文字列で書いた倍率（小数を正確に書くための書き方）
                try:
                    d = Decimal(f.strip())
                    ok = d.is_finite() and d > 0
                except (InvalidOperation, ValueError):
                    ok = False
            else:
                ok = _num(f) and f > 0
            if not ok:
                p.append(f"[units.{dim}] {u} は "
                         + ("基準単位の名前（文字列）" if u == "base" else
                            "0 より大きい数（倍率。25.4 か \"25.4\"）か { to_base = \"式\", from_base = \"式\" }")
                         + f" で書きます（今は {_tname(f)}: {repr(f)[:40]}）")
    for a, u in raw.get("unit_alias", {}).items():
        if not isinstance(u, str) or not u.strip():
            p.append(f"[unit_alias] {a} は単位名（文字列）で書きます（今は {_tname(u)}）")
    for name, tbl in raw.get("synonyms", {}).items():
        if not isinstance(tbl, dict):
            p.append(f"[synonyms.{name}] は \"正しい表記\" = [\"ゆれ1\", \"ゆれ2\"] のテーブルで書きます（今は {_tname(tbl)}）")
            continue
        for canon, vs in tbl.items():
            if isinstance(vs, list) and not all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in vs):
                p.append(f"[synonyms.{name}] {canon} の配列は文字列だけにします")
    for i, f in enumerate(raw.get("field", [])):
        w = f"[[field]]#{i + 1}"
        if not isinstance(f, dict):
            p.append(f"{w}: テーブルで書きます（今は {_tname(f)}）")
            continue
        chk(w, f, _FIELD_T)
        parts(w, f)
        if isinstance(f.get("case"), list):
            for k, cs in enumerate(f["case"]):
                if isinstance(cs, dict):
                    chk(f"{w}.case#{k + 1}", cs, {k2: _FIELD_T[k2] for k2 in ("preset", "pattern", "match", "each")})
                    parts(f"{w}.case#{k + 1}", cs)
    for i, d in enumerate(raw.get("derive", [])):
        if not isinstance(d, dict):
            p.append(f"[[derive]]#{i + 1}: テーブルで書きます（今は {_tname(d)}）")
        else:
            chk(f"[[derive]]#{i + 1}", d, _DERIVE_T)
    for i, t in enumerate(raw.get("test", [])):
        if not isinstance(t, dict):
            p.append(f"[[test]]#{i + 1}: テーブルで書きます（今は {_tname(t)}）")
            continue
        chk(f"[[test]]#{i + 1}", t, {"name": _T_STR})
        for k in ("in", "out"):
            if isinstance(t.get(k), dict) and not all(isinstance(x, (str, int, float)) and not isinstance(x, bool)
                                                      for x in t[k].values()):
                p.append(f"[[test]]#{i + 1}: {k} の値は文字列で書きます（\"7mm\" のように引用符で囲む）")
        if isinstance(t.get("codes"), list) and not _strs(t["codes"]):
            p.append(f"[[test]]#{i + 1}: codes は [\"C111\"] のように文字列の配列で書きます")
    return p


def config_from_dict(raw: dict, corrections: dict | None = None) -> Config:
    """corrections は訂正ファイルを読んだ辞書（[[correct]] / [[hold]]）。"""
    tp = _type_problems(raw)
    if tp:
        raise ConfigError(tp)
    try:
        return _config_from_dict(raw, corrections)
    except ConfigError:
        raise
    except (RecursionError, MemoryError) as e:
        raise ConfigError([f"設定が大きすぎるか入れ子が深すぎて読めません（{type(e).__name__}）"]) from None
    except Exception as e:      # 型の検査をすり抜けた想定外の形。トレースバックではなく設定エラーとして返す
        raise ConfigError([f"設定を解釈できません（{type(e).__name__}: {str(e)[:120]}）。"
                           "値の型（文字列・数・配列・テーブル）が違う可能性があります。tenugui schema で書き方を確認してください"]) from None


def _config_from_dict(raw: dict, corrections: dict | None = None) -> Config:
    p: list[str] = []
    _unknown("トップレベル", raw, _TOP_KEYS, p)
    if raw.get("schema_version") != SCHEMA_VERSION:
        p.append(f"schema_version = {SCHEMA_VERSION} が必要です")
    st = raw.get("settings", {})
    _unknown("[settings]", st, _SETTINGS_KEYS, p)
    on_error = st.get("on_error", "keep")
    if on_error not in ("keep", "empty"):
        p.append("[settings] on_error は keep / empty")
    nulls = st.get("null_values", [])
    if not isinstance(nulls, list) or not all(isinstance(x, str) and x.strip() for x in nulls):
        p.append("[settings] null_values は [\"-\", \"なし\", \"未定\"] のように、空として扱う値の配列で書きます")
        nulls = []
    rare = st.get("rare_shape", 0.05)
    if isinstance(rare, bool) or not isinstance(rare, (int, float)) or not 0 <= rare < 0.5:
        p.append("[settings] rare_shape は 0 以上 0.5 未満の数（0 で無効。既定 0.05）")
        rare = 0.05
    reserved = []
    for pat in st.get("reserved", [r"^NG\d*$"]):
        try:
            rx = re.compile(pat)
        except re.error as e:
            p.append(f"[settings] reserved の正規表現が不正: {pat!r} ({e})")
            continue
        if rx.search(""):
            p.append(f"[settings] reserved の {pat!r} は空の文字列にも一致します。すべての値が抽出NG印として"
                     "扱われ、何も変換されなくなります（'^NG\\d*$' のように、印の形を書きます）")
            continue
        reserved.append(rx)
    ignore_columns = []
    for pat in st.get("ignore_columns", DEFAULT_IGNORE_COLUMNS):
        try:
            ignore_columns.append(re.compile(pat))
        except re.error as e:
            p.append(f"[settings] ignore_columns の正規表現が不正: {pat!r} ({e})")
    for key in ("encoding_in", "encoding_out"):
        if isinstance(st.get(key), str):
            try:
                codecs.lookup(st[key])
            except LookupError:
                p.append(f"[settings] {key} = {st[key]!r} という文字コードはありません（\"utf-8-sig\" や \"cp932\" のように書きます）")

    try:
        units = UnitTable(raw.get("units"), raw.get("unit_alias"))
    except UnitError as e:
        p.append(str(e))
        units = UnitTable()

    synonyms: dict[str, dict[str, str]] = {}
    synonym_tables: dict[str, dict[str, list[str]]] = {}
    for sname, tbl in raw.get("synonyms", {}).items():
        m: dict[str, str] = {}
        synonym_tables[sname] = {str(c): [str(v) for v in vs] for c, vs in tbl.items() if isinstance(vs, list)}
        for canon, variants in tbl.items():
            if not isinstance(variants, list):
                p.append(f"[synonyms.{sname}] {canon} は配列で書いてください")
                continue
            for v in [canon, *variants]:
                k = fold_key(str(v))
                if k in m and m[k] != canon:
                    p.append(f"[synonyms.{sname}] {v!r} が {m[k]!r} と {canon!r} の両方に入っています")
                m[k] = canon
        synonyms[sname] = m

    fields: list[FieldRule] = []
    expanded: list[tuple[str, dict]] = []
    for i, f in enumerate(raw.get("field", [])):
        w = f"[[field]]#{i + 1}"
        if not isinstance(f, dict) or "src" not in f:
            p.append(f"{w}: src が必要です")
            continue
        for g in _expand_field(f, w, p):
            expanded.append((f"{w}({g['src']})", g))
    for w, f in expanded:
        if "case" in f:
            _unknown(w, f, {"src", "case", "drop_src"}, p)     # for / each は展開時に取り除いてある
            if not isinstance(f["case"], list) or not f["case"]:
                p.append(f"{w}: case は [[field.case]] を1つ以上書きます")
                continue
            rxs, labels, by_out = [], [], {}
            for k, cs in enumerate(f["case"]):
                cw = f"{w}.case#{k + 1}"
                _unknown(cw, cs, {"preset", "match", "pattern", "parts"}, p)
                got = _matcher_parts(cs, f["src"], False, cw, units, synonyms, p)
                if got is None:
                    continue
                rx, label, parts = got
                seen_out = set()
                for fr in parts:
                    if fr.sub:
                        p.append(f"{cw}.parts: case の中では sub は使えません")
                    if fr.out in seen_out:
                        p.append(f"{cw}: 出力列名 {fr.out!r} が同じ case の中で重複しています")
                    seen_out.add(fr.out)
                    by_out.setdefault(fr.out, {})[len(rxs)] = fr
                rxs.append(rx)
                labels.append(label)
            for out, alts in by_out.items():
                fields.append(FieldRule(src=f["src"], out=out, drop_src=bool(f.get("drop_src", False)),
                                        case_rx=rxs, case_labels=labels, alts=alts))
            continue
        if "preset" in f or "match" in f or "pattern" in f or "parts" in f:
            _unknown(w, f, {"src", "preset", "match", "pattern", "parts", "drop_src"}, p)
            got = _matcher_parts(f, f["src"], bool(f.get("drop_src", False)), w, units, synonyms, p)
            if got:
                fields.extend(got[2])
        else:
            _unknown(w, f, _FIELD_KEYS, p)
            fr = _mk_field(f, w, units, synonyms, p)
            if fr:
                fields.append(fr)

    for fr in fields:
        if fr.drop_src and fr.out == fr.src:
            p.append(f"[[field]]({fr.src}): drop_src = true と、元の列と同じ名前への出力（out = src）は"
                     "同時に使えません（その列が消えてしまいます）。out を別名にしてください")
    derives: list[DeriveRule] = []
    outs = Counter(fr.out for fr in fields)
    for i, d in enumerate(raw.get("derive", [])):
        w = f"[[derive]]#{i + 1}"
        _unknown(w, d, _DERIVE_KEYS, p)
        if "out" not in d or "expr" not in d:
            p.append(f"{w}: out と expr が必要です")
            continue
        w = f"[[derive]]#{i + 1}({d['out']})"
        try:
            ex = compile_expr(d["expr"])
        except ExprError as e:
            p.append(f"{w}: {e}")
            continue
        vmap: dict[str, tuple[str, str | None]] = {}
        for vn, spec in d.get("vars", {}).items():
            col, _, u = str(spec).partition("@")
            if u:
                u = units.canon_unit(u)
                if not units.known(u):
                    p.append(f"{w}: vars.{vn} の単位 {u!r} は単位表にありません")
            vmap[vn] = (col, u or None)
        missing = ex.names - set(vmap)
        if missing:
            p.append(f"{w}: 式の変数 {sorted(missing)} が vars にありません")
        extra = set(vmap) - ex.names
        if extra:
            p.append(f"{w}: vars の {sorted(extra)} は式で使われていません")
        dr = DeriveRule(out=d["out"], expr=ex, vars=vmap, unit=d.get("unit"), to=d.get("to"),
                        unit_out=d.get("unit_out", "strip"), decimals=d.get("decimals"),
                        trim=bool(d.get("trim", True)), after=d.get("after"))
        if dr.unit_out not in ("strip", "add"):
            p.append(f"{w}: unit_out は strip / add")
        if dr.unit_out == "add" and not (dr.to or dr.unit):
            p.append(f"{w}: unit_out = add には unit（または to）が必要です")
        if dr.to and not dr.unit:
            p.append(f"{w}: to を使うには式の結果の単位 unit が必要です")
        if dr.after is not None and dr.after == dr.out:
            p.append(f"{w}: after に自分自身は書けません（列が出力されなくなります）。直前に置きたい列の名前を書きます")
        for key in ("unit", "to"):
            u = getattr(dr, key)
            if u and not units.known(u):
                p.append(f"{w}: {key} = {u!r} は単位表にありません")
        if dr.unit and dr.to and units.known(dr.unit) and units.known(dr.to) \
                and not units.same_dim(dr.unit, dr.to):
            p.append(f"{w}: unit と to の次元が違います")
        outs[dr.out] += 1
        derives.append(dr)

    for name, n in outs.items():
        if n > 1:
            p.append(f"出力列名 {name!r} が重複しています（out を変えてください）")
    # 派生値は宣言順に評価する。後ろの派生値を参照していたらエラー。
    seen = {fr.out for fr in fields}
    for dr in derives:
        for vn, (col, _) in dr.vars.items():
            later = {d.out for d in derives[derives.index(dr) + 1:]}
            if col in later:
                p.append(f"[[derive]]({dr.out}): {col!r} はこの後で定義されています（依存順に並べてください）")
        seen.add(dr.out)

    tests: list[dict] = []
    for i, t in enumerate(raw.get("test", [])):
        w = f"[[test]]#{i + 1}"
        _unknown(w, t, _TEST_KEYS, p)
        if not isinstance(t.get("in"), dict) or not t["in"]:
            p.append(f"{w}: in = {{ 列名 = \"値\" }} が必要です")
            continue
        if not isinstance(t.get("out", {}), dict) or not isinstance(t.get("codes", []), list):
            p.append(f"{w}: out は {{ 列名 = \"期待値\" }}、codes は [\"C111\"] の形で書きます")
            continue
        if "out" not in t and "codes" not in t:
            p.append(f"{w}: out（期待する出力）か codes（期待する報告コード）が必要です")
            continue
        tests.append({"name": str(t.get("name", f"#{i + 1}")),
                      "in": {str(k): str(v) for k, v in t["in"].items()},
                      "out": {str(k): str(v) for k, v in t.get("out", {}).items()},
                      "codes": sorted(str(c) for c in t.get("codes", []))})

    cor = None
    if corrections is not None:
        cor, cp = parse_corrections(corrections, lambda s: any(r.search(s) for r in reserved))
        p.extend(cp)
    if p:
        raise ConfigError(p)
    return Config(ignore_columns=ignore_columns, corrections=cor, synonym_tables=synonym_tables, null_keys=frozenset(null_key(x) for x in nulls), rare_shape=float(rare), tests=tests, units=units, fields=fields, derives=derives, synonyms=synonyms,
                  encoding_in=st.get("encoding_in", "utf-8-sig"),
                  encoding_out=st.get("encoding_out", "utf-8-sig"),
                  passthrough=bool(st.get("passthrough", True)),
                  keep=list(st.get("keep", [])), reserved=reserved, on_error=on_error)


# ---------------------------------------------------------------- 変換

def uniquify(header: list[str]) -> tuple[list[str], list[str]]:
    """重複する列名に #2, #3 を付けて一意化。付けた元の名前を返す。"""
    seen: Counter = Counter()
    taken = set(header)                 # 元からある名前（"a#2" という列が実在する場合に重ねない）
    out, dups = [], []
    for h in header:
        seen[h] += 1
        if seen[h] > 1:
            n = seen[h]
            while f"{h}#{n}" in taken:
                n += 1
            name = f"{h}#{n}"
            taken.add(name)
            out.append(name)
            if h not in dups:
                dups.append(h)
        else:
            out.append(h)
    return out, dups


def check_header(cfg: Config, header: list[str]) -> list[str]:
    """設定が入力ヘッダに対して成り立つか。問題点のリスト。"""
    p: list[str] = []
    hs = set(header)
    loose = {fold_key(h): h for h in header}

    def missing(col: str) -> str:
        near = loose.get(fold_key(col))
        if near is not None and near != col:
            return f"（入力の列名は {near!r} です。空白・全角半角・大文字小文字が違います。設定の列名を入力に合わせてください）"
        return did_you_mean(col, header)

    for fr in cfg.fields:
        if fr.src not in hs:
            p.append(f"C001 入力に列 {fr.src!r} がありません{missing(fr.src)}")
        if fr.out != fr.src and fr.out in hs:
            p.append(f"C002 out={fr.out!r} が既存の入力列と衝突します")
    produced = {fr.out for fr in cfg.fields}
    for dr in cfg.derives:
        if dr.out in hs:
            p.append(f"C002 派生列 {dr.out!r} が既存の入力列と衝突します")
        for vn, (col, _) in dr.vars.items():
            if col not in hs and col not in produced:
                p.append(f"C001 派生 {dr.out!r} の変数 {vn} が参照する列 {col!r} がありません"
                         f"{did_you_mean(col, [*header, *produced])}")
        produced.add(dr.out)
        if dr.after and dr.after not in hs and dr.after not in produced:
            p.append(f"C001 派生 {dr.out!r} の after={dr.after!r} がありません")
    for k in cfg.keep:
        if k not in hs:
            p.append(f"C001 keep の列 {k!r} がありません")
    return p


def null_key(s: str) -> str:
    return normalize_text(s).casefold()


def is_null(cfg: Config, s: str) -> bool:
    """[settings] null_values にある値か（「-」「なし」など、空と同じ意味の値）。"""
    return bool(cfg.null_keys) and null_key(s) in cfg.null_keys


def is_ignored_column(cfg: Config, name: str) -> bool:
    """[settings] ignore_columns に当たる列か（下書き・集計の対象外。変換ではルールが無ければそのまま通る）。"""
    return any(r.fullmatch(name) for r in cfg.ignore_columns)


def _is_reserved(cfg: Config, s: str) -> bool:
    return any(r.search(s) for r in cfg.reserved)


def select_case(fr: FieldRule, raw: str) -> int | None:
    """case 形式で、値がどの書式に一致するか。一致した番号 / -1=どれにも不一致 / None=空。"""
    text = normalize_text(raw)
    if text == "":
        return None
    if len(text) > MAX_VALUE_CHARS:
        return -1
    sq = match_text(text)
    for k, rx in enumerate(fr.case_rx):
        if rx.search(text if fr.case_labels[k] == "match" else sq):   # 利用者の正規表現にはそのまま渡す
            return k
    return -1


def _apply_field(cfg: Config, fr: FieldRule, raw: str, nums: dict, rowctx: tuple,
                 issues: list[Issue], trace: list | None = None, prov: Provenance | None = None) -> str:
    row, key = rowctx

    def issue(code: str, sev: str, msg: str) -> None:
        issues.append(Issue(row, key, fr.src, code, sev, msg, raw, out=fr.out))

    def tr(stage: str, val) -> None:
        if trace is not None:
            trace.append((stage, str(val)))

    if _is_reserved(cfg, raw):
        tr("reserved", raw)
        return raw  # chozuの抽出NG印などは触らず通す
    if is_null(cfg, raw):
        tr("null", "空として扱う")
        raw = ""
    if fr.case_rx:
        k = select_case(fr, raw)
        if k is None:
            return ""
        if k < 0:
            tr("case", "どの書式にも不一致")
            nums[fr.out] = (None, None)
            issue("C111", "E", f"どの case にも一致しません（形: {_clip(shape_of(raw))}）")
            return "" if (cfg.on_error == "empty" or fr.out != fr.src) else raw
        tr("case", f"#{k + 1} {fr.case_labels[k]}")
        alt = fr.alts.get(k)
        if alt is None:
            return ""       # この書式には、この出力列にあたる部分がない
        return _apply_field(cfg, alt, raw, nums, rowctx, issues, trace, prov)
    text = normalize_text(raw)
    tr("normalize", text)
    if text == "":
        if fr.required:
            issue("C201", "E", "必須項目が空です")
        return ""
    for pat, repl in fr.sub:
        text = pat.sub(repl, text)
    if fr.sub:
        tr("sub", text)
    text = text.strip()
    if fr.synonyms:
        hit = cfg.synonyms[fr.synonyms].get(fold_key(text))
        if hit is not None:
            if prov is not None:
                prov.syn[(fr.synonyms, hit, text)] += 1
                prov.syn_cols.setdefault(fr.synonyms, set()).add(fr.src)
            text = hit
            tr("synonyms", text)
        elif fr.on_unknown != "keep":
            tr("synonyms", f"{text} (該当なし)")
            issue("C121", "E" if fr.on_unknown == "error" else "W",
                  f"同義語表 {fr.synonyms!r} に該当なし")
            if fr.on_unknown == "error" and cfg.on_error == "empty":
                return ""
    # 別名列(out != src)の失敗は空にする。元の値は src 列に残っており、別列に生の複合文字列を複製すると誤読のもと
    fail = "" if (cfg.on_error == "empty" or fr.out != fr.src) else raw
    if (fr.extract or fr.to or fr.number_only) and len(text) > MAX_VALUE_CHARS:
        tr("too_long", f"{len(text)}文字")
        nums[fr.out] = (None, None)
        issue("C101", "E", f"値が長すぎて数値として読めません（{len(text)}文字。上限 {MAX_VALUE_CHARS}）")
        return fail
    if fr.extract:
        m = fr.extract.search(match_text(text) if fr.label not in ("", "match") else text)
        if not m:
            tr("extract", "不一致")
            if fr.label.startswith("preset:"):       # 内部の正規表現ではなく、書式の名前と値の形を見せる
                issue("C111", "E", f"{fr.label.replace(':', ' = ')} の書式に一致しません（形: {_clip(shape_of(raw))}）")
            elif fr.label not in ("", "match"):
                issue("C111", "E", f"pattern {_clip(fr.label)!r} に一致しません（形: {_clip(shape_of(raw))}）")
            else:
                issue("C111", "E", f"{'match' if fr.group else 'extract'} に一致しません: {_clip(repr(fr.extract.pattern), 160)}")
            return fail
        g = m.group(fr.group if fr.group is not None else 1)
        if g is None or g.strip() == "":       # 省略可能なグループが無かった
            tr("extract", "(該当部分なし)")
            if fr.required:
                issue("C201", "E", "必須項目が空です（match の該当部分がありません）")
            return ""
        text = g.strip()
        if fr.label.startswith("preset:"):
            text = text.replace(",", "")           # 桁区切り（1,200）。preset の数値は区切りを含めて一致させている
            if not (fr.to or fr.number_only):
                mixed = _mixed_units(cfg, fr, m)
                if mixed:
                    nums[fr.out] = (None, None)
                    issue("C103", "E", f"1つの値の中で単位が混ざっています（{mixed[0]} と {mixed[1]}）。数値だけを取り出すと"
                                       "意味が変わります。部品に to を付けて換算するか、[[field.case]] で書式を分けてください")
                    return fail
        if fr.unit_group:
            ugs = [fr.unit_group] if isinstance(fr.unit_group, str) else fr.unit_group
            text += next((m.group(g).strip() for g in ugs if m.group(g)), "")
        tr("extract", text)
    if fr.number_only:
        nums[fr.out] = (None, None)
        try:
            if "*" in fr.strip_units:
                num = parse_number(text)
            else:
                num, unit = parse_quantity(text, cfg.units)
                if unit is not None and unit not in fr.strip_units:
                    issue("C104", "E", f"指定外の単位 {unit!r}（strip_units: {', '.join(sorted(fr.strip_units))}）")
                    return fail
        except UnitError as e:
            issue("C101", "E", str(e))
            return fail
        nums[fr.out] = (num, None)
        res = format_decimal(num, fr.decimals, fr.trim)
        tr("number_only", res)
        return res
    if fr.to is None:
        return text
    nums[fr.out] = (None, None)  # 失敗の目印。派生側は同じ原因を二重に報告しない
    try:
        num, unit = parse_quantity(text, cfg.units)
    except UnitError as e:
        issue("C101", "E", str(e))
        return fail
    if unit is None:
        if fr.default_unit is None:
            issue("C102", "E", f"単位がなく default_unit も未指定です（to={fr.to}）")
            return fail
        unit = fr.default_unit
    tr("quantity", f"{num} {unit}")
    try:
        num = cfg.units.convert(num, unit, fr.to)
    except UnitError as e:
        issue("C103", "E", str(e))
        return fail
    nums[fr.out] = (num, fr.to)
    s = format_decimal(num, fr.decimals, fr.trim)
    if fr.unit_out == "add":
        s += fr.unit_label if fr.unit_label is not None else fr.to
    tr("format", s)
    return s


def _mixed_units(cfg: Config, fr: FieldRule, m: re.Match) -> tuple[str, str] | None:
    """preset で、この部品が共有する2か所の単位（100V±5% の V と %）が両方あって違うとき、その組。"""
    names = PRESETS[fr.label[len("preset:"):]][1].get(fr.group or "", [])
    us = [cfg.units.canon_unit(m.group(n)) for n in names if m.group(n)]
    return (us[0], us[1]) if len(us) == 2 and us[0] != us[1] else None


def _var_value(cfg: Config, col: str, unit: str | None, vals: dict, nums: dict,
               ) -> tuple[Decimal | None, tuple[str, str] | None]:
    """派生の変数1つを数値化。戻り値 (値, (code, message)|None)。値None=計算不能。"""
    if col in nums:
        num, have = nums[col]
        if num is None:
            return None, None  # 上流で報告済み
        if unit and have and unit != have:
            try:
                return cfg.units.convert(num, have, unit), None
            except UnitError as e:
                return None, ("C305", str(e))
        return num, None
    raw = vals.get(col, "")
    if _is_reserved(cfg, raw):
        return None, ("C302", f"入力 {col} が抽出NG印 {raw!r} のため計算しません")
    if normalize_text(raw) == "" or is_null(cfg, raw):
        return None, None  # 空は空のまま伝播（報告しない）
    try:
        num, have = parse_quantity(raw, cfg.units)
    except UnitError as e:
        return None, ("C301", f"{col}: {e}")
    if have is None:
        if unit:
            return num, ("C304", f"{col} に単位がないため {unit} とみなしました")
        return num, None
    if not unit:
        return None, ("C311", f"{col}={raw!r} は単位付きです。vars に '{col}@{have}' のように単位を指定してください")
    try:
        return cfg.units.convert(num, have, unit), None
    except UnitError as e:
        return None, ("C305", f"{col}: {e}")


def _apply_derive(cfg: Config, dr: DeriveRule, vals: dict, nums: dict, rowctx: tuple,
                  issues: list[Issue]) -> str:
    row, key = rowctx
    env: dict[str, Decimal] = {}
    ok = True
    for vn, (col, unit) in dr.vars.items():
        v, prob = _var_value(cfg, col, unit, vals, nums)
        if prob:
            code, msg = prob
            sev = "W" if code in ("C302", "C304") else "E"
            issues.append(Issue(row, key, dr.out, code, sev, msg, vals.get(col, "")))
        if v is None:
            ok = False
        else:
            env[vn] = v
    if not ok:
        return ""
    try:
        res = dr.expr.eval(env)
    except ExprError as e:
        issues.append(Issue(row, key, dr.out, "C303", "E", str(e)))
        return ""
    unit = dr.unit
    if dr.to:
        try:
            res, unit = cfg.units.convert(res, dr.unit, dr.to), dr.to
        except UnitError as e:
            issues.append(Issue(row, key, dr.out, "C305", "E", str(e)))
            return ""
    nums[dr.out] = (res, unit)
    s = format_decimal(res, dr.decimals, dr.trim)
    if dr.unit_out == "add":
        s += unit or ""
    return s


def outputs_from(cfg: Config, column: str) -> list[str]:
    """入力の列 column の値から作られる出力列: その列のルールの出力と、それを使う派生（さらにその先も）。"""
    outs = [fr.out for fr in cfg.fields if fr.src == column]
    reach = {column, *outs}
    for dr in cfg.derives:                       # 派生は宣言順に評価するので、1回なめれば足りる
        if any(col in reach for col, _ in dr.vars.values()):
            reach.add(dr.out)
            outs.append(dr.out)
    return list(dict.fromkeys(outs))


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """同じ原因の重複を畳む。match が外れると parts の数だけ同じ C111 が出るため。"""
    seen: set = set()
    out: list[Issue] = []
    for i in issues:
        k = (i.row, i.column, i.code, i.message) if i.code == "C111" else \
            (i.row, i.column, i.out, i.code, i.message)
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


def plan_columns(cfg: Config, header: list[str]) -> list[str]:
    """出力列の並び。置換は元位置、別名outは元列の直後、派生は末尾（afterがあればその直後）。"""
    replaced = {fr.src for fr in cfg.fields if fr.out == fr.src}
    dropped = {fr.src for fr in cfg.fields if fr.drop_src}
    extra_after: dict[str, list[str]] = {}
    for fr in cfg.fields:
        if fr.out != fr.src:
            extra_after.setdefault(fr.src, []).append(fr.out)
    tail: list[str] = []
    for dr in cfg.derives:
        if dr.after:
            extra_after.setdefault(dr.after, []).append(dr.out)
        else:
            tail.append(dr.out)
    plan: list[str] = []

    def emit(name: str) -> None:
        plan.append(name)
        for e in extra_after.get(name, []):
            emit(e)

    for h in header:
        keep_it = (cfg.passthrough or h in cfg.keep or h in replaced) and h not in dropped
        if keep_it:
            emit(h)
        else:
            for e in extra_after.get(h, []):
                emit(e)
    plan.extend(tail)
    return plan


def convert_rows(cfg: Config, header: list[str], rows: Iterable[list[str]],
                 prov: Provenance | None = None) -> tuple[list[str], list[list[str]], list[Issue]]:
    """変換する。prov を渡すと、訂正・保留のかかったセルと、同義語表で使われた表記を記録する。"""
    uh, _ = uniquify(header)
    idx = {h: i for i, h in enumerate(uh)}
    plan = plan_columns(cfg, uh)
    issues: list[Issue] = []
    out_rows: list[list[str]] = []
    watch = _rare_shape_columns(cfg) if cfg.rare_shape > 0 else []
    seen_raw: dict[str, list[tuple[int, str, str]]] = {c: [] for c in watch if c in idx}
    cor = cfg.corrections
    cor_cols = [c for c in cor.columns if c in idx] if cor else []
    for n, row in enumerate(rows, start=1):
        row = list(row) + [""] * (len(uh) - len(row))
        nums: dict[str, tuple[Decimal | None, str | None]] = {}
        key = row[0] if row else ""
        held: set[str] = set()
        # 訂正・保留はルールより前。訂正は値を置き換えてからルールにかけ、保留はその列のルールをかけない
        for c in cor_cols:
            raw = row[idx[c]]
            if not raw.strip() or _is_reserved(cfg, raw):
                continue
            e = cor.lookup(c, raw, key)
            if e is None:
                continue
            if prov is not None:
                prov.hits[e.where] += 1
                prov.keys[n] = key
            if e.kind == "correct":
                if prov is not None:
                    prov.corrected[(n, c)] = (raw, e)
                row[idx[c]] = e.to
            else:
                if prov is not None:
                    prov.held[(n, c)] = (raw, e)
                held.add(c)
                nums[c] = (None, None)       # 派生側は計算しない（ここで報告済み）
                issues.append(Issue(n, key, c, "C141", "W", f"保留のため変換していません（理由: {e.reason}）", raw))
        vals = {h: row[i] for h, i in idx.items()}
        for c in seen_raw:
            seen_raw[c].append((n, row[0] if row else "", row[idx[c]]))
        ctx = (n, vals.get(uh[0], "") if uh else "")
        for fr in cfg.fields:
            if fr.src not in vals:
                continue
            if fr.src in held:               # 保留: 元の列は元の値のまま、別名の出力列は空
                nums[fr.out] = (None, None)
                if fr.out != fr.src:
                    vals[fr.out] = ""
                continue
            vals[fr.out] = _apply_field(cfg, fr, vals[fr.src] if fr.out == fr.src
                                        else row[idx[fr.src]], nums, ctx, issues, None, prov)
        for dr in cfg.derives:
            vals[dr.out] = _apply_derive(cfg, dr, vals, nums, ctx, issues)
        out_rows.append([vals.get(c, "") for c in plan])
    issues = dedupe_issues(issues)
    return plan, out_rows, issues + _rare_shape_issues(cfg, seen_raw, issues)


def _rare_shape_columns(cfg: Config) -> list[str]:
    """少数派の形を調べる列: 数値として読む通常の field と preset。
    pattern / match / case は形を明示しているので対象外。"""
    cols: list[str] = []
    for fr in cfg.fields:
        if fr.case_rx or not (fr.to or fr.number_only):
            continue
        if fr.extract is not None and not fr.label.startswith("preset:"):
            continue
        if fr.src not in cols:
            cols.append(fr.src)
    return cols


def _rare_shape_issues(cfg: Config, seen_raw: dict, issues: list[Issue]) -> list[Issue]:
    """変換はできたが、その列では少数派の形の値（330 だけ単位なし、など）を警告する。誤変換の見落とし防止。"""
    bad = {(i.row, i.column) for i in issues}
    out: list[Issue] = []
    for col, cells in seen_raw.items():
        live = [(n, k, raw, shape_of(raw)) for n, k, raw in cells
                if normalize_text(raw) and not _is_reserved(cfg, raw) and not is_null(cfg, raw)]
        if len(live) < RARE_SHAPE_MIN_ROWS:
            continue
        cnt = Counter(x[3] for x in live)
        main = cnt.most_common(1)[0][0]
        for n, k, raw, sh in live:
            if sh != main and cnt[sh] / len(live) <= cfg.rare_shape and (n, col) not in bad:
                out.append(Issue(n, k, col, "C131", "W",
                                 f"この列では少数派の形です（形 {sh}: {cnt[sh]}/{len(live)} 件。主な形は {main}）。"
                                 "変換はしました。意図どおりか確認してください", raw))
    return out


# ---------------------------------------------------------------- 入出力

MAX_FIELD_CHARS = 10_000_000


def read_csv(path: str | Path, encoding: str) -> tuple[list[str], list[list[str]]]:
    """CSV を読む。戻り値 (ヘッダ, データ行)。行は1つも捨てない・足さない（空行を除く）。

    - 先頭にある「# 」で始まるコメント行（chozu の NG 一覧の `# chozu: ...`）だけ読み飛ばす。
      ヘッダより後ろの行は、# で始まっていてもデータ（シート名が #004 のことがある）
    - 何も書かれていない行（改行だけ）は読み飛ばす。「,,,」のようにセルのある行は残す
    - ヘッダより列の多い行があれば止める（はみ出した値を黙って捨てない）
    """
    csv.field_size_limit(MAX_FIELD_CHARS)
    try:
        with open(path, newline="", encoding=encoding) as f:
            r = [x for x in csv.reader(f) if x]
    except csv.Error as e:
        raise ConfigError([f"{path}: CSV として読めません（{e}）。1つのセルが大きすぎるか、引用符 \" が閉じていません"]) from None
    while r and r[0][0].startswith("# "):
        r.pop(0)
    if not r:
        raise ConfigError([f"{path}: 空のCSVです"])
    header, rows = r[0], r[1:]
    header[0] = header[0].lstrip("\ufeff")          # BOM が二重に付いたファイル
    if len(header) == 1 and rows:
        for sep, name in (("\t", "タブ"), (";", "セミコロン")):
            if sep in header[0]:
                raise ConfigError([f"{path}: 区切り文字がカンマではないようです（{name}区切り）。カンマ区切りのCSVにしてください"])
    wide = [(n, len(x)) for n, x in enumerate(rows, start=1) if len(x) > len(header) and any(c.strip() for c in x[len(header):])]
    if wide:
        ex = "、".join(f"{n}行目（{k}列）" for n, k in wide[:5])
        raise ConfigError([f"{path}: ヘッダ（{len(header)}列）より列の多い行があります: {ex}"
                           f"{' ほか' if len(wide) > 5 else ''}。値の中のカンマが引用符で囲まれていない可能性があります"])
    return header, rows


def write_csv(path: str | Path, header: list[str], rows: list[list[str]], encoding: str) -> None:
    with open(path, "w", newline="", encoding=encoding) as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def write_report(path: str | Path, issues: list[Issue], encoding: str) -> None:
    with open(path, "w", newline="", encoding=encoding) as f:
        w = csv.writer(f)
        w.writerow(["row", "key", "column", "code", "severity", "message", "raw", "out"])
        for i in issues:
            w.writerow([i.row, i.key, i.column, i.code, i.severity, i.message, i.raw, i.out])


def survey(cfg: Config, header: list[str], rows: list[list[str]], column: str, top: int = 30
           ) -> dict:
    """ルールを書く前の実態調査: 列の値の分布と、検出された単位の分布。"""
    uh, _ = uniquify(header)
    i = uh.index(column)
    vals = Counter(normalize_text(r[i]) if i < len(r) else "" for r in rows)
    units: Counter = Counter()
    unparsable = 0
    for v, n in vals.items():
        if not v or _is_reserved(cfg, v):
            continue
        try:
            _, u = parse_quantity(v, cfg.units)
            units[u or "(なし)"] += n
        except UnitError:
            unparsable += n
    return {"column": column, "distinct": len(vals), "top_values": vals.most_common(top),
            "units": dict(units), "unparsable": unparsable}

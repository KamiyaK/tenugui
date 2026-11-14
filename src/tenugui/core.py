"""設定(TOML)の読み込み・検証と変換エンジン。"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .expr import Expr, ExprError, compile_expr
from .units import (UnitError, UnitTable, fold_key, format_decimal,
                    normalize_text, parse_number, parse_quantity)

SCHEMA_VERSION = 1

_SETTINGS_KEYS = {"encoding_in", "encoding_out", "passthrough", "keep", "reserved", "on_error"}
_FIELD_KEYS = {"src", "out", "drop_src", "required", "sub", "synonyms", "on_unknown",
               "extract", "number_only", "strip_units", "to", "default_unit", "unit_out", "unit_label", "decimals",
               "trim"}
_DERIVE_KEYS = {"out", "expr", "vars", "unit", "to", "unit_out", "decimals", "trim", "after"}
_PART_KEYS = (_FIELD_KEYS - {"src", "extract", "drop_src"}) | {"unit_group"}
_TOP_KEYS = {"schema_version", "settings", "units", "unit_alias", "synonyms", "field", "derive"}


# ---- 組み込みの分割パターン（NFKC正規化後の文字列に対して使う）
_NUM = r"[+-]?\d+(?:\.\d+)?"
_UNS = r"\d+(?:\.\d+)?"
_U = r"[^\s±+/()~〜\d.\-][^\s±+/()~〜]*"       # 単位: 先頭は数字・記号以外
PRESETS: dict[str, tuple[str, dict[str, list[str]]]] = {
    # 100±5V / 100V±5V / 100±5 / 100+5/-3V / φ13±0.2 / 100V（許容差なし）
    "tolerance": (
        rf"^[φΦØ]?(?P<nom>{_NUM})\s*(?P<u1>{_U})?\s*"
        rf"(?:±\s*(?P<tol>{_UNS})|\+\s*(?P<hi>{_UNS})\s*/\s*-\s*(?P<lo>{_UNS}))?\s*(?P<u2>{_U})?$",
        {"nom": ["u1", "u2"], "tol": ["u2", "u1"], "hi": ["u2", "u1"], "lo": ["u2", "u1"]}),
    # 1250W±100W(11.5～13.5A) / 1250W(11.5～13.5A): 許容差つきの値 + 括弧内の範囲
    "tolerance_range": (
        rf"^[φΦØ]?(?P<nom>{_NUM})\s*(?P<u1>{_U})?\s*"
        rf"(?:±\s*(?P<tol>{_UNS})|\+\s*(?P<hi>{_UNS})\s*/\s*-\s*(?P<lo>{_UNS}))?\s*(?P<u2>{_U})?\s*"
        rf"(?:\(\s*(?P<rlo>{_NUM})\s*(?P<ru1>{_U})?\s*[~〜]\s*(?P<rhi>{_NUM})\s*(?P<ru2>{_U})?\s*\))?$",
        {"nom": ["u1", "u2"], "tol": ["u2", "u1"], "hi": ["u2", "u1"], "lo": ["u2", "u1"],
         "rlo": ["ru1", "ru2"], "rhi": ["ru2", "ru1"]}),
    # 11.5～13.5A / 5A～10A
    "range": (
        rf"^(?P<lo>{_NUM})\s*(?P<u1>{_U})?\s*[~〜]\s*(?P<hi>{_NUM})\s*(?P<u2>{_U})?$",
        {"lo": ["u1", "u2"], "hi": ["u2", "u1"]}),
}


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


def _unknown(prefix: str, d: dict, allowed: set[str], problems: list[str]) -> None:
    for k in d:
        if k not in allowed:
            problems.append(f"{prefix}: 未知のキー {k!r}（綴り間違い？ 使えるキー: {', '.join(sorted(allowed))}）")


def _mk_field(f: dict, w: str, units: UnitTable, synonyms: dict, p: list[str]) -> FieldRule | None:
    fr = FieldRule(src=f["src"], out=f.get("out", f["src"]))
    fr.drop_src = bool(f.get("drop_src", False))
    fr.required = bool(f.get("required", False))
    for j, pair in enumerate(f.get("sub", [])):
        try:
            fr.sub.append((re.compile(pair[0]), pair[1]))
        except (re.error, IndexError, TypeError) as e:
            p.append(f"{w}: sub[{j}] が不正 ({e})")
    fr.synonyms = f.get("synonyms")
    if fr.synonyms and fr.synonyms not in synonyms:
        p.append(f"{w}: synonyms = {fr.synonyms!r} に対応する [synonyms.*] がありません")
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
                p.append(f"{w}: {key} = {u!r} は単位表にありません")
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


def load_config(path: str | Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError([f"設定ファイルが見つかりません: {path}"]) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"TOMLの構文エラー: {e}"]) from None
    return config_from_dict(raw)


def config_from_dict(raw: dict) -> Config:
    p: list[str] = []
    _unknown("トップレベル", raw, _TOP_KEYS, p)
    if raw.get("schema_version") != SCHEMA_VERSION:
        p.append(f"schema_version = {SCHEMA_VERSION} が必要です")
    st = raw.get("settings", {})
    _unknown("[settings]", st, _SETTINGS_KEYS, p)
    on_error = st.get("on_error", "keep")
    if on_error not in ("keep", "empty"):
        p.append("[settings] on_error は keep / empty")
    reserved = []
    for pat in st.get("reserved", [r"^NG\d*$"]):
        try:
            reserved.append(re.compile(pat))
        except re.error as e:
            p.append(f"[settings] reserved の正規表現が不正: {pat!r} ({e})")

    try:
        units = UnitTable(raw.get("units"), raw.get("unit_alias"))
    except UnitError as e:
        p.append(str(e))
        units = UnitTable()

    synonyms: dict[str, dict[str, str]] = {}
    for sname, tbl in raw.get("synonyms", {}).items():
        m: dict[str, str] = {}
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
    for i, f in enumerate(raw.get("field", [])):
        w = f"[[field]]#{i + 1}"
        if "src" not in f:
            p.append(f"{w}: src が必要です")
            continue
        w += f"({f['src']})"
        if "preset" in f:
            _unknown(w, f, {"src", "preset", "parts", "drop_src"}, p)
            pre = PRESETS.get(f["preset"])
            if pre is None:
                p.append(f"{w}: preset = {f['preset']!r} は未対応（使えるもの: {', '.join(PRESETS)}）")
                continue
            rx, unit_order = re.compile(pre[0]), pre[1]
            if not isinstance(f.get("parts"), dict) or not f["parts"]:
                p.append(f"{w}: parts が必要です（例: parts = {{ nom = {{ out = \"x\", to = \"V\" }} }}、"
                         f"使える名前: {', '.join(unit_order)}）")
                continue
            for gname, pd in f["parts"].items():
                pw = f"{w}.parts.{gname}"
                _unknown(pw, pd, _PART_KEYS, p)
                if gname not in unit_order:
                    p.append(f"{pw}: preset {f['preset']!r} の部品名は {', '.join(unit_order)} です")
                    continue
                if "out" not in pd:
                    p.append(f"{pw}: out が必要です")
                    continue
                fr = _mk_field({**pd, "src": f["src"]}, pw, units, synonyms, p)
                if fr:
                    fr.extract, fr.group = rx, gname
                    fr.unit_group = pd.get("unit_group") or (unit_order[gname] if (fr.to or fr.number_only) else None)
                    fr.drop_src = bool(f.get("drop_src", False))
                    fields.append(fr)
            continue
        if "match" in f or "parts" in f:
            _unknown(w, f, {"src", "match", "parts", "drop_src"}, p)
            if "match" not in f or "parts" not in f:
                p.append(f"{w}: match と parts は一緒に書きます")
                continue
            try:
                rx = re.compile(f["match"])
            except re.error as e:
                p.append(f"{w}: match の正規表現が不正 ({e})")
                continue
            if not isinstance(f["parts"], dict) or not f["parts"]:
                p.append(f"{w}: parts は {{ グループ名 = {{ out = ... }} }} の形で書きます")
                continue
            for gname, pd in f["parts"].items():
                pw = f"{w}.parts.{gname}"
                _unknown(pw, pd, _PART_KEYS, p)
                if gname not in rx.groupindex:
                    p.append(f"{pw}: match に名前付きグループ (?P<{gname}>...) がありません")
                    continue
                if "out" not in pd:
                    p.append(f"{pw}: out が必要です")
                    continue
                ug = pd.get("unit_group")
                for g in ([ug] if isinstance(ug, str) else ug or []):
                    if g not in rx.groupindex:
                        p.append(f"{pw}: unit_group = {g!r} が match にありません")
                fr = _mk_field({**pd, "src": f["src"]}, pw, units, synonyms, p)
                if fr:
                    fr.extract, fr.group, fr.unit_group = rx, gname, ug
                    fr.drop_src = bool(f.get("drop_src", False))
                    fields.append(fr)
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

    if p:
        raise ConfigError(p)
    return Config(units=units, fields=fields, derives=derives, synonyms=synonyms,
                  encoding_in=st.get("encoding_in", "utf-8-sig"),
                  encoding_out=st.get("encoding_out", "utf-8-sig"),
                  passthrough=bool(st.get("passthrough", True)),
                  keep=list(st.get("keep", [])), reserved=reserved, on_error=on_error)


# ---------------------------------------------------------------- 変換

def uniquify(header: list[str]) -> tuple[list[str], list[str]]:
    """重複する列名に #2, #3 を付けて一意化。付けた元の名前を返す。"""
    seen: Counter = Counter()
    out, dups = [], []
    for h in header:
        seen[h] += 1
        if seen[h] > 1:
            out.append(f"{h}#{seen[h]}")
            if h not in dups:
                dups.append(h)
        else:
            out.append(h)
    return out, dups


def check_header(cfg: Config, header: list[str]) -> list[str]:
    """設定が入力ヘッダに対して成り立つか。問題点のリスト。"""
    p: list[str] = []
    hs = set(header)
    for fr in cfg.fields:
        if fr.src not in hs:
            p.append(f"C001 入力に列 {fr.src!r} がありません")
        if fr.out != fr.src and fr.out in hs:
            p.append(f"C002 out={fr.out!r} が既存の入力列と衝突します")
    produced = {fr.out for fr in cfg.fields}
    for dr in cfg.derives:
        if dr.out in hs:
            p.append(f"C002 派生列 {dr.out!r} が既存の入力列と衝突します")
        for vn, (col, _) in dr.vars.items():
            if col not in hs and col not in produced:
                p.append(f"C001 派生 {dr.out!r} の変数 {vn} が参照する列 {col!r} がありません")
        produced.add(dr.out)
        if dr.after and dr.after not in hs and dr.after not in produced:
            p.append(f"C001 派生 {dr.out!r} の after={dr.after!r} がありません")
    for k in cfg.keep:
        if k not in hs:
            p.append(f"C001 keep の列 {k!r} がありません")
    return p


def _is_reserved(cfg: Config, s: str) -> bool:
    return any(r.search(s) for r in cfg.reserved)


def _apply_field(cfg: Config, fr: FieldRule, raw: str, nums: dict, rowctx: tuple,
                 issues: list[Issue], trace: list | None = None) -> str:
    row, key = rowctx

    def issue(code: str, sev: str, msg: str) -> None:
        issues.append(Issue(row, key, fr.src, code, sev, msg, raw, out=fr.out))

    def tr(stage: str, val) -> None:
        if trace is not None:
            trace.append((stage, str(val)))

    if _is_reserved(cfg, raw):
        tr("reserved", raw)
        return raw  # chozuの抽出NG印などは触らず通す
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
    if fr.extract:
        m = fr.extract.search(text)
        if not m:
            tr("extract", "不一致")
            issue("C111", "E", f"{'match' if fr.group else 'extract'} に一致しません: {fr.extract.pattern!r}")
            return fail
        g = m.group(fr.group if fr.group is not None else 1)
        if g is None or g.strip() == "":       # 省略可能なグループが無かった
            tr("extract", "(該当部分なし)")
            if fr.required:
                issue("C201", "E", "必須項目が空です（match の該当部分がありません）")
            return ""
        text = g.strip()
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
    if normalize_text(raw) == "":
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


def convert_rows(cfg: Config, header: list[str], rows: Iterable[list[str]]
                 ) -> tuple[list[str], list[list[str]], list[Issue]]:
    uh, _ = uniquify(header)
    idx = {h: i for i, h in enumerate(uh)}
    plan = plan_columns(cfg, uh)
    issues: list[Issue] = []
    out_rows: list[list[str]] = []
    for n, row in enumerate(rows, start=1):
        row = list(row) + [""] * (len(uh) - len(row))
        vals = {h: row[i] for h, i in idx.items()}
        nums: dict[str, tuple[Decimal | None, str | None]] = {}
        ctx = (n, vals.get(uh[0], "") if uh else "")
        for fr in cfg.fields:
            if fr.src in vals:
                vals[fr.out] = _apply_field(cfg, fr, vals[fr.src] if fr.out == fr.src
                                            else row[idx[fr.src]], nums, ctx, issues)
        for dr in cfg.derives:
            vals[dr.out] = _apply_derive(cfg, dr, vals, nums, ctx, issues)
        out_rows.append([vals.get(c, "") for c in plan])
    return plan, out_rows, dedupe_issues(issues)


# ---------------------------------------------------------------- 入出力

def read_csv(path: str | Path, encoding: str) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding=encoding) as f:
        r = list(csv.reader(f))
    # chozuのCSV先頭の「# chozu:」コメント行など # 始まりは読み飛ばす
    r = [x for x in r if x and any(c.strip() for c in x) and not x[0].startswith("#")]
    if not r:
        raise ConfigError([f"{path}: 空のCSVです"])
    return r[0], r[1:]


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

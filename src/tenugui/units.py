"""値の正規化・量(数値+単位)のパースと単位換算。標準ライブラリのみ。

換算は「宣言された単位表にある単位同士」だけ行う。表にない単位・次元違いは
黙って通さず UnitError にする（静かな誤りを避ける）。
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

__all__ = [
    "UnitError", "UnitTable", "normalize_text", "fold_key", "parse_quantity",
    "format_decimal", "DEFAULT_UNITS", "parse_number",
]


class UnitError(ValueError):
    pass


# マイナスとして読む記号。値のどこにあっても - にする（単位の指数 min⁻¹ → min-1 にも使う）
_MINUS = str.maketrans({"−": "-", "‐": "-", "–": "-", "－": "-"})
# 長音「ー」は、値の先頭で数値の直前にあるときだけマイナス（ー5）。単位の中（メートル、オーム）はそのまま
_HEAD_CHOON = re.compile(r"^ー(?=[\d.])")
_NUM_UNIT = re.compile(
    r"^(?P<num>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?P<unit>[^\d\s].*)?$"
)


# 目に見えない文字（ゼロ幅スペース・BOM・語の結合子など）。Excel からの貼り付けで紛れ込み、見た目は同じなのに一致しない原因になる
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_WS_RUN = re.compile(r"\s+")
MAX_UNIT_LEN = 40          # これより長い「単位」は単位ではない（注記など）


def _note_digits() -> dict[int, str]:
    """丸数字など「数字に見えるが数値の一部ではない」文字 → (n)。
    NFKC はこれらをただの数字にするので、そのままだと 5① が 51、100⒈ が 1001 と読まれてしまう。"""
    table: dict[int, str] = {}
    for lo, hi in ((0x2460, 0x2473), (0x24EA, 0x24EA), (0x2488, 0x249B), (0x3251, 0x325F), (0x32B1, 0x32BF)):
        for cp in range(lo, hi + 1):
            d = unicodedata.normalize("NFKC", chr(cp)).rstrip(".")
            if d.isdigit():
                table[cp] = f"({d})"
    return table


_NOTE_DIGITS = _note_digits()
# 数字の直後の上付き・下付き数字（5² や 100₁）。注記か累乗で、数値の続きではない。単位の後ろ（mm²）は今までどおり mm2
_SUPER_AFTER_DIGIT = re.compile(r"(?<=\d)[\u00b2\u00b3\u00b9\u2070\u2074-\u2079\u2080-\u2089]+")


def normalize_text(s: str) -> str:
    """NFKC（全角→半角、㎜→mm、℃→°C 等）＋見えない文字の除去＋前後空白除去。表記ゆれの第一段。

    丸数字は (1)、数字の直後の上付き数字は ^2 にしてから NFKC にかける（数値に溶け込ませない）。"""
    if not s.isascii():
        s = _SUPER_AFTER_DIGIT.sub(lambda m: "^" + m.group(), s.translate(_NOTE_DIGITS))
    return unicodedata.normalize("NFKC", s).translate(_INVISIBLE).strip()


def squash_ws(s: str) -> str:
    """空白の連続（改行・タブを含む）を1つの空白にする。数値や書式の照合の前にかける。"""
    return _WS_RUN.sub(" ", s)


def fold_key(s: str) -> str:
    """同義語の突き合わせ用キー。NFKC・大小無視・空白とハイフン類の除去。"""
    t = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"[\s\-_・]", "", t)


_NONZERO = re.compile(r"[1-9]")


def format_decimal(d: Decimal, decimals: int | None = None, trim: bool = True) -> str:
    if decimals is not None:
        q = Decimal(1).scaleb(-decimals)
        from decimal import ROUND_HALF_UP
        try:
            d = d.quantize(q, rounding=ROUND_HALF_UP)
        except InvalidOperation:        # 桁が多すぎて丸められない。丸めずに出す
            pass
    s = format(d, "f")
    if trim and "." in s:
        s = s.rstrip("0").rstrip(".")
    if s.startswith("-") and not _NONZERO.search(s):      # -0.00 → 0.00（丸めて 0 になった負の数）
        s = s[1:]
    return "0" if s == "" else s


# 次元 -> {"base": 基準単位, 単位: 基準単位に対する倍率}
DEFAULT_UNITS: dict[str, dict] = {
    "length": {"base": "mm", "mm": 1, "cm": 10, "m": 1000, "μm": "0.001", "km": 1000000},
    "voltage": {"base": "V", "V": 1, "mV": "0.001", "kV": 1000},
    "current": {"base": "A", "A": 1, "mA": "0.001", "μA": "0.000001", "kA": 1000},
    "resistance": {"base": "Ω", "Ω": 1, "mΩ": "0.001", "kΩ": 1000, "MΩ": 1000000},
    "power": {"base": "W", "W": 1, "mW": "0.001", "kW": 1000},
    "mass": {"base": "g", "g": 1, "mg": "0.001", "kg": 1000},
    "time": {"base": "s", "s": 1, "ms": "0.001", "min": 60, "h": 3600},
    "pressure": {"base": "Pa", "Pa": 1, "kPa": 1000, "MPa": 1000000, "bar": 100000},
    "frequency": {"base": "Hz", "Hz": 1, "kHz": 1000, "MHz": 1000000},
    "ratio": {"base": "%", "%": 1, "ppm": "0.0001"},
    # 倍率だけで換算できない単位は to_base / from_base を式で書く（変数は x）
    "temperature": {
        "base": "K", "K": 1,
        "°C": {"to_base": "x + 273.15", "from_base": "x - 273.15"},
        "°F": {"to_base": "(x - 32) * 5 / 9 + 273.15", "from_base": "(x - 273.15) * 9 / 5 + 32"},
    },
}

# 単位の表記ゆれ（NFKC後に適用）。ユーザーの [unit_alias] で追加・上書き。
DEFAULT_ALIASES = {
    "ohm": "Ω", "ohms": "Ω", "Ω": "Ω", "μ": "μ",
    "um": "μm", "uA": "μA", "v": "V", "a": "A", "w": "W", "hz": "Hz",
    "sec": "s", "秒": "s", "分": "min", "hr": "h", "時間": "h", "度C": "°C",
}


class _Resolved:
    """単位式の解決結果。scale=基準単位の積に対する倍率、sig=次元ごとの指数、affine=式定義の単位名。"""
    __slots__ = ("scale", "sig", "affine")

    def __init__(self, scale: Decimal, sig: dict[str, int], affine: str | None) -> None:
        self.scale, self.sig, self.affine = scale, sig, affine


def _sig_text(sig: dict[str, int]) -> str:
    return "·".join(d if e == 1 else f"{d}^{e}" for d, e in sorted(sig.items())) or "無次元"


_TOKEN = re.compile(r"\s*([^\s\d^*·/]+?)\s*(?:\^?\s*(-?\d+))?\s*")


class UnitTable:
    """単位表。線形(倍率)・式定義(to_base/from_base)・複合(mm^2, mm*mm, m/s)を扱う。"""

    def __init__(self, dims: dict[str, dict] | None = None,
                 aliases: dict[str, str] | None = None) -> None:
        from .expr import compile_expr, ExprError
        merged: dict[str, dict] = {k: dict(v) for k, v in DEFAULT_UNITS.items()}
        for dim, tbl in (dims or {}).items():
            merged.setdefault(dim, {}).update(tbl)
        self.factors: dict[str, Decimal] = {}          # 線形単位 -> 倍率
        self.affine: dict[str, tuple] = {}             # 式定義単位 -> (to_base, from_base)
        self.unit_dim: dict[str, str] = {}
        self.base: dict[str, str] = {}
        for dim, tbl in merged.items():
            base = tbl.get("base")
            if not base:
                raise UnitError(f"[units.{dim}] に base がありません")
            for u, f in tbl.items():
                if u == "base":
                    continue
                prev = self.unit_dim.get(u)
                if prev and prev != dim:
                    raise UnitError(f"単位 {u!r} が次元 {prev} と {dim} の両方にあります")
                self.unit_dim[u] = dim
                if isinstance(f, dict):
                    try:
                        tb, fb = compile_expr(f["to_base"]), compile_expr(f["from_base"])
                    except KeyError as e:
                        raise UnitError(f"[units.{dim}] {u}: to_base と from_base の両方が必要です") from e
                    except ExprError as e:
                        raise UnitError(f"[units.{dim}] {u}: {e}") from e
                    if tb.names != {"x"} or fb.names != {"x"}:
                        raise UnitError(f"[units.{dim}] {u}: 式の変数は x だけです")
                    self.affine[u] = (tb, fb)
                else:
                    try:
                        self.factors[u] = Decimal(str(f))
                    except InvalidOperation as e:
                        raise UnitError(f"[units.{dim}] {u} の倍率が数値ではありません: {f!r}") from e
            if base not in self.factors:
                raise UnitError(f"[units.{dim}] base={base!r} は倍率で定義された単位にしてください")
            self.base[dim] = base
        self.aliases = dict(DEFAULT_ALIASES)
        self.aliases.update({normalize_text(k): v for k, v in (aliases or {}).items()})
        self._cache: dict[str, _Resolved] = {}

    def canon_unit(self, raw: str) -> str:
        u = normalize_text(raw)
        return self.aliases.get(u, u)

    def resolve(self, unit: str) -> _Resolved:
        """'mm' 'mm^2' 'mm2' 'mm*mm' 'm/s' 'kg*m/s^2' を (倍率, 次元の指数, 式定義単位) に解く。"""
        hit = self._cache.get(unit)
        if hit:
            return hit
        text = normalize_text(unit)
        num, slash, den = text.partition("/")
        if slash and not den.strip():
            raise UnitError(f"単位式が不正です: {unit!r}")
        scale, sig, atoms, aff = Decimal(1), {}, 0, None
        for part, sign in ((num, 1), (den, -1)):
            if not part.strip():
                continue
            for tok in re.split(r"[*·]", part):
                m = _TOKEN.fullmatch(tok)
                if not m:
                    raise UnitError(f"単位式を読めません: {unit!r}")
                name = self.aliases.get(m.group(1), m.group(1))
                if len(m.group(2) or "") > 3 or abs(int(m.group(2) or 1)) > MAX_UNIT_EXPONENT:
                    raise UnitError(f"単位の指数が大きすぎます（±{MAX_UNIT_EXPONENT} まで）: {unit!r}")
                exp = int(m.group(2) or 1) * sign
                dim = self.unit_dim.get(name)
                if dim is None:
                    raise UnitError(f"未定義の単位: {name!r}（単位式 {unit!r}）")
                atoms += 1
                if name in self.affine:
                    aff = name
                else:
                    try:
                        scale *= self.factors[name] ** exp
                    except ArithmeticError:
                        raise UnitError(f"単位式を計算できません: {unit!r}") from None
                sig[dim] = sig.get(dim, 0) + exp
        sig = {d: e for d, e in sig.items() if e != 0}
        if aff and (atoms != 1 or sig.get(self.unit_dim[aff]) != 1):
            raise UnitError(f"式定義の単位 {aff!r} は複合単位の中では使えません: {unit!r}")
        r = _Resolved(scale, sig, aff)
        self._cache[unit] = r
        return r

    def known(self, unit: str) -> bool:
        try:
            self.resolve(unit)
            return True
        except UnitError:
            return False

    def same_dim(self, a: str, b: str) -> bool:
        return self.resolve(a).sig == self.resolve(b).sig

    def convert(self, value: Decimal, src: str, dst: str) -> Decimal:
        a, b = self.resolve(src), self.resolve(dst)
        if a.sig != b.sig:
            raise UnitError(f"次元が違います: {src}({_sig_text(a.sig)}) → {dst}({_sig_text(b.sig)})")
        if src == dst:
            return value
        try:
            if a.affine:
                base_val = self.affine[a.affine][0].eval({"x": value})
            else:
                base_val = value * a.scale
            if b.affine:
                return self.affine[b.affine][1].eval({"x": base_val})
            return base_val / b.scale
        except ArithmeticError:
            raise UnitError(f"換算の計算ができません（値が大きすぎるか小さすぎます）: {src} → {dst}") from None


MAX_UNIT_EXPONENT = 24  # 単位の指数（mm^3 など）の上限。これを超えるものは単位ではなく読み違い
MAX_DIGITS = 28        # Decimal の計算精度。これを超える桁は、換算のときに黙って丸められてしまう
MAX_EXPONENT = 40      # 10 の ±40 乗を超える値は、設計値としてあり得ない（1E1250 のような読み違いを通さない）


def _check_size(num: Decimal, text: str) -> Decimal:
    if not num.is_finite():
        raise UnitError(f"数値として読めません: {_short(text)}")
    if num and abs(num.adjusted()) > MAX_EXPONENT:
        raise UnitError(f"数値が大きすぎるか小さすぎます（10 の ±{MAX_EXPONENT} 乗まで）: {_short(text)}")
    if len(num.as_tuple().digits) > MAX_DIGITS:
        raise UnitError(f"桁数が多すぎます（有効数字 {MAX_DIGITS} 桁まで）: {_short(text)}")
    return num


def _short(text: str, n: int = 60) -> str:
    """エラーメッセージ用に値を短くする。"""
    r = repr(text)
    return r if len(r) <= n else r[:n] + f"…（{len(text)}文字）"


_THOUSANDS = re.compile(r"^([+-]?)([1-9]\d{0,2}(?:,\d{3})+)(?![\d,])")
_COMMA_IN_NUMBER = re.compile(r"^[+-]?\d+,\d")


def _number_head(text: str) -> str:
    """数値を読む前の整形: 正規化・空白の圧縮・マイナス記号の統一・桁区切りの除去。

    桁区切りと認めるのは 1,200 / 12,345,678 のように、先頭が 1〜3 桁（0 で始まらない）で、あとが 3 桁ずつのものだけ。
    0,500 や 12345,678 や 3,4 は、小数点のつもりか区切りの位置違いか決められないので読まない。"""
    t = _HEAD_CHOON.sub("-", squash_ws(normalize_text(text).translate(_MINUS)))
    m = _THOUSANDS.match(t)
    if m:
        return m.group(1) + m.group(2).replace(",", "") + t[m.end():]
    if _COMMA_IN_NUMBER.match(t):
        raise UnitError(f"カンマの位置が桁区切り（3桁ごと）になっていません。小数点のつもりか区切りか決められません: {_short(text)}")
    return t


def parse_quantity(text: str, table: UnitTable) -> tuple[Decimal, str | None]:
    """'35mm' / '１．５ cm' / '1,200V' -> (Decimal, canonical unit | None)。

    数値で始まらない・数値が複数ある（φ10±0.2 など）ものは UnitError。
    そういう複合表記は呼び出し側で regex 分割してから渡すこと。
    """
    t = _number_head(text)
    m = _NUM_UNIT.match(t)
    if not m:
        raise UnitError(f"数値+単位として読めません: {_short(text)}")
    if m.group("unit") and len(m.group("unit")) > MAX_UNIT_LEN:
        raise UnitError(f"数値の後ろが長すぎて単位として読めません: {_short(text)}")
    try:
        num = Decimal(m.group("num"))
    except InvalidOperation as e:
        raise UnitError(f"数値として読めません: {text!r}") from e
    unit = m.group("unit")
    return _check_size(num, text), (table.canon_unit(unit) if unit else None)


def parse_number(text: str) -> Decimal:
    """先頭の数値だけを取り出す。後ろに何があっても（単位・注記）捨てる。換算はしない。"""
    t = _number_head(text)
    m = re.match(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", t)
    if not m:
        raise UnitError(f"先頭が数値ではありません: {text!r}")
    if re.match(r"\.\d", t[m.end():]):         # 1.2.3 の 1.2 だけを取らない（数値がどこまでか決められない）
        raise UnitError(f"小数点が2つあり、数値として読めません: {_short(text)}")
    try:
        return _check_size(Decimal(m.group(0)), text)
    except InvalidOperation as e:
        raise UnitError(f"数値として読めません: {text!r}") from e

"""派生値用の式評価。ast ホワイトリスト＋Decimal。式言語は意図的に小さく保つ。

使えるもの: 数値リテラル, 変数, + - * / ** (指数は整数のみ), 単項 +/-, 括弧,
定数 pi, 関数 abs/min/max/round/sqrt。比較・代入・属性・添字・内包表記は不可。
"""
from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation

__all__ = ["ExprError", "compile_expr", "Expr"]

PI = Decimal("3.14159265358979323846264338327950288")
_BIN = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}
_FUNCS = {"abs", "min", "max", "round", "sqrt"}
_CONSTS = {"pi": PI}


class ExprError(ValueError):
    pass


class Expr:
    def __init__(self, source: str, tree: ast.Expression, names: frozenset[str]) -> None:
        self.source, self._tree, self.names = source, tree, names

    def eval(self, env: dict[str, Decimal]) -> Decimal:
        try:
            return self._ev(self._tree.body, env)
        except (DivisionByZero, InvalidOperation, ZeroDivisionError) as e:
            raise ExprError(f"計算できません（0除算など）: {self.source}") from e

    def _ev(self, n: ast.AST, env: dict[str, Decimal]) -> Decimal:
        if isinstance(n, ast.Constant):
            return Decimal(str(n.value))
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            return _CONSTS[n.id]
        if isinstance(n, ast.UnaryOp):
            v = self._ev(n.operand, env)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            if isinstance(n.op, ast.Pow):
                base, ex = self._ev(n.left, env), self._ev(n.right, env)
                if ex != ex.to_integral_value() or abs(ex) > 64:
                    raise ExprError(f"指数は絶対値64以下の整数のみ: {self.source}")
                return base ** int(ex)
            return _BIN[type(n.op)](self._ev(n.left, env), self._ev(n.right, env))
        if isinstance(n, ast.Call):
            args = [self._ev(a, env) for a in n.args]
            f = n.func.id  # type: ignore[attr-defined]
            if f == "abs":
                return abs(args[0])
            if f == "min":
                return min(args)
            if f == "max":
                return max(args)
            if f == "sqrt":
                return args[0].sqrt()
            if f == "round":
                from decimal import ROUND_HALF_UP
                nd = int(args[1]) if len(args) > 1 else 0
                return args[0].quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP)
        raise ExprError(f"評価できない構文: {self.source}")  # check で弾済みのはず


def compile_expr(source: str) -> Expr:
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"式の構文エラー: {source!r} ({e.msg})") from e
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expression):
            continue
        if isinstance(n, ast.Constant):
            if isinstance(n.value, bool) or not isinstance(n.value, (int, float)):
                raise ExprError(f"数値以外のリテラルは使えません: {source!r}")
        elif isinstance(n, ast.Name):
            if n.id not in _FUNCS and n.id not in _CONSTS:
                names.add(n.id)
        elif isinstance(n, ast.BinOp):
            if not isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                raise ExprError(f"使えない演算子: {source!r}")
        elif isinstance(n, ast.UnaryOp):
            if not isinstance(n.op, (ast.USub, ast.UAdd)):
                raise ExprError(f"使えない演算子: {source!r}")
        elif isinstance(n, ast.Call):
            if (not isinstance(n.func, ast.Name) or n.func.id not in _FUNCS
                    or n.keywords):
                raise ExprError(f"使えない関数呼び出し: {source!r}")
        elif isinstance(n, (ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
                            ast.USub, ast.UAdd)):
            pass
        else:
            raise ExprError(f"使えない構文 {type(n).__name__}: {source!r}")
    # 関数名を Name として拾わない（Call.func の Name）
    return Expr(source, tree, frozenset(names))

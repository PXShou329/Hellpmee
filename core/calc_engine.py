"""
算算數引擎（v2.2.0）— sympy 優先 + 輕量 fallback
─────────────────────────────────────────────────────────────────
支援：
  基本：1+2、3*4、2^10、(10/2)**3、100%7
  函數：sqrt(2)、sin(pi/2)、cos(pi)、log(100)、ln(e)
  總和：sum(i, i=1..n)、sum(i^2, i=1..100)、Σ i, i=1..n
  symbolic：sum(i, i=1..n) → n*(n+1)/2

安全：不使用裸 eval。有 sympy 用 sympy（限定 transformations），
沒裝 sympy fallback 到 safe AST + math 白名單。
"""
import re
import math

try:
    import sympy
    from sympy import symbols, summation, sqrt, sin, cos, tan, log, ln, pi, E, simplify, factor
    from sympy.parsing.sympy_parser import parse_expr, standard_transformations, implicit_multiplication_application, convert_xor
    _HAS_SYMPY = True
except Exception:
    _HAS_SYMPY = False


class CalcError(Exception):
    pass


# ════════════════════════════════════════════════════════════════
#  輸入正規化：把使用者寫法轉成引擎吃得下的
# ════════════════════════════════════════════════════════════════
def _normalize(expr: str) -> str:
    s = expr.strip()
    # Σ → sum
    s = s.replace("Σ", "sum ")
    # 全形 → 半形
    s = s.translate(str.maketrans("（）＝＋－＊／＾", "()=+-*/^"))
    return s.strip()


# ════════════════════════════════════════════════════════════════
#  sum(expr, i=1..n) 解析
# ════════════════════════════════════════════════════════════════
_SUM_RE = re.compile(
    r"""sum\s*\(?\s*(.+?)\s*,\s*([a-zA-Z])\s*=\s*(.+?)\.\.(.+?)\s*\)?$""",
    re.IGNORECASE,
)

def _try_parse_sum(expr: str):
    """
    回傳 (body, var, lo, hi) 或 None
    例：sum(i^2, i=1..100) → ('i^2', 'i', '1', '100')
    """
    m = _SUM_RE.match(expr.strip())
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip(), m.group(3).strip(), m.group(4).strip()


# ════════════════════════════════════════════════════════════════
#  主入口
# ════════════════════════════════════════════════════════════════
def evaluate(expr: str) -> str:
    """
    回傳結果字串。失敗拋 CalcError。
    """
    norm = _normalize(expr)
    if not norm:
        raise CalcError("空算式")

    # 先看是不是 sum(...)
    parsed = _try_parse_sum(norm)
    if parsed:
        return _eval_sum(*parsed)

    # 一般算式
    if _HAS_SYMPY:
        return _eval_sympy(norm)
    return _eval_lightweight(norm)


def _eval_sum(body: str, var: str, lo: str, hi: str) -> str:
    if _HAS_SYMPY:
        try:
            transformations = standard_transformations + (
                implicit_multiplication_application, convert_xor,
            )
            i = symbols(var)
            body_expr = parse_expr(body, transformations=transformations,
                                   local_dict={var: i})
            lo_expr = parse_expr(lo, transformations=transformations)
            # hi 可能是 symbolic n
            hi_is_symbol = not hi.replace("-", "").isdigit()
            if hi_is_symbol:
                n = symbols(hi)
                result = summation(body_expr, (i, lo_expr, n))
                result = factor(simplify(result))
                return f"Σ {body} (其中 {var}={lo}..{hi}) = {result}"
            else:
                hi_expr = parse_expr(hi, transformations=transformations)
                result = summation(body_expr, (i, lo_expr, hi_expr))
                return f"Σ {body} (其中 {var}={lo}..{hi}) = {result}"
        except Exception as e:
            raise CalcError(f"總和解析失敗：{str(e)[:60]}")
    else:
        # 輕量：只支援數值範圍
        if not (lo.lstrip("-").isdigit() and hi.lstrip("-").isdigit()):
            raise CalcError("沒裝 sympy，symbolic 總和算不了。請改成數值範圍，例如 sum(i, i=1..100)")
        lo_i, hi_i = int(lo), int(hi)
        if hi_i - lo_i > 1_000_000:
            raise CalcError("範圍太大了喵，本喵會算到天荒地老")
        total = 0
        for k in range(lo_i, hi_i + 1):
            total += _eval_lightweight_num(body.replace(var, f"({k})"))
        return f"Σ {body} (其中 {var}={lo}..{hi}) = {total:g}"


def _eval_sympy(expr: str) -> str:
    try:
        transformations = standard_transformations + (
            implicit_multiplication_application, convert_xor,
        )
        # log = 常用對數（base 10）、ln = 自然對數（規格 III.3）
        def _log10(x):
            return log(x, 10)
        local = {"pi": pi, "e": E, "E": E, "ln": ln,
                 "sqrt": sqrt, "sin": sin, "cos": cos, "tan": tan,
                 "log": _log10, "log10": _log10}
        result = parse_expr(expr, transformations=transformations, local_dict=local)
        evalf = result.evalf()
        try:
            num = float(evalf)
            if num.is_integer():
                return str(int(num))
            return f"{num:.10g}"
        except (TypeError, ValueError):
            return str(simplify(result))
    except Exception as e:
        raise CalcError(f"算式解析失敗：{str(e)[:60]}")


# ════════════════════════════════════════════════════════════════
#  輕量 fallback：safe AST + math 白名單
# ════════════════════════════════════════════════════════════════
import ast as _ast

_ALLOWED_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log10, "ln": math.log, "exp": math.exp, "abs": abs,
    "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial,
}
_ALLOWED_CONSTS = {"pi": math.pi, "e": math.e}

def _eval_lightweight(expr: str) -> str:
    # ^ → **
    expr = expr.replace("^", "**")
    val = _eval_lightweight_num(expr)
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return f"{val:.10g}"

def _eval_lightweight_num(expr: str) -> float:
    expr = expr.replace("^", "**")
    tree = _ast.parse(expr, mode="eval").body
    return _eval_node(tree)

def _eval_node(node):
    if isinstance(node, _ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise CalcError("不允許的常數")
    if isinstance(node, _ast.BinOp):
        l, r = _eval_node(node.left), _eval_node(node.right)
        op = type(node.op)
        if op is _ast.Add:  return l + r
        if op is _ast.Sub:  return l - r
        if op is _ast.Mult: return l * r
        if op is _ast.Div:  return l / r
        if op is _ast.Mod:  return l % r
        if op is _ast.Pow:  return l ** r
        if op is _ast.FloorDiv: return l // r
        raise CalcError("不允許的運算")
    if isinstance(node, _ast.UnaryOp):
        v = _eval_node(node.operand)
        if isinstance(node.op, _ast.USub): return -v
        if isinstance(node.op, _ast.UAdd): return +v
        raise CalcError("不允許的一元運算")
    if isinstance(node, _ast.Call):
        if not isinstance(node.func, _ast.Name):
            raise CalcError("不允許的函數呼叫")
        fn = _ALLOWED_FUNCS.get(node.func.id)
        if not fn:
            raise CalcError(f"不支援的函數：{node.func.id}")
        args = [_eval_node(a) for a in node.args]
        return fn(*args)
    if isinstance(node, _ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id]
        raise CalcError(f"不認識的符號：{node.id}")
    raise CalcError("不允許的算式結構")


# ════════════════════════════════════════════════════════════════
#  符號速查說明（規格三.3）
# ════════════════════════════════════════════════════════════════
def help_text() -> str:
    backend = "sympy（完整 symbolic）" if _HAS_SYMPY else "輕量版（數值為主）"
    return f"""**🔢 算算數使用說明**（引擎：{backend}）

**加減乘除**
`1 + 2`、`3 - 1`、`4 * 5`、`10 / 2`

**次方**
`2^10` 或 `2**10`

**平方根**
`sqrt(2)`

**三角函數**
`sin(pi/2)`、`cos(pi)`、`tan(pi/4)`

**對數**
`log(100)`（常用對數）、`ln(e)`（自然對數）

**總和**
`sum(i, i=1..n)`、`sum(i^2, i=1..100)`、`sum(i^3, i=1..n)`
也可以用 `Σ i, i=1..n`

**常見公式**
`Σ i = n(n+1)/2`
`Σ i² = n(n+1)(2n+1)/6`
`Σ i³ = [n(n+1)/2]²`

牢大直接用 `/工具 算算數 表達式:<算式>` 就好喵♡"""


def has_sympy() -> bool:
    return _HAS_SYMPY

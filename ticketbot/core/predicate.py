"""The safe `when:` evaluator.

This is a security-critical module: `when:` strings come from profile and pipeline
YAML that may be user-supplied, and untrusted ticket text flows into the evaluation
context. Evaluation is a hand-written tokenizer plus a recursive-descent parser that
builds a small tuple-based AST and walks it — **never** `eval`, `exec`, `compile`,
`ast.literal_eval` on user text, or any other path that reaches the Python
interpreter. Every limit below (`MAX_EXPR_LEN`, `MAX_TOKENS`, `MAX_DEPTH`) is
enforced before or during parsing so a hostile expression fails fast and cheap
instead of doing unbounded work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_EXPR_LEN = 2000
MAX_TOKENS = 400
MAX_DEPTH = 20

ORDERED_ENUMS: dict[str, list[str]] = {
    "ambiguity": ["low", "medium", "high"],
    "size": ["xs", "s", "m", "l", "xl"],
    "severity": ["nit", "should-fix", "blocker"],
}

_COMPARE_OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "in", "contains")
_KEYWORD_OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "contains")
_SYMBOL_OPS = {"==": "eq", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
_OP_LABELS = {
    "eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=",
    "in": "in", "contains": "contains", "empty": "is empty", "not_empty": "is not empty",
}


class PredicateError(ValueError):
    """Raised for any malformed, oversized or unsupported `when:` expression."""


class _Missing:
    """Singleton sentinel for an absent path — distinct from a real `None` value."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Token:
    type: str  # NUMBER, STRING, IDENT, OP, LPAREN, RPAREN, LBRACKET, RBRACKET, COMMA, DOT
    text: str
    pos: int


def _tokenize(expr: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and expr[i + 1].isdigit()):
            j = i + 1
            while j < n and expr[j].isdigit():
                j += 1
            if j < n and expr[j] == "." and j + 1 < n and expr[j + 1].isdigit():
                j += 1
                while j < n and expr[j].isdigit():
                    j += 1
            tokens.append(_Token("NUMBER", expr[i:j], i))
            i = j
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and expr[j] != ch:
                if expr[j] == "\\" and j + 1 < n:
                    j += 2
                else:
                    j += 1
            if j >= n:
                raise PredicateError(f"unterminated string starting at position {i}")
            tokens.append(_Token("STRING", expr[i : j + 1], i))
            i = j + 1
            continue
        two = expr[i : i + 2]
        if two in ("==", "!=", "<=", ">="):
            tokens.append(_Token("OP", two, i))
            i += 2
            continue
        if ch in ("<", ">"):
            tokens.append(_Token("OP", ch, i))
            i += 1
            continue
        if ch == "(":
            tokens.append(_Token("LPAREN", ch, i)); i += 1; continue
        if ch == ")":
            tokens.append(_Token("RPAREN", ch, i)); i += 1; continue
        if ch == "[":
            tokens.append(_Token("LBRACKET", ch, i)); i += 1; continue
        if ch == "]":
            tokens.append(_Token("RBRACKET", ch, i)); i += 1; continue
        if ch == ",":
            tokens.append(_Token("COMMA", ch, i)); i += 1; continue
        if ch == ".":
            tokens.append(_Token("DOT", ch, i)); i += 1; continue
        if ch == "_" or ch.isalpha():
            j = i + 1
            while j < n and (expr[j] == "_" or expr[j].isalnum()):
                j += 1
            tokens.append(_Token("IDENT", expr[i:j], i))
            i = j
            continue
        raise PredicateError(f"unexpected character {ch!r} at position {i}")
    return tokens


def _unquote(literal: str) -> str:
    inner = literal[1:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if c == "\\" and i + 1 < len(inner):
            out.append(inner[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _to_number(text: str) -> int | float:
    return float(text) if "." in text else int(text)


# --------------------------------------------------------------------------- #
# Parser — recursive descent over the grammar in section-2.md, building a small
# tuple-based AST. No token is ever handed to eval/exec/compile.
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, tokens: list[_Token], expr_text: str) -> None:
        self._tokens = tokens
        self._pos = 0
        self._depth = 0
        self._expr_text = expr_text

    def _peek(self) -> _Token | None:
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _peek_next(self) -> _Token | None:
        return self._tokens[self._pos + 1] if self._pos + 1 < len(self._tokens) else None

    def _advance(self) -> _Token:
        tok = self._peek()
        if tok is None:
            raise PredicateError(f"unexpected end of expression in {self._expr_text!r}")
        self._pos += 1
        return tok

    def _is_ident(self, *words: str) -> bool:
        tok = self._peek()
        return tok is not None and tok.type == "IDENT" and tok.text in words

    def _expect_ident(self, word: str) -> _Token:
        tok = self._peek()
        if tok is None or tok.type != "IDENT" or tok.text != word:
            got, pos = self._describe(tok)
            raise PredicateError(f"expected {word!r} at position {pos}, got {got!r}")
        return self._advance()

    def _expect_type(self, ttype: str, label: str) -> _Token:
        tok = self._peek()
        if tok is None or tok.type != ttype:
            got, pos = self._describe(tok)
            raise PredicateError(f"expected {label} at position {pos}, got {got!r}")
        return self._advance()

    def _describe(self, tok: _Token | None) -> tuple[str, int]:
        if tok is None:
            return "<end of expression>", len(self._expr_text)
        return tok.text, tok.pos

    def _enter(self) -> None:
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise PredicateError(f"expression nested too deeply (max depth {MAX_DEPTH})")

    def _exit(self) -> None:
        self._depth -= 1

    def parse(self) -> tuple:
        node = self._parse_expr()
        trailing = self._peek()
        if trailing is not None:
            raise PredicateError(f"unexpected token {trailing.text!r} at position {trailing.pos}")
        return node

    def _parse_expr(self) -> tuple:
        """Entry point for both the top-level expression and a parenthesized
        sub-expression — every recursive re-entry here counts one nesting level.
        """
        self._enter()
        try:
            return self._parse_or()
        finally:
            self._exit()

    def _parse_or(self) -> tuple:
        left = self._parse_and()
        while self._is_ident("or"):
            self._advance()
            right = self._parse_and()
            left = ("or", left, right)
        return left

    def _parse_and(self) -> tuple:
        left = self._parse_not()
        while self._is_ident("and"):
            self._advance()
            right = self._parse_not()
            left = ("and", left, right)
        return left

    def _parse_not(self) -> tuple:
        if self._is_ident("not"):
            self._advance()
            self._enter()
            try:
                operand = self._parse_not()
            finally:
                self._exit()
            return ("not", operand)
        return self._parse_primary()

    def _parse_primary(self) -> tuple:
        tok = self._peek()
        if tok is not None and tok.type == "LPAREN":
            self._advance()
            node = self._parse_expr()
            self._expect_type("RPAREN", "')'")
            return node
        return self._parse_comparison()

    def _parse_comparison(self) -> tuple:
        path = self._parse_path()
        tok = self._peek()
        if tok is None:
            return ("truthy", path)
        if tok.type == "IDENT" and tok.text == "is":
            self._advance()
            negate = False
            if self._is_ident("not"):
                self._advance()
                negate = True
            self._expect_ident("empty")
            return ("is_empty", path, negate)
        op = self._try_parse_op()
        if op is None:
            return ("truthy", path)
        operand = self._parse_operand()
        return ("compare", path, op, operand)

    def _try_parse_op(self) -> str | None:
        tok = self._peek()
        if tok is None:
            return None
        if tok.type == "OP" and tok.text in _SYMBOL_OPS:
            self._advance()
            return _SYMBOL_OPS[tok.text]
        if tok.type == "IDENT":
            if tok.text in _KEYWORD_OPS:
                self._advance()
                return tok.text
            if tok.text == "in":
                self._advance()
                return "in"
            if tok.text == "not":
                nxt = self._peek_next()
                if nxt is not None and nxt.type == "IDENT" and nxt.text == "in":
                    self._advance()
                    self._advance()
                    return "not_in"
        return None

    def _parse_path(self) -> str:
        tok = self._peek()
        if tok is None or tok.type != "IDENT":
            got, pos = self._describe(tok)
            raise PredicateError(f"expected an identifier at position {pos}, got {got!r}")
        parts = [self._advance().text]
        while self._peek() is not None and self._peek().type == "DOT":
            self._advance()
            tok = self._peek()
            if tok is None or tok.type != "IDENT":
                got, pos = self._describe(tok)
                raise PredicateError(f"expected an identifier after '.' at position {pos}, got {got!r}")
            parts.append(self._advance().text)
        return ".".join(parts)

    def _parse_operand(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise PredicateError(f"expected a value at position {len(self._expr_text)}")
        if tok.type == "NUMBER":
            self._advance()
            return _to_number(tok.text)
        if tok.type == "STRING":
            self._advance()
            return _unquote(tok.text)
        if tok.type == "LBRACKET":
            self._advance()
            items: list[Any] = []
            if self._peek() is not None and self._peek().type != "RBRACKET":
                items.append(self._parse_operand())
                while self._peek() is not None and self._peek().type == "COMMA":
                    self._advance()
                    items.append(self._parse_operand())
            self._expect_type("RBRACKET", "']'")
            return items
        if tok.type == "IDENT":
            self._advance()
            if tok.text == "true":
                return True
            if tok.text == "yes":
                return True
            if tok.text == "false":
                return False
            if tok.text == "no":
                return False
            if tok.text in ("null", "none"):
                return None
            return tok.text  # bare string literal, e.g. Bug
        raise PredicateError(f"unexpected token {tok.text!r} at position {tok.pos}")


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _lookup_path(context: Mapping[str, Any], dotted: str) -> Any:
    """Walk nested `Mapping`s and fall back to `getattr`; anything else -> MISSING."""
    current: Any = context
    for part in dotted.split("."):
        if current is MISSING:
            return MISSING
        if isinstance(current, Mapping):
            if part in current:
                current = current[part]
            else:
                return MISSING
        else:
            try:
                current = getattr(current, part)
            except AttributeError:
                return MISSING
    return current


def _is_empty(value: Any) -> bool:
    if value is MISSING or value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _apply_order(left: Any, op: str, right: Any) -> bool:
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right  # gte


def _order_compare(value: Any, op: str, operand: Any, path: str) -> bool:
    left_num = _as_number(value)
    right_num = _as_number(operand)
    if left_num is not None and right_num is not None:
        return _apply_order(left_num, op, right_num)

    last_segment = path.rsplit(".", 1)[-1]
    if last_segment in ORDERED_ENUMS:
        order = ORDERED_ENUMS[last_segment]
        left_str = value.lower() if isinstance(value, str) else str(value).lower()
        right_str = operand.lower() if isinstance(operand, str) else str(operand).lower()
        if left_str not in order or right_str not in order:
            return False
        return _apply_order(order.index(left_str), op, order.index(right_str))

    if isinstance(value, str) and isinstance(operand, str):
        return _apply_order(value.lower(), op, operand.lower())

    return False


def _bool_like(value: Any) -> Any:
    """Normalize a "yes"/"no"/"true"/"false" string so it compares equal to the
    keyword-literal booleans an operand parses to (`plan.security == yes` must hold
    when `plan.security` is the string "yes", not just the bool True).
    """
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
    return value


def _values_equal(value: Any, operand: Any) -> bool:
    if isinstance(value, bool) or isinstance(operand, bool):
        return _bool_like(value) == _bool_like(operand)
    if isinstance(value, str) and isinstance(operand, str):
        return value.lower() == operand.lower()
    try:
        return bool(value == operand)
    except Exception:
        return False


def _membership(item: Any, container: Any) -> bool:
    """`item` is a member of `container` (a list/tuple/set or a string)."""
    if item is MISSING or container is MISSING:
        return False
    if isinstance(container, str):
        return isinstance(item, str) and item.lower() in container.lower()
    if isinstance(container, (list, tuple, set)):
        for element in container:
            if isinstance(item, str) and isinstance(element, str):
                if item.lower() == element.lower():
                    return True
            elif item == element:
                return True
        return False
    return False


def _compare(value: Any, op: str, operand: Any, path: str) -> bool:
    if op == "eq":
        return False if value is MISSING else _values_equal(value, operand)
    if op == "ne":
        return True if value is MISSING else not _values_equal(value, operand)
    if op in ("lt", "lte", "gt", "gte"):
        return False if value is MISSING else _order_compare(value, op, operand, path)
    if op == "in":
        return _membership(value, operand)
    if op == "not_in":
        return not _membership(value, operand)
    if op == "contains":
        return _membership(operand, value)
    raise PredicateError(f"unknown operator {op!r}")


def _eval_node(node: tuple, context: Mapping[str, Any]) -> bool:
    kind = node[0]
    if kind == "or":
        return _eval_node(node[1], context) or _eval_node(node[2], context)
    if kind == "and":
        return _eval_node(node[1], context) and _eval_node(node[2], context)
    if kind == "not":
        return not _eval_node(node[1], context)
    if kind == "truthy":
        return bool(_lookup_path(context, node[1]))
    if kind == "is_empty":
        _, path, negate = node
        result = _is_empty(_lookup_path(context, path))
        return (not result) if negate else result
    if kind == "compare":
        _, path, op, operand = node
        return _compare(_lookup_path(context, path), op, operand, path)
    raise PredicateError(f"internal error: unknown AST node {kind!r}")


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def evaluate(expr: str, context: Mapping[str, Any]) -> bool:
    """String form, e.g.
      "workitem.acceptance is empty or workitem.ambiguity >= medium"
      "plan.security == yes or diff.touches_security"
      "not (story_points > 5) and issue_type in [Bug, Defect]"
    """
    if not isinstance(expr, str):
        raise PredicateError(f"expression must be a string, got {type(expr).__name__}")
    if len(expr) > MAX_EXPR_LEN:
        raise PredicateError(f"expression too long: {len(expr)} chars (max {MAX_EXPR_LEN})")

    tokens = _tokenize(expr)
    if len(tokens) > MAX_TOKENS:
        raise PredicateError(f"expression has too many tokens: {len(tokens)} (max {MAX_TOKENS})")
    if not tokens:
        raise PredicateError("empty expression")

    node = _Parser(tokens, expr).parse()
    return _eval_node(node, context)


def _single_op(path: str, rule: Mapping[str, Any]) -> tuple[str, Any]:
    if len(rule) != 1:
        raise PredicateError(
            f"mapping predicate for {path!r} must have exactly one operator, got {list(rule)}"
        )
    ((op, operand),) = rule.items()
    return str(op), operand


def _eval_mapping_rule(path: str, rule: Any, context: Mapping[str, Any]) -> bool:
    value = _lookup_path(context, path)
    if isinstance(rule, Mapping):
        op, operand = _single_op(path, rule)
        if op == "empty":
            return _is_empty(value)
        if op == "not_empty":
            return not _is_empty(value)
        if op not in _COMPARE_OPS:
            raise PredicateError(f"unknown operator {op!r} for {path!r}")
        return _compare(value, op, operand, path)
    return _compare(value, "eq", rule, path)


def evaluate_mapping(spec: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """Structured form used by pipeline_selector rules. ALL keys must hold (AND).
      {story_points: {lte: 2}, issue_type: Bug}      # nested op-dict, or a bare equality
      {labels: {contains: agent}}
      {issue_type: {in: [Bug, Defect]}}
    Operator names: eq ne lt lte gt gte in contains empty not_empty.
    """
    if not isinstance(spec, Mapping):
        raise PredicateError(f"mapping predicate must be a mapping, got {type(spec).__name__}")
    return all(_eval_mapping_rule(path, rule, context) for path, rule in spec.items())


def evaluate_any(spec: str | Mapping[str, Any] | None, context: Mapping[str, Any]) -> bool:
    """None/'' -> True. str -> evaluate. Mapping -> evaluate_mapping."""
    if spec is None:
        return True
    if isinstance(spec, str):
        return True if spec.strip() == "" else evaluate(spec, context)
    if isinstance(spec, Mapping):
        return evaluate_mapping(spec, context)
    raise PredicateError(f"predicate must be None, a string or a mapping, got {type(spec).__name__}")


def _describe_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "[" + ", ".join(_describe_value(v) for v in value) + "]"
    return str(value)


def describe_mapping(spec: Mapping[str, Any]) -> str:
    """Human text for the banner, e.g. {story_points: {lte: 5}} -> 'story_points <= 5'.
    Multiple keys joined with ' and '.
    """
    parts: list[str] = []
    for path, rule in spec.items():
        if isinstance(rule, Mapping):
            op, operand = _single_op(path, rule)
            label = _OP_LABELS.get(op, op)
            if op in ("empty", "not_empty"):
                parts.append(f"{path} {label}")
            else:
                parts.append(f"{path} {label} {_describe_value(operand)}")
        else:
            parts.append(f"{path} == {_describe_value(rule)}")
    return " and ".join(parts)

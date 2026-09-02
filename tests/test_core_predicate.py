import inspect

import pytest

from ticketbot.core import predicate
from ticketbot.core.predicate import (
    MAX_DEPTH,
    MAX_EXPR_LEN,
    MAX_TOKENS,
    MISSING,
    PredicateError,
    describe_mapping,
    evaluate,
    evaluate_any,
    evaluate_mapping,
)

CTX = {
    "workitem": {
        "key": "ENG-1842",
        "external_id": "ENG-1842",
        "title": "Login times out on SSO",
        "description": "Users see a spinner forever.",
        "issue_type": "Bug",
        "story_points": 5,
        "labels": ["agent", "sso"],
        "acceptance": "",
        "status": "In Progress",
        "ambiguity": "medium",
        "size": "m",
        "url": "https://example.atlassian.net/browse/ENG-1842",
        "comment_count": 2,
    },
    "story_points": 5,
    "issue_type": "Bug",
    "labels": ["agent", "sso"],
    "size": "m",
    "ambiguity": "medium",
    "plan": {"security": "yes", "sections": 3},
    "diff": {"touches_security": False, "files": 7},
    "step": {"id": "verify", "status": "ok"},
    "run": {"clarify_rounds": 0},
    "x": False, "y": True, "z": True,
    "m": False, "n": False,
}

# ---- table-driven: one row per grammar feature -------------------------------

CASES = [
    # numeric comparisons, symbol and keyword operator spellings
    ("story_points == 5", True),
    ("story_points eq 5", True),
    ("story_points != 6", True),
    ("story_points ne 6", True),
    ("story_points < 8", True),
    ("story_points lt 8", True),
    ("story_points <= 5", True),
    ("story_points lte 5", True),
    ("story_points > 3", True),
    ("story_points gt 3", True),
    ("story_points >= 5", True),
    ("story_points gte 5", True),
    ("story_points > 5", False),
    ("story_points < 5", False),
    # bare IDENT operand is a string literal; eq/ne are case-insensitive
    ("issue_type == Bug", True),
    ("issue_type == bug", True),
    ("issue_type != Defect", True),
    # in / not in / contains
    ("issue_type in [Bug, Defect]", True),
    ("issue_type in [Defect, Task]", False),
    ("issue_type not in [Defect, Task]", True),
    ("issue_type not in [Bug, Defect]", False),
    ("labels contains agent", True),
    ("labels contains nope", False),
    # is empty / is not empty
    ("workitem.acceptance is empty", True),
    ("workitem.acceptance is not empty", False),
    ("workitem.title is empty", False),
    ("workitem.title is not empty", True),
    # ordered enums, dotted and bare path
    ("workitem.ambiguity >= medium", True),
    ("workitem.ambiguity >= high", False),
    ("workitem.ambiguity > low", True),
    ("ambiguity >= medium", True),
    ("size >= s", True),
    ("size < s", False),
    ("size <= xl", True),
    # missing paths
    ("nonexistent.path is empty", True),
    ("nonexistent.path is not empty", False),
    ("nonexistent.path == 5", False),
    ("nonexistent.path != 5", True),
    ("nonexistent.path > 5", False),
    ("nonexistent.path", False),
    # mismatched-type ordering never raises, always False
    ("story_points > Bug", False),
    # bare truthy path
    ("story_points", True),
    ("diff.touches_security", False),
    # keyword literals: yes/no map to True/False, forgiving of string "yes"/"no" context values
    ("plan.security == yes", True),
    ("plan.security == no", False),
    # and / or / not, with the two examples straight from section-2.md
    ("workitem.acceptance is empty or workitem.ambiguity >= medium", True),
    ("not (story_points > 5) and issue_type in [Bug, Defect]", True),
    ("plan.security == yes or diff.touches_security", True),
    # precedence: 'and' binds tighter than 'or'
    ("x and y or z", True),   # (x and y) or z = (F and T) or T = True
    ("x and (y or z)", False),  # explicit grouping forces the other reading
    # precedence: 'not' binds tighter than 'and'
    ("not m and n", False),   # (not m) and n = (not F) and F = True and False = False
    ("not (m and n)", True),  # explicit grouping: not(F and F) = not F = True
    # parentheses
    ("(x and y) or z", True),
]


@pytest.mark.parametrize("expr,expected", CASES, ids=[c[0] for c in CASES])
def test_evaluate_table(expr, expected):
    assert evaluate(expr, CTX) is expected


def test_evaluate_none_context_value_reads_as_missing_not_error():
    ctx = {"workitem": {"assignee": None}}
    assert evaluate("workitem.assignee is empty", ctx) is True
    assert evaluate("workitem.assignee == someone", ctx) is False


def test_evaluate_non_string_raises_predicate_error():
    with pytest.raises(PredicateError):
        evaluate(123, CTX)  # type: ignore[arg-type]


# ---- evaluate_any -------------------------------------------------------------


def test_evaluate_any_none_is_true():
    assert evaluate_any(None, CTX) is True


def test_evaluate_any_empty_string_is_true():
    assert evaluate_any("", CTX) is True
    assert evaluate_any("   ", CTX) is True


def test_evaluate_any_string_delegates_to_evaluate():
    assert evaluate_any("story_points == 5", CTX) is True
    assert evaluate_any("story_points == 6", CTX) is False


def test_evaluate_any_mapping_delegates_to_evaluate_mapping():
    assert evaluate_any({"story_points": {"lte": 5}}, CTX) is True
    assert evaluate_any({"story_points": {"lte": 2}}, CTX) is False


def test_evaluate_any_rejects_other_types():
    with pytest.raises(PredicateError):
        evaluate_any(123, CTX)  # type: ignore[arg-type]


# ---- evaluate_mapping -----------------------------------------------------------


def test_evaluate_mapping_all_keys_must_hold():
    assert evaluate_mapping({"story_points": {"lte": 5}, "issue_type": "Bug"}, CTX) is True
    assert evaluate_mapping({"story_points": {"lte": 2}, "issue_type": "Bug"}, CTX) is False


def test_evaluate_mapping_bare_equality():
    assert evaluate_mapping({"issue_type": "Bug"}, CTX) is True
    assert evaluate_mapping({"issue_type": "Defect"}, CTX) is False


def test_evaluate_mapping_contains():
    assert evaluate_mapping({"labels": {"contains": "agent"}}, CTX) is True
    assert evaluate_mapping({"labels": {"contains": "nope"}}, CTX) is False


def test_evaluate_mapping_in():
    assert evaluate_mapping({"issue_type": {"in": ["Bug", "Defect"]}}, CTX) is True
    assert evaluate_mapping({"issue_type": {"in": ["Defect", "Task"]}}, CTX) is False


def test_evaluate_mapping_empty_and_not_empty():
    assert evaluate_mapping({"workitem.acceptance": {"empty": True}}, CTX) is True
    assert evaluate_mapping({"workitem.acceptance": {"not_empty": True}}, CTX) is False
    assert evaluate_mapping({"workitem.title": {"not_empty": True}}, CTX) is True


def test_evaluate_mapping_dotted_path_key():
    assert evaluate_mapping({"workitem.status": "In Progress"}, CTX) is True


def test_evaluate_mapping_rejects_multi_key_rule():
    with pytest.raises(PredicateError):
        evaluate_mapping({"story_points": {"lte": 5, "gte": 1}}, CTX)


def test_evaluate_mapping_unknown_operator_raises():
    with pytest.raises(PredicateError):
        evaluate_mapping({"story_points": {"bogus": 5}}, CTX)


def test_evaluate_mapping_requires_a_mapping():
    with pytest.raises(PredicateError):
        evaluate_mapping("not a mapping", CTX)  # type: ignore[arg-type]


# ---- describe_mapping -----------------------------------------------------------


def test_describe_mapping_lte():
    assert describe_mapping({"story_points": {"lte": 5}}) == "story_points <= 5"


def test_describe_mapping_bare_equality():
    assert describe_mapping({"issue_type": "Bug"}) == "issue_type == Bug"


def test_describe_mapping_multiple_keys_joined_with_and():
    result = describe_mapping({"story_points": {"lte": 2}, "issue_type": "Bug"})
    assert result == "story_points <= 2 and issue_type == Bug"


def test_describe_mapping_contains():
    assert describe_mapping({"labels": {"contains": "agent"}}) == "labels contains agent"


def test_describe_mapping_in_list():
    result = describe_mapping({"issue_type": {"in": ["Bug", "Defect"]}})
    assert result == "issue_type in [Bug, Defect]"


def test_describe_mapping_empty():
    assert describe_mapping({"workitem.acceptance": {"empty": True}}) == "workitem.acceptance is empty"


# ---- MISSING sentinel -----------------------------------------------------------


def test_missing_is_falsey_and_singleton():
    assert bool(MISSING) is False
    from ticketbot.core.predicate import MISSING as MISSING2
    assert MISSING is MISSING2


# ---- hostile inputs: must never execute anything, must fail safely -------------


HOSTILE_INPUTS = [
    "__import__('os').system('echo pwned')",
    "1; import os",
    "exec('x')",
    "eval('1+1')",
    "(" * 100 + "a" + ")" * 100,
    "a" * (MAX_EXPR_LEN + 1),
    " or ".join(["workitem.title"] * 501),
]


@pytest.mark.parametrize("expr", HOSTILE_INPUTS, ids=[f"hostile-{i}" for i in range(len(HOSTILE_INPUTS))])
def test_hostile_inputs_raise_predicate_error_never_execute(expr):
    with pytest.raises(PredicateError):
        evaluate(expr, CTX)


def test_hostile_paren_nesting_raises_before_python_recursion_limit():
    expr = "(" * 5000 + "a" + ")" * 5000
    with pytest.raises(PredicateError):
        evaluate(expr, CTX)


def test_max_expr_len_enforced():
    expr = "a" * (MAX_EXPR_LEN + 1)
    with pytest.raises(PredicateError):
        evaluate(expr, CTX)


def test_max_tokens_enforced_with_many_chained_ors():
    # 250 idents + 249 'or's = 499 tokens (> MAX_TOKENS), while staying under
    # MAX_EXPR_LEN so this test isolates the token-count limit specifically.
    expr = " or ".join(["x"] * 250)
    assert len(expr) < MAX_EXPR_LEN
    with pytest.raises(PredicateError):
        evaluate(expr, CTX)


def test_hostile_500_chained_ors_rejected():
    # The literal hostile case from section-2.md: 500 chained 'or's. This string is
    # long enough to also trip MAX_EXPR_LEN, but either limit rejecting it safely is
    # the point — it must never be evaluated.
    expr = " or ".join(["x"] * 501)
    with pytest.raises(PredicateError):
        evaluate(expr, CTX)


def test_max_depth_enforced_for_deeply_nested_parens():
    depth = MAX_DEPTH + 5
    expr = "(" * depth + "x" + ")" * depth
    with pytest.raises(PredicateError):
        evaluate(expr, CTX)


def test_unterminated_string_raises_predicate_error_not_bare_exception():
    with pytest.raises(PredicateError):
        evaluate('issue_type == "unterminated', CTX)


def test_unexpected_character_raises_predicate_error():
    with pytest.raises(PredicateError):
        evaluate("issue_type == Bug; DROP TABLE tickets", CTX)


def test_predicate_module_never_calls_eval_exec_or_compile():
    source = inspect.getsource(predicate)
    # Substrings that would indicate an actual call, not merely prose in a comment
    # or docstring explaining why these are forbidden.
    assert "eval(" not in source
    assert "exec(" not in source
    assert "compile(" not in source
    assert "literal_eval(" not in source
    assert "__import__(" not in source

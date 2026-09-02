from pathlib import Path

from ticketbot.core.templating import lookup, missing_placeholders, render


def test_render_simple_placeholder_substitutes():
    assert render("Hello {name}!", {"name": "world"}) == "Hello world!"


def test_render_dotted_path_walks_nested_mapping():
    values = {"workitem": {"title": "Login times out", "meta": {"points": 5}}}
    assert render("{workitem.title}", values) == "Login times out"
    assert render("{workitem.meta.points}", values) == "5"


def test_render_dotted_path_walks_object_attributes():
    class Obj:
        def __init__(self):
            self.title = "from object"

    assert render("{item.title}", {"item": Obj()}) == "from object"


def test_render_escaped_braces_unescape_to_literal():
    assert render("use {{literal}} braces", {}) == "use {literal} braces"
    assert render("{{just a brace", {}) == "{just a brace"
    assert render("just a brace}}", {}) == "just a brace}"


def test_render_unknown_placeholder_preserved_verbatim():
    assert render("Hello {nope}!", {"name": "world"}) == "Hello {nope}!"


def test_render_json_body_with_real_braces_round_trips_unharmed():
    # Braces that are not immediately doubled ("{{"/"}}") and whose contents are not
    # a bare dotted identifier never match PLACEHOLDER, so JSON text passes through.
    template = 'Payload: {"key": "value", "flag": true} and {"other": 2} done'
    assert render(template, {"key": "ignored"}) == template


def test_render_none_value_is_empty_string():
    assert render("acceptance=[{acceptance}]", {"acceptance": None}) == "acceptance=[]"


def test_render_list_value_joined_with_comma_space():
    assert render("labels: {labels}", {"labels": ["agent", "sso"]}) == "labels: agent, sso"


def test_render_path_value_uses_str():
    p = Path("repo") / "sub"
    assert render("{p}", {"p": p}) == str(p)


def test_render_does_not_recursively_expand_substituted_value():
    # the substituted value itself contains a placeholder-looking string;
    # it must NOT be expanded again.
    values = {"a": "{b}", "b": "should not appear"}
    assert render("{a}", values) == "{b}"


def test_render_substituted_json_value_with_braces_not_reexpanded():
    values = {"body": '{"x": 1}', "x": "should not appear"}
    assert render("payload={body}", values) == 'payload={"x": 1}'


def test_missing_placeholders_lists_only_absent_names():
    template = "{present} and {absent} and {also.absent}"
    result = missing_placeholders(template, {"present": "here"})
    assert result == ["absent", "also.absent"]


def test_missing_placeholders_empty_when_all_present():
    template = "{a} {b.c}"
    values = {"a": 1, "b": {"c": 2}}
    assert missing_placeholders(template, values) == []


def test_missing_placeholders_ignores_escaped_braces():
    assert missing_placeholders("{{not a placeholder}}", {}) == []


def test_missing_placeholders_deduplicates():
    template = "{a} then {a} again"
    assert missing_placeholders(template, {}) == ["a"]


def test_lookup_returns_none_for_missing_path():
    assert lookup({"a": {"b": 1}}, "a.c") is None
    assert lookup({}, "a.b.c") is None


def test_lookup_returns_value_for_present_path():
    assert lookup({"a": {"b": 1}}, "a.b") == 1


def test_lookup_walks_object_attributes():
    class Obj:
        pass

    o = Obj()
    o.x = 42
    assert lookup({"o": o}, "o.x") == 42

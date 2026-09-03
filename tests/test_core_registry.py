import pytest

from ticketbot.config.schema import AdapterConfig
from ticketbot.core.registry import (
    EXECUTORS,
    MODELS,
    REPOS,
    RUNTIMES,
    SINKS,
    SOURCES,
    Registry,
    RegistryError,
)
from tests.fixtures.dummy_adapters import DummyAdapter


def test_register_get_create_with_class_target():
    registry = Registry("widget")
    registry.register("dummy", DummyAdapter)

    assert registry.get("dummy") is DummyAdapter

    cfg = AdapterConfig(type="dummy", option="value")
    instance = registry.create(cfg, extra=1)
    assert isinstance(instance, DummyAdapter)
    assert instance.cfg is cfg
    assert instance.kwargs == {"extra": 1}


def test_register_get_create_with_string_target():
    registry = Registry("widget")
    registry.register("dummy", "tests.fixtures.dummy_adapters:DummyAdapter")

    resolved = registry.get("dummy")
    assert resolved is DummyAdapter

    cfg = AdapterConfig(type="dummy")
    instance = registry.create(cfg)
    assert isinstance(instance, DummyAdapter)


def test_names_returns_sorted_registered_names():
    registry = Registry("widget")
    registry.register("zeta", DummyAdapter)
    registry.register("alpha", DummyAdapter)
    assert registry.names() == ["alpha", "zeta"]


def test_unknown_name_raises_registry_error_listing_known_names():
    registry = Registry("widget")
    registry.register("known-one", DummyAdapter)
    registry.register("known-two", DummyAdapter)

    with pytest.raises(RegistryError) as exc_info:
        registry.get("unknown")

    message = str(exc_info.value)
    assert "unknown" in message
    assert "known-one" in message
    assert "known-two" in message


def test_import_error_surfaces_as_registry_error_naming_the_extra():
    registry = Registry("runtime")
    registry.register("solari", "tests.fixtures.totally_missing_module:SolariRuntime")

    with pytest.raises(RegistryError) as exc_info:
        registry.get("solari")

    message = str(exc_info.value)
    assert "solari" in message
    assert "pip install ticketbot[solari]" in message


def test_invalid_target_string_without_colon_raises_registry_error():
    registry = Registry("widget")
    registry.register("bad", "not-a-valid-target")

    with pytest.raises(RegistryError):
        registry.get("bad")


def test_missing_class_attribute_raises_registry_error():
    registry = Registry("widget")
    registry.register("bad-class", "tests.fixtures.dummy_adapters:NoSuchClass")

    with pytest.raises(RegistryError):
        registry.get("bad-class")


# ---- built-in registrations exist, without importing the (not-yet-written) targets ----


@pytest.mark.parametrize(
    "registry,name",
    [
        (SOURCES, "file"),
        (SOURCES, "jira"),
        (SINKS, "file"),
        (SINKS, "jira"),
        (SINKS, "github_pr"),
        (RUNTIMES, "none"),
        (RUNTIMES, "local_shell"),
        (RUNTIMES, "solari"),
        (REPOS, "git_local"),
        (REPOS, "github"),
        (MODELS, "anthropic"),
        (MODELS, "openai_compat"),
        (MODELS, "fake"),
        (EXECUTORS, "process"),
        (EXECUTORS, "api"),
    ],
)
def test_builtin_registrations_are_present(registry, name):
    assert name in registry.names()

# The `type:`-string registry contract

`ticketbot/core/registry.py` maps an adapter family plus a `type:` name to a class, imported with
`importlib` at first use. It is the reason the config schema never has to learn about adapter kinds
and the reason optional dependencies (the Solari SDKs) do not have to be installed to import the
package.

## Shape

```python
SOURCES = Registry("source")   # file, jira
SINKS = Registry("sink")       # file, jira, github_pr
RUNTIMES = Registry("runtime") # none, local_shell, solari
REPOS = Registry("repo")       # git_local, github
MODELS = Registry("model")     # anthropic, openai_compat, fake
EXECUTORS = Registry("executor")  # process, api

SOURCES.register("jira", "ticketbot.adapters.sources.jira:JiraSource")
```

A target is either a `"module:ClassName"` string (imported lazily) or a class object.
`Registry.get(name)` raises `RegistryError` for an unknown name, listing the registered names, and
turns an `ImportError` into `'<family> type "<name>" needs: pip install ticketbot[<name>]'` — the
registry key doubles as the extra name, which is why the `solari` runtime key matches the
`[solari]` extra.

`Registry.create(cfg, **kwargs)` is `get(cfg.type)(cfg, **kwargs)`.

## Construction: one call site, every adapter

`Orchestrator._instantiate()` filters kwargs through `inspect.signature`, so a single call site
builds `FileSource(cfg, base_dir=...)`, `JiraSource(cfg, client=...)` and
`GithubRepo(cfg, base_dir=..., run_dir=..., client=...)` alike:

```python
def _instantiate(registry, cfg, **kwargs):
    cls = registry.get(cfg.type)
    return cls(cfg, **_filtered_kwargs(cls, kwargs))
```

Consequence: an adapter constructor may declare any subset of `base_dir`, `run_dir`, `client`,
`provider`, `runtime`, `connection`, `pr_url_getter` and get exactly what it asked for. Adding a new
keyword to the call site cannot break an adapter that does not want it.

## Adding an adapter — the whole checklist

1. New module under the family's directory; a class implementing that family's protocol
   ([adapter-protocols.md](adapter-protocols.md)).
2. `describe() -> str` — the banner is built from it, and `tests/test_seams.py` imports every
   registered target and asserts it has one.
3. Validate the adapter's own options in `__init__` (raise the family's error type). Do NOT touch
   `config/schema.py` — adapter blocks are permissive `{type: ..., ...}` by design.
4. One `register()` line in `core/registry.py`.
5. `${ENV}` refs expanded in `__init__` (or later, if `validate` must work without the variable set
   — `SolariRuntime` keeps `api_key_ref` unexpanded until `start()` for exactly that reason) and
   `register_secret()`'d.
6. README: a row in the swap-point table and any new env var in the install table.
7. Tests: the adapter's own `test_*.py`; the seams file already covers registry agreement.

## Invariants

- Every registered name must import to a class with `describe()` — asserted in
  `tests/test_seams.py::test_every_registered_target_imports_and_describes_itself`.
- Every registered adapter must satisfy its `runtime_checkable` protocol — asserted in
  `tests/test_seams.py::test_every_registered_adapter_satisfies_its_protocol`.
- Registration is import-time and unconditional; the module behind it must import cleanly without its
  optional dependency installed (lazy SDK imports inside functions).

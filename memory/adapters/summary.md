# Adapters

Four families live under `ticketbot/adapters/`, one directory each: `sources/`, `sinks/`,
`runtimes/`, `repos/`. (Models and executors are separate top-level packages but follow the same
registry rules.) Protocol shapes are in
[../architecture/adapter-protocols.md](../architecture/adapter-protocols.md); the registry contract
and the "how to add one" checklist are in [../architecture/registry.md](../architecture/registry.md).

| Family | Kinds | Detail |
|---|---|---|
| source | `file`, `jira` | [sources.md](sources.md) |
| sink | `file`, `jira`, `github_pr` | [sinks.md](sinks.md) |
| repo | `git_local`, `github` | [repos.md](repos.md) |
| runtime | `none`, `local_shell`, `solari` | [runtimes.md](runtimes.md) |

## Rules every adapter obeys

- Validate your own options in `__init__`; raise the family's error type. Never touch
  `config/schema.py`.
- Expand `${ENV}` refs at use time and `register_secret()` the result immediately.
- `describe()` returns a short human string; the banner is built from it.
- Own your resources and release them in `close()`/`cleanup()`. The engine closes what it opens.
- Anything outward-facing goes through `subprocess` with `shell=False` and an argv list, or through
  an injectable `httpx.Client` so tests can use `MockTransport`.
- Optional capabilities are extra methods the caller `getattr`-probes, never protocol members.

## Injectable seams (what tests pass in)

| Adapter | Kwarg |
|---|---|
| `FileSource`, `GitLocalRepo`, `GithubRepo` | `base_dir`, `run_dir` |
| `JiraSource`, `JiraSink` | `client` (an `httpx.Client`) or a whole `connection` |
| `GithubPrSink`, `GithubRepo`, `OpenAICompatProvider` | `client` |
| `GithubPrSink` | `pr_url_getter` |
| `FileSink` | `run_dir` (the default output directory) |
| `ApiLoopExecutor` | `provider`, `runtime` |
| `LocalShellRuntime` | `root` (wins over `cfg.opt("root")`) |

# Secrets and redaction

## The one legal form

A credential appears in a profile only as a `${ENV_VAR}` reference. The loader never interprets the
name and never expands it — `${ANY_NAME}` works anywhere. Expansion happens in the ADAPTER that
needs the value, at construction (or later), through `config/loader.py: expand_env()`, which raises
`MissingEnvError` naming the variable when it is unset or empty.

Consequences that are load-bearing:

- `ticketbot validate` / `config show` / `config banner` succeed with no environment set at all.
- `runs/<id>/config.resolved.yaml` holds the profile with `${ENV}` refs still unexpanded, and is
  additionally passed through `redact()` before being written.
- An adapter that must not raise at construction keeps the ref instead: `SolariRuntime` stores
  `api_key_ref` and expands it inside `start()`.

Env vars the shipped profiles use: `ANTHROPIC_API_KEY`, `JIRA_EMAIL`, `JIRA_API_TOKEN`,
`JIRA_BOT_ACCOUNT_ID`, `GITHUB_TOKEN`, `SOLARI_API_KEY`, `MODEL_BASE_URL`, `MODEL_API_KEY`,
`PEER_BASE_URL`, `PEER_API_KEY`.

## Redaction — `config/redact.py`

Two layers, both applied by `Redactor.scrub()`:

1. **Patterns** for known token shapes: Anthropic `sk-ant-`, Solari `slr_live_`/`slr_test_`, GitHub
   `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` and `github_pat_`, Atlassian `ATATT`, OpenAI `sk-`/`sk-proj-`,
   and an `Authorization: Bearer|Basic` header form (group-preserving).
2. **Registered literal values** — every secret an adapter expanded, remembered via
   `register_secret(value)` (values under 8 chars or blank are ignored) and replaced verbatim.

```python
register_secret(token)   # populates the ONE process-wide Redactor
redact(text)             # scrubs through that same instance
default_redactor()       # the instance itself, for code that scrubs on its own
```

**The trap:** a private `Redactor()` sees no registered secrets and falls back to pattern matching
alone. `RunStore` used one, so every credential the adapters registered (Jira base url/email/token,
`GITHUB_TOKEN`, `ANTHROPIC_API_KEY`, `SOLARI_API_KEY`, every value expanded into a `process`
executor's `env:`) was written verbatim into run artifacts and logs. `RunStore` now defaults to
`default_redactor()`, looked up at construction so a test that monkeypatches the module-level
instance still isolates correctly.

**Rule: anything that scrubs without calling `redact()` must take `default_redactor()`.**

## Who scrubs what

| Writer | Scrubs via |
|---|---|
| `RunStore.write_artifact` / `append_log` | `self.redactor` (= `default_redactor()`) — str data only; `bytes` are written verbatim |
| `FileSink` | `redact()` on every appended line |
| `ProcessExecutor` | `redact()` on stdout, stderr and the error message |
| `ApiLoopExecutor` | `redact()` on every log line |
| `tools.dispatch` | `redact()` on every tool result AND every tool error |
| repo/sink/source adapters | `redact()` on any HTTP body snippet or `git`/`gh` stderr in an error |
| CLI | `redact()` on the banner, on `config show` output, and on every printed error |

## Outbound content is scrubbed too, not just errors and local writes

Everything above except the last two rows writes LOCALLY. The paths that leave the machine carry
model-written text — the reporter's `ticket_comment.md`, a `QUESTION:` block, `pr.md`, a step's
returned summary as a commit body — and a ticket or a public PR is readable by whoever filed the
work item. Scrubbing the local copy while posting the identical string in the clear made the remote
copy the leakiest artifact in the system, so each outbound boundary scrubs:

| Boundary | Scrubs |
|---|---|
| `JiraSink.comment` | the comment body, **before** `markdown_to_adf` |
| `JiraSink.link` | the remote-link url and title |
| `GithubPrSink.comment` | the comment body (after the attachment refs are appended) |
| `GithubRepo.open_pr` | the PR title and body |
| `GitLocalRepo._compose_message` | the whole commit message — `push()` publishes it |

**Order matters for Jira.** Scrub BEFORE the ADF conversion, never after. Scrubbing the finished ADF
tree keeps the `***REDACTED***` marker intact as literal text (after the conversion it is re-read as
markdown bold and lands as a `strong` node saying `REDACTED`), but it is defeatable: `adf.py`'s
`_INLINE` splits on `*` and `_`, so a `github_pat_…` or `sk-proj-…` token is already broken across
several text nodes by then and no pattern matches either half.

**A base URL is not a credential.** `JiraConnection` registers `email` and `token` only. The tenant
host is a substring of every `{base_url}/browse/KEY` ticket URL, so registering it as a literal
secret rewrote that URL to `***REDACTED***` in artifacts — and, now that comments are scrubbed,
would corrupt the comment posted back to the ticket.

## Other handling rules

- `run_git()` error messages name the argv and the exit code, never the child environment.
- A token is never embedded in a git remote URL; auth is a credential helper, SSH agent, or the
  `gh` CLI's own login.
- `ProcessExecutor` builds the child environment from `DEFAULT_PASSTHROUGH` plus the profile's
  declared `env:`/`env_passthrough:` — never `os.environ` wholesale, so an API key in the parent
  process is not handed to an arbitrary subprocess. `DEFAULT_PASSTHROUGH` carries only non-secret
  locators so the spawned CLI can find its OWN credential store; a forwarded name that reads like
  a credential is `register_secret()`'d. See
  [../executors/summary.md](../executors/summary.md) for the full contract.
- `tests/test_repo_hygiene.py` asserts no shipped profile contains a secret-shaped literal.

# Sinks

Where a run's results are reported. The profile's `sink:` block names the primary and an `also:` list;
`Orchestrator._build_sink` wraps them in a `MultiSink`.

## `MultiSink` and `DryRunSink` — `adapters/sinks/base.py`

`MultiSink` calls the **primary first and lets its exception propagate** — a broken primary is never
silently swallowed. Each secondary is then called in order; a secondary's exception is caught,
logged, reported through the `on_error` callback and never allowed to stop the remaining secondaries
or lose the primary's result (a broken GitHub token must not lose the Jira comment). `close()` is
best-effort across all of them. `set_pr_url()` is fanned out by `getattr` probe rather than
`_fan_out`, because it is a hand-off, not a report — see [../engine/reporting.md](../engine/reporting.md).

`DryRunSink` records one line per intended call in `self.calls` and in `dryrun.log`, and never calls
the wrapped sink at all (only its `describe()`). That is why the engine closes the sink it BUILT, not
the wrapper.

## `FileSink` — `adapters/sinks/file.py`

Writes into `cfg.opt("dir")` or, by default, the run directory.

- `comment()` appends to `ticket_comment.md`, prefixed with a `---` separator rule when the file
  already exists; copies attachments into `attachments/` — filenames reduced to a basename and the
  resolved destination re-checked, so a `../` filename cannot escape; and appends a one-line summary
  to `result.md`.
- `transition()`, `unassign()`, `link()` append one line each to `result.md`.
- Everything it writes goes through `redact()`.

The append behaviour is why the engine writes the canonical `ticket_comment.md` AFTER the sink call.

## `JiraSink` — `adapters/sinks/jira.py`

Shares `JiraConnection` with `JiraSource`. Attachments are uploaded FIRST (so the comment can
reference them, with the `X-Atlassian-Token: no-check` header); an upload failure is logged and
appended to the comment as a note rather than aborting it. The comment body is converted from
markdown to ADF. `transition()` looks the target up by `to.name` case-insensitively and raises
`SinkError` listing the available targets when there is no match. `unassign()` PUTs a null
`accountId`. `link()` posts a remote link.

## `GithubPrSink` — `adapters/sinks/github_pr.py`

Posts comments onto an EXISTING pull request; it never opens, advances or finalizes one.
`gh pr comment` when `gh` is on PATH and `prefer_gh` is true, else REST
(`/repos/{owner}/{repo}/issues/{number}/comments`).

**Until it knows the PR URL it logs and DROPS every comment.** The URL arrives via `set_pr_url(url)`
(or an injected `pr_url_getter`), and the orchestrator must call that before the first report — see
[../engine/reporting.md](../engine/reporting.md). GitHub comments cannot carry uploads, so
attachments are referenced by local path in the body instead. `transition()` and `unassign()` are
deliberate no-ops; `link()` is a comment.

`github_rest_headers(token)` and `write_body_tempfile(body)` live here and are reused by the `github`
REPO adapter, so the two GitHub clients cannot diverge on auth/version headers or on the "model text
never reaches a command line inline" rule.

## ADF — `adapters/sinks/adf.py`

Jira comment and description bodies are ADF, not markdown. `markdown_to_adf` converts the subset that
matters (paragraphs, headings, fenced code, bullet and ordered lists, links, bold, italic, inline
code, horizontal rules) and degrades safely: anything the inline parser cannot handle stays literal
text (never guessed at), only http(s) hrefs are recognised as links, and if the whole conversion
raises, the result falls back to a single readable code block rather than posting broken markup.
`adf_to_text` is the inverse, used on untrusted Jira responses: depth- and node-bounded
(`_MAX_DEPTH = 6`, `_MAX_NODES = 5000`) and it never raises however malformed the document is.

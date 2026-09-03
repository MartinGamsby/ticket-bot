# Sources

Where a `WorkItem` comes from. `claim()` means "assign this to the bot and move it to In Progress";
returning `False` (someone else already claimed it) is how two pollers avoid duplicating work.

## `FileSource` — `adapters/sources/file.py`

The default, and what keeps the whole pipeline runnable offline. Options: `path`, `text`, `glob`
(default `inbox/*.md`), `processed_dir` (default `inbox/processed`), `encoding`. Relative paths
resolve against `base_dir` (the profile's directory), never cwd.

- `fetch()` precedence: an `external_id` that exists as a path -> `cfg.path` -> `cfg.text` ->
  `WorkItemNotFound`.
- `poll()` globs, sorts by mtime (oldest first) and skips anything already under `processed_dir`.
- `claim()` is always `True` — there is no contention on a local file.
- `mark_processed(item)` moves the file into `processed_dir`. The ORCHESTRATOR decides when to call
  it (once the run is terminal); without that call every sweep re-yields every inbox file forever.

Front matter is UNTRUSTED input: a leading `---` block parsed with `yaml.safe_load` only; a parse
error or a non-mapping raises `SourceError` naming the file rather than crashing the poller; unknown
keys land in `WorkItem.raw`, never on an attribute and never on a filesystem path. Known keys:
`key`, `title`, `type`, `points`, `labels`, `acceptance`, `url`, `status`, `assignee`. A
non-numeric `points` warns and is ignored. Title falls back to the first `# ` heading, then the first
non-blank line, then the file stem, then `"untitled task"`.

`Orchestrator._resolve_source` constructs a `FileSource` directly for `--input-text`/`--input`,
overriding whatever source the profile configures — the one deliberate direct adapter import in the
engine.

## `JiraSource` — `adapters/sources/jira.py`

Jira Cloud REST v3. Options: `base_url`, `email`, `token`, `jql`, `poll_seconds` (60),
`points_field` (the story-point custom field id differs per instance), `max_results` (50),
`account_id`, `in_progress_status` (default `"In Progress"`).

`JiraConnection` is the ONE place the Jira `httpx.Client` is built (basic auth from `email`/`token`);
`JiraSink` reuses it. `email` and `token` are expanded and `register_secret()`'d at construction;
`base_url` deliberately is NOT — a tenant host is not a credential, and it is a substring of every
`{base_url}/browse/KEY` ticket URL, so registering it rewrote that URL to `***REDACTED***` in every
artifact and would now corrupt the outbound ticket comment. The `Authorization` header is never read
back or logged. Every non-2xx raises with the status, Jira's `errorMessages` when present, and a
redacted body snippet, and carries `status_code` so callers can special-case 404.

- `fetch(key)` requires a key; a 404 becomes `WorkItemNotFound`.
- `poll()` POSTs `/search/jql` and pages via `nextPageToken`.
- `claim()` re-fetches first and returns `False` without touching anything when the issue is already
  assigned to someone other than `account_id`; then assigns (a missing `account_id` logs a warning
  and makes assignment advisory) and transitions (a missing transition is a warning, not an error).
- No `mark_processed` — `claim()` already moved the issue out of the polled JQL.
- `download_attachment()` streams with a 25 MB cap, reduces the Jira-supplied filename to its
  basename, re-checks the resolved destination against `dest_dir`, and deletes any partial file on
  failure.

Descriptions and comments arrive as ADF and are flattened by `adapters/sinks/adf.py: adf_to_text`;
acceptance criteria are extracted from the raw ADF tree by finding an h2/h3 heading matching
"acceptance criteria" (headings are indistinguishable once flattened).

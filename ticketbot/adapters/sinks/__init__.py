"""ticketbot.adapters.sinks: where a run's results go.

`FileSink` (the default -- `result.md`/`ticket_comment.md` under the run
directory), `JiraSink` (comments as ADF, transitions, unassign, remote links) and
`GithubPrSink` (PR comments; never creates or lands a PR itself) all implement the
`Sink` protocol in `sinks.base`. `MultiSink` fans a call out to a primary plus the
profile's `also:` list; `DryRunSink` records instead of calling for `--dry-run`.
"""

"""ticketbot.adapters.runtimes: where a step's shell commands run and where its
screenshots come from.

`NoneRuntime` (no-op, keeps pipelines completing without a runtime configured),
`LocalShellRuntime` (jailed subprocess on this machine) and `SolariRuntime`
(cloud sandbox/browser/desktop behind `SOLARI_API_KEY`) all implement the sync
`Runtime` protocol in `runtimes.base` — Solari is not a model; it is where code
runs and where screenshots come from.
"""

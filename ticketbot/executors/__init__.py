"""ticketbot.executors: how a pipeline step's work actually gets done.

`ProcessExecutor` spawns a coding CLI defined entirely by config (argv, prompt
delivery, env allowlist). `ApiLoopExecutor` runs our own path-jailed tool loop over
a `ModelProvider`. Both implement the same `Executor` protocol (`executors.base`),
which is what makes swapping "which AI does the work" a one-line config change.
"""

"""ticketbot.adapters.repos: get an isolated workspace, commit each pipeline step,
produce a diff, push, and open a pull request (`git_local`, `github`).

The one section of this codebase that touches a real `git`/`gh` subprocess. Every
invocation is an argv list with `shell=False` -- never a composed command string --
because branch names and commit/PR text are derived from untrusted ticket and model
output.
"""

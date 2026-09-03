Context
- Repo root (use ABSOLUTE paths under it for every Read/Edit/Write, and pass it to git/run commands
  e.g. `git -C "{workspace}" ...`): {workspace}
- WORKING DIRECTORY WARNING: your shell and file tools may default to somewhere other than the
  workspace above. Always use absolute paths under it. Before finishing, re-read one file you wrote,
  by absolute path, to confirm it landed in the workspace.
- Platform: {platform}
- Repo root of record: {repo_root}
- Run directory (write artifacts here, absolute): {run_dir}
- Ticket: {ticket_key} — {ticket_title} ({ticket_type}, {ticket_points} points) {ticket_url}
- Task (verbatim):
{task}
- Read these first — they hold the context you need:
{context_paths}
- {python_note}

{git_prohibition}

{question_protocol}

You are the REPORTER. The work is done. Write the two write-ups — they have different audiences and
must NOT be the same text.

Inputs:
- Plan: {plan_file}
- Test report: {test_report}
- Review: {review_file}
- Diff:
{diff}
- Screenshots (run-relative paths): {screenshots}

1. Write {run_dir}/pr.md — the FULL pull-request body: context (what the ticket asked for), the
   approach taken, the files touched grouped by area, the test plan and its result, risks and
   follow-ups. Markdown headings. This can be long.

2. Write {run_dir}/ticket_comment.md — the SHORT comment for {ticket_key}. Six lines or fewer plus the
   links. It is NOT a summary of the PR description; it answers "what changed, is it verified, where
   do I look":
     - one line on what changed
     - one line on how it was verified (tests, screenshots)
     - the screenshots, if any
     - the PR link line, written exactly as: PR: {pr_url}
     - one line on anything the reviewer should watch for

Return ONLY: the two file paths you wrote and the three-line version of the ticket comment.

<!-- {pr_url} is empty at the time this step runs, because the PR is not open yet -- `publish` writes
     ticket_comment.md before repo.open_pr() has a URL to give it. The orchestrator substitutes the
     real PR URL into the written ticket_comment.md after open_pr() returns, so write the "PR: {pr_url}"
     line exactly as instructed above even when it renders blank here. -->

"""ticketbot.adapters.sources: where a `WorkItem` comes from.

`FileSource` (a single file, inline text, or a poll glob of front-matter markdown
files -- the default, so the whole pipeline runs offline) and `JiraSource` (Jira
Cloud REST v3) both implement the `Source` protocol in `sources.base`.
"""

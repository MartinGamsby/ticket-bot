# Pipeline selection

`engine/selector.py: select(profile, item) -> Selection(ref, reason)` decides which pipeline YAML a
work item runs, before the run exists.

- `profile.pipeline_selector.rules` are evaluated IN ORDER; the first whose `when:` MAPPING holds
  wins, and its human-readable `describe_mapping()` text becomes the reason.
- No rule matches, falls through to `pipeline_selector.default`, reason `"default"`.
- The reason lands in `run.pipeline_reason` and in the banner:
  `pipeline=builtin:pipelines/standard.yaml  (rule: story_points <= 5)`.
- An invalid rule raises `ConfigError` naming the rule index.

```yaml
pipeline_selector:
  rules:
    - when: {story_points: {lte: 2}, issue_type: Bug}
      use: builtin:pipelines/small-bug.yaml
    - when: {story_points: {lte: 5}}
      use: builtin:pipelines/standard.yaml
    - when: {story_points: {gte: 8}}
      use: builtin:pipelines/large-with-clarification.yaml
  default: builtin:pipelines/standard.yaml
```

## The throwaway `Run`

No `Run` exists yet at selection time (it is created immediately after), so `select()` builds a
never-persisted `Run(id="")` purely to produce the same context shape `build_context()` always
produces. That is safe because selector rules only reference `workitem.*` fields, which are known
before any step has run — `plan.*` / `diff.*` / `run.*` are all still at their defaults here.

## What the rules can see

`build_context()` mirrors every `WorkItem.as_context()` key at the TOP level as well as under
`workitem.`, so a rule can say `story_points`, `issue_type`, `labels`, `size`, `ambiguity` directly:

```
key, external_id, title, description, issue_type, story_points, labels,
acceptance, status, ambiguity, size, url, comment_count
```

`size` is derived from story points by `WorkItem.size()`: `None` or <=1 -> `xs`, <=2 -> `s`,
<=5 -> `m`, <=8 -> `l`, else `xl`. `ambiguity` is `None` until the `ingest` step sets it — which is
why an ambiguity-based SELECTOR rule cannot work, but an ambiguity-based `when:` on the `clarify`
step can. Operators and semantics: [predicates.md](predicates.md).

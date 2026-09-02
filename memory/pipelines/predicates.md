# The `when:` predicate language

`ticketbot/core/predicate.py` — a hand-written tokenizer plus a recursive-descent parser over a small
tuple AST. **Never `eval`, `exec`, `compile`, or `ast.literal_eval`.** `when:` strings come from
profile and pipeline YAML that may be user-supplied, and untrusted ticket text flows into the
evaluation context, so this module is security-critical.

Bounds, enforced before or during parsing so a hostile expression fails fast and cheap:
`MAX_EXPR_LEN = 2000` chars, `MAX_TOKENS = 400`, `MAX_DEPTH = 20` nesting levels.

## Two surface forms

```python
evaluate(expr, ctx)            # string form, used by a step's when:
evaluate_mapping(spec, ctx)    # mapping form, used by pipeline_selector rules (ALL keys AND together)
evaluate_any(spec, ctx)        # None or "" -> True; str -> evaluate; Mapping -> evaluate_mapping
describe_mapping(spec)         # "story_points <= 5" for the banner
```

String form:

```
workitem.acceptance is empty or workitem.ambiguity >= medium
plan.security == yes or diff.touches_security
not (story_points > 5) and issue_type in [Bug, Defect]
```

Mapping form: `{story_points: {lte: 2}, issue_type: Bug}`, `{labels: {contains: agent}}`,
`{issue_type: {in: [Bug, Defect]}}`. A bare value means equality.

## Operators

| Concept | String form | Mapping form |
|---|---|---|
| equality | `eq` / `==`, `ne` / `!=` | `eq`, `ne` |
| ordering | `lt` `lte` `gt` `gte`, `<` `<=` `>` `>=` | same names |
| membership | `in`, `not in`, `contains` | `in`, `contains` |
| emptiness | `is empty`, `is not empty` | `empty`, `not_empty` |
| logic | `and`, `or`, `not`, parentheses | (keys AND together) |

Semantics worth remembering:

- A bare path with no operator is a truthiness test.
- A missing path evaluates to the `MISSING` sentinel: falsy, `eq` is False, `ne` is True, ordering is
  False, `is empty` is True.
- String comparisons are case-insensitive; `in`/`contains` work over lists, tuples, sets and strings.
- `true`/`yes` and `false`/`no` parse as booleans, and a `"yes"`/`"no"` STRING value compares equal
  to them — which is what makes `plan.security == yes` work against the string the plan parser
  stores.
- `ambiguity`, `size` and `severity` compare by their declared order, not alphabetically:
  `low < medium < high`, `xs < s < m < l < xl`, `nit < should-fix < blocker` (`ORDERED_ENUMS`,
  matched on the LAST path segment).
- Anything else falls back to numeric comparison when both sides parse as numbers, else lowercase
  string ordering, else False. Errors raise `PredicateError`, never a Python exception from the data.

## The evaluation context

Built once by `engine/context.py: build_context()` and shared with role-prompt rendering so a `when:`
and a `{placeholder}` can never drift on what a name means:

```python
{
  "workitem": {...as_context()...},   # plus every one of those keys mirrored at top level
  "plan": {"security": run.extra["plan_security"], "sections": run.extra["section_count"]},
  "diff": {"touches_security": ..., "files": ...},
  "run":  {"id": ..., "clarify_rounds": ..., "status": ...},
  "step": {},                          # RESERVED and deliberately empty
}
```

`step` is a reserved namespace nothing fills: a `when: "step.<anything>"` resolves to MISSING
(falsy) rather than raising. If a per-step fact is ever needed there, the orchestrator must pass it
through `extra=` — do not assume it is populated.

`plan.security` is scraped from `plan.md` by `_PLAN_SECURITY_RE`
(`^\s*(?:##+\s*)?Security[: ].*?\b(yes|no)\b`), and `prompts/roles/planner.md` is written to emit
exactly that shape — keep the two in sync. `diff.touches_security` is a keyword scan over
`patch.diff` (`auth`, `login`, `token`, `secret`, `password`, `crypto`, `subprocess`, `shell`,
`eval`, `pickle`, `sql`).

Validation timing: every `when:` is parsed once at pipeline LOAD time against an empty context, so a
typo fails before the run starts. Re-evaluation failure mid-run is logged and treated as True.

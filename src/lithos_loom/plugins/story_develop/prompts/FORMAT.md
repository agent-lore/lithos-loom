# Handoff format

Agents communicate by writing one **handoff file** per turn into
`/workspace/.handoff/`. The handoff is the only thing that crosses between
agents — your working notes stay in your own session.

A handoff is Markdown with this shape:

```markdown
## Status: FINDINGS | LGTM

## Summary
One short paragraph. The coder also reports test results here.

## Findings
(only when Status is FINDINGS — structured, one block per finding)
- finding_id: <assigned by the orchestrator; reference existing ones, do not invent>
  severity: critical | major | minor
  status: open | fixed | accepted | disputed | needs-clarification
  files: ["path:line", ...]
  rationale: <why>
  coder_response: <what changed, or why disputed>
```

**Reviewers:** `LGTM` means *no issues at all* (it closes every finding you
previously raised). Record every issue as a structured finding with an honest
severity — the orchestrator applies the project's severity threshold to decide
which findings block, and sub-threshold findings are recorded without
blocking. An issue mentioned only in the summary prose is invisible to the
rest of the pipeline.

**Finding identity:** ids are orchestrator-assigned. Leave `finding_id:` blank
for a NEW finding; on re-review, account for EVERY id you were given (update
its status — never drop, renumber, or invent ids).

**Coders:** to dispute a finding, include a `## Findings` block with that id,
`status: disputed`, and your reasoning in `coder_response:`.

For the coder's first turn there are no findings — just write
`## Status: LGTM` plus a `## Summary` of what you implemented and the result of
running the project's tests.

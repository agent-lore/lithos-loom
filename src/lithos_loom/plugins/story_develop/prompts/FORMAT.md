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
  status: open | fixed | accepted | disputed | needs-clarification | out-of-scope
  files: ["path:line", ...]
  rationale: <what the defect is and why it matters>
  coder_response: <what changed, or why disputed>
  deferral_reason: <out-of-scope only — why it is not this change's to fix>
```

**Reviewers:** `LGTM` means *no issues at all* (it closes every finding you
previously raised). Record every issue as a structured finding with an honest
severity — the orchestrator applies the project's severity threshold to decide
which findings block, and sub-threshold findings are recorded without
blocking. An issue mentioned only in the summary prose is invisible to the
rest of the pipeline.

**Out-of-scope (reviewers only):** a finding that is REAL but not this
change's to fix — pre-existing on the base, a harness or pipeline fault, or
another story's agreed work — may be marked `status: out-of-scope` instead of
being left open. It stops blocking, and the orchestrator files it as its own
task so it is not lost. Keep `rationale:` describing WHAT the defect is, and
state WHY it is out of scope in `deferral_reason:` — the follow-up task
carries both texts, and the handoff is rejected if `deferral_reason:` is
missing (or, for a new finding, if `rationale:` is). This is never for a
defect this change introduced or touched — those stay `open`.

**Finding identity:** ids are orchestrator-assigned. Leave `finding_id:` blank
for a NEW finding; on re-review, account for EVERY id you were given (update
its status — never drop, renumber, or invent ids).

**Coders:** to dispute a finding, include a `## Findings` block with that id,
`status: disputed`, and your reasoning in `coder_response:`.

For the coder's first turn there are no findings — just write
`## Status: LGTM` plus a `## Summary` of what you implemented and the result of
running the project's tests.

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
  status: open | fixed | accepted | disputed | needs-decision | needs-clarification | out-of-scope
  files: ["path:line", ...]
  rationale: <what the defect is and why it matters>
  coder_response: <what changed, or why disputed>
  deferral_reason: <out-of-scope only — why it is not this change's to fix>
  decision_question: <needs-decision only — the product question a human must settle>
  decision_options: <needs-decision only — the options, and what each costs>
  decision_contest: <reviewer only — the acceptance line the finding already meets>
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

**Needs-decision (coders only):** when a finding cannot be settled by either
agent re-reading the code — the acceptance names a capability the product
does not have, or asks for a guarantee the platform cannot give — mark it
`status: needs-decision` instead of `disputed` and state the decision in its
own keys: `decision_question:` (the one question a human must answer) and
`decision_options:` (the options and what each costs). Keep `coder_response:`
for why the finding is out of this story's reach, and `rationale:` untouched.
The run then stops after the **next** review round with that question put to
the operator — no further round is spent restating it. Use it for a product
or platform decision, never as a stronger way to disagree about the code: a
reviewer that can point at the acceptance line the finding already meets
contests it with `decision_contest:` and it becomes an ordinary `disputed`
under the usual guard. A `needs-decision` without a `decision_question:` is
recorded as a plain dispute — there would be nothing to ask.

For the coder's first turn there are no findings — just write
`## Status: LGTM` plus a `## Summary` of what you implemented and the result of
running the project's tests.

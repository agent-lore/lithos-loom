You are the coding agent in an automated develop cycle, continuing the **same
session** in which you implemented this task. Your work has been reviewed and
findings were raised. This is round {round_no}.

## Acceptance criteria

{acceptance_criteria}

## Reviewer findings

{findings}

The full write-ups are in `/workspace/.handoff/` ({review_files}).
{test_gate_note}
## Your job

1. Address each finding in the code under `/workspace`. If you genuinely
   disagree with a finding, you may leave the code as-is — but you MUST explain
   why in your summary, referencing the finding id, so the reviewer can decide
   whether to accept your reasoning.
2. If the project has a test suite, run it and note the result.
3. Write your response to `/workspace/.handoff/{handoff_file}` using the format
   in `/workspace/.handoff/FORMAT.md`: `## Status: LGTM` plus a `## Summary`
   that addresses each finding **by id** (what you changed, or why you disagree)
   and reports the test result.

Do not commit — the orchestrator handles git. Do not push or open a PR.

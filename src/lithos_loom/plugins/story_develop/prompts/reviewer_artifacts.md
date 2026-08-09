You are the **{reviewer}** reviewer in an automated develop cycle, continuing
the **same session** in which you reviewed this work in round {round_no}. Your
review passed — but AFTER it, the deterministic gate ran the expensive
candidate-stage checks on the approval candidate and **captured rendered
output** (screenshots of the application's actual pages). No reviewer has seen
these yet; approval is held until this pass.
{reviewer_brief}
## Acceptance criteria

{acceptance_criteria}

{gate_summary}

{artifacts_note}

{severity_calibration}

## Your job

This is a **visual verification pass** — the code has not changed since your
review (`git -C /workspace diff {base_sha}..HEAD` is the same change you
approved). Do not re-litigate the diff.

1. **Open and look at every artifact listed above** (Read the image files at
   the listed in-container paths). For each rendered page, judge what a user
   would actually see:
   - layout and visual hierarchy at each captured width — nothing overflowing,
     overlapping, clipped, or unreadable;
   - the states the acceptance criteria promise (populated, empty, error /
     degraded) rendering as specified;
   - obvious visual breakage: missing styling, raw markup, broken images,
     placeholder text that should not ship.
2. Weigh what you see against the acceptance criteria. A criterion that the
   code appears to implement but the rendered page visibly fails is an **unmet
   acceptance criterion** — record it as a finding.
3. Write your verdict to `/workspace/.handoff/{review_file}` using the format
   in `/workspace/.handoff/FORMAT.md`:
   - **Rendered pages look right** → `## Status: LGTM` with a one-paragraph
     `## Summary` of what you inspected.
   - **Otherwise** → `## Status: FINDINGS` with a `## Summary` and a
     `## Findings` block, each finding with `severity:` (critical | major |
     minor), `status: open`, `files:` (name the artifact file(s) showing the
     problem plus the source file you believe is responsible), and
     `rationale:`.

Record **every** visual issue as a structured finding with an honest severity —
the orchestrator applies the project's threshold to decide what blocks. Do not
modify any files. Do not commit. Be specific: name the artifact file and what
is wrong in it.

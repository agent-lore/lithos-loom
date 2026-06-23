# Review-correctness eval harness (#183)

A **seeded-defect benchmark** that measures how reliably the story-develop
reviewer panel catches real defects. It runs review-only mode (#154) against
known-buggy changes K times and reports catch-rate, severity-correctness, and
false-positive rate. See [ADR 0005](../../docs/adr/0005-review-correctness-eval-harness.md).

**On-demand only — not part of `make check`.** A live run spends real tokens and
needs the host sandbox (docker) + the agent CLIs (`claude` / `codex`). The
harness *logic* is unit-tested hermetically; only the live run below calls agents.

## Run it

```bash
# All cases, 5 runs each (host, from the loom checkout)
uv run lithos-loom eval review

# One case, 8 runs, a stricter bar
uv run lithos-loom eval review --case 180-attach-delivery -k 8 --bar 0.9
```

The command prints a per-case table (catch-rate / severity-correctness / FP) and
exits non-zero if any case falls below its bar.

## Add a case

Every defect that escapes review and is caught later (by a human, by the codex
backstop, in prod) should become a regression case. Create a directory under
`cases/<id>/`:

```
cases/<id>/
  case.toml   # the defect: base..head, personas/profile, expected findings
  ac.md       # the acceptance criteria the reviewer receives (the issue body)
```

`case.toml`:

```toml
[case]
id = "<id>"
description = "..."
repo = "."                      # the repo to review in
base = "<base sha>"             # the defect diff is base..head
head = "<buggy head sha>"
personas = ["correctness"]      # canonical persona names (or set `profile`)
profile = "standard"
acceptance_criteria_file = "ac.md"

# Optional clean pair for the false-positive measurement. May use its own base
# so the known-good is an independent clean diff, not the empty diff.
[known_good]
base = "<clean base sha>"
head = "<clean head sha>"

# One or more expected defects a correct review MUST surface.
[[expected]]
file = "path/to/file.py"        # the finding must touch this file
keywords = ["delivery", "approved"]  # ...and mention >= 1 keyword
min_severity = "critical"       # ...at or above this band
mechanism = "prose describing the defect (for the LLM-judge fallback)"
```

## Scoring (how a finding matches)

- **Structured (default):** a produced finding matches an expected defect when it
  touches the expected `file` AND mentions ≥1 `keyword`. Severity-correct when the
  matched finding is at/above `min_severity`.
- **LLM-judge (fallback):** the matcher supports an injected judge for
  correctly-worded-but-different findings; wiring an agent judge into the CLI is a
  follow-up.

A case is **caught** in a run iff *every* expected defect matches. Reported over K
runs: catch-rate, severity-correctness (among caught), and false-positive rate (on
the known-good head). A case **passes** at `catch-rate ≥ bar` (default 0.8).

## Seed case

`180-attach-delivery` — the #180 / #171 defect: `develop attach` exits on the
`approved` verdict before PR delivery (the false-done window). It reviews the
*removal* of the #180 fix as the defect, and the fix itself as the known-good.

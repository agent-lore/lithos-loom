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
# All cases, 5 runs each (host, from the loom checkout) — judge ON by default
uv run lithos-loom eval review

# One case, 8 runs, a stricter bar, retain each run's report for inspection
uv run lithos-loom eval review --case 180-attach-delivery -k 8 --bar 0.9 \
  --report-dir /tmp/eval-reports

# Quick, cheap, agent-free pass (topic-loose — not a trusted number)
uv run lithos-loom eval review --no-judge
```

The command prints a per-case table (catch-rate / severity-correctness / FP) and
exits non-zero if any case falls below its bar.

- `--judge` / `--no-judge` (**default on**): the mechanism LLM-judge confirms each
  finding describes the case's *specific* defect, not just the same file/topic.
  Without it the structured matcher over-counts on same-topic changes (the first
  live run measured 100% FP on the seed). `--judge-tool` picks the agent
  (`claude` | `codex`).
- `--report-dir DIR`: write every run's report to `DIR/<case>/<variant>-<i>.json`
  (`variant` = `buggy` / `known-good`) so you can read the findings behind a number.

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
personas = ["correctness"]      # the panel under test — canonical persona names,
                                # validated at load (a typo fails closed)
profile = "standard"            # selects the check-set; validated at load
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
mechanism = "prose describing the defect (the LLM-judge keys on this)"
```

## Scoring (how a finding matches)

- **Mechanism LLM-judge (default, `--judge`):** authoritative. Given the reviewer's
  findings and the expected `mechanism`, it returns which findings describe *that
  specific* defect — so it both **vetoes** a same-topic false hit and **rescues** a
  correctly-worded finding that shares no `keyword`. Severity-correct when a matched
  finding is at/above `min_severity`.
- **Structured (`--no-judge`):** a produced finding matches when it touches the
  expected `file` AND mentions ≥1 `keyword`. Cheap and agent-free, but over-counts
  when the known-good shares the defect's topic (the first live run measured 100% FP
  this way) — useful for a quick pass, not a trusted number.

A case is **caught** in a run iff *every* expected defect matches. Reported over K
runs: catch-rate, severity-correctness (among caught), and false-positive rate (on
the known-good head). A case **passes** at `catch-rate ≥ bar` (default 0.8).

## Seed case

`180-attach-delivery` — the #180 / #171 defect: `develop attach` exits on the
`approved` verdict before PR delivery (the false-done window). It reviews the
*removal* of the #180 fix as the defect, and the fix itself as the known-good.

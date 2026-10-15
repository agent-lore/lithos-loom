# `lithos-loom brief-reviews`

Report what the dispatch-time **brief review** has recorded on the stories, so
the reading behind `develop_brief_review_auto_facts` can be taken again on
demand. Read-only: one Lithos `task_list` call, no writes, no other network.

```
lithos-loom brief-reviews [--project SLUG] [--since <N>d|YYYY-MM-DD] [--json]
    [--config config.toml]
```

This is **not** `lithos-loom develop brief-review` ([`brief-review.md`](brief-review.md)),
which drafts an addendum for one story. This command only reads the records
that dispatch left behind.

## Why it exists

Every brief-review pass appends an entry to its story's
`metadata.brief_review`: what it drafted, how it settled, the structural
`check` phase 2 ran on the draft, and — once the operator approved it at the
gate — what was done to each item. The questions that decide whether the
auto-facts dial is safe on a project ("did the operator ever edit or cut a
fact?", "how often does the check hold a facts-only draft for nothing?") are
answered by counting those entries. This command counts them.

## Selection

- `--project SLUG` reads the stories whose `metadata.project` is `SLUG`, in
  every status (completed and cancelled stories keep their records). Without
  it, every story is read and stories with no project are grouped under `—`.
- `--since` keeps an entry by its own `drafted_at`: `<N>d` is N×24h back, a
  date is UTC midnight, and a time without a zone is read as UTC. An entry
  whose `drafted_at` cannot be read is outside any window. The story's
  `updated_at` is not used — any later write bumps it.

## The report

```
12 brief-review entries on 11 stories

entries by outcome
PROJECT  MODE   PENDING  APPROVED  AUTO_APPENDED  NO_CHANGE  FAILED  OTHER  TOTAL
k2       delta  0        0         1              0          0       —      1
k2       full   1        10        0              0          0       —      11

approval edits (10 approved entries)
KIND  UNCHANGED  EDITED  CUT  ADDED
S     9          0       1    0
F     24         0       0    1
D     9          1       0    0
facts edited or cut at a gate: 0

auto-applied (full mode, dial on): 0 — delta rechecks that skipped their gate: 1 (not dial-governed)

check false holds: 1/2 held facts-only drafts approved all-unchanged (50%)
  story-7 run-7 — problems: F2: no Basis line
awaiting approval: 1 held facts-only drafts
  story-11 run-11 — problems: F1: names no file
no check: 0 entries (drafted before phase 2, or failed) — outside the false-hold count
```

- **Entries by outcome** — per project and mode. `OTHER` counts an outcome
  outside the five the recorder writes.
- **Approval edits** — over `approved` entries, each item's outcome by kind.
  The kind is the item's recorded kind; an `added` item has no recorded kind,
  so its id letter (`S`, `F`, `D`) is used. A fact edited or cut at a gate is
  the dial's first criterion, so its count closes the section.
- **Auto-applied** — `auto_appended` and `no_change` entries with
  `mode: full`. A full pass reaches those outcomes only with the dial on;
  entries record no dial, so this is read from the mode. A delta recheck
  skips its gate whatever the dial says, so it is counted on the heading
  only. The `CHECK` column reads `passed` or `not passed` (with any
  `problems`), never "held": a full `No change` pass records the check as
  not facts-only and not passed, yet skipped its gate.
- **Check false holds** — of the `approved` entries the check held as a
  facts-only draft (`check.facts_only` true, `check.passed` false), those the
  operator approved with every item `unchanged`. Each is listed with the
  check's `problems`, so the rule that misfired can be read. A held entry
  still `pending` is listed as awaiting approval and counts in neither side.
  An entry with no `check` (drafted before phase 2, or `failed`) is outside
  the rate.

An entry whose `items` is not a list (a hand-edited record) is read as having
no drafted items and is named on a closing `malformed items (not a list)` line;
the rest of the report is unaffected. The record is written by Lithos clients,
so the text report strips terminal control bytes before printing, and folds a
newline or tab inside a value to a space so it cannot forge a line.

The report has no cost section: entries record no cost. A held pass's cost is
on the human gate it raised (`run_brief.cost_usd`); a pass that skipped its
gate recorded it nowhere in Lithos.

## `--json`

One JSON array, one object per entry: the entry as recorded, every key passed
through (`run_id`, `mode`, `base_sha`, `drafted_at`, `outcome`, `items`,
`check`, `outcomes`, `note`, `approved_at`, `gate_id`, `operator_note`,
`appended`, `superseded_gates`, …), plus `story_id` and `project`. Ordered by story id,
then record order.

## Exit codes

| Code | When |
|---|---|
| `0` | the report (or `no brief-review entries in the window`) was printed |
| `1` | the config cannot load, or Lithos is unreachable or errors |
| `2` | `--since` cannot be read (checked before any I/O) |

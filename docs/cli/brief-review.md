# `lithos-loom develop brief-review`

Draft a story's **brief-review addendum** — the corrections and clarifications its
coder needs to build on the code as it is now — and print it. Writes nothing to
Lithos.

```
lithos-loom develop brief-review <story>
    [--base REF] [--delta-from REF] [--brief-file PATH] [--repo PATH]
    [--timeout SECONDS] [--config config.toml]
```

## Why it exists

A PRD's slices are all written before any of them is built. Each merge then moves
the code the later briefs describe. Loom dispatches a story with its brief as
written, so a slice can be built against a picture of the code that two merges
have already changed.

The lens T3 hand pilot (2026-10-03 → 10-05, slices W1 and W4–W8) closed that gap
by hand. Before each slice dispatched, its brief was checked against the exact
tree the coder would start from, and the result went to the operator as an
addendum to approve. It caught:
- a Complete button that never submits;
- the wrong `next` on an HTMX receipt;
- a dependents-read failure classified as the write's own failure;
- e2e writes mutating a server that the screenshot captures share.

It also changed scope materially twice (W7, W8).

This command is that check, automated: one read-only agent turn. Phase 1 of task
604fb936 then holds a dispatch for the operator's approval of the addendum. This
command is the half that runs on its own: use it to try the pass on a project
before switching that on.

## The addendum

Three kinds of item, each a top-level `- **<id>. <title>.**` bullet with its
detail as nested lines:

| Kind | Ids | What it is |
|---|---|---|
| Scope cut | `S1, S2, …` | A proposal to drop part of the brief and its acceptance. Ends with a `Basis:` line. |
| Fact | `F1, F2, …` | **Describes** the code at the base (names, paths, numbers, behaviour), with where it was seen. Never says what to build. |
| Decision | `D1, D2, …` | Anything that **prescribes**: where something goes, which reading to build, how a brief/code conflict resolves. Ends with a `Basis:` line naming the facts, `file:line` or PRD section it stands on. |

When in doubt, an item is a decision. Guardrail budgets appear only as facts
(their current values). The reviewer is told that budgets are guides, never a
reason for a structure: a better structure raises the budget, with the reason
recorded.

If nothing needs adding, the answer is `No change` with a one-line reason.

## What it resolves

- **The checkout:** the story's project, `[projects.<slug>].repo` (or `--repo`).
- **The agent:** the coder's engine, model and effort, from the story's
  `develop_*` settings, then the host's `[story_develop.default_models]`. The
  same resolution `develop converge` uses.
- **The base:** `origin/main` fetched now, the sha a dispatched coder's worktree
  would be cut at. `--base REF` names another commit (a branch, a tag or a sha);
  the review is then at exactly that tree.
- **The inputs.** These are written as files under the run's read-only
  artifacts mount, never as prompt text, because a brief can contain
  `{braces}`:
  - the brief (title plus description, or `--brief-file`);
  - the story's explicit `metadata.acceptance_criteria`, under a closing
    `## Acceptance criteria` heading in the brief file. It is read exactly as
    dispatch reads it: a blank or non-string value is absent. The coder
    receives that section separately, so the reviewer checks it too;
  - `metadata.prd` and `metadata.prd_sections`;
  - the first-parent history of the base since the story's `created_at`, at
    most 200 commits, the newest kept.

  The PRD itself is read in the repository, as the coder would read it.

## Recheck: `--delta-from REF`

The brief, including any addendum already appended to it, was approved at REF,
and the base has moved since. The reviewer gets the commits and the files changed
between REF and the base. It reports only what those change:
- a fact that no longer holds;
- a name that moved;
- a new thing to build on;
- a decision the new code overturns.

Otherwise it answers `No change`. New items continue the numbering of the
addendum already in the brief.

## Validation and output

The agent's file is checked:
- every item sits under a section;
- ids are well formed, unique and carry their section's letter;
- every decision and scope cut has a `Basis:` line;
- the file says something: items, or a reasoned `No change`, but not both;
- no line is left that the parse cannot place. An item-like line it cannot
  read, such as `- **D1: …**` with a colon for the full stop, or prose in a
  section outside any item, is reported, never silently dropped.

A draft that fails gets **one** correction turn in the same session, told every
problem.

| Exit | Meaning | stdout | stderr |
|---|---|---|---|
| 0 | addendum drafted | the rendered addendum | counts and cost |
| 1 | degraded: failed turn, no file, still invalid after the correction, or a runtime failure (docker unavailable, a turn that raised) | the agent's raw text, if any | the note |
| 2 | refused before any agent ran: unknown story, unmapped project, a ref that is not a commit | — | the reason |

The rendered addendum opens with a header naming the base it was checked
against (and, for a recheck, the base it moved from). Items are in
cut → fact → decision order, each exactly as written.

## Comparing against a hand-written addendum

```
# the brief as it was before its hand addendum was appended
lithos-loom develop brief-review 24ad5f91 --base 392301b --brief-file w8-brief.md
```

Host-only: it needs docker, the sandbox image and Lithos. It is not part of the
hermetic `make check`.

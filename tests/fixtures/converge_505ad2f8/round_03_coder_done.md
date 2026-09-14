## Status: LGTM

## Summary

**correctness/f-001 — the supersession predicate was wrong on the `focus=`
startup path.** The reviewer is right and the repro is exact: `tasks.js` seeds
BOTH `selectedTaskId` and `desiredTaskId` from the URL at load, so when the
server could not render the `focus=` panel and `graph.js` starts its fallback
`open(A, {push: false})`, that request is in flight with the two ids already
equal. Round 2's `desiredTaskId === selectedTaskId` test read that as "settled"
and returned early, so the response still landed mid-gesture.

Fixed by tracking what the predicate was really asking about — whether a
request is pending — instead of inferring it from the ids:

- `tasks.js` gains `pendingOpenGeneration`, set to the open's own generation
  when one starts and cleared at the point where that open SETTLES (after the
  last supersession check, before it either paints or reports failure).
  `supersedePendingOpen()` now tests `pendingOpenGeneration !== panelGeneration`.
- It needs no clearing on the superseded exits, and that is the reason it can
  be one number rather than a counter: only the newest open can still write —
  every earlier one has had the generation moved past it and returns in
  silence — and the same bump that supersedes an open is what makes this value
  stop matching `panelGeneration`.
- Behaviour preserved: a settled panel, a server-rendered one and a closed one
  all leave `pendingOpenGeneration` unequal to `panelGeneration`, so the tap is
  still a no-op for them and nothing on screen moves.

Regression: `test_a_startup_panel_request_is_superseded_like_any_other` — the
reviewer's own repro (`focus=ship` with no server-rendered panel, `firsttap` on
another node, then release) asserts the held startup response is dropped, the
ring stays on the node being clicked, and the gesture still navigates.
Restoring the round-2 predicate fails it; the round-2 tests stay green either
way, which is precisely the hole.

**test-quality/f-002 — the load-bearing combination was untested.** Agreed:
neither round-2 test had a panel displayed AND a request pending at the same
time, so a supersession that also cleared the host would have passed both.
Added, at both levels:

- `test_superseding_a_pending_open_leaves_the_panel_already_on_screen`
  (`tests/test_tasks_js.py`) — settle A, hold B, start C's double-click,
  release B between C's two taps: A's panel and `focus=A` survive every step,
  B never reaches the URL, and C navigates. A supersede that empties the host
  fails it.
- `a superseded panel response neither opens nor moves the graph`
  (`e2e/tests/smoke.spec.ts`) — the same sequence as real pointer input against
  the real Cytoscape, with B's fragment held by a Playwright route and released
  between C's two physical clicks. It asserts the no-reflow property directly:
  C's rendered position is identical before and after, so the second click hits
  the node it was aimed at, and `/tasks/loom-announce` is reached. The two
  panels are opened through the page's own API rather than by clicking, because
  Cytoscape's multi-click detector measures time alone and a setup click would
  pair with the gesture's opening click — it is also the truer setup, since the
  `focus=` fallback in f-001 starts its open with no tap at all. Verified red:
  with a host-clearing supersede the canvas widens, the panel assertion fails
  and the node moves.

**test-quality/f-001** — the reviewer marks it fixed; nothing changed for it
this round.

`docs/SPECIFICATION.md` §5.12.1 now names the `focus=` fallback among the opens
a node tap supersedes, and states that dropping one moves nothing on screen.

**Validation:** `make lint` clean (ruff check + format), `make typecheck` clean
(pyright, 0 errors), `make diagrams` clean (41 passed; `docs/generated/` carries
only the test-line-ratio move). Targeted suites — `tests/test_tasks_js.py`,
`tests/test_task_panel.py`, `tests/test_graph_page.py`,
`tests/test_graph_layout.py`, `tests/test_tasks_mvp.py` — **393 passed**.
Playwright: the 16 canvas/node/panel e2e tests including the new one — **16
passed** (run from `e2e/` against the sandbox's own Chromium). Full suite and
the rest of e2e left to the orchestrator's gate.

## External findings
- f-001: FIXED — correctness: `supersedePendingOpen()` now tests a real pending
  request (`pendingOpenGeneration` in `src/lithos_lens/static/tasks.js`) instead
  of `desiredTaskId === selectedTaskId`, so the client's `focus=` startup
  fallback is superseded like any other open; regression test in
  `tests/test_tasks_js.py` reproduces the reviewer's `focus=ship` case.
  (test-quality/f-001 is marked fixed by its reviewer and is unchanged.)
- f-002: FIXED — test-quality: added the displayed-A / pending-B / gesture-on-C
  sequence both deterministically (`tests/test_tasks_js.py`) and as real pointer
  input with the response released between the two clicks
  (`e2e/tests/smoke.spec.ts`), asserting A stays on screen and nothing reflows.

## Status: LGTM

## Summary

**correctness/f-001 — a pending panel open landing inside a double-click.**
The reviewer is right, and the hole is exactly where they put it: the `onetap`
debounce only stops THIS gesture's own click from painting mid-gesture; an open
started by an EARLIER click carries its own generation and is still "current"
when it answers, so its response inserts the panel beside the canvas, narrows
it, refits it, and moves the node out from under a pointer halfway through a
double-click. Same failure the debounce exists to prevent, reached through an
older request.

Fixed the first way the finding offers — invalidate the pending open when a raw
node tap starts a new gesture:

- `tasks.js` gains `supersedePendingOpen()` (exported as
  `LithosLens.panel.supersedePending`): it takes the next `panelGeneration` and
  walks `desiredTaskId` back to `selectedTaskId`, the same walk-back a failed
  open does. It is a no-op when `desiredTaskId === selectedTaskId`, which is
  precisely "nothing is in flight" — so a SETTLED panel, a closed one, and the
  server-rendered `focus=` panel (both ids are seeded equal at load) are all
  untouched. Nothing is announced: the selection has not changed, and
  announcing would re-render the canvas host mid-gesture.
- `graph.js` calls it from the raw `tap` handler on a task node, before the
  focus ring — so the pending open dies the moment the gesture opens, and
  whatever the gesture settles into (`onetap` → panel, `dbltap` → navigation)
  is what writes next. Scoped to node taps because a node tap is always
  followed by an open or a navigation, which is what keeps the URL and the
  panel in agreement afterwards.

Regression tests, both driving the real `graph.js` + Cytoscape:

- `test_an_older_panel_request_cannot_land_inside_a_double_click` — a settled
  click on `schema` whose response is HELD, then the first click of a
  double-click on `ship`, then the response lands, then the second click. With
  the fix: no panel, no push, and `/tasks/ship`. Removing the two lines in
  `graph.js` fails it with "a stale panel opened mid-gesture".
- `test_a_settled_panel_survives_a_gesture_that_starts_elsewhere` — the guard
  clause: a panel that has arrived stays, and the new tap still lights its own
  node.
- The harness gained what those need and nothing more: a `hold` panel-fetch
  mode with a `release` action (the only way to put a gesture between a request
  and its answer) and a `secondtap` action — the closing half of a double-click
  whose first half was `firsttap` (`firsttap:x` + `secondtap:x` emits exactly
  what `dbltap:x` does, with somewhere to put an action in between).

`docs/SPECIFICATION.md` §5.12.1 now states the rule beside the debounce it
completes.

**test-quality/f-001 — the serialized membership contract was asserted too
weakly.** Agreed on all three counts; the producer and the consumer were tested
with different fixtures and nothing pinned the wire between them. Added:

- `test_the_payloads_chain_membership_is_the_active_partition`
  (`tests/test_graph_page.py`) — the EXACT emitted groups, on two shapes that
  fail differently: the reviewer's mixed open/open/completed cycle, where the
  drawn cycle holds all three and the chain's groups must be
  `[["cyc-a"], ["cyc-b"]]` (display-cycle membership fails here), and a live
  SCC on the chain, where they must be `[["p"], ["cyc-x", "cyc-y"], ["d"]]`
  (singleton groups fail here).
- `test_the_canvas_traces_the_chain_the_served_payload_states` — the same two
  payloads SERVED by the route and drawn by the real `graph.js` against the
  real Cytoscape, which is the end-to-end wiring the reviewer asked for: the
  live step inside the drawn cycle is traced, completed `cyc-c` is not accented
  though it is in the same box, and where the loop is live the trace enters at
  `cyc-y` — the member the chain does NOT name — with both members accented.
  The A4 harness runner is imported from `tests/test_tasks_js.py` rather than
  rebuilt (test-module helpers only; no `lithos_lens` private is reached, and
  the `tests_private_imports` budget stays at 0).
- `tests/test_graph_layout.py` — the `through=` branch now asserts the exact
  `focused.members`, so a focused chain returned without its membership fails.

Verified by injecting each wiring error the finding names: serializing
`topology.condensation_of(...)` membership and serializing singletons each fail
BOTH new tests; returning `BlockingChain(nodes=chain, bound=bound)` from the
`through=` branch fails the layout test. All pass on the tree as it stands.

**Validation:** `make lint` clean (ruff check + format), `make typecheck` clean
(pyright, 0 errors), `make diagrams` clean (41 passed; `docs/generated/` carries
only the test-line-ratio move, committed in this tree). Targeted suites —
`tests/test_tasks_js.py`, `tests/test_graph_page.py`, `tests/test_graph_layout.py`,
`tests/test_task_panel.py` — **243 passed**. Full suite and e2e left to the
orchestrator's gate.

## External findings
- f-001: FIXED — correctness: a node tap now supersedes any unpainted panel
  open (`tasks.js` `supersedePendingOpen` via `LithosLens.panel.supersedePending`,
  called from `graph.js`'s raw node-tap handler), with a held-response
  regression test that fails without it; test-quality: the exact emitted
  `longest_chain.members` is now asserted on a served payload for both the
  mixed and the live-SCC cycle, driven through the real JS harness, and the
  `through=` branch asserts its membership too.

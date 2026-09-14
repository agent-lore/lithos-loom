Slice A4 of `docs/prd/t2-task-relationship-graphs.md` (read the PRD first — D3, D8, D9 are binding; REQUIREMENTS §5.7 "Cytoscape rendering" agrees). Needs A3 (the text page + payload) and A6 (the panel implementation).

**Build** the progressive enhancement over A3's embedded payload:
- Load the already-vendored `static/vendor/cytoscape.min.js` (3.30.3, SHA in `docs/vendor-assets.md`) on the graph page only. New `graph.js` (test it in the `test_tasks_js.py` pattern).
- Draw from the payload with the server's explicit `roots`; `breadthfirst`, no physics. Colour = status (open, completed, cancelled; blocked tinted; in-progress pulse), shape = type (ellipse task, round-rect epic, diamond gate); cycle members inside a compound parent with T1's `cycle` styling; ghosts dimmed with project chip; `unknown`-status ghosts and `unknown` edges in the `unknown` style; inactive edges faded.
- **Arrowheads on every edge.** `blocks` solid, `waits_on_gate` dashed. `parent_child` (thin, light) and `discovered_from` (dotted) are overlays, OFF by default, toggled in the toolbar, remembered in the URL (`overlays=hierarchy,provenance`), applied client-side from the static payload with no fetch (context ghosts are already in it); `popstate` re-applies them.
- Persistent plain-language legend (from the text page). Longest-chain trace on the canvas. Isolated toggle (`isolated=1|0`) mirroring the disclosure defaults.
- Click on a node → the A6 panel, hosted beside the canvas, using `focus=` as the graph page's single selection parameter (D8/D9 — A7 completes the focus transitions; here: click pushes `focus=`, panel opens, close clears it). Double-click → `/tasks/{id}`.
- "graph changed — refresh" pill when a consumed event's `task_id` matches a node id; never auto re-layout.
- "Show as text" toggle: the canvas collapses the text layers behind it; the text stays in the DOM.

**Acceptance:** e2e artifacts show the fake's cycle as a compound node, the ghost dimmed, arrowheads and the legend; toggling hierarchy adds the `parent_child` edges and `overlays=hierarchy` to the URL; toggling provenance shows the `discovered_from` edges and their context ghost from the payload with no network request (JS test), and back after the toggle hides them again; a `task.updated` for a node shows the pill and does not re-layout; text remains in the DOM when the canvas is up; clicking a node opens the panel with that task and pushes `focus=`.

`make check` + `make diagrams` clean; `develop_artifacts_path` screenshots for the visual review. Update `docs/SPECIFICATION.md` and `docs/vendor-assets.md` if the asset changes.

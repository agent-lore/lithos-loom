Slice A6 of `docs/prd/t2-task-relationship-graphs.md` (read the PRD first — D9 and the Routes table are binding; REQUIREMENTS §5.5 is the normative contract and agrees). Independent of the graph slices: dependents come from the task's own `edge_list`.

**Build** the side panel on the dashboard, as one implementation that the graph page (A4) will reuse with its own selection parameter:
- `GET /tasks?selected=<id>` server-renders the panel open (no-JS baseline). A row click fetches `GET /tasks/{id}?fragment=panel` (same template as the detail page, a partial block) and pushes `selected` onto the URL via `pushState`; close clears it and preserves list state (filters, epic scope); Expand navigates to `/tasks/{id}`. The dashboard keeps `selected` as its single selection parameter.
- Panel content (REQUIREMENTS §5.5.1): header (title, type badge, status, project chip), active claims, **blockers** with live status (T1's level-1 text chain), **Blocks** — level-1 dependents (outgoing `blocks`/`waits_on_gate`) with status, **parent** breadcrumb, findings count with a link. The downstream-impact line (D10) is A7's; leave a clearly named slot.
- The same "Blocks:" level-1 dependents line is added to the full detail page's text baseline (REQUIREMENTS §5.5.2).
- An unknown id → the existing not-found panel, never HTTP 500.

**Acceptance:** `GET /tasks?selected=<id>` contains the panel with the task's title, its blockers with live status, and its level-1 dependents; the fragment route returns the partial only; unknown id → not-found panel; closing keeps `?project=` intact (JS test in the `test_tasks_js.py` pattern); the detail page lists dependents under "Blocks:".

`make check` + `make diagrams` clean. Update `docs/SPECIFICATION.md` §5.6 for what ships; e2e artifacts for the visual review.

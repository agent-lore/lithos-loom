

**Clarifications (2026-10-05, Dave)** — binding. Checked against lens main `1b8c59e` (W1–W5 merged). They pin down the build above; they do not widen it. Where one contradicts the brief or the PRD, the clarification wins.

**Facts** (corrections — no judgment involved)

- **F1. Not independent of W4b and W5.** Both have merged, and this slice builds on them:
  - W4b's confirm hook: `TaskWrite.confirm_page` and `CONFIRMATION_REQUIRED` in `write_funnel.py`;
  - W5's fields on `WriteDone` and `WriteReceipt`: `prior_status`, `checked_status`, `released_exact` (false = a lower bound), `released_unread` (true = could not be read at all) and `released_waiting`;
  - `back_to`, which the funnel sets on every receipt.
- **F2. There is no downstream, cross-project walk to reuse.**
  - What exists: `graph_scope.assemble_scope` stops at one project or epic, `graph_impact` counts only inside its scope, and `blocker_chain` walks upstream.
  - The building blocks are:
    - `graph_fanout.read_edges(lithos, tasks, cache, limiter, tally)` → `(entries, incomplete: {id: reason})`;
    - `graph_scope.dependency_edge_state(pred_status, dep_status)` (active / inactive / unknown);
    - `task_links.BLOCKER_EDGE_TYPES`;
    - the limiter `asyncio.Semaphore(config.graph.fetch_concurrency)`.
- **F3. "Re-read the focal task's entry first"** means `state.graph_cache.evict(task_id)` and then `read_edges`. A failed read is never cached; it appears in `incomplete` with its reason.
- **F4. "The existing page-scope bound" is `[lithos-lens.graph].max_tasks`** (default 300; `state.config.graph.max_tasks`). The graph page *refuses* a scope over it; this walk instead *degrades* to "≥ N".
- **F5. Claims are read before the cancel, or not at all.** Read them with `client.task_status(id).claims` (`ClaimRecord(agent, aspect, expires_at)`). Once the cancel lands they are gone, in the fake as upstream.
- **F6. Open children** come from `client.task_children(id)` (open only by default; any task type).
- **F7. `[writes].confirm_cancel`** shipped in W1 (default `true`, with an env override), and nothing reads it yet. SPECIFICATION's "a later slice" note about it is updated in this slice.
- **F8. Copy.** REQUIREMENTS says direct dependents become "permanently blocked **until re-routed**"; use that wording.
- **F9. Fixtures.**
  - In the demo:
    - the depth-5 chain `loom-schema` → `loom-transport` → `loom-worker` → `loom-ship` → `loom-announce`, with the cross-project `loom-ship` → `lens-graph-page` (cancelling `loom-schema` gives 1 direct and 4 behind);
    - the cycle `loom-cycle-a` ⇄ `loom-cycle-b`;
    - the epics `loom-epic` and `influx-epic`;
    - the gates with waiters `influx-read-swap-approval` and `influx-replica-cooldown` → `influx-backfill`;
    - one claim: `worker-a` on `influx-ingest-cutover`.
  - Test-local:
    - claims by several agents (`FakeLithosDataset(claims=…)`);
    - a failed edge read (a fake subclass, as `EdgesDown` in `tests/test_proceed_anyway.py`);
    - a budget hit (a small `max_tasks`).
- **F10. Budgets.**
  - `cross_component_edges` is 38/38, `max_module_lines` 849/850 (`tasks.py`), `modules_over_800_lines` 5/5.
  - `graph_scope.py` (793 lines) and `graph_mini.py` (797) cannot host the walk.
  - `write_routes.py` is now 650 lines (W4b and W5 grew it from 362), so the cancel routes would push it past 800, where `modules_over_800_lines` has no room. They go in a **new Web module** that the write route group registers (`register_write_routes`). Web → TaskGraph and Web → Writes already exist, so this adds no new edge.
  - A new module needs entries in `[components]` and `[component_docs]` (`docs/architecture.toml`), a `[domain]` include or exclude entry, and the import-linter lists in `pyproject.toml`.

**Decisions** (proposed by review, approved by Dave)

- **D1. Placement.** The walk is a new module in the **TaskGraph** component, typed against its own narrow client Protocol (as `graph_fanout.GraphScopeClient` is), so it adds no cross-component edge. `receipts.py` (Writes) cannot import it. So the cancel facts travel on `WriteReceipt` as plain fields and `ReceiptTask` tuples, filled in by the new Web module (F10). Lower bounds and unread counts reuse the `released_exact` / `released_unread` pattern W5 shipped.
- **D2. Statuses, bounds and order.**
  - **Statuses.** The walk learns which dependents are open from one cross-project `list_tasks(status="open")` read — W4b's gate override reads the same way — not from a `task_get` per node. If that read fails, the page says the consequences could not be computed and why, and still offers the cancel.
  - **Lower bounds.** Over `max_tasks` nodes, after any failed edge read, or past a module-constant deadline (10 s, "took too long"), both numbers render "≥ N" with the reason.
  - **Cycles.** The walk keeps a visited set and never counts the focal task.
  - **"The first few"** means five (`receipts.MAX_TITLED_RELEASES`), ordered by `created_at` then id.
- **D3. With `confirm_cancel = true`, the confirmation cannot be skipped** (PRD user story 25). Follow W4b's pattern:
  - the confirm page's form carries the hidden confirmation;
  - `confirm_page(task)` returns the GET URL when the POST is unconfirmed;
  - the funnel answers 303 to that page, recorded as `rejected` / `confirmation_required`.
- **D4. With `confirm_cancel = false`, the facts are computed inside `perform`, before the `lithos_task_cancel` call.** That is the only point at which the claims still exist.
  - `perform` catches its own errors in the facts read. A failed walk degrades the facts to lower bounds or "unknown"; it never becomes the write's failure.
  - The receipt carries the facts **only** in this mode (REQUIREMENTS: "instead"). With `confirm_cancel = true` the receipt says the task was cancelled and repeats the reason's not-stored note.
  - `GET /cancel` still renders in both modes; it is a read.
- **D5. Cancel is a plain form POST everywhere, not HTMX.** D4 allows HTMX for row actions but does not require it, and a plain 303 keeps this slice off W4's fragment path.
- **D6. The row overflow menu is new.** Build one shared partial, a no-JS `<details>` / `<summary>⋯</summary>` menu. Include it in `tasks/row.html` (open tasks only — that template also renders the completed and cancelled sections) and in `tasks/gate_row.html`. It renders only when an identity resolves. It holds only a link to the confirm page (D9 — not the PRD mock's computed summary), or the direct form when `confirm_cancel = false`. A reconcile re-render collapses an open menu; accept that, and do not add menu-state preservation to `tasks.js`.
- **D7. `GET /cancel` on a task that is not open** answers W2's conflict page ("This task is now *\<status\>*.", code `stale_status`, 409) and shows no consequences. An absent task gets "this task no longer exists"; a failed read is 503.
- **D8. Cancel's `admits` refuses any task that is not open**, so a form claiming `expected_status=cancelled` on a cancelled task makes no call. That crafted case renders the refused page.
- **D9. Open children** are stated as a count plus the first five titles: "its N open children are not cancelled with it". This applies to any task with children, not only epics.
- **D10. Telemetry.** The arguments are `{"task_id", "reason_chars"}`; the reason's text never reaches the audit line or the span.
- **D11. e2e.** The artifact is a GET-only capture of `/tasks/loom-schema/cancel` on the writes server, so the chain stays intact for every later step. If a POST step is added, it cancels an isolated task (`loom-docs-tidy`) as the last step of the serial writes `describe`.

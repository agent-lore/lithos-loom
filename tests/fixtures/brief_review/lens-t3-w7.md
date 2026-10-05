

**Clarifications (2026-10-05, Dave)** — binding. Checked against lens main `1b8c59e` (W1–W5 merged); W6 (cancel) merges before this slice starts. They pin down the build above, and the scope cuts below narrow it. Where one contradicts the brief or the PRD, the clarification wins.

**Scope cuts** (decided by Dave, 2026-10-05) — they replace the matching parts of the brief and its acceptance:

- **S1. De-duplication is in memory only. There is no Lithos lookup and no Check again.**
  - Lens keeps each request id's outcome in a bounded in-process map (D5). A double-click, a resubmit or the back button lands on the task the first submit created.
  - The task still carries `metadata.lens_request_id`, as provenance and so that a lookup can be added later.
  - Remove from the brief: rule 2 ("lookup before create") and **Check again**.
  - Acceptance:
    - "A resubmit after the first finished creates nothing and lands on the existing task" now holds through the in-memory map.
    - "**Check again** after the fake completes reports the task" is removed.
    - The stated guarantee gains: "a Lens restart between a submit and its resubmit forgets the request id".
- **S2. No datalist of open tasks.** Parent and predecessors are typed or pasted as full or short ids. **Add child** pre-fills the parent.
- **S3. Ambiguous-prefix candidates are listed as text** under the field the prefix was typed in (short id and title), with the input kept. The operator corrects the field; there are no choice controls. The acceptance line becomes: "An ambiguous parent prefix re-renders the form listing the candidates under the parent field."
- **S4. A person creates only `human`, `external_task` and `timer` gates.** A hand-made `ci` or `pr` gate has nothing watching it; loom creates its own `pr` gates with the PR's metadata. `ci`, `pr` and any other type are refused before any Lithos call, as the unknown type in the brief's acceptance is.
- **S5. The documents follow what ships.** Update REQUIREMENTS §5C.2 "Create", PRD D10 and PRD Testing Decisions (Create) to S1–S4, and state the restart limit in `docs/SPECIFICATION.md`.

**Facts** (corrections — no judgment involved)

- **F1. Not independent of W6.** W6 merges first. Follow how its new Web module receives the funnel: `funnel` is a local in `register_write_routes`.
- **F2. The funnel assumes an existing task** (`write_funnel.py`):
  - it takes `WriteForm(task_id, expected_status)`;
  - an `expected_status` outside `TASK_STATUSES` is refused as `bad_form` (400), so a create posted through `submit` is refused there;
  - the pre-check is a `task_get`, and `describe`, `admits`, `confirm_page` and `perform` all take a `TaskRecord`;
  - an unknown outcome re-reads `task.id`;
  - `_refuse` always renders a standalone page, never a form;
  - `back_to` is fixed before the call;
  - the span's `task_id` comes from the form.

  Reusable unchanged: `origin_refusal`, the no-operator path, `ensure_registered`, `_mint` and `_record`.
- **F3. Do not add a lookup.** `list_tasks` has no `metadata_match` parameter in Lens's protocol, client or fake. With S1 none is needed, and this slice adds no contract variant for it.
- **F4. Fake and test seams.**
  - The fake's `task_create` resolves prefixes, returns the resolved `depends_on` / `parent_task_id`, and emits `task.created` when a hub is wired.
  - There is **no** "times out now, lands later" hook. Patterns to copy: the `LostAnswer` subclass (`tests/test_complete_gate.py:318`) and `_BarrierClient`'s `asyncio.Event` (`tests/test_operator_identity.py:391`).
  - There is no HTTP-level concurrency harness.
- **F5. W2's create rows.**
  - **Field placement.** It comes from parsing the message (`_field_named_in`, `write_errors.py:440-487`), keyed by Lithos parameter names: `parent_task_id`, `depends_on`, `metadata.gate_type`, `metadata.ready_at`. A `depends_on` error does not say *which* predecessor.
  - **`ambiguous_id_prefix`.** It sets no field; its envelope has no `prefix` key (the message carries it); and `writes/notice.html` renders the candidates as links to task pages, which leave the form, so S3 renders them in the form instead.
  - **Codes that fall to the unmapped path:** `invalid_task_type` and `invalid_metadata_key`.
  - **`parent_exists`.** Create's contract and the fake never raise it, despite PRD D6.
- **F6. Layering and `ready_at`.**
  - **Layering.** TaskGraph (`gates.KNOWN_GATE_TYPES`) and Writes are both Foundation, so a Writes → TaskGraph import is a budget item, not a layering violation. Writes may **not** import `lithos_client` or `mcp_transport` (Core): that is an import-linter rule. A Writes module that calls Lithos types its client with a structural Protocol, as `operator.OperatorLithosClient` does.
  - **`ready_at`.** `tasks.parse_timestamp` reads a naive value as UTC, as Lithos does.
- **F7. Scripts.** `tasks.js` opens the SSE stream on any page that loads it. Every CSS class a template emits needs a rule.
- **F8. Projects.**
  - The tag key is `[tasks].project_tag_key` (default `"project"`).
  - **No helper writes both conventions**; that is new code.
  - The project list comes from `task_filtering.project_universe(tasks, filters)`, the helper the board's filter uses (`filter_options.py:91`). It is the union of both conventions' slugs over the tasks it is given.
  - The board's filters are `project` (multi-valued) and `epic`.
  - The `short_id` filter shows 8 characters.
- **F9. `/tasks/new`.** It is reserved (`tasks.py:137-139`) and answers 404 at `web.py:443`. That is pinned by `tests/test_operator_identity.py:1028-1052` and `SPECIFICATION.md:158-162`, all of which this slice flips.
- **F10. Edge cache.** `task.created` evicts only the new id (`events.py:379`), so a parent's and its predecessors' edges stay cached.
- **F11. Budgets are guides, not limits.**
  - Today: `cross_component_edges` 38/38; `max_module_lines` 849/850 (`tasks.py`); `modules_over_800_lines` 5/5 (`web.py` at 830 and `write_errors.py` at 825 are among the five); `write_routes.py` 650.
  - A budget is a prompt for a decision (the header of `docs/architecture.toml`). Choose the structure that reads best. When that crosses a budget, raise it in `docs/architecture.toml` and give the reason in the diff.
  - Never split a module, duplicate code, or contort a design just to stay under a number.
- **F12. e2e.**
  - The writes server runs `writes.spec.ts` serially, with `retries: 0`.
  - Its steps photograph `BOARD` = `/tasks?project=influx&since=2026-08-01`.
  - The default server sets no `default_operator`, so "New task" stays out of the existing captures.

**Decisions** (proposed by review, approved by Dave)

- **D0. Placement follows cohesion.** Create is a self-contained unit — a form model, a de-duplication coordinator and two routes — so it gets its own modules on the merits:
  - the form model and the coordinator in **Writes**, typed against a structural client Protocol (F6);
  - the routes in a new **Web** module, registered by the write route group.

  Where a piece reads better elsewhere, put it there and raise any budget it crosses (F11).
- **D1. A task-less entry point.** Add `submit_create` to the funnel.
  - **Shared with `submit`:** the Origin check, identity and register-once, the audit line, span and counter, receipt minting, and the answer.
  - **In place of the pre-check:** Lens's validation (refused as `rejected`, the form re-rendered), then the create through the coordinator (D5).
  - **Refusals.** A REFUSED problem from create re-renders the form, 422 with the input kept; no conflict case applies.
  - **Plain POST only.** On success, 303 to the new task's detail page with the receipt.
  - **Span.** `task_id` is the new id once known; `expected_status` and `observed_status` stay empty; the request id is a span and audit attribute.
- **D2. Telemetry for de-duplicated submits.** A resubmit that lands on a remembered task, and a waiter that joins an in-flight create, are both result `ok`. A span and audit attribute `dedup` = `remembered` / `joined` marks them, never a counter label.
- **D3. No identity, no form.** Without an identity, `GET /tasks/new` renders only the "choose an operator" link, so nothing is typed that a redirect would lose. A POST without an identity redirects to `/operator?next=/tasks/new…` (with the pre-fill query) and its input is not replayed.
- **D4. The task carries its request id** as `metadata.lens_request_id`. Nothing in this slice reads it back from Lithos (S1).
- **D5. The coordinator.** It is in-process, built in the routes module's registration closure like `registry` and `receipts` — not on `AppState`. It holds one map from request id to entry, count-bounded (a module constant; oldest settled entry evicted first; no TTL). By entry state:
  - **No entry:** run the create, shielded from waiter cancellation (the `graph_cache.py:300-347` pattern).
  - **In flight:** wait for it and receive *its* outcome *value*. Each waiter mints its own receipt, because receipts are consumed once.
  - **Created:** land on that task, with no call.
  - **Unknown outcome:** never call create again under that id. Show "not visible yet" (D7).
  - **Refused:** drop the entry. Nothing was created, so the re-rendered form keeps the same id and a corrected submit may create.
  - A restart empties the map, and the SPECIFICATION says so (S5).
- **D6. The request id.** The server generates it when it renders the form: `uuid4().hex`, validated as `^[0-9a-f]{32}$`. A POST without a valid one is `bad_form` (400) with no call.
- **D7. Not visible yet.** After an unknown outcome, create's own page says: "Lens could not confirm this task was created — it is not visible yet. If it was created, it will appear on the board." It never says "not created". It offers two things:
  - a link to the board, filtered to the project when there is one;
  - **Start again**, a POST with `intent=restart` and the full input, which re-renders the form with the input kept and a **new** request id. It makes no call and is not an attempt.
- **D8. Errors on the form.**
  - Map Lithos parameter names to inputs: `title`, `parent_task_id` → parent, `depends_on` → predecessors, `metadata.gate_type`, `metadata.ready_at`.
  - For `ambiguous_id_prefix` (S3), find the field by matching each typed value against the candidate ids by prefix, and list the candidates under that field.
  - The mapping belongs with the create form model, because it describes that form.
  - Do not build `parent_exists` for create.
- **D9. No arbitrary metadata.** Drop "advisory metadata keys pass through verbatim": the form has no input for them. A created task's metadata is the project, `lens_request_id` and the gate's fields — nothing else.
- **D10. The gate fieldset.**
  - **The gate types a person may create** (S4) are their own ordered tuple in the form model: `human`, `external_task`, `timer`. That is a policy subset, not a copy of `KNOWN_GATE_TYPES`; a test asserts it stays a subset.
  - `ready_at` is a `datetime-local` input labelled UTC and parsed as UTC. A past instant is allowed: that timer is simply already ready.
  - The model drops gate fields for a non-gate, because the no-JS form posts them anyway.
  - The show/hide toggle is a **new small static script**, not `tasks.js`.
- **D11. Input formats.**
  - **Tags and predecessors:** one per line in a textarea, because a comma can be part of a tag.
  - **Parent:** a single input.
  - **Project:** optional free text with a datalist of known projects (D13), validated as a lowercase slug and pre-filled from `?project=`. A slug not in the list is accepted; empty writes no project.
- **D12. Pre-fill and the affordances.**
  - The dashboard's **New task** carries `?project=` only when exactly one project is selected.
  - **Add child** is offered on open epics only, and carries `?parent=`.
- **D13. The project list is the form page's only Lithos read.**
  - The page makes one cross-project `list_tasks(status="open")` read and passes it to `project_universe`, the same derivation the board's project filter uses. A project with no open task is not listed, but can still be typed.
  - The list is an enhancement only. A failed read renders the form without it, so creating still works when Lithos is slow.
  - There is no datalist of tasks (S2).
- **D14. Keeping edges fresh.** After a successful create with a parent or predecessors, evict each of them from the graph edge cache, so their pages show the new edge at once.
- **D15. Create carries no `expected_status`.** REQUIREMENTS §5C.6's `expected_status` rule covers forms on an existing task and does not apply to create. Say so in the SPECIFICATION when the 404 is flipped (F9).
- **D16. e2e.** The artifact is GET-only on the writes server, with `gate` selected via `selectOption`. If a create POST step is added, it is the last step, and creates into a project that is not on `BOARD`, so no capture shows it.
- **D17. Tests.**
  - Test the coordinator directly:
    - two concurrent submits make one create;
    - a create that times out and lands later leaves its id remembered as unknown, and a second POST under it makes no create call;
    - a remembered created id lands on its task;
    - a refused id is not remembered.
  - Add one route-level test proving the route goes through the coordinator.

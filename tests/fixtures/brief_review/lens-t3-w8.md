

**Clarifications (2026-10-05, Dave)** — binding. Checked against lens #113 @ `392301b` (W1–W6 merged); W7 (create) merges before this slice starts. The scope cuts narrow the build above; the rest pins it down. Where one contradicts the brief or the PRD, the clarification wins.

**Scope cuts** (decided by Dave, 2026-10-05) — they replace the matching parts of the brief and its acceptance:

- **S1. No removal.** Drop "Remove this dependency" and everything behind it:
  - `POST /tasks/{task_id}/edges/remove`;
  - a `lithos_task_edge_delete` client method, contract and fake behaviour;
  - the receipt's `created_by` / `created_at` stamps.

  A mis-drawn edge is removed outside Lens with `lithos_task_edge_delete` (live upstream since Lithos 0.6.0); Lens can add a delete later. Remove from the acceptance:
  - the "Removal: …" bullet;
  - "no remove form renders";
  - "one receipt offers removal";
  - "The delete tool's contract file passes the contract suite".

  "Before starting: the delete tool" no longer applies. Of the residuals, keep only the one that still holds: another agent inserting the same relation between Lens's read and its upsert has its metadata replaced. State it in the docs.
- **S2. No per-relation lock.** Without a removal offer, two concurrent submits of one relation at worst upsert the same edge twice, with the same (empty) metadata. Remove the "Concurrent same-operator submits" acceptance bullet.
- **S3. No synthetic event.** After a successful write, evict **both** endpoints from the graph edge cache in-process. The page the redirect lands on renders from fresh reads; other tabs converge on the edge cache's TTL or their next event.
  - Drop the "Convergence" bullet, the hub change, and the JS work in `tasks.js` / `graph.js`.
  - The acceptance bullet becomes: "A successful write evicts both endpoints from the edge cache."
- **S4. Three sentences:**
  - "this task is blocked by ▁" → `blocks`, other → this;
  - "this task blocks ▁" → `blocks`, this → other;
  - on a gate's detail page only, "▁ waits on this gate" → `waits_on_gate`, this gate → other.

  No `parent_child` sentences (a parent is set when a task is created — W7) and no `discovered_from`, which is provenance agents record.
- **S5. The documents follow what ships.** Update to S1–S4:
  - REQUIREMENTS §5C.2 "Add dependency edge" — including the line saying it ships "only once Lithos can delete a task edge", and the claim that no delete tool exists;
  - REQUIREMENTS §5C.3 cross-tab convergence and the `lens.*` namespace list (no `lens.edge_upserted`);
  - PRD D11 and Testing Decisions (Edge);
  - `docs/SPECIFICATION.md`;
  - ROADMAP ledger #2: the delete tool exists upstream, and Lens does not use it yet.

**Facts** (corrections — no judgment involved)

- **F1. Not independent of W7.** W7 merges first. Follow W6's and W7's pattern: a route module of its own, registered from `register_write_routes` as `register_…_routes(app, state, templates, funnel)`. Reuse whatever W7 added to the funnel for a refusal that re-renders a form, and for ambiguous candidates listed as text.
- **F2. The funnel** (`write_funnel.py`):
  - `expected_status` is mandatory; outside `TASK_STATUSES` it is `bad_form`.
  - `admits` is synchronous and sees only the `TaskRecord`.
  - `describe` runs before the call.
  - **Anything `perform` raises is classified as the write's own failure.**
  - Action-specific facts travel on `WriteDone` / `WriteReceipt` as fields (W6's `CancelFacts` is the precedent), and `_mint` copies them field by field.
  - A refusal names the focal task.
- **F3. The edge client and the fake.**
  - `task_edge_upsert(*, from_task_id, to_task_id, edge_type, agent, metadata)` → `TaskEdgeUpsertResult`.
  - `task_edge_list(task_id, direction="both", types=None)` → `EdgeRecord`s carrying `metadata`, `created_by`, `created_at` and `direction`.
  - An upsert on an existing `(from, to, type)` replaces its metadata, upstream and in the fake. The fake keeps the stamps and emits no event.
  - Seed edges in the fake have empty `created_by` / `created_at`.
- **F4. The edge cache.**
  - A fresh read is `state.graph_cache.evict(id)` then `await state.graph_cache.edges_for(id, fetch)`, the pattern in `cancel_consequences.py`, with `fetch` = `task_edge_list` under `LINK_READ_TIMEOUT_S`.
  - Entries live 30 s.
  - Events evict only `event.task_id`.
- **F5. Resolving the other task by prefix.**
  - `lithos_task_get` resolves prefixes upstream since 0.5.0 (`src/lithos/tools/tasks.py:1200`).
  - Lens's vendored `tests/contracts/lithos_task_get.json` (0.4.0) lists only `task_not_found`.
  - The fake's `task_get` matches exact ids only (`fake_lithos.py:320`), while `FakeWriteStore.resolve_id` (`fake_writes.py:503`) resolves prefixes.
- **F6. W2's rows** for `cycle` (verbatim, ids linked as presentation only), `self_edge`, `not_a_gate`, `ambiguous_id_prefix`, `task_not_found` (split by the re-read) and `invalid_edge_type` (the Lens-defect path) all exist. `parent_exists` is unreachable after S4.
- **F7. The detail page** has no single relations section. Its actions are in the header action block of `tasks/detail.html`, which W7 also extends with Add child. Test for a gate with `task.task_type == "gate"`: `detail.gate_type` is empty when `metadata.gate_type` is missing.
- **F8. Readiness depends on the other task's status.**
  - A completed blocker is already satisfied.
  - A cancelled blocker strands its dependent (`blocker_unsatisfiable`).
  - A `waits_on_gate` waiter waits until the gate resolves: completed, or a timer gate past `ready_at`.
- **F9. Telemetry.** `WriteAction` already includes `edge_upsert`, so the span `lens.writes.edge_upsert` is automatic.
- **F10. The vendored upsert contract's** canonical request carries `metadata={"added_via": "lithos-lens"}`; REQUIREMENTS sends none.
- **F11. Budgets are guides, not limits.**
  - Today: `cross_component_edges` 38/38; `max_module_lines` 849/850; `modules_over_800_lines` 5/5.
  - A same-tier import (Writes → TaskGraph — both Foundation) is a budget item. Only a cross-tier import (Foundation → Core) is a layering rule.
  - Choose the structure that reads best. Raise a budget it crosses in `docs/architecture.toml`, with the reason in the diff. Never split, duplicate or contort code to fit a number.
- **F12. e2e.** The writes server (port 8126) runs `writes.spec.ts` serially and never resets.

**Decisions** (proposed by review, approved by Dave)

- **D1. Placement by cohesion.**
  - The relation-sentence model is pure (sentence ↔ `from` / `to` / `type`, plus the readiness wording of F8). It belongs with the edge types and `EdgeRecord` in **TaskGraph**.
  - The routes go in their own **Web** module (F1).
- **D2. The entry and the confirm step.**
  - On the detail page of an **open** task, one small form takes a sentence and the other task's id or prefix, and GETs `/tasks/{task_id}/edges/new`.
  - The confirm step:
    - resolves the other task with `task_get`, by prefix (D8);
    - reads the focal task's edges fresh (F4);
    - if the relation already exists, renders "already exists" (D5) with no form;
    - otherwise restates the relation with both titles and its readiness meaning by status (F8), and a form posting to `POST /tasks/{task_id}/edges`.
  - That form carries the resolved **full** ids, the type, and the focal task's status as `expected_status`.
- **D3. The write.** `POST /tasks/{task_id}/edges` goes through the funnel, and `perform` does three things in order:
  1. reads the focal task's edges fresh;
  2. if the relation exists, makes **no** upsert, and the receipt says "already exists";
  3. otherwise upserts, then evicts both endpoints (S3).

  **If the fresh read fails, nothing is written:** an upsert could replace the metadata of an edge Lens could not see. Refuse with 503 and `precheck_failed`, "Lens couldn't check whether this relation exists — nothing was written". Use W7's refusal path if it fits; otherwise add the smallest funnel hook that lets `perform` return a refusal without it being classified as the write's own failure.
- **D4. No metadata on the upsert** (REQUIREMENTS). The edge's `created_by` already records the operator. The vendored contract's canonical example stays as it is: it describes the payload's shape, not Lens's policy.
- **D5. "Already exists" is a success, not a refusal.**
  - Result `ok`, with an `already_exists` fact on `WriteDone`, carried to the span and audit attribute and to the receipt.
  - Copy: "This relation already exists (added by \<agent\>, \<date\>); nothing was written." When `created_by` / `created_at` are empty (F3), drop the parenthesis.
- **D6. Refusals.**
  - `cycle`, `self_edge`, `not_a_gate`, `task_not_found` and `invalid_edge_type` go through W2's rows, and each says nothing was written.
  - An ambiguous prefix on the confirm step lists the candidates as text under the input, as W7 does, for the operator to retype.
- **D7. Telemetry.** The arguments are `{"from_task_id", "to_task_id", "type"}`.
- **D8. Prefix resolution on GET.** Add the prefix envelopes `lithos_task_get` raises (`invalid_input`, `task_not_found` for an unmatched prefix, and `ambiguous_id_prefix` with `candidates`) to its vendored contract, transcribed from the Lithos source with the `source` citation. Route the fake's `task_get` through `resolve_id`, and run `make contracts-verify` on the host, recorded in the PR.
- **D9. e2e.** The artifact is a GET-only capture of the confirm step on the writes server, for `loom-docs-tidy` "is blocked by" `loom-metrics-note`. Both are open and unconnected, and W4–W6 do not touch them. If W7's steps use either one, pick another unconnected pair.
- **D10. Tests.**
  - The sentence model: each of the three sentences produces the right `from` / `to` / `type`, and the readiness wording covers each status case.
  - **Already exists:** an edge present in Lithos but absent from the cache reports "already exists" on both the confirm step and the POST, with no upsert in the call log.
  - **A failed fresh read** makes no upsert.
  - **A successful write** evicts both endpoints.
  - **The gate-only sentence** is offered only when `task_type == "gate"`.

"""The develop-run on-disk contract: classify a run's fate from its markers.

A story-develop run communicates its fate between three processes — the plugin
subprocess that runs it, the daemon that delivers its PR, and the ``lithos-loom
develop`` CLI that observes it — entirely through files in the run dir
(``<work_dir>/<task_id>/<run_id>/``) and the shared per-task dir
(``run_dir.parent``). This module owns the **read/classify half** of that
contract so the invariants live in one place instead of being duplicated as prose
across the reader (``cli/develop.py``) and the writer (``story_develop/__main__``,
``pr_delivery``). The write half (the delivery markers) lands in a later slice.

Marker inventory (read side):

- ``state.json`` (run dir) — the dialogue verdict, written by ``develop()`` at run
  end. Read by :func:`read_state`. An ``approved`` verdict is NOT terminal on its
  own: in daemon mode PR delivery runs *after* ``develop()`` returns (#171).
- ``result.json`` (shared per-task dir) — the plugin's final contract output,
  written after delivery. Bound to THIS run by ``run_id == run_dir.name`` (#198)
  so a prior run's leftover can't false-done a retry. Read by
  :func:`result_for_run` / :func:`delivery_complete`.
- ``delivery.json`` (run dir, private) — the delivery deadline (#189) and/or a
  best-effort delivery-failure marker (#194). Read by :func:`delivery_deadline` /
  :func:`delivery_failed` / :func:`delivery_timed_out`.
- ``conversation.md`` (run dir) — the teardown marker (the plugin writes it just
  before ``state.json``); its presence means the run reached teardown. Read by
  :func:`capture_outcome`.
- run-dir **absence** — the route-runner reaps the dir after applying a succeeded
  result, so its absence is itself an end signal; the outcome is then recovered
  from the host-persistent completion store (:func:`recover_reaped_outcome`).

Rendering (``_outcome_line`` / ``_outcome_event``) and the observe/attach loop
stay in ``cli/develop.py`` and consume :class:`RunOutcome` + these classifiers.

Imports stay light (stdlib + ``.idempotency``) so ``cli/develop.py`` can read the
contract without dragging in the plugin's runtime dependencies.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .idempotency import lookup_completed_for_run

# Marker filenames, single-sourced (the writer half adopts these in a later slice).
STATE_FILE = "state.json"
RESULT_FILE = "result.json"
DELIVERY_MARKER = "delivery.json"
CONVERSATION_LOG = "conversation.md"

# The only success status; an approved dialogue still has PR delivery to do.
APPROVED = "approved"

# Fallback bound for an in-flight delivery when the daemon recorded no deadline
# (a run predating #189, or one whose marker write failed). > the full DEFAULT
# delivery budget (copilot 600 + coder 3600 + gate 900 + overhead 1800 = 6900s;
# see pr_delivery.delivery_budget_seconds) so it can't false-fire on a
# default-config run.
DELIVERY_FALLBACK_SECONDS = 9000.0  # 2.5 h

RunPhase = Literal["running", "delivering", "terminal", "vanished"]


def read_state(run_dir: Path) -> dict | None:
    """The run's terminal ``state.json`` (status + rounds + branch), or ``None``.

    Written by the plugin only at run end, alongside ``conversation.md``.
    """
    try:
        data = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def result_for_run(run_dir: Path) -> dict | None:
    """THIS run's ``result.json`` (the plugin's final contract output), or ``None``.

    ``result.json`` lives in the SHARED per-task dir (``run_dir.parent``), so a
    prior run of the same task can leave one behind. #198 binds it to the run by
    ``run_id``: the file is THIS run's iff its ``run_id`` equals ``run_dir.name``.
    The earlier "a succeeded survivor must be the current run because a success is
    reaped" reasoning relied on a BEST-EFFORT reap (``_cleanup_work_dir`` suppresses
    ``rmtree`` ``OSError``) and didn't cover a failed result at all; the explicit
    run_id binding removes that dependency. A result without ``run_id`` (an old
    daemon's) does not bind — safe direction (treated as not-this-run).
    """
    try:
        data = json.loads((run_dir.parent / RESULT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data if data.get("run_id") == run_dir.name else None


def delivery_complete(run_dir: Path) -> bool:
    """Whether THIS approved run's post-dialogue PR delivery succeeded.

    ``develop()`` writes ``state.json`` the moment the dialogue approves, but in
    daemon mode the branch push, the Copilot round, and the ``result.json`` write
    all happen AFTER it returns (``story_develop/__main__`` calls ``deliver()``
    then ``write_result_atomically``). ``result.json`` — the plugin's final
    contract output — is the "fully delivered" signal, bound to this run by
    ``run_id`` (:func:`result_for_run`) so a prior run's leftover can't false-done
    a retry.
    """
    data = result_for_run(run_dir)
    return data is not None and data.get("status") == "succeeded"


def delivery_failed(run_dir: Path) -> str | None:
    """The reason THIS run's PR delivery FAILED (#194), or ``None``.

    When ``deliver()`` raises before a PR exists (e.g. ``push_branch()`` /
    ``gh pr create`` fails), the daemon records the failure in this run's PRIVATE
    ``run_dir/delivery.json`` marker so attach reports it at once rather than
    sitting in ``"delivering"`` until the #189 deadline. The marker write is
    BEST-EFFORT, though, so when it's missing fall back to this run's terminal
    ``result.json`` (run_id-bound, ``status: failed`` with a ``delivery`` error) —
    the durable contract output, written atomically (#198, Hole 2). An approved
    dialogue's failed result is always a delivery failure (``build_result_payload``
    maps approved→succeeded otherwise), so the category check is just defensive.
    """
    try:
        data = json.loads((run_dir / DELIVERY_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and data.get("failed"):
        reason = data.get("reason")
        return str(reason) if reason else "PR delivery failed"
    result = result_for_run(run_dir)
    if result is not None and result.get("status") == "failed":
        error = result.get("error")
        if isinstance(error, dict) and error.get("category") == "delivery":
            return str(error.get("message") or "PR delivery failed")
    return None


def delivery_deadline(run_dir: Path) -> datetime | None:
    """The instant this run's delivery budget expires (#189), or ``None``.

    The daemon writes ``run_dir/delivery.json`` with an absolute ``deadline``
    (its own ``copilot_timeout + coder_timeout`` budget) before delivery starts.
    Reading it lets attach bound a crashed/orphaned delivery WITHOUT timing out a
    delivery still inside its budget — which attach can't otherwise size, since the
    budget is the daemon's configurable flags.
    """
    try:
        data = json.loads((run_dir / DELIVERY_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("deadline") if isinstance(data, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        deadline = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return deadline if deadline.tzinfo else deadline.replace(tzinfo=UTC)


def delivery_timed_out(run_dir: Path, *, delivering_seconds: float) -> bool:
    """Whether an in-flight delivery has exceeded its bound (#189).

    Prefers the daemon's recorded deadline (so a delivery inside its own budget is
    never falsely timed out); falls back to a generous flat grace only when no
    deadline was recorded — generous enough not to false-fire on a default budget.

    *delivering_seconds* is the wall-clock spent in the current delivering episode;
    the caller passes ``polls * poll_interval`` (the polls→seconds form of the
    former poll-count grace — equivalent for the CLI's fixed poll cadence).
    """
    deadline = delivery_deadline(run_dir)
    if deadline is not None:
        return datetime.now(UTC) >= deadline
    return delivering_seconds >= DELIVERY_FALLBACK_SECONDS


def run_phase(
    run_dir: Path,
    state: dict | None,
    *,
    containers_running: bool | None,
    seen_container: bool,
) -> RunPhase:
    """Classify the run for ``attach``: ``"running"`` / ``"delivering"`` /
    ``"terminal"`` / ``"vanished"``.

    Terminal *state*, not agent *liveness*. The graceful terminal signal is the
    run's recorded **outcome** — not container liveness and not ``conversation.md``
    (the plugin writes the log *before* ``state.json``, so stopping on the log
    would misreport an approved run). But the verdict in ``state.json`` is not the
    whole story: ``develop()`` writes it the instant the dialogue ends, while in
    daemon mode the **PR delivery** (branch push, Copilot round, ``result.json``)
    all happen *after* it returns. So an **approved** verdict alone is NOT
    terminal — exiting there is the #171 false-done window (attach quits while the
    PR is still being pushed). We stay in ``"delivering"`` until this run's
    ``result.json`` lands (or the work dir is reaped on success).

    - ``"terminal"`` — a non-approved outcome is recorded, or an approved run's
      delivery has completed (:func:`delivery_complete`), or the run dir was
      **reaped** (the route-runner removes it after applying the result, so its
      absence is itself an end signal; the outcome is then recovered from the
      completion store — see :func:`recover_reaped_outcome`).
    - ``"delivering"`` — the dialogue **approved** but post-approval PR delivery
      is still in flight (``result.json`` not yet written, dir not yet reaped).
      Keep following; the caller renders a distinct "delivering PR…" phase.
    - ``"running"`` — an agent container is up (``containers_running`` True), or
      we're still in the **startup window** (``containers_running`` False but no
      container seen yet), or docker is absent (``containers_running`` None) and
      the run dir is still present (can't observe containers — keep following).
    - ``"vanished"`` — docker showed the agent containers, having been **seen**,
      are now all gone but no outcome is recorded yet. Ambiguous: either the
      normal teardown window before ``state.json`` is written, or a hard crash.
      The caller grace-polls before deciding.

    *containers_running* is ``None`` when docker is unavailable (can't observe),
    else whether any agent container is currently running — the two signals the
    classifier needs, decoupled from the CLI's ``ContainerStatus`` docker type.
    *state* is the already-read ``state.json`` for this poll (passed in so the
    caller can capture the exact dict it classified on, without a second read
    that could race the work-dir reap).
    """
    if state is not None and state.get("status"):
        # An approved verdict still has PR delivery to do in daemon mode — not
        # terminal until this run's result.json lands (or it's reaped on success,
        # handled below), UNLESS delivery already FAILED (#194), which is terminal
        # at once. Every other terminal status has no post-dialogue work.
        if (
            state.get("status") == APPROVED
            and not delivery_failed(run_dir)
            and not delivery_complete(run_dir)
        ):
            return "delivering"
        return "terminal"
    if not run_dir.is_dir():
        return "terminal"  # reaped by the route-runner after applying the result
    if containers_running is None:
        return "running"
    if containers_running:
        return "running"
    return "vanished" if seen_container else "running"


@dataclass
class RunOutcome:
    """A run's terminal outcome, captured the moment ``attach`` detects it.

    The follow loop snapshots this **before returning**, so the rendered summary
    survives the route-runner reaping the work dir on success — re-reading the
    (now-deleted) ``run_dir`` afterwards would misreport an approved run as a
    crash (correctness/f-003).
    """

    state: dict | None = None  # parsed state.json (or recovered) at capture time
    has_log: bool = False  # conversation.md present at capture time
    reaped: bool = False  # run dir removed by the route-runner's success cleanup
    delivery_timed_out: bool = False  # approved, but result.json never landed (#189)
    delivery_failed: bool = False  # approved, but PR delivery raised (no PR) (#194)
    pr_url: str | None = None  # the delivered PR url, when approved+delivered (#188)
    # why a run stopped (#188), or why its PR delivery failed (#194)
    failure_reason: str | None = None


def recover_reaped_outcome(run_dir: Path) -> dict | None:
    """Recover a **reaped** run's outcome from the host-persistent completion store.

    The route-runner removes the whole work dir on a succeeded result, taking
    ``state.json`` with it — and a follow can miss the brief window where the
    file exists (a poll lands before it is written, then the dir is gone by the
    next poll). The plugin records that success in the idempotency store *before*
    the dir is reaped, a source the route-runner never touches. The record is
    keyed by the (possibly explicit ``--idempotency-key``) key, so it is located
    by this run's id — bound to **this** run, not a prior success of the same
    task. A match means the run was approved (the only success).

    The record is this run's ``result.json`` payload, so it carries the delivered
    ``pr_url`` (#188) — surfaced here so a write-then-reap between two polls still
    names the PR.
    """
    record = lookup_completed_for_run(run_dir.parent.name, run_dir.name)
    if not record:
        return None
    return state_from_completion_record(record)


def state_from_completion_record(record: dict) -> dict:
    """Translate a completion-store record (a ``result.json`` payload) into the
    ``state.json``-shaped dict ``attach`` renders from.

    A recorded run is always **approved** (the only success). The record carries
    the round count (#196) and the delivered ``pr_url`` (#188), so a reaped or
    idempotency-replayed run — whose ``state.json`` was never seen — still reports
    a complete terminal summary (verdict + rounds + PR).
    """
    recovered: dict = {"status": APPROVED}
    if isinstance(record.get("rounds"), int):
        recovered["rounds"] = record["rounds"]
    if record.get("pr_url"):
        recovered["pr_url"] = str(record["pr_url"])
    return recovered


def delivered_pr_url(run_dir: Path, state: dict | None) -> str | None:
    """The delivered PR url for an approved run, or ``None`` (#188).

    A reaped run's recovered *state* already carries it (from the completion-store
    payload); otherwise read this run's ``result.json``, bound to the run by
    ``run_id`` (:func:`result_for_run`, #198) so a prior run's leftover PR url is
    never surfaced for this one.
    """
    if state and state.get("pr_url"):
        return str(state["pr_url"])
    data = result_for_run(run_dir)
    if data is not None and data.get("status") == "succeeded" and data.get("pr_url"):
        return str(data["pr_url"])
    return None


def capture_outcome(outcome: RunOutcome, run_dir: Path, state: dict | None) -> None:
    """Snapshot the terminal outcome into *outcome* from the already-read *state*.

    Done before the follow loop returns, while the result is still recoverable.
    When the run dir has been reaped (success cleanup) with no ``state.json``
    captured, the outcome is recovered from the completion store
    (:func:`recover_reaped_outcome`).
    """
    outcome.reaped = not run_dir.is_dir()
    if state is None and outcome.reaped:
        state = recover_reaped_outcome(run_dir)
    outcome.state = state
    outcome.has_log = (run_dir / CONVERSATION_LOG).is_file()
    # #188: enrich the summary — why a non-approved run stopped, and the PR url
    # of an approved+delivered one (only an approved run has a delivered PR).
    if state:
        outcome.failure_reason = state.get("failure_reason")
        if state.get("status") == APPROVED:
            # #194: an approved run whose delivery FAILED (raised before a PR
            # opened) is not a clean delivery — surface the reason, not a PR url.
            reason = delivery_failed(run_dir)
            if reason:
                outcome.delivery_failed = True
                outcome.failure_reason = reason
            else:
                outcome.pr_url = delivered_pr_url(run_dir, state)
                # #196 (Gap A1): the route-runner can rmtree the task dir between
                # the poll's state.json read and delivered_pr_url's result.json
                # read. When that race drops the url, recover it from the durable
                # completion store rather than losing it from the summary.
                if outcome.pr_url is None and not run_dir.is_dir():
                    recovered = recover_reaped_outcome(run_dir)
                    if recovered and recovered.get("pr_url"):
                        outcome.pr_url = str(recovered["pr_url"])


def is_clean_success(outcome: RunOutcome) -> bool:
    """Whether the run reached the only success status (``approved``) **and**
    fully delivered.

    A delivery that never completed (:attr:`RunOutcome.delivery_timed_out`, #189)
    or that FAILED (:attr:`RunOutcome.delivery_failed`, #194) is not a clean
    success — no PR exists — so ``attach --wait`` must exit nonzero, or ``attach
    --wait && gh pr view`` would race a PR that never opened.
    """
    if outcome.delivery_timed_out or outcome.delivery_failed:
        return False
    return bool(outcome.state and outcome.state.get("status") == APPROVED)

"""The develop-run on-disk contract: read/classify a run's fate, and write its markers.

A story-develop run communicates its fate between three processes — the plugin
subprocess that runs it, the daemon that delivers its PR, and the ``lithos-loom
develop`` CLI that observes it — entirely through files in the run dir
(``<work_dir>/<task_id>/<run_id>/``) and the shared per-task dir
(``run_dir.parent``). This module owns **both halves** of that contract — the
read/classify functions AND the delivery-marker writers
(:func:`record_delivery_deadline` / :func:`record_delivery_failure`) — so the
invariants live in one place instead of being duplicated as prose across the
reader (``cli/develop.py``) and the writer (``story_develop/__main__`` today;
``pr_delivery.deliver_guarded`` calls the writers after ARCH-1.S3).

Marker inventory (who writes / who reads each):

- ``state.json`` (run dir) — the dialogue verdict, written by ``develop()`` at run
  end. Read by :func:`read_state`. An ``approved`` verdict is NOT terminal on its
  own: in daemon mode PR delivery runs *after* ``develop()`` returns (#171).
  Three writers, all through :func:`write_state` (which MERGES): the loop's own
  exit, ``develop converge``'s intake record of the PR it is converging
  (:func:`record_converge_intake`, written before the first paid turn) and
  ``develop converge-push``'s record of what it pushed
  (:func:`record_converge_push`).
- ``result.json`` (shared per-task dir) — the plugin's final contract output,
  written after delivery. Bound to THIS run by ``run_id == run_dir.name`` (#198)
  so a prior run's leftover can't false-done a retry. Read by
  :func:`result_for_run` / :func:`delivery_complete`.
- ``delivery.json`` (run dir, private) — the delivery deadline (#189), a
  best-effort delivery-failure marker (#194), and/or the PR a HAND delivery
  (``develop deliver``, §4.15c) put this run's branch behind. Written by
  :func:`record_delivery_deadline` / :func:`record_delivery_failure` /
  :func:`record_manual_delivery`; read by :func:`delivery_deadline` /
  :func:`delivery_failed` / :func:`delivery_timed_out` / :func:`run_pr_url`.
- ``conversation.md`` (run dir) — the teardown marker (the plugin writes it just
  before ``state.json``); its presence means the run reached teardown. Read by
  :func:`capture_outcome`.
- ``owner.json`` (run dir) — WHICH host process is running this run, stamped at
  run start. Not in this module (it needs ``runner.orphans``, and this one stays
  a stdlib leaf): see :mod:`run_owner`. It is what tells ``develop prune`` that a
  run dir with no container and no epilogue is alive rather than abandoned.
- run-dir **absence** — the route-runner reaps the dir after applying a succeeded
  result, so its absence is itself an end signal; the outcome is then recovered
  from the host-persistent completion store (:func:`recover_reaped_outcome`).

Rendering (``_outcome_line`` / ``_outcome_event``) and the observe/attach loop
stay in ``cli/develop.py`` and consume :class:`RunOutcome` + these classifiers.

Imports stay light (stdlib + ``.idempotency``) so ``cli/develop.py`` can read the
contract without dragging in the plugin's runtime dependencies.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .idempotency import lookup_completed_for_run

# Marker filenames, single-sourced across the read + write functions here and
# develop()'s epilogue writers (STATE_FILE / CONVERSATION_LOG).
STATE_FILE = "state.json"
RESULT_FILE = "result.json"
DELIVERY_MARKER = "delivery.json"
CONVERSATION_LOG = "conversation.md"

# `develop converge` runs live under their own pseudo-task dir rather than a
# story's (``<work_dir>/converge/<run_id>/``), because a converge run has no
# Lithos source task — the PR is its subject. Named here, with the rest of the
# layout, so the two commands that must tell a converge run from a story run
# (``develop converge-push``, which is only for one, and ``develop deliver``,
# which refuses one) agree on the test.
CONVERGE_DIR = "converge"

# ``state.json`` blocks a converge run owns (nested, so the loop's own
# top-level keys — ``base_sha`` is both a PR fact and a fork point — can never
# be confused with the PR's).
CONVERGE_KEY = "converge"
CONVERGE_PUSH_KEY = "converge_push"

# The only success status; an approved dialogue still has PR delivery to do.
APPROVED = "approved"

# Fallback bound for an in-flight delivery when the daemon recorded no deadline
# (a run predating #189, or one whose marker write failed). > the full DEFAULT
# delivery budget (push/PR/gh overhead only since S2 slice D retired the
# inline Copilot round — 1800s;
# see pr_delivery.delivery_budget_seconds) so it can't false-fire on a
# default-config run.
DELIVERY_FALLBACK_SECONDS = 9000.0  # 2.5 h

RunPhase = Literal["running", "delivering", "terminal", "vanished"]


def is_run_dir(path: Path) -> bool:
    """A run dir is recognised by its seeded ``handoff/`` subdir.

    Part of the on-disk contract: the plugin seeds ``handoff/`` before the
    first round, so the directory is identifiable from the moment a run
    exists — before any marker file lands.
    """
    return path.is_dir() and (path / "handoff").is_dir()


def resolve_run_dir(work_dir: Path, key: str) -> Path | None:
    """Resolve *key* (a run_id or task_id) to a run dir, newest run if a task.

    The ``<work_dir>/<task_id>/<run_id>/`` layout is the contract, so the
    lookup lives here with the rest of it: ``develop attach`` / ``dump`` /
    ``prune`` and ``develop deliver`` all take the same operator-typed key.
    """
    # run_id: <work_dir>/<any task>/<key>
    matches = [
        run_dir
        for task_dir in (work_dir.iterdir() if work_dir.is_dir() else [])
        if task_dir.is_dir()
        for run_dir in [task_dir / key]
        if is_run_dir(run_dir)
    ]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    # task_id: <work_dir>/<key>/<newest run>
    task_dir = work_dir / key
    if task_dir.is_dir():
        runs = [r for r in task_dir.iterdir() if is_run_dir(r)]
        if runs:
            return max(runs, key=lambda p: p.stat().st_mtime)
    return None


def read_state(run_dir: Path) -> dict | None:
    """The run's terminal ``state.json`` (status + rounds + branch), or ``None``.

    Written by the plugin only at run end, alongside ``conversation.md``.
    """
    try:
        data = json.loads((run_dir / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_state(run_dir: Path, payload: dict) -> None:
    """Write the run's ``state.json``, MERGING over whatever is already there.

    ``develop()`` writes the dialogue verdict at run end, but it is no longer
    the only writer: ``develop converge`` records the PR it is converging
    **at intake** (:func:`record_converge_intake`), before the first paid
    turn, so a run killed mid-loop is still resolvable; and
    ``develop converge-push`` records its push afterwards. A plain overwrite
    would drop the intake block the moment the loop ended — so the loop's
    keys are laid over the file rather than replacing it.

    Top-level, last-writer-wins per key: the two extra writers own their own
    nested blocks (:data:`CONVERGE_KEY` / :data:`CONVERGE_PUSH_KEY`) and
    never a key ``develop()`` writes.
    """
    data = read_state(run_dir) or {}
    data.update(payload)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / STATE_FILE).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )


def is_converge_run_dir(run_dir: Path) -> bool:
    """Whether *run_dir* is a ``develop converge`` run's (``<work_dir>/converge/<id>``).

    The layout is the test, not the run's contents: a converge run always has
    a PR (the thing it converges), and a story-develop run never does — so
    ``deliver`` refuses the former and ``converge-push`` refuses the latter
    on this alone, before reading anything an agent could have written.
    """
    return run_dir.parent.name == CONVERGE_DIR


def record_converge_intake(
    run_dir: Path,
    *,
    pr_url: str,
    pr_number: int | None,
    pr_head_branch: str,
    intake_head_sha: str,
    base_sha: str,
    repo: str,
    story_id: str = "",
) -> None:
    """Record the PR a converge run is converging, at INTAKE.

    Everything ``develop converge-push`` needs to push an exhausted run's
    rounds onto the right branch — and nothing the loop's own ``state.json``
    carries: the run's ``branch`` is its LOCAL branch, not the PR's head, and
    nothing else on disk names the PR at all (the operator had to read it out
    of the daemon log or the process argv).

    Written before the first paid turn, so a run killed at any point after
    intake (SIGTERM, exit 143) is still resolvable — which is exactly the
    run an operator most wants to salvage. "Resolvable" needs the ``handoff/``
    dir too, not just the file: :func:`is_run_dir` — which every lookup here,
    in ``develop list`` and in ``develop deliver`` goes through — recognises a
    run BY that dir, and converge's own first paid phase seeds only the
    sibling ``<run>-intake``'s. So this seeds it (``develop()`` later adds to
    it; nothing clears it), and a run killed during the intake review is found
    rather than treated as nonexistent.
    """
    with contextlib.suppress(OSError):
        (run_dir / "handoff").mkdir(parents=True, exist_ok=True)
    write_state(
        run_dir,
        {
            CONVERGE_KEY: {
                "pr_url": pr_url,
                "pr_number": pr_number,
                "pr_head_branch": pr_head_branch,
                "intake_head_sha": intake_head_sha,
                "base_sha": base_sha,
                "repo": repo,
                "story_id": story_id,
            }
        },
    )


def record_converge_cost(
    run_dir: Path, *, intake_cost_usd: float, total_cost_usd: float | None = None
) -> None:
    """Record what the WHOLE converge command spent, not just its loop.

    ``develop()`` writes the loop's own ``cost_usd`` at its exit; converge's
    pre-loop phase — the local-panel intake review, or external mode's triage
    turn — is converge's own and lives only in its process. Merged in here so
    the run dir carries the figure ``develop converge-push`` puts in front of
    the operator's push decision.

    Called **twice**, and the order matters: ``intake_cost_usd`` alone before
    the loop starts, so that a reader arriving after ``develop()`` has written
    the terminal status but before the loop's own total lands can still SUM
    the two halves; then again with *total_cost_usd*, the authoritative
    figure. A run from before this records neither key and the reader falls
    back to the loop-only figure.
    """
    record: dict[str, object] = {"intake_cost_usd": round(intake_cost_usd, 4)}
    if total_cost_usd is not None:
        record["total_cost_usd"] = round(total_cost_usd, 4)
    write_state(run_dir, record)


def converge_intake(run_dir: Path) -> dict | None:
    """The PR facts :func:`record_converge_intake` wrote, or ``None``.

    ``None`` for a story-develop run, and for a converge run that predates
    the record — the caller then has no PR to push onto and says so.
    """
    state = read_state(run_dir) or {}
    block = state.get(CONVERGE_KEY)
    return block if isinstance(block, dict) else None


# Who put this run's rounds on the PR: its own push epilogue (``converge``
# approved and pushed) or the operator's decision (``develop converge-push``).
PUSHED_BY_CONVERGE = "converge"
PUSHED_BY_CONVERGE_PUSH = "converge-push"


def record_converge_push(
    run_dir: Path,
    *,
    pushed_sha: str,
    pr_url: str,
    by: str = PUSHED_BY_CONVERGE_PUSH,
    finding_posted: bool | None = None,
    gate_completed: str | None = None,
) -> None:
    """Record that this run's rounds are on the PR at *pushed_sha*, and how
    far the work that follows the push has got.

    The run's own answer to "is anything still unpushed?" — read by a second
    ``converge-push`` (which then reports ``already pushed`` without touching
    the network) and by ``develop list``, which drops its ``unpushed`` marker.
    Written by **both** pushers: ``converge``'s own approved push (*by* =
    ``converge``) and the operator's (*by* = ``converge-push``). Approval is
    not the discriminator — a ``--no-push`` run, and one whose push raced or
    failed, are approved with their rounds still only local.

    *finding_posted* / *gate_completed* record the steps AFTER the push, which
    are best-effort and can each fail on their own. They are what lets a
    re-run finish an epilogue a crash or a transport left half-done instead of
    reporting "already pushed" over work still owed. Merged, never reset: a
    later call that knows nothing about them leaves them alone.

    The remote is still the authority; this is the offline fast path, exactly
    as ``delivery.json`` is for a hand delivery.
    """
    record = converge_push_record(run_dir)
    record.update(
        {
            "pushed_sha": pushed_sha,
            "pr_url": pr_url,
            "by": by,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    if finding_posted is not None:
        record["finding_posted"] = finding_posted
    if gate_completed is not None:
        record["gate_completed"] = gate_completed
    write_state(run_dir, {CONVERGE_PUSH_KEY: record})


def record_converge_push_intent(run_dir: Path, *, tip: str, pr_url: str) -> None:
    """Record that ``develop converge-push`` is ABOUT to push *tip*.

    Written before the push, because the push is the one step whose effect
    outlives this process: killed between ``git push`` returning and
    :func:`record_converge_push`, the run would otherwise carry no trace that
    its rounds are on the PR — and the next invocation, seeing the remote
    already at the tip with nothing recorded, would report "already pushed"
    over an audit that was never written. The intent plus a remote that holds
    the tip is what says the push landed.

    Never sets ``pushed_sha``: a push that is then refused must not leave a
    record claiming it happened (``develop list`` reads that key).
    """
    record = converge_push_record(run_dir)
    record.update(
        {
            "intent_sha": tip,
            "intent_by": PUSHED_BY_CONVERGE_PUSH,
            "pr_url": pr_url,
        }
    )
    write_state(run_dir, {CONVERGE_PUSH_KEY: record})


def converge_push_record(run_dir: Path) -> dict:
    """The push record as written, or ``{}`` — the merge base for an update."""
    state = read_state(run_dir) or {}
    block = state.get(CONVERGE_PUSH_KEY)
    return dict(block) if isinstance(block, dict) else {}


def converge_pushed_sha(run_dir: Path) -> str | None:
    """The sha this run's rounds were pushed at, by either pusher, or ``None``."""
    sha = converge_push_record(run_dir).get("pushed_sha")
    return sha if isinstance(sha, str) and sha else None


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
    daemon mode the branch push, the PR open, and the ``result.json`` write
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
    (the push/PR overhead budget — no agent phase remains) before delivery starts.
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


def delivery_budget_expired(run_dir: Path) -> bool:
    """Whether the recorded delivery deadline has already PASSED (#189).

    The "this delivery is not coming back" half of :func:`delivery_deadline`,
    shared by every caller that must tell a delivery still inside its budget
    from one that outlived it: `develop deliver`'s salvage guard, and the
    provenance it publishes for the run it salvages. ``False`` when no deadline
    was recorded — an unbounded delivery is not an expired one (callers that
    need a bound without a deadline use :func:`delivery_timed_out`'s grace).
    """
    deadline = delivery_deadline(run_dir)
    return deadline is not None and datetime.now(UTC) >= deadline


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


def record_delivery_deadline(run_dir: Path, *, budget_seconds: int) -> None:
    """Record when this run's PR delivery budget expires, for `develop attach` (#189).

    deliver() runs host-side after the agent containers stop, so attach can't use
    container liveness to tell a slow delivery from a dead one — and the budget is
    the daemon's *configurable* timeouts, which attach can't see. Writing an
    absolute deadline (now + the full delivery budget; see
    :func:`pr_delivery.delivery_budget_seconds`) lets attach bound a crashed/orphaned
    delivery without ever timing out one still inside its budget. Best-effort: a
    write failure just means attach falls back to its flat grace.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=budget_seconds)
    with contextlib.suppress(OSError):
        (run_dir / DELIVERY_MARKER).write_text(
            json.dumps({"deadline": deadline.isoformat()}) + "\n", encoding="utf-8"
        )


def record_delivery_failure(run_dir: Path, *, reason: str) -> None:
    """Mark this run's PR delivery as FAILED in its private delivery.json (#194).

    When ``deliver()`` raises (e.g. ``push_branch()`` / ``gh pr create`` fails
    before a PR exists), the run is still an approved dialogue but produced no PR.
    `develop attach` reads this PRIVATE per-run marker (not the SHARED result.json,
    which a prior run could have left behind) to report the failure at once rather
    than waiting out the #189 delivery deadline. Merges into the existing marker so
    the recorded deadline is preserved. Best-effort: a write failure just means
    attach falls back to the deadline/grace bound.
    """
    marker = run_dir / DELIVERY_MARKER
    data: dict[str, object] = {}
    try:
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            data = existing
    except (OSError, json.JSONDecodeError):
        # Best-effort merge: if the prior marker is missing/unreadable/invalid,
        # continue with a fresh payload and still record this failure.
        pass
    data["failed"] = True
    data["reason"] = reason
    with contextlib.suppress(OSError):
        marker.write_text(json.dumps(data) + "\n", encoding="utf-8")


def record_manual_delivery(
    run_dir: Path, *, pr_url: str, complete: bool = False
) -> None:
    """Record that ``develop deliver`` put this run's branch behind *pr_url*
    — and, once the delivery has FINISHED, that it did (*complete*).

    Two facts, written at two moments (PR #427 review, Medium 2): the url the
    moment the PR exists, so ``develop list`` can show it beside the run; the
    completion bit only when every step landed — the gate swap, the
    provenance, the record the operator asked for. ``prune`` reads the second
    (:func:`manual_delivery_complete`), never the first: a partial delivery's
    exit-2 text tells the operator to RE-RUN the command on this run dir, and
    a run dir that has become prunable in the meantime is the one thing that
    makes that instruction impossible to follow. Never downgraded: a later
    partial pass over a completed delivery leaves the bit alone.

    A stopped run's own delivery never ran, so nothing in the run dir would
    otherwise say the work has since been delivered: ``develop list`` would
    keep showing it beside runs still waiting for a decision, and ``prune``
    would keep a run whose branch is already a monitored PR. The story's `pr`
    gate is the authoritative record (and the only one a second host can
    read), but both of those commands are local, offline inventories of the
    work dir — so the delivery leaves its answer where they already look.
    Merges into the existing marker so a recorded deadline / failure survives.
    Best-effort: a write failure costs a column, never the delivery.
    """
    marker = run_dir / DELIVERY_MARKER
    data: dict[str, object] = {}
    try:
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            data = existing
    except (OSError, json.JSONDecodeError):
        # No marker yet, or one we cannot read: start from an empty record —
        # the write below is best-effort either way.
        pass
    data["manual_pr_url"] = pr_url
    if complete:
        data["manual_delivery_complete"] = True
    with contextlib.suppress(OSError):
        marker.write_text(json.dumps(data) + "\n", encoding="utf-8")


def manual_delivery_complete(run_dir: Path) -> bool:
    """Whether a hand delivery of this run FINISHED — the bit ``prune`` reads.

    False for no marker, a marker with only the url (a partial delivery the
    operator was told to re-run), or anything unreadable.
    """
    try:
        data = json.loads((run_dir / DELIVERY_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("manual_delivery_complete") is True


def manual_delivery_pr(run_dir: Path) -> str | None:
    """The PR a HAND delivery put this run's branch behind, or ``None``."""
    try:
        data = json.loads((run_dir / DELIVERY_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    url = data.get("manual_pr_url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


def run_pr_url(run_dir: Path) -> str | None:
    """The PR this run's branch is behind — delivered by the daemon or by hand.

    What ``develop list`` shows: the run's own delivery (#188, from its
    run-bound ``result.json``) first, since only an approved run has one, then
    the ``develop deliver`` marker. ``None`` means "no PR of this run's" — a
    run still waiting on a decision. Two file reads and no ``state.json``:
    this is a listing, called once per run dir, and the verdict adds nothing
    a delivery record does not already say.
    """
    return delivered_pr_url(run_dir, None) or manual_delivery_pr(run_dir)


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
    daemon mode the **PR delivery** (branch push, PR open, ``result.json``)
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

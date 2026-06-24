"""``lithos-loom develop`` — observe in-flight story-develop runs (#88).

A mostly read-only operator surface over the per-run state a daemon-mode
``story-develop`` run leaves on disk + its live agent containers. Four
commands:

* ``develop list`` — enumerate inspectable runs (run id, task, current round,
  which agent is active, container status, run dir).
* ``develop attach <run-id|task-id>`` — follow a live run: round + active agent,
  printing each handoff as it lands, until the run reaches a terminal state (its
  recorded outcome — ``state.json`` with a status, or, when the work dir was
  reaped on success before a poll saw it, the outcome recovered from the
  completion store), then a one-line outcome summary. ``--wait`` blocks quietly
  for the outcome (exit non-zero unless approved); ``--stream`` emits JSONL events.
* ``develop dump <run-id|task-id>`` — print the assembled conversation log so
  far.
* ``develop prune`` — delete the on-disk run-state dirs of finished runs (the
  one mutating command; ``--dry-run`` previews). Finished = no longer in flight,
  so an in-flight run is never removed out from under a live daemon.

**Discovery is zero-state.** It scans the orchestrator ``work_dir`` for
the ``<work_dir>/<task_id>/<run_id>/`` layout the route-runner + plugin produce,
and queries ``docker`` for container/agent liveness — no new index file (issue
#88 open-Q 1). Note the route-runner reaps the work dir on **success**, so this
observes **in-flight + failed/interrupted** runs (exactly the watch-a-live-run
case); a succeeded run's dir is gone.

Mid-run, ``conversation.md`` / ``state.json`` don't exist yet (the plugin writes
them only at the end), so ``dump`` assembles from the per-round ``handoff/``
files via :func:`story_develop.handoff.conversation_log`.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

import typer

from lithos_loom.config import load_config
from lithos_loom.errors import LithosLoomError
from lithos_loom.plugins.story_develop import handoff
from lithos_loom.plugins.story_develop.idempotency import lookup_completed_for_run

develop_app = typer.Typer(
    name="develop",
    help="Observe in-flight story-develop runs (read-only).",
    no_args_is_help=True,
)

# `develop review` (#154): run the panel + gate on an existing change. Registered
# here (not a read-only observability command) so it shares the `develop`
# namespace; the implementation lives in `cli/review.py`.
from lithos_loom.cli.review import review_command  # noqa: E402

develop_app.command("review")(review_command)

_FORMAT_TEXT = "text"
_FORMAT_JSON = "json"
# Active-agent label when docker is unavailable: we can't tell which (if any)
# agent is executing, but the file-based views still work.
_UNKNOWN = "—"

# Container naming owned by story_develop.containers.container_name:
# loom-develop-<run_id>-<agent>  (agent = "coder" | "review-<name>").
_CONTAINER_PREFIX = "loom-develop-"
# An agent turn is one `docker exec` of the tool CLI into the long-lived
# container; the *active* agent is the one with a live agent process (#94:
# codex as well as claude).
_AGENT_PROCESS_RE = re.compile(r"\b(?:claude|codex)\b")
# Handoff filenames (story_develop.handoff): round_NN_coder_done.md /
# round_NN_review_<name>.md.
_CODER_DONE_RE = re.compile(r"^round_(\d+)_coder_done\.md$")
_REVIEW_RE = re.compile(r"^round_(\d+)_review_(.+)\.md$")

_ATTACH_POLL_SECONDS = 2.0
# Grace after a run's agent containers vanish before we call it a crash. The
# plugin force-removes its containers (containers.stop_container) *before* it
# computes commits and writes the terminal state.json/conversation.md, so a
# normally-completing run spends a short window with no containers and no
# outcome yet. We keep polling for the outcome across that window; only if it
# never lands do we report a crash — following terminal *state*, not liveness.
_TEARDOWN_GRACE_SECONDS = 30.0
_TEARDOWN_GRACE_POLLS = max(1, int(_TEARDOWN_GRACE_SECONDS / _ATTACH_POLL_SECONDS))

# Bound the post-approval delivery window (#189). deliver() — branch push, PR
# open, result.json write — runs host-side in the plugin subprocess AFTER the
# agent containers stop, so "containers gone" is the NORMAL state during
# delivery, not a crash signal. If the plugin crashes after writing the approved
# state.json but before result.json lands, attach would otherwise stay in
# "delivering" forever. We bound it on wall-clock instead: generous enough for a
# slow push / Copilot round, but finite, so a crash-during-delivery terminates
# with a clear outcome rather than hanging --wait forever.
_DELIVERY_GRACE_SECONDS = 300.0
_DELIVERY_GRACE_POLLS = max(1, int(_DELIVERY_GRACE_SECONDS / _ATTACH_POLL_SECONDS))

# Handoff files are bind-mounted RW into agent containers (containers.py), so an
# agent can write arbitrary bytes — both the body and (via the reviewer-name
# segment) the filename. Treat them as adversarial: cap the read so one poisoned
# multi-GB file can't OOM this observability process, and strip terminal control
# bytes before echoing so a crafted handoff can't forge/hide output on the
# operator's terminal. The JSON `--stream` path is escape-safe via json.dumps.
_MAX_HANDOFF_BYTES = 1 << 20  # 1 MiB — handoffs are short markdown
# Cap how many *new* handoffs we materialise in a single poll. A genuine round
# adds a handful (1 coder + a few reviewers); an agent could otherwise drop
# thousands of matching filenames into the RW mount, and reading them all at
# once (count × ≤1 MiB) would balloon this process even with the per-file cap.
# Overflow surfaces over subsequent polls (unprocessed files aren't marked seen).
_MAX_HANDOFFS_PER_POLL = 64
# C0 controls except TAB/LF, plus DEL and the C1 range (covers ESC 0x1b).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


# ── run-dir model (pure; unit-tested) ──────────────────────────────────


@dataclass(frozen=True)
class RunInfo:
    """A story-develop run discovered on disk."""

    run_id: str
    task_id: str
    title: str
    round: int  # highest round with any handoff (0 = no handoff yet)
    reviewers: tuple[str, ...]
    run_dir: str


def _is_run_dir(path: Path) -> bool:
    """A run dir is recognised by its seeded ``handoff/`` subdir."""
    return path.is_dir() and (path / "handoff").is_dir()


def _latest_mtime(run_dir: Path) -> float:
    """Newest mtime under *run_dir* — its last on-disk activity (``0.0`` if none).

    The bare ``run_dir`` mtime is stale for a live run: handoff files land in
    ``run_dir/handoff/``, and on POSIX writing a child bumps the *handoff* dir's
    mtime, not its parent's. So a run whose only change is a fresh round handoff
    would otherwise report its seed time. We take the max over the run dir, its
    handoff dir + handoff files, and any terminal ``conversation.md`` — the round
    activity ``develop list`` actually observes.
    """
    candidates = [run_dir, run_dir / "handoff", run_dir / "conversation.md"]
    with contextlib.suppress(OSError):
        candidates.extend((run_dir / "handoff").iterdir())
    latest = 0.0
    for path in candidates:
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    return latest


def _iter_run_dirs(work_dir: Path) -> list[Path]:
    """All ``<work_dir>/<task_id>/<run_id>/`` run dirs, newest first."""
    if not work_dir.is_dir():
        return []
    runs = [
        run_dir
        for task_dir in work_dir.iterdir()
        if task_dir.is_dir()
        for run_dir in task_dir.iterdir()
        if _is_run_dir(run_dir)
    ]
    # newest first by last on-disk activity (handoff writes included), so the
    # ordering matches the `updated` column `develop list` renders.
    return sorted(runs, key=_latest_mtime, reverse=True)


def _task_title(run_dir: Path) -> str:
    """Title for *this* run (best-effort).

    Prefers the **per-run** ``task.json`` the plugin snapshots into the run dir
    at run start — immune to a later re-dispatch overwriting the shared
    per-task ``task.json`` (#88). Falls back to the per-task sibling for runs
    that predate the snapshot.
    """
    for candidate in (run_dir / "task.json", run_dir.parent / "task.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task = data.get("task", data) if isinstance(data, dict) else {}
        if isinstance(task, dict) and task.get("title"):
            return str(task["title"])
    return ""


def _round_and_reviewers(handoff_dir: Path) -> tuple[int, tuple[str, ...]]:
    """Highest round with any handoff + the reviewer names seen, from filenames."""
    max_round = 0
    reviewers: list[str] = []
    try:
        names = sorted(p.name for p in handoff_dir.iterdir())
    except OSError:
        return 0, ()
    for name in names:
        m = _CODER_DONE_RE.match(name)
        if m:
            max_round = max(max_round, int(m.group(1)))
            continue
        m = _REVIEW_RE.match(name)
        if m:
            max_round = max(max_round, int(m.group(1)))
            if m.group(2) not in reviewers:
                reviewers.append(m.group(2))
    return max_round, tuple(reviewers)


def _run_info(run_dir: Path) -> RunInfo:
    round_no, reviewers = _round_and_reviewers(run_dir / "handoff")
    return RunInfo(
        run_id=run_dir.name,
        task_id=run_dir.parent.name,
        title=_task_title(run_dir),
        round=round_no,
        reviewers=reviewers,
        run_dir=str(run_dir),
    )


def _resolve(work_dir: Path, key: str) -> Path | None:
    """Resolve *key* (a run_id or task_id) to a run dir, newest run if a task."""
    # run_id: <work_dir>/<any task>/<key>
    matches = [
        run_dir
        for task_dir in (work_dir.iterdir() if work_dir.is_dir() else [])
        if task_dir.is_dir()
        for run_dir in [task_dir / key]
        if _is_run_dir(run_dir)
    ]
    if matches:
        return max(matches, key=lambda p: p.stat().st_mtime)
    # task_id: <work_dir>/<key>/<newest run>
    task_dir = work_dir / key
    if task_dir.is_dir():
        runs = [r for r in task_dir.iterdir() if _is_run_dir(r)]
        if runs:
            return max(runs, key=lambda p: p.stat().st_mtime)
    return None


# ── docker layer (thin seam; monkeypatched in tests) ───────────────────


@dataclass(frozen=True)
class ContainerStatus:
    name: str
    agent: str  # "coder" | "review-<name>"
    status: str  # docker's status string, e.g. "Up 3 minutes"
    running: bool


def _docker(args: list[str]) -> str | None:
    """Run a read-only ``docker`` command; ``None`` when docker is unavailable."""
    try:
        proc = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _run_containers(run_id: str) -> list[ContainerStatus] | None:
    """Agent containers for *run_id* (running + exited).

    Returns ``None`` when **docker is unavailable** — distinct from an empty
    list (docker works, but the run has no containers: finished / reaped /
    not-yet-started). Callers must keep the two apart: ``None`` means "can't
    tell" (active agent → ``—``, file views still work), ``[]`` means "done".
    """
    out = _docker(["ps", "-a", "--format", "{{.Names}}\t{{.Status}}"])
    if out is None:
        return None
    prefix = f"{_CONTAINER_PREFIX}{run_id}-"
    result: list[ContainerStatus] = []
    for line in out.splitlines():
        name, _, status = line.partition("\t")
        if not name.startswith(prefix):
            continue
        result.append(
            ContainerStatus(
                name=name,
                agent=name[len(f"{_CONTAINER_PREFIX}{run_id}-") :],
                status=status.strip(),
                running=status.startswith("Up"),
            )
        )
    return result


def _active_agent(containers: list[ContainerStatus]) -> str | None:
    """The agent currently executing a turn (live claude/codex process), or None."""
    for c in containers:
        if not c.running:
            continue
        top = _docker(["top", c.name])
        if top is not None and _AGENT_PROCESS_RE.search(top):
            return c.agent
    return None


# ── output helpers ─────────────────────────────────────────────────────


def _format_mtime(mtime: float) -> str:
    """Local wall-clock timestamp of a run's last on-disk activity.

    ``0.0`` (an unstat-able run dir) renders as ``—`` rather than the 1970 epoch.
    An out-of-range value also renders as ``—``: handoff files are bind-mounted
    RW into agent containers, so a misbehaving/compromised agent can poison a
    handoff mtime (e.g. ``os.utime(..., (9e18, 9e18))``); without this guard
    ``time.localtime`` would raise and abort the whole text listing — denying the
    operator a view of *every* run, not just the poisoned one.
    """
    if not mtime:
        return _UNKNOWN
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    except (OverflowError, OSError, ValueError):
        return _UNKNOWN


def _agent_state(info: RunInfo) -> str:
    """Human label for what the run is doing now."""
    containers = _run_containers(info.run_id)
    if containers is None:
        return _UNKNOWN  # docker unavailable — can't tell (file views still work)
    if not containers:
        return "done"  # docker works, no containers: finished/reaped
    active = _active_agent(containers)
    if active:
        return active
    return "idle" if any(c.running for c in containers) else "done"


def _run_phase(
    run_dir: Path,
    containers: list[ContainerStatus] | None,
    state: dict | None,
    *,
    seen_container: bool,
) -> str:
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
      delivery has completed (:func:`_delivery_complete`), or the run dir was
      **reaped** (the route-runner removes it after applying the result, so its
      absence is itself an end signal; the outcome is then recovered from the
      completion store — see :func:`_recover_reaped_outcome`).
    - ``"delivering"`` — the dialogue **approved** but post-approval PR delivery
      is still in flight (``result.json`` not yet written, dir not yet reaped).
      Keep following; the caller renders a distinct "delivering PR…" phase.
    - ``"running"`` — an agent container is up, or we're still in the **startup
      window** (no container seen yet), or docker is absent and the run dir is
      still present (can't observe containers — keep following for the outcome).
    - ``"vanished"`` — docker shows the agent containers, having been **seen**,
      are now all gone but no outcome is recorded yet. Ambiguous: either the
      normal teardown window before ``state.json`` is written, or a hard crash.
      The caller grace-polls before deciding (see ``_TEARDOWN_GRACE_POLLS``).

    *state* is the already-read ``state.json`` for this poll (passed in so the
    caller can capture the exact dict it classified on, without a second read
    that could race the work-dir reap).
    """
    if state is not None and state.get("status"):
        # An approved verdict still has PR delivery to do in daemon mode — not
        # terminal until this run's result.json lands (or it's reaped on success,
        # handled below). Every other terminal status has no post-dialogue work.
        if state.get("status") == "approved" and not _delivery_complete(run_dir):
            return "delivering"
        return "terminal"
    if not run_dir.is_dir():
        return "terminal"  # reaped by the route-runner after applying the result
    if containers is None:
        return "running"
    if any(c.running for c in containers):
        return "running"
    return "vanished" if seen_container else "running"


def _delivery_complete(run_dir: Path) -> bool:
    """Whether an approved run's post-dialogue PR delivery has finished.

    ``develop()`` writes ``state.json`` the moment the dialogue approves, but in
    daemon mode the branch push, the Copilot round, and the ``result.json`` write
    all happen AFTER it returns (``story_develop/__main__`` calls ``deliver()``
    then ``write_result_atomically``). ``result.json`` — the plugin's final
    contract output — is therefore the "fully delivered" signal. It lives in the
    SHARED per-task dir (``run_dir.parent``); a ``"succeeded"`` status binds it to
    THIS run, since a succeeded run's whole work dir is reaped — so any
    ``result.json`` that survives there from a prior *retained* run is necessarily
    a non-success and must not be mistaken for this run's delivery.
    """
    try:
        data = json.loads((run_dir.parent / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("status") == "succeeded"


def _wait_for_run(work_dir: Path, key: str) -> Path:
    """Block (polling) until a run for *key* appears under *work_dir*.

    For ``attach --wait`` invoked right after dispatching a task, before the
    route-runner has created the run dir. ``Ctrl-C`` exits cleanly, like the
    follow loop.
    """
    while True:
        run_dir = _resolve(work_dir, key)
        if run_dir is not None:
            return run_dir
        time.sleep(_ATTACH_POLL_SECONDS)


def _is_finished(run_dir: Path) -> bool:
    """Whether a run is terminal — i.e. safe for ``prune`` to remove.

    The signal is the on-disk terminal marker: the plugin writes
    ``conversation.md`` only after the agent containers stop, so its presence
    means the run reached teardown. Container state alone is *not* sufficient —
    agent containers run with ``--rm`` (``containers.py``), so a finished run and
    a run still in its **startup window** (handoff dir seeded, containers not yet
    started) both report zero containers. Pruning on "no running container" would
    delete a live run out from under the daemon during that window. Requiring the
    durable marker leaves every in-flight run (and any hard-crashed run that
    never wrote the marker) untouched, erring conservative. A still-live agent
    container is treated as a definitive override in case a future change writes
    the marker earlier.
    """
    if not (run_dir / "conversation.md").is_file():
        return False
    containers = _run_containers(run_dir.name)
    return not (containers and any(c.running for c in containers))


def _reap_empty_task_dir(task_dir: Path) -> None:
    """Remove a per-task staging dir once it holds no run subdirs (best-effort).

    After pruning a task's last retained run the only thing left under
    ``<work_dir>/<task_id>/`` is the shared ``task.json``; dropping the whole
    dir keeps ``work_dir`` as clean as the route-runner leaves it on success.

    We gate on *any* remaining child directory, not just one matching
    :func:`_is_run_dir`: a brand-new dispatch creates ``<work_dir>/<task>/<run>/``
    before ``develop()`` seeds its ``handoff/`` subdir, so an in-flight startup
    run is a directory that doesn't yet look like a run dir. Treating any
    subdirectory as a live run keeps that window safe — only a task dir down to
    plain files (the stale ``task.json``) is reaped.
    """
    try:
        if any(child.is_dir() for child in task_dir.iterdir()):
            return
    except OSError:
        return
    with contextlib.suppress(OSError):
        shutil.rmtree(task_dir)


# Human phrasing for a finished run's terminal status (story_develop writes
# these into state.json — see develop.py). Kept terse + greppable; an unknown
# status falls through to its raw value.
_OUTCOME_PHRASES = {
    "approved": "approved",
    "max_rounds": "NOT approved (max rounds reached)",
    "failed": "failed",
    "interrupted": "interrupted (re-run to retry)",
    "stalled": "stopped (stalled)",
    "disputed": "stopped (dispute needs human arbitration)",
    "cost_exceeded": "stopped (cost ceiling reached)",
}


def _read_state(run_dir: Path) -> dict | None:
    """The run's terminal ``state.json`` (status + rounds + branch), or ``None``.

    Written by the plugin only at run end, alongside ``conversation.md``.
    """
    try:
        data = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class _Outcome:
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


def _recover_reaped_outcome(run_dir: Path) -> dict | None:
    """Recover a **reaped** run's outcome from the host-persistent completion store.

    The route-runner removes the whole work dir on a succeeded result, taking
    ``state.json`` with it — and a follow can miss the brief window where the
    file exists (a poll lands before it is written, then the dir is gone by the
    next poll). The plugin records that success in the idempotency store *before*
    the dir is reaped, a source the route-runner never touches. The record is
    keyed by the (possibly explicit ``--idempotency-key``) key, so it is located
    by this run's id — bound to **this** run, not a prior success of the same
    task. A match means the run was approved (the only success).
    """
    if lookup_completed_for_run(run_dir.parent.name, run_dir.name):
        return {"status": "approved"}
    return None


def _capture_outcome(outcome: _Outcome, run_dir: Path, state: dict | None) -> None:
    """Snapshot the terminal outcome into *outcome* from the already-read *state*.

    Done before the follow loop returns, while the result is still recoverable.
    When the run dir has been reaped (success cleanup) with no ``state.json``
    captured, the outcome is recovered from the completion store
    (:func:`_recover_reaped_outcome`).
    """
    outcome.reaped = not run_dir.is_dir()
    if state is None and outcome.reaped:
        state = _recover_reaped_outcome(run_dir)
    outcome.state = state
    outcome.has_log = (run_dir / "conversation.md").is_file()


def _approved(outcome: _Outcome) -> bool:
    """Whether the run reached the only success status (``approved``) **and**
    fully delivered.

    A delivery that never completed (:attr:`_Outcome.delivery_timed_out`, #189) is
    not a clean success — the PR may not exist — so ``attach --wait`` must exit
    nonzero, or ``attach --wait && gh pr view`` would race a PR that never opened.
    """
    if outcome.delivery_timed_out:
        return False
    return bool(outcome.state and outcome.state.get("status") == "approved")


def _outcome_line(run_id: str, outcome: _Outcome) -> str:
    """One-line outcome summary for a run that has reached a terminal state.

    Prefers the recorded (or recovered) ``state.json`` status; then the bare
    terminal marker (``conversation.md`` present but no status); then a reaped
    run whose success could not be recovered; failing all, a crash.
    """
    state = outcome.state
    if outcome.delivery_timed_out:
        # approved, but result.json never landed within the grace window — the
        # run likely crashed mid-delivery (#189). Distinct from a clean approval.
        parts = [f"── run {run_id} approved but PR delivery did not complete"]
        rounds = state.get("rounds") if state else None
        if isinstance(rounds, int):
            parts.append(f"after {rounds} round{'s' if rounds != 1 else ''}")
        if state and state.get("branch"):
            parts.append(f"on {state['branch']}")
        parts.append("(timed out waiting for result.json — check `develop dump`)")
        return " ".join(parts)
    if state and state.get("status"):
        status = str(state["status"])
        phrase = _OUTCOME_PHRASES.get(status, status)
        parts = [f"── run {run_id} {phrase}"]
        rounds = state.get("rounds")
        if isinstance(rounds, int):
            parts.append(f"after {rounds} round{'s' if rounds != 1 else ''}")
        if state.get("branch"):
            parts.append(f"on {state['branch']}")
        return " ".join(parts)
    if outcome.has_log:
        return f"── run {run_id} finished (status not recorded)"
    if outcome.reaped:
        return f"── run {run_id} finished (work dir reaped; outcome not recovered)"
    return f"── run {run_id} ended without recording an outcome (crashed?)"


def _outcome_event(run_id: str, outcome: _Outcome) -> dict:
    """The ``--stream`` terminal event mirroring :func:`_outcome_line`."""
    state = outcome.state
    event: dict = {"event": "outcome", "run_id": run_id, "status": None}
    if outcome.delivery_timed_out:
        # approved verdict, but delivery never completed (#189) — flag it so a
        # consumer doesn't read the bare "approved" status as a delivered PR.
        event["status"] = "approved"
        event["delivery_timed_out"] = True
        if state and isinstance(state.get("rounds"), int):
            event["rounds"] = state["rounds"]
        if state and state.get("branch"):
            event["branch"] = str(state["branch"])
        return event
    if state and state.get("status"):
        event["status"] = str(state["status"])
        if isinstance(state.get("rounds"), int):
            event["rounds"] = state["rounds"]
        if state.get("branch"):
            event["branch"] = str(state["branch"])
    elif outcome.reaped:
        event["reaped"] = True
    elif not outcome.has_log:
        event["crashed"] = True
    return event


def _fail(msg: str, code: int = 1) -> NoReturn:
    typer.echo(f"lithos-loom: {msg}", err=True)
    sys.exit(code)


# ── commands ───────────────────────────────────────────────────────────


@develop_app.command("list")
def develop_list(
    config: Path | None = typer.Option(  # noqa: B008 (Typer DI)
        None, "--config", "-c", help="Explicit TOML config path."
    ),
    output_format: str = typer.Option(  # noqa: B008
        _FORMAT_TEXT, "--format", "-f", help="Output format: 'text' or 'json'."
    ),
) -> None:
    """List inspectable story-develop runs (in-flight + failed/interrupted).

    Succeeded runs are reaped by the route-runner, so they won't appear.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        _fail(str(exc))
    work_dir = cfg.orchestrator.work_dir
    infos = [_run_info(d) for d in _iter_run_dirs(work_dir)]

    if output_format == _FORMAT_JSON:
        typer.echo(
            json.dumps(
                [
                    {
                        **asdict(i),
                        "active": _agent_state(i),
                        "mtime": _latest_mtime(Path(i.run_dir)),
                    }
                    for i in infos
                ]
            )
        )
        return
    if output_format != _FORMAT_TEXT:
        _fail(
            f"unknown --format {output_format!r} "
            f"(expected {_FORMAT_TEXT}/{_FORMAT_JSON})",
            code=2,
        )
    if not infos:
        typer.echo(
            f"no story-develop runs under {work_dir} "
            "(succeeded runs are reaped; only in-flight / failed runs persist)"
        )
        return
    rows = [
        (
            i.run_id,
            i.task_id,
            (i.title[:40] + "…") if len(i.title) > 41 else i.title,
            f"r{i.round}",
            _agent_state(i),
            _format_mtime(_latest_mtime(Path(i.run_dir))),
        )
        for i in infos
    ]
    headers = ("run", "task", "title", "round", "active", "updated")
    widths = [
        max(len(h), max((len(r[c]) for r in rows), default=0))
        for c, h in enumerate(headers)
    ]
    typer.echo("  ".join(h.ljust(widths[c]) for c, h in enumerate(headers)))
    for row in rows:
        typer.echo("  ".join(str(v).ljust(widths[c]) for c, v in enumerate(row)))


@develop_app.command("prune")
def develop_prune(
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", "-c", help="Explicit TOML config path."
    ),
    dry_run: bool = typer.Option(  # noqa: B008
        False, "--dry-run", "-n", help="List what would be removed; delete nothing."
    ),
    output_format: str = typer.Option(  # noqa: B008
        _FORMAT_TEXT, "--format", "-f", help="Output format: 'text' or 'json'."
    ),
) -> None:
    """Remove the on-disk run-state dirs of **finished** story-develop runs.

    Succeeded runs are reaped by the route-runner; this clears the failed /
    interrupted dirs that accumulate. A run is *finished* once it has written its
    terminal ``conversation.md`` (after its agent containers stop); an in-flight
    run — including one still in its startup window — is left untouched.
    ``--dry-run`` previews without deleting. A deletion that fails (permissions,
    busy filesystem) is reported as an error, never as a success, and makes the
    command exit non-zero so automation can tell a clean sweep from a partial one.
    """
    if output_format not in (_FORMAT_TEXT, _FORMAT_JSON):
        _fail(
            f"unknown --format {output_format!r} "
            f"(expected {_FORMAT_TEXT}/{_FORMAT_JSON})",
            code=2,
        )
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        _fail(str(exc))
    work_dir = cfg.orchestrator.work_dir
    finished = [d for d in _iter_run_dirs(work_dir) if _is_finished(d)]

    # (info, removed, error) — `removed` is the *actual* outcome, not an
    # assumption: a swallowed rmtree failure that still claimed success would
    # leave callers acting on a dir that is still on disk (f-002).
    results: list[tuple[RunInfo, bool, str | None]] = []
    for run_dir in finished:
        info = _run_info(run_dir)
        if dry_run:
            results.append((info, False, None))
            continue
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            results.append((info, False, str(exc)))
            continue
        _reap_empty_task_dir(run_dir.parent)
        results.append((info, True, None))

    failed = any(err is not None for _, _, err in results)

    if output_format == _FORMAT_JSON:
        typer.echo(
            json.dumps(
                [
                    {**asdict(i), "pruned": removed}
                    | ({"error": err} if err is not None else {})
                    for i, removed, err in results
                ]
            )
        )
        if failed:
            sys.exit(1)
        return
    if not results:
        typer.echo(f"no finished story-develop runs to prune under {work_dir}")
        return
    verb = "would remove" if dry_run else "removed"
    done = 0
    for info, _removed, err in results:
        if err is not None:
            typer.echo(
                f"lithos-loom: failed to remove {info.run_id} "
                f"(task {info.task_id}): {err}",
                err=True,
            )
            continue
        done += 1
        typer.echo(f"{verb} {info.run_id} (task {info.task_id})  {info.run_dir}")
    typer.echo(f"{verb} {done} finished run{'s' if done != 1 else ''}")
    if failed:
        sys.exit(1)


@develop_app.command("dump")
def develop_dump(
    key: str = typer.Argument(..., help="run id or task id"),  # noqa: B008
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", "-c", help="Explicit TOML config path."
    ),
) -> None:
    """Print the assembled conversation log for a run (finished or in-flight)."""
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        _fail(str(exc))
    run_dir = _resolve(cfg.orchestrator.work_dir, key)
    if run_dir is None:
        _fail(f"no run found for {key!r} under {cfg.orchestrator.work_dir}")

    finished = run_dir / "conversation.md"
    if finished.is_file():
        typer.echo(finished.read_text(encoding="utf-8"))
        return
    # In-flight: assemble from the per-round handoffs (conversation.md is
    # written only at run end).
    round_no, reviewers = _round_and_reviewers(run_dir / "handoff")
    if round_no == 0:
        typer.echo(f"(run {run_dir.name} has no handoffs yet)")
        return
    typer.echo(handoff.conversation_log(run_dir / "handoff", round_no, reviewers))


@develop_app.command("attach")
def develop_attach(
    key: str = typer.Argument(..., help="run id or task id"),  # noqa: B008
    config: Path | None = typer.Option(  # noqa: B008
        None, "--config", "-c", help="Explicit TOML config path."
    ),
    once: bool = typer.Option(  # noqa: B008
        False, "--once", help="Print one snapshot and exit (no follow)."
    ),
    wait: bool = typer.Option(  # noqa: B008
        False,
        "--wait",
        help="Block silently until the run reaches a terminal state, then print "
        "only the outcome (exit non-zero unless approved).",
    ),
    stream: bool = typer.Option(  # noqa: B008
        False,
        "--stream",
        help="Emit newline-delimited JSON events (state / handoff / outcome) for "
        "machine consumption.",
    ),
) -> None:
    """Follow a live run until it reaches a **terminal state**, printing handoffs
    as they land plus the current round + active agent, then a one-line outcome
    summary. Following keys on terminal state, not agent liveness, so it spans
    both the startup window before the first container and the commit / test-gate
    / teardown after the last agent turn, grace-polling through the window where
    the plugin has stopped its containers but not yet written the outcome. An
    **approved** verdict is not yet the end in daemon mode — PR delivery (push +
    Copilot round + ``result.json``) runs after the dialogue approves, shown as a
    distinct "delivering PR…" phase — so attach follows through it instead of
    exiting early. If the work dir is reaped on success before a poll observes the
    result, the outcome is recovered from the plugin's completion store.
    Read-only; ``Ctrl-C`` exits cleanly. When docker is unavailable it still
    follows the handoff files (active agent shows as ``—``).

    ``--once`` prints a single snapshot and exits. ``--wait`` blocks quietly —
    first until the run appears (so it can be used immediately after dispatch),
    then through to the terminal outcome — and prints only that outcome (exit
    non-zero unless approved). ``--stream`` emits JSONL events. The three are
    mutually exclusive.
    """
    chosen = [
        flag
        for flag, on in (("--once", once), ("--wait", wait), ("--stream", stream))
        if on
    ]
    if len(chosen) > 1:
        _fail(f"pass at most one of {' / '.join(chosen)}", code=2)
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        _fail(str(exc))
    run_dir = _resolve(cfg.orchestrator.work_dir, key)
    if run_dir is None:
        if wait:
            # --wait may be used right after dispatch, before the route-runner
            # has seeded the run dir — block until it appears rather than failing.
            run_dir = _wait_for_run(cfg.orchestrator.work_dir, key)
        else:
            _fail(f"no run found for {key!r} under {cfg.orchestrator.work_dir}")
    info = _run_info(run_dir)

    if once:
        typer.echo(_attach_header(info))
        _print_snapshot(run_dir)
        return

    outcome = _Outcome()

    if stream:
        for event in _follow_events(run_dir, info, outcome):
            typer.echo(json.dumps(event))
        typer.echo(json.dumps(_outcome_event(info.run_id, outcome)))
        return

    if wait:
        for _event in _follow_events(run_dir, info, outcome):
            pass  # quiet — drain the follow, surface only the outcome
        typer.echo(_outcome_line(info.run_id, outcome))
        if not _approved(outcome):
            sys.exit(1)
        return

    typer.echo(_attach_header(info))
    for event in _follow_events(run_dir, info, outcome):
        if event["event"] == "state":
            typer.echo(event["label"])
        else:  # handoff — sanitize agent-written name/body before the terminal
            typer.echo(f"\n── {_sanitize(event['name'])}\n{_sanitize(event['body'])}")
    typer.echo(_outcome_line(info.run_id, outcome))
    typer.echo(f"── `lithos-loom develop dump {key}` for the full log")


def _attach_header(info: RunInfo) -> str:
    return (
        f"── attached to run {info.run_id} (task {info.task_id}"
        f"{f': {info.title}' if info.title else ''})"
    )


def _follow_state(
    run_dir: Path, containers: list[ContainerStatus] | None
) -> tuple[str, int, str | None]:
    """Human label, round, and active agent for the current poll while running."""
    round_no = _round_and_reviewers(run_dir / "handoff")[0]
    if containers is None:
        return "── (docker unavailable — following handoffs only)", round_no, None
    active = _active_agent(containers)
    if active is not None:
        return f"── round {round_no}: {active} working…", round_no, active
    if any(c.running for c in containers):
        return "── (between turns: commit / test gate / next prompt…)", round_no, None
    return "── (starting up — waiting for agent containers…)", round_no, None


def _follow_events(run_dir: Path, info: RunInfo, outcome: _Outcome) -> Iterator[dict]:
    """Yield follow events until the run reaches a terminal state.

    Each event is a dict tagged by ``event``: ``state`` (label / round / agent,
    re-emitted only when the label changes) or ``handoff`` (name / body, once
    per file). The loop exits on terminal *state* — see :func:`_run_phase` — not
    agent liveness, so it survives the startup window and the post-agent
    teardown. When the agent containers vanish before the outcome is recorded
    (the normal window where the plugin has stopped its containers but not yet
    written ``state.json``) it grace-polls rather than declaring a crash. The
    final handoffs are surfaced on the last poll before exit.

    On exit it populates *outcome* from the ``state.json`` it classified on, so
    the caller renders the summary from that snapshot — re-reading ``run_dir``
    after the loop would race the route-runner reaping it on success
    (correctness/f-003). ``state.json`` is read once per poll and reused for both
    the classification and the capture, so the captured dict is exactly the one
    that triggered the terminal decision.
    """
    seen: set[str] = set()
    seen_container = False
    last_label: str | None = None
    grace = _TEARDOWN_GRACE_POLLS
    delivery_grace = _DELIVERY_GRACE_POLLS
    while True:
        containers = _run_containers(info.run_id)
        if containers:
            seen_container = True
        state = _read_state(run_dir)
        phase = _run_phase(run_dir, containers, state, seen_container=seen_container)
        if phase != "vanished":
            grace = _TEARDOWN_GRACE_POLLS  # only count down once truly ending
        if phase != "delivering":
            delivery_grace = _DELIVERY_GRACE_POLLS  # reset unless still delivering
        if phase == "running":
            label, round_no, agent = _follow_state(run_dir, containers)
            if label != last_label:  # re-announce only on a state change
                yield {
                    "event": "state",
                    "label": label,
                    "round": round_no,
                    "agent": agent,
                }
                last_label = label
        elif phase == "delivering":
            # approved, but post-approval PR delivery is still in flight — surface
            # it as a distinct phase rather than letting the window read as done.
            label = "── approved — delivering PR…"
            if label != last_label:
                yield {
                    "event": "state",
                    "label": label,
                    "round": _round_and_reviewers(run_dir / "handoff")[0],
                    "agent": None,
                }
                last_label = label
        for name, body in _iter_new_handoffs(run_dir / "handoff", seen):
            seen.add(name)
            yield {"event": "handoff", "name": name, "body": body}
        if phase == "terminal":
            _capture_outcome(outcome, run_dir, state)
            return
        if phase == "vanished":
            grace -= 1
            if grace <= 0:  # outcome never landed across the grace window → crash
                _capture_outcome(outcome, run_dir, state)
                return
        if phase == "delivering":
            delivery_grace -= 1
            if delivery_grace <= 0:  # result.json never landed → bound the hang
                _capture_outcome(outcome, run_dir, state)
                outcome.delivery_timed_out = True
                return
        time.sleep(_ATTACH_POLL_SECONDS)


def _print_snapshot(run_dir: Path) -> None:
    info = _run_info(run_dir)
    typer.echo(f"round: r{info.round}   active: {_agent_state(info)}")
    if info.reviewers:
        typer.echo(f"reviewers: {', '.join(info.reviewers)}")
    typer.echo(f"run_dir: {info.run_dir}")
    _print_new_handoffs(run_dir / "handoff", set())


def _sanitize(text: str) -> str:
    """Strip terminal control/escape bytes (keeping TAB/LF) from agent-written
    text before echoing it to the operator's terminal.

    Handoff bodies and filenames are agent-writable (RW bind mount), so a crafted
    handoff could otherwise inject ANSI escapes to forge a fake outcome line,
    clear the screen, or set the window title. Plain text is unaffected.
    """
    return _CONTROL_CHARS_RE.sub("", text)


def _read_handoff(path: Path) -> str:
    """Read a handoff body, bounded to :data:`_MAX_HANDOFF_BYTES`.

    The file is agent-writable, so a slurp (``read_text``) of a poisoned multi-GB
    file would OOM this read-only process. We read at most the cap (+1 to detect
    overflow) and decode leniently — adversarial bytes must not raise either.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_HANDOFF_BYTES + 1)
    except OSError:
        return ""
    truncated = len(raw) > _MAX_HANDOFF_BYTES
    text = raw[:_MAX_HANDOFF_BYTES].decode("utf-8", errors="replace").strip()
    return f"{text}\n…(handoff truncated)" if truncated else text


def _iter_new_handoffs(handoff_dir: Path, seen: set[str]) -> list[tuple[str, str]]:
    """New ``(name, body)`` handoff pairs not in *seen*, sorted by filename.

    Bounded to :data:`_MAX_HANDOFFS_PER_POLL` per call so a flood of
    agent-written handoff files can't be slurped all at once (security/f-003);
    a final notice pair reports any overflow, which surfaces on later polls.
    """
    try:
        names = sorted(
            p.name
            for p in handoff_dir.iterdir()
            if _CODER_DONE_RE.match(p.name) or _REVIEW_RE.match(p.name)
        )
    except OSError:
        return []
    new_names = [name for name in names if name not in seen]
    capped = new_names[:_MAX_HANDOFFS_PER_POLL]
    out = [(name, _read_handoff(handoff_dir / name)) for name in capped]
    overflow = len(new_names) - len(capped)
    if overflow:
        out.append((f"(+{overflow} more handoffs this poll — output capped)", ""))
    return out


def _print_new_handoffs(handoff_dir: Path, seen: set[str]) -> set[str]:
    """Echo handoff files not yet shown; return the updated seen-set."""
    updated = set(seen)
    for name, body in _iter_new_handoffs(handoff_dir, seen):
        updated.add(name)
        typer.echo(f"\n── {_sanitize(name)}\n{_sanitize(body)}")
    return updated

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
* ``develop prune`` — delete the on-disk run-state dirs of finished runs
  (``--dry-run`` previews, naming the reason + size per candidate). Finished is
  read from **liveness**, not from one file: the run wrote its terminal
  ``state.json`` / ``conversation.md``, or **both**: nothing is alive for it —
  no running agent container, and the owner process it stamped into its run dir
  (``story_develop.run_owner``) provably gone — *and* nothing anywhere under it
  written for longer than one agent turn, the grace window a just-stopped run
  gets. An in-flight run is never removed out from under a live daemon, and a
  dir the rule cannot classify — docker unavailable, or a tree whose age it
  could not establish — is kept and named ``unknown``.

The mutating commands registered in this namespace live in their own modules:
``review`` / ``converge`` / ``merge-gate`` / ``deliver`` (the last turns a
stopped run's branch into a delivered, gated PR — see :mod:`cli.deliver`).

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

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

import typer

from lithos_loom.cli._deliver_lithos import DELIVERY_MARKER_KEY
from lithos_loom.config import load_config
from lithos_loom.errors import LithosClientError, LithosLoomError
from lithos_loom.gates import GATE_TYPE_PR, STORY_GATE_ID_KEY
from lithos_loom.lithos_client import LithosClient
from lithos_loom.plugins.story_develop import engines, handoff, run_outcome, run_owner
from lithos_loom.plugins.story_develop.config import DEFAULT_CODER_TIMEOUT
from lithos_loom.plugins.story_develop.idempotency import lookup_completed
from lithos_loom.plugins.story_develop.publish_text import CONTROL_CHARS_RE
from lithos_loom.plugins.story_develop.run_outcome import is_run_dir, resolve_run_dir
from lithos_loom.runner.orphans import PID_LABEL, identity_alive, pid_alive
from lithos_loom.runner.signals import bind_lifetime_to_parent, install_sigterm_exit

logger = logging.getLogger(__name__)

develop_app = typer.Typer(
    name="develop",
    help="Observe in-flight story-develop runs (read-only).",
    no_args_is_help=True,
)


@develop_app.callback()
def _develop_group() -> None:
    """Runs before every ``develop`` subcommand (#407 slice 2a): the daemon
    stops a dispatched converge / merge-gate / review with SIGTERM, and the
    container teardown lives in ``finally`` blocks that a bare SIGTERM
    would skip. One install, every subcommand. And the lifetime bind: a
    run loom spawned dies with loom (PDEATHSIG), never beside a successor."""
    install_sigterm_exit()
    bind_lifetime_to_parent()


# `develop review` (#154): run the panel + gate on an existing change. Registered
# here (not a read-only observability command) so it shares the `develop`
# namespace; the implementation lives in `cli/review.py`.
from lithos_loom.cli.review import review_command  # noqa: E402

develop_app.command("review")(review_command)

# `develop converge` (converge / ADR 0003 §9 Shape 1): loop panel + gate + coder
# fixes on an existing PR until review-green, then push. Like `review`, it is a
# mutating action registered in the `develop` namespace; impl in `cli/converge.py`.
from lithos_loom.cli.converge import converge_command  # noqa: E402

develop_app.command("converge")(converge_command)

# `develop merge-gate` (PRD S3): trial-merge a PR's current base in a throwaway
# worktree, name the conflicting paths or run the current check-set on the
# merge result, and push the update when green and behind. Zero-token; the
# github-watcher sweep drives it as a subprocess. Impl in `cli/merge_gate.py`.
from lithos_loom.cli.merge_gate import merge_gate_command  # noqa: E402

develop_app.command("merge-gate")(merge_gate_command)

# `develop deliver`: push a stopped run's branch, open (or adopt) its PR, and
# swap the needs-human gate the stop raised for a `pr` gate — the operator's
# third choice on a stopped run, beside re-dispatch and abandon. Impl in
# `cli/deliver.py`.
from lithos_loom.cli.deliver import deliver_command  # noqa: E402

develop_app.command("deliver")(deliver_command)

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
# codex as well as claude). The alternation is registry-derived (ARCH-2.E5) so a
# new Engine is picked up by live-process detection without editing this regex.
_AGENT_PROCESS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in engines.supported_tools()) + r")\b"
)
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
# A line on a screen the operator DECIDES on must not be able to render
# differently from the text it carries — so the same class every other
# publication boundary strips, from its ONE definition:
# `plugins.story_develop.publish_text.CONTROL_CHARS_RE` (C0 controls except
# TAB/LF, DEL and the C1 range that covers ESC 0x1b, and
# `Default_Ignorable_Code_Point` in full). It was a third copy of that literal
# until security/f-002; the copies had drifted from it, which is the whole
# reason there is now one (`_deliver_facts.sanitize_for_terminal` and
# `handoff.sanitize_agent_text` import the same name).


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
    # The PR this run's branch is behind — its own delivery (#188) or a hand
    # one (`develop deliver`); "" while the run has none. A stopped run whose
    # branch has since been delivered is a different thing from one still
    # waiting on its gate, and only this tells them apart.
    pr: str = ""


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
        if is_run_dir(run_dir)
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
        pr=run_outcome.run_pr_url(run_dir) or "",
    )


# ── docker layer (thin seam; monkeypatched in tests) ───────────────────


@dataclass(frozen=True)
class ContainerStatus:
    name: str
    agent: str  # "coder" | "review-<name>"
    status: str  # docker's status string, e.g. "Up 3 minutes"
    running: bool
    owner_label: str = ""
    """The RAW ``loom.pid`` label docker reports (``containers.build_run_command``
    stamps the owner process on every run container), unparsed on purpose:
    ``prune`` must tell "no owner was recorded" (an empty label — a container
    from before loom labelled them) from "an owner I cannot check" (malformed,
    non-positive, or too long to parse), and parsing to ``int | None`` is
    exactly what loses that distinction."""


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
    out = _docker(
        [
            "ps",
            "-a",
            "--format",
            f'{{{{.Names}}}}\t{{{{.Status}}}}\t{{{{.Label "{PID_LABEL}"}}}}',
        ]
    )
    if out is None:
        return None
    prefix = f"{_CONTAINER_PREFIX}{run_id}-"
    result: list[ContainerStatus] = []
    for line in out.splitlines():
        name, _, rest = line.partition("\t")
        status, _, owner = rest.partition("\t")
        if not name.startswith(prefix):
            continue
        result.append(
            ContainerStatus(
                name=name,
                agent=name[len(f"{_CONTAINER_PREFIX}{run_id}-") :],
                status=status.strip(),
                running=status.startswith("Up"),
                owner_label=owner.strip(),
            )
        )
    return result


def _label_pid(label: str) -> int | None:
    """The ``loom.pid`` label as a pid the kernel can be asked about.

    ``None`` means *unusable*, never "no owner": a docker label value is an
    arbitrary string, so anything non-numeric, non-positive (``0`` is not an
    owner — ``os.kill(0, 0)`` probes our own process group and would report
    "alive" forever) or too long for ``int()`` to parse (it raises past
    Python's conversion limit, as ``orphans`` found the hard way) cannot be
    checked. The caller keeps the dir rather than guessing — an *empty* label
    is the separate, genuinely-absent case, handled there.
    """
    if not label.isdigit():
        return None
    try:
        pid = int(label)
    except ValueError:
        return None
    return pid if pid > 0 else None


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


def _wait_for_run(work_dir: Path, key: str) -> tuple[Path | None, dict | None]:
    """Block (polling) until a run for *key* appears, or it already completed.

    Returns ``(run_dir, None)`` once the run dir appears (the common case: ``attach
    --wait`` used right after dispatch, before the route-runner seeds the dir), or
    ``(None, recovered_state)`` when the run **completed without an observable run
    dir** — an idempotency replay (``__main__`` writes result.json and exits
    *before* creating the run dir) or a fast success the route-runner reaped
    between two polls. Without the second exit this loops forever (#196): the dir
    never appears. The completion store is the durable signal the route-runner
    never removes; it is keyed by the idempotency key (the task id by default), so
    ``attach --wait <task-id>`` finds it. ``Ctrl-C`` exits cleanly, like the follow
    loop.
    """
    while True:
        run_dir = resolve_run_dir(work_dir, key)
        if run_dir is not None:
            return run_dir, None
        record = lookup_completed(key, expected_task_id=key)
        if record:
            return None, run_outcome.state_from_completion_record(record)
        time.sleep(_ATTACH_POLL_SECONDS)


# ── the authoritative delivery record: the story's own `pr` gate ───────


@dataclass(frozen=True)
class GateDelivery:
    """A story's delivered PR, as its own state records it.

    *run_id* is which run's branch that PR carries — the story's
    ``manual_delivery`` marker, when it names this very PR. ``""`` when the
    story does not say (the `pr` gate itself records none): the delivery is
    real but unattributed, and the caller must not guess.
    """

    pr_url: str
    run_id: str = ""


def gate_delivered_prs(cfg: Any, task_ids: Sequence[str]) -> dict[str, GateDelivery]:
    """``{task_id: GateDelivery}`` from each story's OPEN ``pr`` gate
    (best-effort).

    The run dir's own markers are the fast path, but they are not the record:
    a hand delivery made before the marker existed has none, and the marker
    write is deliberately best-effort (a full disk costs a column, never the
    delivery). The **gate** is what actually says a story's work is a
    monitored PR — it is the thing that withholds the story — so it is the
    fallback both `list` and `prune` fall back to, exactly as the acceptance
    asks (`pr_gate_id` → that gate's `pr_url`).

    Four guards, all in the conservative direction:

    * the gate must still be **open**. ``pr_gate_id`` is provenance and
      outlives the gate it names, so a story whose PR closed unmerged, was
      re-escalated and re-developed would otherwise have its live run read as
      delivered — and `prune` DELETES on that answer.
    * it must really be a ``pr`` gate.
    * the answer is about a **story**, and a story can have several retained
      runs. The gate names no run, so the ``manual_delivery`` marker is read
      beside it and its ``run_id`` travels — but only when the marker is about
      THIS PR (a re-delivered story's marker names the current one). What to
      do with an unattributed delivery is :func:`delivered_runs`' decision.
    * any failure — unreachable Lithos, a missing task, a transport error —
      yields nothing at all. `list` then shows what the run dir knows and
      `prune` keeps the run: neither command may depend on a server being up
      to avoid deleting live work.
    """
    orchestrator = getattr(cfg, "orchestrator", None)
    url = getattr(orchestrator, "lithos_url", "") or ""
    agent = getattr(orchestrator, "agent_id", "") or ""
    if not url or not task_ids:
        return {}

    async def _read() -> dict[str, GateDelivery]:
        found: dict[str, GateDelivery] = {}
        async with LithosClient(url, agent_id=agent) as client:
            for task_id in task_ids:
                story = await client.task_get(task_id=task_id)
                story_meta = getattr(story, "metadata", None) or {}
                gate_id = story_meta.get(STORY_GATE_ID_KEY)
                if not isinstance(gate_id, str) or not gate_id:
                    continue
                gate = await client.task_get(task_id=gate_id)
                meta = getattr(gate, "metadata", None) or {}
                if getattr(gate, "status", None) != "open":
                    continue
                if meta.get("gate_type") != GATE_TYPE_PR:
                    continue
                pr_url = meta.get("pr_url")
                if not isinstance(pr_url, str) or not pr_url:
                    continue
                found[task_id] = GateDelivery(
                    pr_url=pr_url, run_id=_marked_run(story_meta, pr_url)
                )
        return found

    try:
        return asyncio.run(_read())
    except (LithosClientError, OSError, ExceptionGroup) as exc:
        # `LithosClient.__aenter__` surfaces a connect failure as a plain
        # OSError, or as an ExceptionGroup wrapping one inside a task group
        # (the `gates` command's rationale). ExceptionGroup, not
        # BaseExceptionGroup, so KeyboardInterrupt / SystemExit propagate.
        logger.debug("develop: could not read delivery gates from Lithos: %s", exc)
        return {}


def _marked_run(story_meta: Mapping[str, Any], pr_url: str) -> str:
    """The run the story's ``manual_delivery`` marker says is behind *pr_url*.

    ``""`` when there is no marker, when it is about a different PR, or when
    its ``run_id`` is missing — every one of which means "the story does not
    attribute this PR to a run", not "attribute it to whatever is left".
    """
    marker = story_meta.get(DELIVERY_MARKER_KEY)
    if not isinstance(marker, Mapping) or marker.get("pr_url") != pr_url:
        return ""
    run_id = marker.get("run_id")
    return run_id if isinstance(run_id, str) else ""


def delivered_runs(
    cfg: Any, runs: Sequence[tuple[str, str, str]]
) -> dict[tuple[str, str], str]:
    """``{(task_id, run_id): pr_url}`` for the runs a story's open ``pr`` gate
    can be attributed to.

    *runs* is ``(task_id, run_id, local_pr_url)`` for **every** run on disk —
    not only the ones missing a PR. A story's delivery is a fact about ONE
    run's branch, and a task can retain several runs: attributing it to all of
    them would show an older run behind a PR it never produced and — worse —
    let `prune` delete that older run because a *different* run of the task
    was delivered. Two facts decide, in order:

    * the story's ``manual_delivery`` marker, when it names a run for this PR;
    * otherwise the runs' OWN records. A sibling that already records this
      exact PR locally **is** the delivery — the gate is accounted for, and
      nothing unresolved may claim it as well. (Without this the sole
      remaining candidate is not "the only run it could be" but "the only run
      we happened to ask about": a daemon-delivered run whose `result.json`
      survived a failed reap answers for itself, and the hard-killed sibling
      beside it would inherit its PR and be pruned for it.)

    Only when neither says anything is the delivery applied to a candidate,
    and then only if exactly one unresolved run could be its subject.
    """
    needs_lookup = sorted({task for task, _, pr in runs if not pr})
    gated = gate_delivered_prs(cfg, needs_lookup)
    if not gated:
        return {}
    candidates: dict[str, list[str]] = {}
    claimed: dict[str, set[str]] = {}
    for task_id, run_id, local_pr in runs:
        if local_pr:
            claimed.setdefault(task_id, set()).add(local_pr)
        else:
            candidates.setdefault(task_id, []).append(run_id)
    attributed: dict[tuple[str, str], str] = {}
    for task_id, delivery in gated.items():
        unresolved = candidates.get(task_id, [])
        if delivery.run_id:
            if delivery.run_id in unresolved:
                attributed[(task_id, delivery.run_id)] = delivery.pr_url
        elif delivery.pr_url in claimed.get(task_id, set()):
            continue  # a sibling records this PR: the gate is already placed
        elif len(unresolved) == 1:
            attributed[(task_id, unresolved[0])] = delivery.pr_url
    return attributed


def _with_delivery(cfg: Any, infos: Sequence[RunInfo]) -> list[RunInfo]:
    """*infos* with the ``pr`` column filled in for every run the run dir
    could not answer for — one Lithos session, only for the stories that need
    it (a run whose own delivery record is on disk costs nothing)."""
    attributed = delivered_runs(cfg, [(i.task_id, i.run_id, i.pr) for i in infos])
    if not attributed:
        return list(infos)
    return [
        i if i.pr else replace(i, pr=attributed.get((i.task_id, i.run_id), ""))
        for i in infos
    ]


def _delivered_pr(
    run_dir: Path, delivered: Mapping[tuple[str, str], str] | None = None
) -> str:
    """The PR this run's branch is delivered behind — ``""`` when it is not.

    A delivery is a finished lifecycle by construction — ``develop deliver``
    refuses a run a live dispatch still claims, and the runner's readiness
    check defers a story behind an OPEN ``pr`` gate, so nothing is developing
    it — and it is what makes a run killed before its ``conversation.md``
    prunable once its work is a monitored PR, whatever the liveness rule
    would say. Read from the run dir first (the daemon's own delivery record,
    or a hand delivery that FINISHED — PR #427 review, Medium 2: the marker
    records the PR the moment it exists, for ``list``, and its completion bit
    only when the gate swap and the provenance landed; a partial delivery's
    exit-2 text tells the operator to re-run the command on this very run
    dir, so the dir must outlive the partial) and, for what the run dir
    cannot answer, from the story's own gate (*delivered*, keyed by
    ``(task_id, run_id)`` — see :func:`delivered_runs`, which attributes a
    story's delivery to the ONE run that produced it and yields nothing at
    all when Lithos cannot be reached, so neither a sibling run nor an outage
    can make this one deletable).
    """
    return (
        run_outcome.delivered_pr_url(run_dir, None)
        or (
            run_outcome.manual_delivery_pr(run_dir)
            if run_outcome.manual_delivery_complete(run_dir)
            else None
        )
        or (delivered or {}).get((run_dir.parent.name, run_dir.name))
        or ""
    )


# ── prune: finished = nothing is alive for this run ────────────────────

_FINISHED = "finished"
_IN_FLIGHT = "in flight"
_UNCLASSIFIED = "unknown"
# Two more states, internal to the liveness probes (:class:`_Liveness`): a
# **positively dead** owner is a different answer from **no signal at all**,
# and the difference decides whether the idle window is consulted.
_GONE = "gone"
_NO_SIGNAL = "no signal"

# How long a run dir must sit untouched before it counts as finished: one agent
# turn, the longest a healthy run can go without writing anything. It is the
# second half of the rule, never a substitute for the first — a live run is
# recognised by its containers or its owner marker (``story_develop.run_owner``)
# — and it doubles as the grace window a just-stopped run gets, so a crash stays
# on disk long enough to be looked at. This is the FLOOR: a run records its own
# largest agent-turn timeout with its owner marker (``--coder-timeout 7200`` is
# a legal turn that writes nothing for two hours — PR #428 round-5
# correctness), and :func:`_idle_window` takes the larger of the two; a run
# that recorded a shorter one still gets the default grace window.
_DEFAULT_IDLE_SECONDS = float(DEFAULT_CODER_TIMEOUT)

# A converge run's intake pass runs under its own run id (``<run>-intake``, see
# ``story_develop.converge``) and is review-only: it writes handoffs but never a
# conversation.md, so the terminal-log rule alone would keep its worktree
# forever. It is finished with its parent run.
_INTAKE_SUFFIX = "-intake"
# The on-demand work-dir subtrees (`develop converge` / `review` / `merge-gate`
# runs): their parent dir is not a story id, so they take no gate attribution.
_ON_DEMAND_DIRS = frozenset({"converge", "review", "merge-gate"})

# What ``prune`` recognises as a run dir — deliberately wider than
# :func:`~story_develop.run_outcome.is_run_dir` (which the observability
# commands use), because the residue this sweep exists to clear is precisely
# the runs that never got as far as a normal shape: a ``merge-gate`` run dir
# holds only ``worktree/``, and a run killed during startup may hold only the
# owner marker. A dir with NONE of these is not a run yet (a dispatch caught
# between ``mkdir`` and its first write) and is left alone — the same
# conservative treatment ``_reap_empty_task_dir`` gives it.
_RUN_DIR_MARKERS = ("handoff", "worktree", "agents", "test_gate")

# Entry budget for the walk. The number of files under a run dir is
# agent-controlled (its worktree is an RW mount with no file-count limit), and
# prune walks every discovered dir — ``--dry-run`` included — before it prints
# anything, so the walk is bounded. A truncated walk decides nothing (the dir is
# kept and named, and its size is reported as the lower bound it is), which is
# why the budget is set far above any honest tree rather than at a tight one: a
# big monorepo worktree plus a gate export per check per round runs to hundreds
# of thousands of entries, and holding THAT dir forever would be the bug this
# sweep exists to fix. It bounds the pathological case, not the large one.
_MAX_SCAN_ENTRIES = 1_000_000


@dataclass(frozen=True)
class PruneVerdict:
    """Why ``prune`` will — or won't — remove a run dir.

    ``state`` is :data:`_FINISHED` (safe to delete), :data:`_IN_FLIGHT` (a live
    run: never touched) or :data:`_UNCLASSIFIED` (the rule could not tell, so
    the dir is kept and named). ``reason`` is the operator-facing "why".
    """

    state: str
    reason: str


@dataclass(frozen=True)
class _TreeScan:
    """One walk of a run dir: how big it is, and when it was last written.

    ``newest_mtime`` is the newest mtime anywhere in the tree (``0.0`` when
    nothing could be stat-ed) — the whole tree, because a run writes into
    ``worktree/``, ``agents/``, ``test_gate/`` and ``artifacts/`` far more often
    than into the handoff dir, and reading only the latter would age a busy run
    into "idle".

    Three fields say the walk could not answer that question, each naming the
    first place it fell short: ``unreadable`` (an entry that could not be listed
    or stat-ed — a mode-000 directory, which a red check can leave behind by
    itself), ``future`` (an entry stamped after the sweep's ``now``, which
    cannot describe past activity and so is not a usable *newest*), and
    ``truncated`` (the entry budget ran out). Any of them makes
    ``newest_mtime`` a floor rather than the answer, and a floor cannot satisfy
    "older than the timeout": an unseen file may have been modified in place
    seconds before the run died without bumping any visible ancestor. So a
    scan with a gap **never licenses a deletion**, however liveness was
    settled — it is exactly the "a dir the rule cannot classify is reported as
    unknown — kept, never deleted" case.
    """

    size: int
    newest_mtime: float
    unreadable: str
    future: str
    truncated: bool


def _scan_tree(run_dir: Path, *, now: float, budget: int | None = None) -> _TreeScan:
    """Walk *run_dir* once for :class:`_TreeScan`.

    **Never follows a symlink** (``os.lstat`` / ``follow_symlinks=False``): the
    handoff dir and the worktree are bind-mounted RW into agent containers, so
    a link planted there would otherwise let an agent point the privileged walk
    at a host path — reporting that path's mtime (keeping its own dir forever)
    and its existence (a metadata oracle).

    An mtime **in the future** is neither counted as the newest nor silently
    dropped — it is *recorded*. It cannot describe past activity (so counting
    it would let `utime(2)`, which needs no privilege on a file one owns, pin
    the dir forever), and ignoring it would let the same stamp hide the real
    newest write behind an older visible one. Recorded, it simply means the age
    is not established: see :class:`_TreeScan`.

    The walk stops after *budget* entries (:data:`_MAX_SCAN_ENTRIES`) and says
    so; see :class:`_TreeScan` for what a walk with a gap may decide (nothing).
    """
    size = 0
    newest = 0.0
    unreadable = ""
    future = ""
    seen = 0
    limit = _MAX_SCAN_ENTRIES if budget is None else budget
    try:
        root = os.lstat(run_dir)
    except OSError:
        return _TreeScan(
            size=0,
            newest_mtime=0.0,
            unreadable=str(run_dir),
            future="",
            truncated=False,
        )
    if root.st_mtime <= now:
        newest = root.st_mtime
    else:
        future = str(run_dir)
    stack = [run_dir]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            unreadable = unreadable or str(current)
            continue
        for entry in entries:
            seen += 1
            if seen > limit:
                return _TreeScan(
                    size=size,
                    newest_mtime=newest,
                    unreadable=unreadable,
                    future=future,
                    truncated=True,
                )
            try:
                st = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                unreadable = unreadable or entry.path
                continue
            if st.st_mtime <= now:
                newest = max(newest, st.st_mtime)
            else:
                future = future or entry.path
            if is_dir:
                stack.append(Path(entry.path))
            else:
                size += st.st_size
    return _TreeScan(
        size=size,
        newest_mtime=newest,
        unreadable=unreadable,
        future=future,
        truncated=False,
    )


def _iter_prunable_dirs(work_dir: Path) -> list[Path]:
    """The ``<work_dir>/<task_id>/<run_id>/`` dirs ``prune`` considers.

    Wider than :func:`_iter_run_dirs` (see :data:`_RUN_DIR_MARKERS`) and
    symlink-hostile at both levels: a symlinked task or run dir is skipped
    outright, never classified and never walked. Nothing loom creates is a
    symlink, so one is either a mistake or a plant — and following it would
    hand a privileged ``rmtree`` (which refuses it, then reports a failure and
    a non-zero exit on every future sweep) and a whole-tree stat walk a target
    outside the work dir.
    """
    if not work_dir.is_dir():
        return []
    runs: list[Path] = []
    try:
        task_dirs = sorted(work_dir.iterdir())
    except OSError:
        return []
    for task_dir in task_dirs:
        if task_dir.is_symlink() or not task_dir.is_dir():
            continue
        try:
            children = sorted(task_dir.iterdir())
        except OSError:
            continue
        runs.extend(
            run_dir
            for run_dir in children
            if not run_dir.is_symlink() and _looks_like_a_run_dir(run_dir)
        )
    return runs


def _looks_like_a_run_dir(path: Path) -> bool:
    """Whether *path* is a run dir prune may classify (see :data:`_RUN_DIR_MARKERS`)."""
    if not path.is_dir():
        return False
    return (
        any((path / marker).is_dir() for marker in _RUN_DIR_MARKERS)
        or run_owner.owner_recorded(path)
        or _has_terminal_log(path)
    )


def _has_terminal_log(run_dir: Path) -> bool:
    """Whether the run wrote its epilogue.

    ``develop()`` writes ``conversation.md`` and then ``state.json`` after the
    agent containers stop, so either file means the run reached teardown. A run
    killed before the epilogue (OOM, SIGKILL, a crash) has neither — which is
    why this is only *one* of prune's finished signals: when it was the only
    one, a prune left 54 dirs / 8.9 GB behind as "in flight" forever.
    """
    return (run_dir / run_outcome.CONVERSATION_LOG).is_file() or (
        run_dir / run_outcome.STATE_FILE
    ).is_file()


@dataclass(frozen=True)
class _Liveness:
    """What the liveness probes established about a run.

    ``state`` is :data:`_IN_FLIGHT` (something is alive), :data:`_GONE`
    (positively established: the process that ran it is dead and no container
    of its is up), :data:`_UNCLASSIFIED` (a probe could not answer) or
    :data:`_NO_SIGNAL` (nothing to read — an old run dir on a host where docker
    works and reports no containers). Only :data:`_NO_SIGNAL` hands the verdict
    to the idle window.
    """

    state: str
    reason: str


def _run_liveness(run_dir: Path) -> _Liveness:
    """What still holds this run.

    In the order the signals are trusted:

    0. **docker itself.** If the daemon cannot be asked, a run's containers
       cannot be ruled out — a killed plugin leaves its ``--rm`` container (and
       the ``docker exec`` inside it) running — so the run is unclassifiable
       whatever else says. A dead owner settles the *host process*, never the
       container beside it.
    1. a **running** agent container for the run id;
    2. the run's **owner marker** (``story_develop.run_owner``): the identity of
       the process running it, stamped into the run dir before the fetch +
       worktree checkout. This is the one signal that survives ``--rm`` and the
       teardown ``docker rm -f``, and the only one that exists during the
       containerless startup phase — which is unbounded, so no idle window can
       stand in for it. With docker answering "no containers", a dead owner is
       a *definitive* :data:`_GONE`;
    3. failing a marker (a run dir predating it), the ``loom.pid`` **label** on
       a stopped container.

    Anything unverifiable — an unreadable marker, one this host could not fill
    in, an owner the kernel will not answer for, a container row whose label is
    not a checkable pid — is :data:`_UNCLASSIFIED`, so the caller keeps the dir.
    """
    containers = _run_containers(run_dir.name)
    if containers is None:
        return _Liveness(
            _UNCLASSIFIED,
            "docker is unavailable — a live agent container cannot be ruled out",
        )
    for container in containers:
        if container.running:
            return _Liveness(_IN_FLIGHT, f"agent container {container.name} is up")
    if run_owner.owner_recorded(run_dir):
        identity = run_owner.read_owner(run_dir)
        if identity is None:
            return _Liveness(
                _UNCLASSIFIED,
                "its owner marker names no process this host can verify "
                "(a corrupt marker, or a host that cannot identify its own "
                "processes)",
            )
        alive = identity_alive(identity)
        if alive:
            return _Liveness(
                _IN_FLIGHT, f"the process running it (pid {identity.pid}) is alive"
            )
        if alive is None:
            return _Liveness(
                _UNCLASSIFIED,
                f"the process recorded as running it (pid {identity.pid}) "
                "cannot be checked",
            )
        return _Liveness(
            _GONE,
            f"the process that ran it (pid {identity.pid}) is gone and no "
            "container of its is up",
        )
    for container in containers:
        if not container.owner_label:
            return _Liveness(
                _UNCLASSIFIED,
                f"container {container.name} records no owner to check",
            )
        pid = _label_pid(container.owner_label)
        if pid is None:
            return _Liveness(
                _UNCLASSIFIED,
                f"the owner label of {container.name} "
                f"({container.owner_label!r}) is not a pid that can be checked",
            )
        alive = pid_alive(pid)
        if alive:
            return _Liveness(
                _IN_FLIGHT, f"the run's owner process (pid {pid}) is alive"
            )
        if alive is None:
            return _Liveness(
                _UNCLASSIFIED,
                f"the owner pid of {container.name} ({pid}) cannot be checked",
            )
        return _Liveness(
            _GONE, f"the run's owner process (pid {pid}) is gone, no container up"
        )
    return _Liveness(_NO_SIGNAL, "")


def _idle_window(run_dir: Path, *, floor: float) -> float:
    """The idle window for *run_dir*: its own recorded largest agent-turn
    timeout when it has one, never less than *floor* (the host default, which
    is also the grace window every just-stopped run gets)."""
    recorded = run_owner.read_turn_timeout(run_dir)
    return max(floor, float(recorded)) if recorded is not None else floor


def _prune_verdict(
    run_dir: Path,
    *,
    scans: dict[Path, _TreeScan],
    idle_seconds: float,
    now: float,
    delivered: Mapping[tuple[str, str], str] | None = None,
) -> PruneVerdict:
    """Classify *run_dir* for ``prune``: finished, in flight, or unclassifiable.

    Finished is the run's epilogue (:func:`_has_terminal_log`), or **both**
    halves of the liveness rule: nothing is alive for it *and* nothing anywhere
    under it has been written for longer than one agent turn — the run's OWN
    largest turn timeout, recorded with its owner marker, with *idle_seconds*
    (the host default) as the floor (:func:`_idle_window`). The second half
    is not a proxy for the first — it is the grace window a just-stopped run
    gets, so a crash stays on disk long enough to be looked at (and long enough
    for the route-runner to finish reading its result). Neither half alone
    deletes anything, and neither may be *assumed*: a walk that could not see
    the whole tree (:class:`_TreeScan`) has not established an age, and an
    unestablished age is `unknown — kept`, whatever the liveness probes said.
    A dead owner proves nothing is being written **now**; it cannot prove that
    an unseen file was not modified in place seconds before the run died.

    That is deliberately the conservative direction: the cost of keeping a dir
    that could have gone is disk the operator can reclaim by hand (named, every
    sweep, with the path that blocks it), while the cost of the other reading is
    a dead run's artifacts deleted inside the window that exists to preserve
    them. It also means a run whose own agent left a mode-000 directory behind
    is held rather than swept — accepted, per the acceptance criteria's
    "a dir the rule cannot classify … is reported as unknown — kept, never
    deleted".
    """
    live = _run_liveness(run_dir)
    if live.state == _IN_FLIGHT:
        return PruneVerdict(_IN_FLIGHT, live.reason)
    if _has_terminal_log(run_dir):
        return PruneVerdict(_FINISHED, "terminal log written")
    if pr := _delivered_pr(run_dir, delivered):
        # A delivered branch is a finished lifecycle whatever the tree's age
        # says (:func:`_delivered_pr`); only a live container above outranks it.
        return PruneVerdict(_FINISHED, f"delivered as {pr}")
    if run_dir.name.endswith(_INTAKE_SUFFIX):
        parent = run_dir.parent / run_dir.name[: -len(_INTAKE_SUFFIX)]
        if not parent.is_symlink() and _looks_like_a_run_dir(parent):
            # The intake pass belongs to its parent run: it is finished with it
            # (and, while the parent lives, held with it — its own worktree is
            # the parent run's intake export).
            verdict = _prune_verdict(
                parent,
                scans=scans,
                idle_seconds=idle_seconds,
                now=now,
                delivered=delivered,
            )
            prefix = f"intake pass of run {parent.name}"
            if verdict.state == _FINISHED:
                return PruneVerdict(_FINISHED, f"{prefix}, finished: {verdict.reason}")
            return PruneVerdict(
                verdict.state, f"{prefix}, {verdict.state}: {verdict.reason}"
            )
    if live.state == _UNCLASSIFIED:
        return PruneVerdict(_UNCLASSIFIED, live.reason)
    settled = live.state == _GONE
    scan = scans.get(run_dir) or _scan_tree(run_dir, now=now)
    gap = _age_gap(scan)
    if gap:
        return PruneVerdict(
            _UNCLASSIFIED,
            (
                f"{live.reason}, but its age cannot be established ({gap}) — "
                "delete it by hand if you know the run is done"
                if settled
                else f"this run left no owner marker and its age cannot be "
                f"established ({gap})"
            ),
        )
    written = _format_mtime(scan.newest_mtime)
    idle_seconds = _idle_window(run_dir, floor=idle_seconds)
    if now - scan.newest_mtime <= idle_seconds:
        # Both halves of the rule must hold, so a run that stopped moments ago
        # is kept even once liveness has settled: that window is the operator's
        # (and the route-runner's) chance to look at what a crash left behind.
        return PruneVerdict(
            _IN_FLIGHT,
            (
                f"{live.reason}, but it wrote at {written} — inside the "
                f"{int(idle_seconds)}s grace window"
                if settled
                else f"no terminal log, but written at {written} — inside the "
                f"{int(idle_seconds)}s agent-turn window"
            ),
        )
    if not settled:
        return PruneVerdict(
            _FINISHED, f"no live process and nothing written since {written}"
        )
    return PruneVerdict(_FINISHED, f"{live.reason}; nothing written since {written}")


def _age_gap(scan: _TreeScan) -> str:
    """Why *scan* did not establish the tree's age — ``""`` when it did.

    Paths are rendered with ``!r``: they are agent-chosen (any byte but NUL and
    ``/`` is a legal filename) and this text lands on one line of the operator's
    terminal during the one command that deletes things, so every control byte
    — the newline the shared sanitiser deliberately keeps for multi-line bodies
    included — must arrive literal rather than as layout.
    """
    if scan.unreadable:
        return f"part of its dir could not be read: {scan.unreadable!r}"
    if scan.future:
        return f"{scan.future!r} is stamped in the future"
    if scan.truncated:
        return f"its dir holds more entries than the sweep walks ({_MAX_SCAN_ENTRIES})"
    if not scan.newest_mtime:
        return "nothing under it can be stat-ed"
    return ""


def _format_size(size: int, *, at_least: bool = False) -> str:
    """Human-readable byte count (the operator is deciding about disk space).

    ``at_least`` renders it as the lower bound it is when the walk that
    measured it stopped at its entry budget.
    """
    prefix = ">= " if at_least else ""
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            rendered = f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            return prefix + rendered
        value /= 1024
    return f"{prefix}{value:.1f} GiB"


def _reap_empty_task_dir(task_dir: Path) -> None:
    """Remove a per-task staging dir once it holds no run subdirs (best-effort).

    After pruning a task's last retained run the only thing left under
    ``<work_dir>/<task_id>/`` is the shared ``task.json``; dropping the whole
    dir keeps ``work_dir`` as clean as the route-runner leaves it on success.

    We gate on *any* remaining child directory, not just one matching
    :func:`~story_develop.run_outcome.is_run_dir`: a brand-new dispatch creates
    ``<work_dir>/<task>/<run>/`` before ``develop()`` seeds its ``handoff/``
    subdir, so an in-flight startup run is a directory that doesn't yet look
    like a run dir. Treating any subdirectory as a live run keeps that window
    safe — only a task dir down to plain files (the stale ``task.json``) is
    reaped.
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
    "needs_decision": "stopped (a product decision is needed)",
    "cost_exceeded": "stopped (cost ceiling reached)",
    "infra_failed": "stopped (infrastructure failure persisted; needs a human)",
}


def _outcome_line(run_id: str, outcome: run_outcome.RunOutcome) -> str:
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
    if outcome.delivery_failed:
        # approved, but PR delivery raised before a PR opened (#194). Not a clean
        # success — name the failure + reason (AC#3 of #171).
        parts = [f"── run {run_id} approved but PR delivery failed"]
        rounds = state.get("rounds") if state else None
        if isinstance(rounds, int):
            parts.append(f"after {rounds} round{'s' if rounds != 1 else ''}")
        if state and state.get("branch"):
            parts.append(f"on {state['branch']}")
        if outcome.failure_reason:
            parts.append(f"— {_sanitize(outcome.failure_reason)}")
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
        # #188: name the PR an approved run delivered, or why a stopped run stopped
        # — so the terminal summary answers "did it finish, and how?" (AC#3 of #171).
        if status == "approved" and outcome.pr_url:
            parts.append(f"· {outcome.pr_url}")
        elif status != "approved" and outcome.failure_reason:
            # The reason is not always loom-authored (a `failed` run's is the
            # agent's own error text, and a stop's may quote a finding), and
            # this is the line the operator reads to decide whether to look —
            # so it is stripped like any other echoed agent text. Forging an
            # outcome line from inside the outcome line is exactly what
            # `_sanitize` exists to stop (security/f-001).
            parts.append(f"— {_sanitize(outcome.failure_reason)}")
        return " ".join(parts)
    if outcome.has_log:
        return f"── run {run_id} finished (status not recorded)"
    if outcome.reaped:
        return f"── run {run_id} finished (work dir reaped; outcome not recovered)"
    return f"── run {run_id} ended without recording an outcome (crashed?)"


def _outcome_event(run_id: str, outcome: run_outcome.RunOutcome) -> dict:
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
    if outcome.delivery_failed:
        # approved verdict, but PR delivery raised before a PR opened (#194) — flag
        # it so a consumer doesn't read the bare "approved" status as a delivered PR.
        event["status"] = "approved"
        event["delivery_failed"] = True
        if state and isinstance(state.get("rounds"), int):
            event["rounds"] = state["rounds"]
        if state and state.get("branch"):
            event["branch"] = str(state["branch"])
        if outcome.failure_reason:
            event["failure_reason"] = outcome.failure_reason
        return event
    if state and state.get("status"):
        event["status"] = str(state["status"])
        if isinstance(state.get("rounds"), int):
            event["rounds"] = state["rounds"]
        if state.get("branch"):
            event["branch"] = str(state["branch"])
        if outcome.pr_url:  # #188: the delivered PR url (approved+delivered)
            event["pr_url"] = outcome.pr_url
        if outcome.failure_reason:  # #188: why a non-approved run stopped
            event["failure_reason"] = outcome.failure_reason
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

    Succeeded runs are reaped by the route-runner, so they won't appear. The
    ``pr`` column names the PR a run's branch is behind — its own delivery, or
    a hand one (``develop deliver``) — so a stopped run whose work is already
    on the maintained path is distinguishable from one still waiting.
    """
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        _fail(str(exc))
    work_dir = cfg.orchestrator.work_dir
    infos = _with_delivery(cfg, [_run_info(d) for d in _iter_run_dirs(work_dir)])

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
        tuple(
            _cell(value)
            for value in (
                i.run_id,
                i.task_id,
                (i.title[:40] + "…") if len(i.title) > 41 else i.title,
                f"r{i.round}",
                _agent_state(i),
                _format_mtime(_latest_mtime(Path(i.run_dir))),
                # the delivered PR, or `—` for a run still waiting on its gate
                # — the one column that says whether a stopped run's work is on
                # the maintained path or still only on a branch
                i.pr or _UNKNOWN,
            )
        )
        for i in infos
    ]
    headers = ("run", "task", "title", "round", "active", "updated", "pr")
    # Widths off the SHAPED cells: measuring the raw text would let an escape
    # run that renders as nothing still pad every other row (security/f-001).
    widths = [
        max(len(h), max((len(r[c]) for r in rows), default=0))
        for c, h in enumerate(headers)
    ]
    typer.echo("  ".join(h.ljust(widths[c]) for c, h in enumerate(headers)))
    for row in rows:
        typer.echo("  ".join(v.ljust(widths[c]) for c, v in enumerate(row)))


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
    interrupted / killed dirs (and the on-demand ``converge`` / ``review``
    worktrees) that accumulate. A run is *finished* when it wrote its terminal
    ``state.json`` / ``conversation.md``, **or** when both halves of the
    liveness rule hold: nothing is alive for it — no running agent container,
    and the process it stamped into its run dir at start provably gone — *and*
    nothing anywhere under it written for longer than one agent turn (the grace
    window a just-stopped run gets) — **or** once its branch is delivered as a
    PR (the daemon's own delivery, a ``develop deliver`` that finished, or the
    story's ``pr`` gate attributing the delivery to this run). A converge
    ``-intake`` pass, which never writes an epilogue, is finished with its
    parent run; a ``merge-gate``
    worktree, which has no handoff dir either, is judged by the same rule. Every
    in-flight run — including one still in its startup window — is left
    untouched, and so is any dir the rule cannot classify (docker unavailable,
    an owner it cannot check, a tree whose age it cannot establish — unreadable,
    future-stamped, or too large to walk): those are ``unknown — kept``.
    ``--dry-run`` previews without deleting, naming why each candidate counts as
    finished and how much disk it holds. A deletion that fails (permissions,
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
    # One walk per run dir, up front: it answers both "how old is it" and "how
    # much disk is it holding", and classifying EVERYTHING before deleting
    # anything is what lets an intake dir read its parent's verdict (the parent
    # may be removed earlier in the sweep). Newest first, as `list` orders.
    now = time.time()
    scans = {
        run_dir: _scan_tree(run_dir, now=now)
        for run_dir in _iter_prunable_dirs(work_dir)
    }
    # The story-gate attribution, read ONCE for every story run on disk (the
    # on-demand `converge` / `review` / `merge-gate` dirs have no story, and a
    # pseudo task id in the batch would fail the whole best-effort read).
    delivered = delivered_runs(
        cfg,
        [
            (run_dir.parent.name, run_dir.name, run_outcome.run_pr_url(run_dir) or "")
            for run_dir in scans
            if run_dir.parent.name not in _ON_DEMAND_DIRS
        ],
    )
    verdicts = [
        (
            run_dir,
            _prune_verdict(
                run_dir,
                scans=scans,
                idle_seconds=_DEFAULT_IDLE_SECONDS,
                now=now,
                delivered=delivered,
            ),
        )
        for run_dir in sorted(scans, key=lambda d: scans[d].newest_mtime, reverse=True)
    ]

    # (info, verdict, size, removed, error) — `removed` is the *actual* outcome,
    # not an assumption: a swallowed rmtree failure that still claimed success
    # would leave callers acting on a dir that is still on disk (f-002).
    results: list[tuple[RunInfo, PruneVerdict, _TreeScan, bool, str | None]] = []
    for run_dir, verdict in verdicts:
        info = _run_info(run_dir)
        scan = scans[run_dir]
        if dry_run or verdict.state != _FINISHED:
            results.append((info, verdict, scan, False, None))
            continue
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            results.append((info, verdict, scan, False, str(exc)))
            continue
        _reap_empty_task_dir(run_dir.parent)
        results.append((info, verdict, scan, True, None))

    failed = any(err is not None for *_, err in results)

    if output_format == _FORMAT_JSON:
        typer.echo(
            json.dumps(
                [
                    {
                        **asdict(i),
                        "state": v.state,
                        "reason": v.reason,
                        "size_bytes": scan.size,
                        # the walk stopped at its entry budget: `size_bytes` is
                        # a floor, not the total (the verdict never depends on
                        # it — see `_prune_verdict`).
                        "size_partial": scan.truncated,
                        "pruned": removed,
                    }
                    | ({"error": err} if err is not None else {})
                    for i, v, scan, removed, err in results
                ]
            )
        )
        if failed:
            sys.exit(1)
        return
    if not results:
        typer.echo(f"no story-develop run dirs under {work_dir}")
        return
    verb = "would remove" if dry_run else "removed"
    done = 0
    kept: list[str] = []
    for info, verdict, scan, _removed, err in results:
        if err is not None:
            typer.echo(
                f"lithos-loom: failed to remove {info.run_id} "
                f"(task {info.task_id}): {err}",
                err=True,
            )
            continue
        if verdict.state == _FINISHED:
            done += 1
            label = verb
        else:
            kept.append(verdict.state)
            label = f"{verdict.state} — kept"
        # The reason can quote a path an agent named (an unreadable entry), and
        # this is the one command that deletes things — a crafted name must not
        # be able to erase or forge a line beside it. Rendered line-safe at
        # construction too; this is the sink, and the sink is where the rule
        # holds.
        typer.echo(
            f"{label} {info.run_id} (task {info.task_id})  {info.run_dir}  "
            f"{_format_size(scan.size, at_least=scan.truncated)}  "
            f"— {_line_safe(verdict.reason)}"
        )
    if done or not kept:
        typer.echo(f"{verb} {done} finished run{'s' if done != 1 else ''}")
    else:
        typer.echo(f"no finished story-develop runs to prune under {work_dir}")
    if kept:
        typer.echo(
            f"kept {len(kept)} run{'s' if len(kept) != 1 else ''}: "
            f"{kept.count(_IN_FLIGHT)} in flight, "
            f"{kept.count(_UNCLASSIFIED)} unknown (never deleted)"
        )
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
    run_dir = resolve_run_dir(cfg.orchestrator.work_dir, key)
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
    ``result.json``) runs after the dialogue approves, shown as a
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
    run_dir = resolve_run_dir(cfg.orchestrator.work_dir, key)
    if run_dir is None:
        if not wait:
            _fail(f"no run found for {key!r} under {cfg.orchestrator.work_dir}")
        # --wait may be used right after dispatch, before the route-runner has
        # seeded the run dir — block until it appears rather than failing.
        run_dir, recovered = _wait_for_run(cfg.orchestrator.work_dir, key)
        if run_dir is None:
            # The run completed with no observable run dir (idempotency replay /
            # fast reap) — report the recovered terminal outcome instead of
            # hanging forever (#196). --wait is the only mode that waits.
            outcome = run_outcome.RunOutcome(state=recovered, reaped=True)
            if recovered and recovered.get("pr_url"):
                outcome.pr_url = str(recovered["pr_url"])
            typer.echo(_outcome_line(key, outcome))
            if not run_outcome.is_clean_success(outcome):
                sys.exit(1)
            return
    info = _run_info(run_dir)

    if once:
        typer.echo(_attach_header(info))
        _print_snapshot(run_dir)
        return

    outcome = run_outcome.RunOutcome()

    if stream:
        for event in _follow_events(run_dir, info, outcome):
            typer.echo(json.dumps(event))
        typer.echo(json.dumps(_outcome_event(info.run_id, outcome)))
        return

    if wait:
        for _event in _follow_events(run_dir, info, outcome):
            pass  # quiet — drain the follow, surface only the outcome
        typer.echo(_outcome_line(info.run_id, outcome))
        if not run_outcome.is_clean_success(outcome):
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


def _follow_events(
    run_dir: Path, info: RunInfo, outcome: run_outcome.RunOutcome
) -> Iterator[dict]:
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
    delivering_polls = 0  # polls in the current delivering episode (fallback bound)
    while True:
        containers = _run_containers(info.run_id)
        if containers:
            seen_container = True
        state = run_outcome.read_state(run_dir)
        phase = run_outcome.run_phase(
            run_dir,
            state,
            containers_running=(
                None if containers is None else any(c.running for c in containers)
            ),
            seen_container=seen_container,
        )
        if phase != "vanished":
            grace = _TEARDOWN_GRACE_POLLS  # only count down once truly ending
        if phase != "delivering":
            delivering_polls = 0  # reset the fallback counter unless still delivering
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
            run_outcome.capture_outcome(outcome, run_dir, state)
            return
        if phase == "vanished":
            grace -= 1
            if grace <= 0:  # outcome never landed across the grace window → crash
                run_outcome.capture_outcome(outcome, run_dir, state)
                return
        if phase == "delivering":
            delivering_polls += 1
            # bound the hang on the daemon's recorded delivery deadline (or a
            # generous flat fallback) — never on a delivery still inside its budget.
            if run_outcome.delivery_timed_out(
                run_dir, delivering_seconds=delivering_polls * _ATTACH_POLL_SECONDS
            ):
                run_outcome.capture_outcome(outcome, run_dir, state)
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


def _line_safe(text: str) -> str:
    """:func:`_sanitize`, plus the two control bytes it deliberately keeps.

    ``CONTROL_CHARS_RE`` preserves LF and TAB because it guards multi-line
    *bodies* (PR bodies, handoffs). ``prune``'s output is not a body: it is one
    record per line, read as a list, so a newline in an agent-chosen path would
    forge whole records — including a plausible summary tail — on the one
    command that deletes things. Here the record is the unit, so LF and TAB fold
    to spaces.
    """
    return _sanitize(text).replace("\n", " ").replace("\t", " ")


def _sanitize(text: str) -> str:
    """Strip terminal control/escape bytes (keeping TAB/LF) from agent-written
    text before echoing it to the operator's terminal.

    Handoff bodies and filenames are agent-writable (RW bind mount), so a crafted
    handoff could otherwise inject ANSI escapes to forge a fake outcome line,
    clear the screen, or set the window title. Plain text is unaffected.
    """
    return CONTROL_CHARS_RE.sub("", text)


def _cell(value: str) -> str:
    """One table cell, shaped so it can only render as the text it carries.

    `develop list` is the screen an operator decides on — since the `pr`
    column it is the "delivered vs still waiting" inventory — and the `title`
    cell is a mirrored story's **GitHub issue title**, i.e. anyone's to write.
    The whole row is emitted as ONE echo, so a bare `\r` rewrites it from
    column 0 and an embedded LF starts a forged row: :func:`_sanitize` removes
    the escape class every publication boundary strips (see the comment above
    `_MAX_HANDOFF_BYTES`), and folding the remaining whitespace — LF and TAB
    survive that class by design — keeps a cell to the single visual line the
    table lays out (security/f-001; the `--format json` path is escape-safe
    via `json.dumps`).
    """
    return " ".join(_sanitize(str(value)).split()) or ""


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

"""``lithos-loom develop`` — observe in-flight story-develop runs (#88).

A read-only operator surface over the per-run state a daemon-mode
``story-develop`` run leaves on disk + its live agent containers. Three
commands:

* ``develop list`` — enumerate inspectable runs (run id, task, current round,
  which agent is active, container status, run dir).
* ``develop attach <run-id|task-id>`` — follow a live run: round + active agent,
  printing each handoff as it lands, until the run's containers stop.
* ``develop dump <run-id|task-id>`` — print the assembled conversation log so
  far.

**Read-only and zero-state.** Discovery scans the orchestrator ``work_dir`` for
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

import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

import typer

from lithos_loom.config import load_config
from lithos_loom.errors import LithosLoomError
from lithos_loom.plugins.story_develop import handoff

develop_app = typer.Typer(
    name="develop",
    help="Observe in-flight story-develop runs (read-only).",
    no_args_is_help=True,
)

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
    # newest first; mtime is stable enough for an operator listing.
    return sorted(runs, key=lambda p: p.stat().st_mtime, reverse=True)


def _task_title(run_dir: Path) -> str:
    """Task title from the runner-written sibling ``task.json`` (best-effort)."""
    try:
        data = json.loads((run_dir.parent / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    task = data.get("task", data) if isinstance(data, dict) else {}
    return str(task.get("title") or "") if isinstance(task, dict) else ""


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


def _still_running(run_dir: Path, containers: list[ContainerStatus] | None) -> bool:
    """Whether ``attach`` should keep following.

    With docker, the run is live while any agent container runs. Without docker
    (``containers is None``), fall back to a file-based end signal so the
    handoff view still follows: the run dir is reaped on success, and
    ``conversation.md`` is written on a non-success end — either means done.
    """
    if containers is None:
        return run_dir.is_dir() and not (run_dir / "conversation.md").is_file()
    return any(c.running for c in containers)


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
            json.dumps([{**asdict(i), "active": _agent_state(i)} for i in infos])
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
        )
        for i in infos
    ]
    headers = ("run", "task", "title", "round", "active")
    widths = [
        max(len(h), max((len(r[c]) for r in rows), default=0))
        for c, h in enumerate(headers)
    ]
    typer.echo("  ".join(h.ljust(widths[c]) for c, h in enumerate(headers)))
    for row in rows:
        typer.echo("  ".join(str(v).ljust(widths[c]) for c, v in enumerate(row)))


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
) -> None:
    """Follow a live run: current round + active agent, printing handoffs as
    they land, until the run ends. Read-only. When docker is unavailable it
    still follows the handoff files (active agent shows as ``—``)."""
    try:
        cfg = load_config(config)
    except LithosLoomError as exc:
        _fail(str(exc))
    run_dir = _resolve(cfg.orchestrator.work_dir, key)
    if run_dir is None:
        _fail(f"no run found for {key!r} under {cfg.orchestrator.work_dir}")
    info = _run_info(run_dir)
    typer.echo(
        f"── attached to run {info.run_id} (task {info.task_id}"
        f"{f': {info.title}' if info.title else ''})"
    )

    if once:
        _print_snapshot(run_dir)
        return

    seen: set[str] = set()
    last_line: str | None = None
    while True:
        containers = _run_containers(info.run_id)
        running = _still_running(run_dir, containers)
        if running:
            if containers is None:
                line = "── (docker unavailable — following handoffs only)"
            elif (active := _active_agent(containers)) is not None:
                round_no = _round_and_reviewers(run_dir / "handoff")[0]
                line = f"── round {round_no}: {active} working…"
            else:
                line = "── (between turns: commit / test gate / next prompt…)"
            if line != last_line:  # re-announce only on a state change
                typer.echo(line)
                last_line = line
        # Print new handoffs every poll — including the final one before we
        # stop — so a docker-absent follow still surfaces them as they land.
        seen = _print_new_handoffs(run_dir / "handoff", seen)
        if not running:
            break
        time.sleep(_ATTACH_POLL_SECONDS)

    typer.echo(
        f"── run {info.run_id} not running — "
        f"`lithos-loom develop dump {key}` for the full log"
    )


def _print_snapshot(run_dir: Path) -> None:
    info = _run_info(run_dir)
    typer.echo(f"round: r{info.round}   active: {_agent_state(info)}")
    if info.reviewers:
        typer.echo(f"reviewers: {', '.join(info.reviewers)}")
    typer.echo(f"run_dir: {info.run_dir}")
    _print_new_handoffs(run_dir / "handoff", set())


def _print_new_handoffs(handoff_dir: Path, seen: set[str]) -> set[str]:
    """Echo handoff files not yet shown; return the updated seen-set."""
    try:
        names = sorted(
            p.name
            for p in handoff_dir.iterdir()
            if _CODER_DONE_RE.match(p.name) or _REVIEW_RE.match(p.name)
        )
    except OSError:
        return seen
    updated = set(seen)
    for name in names:
        if name in updated:
            continue
        updated.add(name)
        body = (handoff_dir / name).read_text(encoding="utf-8").strip()
        typer.echo(f"\n── {name}\n{body}")
    return updated

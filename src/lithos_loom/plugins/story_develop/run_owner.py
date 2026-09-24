"""The per-run owner marker: WHICH host process is running this run.

Part of the develop-run on-disk contract (:mod:`run_outcome` owns the rest), in
its own module because it needs :mod:`runner.orphans` and ``run_outcome`` is
pinned as a stdlib-only leaf.

The operator's sweep (``lithos-loom develop prune``) has to answer "is anything
still alive for this run dir?" before it deletes a git worktree, and **docker
cannot answer it**: agent containers run with ``--rm`` and are force-removed at
teardown, and a run has no container *at all* between creating its dirs and
finishing ``worktree.create`` — an unbounded fetch + checkout that can outlast
any idle window. A run that is merely slow there would read exactly like one
that was OOM-killed an hour ago.

So every run stamps ``owner.json`` into its run dir before it does anything
slow. The value is an *identity*, not a bare pid
(:class:`~lithos_loom.runner.orphans.ProcessIdentity`: pid + kernel start time +
host boot id): pids are reused — after a reboot quite plausibly by loom itself —
and "pid alive" alone would read a long-dead run as live forever.

Written by the run-dir producers (``develop()``, ``review_only``'s panel pass,
``external_triage``, ``merge_gate``); read by ``cli/develop.py``'s prune.
**Best-effort on the write** — a host that cannot answer for its own process
records nothing, and prune falls back to containers + idle time.
**Conservative on the read** — a marker that exists but cannot be checked keeps
the dir.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from ...runner.orphans import ProcessIdentity, process_identity

#: The marker filename, in the run dir beside ``state.json`` / ``conversation.md``.
OWNER_FILE = "owner.json"


def record_owner(run_dir: Path) -> None:
    """Stamp *run_dir* with the identity of the process running this run.

    Best-effort and idempotent (a re-stamp by the same process rewrites the
    same identity). Temp file + ``os.replace`` so a crash mid-write can never
    leave a half-written marker behind: prune keeps a dir whose marker it
    cannot parse, and a torn write would strand the dir it exists to free.
    """
    identity = process_identity(os.getpid())
    if identity is None:
        return  # this host cannot answer for its own process — record nothing
    payload = json.dumps(
        {
            "pid": identity.pid,
            "start_ticks": identity.start_ticks,
            "host_boot": identity.host_boot,
        }
    )
    tmp = run_dir / f".{OWNER_FILE}.tmp"
    with contextlib.suppress(OSError):
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, run_dir / OWNER_FILE)


def owner_recorded(run_dir: Path) -> bool:
    """Whether *run_dir* carries an owner marker at all.

    The distinction prune needs: **no marker** (a run from before this contract,
    or a host that could not stamp one) falls back to the other signals, while a
    marker that is present but unreadable is "cannot tell" — kept, never
    deleted on a guess.
    """
    return (run_dir / OWNER_FILE).is_file()


def read_owner(run_dir: Path) -> ProcessIdentity | None:
    """The recorded owner identity, or ``None`` when there is no usable one.

    ``None`` covers both "no marker" and "a marker this cannot make sense of";
    callers that must tell them apart ask :func:`owner_recorded` first. Every
    field is validated — a marker is a file on disk, and a run dir's own agents
    can write into that dir tree.
    """
    try:
        data = json.loads((run_dir / OWNER_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    pid, start, boot = data.get("pid"), data.get("start_ticks"), data.get("host_boot")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
        return None
    if not isinstance(boot, str) or not boot:
        return None
    return ProcessIdentity(pid=pid, start_ticks=start, host_boot=boot)

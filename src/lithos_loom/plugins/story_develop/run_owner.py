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

**The stamp is not best-effort.** A run dir that reaches the slow work without
one is a run prune can only guess about, so a host that cannot identify its own
process still gets a marker — one that says exactly that, and that prune reads
as "cannot tell" (keep the dir) rather than "nobody home". Only if even *that*
cannot be written does :func:`record_owner` raise, and its callers then fail
before operating in the run dir at all.

**Both ends are symlink-hostile.** The marker sits at a predictable path in a
tree this subsystem itself documents as agent-writable, so the write goes to a
random temp name opened ``O_EXCL|O_NOFOLLOW`` and is renamed into place (never
a host-privileged write through a planted link), and the read refuses anything
but a regular file and is capped — the same treatment ``cli/develop`` gives
handoff files.

Written by the run-dir producers (``develop()``, ``review_only``'s panel pass,
``external_triage``, ``merge_gate``); read by ``cli/develop.py``'s prune.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import stat
from pathlib import Path

from ...runner.orphans import ProcessIdentity, process_identity

#: The marker filename, in the run dir beside ``state.json`` / ``conversation.md``.
OWNER_FILE = "owner.json"

#: A marker is three small fields; anything larger is not one. Capped for the
#: same reason every other agent-adjacent read here is (``_MAX_HANDOFF_BYTES``).
_MAX_MARKER_BYTES = 4096


def record_owner(run_dir: Path) -> None:
    """Stamp *run_dir* with the identity of the process running this run.

    Idempotent (a re-stamp by the same process rewrites the same identity).
    Temp file + ``os.replace`` so a crash mid-write cannot leave a half-written
    marker behind — prune keeps a dir whose marker it cannot parse, and a torn
    write would strand the dir it exists to free. The temp name is random and
    opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` (the repo's ``.<name>.tmp.<rand>``
    convention): a fixed one could be pre-created as a symlink and turn this
    into a host-privileged write to wherever it points.

    **Raises** ``OSError`` when the marker cannot be written at all. The caller
    must not proceed into the run dir unstamped: everything slow happens next,
    with no container up, and prune would have nothing to read but mtimes.
    """
    identity = process_identity(os.getpid())
    payload: dict[str, object] = (
        {
            "pid": identity.pid,
            "start_ticks": identity.start_ticks,
            "host_boot": identity.host_boot,
        }
        if identity is not None
        # This host cannot answer for its own process (no procfs, no `ps`, no
        # boot id). Record that *durably* rather than nothing at all: an absent
        # marker reads as "an old run dir" and hands the verdict to the idle
        # window, which would delete this run while it is still fetching.
        else {"pid": os.getpid(), "unverifiable": True}
    )
    tmp = run_dir / f".{OWNER_FILE}.tmp.{secrets.token_hex(4)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, run_dir / OWNER_FILE)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def owner_recorded(run_dir: Path) -> bool:
    """Whether *run_dir* carries an owner marker at all.

    The distinction prune needs: **no marker** (a run dir predating this
    contract) falls back to the other signals, while a marker that is present
    but unusable is "cannot tell" — kept, never deleted on a guess. So this
    asks only whether the *path* is there (``lstat``, no symlink resolution):
    a marker replaced by a link is present-and-unusable, not absent.
    """
    try:
        os.lstat(run_dir / OWNER_FILE)
    except OSError:
        return False
    return True


def read_owner(run_dir: Path) -> ProcessIdentity | None:
    """The recorded owner identity, or ``None`` when there is no usable one.

    ``None`` covers "no marker", "a marker this cannot make sense of" and "a
    host that could not identify its own process"; callers that must tell an
    absent marker from an unusable one ask :func:`owner_recorded` first. Every
    field is validated, the path must be a **regular file** (never a link out
    of the run dir), and the read is bounded — a marker is a file in a tree
    whose own agents can write.
    """
    raw = _read_marker(run_dir)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
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


def _read_marker(run_dir: Path) -> bytes | None:
    """The marker's bytes — ``None`` unless it is a regular file of sane size."""
    try:
        fd = os.open(run_dir / OWNER_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, _MAX_MARKER_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if len(raw) > _MAX_MARKER_BYTES else raw

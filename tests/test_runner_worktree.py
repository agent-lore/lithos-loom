"""Unit tests for ``lithos_loom.runner.worktree``."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lithos_loom.runner import worktree


def _branch_of(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_create_makes_worktree_on_new_branch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    parent = tmp_path / "wts"
    wt = worktree.create(tmp_git_repo, "main", "Add a CLI flag!", parent=parent)
    assert wt.is_dir()
    assert wt.parent == parent
    # branch name is the dir name, slugged + random suffix
    assert wt.name.startswith("add-a-cli-flag-")
    assert _branch_of(wt) == wt.name
    # worktree HEAD matches the base branch tip
    repo_head = subprocess.run(
        ["git", "rev-parse", "main"], cwd=tmp_git_repo, capture_output=True, text=True
    ).stdout.strip()
    wt_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True
    ).stdout.strip()
    assert wt_head == repo_head


def test_create_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    a = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    b = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    assert a != b


def test_remove_deletes_clean_worktree(tmp_git_repo: Path, tmp_path: Path) -> None:
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    worktree.remove(wt)
    assert not wt.exists()


def test_remove_refuses_dirty_without_force(tmp_git_repo: Path, tmp_path: Path) -> None:
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    (wt / "untracked.txt").write_text("dirty")
    with pytest.raises(RuntimeError):
        worktree.remove(wt, force=False)
    worktree.remove(wt, force=True)
    assert not wt.exists()


def test_remove_rejects_non_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.remove(tmp_path / "nope")


def test_git_common_dir_is_main_repo_git(tmp_git_repo: Path, tmp_path: Path) -> None:
    # A linked worktree's common dir is the main repo's `.git` (#109).
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    common = worktree.git_common_dir(wt)
    assert common.is_absolute()
    assert common.resolve() == (tmp_git_repo / ".git").resolve()


def test_git_common_dir_rejects_non_worktree(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.git_common_dir(tmp_path / "nope")


# --- create_at: materialise a worktree AT an existing commit (#154) ----------


def _sha(path: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=path, capture_output=True, text=True
    ).stdout.strip()


def _add_commit(repo: Path, filename: str, content: str) -> str:
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", f"add {filename}"], cwd=repo, check=True)
    return _sha(repo, "HEAD")


def _clone_with_remote(tmp_path: Path, origin: Path) -> Path:
    """A local clone whose ``main`` tracks *origin* — the operator's checkout."""
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=clone, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=clone, check=True)
    return clone


def test_create_starts_at_the_fetched_remote_base_not_the_stale_local_branch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """#390 (lens #85): the operator's local `main` was one commit behind
    origin when the story dispatched; the branch cut there delivered a PR
    born behind its base. The start point is the remote base, fetched first
    — the local branch is only what the operator last pulled."""
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    stale_local = _sha(clone, "main")
    # origin moves on (a PR merges); the clone has not pulled
    moved = _add_commit(tmp_git_repo, "landed.txt", "merged upstream\n")
    assert _sha(clone, "main") == stale_local

    wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")

    assert _sha(wt, "HEAD") == moved  # the fetched origin/main, not local main
    assert _sha(clone, "main") == stale_local  # the operator's branch untouched
    assert _branch_of(wt).startswith("task-")
    # started at the resolved sha, so no upstream-tracking config was written
    # (opus round 1: `-b x origin/main` writes .git/config under a non-retrying
    # lock — concurrent cuts in one checkout failed 24/40 times)
    tracking = subprocess.run(
        ["git", "config", "--get-regexp", f"branch\\.{_branch_of(wt)}\\."],
        cwd=clone,
        capture_output=True,
        text=True,
    )
    assert tracking.returncode != 0 and tracking.stdout == ""


def test_create_fetches_by_explicit_refspec_whatever_the_remote_config_says(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """opus round 1: a bare `git fetch origin main` updates the tracking ref
    only when `remote.origin.fetch` maps it — a checkout narrowed to another
    branch would report success and cut at the stale ref, the very defect
    again. The explicit refspec updates it regardless."""
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    subprocess.run(
        [
            "git",
            "config",
            "remote.origin.fetch",
            "+refs/heads/other:refs/remotes/origin/other",
        ],
        cwd=clone,
        check=True,
    )
    moved = _add_commit(tmp_git_repo, "landed.txt", "merged upstream\n")

    wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")

    assert _sha(wt, "HEAD") == moved
    assert _sha(clone, "origin/main") == moved


def test_create_fetches_when_origin_exists_but_the_tracking_ref_does_not(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """PR #393 review (High): a checkout with an `origin` remote but no
    materialised `origin/<base>` (narrowed then widened, a pruned tracking
    ref) must still fetch — the explicit refspec CREATES the ref — not cut
    at the stale local branch, which is the defect this PR exists to fix."""
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    stale_local = _sha(clone, "main")
    subprocess.run(
        ["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=clone, check=True
    )
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "refs/remotes/origin/main"],
            cwd=clone,
            capture_output=True,
        ).returncode
        != 0
    )
    moved = _add_commit(tmp_git_repo, "landed.txt", "merged upstream\n")

    wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")

    assert _sha(wt, "HEAD") == moved
    assert _sha(clone, "origin/main") == moved  # the fetch created the ref
    assert _sha(clone, "main") == stale_local


def test_create_falls_back_to_the_local_branch_when_no_ref_and_the_fetch_fails(
    tmp_git_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # origin exists but is dead AND there is no tracking ref to fall back on:
    # the local branch is all there is (logged)
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    subprocess.run(
        ["git", "update-ref", "-d", "refs/remotes/origin/main"], cwd=clone, check=True
    )
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "gone")],
        cwd=clone,
        check=True,
    )
    with caplog.at_level("WARNING", logger="lithos_loom.runner.worktree"):
        wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == _sha(clone, "main")
    assert any("fetch" in r.message and "local" in r.message for r in caplog.records)


def test_fetch_base_retries_once_when_a_concurrent_fetch_moved_the_ref(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opus round 1: two fetches of a moved base race on the ref's CAS; the
    loser exits 1 with `cannot lock ref … is at X but expected Y` although
    both would write X. That is not a failure — retry once."""
    from lithos_loom.runner import git

    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    moved = _add_commit(tmp_git_repo, "landed.txt", "merged upstream\n")
    real_run = git.run_group
    calls: list[list[str]] = []

    def racing_run(argv, **kw):
        calls.append(list(argv))
        if len(calls) == 1:
            return 1, (
                "error: cannot lock ref 'refs/remotes/origin/main': is at "
                f"{moved} but expected {'0' * 40}\n"
            )
        return real_run(argv, **kw)

    monkeypatch.setattr(git, "run_group", racing_run)

    assert git.fetch_branch(clone, "main") == ""
    assert len(calls) == 2
    assert _sha(clone, "origin/main") == moved


def test_create_falls_back_to_the_local_branch_without_a_remote(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # the test fixtures / a repo with no origin: the local branch is the base
    wt = worktree.create(tmp_git_repo, "main", "task", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == _sha(tmp_git_repo, "main")


def test_create_falls_back_to_the_local_branch_when_the_fetch_fails(
    tmp_git_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Offline (or a dead remote) must not kill the run: the last-fetched
    remote ref is the base (never behind the operator's branch, unlike a
    local commit on main), and the fallback is logged so a stale run is
    explicable."""
    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    subprocess.run(
        ["git", "remote", "set-url", "origin", str(tmp_path / "gone")],
        cwd=clone,
        check=True,
    )
    with caplog.at_level("WARNING", logger="lithos_loom.runner.worktree"):
        wt = worktree.create(clone, "main", "task", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == _sha(clone, "origin/main")
    assert any(
        "fetch" in r.message and "last fetched" in r.message for r in caplog.records
    )


def test_fetch_branch_kills_a_hung_transport_with_its_group(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """opus round 1: killing `git fetch` alone leaves the ssh / https helper
    running; the fetch runs in its own process group and the group is killed
    on timeout, so a hung remote costs the timeout and nothing else."""
    import os
    import time

    from lithos_loom.runner import git

    clone = _clone_with_remote(tmp_path, tmp_git_repo)
    # an ssh "transport" that hangs: git runs it through GIT_SSH_COMMAND
    hang = tmp_path / "hang.sh"
    hang.write_text(
        "#!/bin/sh\necho $$ > " + str(tmp_path / "hang.pid") + "\nsleep 300\n"
    )
    hang.chmod(0o755)
    subprocess.run(
        ["git", "remote", "set-url", "origin", "ssh://localhost/nowhere.git"],
        cwd=clone,
        check=True,
    )
    started = time.monotonic()
    env_backup = os.environ.get("GIT_SSH_COMMAND")
    os.environ["GIT_SSH_COMMAND"] = str(hang)
    try:
        problem = git.fetch_branch(clone, "main", timeout=1.0)
    finally:
        if env_backup is None:
            del os.environ["GIT_SSH_COMMAND"]
        else:
            os.environ["GIT_SSH_COMMAND"] = env_backup
    assert "timed out" in problem
    assert time.monotonic() - started < 10
    helper = int((tmp_path / "hang.pid").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(helper, 0)  # the helper died with the group


def test_create_at_checks_out_detached_at_ref(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """Review-only materialises a worktree AT the change head (detached), unlike
    ``create`` which branches fresh off a base."""
    first = _sha(tmp_git_repo, "HEAD")
    _add_commit(tmp_git_repo, "feature.txt", "the change\n")

    wt = worktree.create_at(tmp_git_repo, first, "review pr 1", parent=tmp_path / "w")

    assert wt.is_dir()
    assert wt.parent == tmp_path / "w"
    # HEAD is exactly the requested commit, and it is DETACHED (no branch)
    assert _sha(wt, "HEAD") == first
    assert _branch_of(wt) == "HEAD"
    # the tree reflects that commit — the later file is absent
    assert not (wt / "feature.txt").exists()


def test_create_at_reflects_head_ref(tmp_git_repo: Path, tmp_path: Path) -> None:
    head = _add_commit(tmp_git_repo, "feature.txt", "the change\n")
    wt = worktree.create_at(tmp_git_repo, head, "review", parent=tmp_path / "w")
    assert _sha(wt, "HEAD") == head
    assert (wt / "feature.txt").read_text() == "the change\n"


def test_create_at_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    head = _sha(tmp_git_repo, "HEAD")
    a = worktree.create_at(tmp_git_repo, head, "task", parent=tmp_path / "w")
    b = worktree.create_at(tmp_git_repo, head, "task", parent=tmp_path / "w")
    assert a != b


def test_create_at_rejects_unknown_ref(tmp_git_repo: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        worktree.create_at(tmp_git_repo, "deadbeef" * 5, "task", parent=tmp_path / "w")


# --- create_on_branch: a committable branch AT an existing commit (converge) --


def test_create_on_branch_makes_committable_branch_at_commit(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """Converge materialises a committable local branch AT the PR head so the
    fixer can commit onto it and it can be pushed back — unlike ``create_at``
    (detached, no branch) and ``create`` (branched off a base)."""
    first = _sha(tmp_git_repo, "HEAD")
    _add_commit(tmp_git_repo, "later.txt", "later\n")

    wt = worktree.create_on_branch(
        tmp_git_repo, first, "converge pr 1", parent=tmp_path / "w"
    )

    assert wt.is_dir()
    # a real branch (named after the dir), NOT detached HEAD
    assert _branch_of(wt) == wt.name
    assert wt.name.startswith("converge-pr-1-")
    # positioned at the requested commit — the later file is absent
    assert _sha(wt, "HEAD") == first
    assert not (wt / "later.txt").exists()
    # and it is committable: a commit lands on the branch
    (wt / "fix.txt").write_text("fix\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "fix"], cwd=wt, check=True)
    assert _sha(wt, "HEAD") != first


def test_create_on_branch_is_unique(tmp_git_repo: Path, tmp_path: Path) -> None:
    head = _sha(tmp_git_repo, "HEAD")
    a = worktree.create_on_branch(tmp_git_repo, head, "task", parent=tmp_path / "w")
    b = worktree.create_on_branch(tmp_git_repo, head, "task", parent=tmp_path / "w")
    assert a != b


def test_create_on_branch_rejects_unknown_ref(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError):
        worktree.create_on_branch(
            tmp_git_repo, "deadbeef" * 5, "task", parent=tmp_path / "w"
        )


# ── #392: fetch_refspecs, the tolerance every fetch shares ───────────────────


def _lock_race_stderr() -> str:
    return (
        f"error: cannot lock ref 'refs/remotes/origin/main': is at {'a' * 40} but "
        f"expected {'0' * 40}\n"
    )


def test_fetch_refspecs_gives_up_after_a_second_lock_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.runner import git

    calls: list[list[str]] = []

    def always_losing(argv, **kw):
        calls.append(list(argv))
        return 1, _lock_race_stderr()

    monkeypatch.setattr(git, "run_group", always_losing)

    problem = git.fetch_refspecs(tmp_path, ("pull/7/head", "main"))

    assert "cannot lock ref" in problem
    assert len(calls) == 2
    assert calls[0][-2:] == ["pull/7/head", "main"]


def test_fetch_refspecs_reports_a_hung_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.runner import git

    monkeypatch.setattr(git, "run_group", lambda argv, **kw: (None, ""))
    assert "timed out" in git.fetch_refspecs(tmp_path, ("main",), timeout=1.0)


def test_fetch_refspecs_does_not_retry_an_ordinary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.runner import git

    calls: list[int] = []

    def failing(argv, **kw):
        calls.append(1)
        return 128, "fatal: couldn't find remote ref main"

    monkeypatch.setattr(git, "run_group", failing)

    assert "couldn't find" in git.fetch_refspecs(tmp_path, ("main",))
    assert len(calls) == 1


def test_fetch_refspecs_reports_the_error_line_not_gits_trailing_boilerplate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opus round 1 (M2): for the very failure #392 is about, git's `error:`
    is the FIRST stderr line and the last one is "remove the file manually
    to continue." — the reason must name the ref and the lock, or the
    merge-gate `crashed` record and the remediation `[Friction]` say
    nothing useful."""
    from lithos_loom.runner import git

    stderr = (
        "error: cannot lock ref 'refs/remotes/origin/main': Unable to create "
        "'/r/.git/refs/remotes/origin/main.lock': File exists.\n\n"
        "Another git process seems to be running in this repository, e.g.\n"
        "an editor opened by 'git commit'. Please make sure all processes\n"
        "are terminated then try again.\n"
        "remove the file manually to continue.\n"
    )
    monkeypatch.setattr(git, "run_group", lambda argv, **kw: (1, stderr))

    problem = git.fetch_refspecs(tmp_path, ("main",))

    assert problem.startswith("error: cannot lock ref 'refs/remotes/origin/main'")
    assert "remove the file manually" not in problem


# ── #431 review: the whole stderr beside the reported line, and no escape ─────


def test_fetch_problem_keeps_the_whole_stderr_beside_the_reported_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review f-001: a caller that CLASSIFIES the failure needs everything git
    said — the collapsed reason keeps only the first `error:`/`fatal:` line,
    which hides the transport sign on the line below."""
    from lithos_loom.runner import git

    stderr = (
        "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly\n"
        "fatal: early EOF\n"
    )
    monkeypatch.setattr(git, "run_group", lambda argv, **kw: (128, stderr))

    problem = git.fetch_problem(tmp_path, ("main",))

    assert problem.reason.startswith("error: RPC failed")
    assert "early EOF" in problem.detail  # what a classifier reads
    assert problem.fatal_line == "fatal: early EOF"  # what an operator reads
    # the thin wrapper still reports exactly what it always did
    assert git.fetch_refspecs(tmp_path, ("main",)) == problem.reason


def test_fetch_problem_reports_a_git_that_cannot_be_spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review f-002: `run_group` documents that it never raises, but spawned
    the child outside any OSError guard — a missing `git` or a deleted `cwd`
    escaped as a traceback through every caller."""
    from lithos_loom.runner import git

    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(git.subprocess, "Popen", no_git)

    problem = git.fetch_problem(tmp_path, ("main",))

    assert problem.reason.startswith("fatal: cannot run git:")
    assert problem.fatal_line == problem.reason
    # and the same for every other caller of the shared spawn
    assert "cannot run git" in git.fetch_branch(tmp_path, "main")


def test_fetch_problem_shares_one_timeout_with_its_lock_race_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-2 review f-005: both internal calls were given the caller's FULL
    timeout, so a lock-race answer at 299 s followed by a hung retry spent
    ~600 s of a caller that had budgeted 300."""
    import time

    from lithos_loom.runner import git

    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    timeouts: list[float] = []

    def racing_then_hung(argv, *, timeout, **kw):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            clock["now"] += 299
            return 1, _lock_race_stderr()
        clock["now"] += timeout
        return None, ""

    monkeypatch.setattr(git, "run_group", racing_then_hung)

    problem = git.fetch_problem(tmp_path, ("main",), timeout=300.0)

    assert timeouts == [300.0, 1.0]
    assert clock["now"] == 300.0  # the whole call, retry included
    # a hung transport is its own class, never a transport MESSAGE a caller
    # would read as a hiccup worth retrying
    assert problem.timed_out and "timed out" in problem.reason


def test_fetch_problem_marks_only_the_watchdog_kill_as_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.runner import git

    stderr = (
        "ssh: connect to host github.com port 22: Connection timed out\n"
        "fatal: Could not read from remote repository.\n"
    )
    monkeypatch.setattr(git, "run_group", lambda argv, **kw: (128, stderr))
    problem = git.fetch_problem(tmp_path, ("main",))
    # the TRANSPORT said it timed out: git answered, so this is not the watchdog
    assert not problem.timed_out and "Connection timed out" in problem.detail

"""Tests for review-only change resolution (#154).

Range + branch forms run against a real throwaway git repo (no network); the
PR-number form stubs the ``gh`` / fetch wrappers so the test stays hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.github_client import PullRequest
from lithos_loom.plugins.story_develop import review_resolve


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, filename: str) -> str:
    (repo / filename).write_text(f"{filename}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", filename], cwd=repo, check=True)
    return _sha(repo, "HEAD")


# --- range form --------------------------------------------------------------


def test_resolves_explicit_ref_range(tmp_git_repo: Path) -> None:
    base = _sha(tmp_git_repo, "HEAD")
    head = _commit(tmp_git_repo, "feature.txt")

    change = review_resolve.resolve_change(tmp_git_repo, f"{base}..{head}")

    assert change.base_sha == base
    assert change.head_sha == head
    # the typed base is the live ref for a range (S5c)
    assert change.base_ref == base
    # a bare range carries no acceptance-criteria source
    assert change.title == ""
    assert change.body == ""


# --- branch form -------------------------------------------------------------


def test_resolves_local_branch_against_merge_base(tmp_git_repo: Path) -> None:
    main_tip = _sha(tmp_git_repo, "HEAD")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_git_repo, check=True)
    head = _commit(tmp_git_repo, "feature.txt")

    change = review_resolve.resolve_change(tmp_git_repo, "feature", base_branch="main")

    # base is the merge-base of main and the branch (here: the main tip)
    assert change.base_sha == main_tip
    assert change.head_sha == head
    assert change.head_ref == "feature"
    assert change.base_ref == "main"  # the base branch is the live ref (S5c)


def test_base_override_wins_for_branch(tmp_git_repo: Path) -> None:
    first = _sha(tmp_git_repo, "HEAD")
    second = _commit(tmp_git_repo, "second.txt")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=tmp_git_repo, check=True)
    head = _commit(tmp_git_repo, "feature.txt")

    change = review_resolve.resolve_change(
        tmp_git_repo, "feature", base_branch="main", base_override=second
    )
    assert change.base_sha == second
    assert change.head_sha == head
    assert first != second  # sanity: the override is not the default merge-base
    # an operator-forced base IS the base — no live ref to move it (S5c)
    assert change.base_ref == ""


def test_unknown_ref_raises(tmp_git_repo: Path) -> None:
    with pytest.raises(RuntimeError):
        review_resolve.resolve_change(tmp_git_repo, "no-such-ref..also-missing")


# --- PR form (gh stubbed) ----------------------------------------------------


def _stub_pr(
    number: str,
    *,
    head_repo: str = "agent-lore/lithos-loom",
    merged: bool = False,
) -> PullRequest:
    return PullRequest(
        repo="agent-lore/lithos-loom",
        number=int(number),
        state="closed" if merged else "open",
        merged=merged,
        merged_at=None,
        merge_commit_sha=None,
        head_sha="h" * 40,
        base_ref="main",
        head_ref="feature",
        head_repo=head_repo,
        base_repo="agent-lore/lithos-loom",
        title="Add a thing",
        body="This PR adds a thing.\n\n## Acceptance\n- it works",
    )


@pytest.fixture
def stub_gh(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    # ``_gh_pr_view`` now returns a typed PullRequest. The base sha is still
    # derived locally via merge-base (never from the PR object's base ref — the
    # reason #207 is moot), so the stub records the merge-base call.
    calls = SimpleNamespace(fetches=[], merge_base=[])
    monkeypatch.setattr(review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n))
    monkeypatch.setattr(
        review_resolve, "_git_fetch", lambda repo, *refs: calls.fetches.append(refs)
    )

    def _fake_merge_base(repo: Path, a: str, b: str) -> str:
        calls.merge_base.append((a, b))
        return "m" * 40

    monkeypatch.setattr(review_resolve, "_merge_base", _fake_merge_base)
    return calls


def test_resolves_pr_number(stub_gh: SimpleNamespace, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(tmp_path, "#142")
    # base is the merge-base of the base branch and the PR head — the real diff
    # base GitHub shows — derived locally, NOT from the PR object's base ref
    # (which is why #207's missing baseRefOid never mattered).
    assert change.base_sha == "m" * 40
    assert stub_gh.merge_base == [("origin/main", "h" * 40)]
    # the remote-tracking base branch is the live ref converge merges (S5c)
    assert change.base_ref == "origin/main"
    assert change.head_sha == "h" * 40
    # the PR body is the default acceptance-criteria source
    assert "adds a thing" in change.body
    assert change.title == "Add a thing"
    assert "142" in change.head_ref
    # the raw pushable branch + fork flag drive converge's push epilogue
    assert change.head_branch == "feature"
    assert change.is_fork is False
    assert change.is_merged is False
    # the PR head was fetched so the commit is local
    assert fetches_for(stub_gh.fetches, "142")


def test_resolve_pr_flags_merged_but_still_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A merged PR resolves normally and is FLAGGED, not refused.

    Reviewing an already-merged PR is a legitimate read-only operation, so the
    refusal belongs in converge (which pushes fixes that could never land), not
    here. Resolution just reports the fact.
    """
    monkeypatch.setattr(
        review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n, merged=True)
    )
    monkeypatch.setattr(review_resolve, "_git_fetch", lambda repo, *refs: None)
    monkeypatch.setattr(review_resolve, "_merge_base", lambda repo, a, b: "m" * 40)

    change = review_resolve.resolve_change(tmp_path, "#142")

    assert change.is_merged is True
    assert change.head_sha == "h" * 40  # still fully resolved


def test_resolve_pr_flags_fork_when_head_repo_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A PR whose head lives on a fork (head repo != base repo) is flagged so
    converge can refuse to push to it under origin credentials."""
    monkeypatch.setattr(
        review_resolve,
        "_gh_pr_view",
        lambda repo, n: _stub_pr(n, head_repo="contributor/lithos-loom"),
    )
    monkeypatch.setattr(review_resolve, "_git_fetch", lambda repo, *refs: None)
    monkeypatch.setattr(review_resolve, "_merge_base", lambda repo, a, b: "m" * 40)

    change = review_resolve.resolve_change(tmp_path, "#142")

    assert change.is_fork is True
    assert change.head_branch == "feature"


def test_resolves_bare_digits_as_pr(stub_gh: SimpleNamespace, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(tmp_path, "142")
    assert change.head_sha == "h" * 40


def test_resolves_pr_url(stub_gh: SimpleNamespace, tmp_path: Path) -> None:
    change = review_resolve.resolve_change(
        tmp_path, "https://github.com/agent-lore/lithos-loom/pull/142"
    )
    assert change.head_sha == "h" * 40


def fetches_for(fetches: list, number: str) -> bool:
    return any(any(number in r for r in refs) for refs in fetches)


def test_gh_pr_view_resolves_local_owner_then_fetches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # gh pr view always resolved the PR against the LOCAL checkout; the typed
    # path preserves that — resolve owner/repo from the tree, fetch that repo's
    # PR by number (the number, not any URL, selects the PR).
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        review_resolve, "repo_name_with_owner", lambda repo: "agent-lore/lithos-loom"
    )

    def fake_call(op: object) -> PullRequest:
        # Run the op against a stub client to capture (repo, number).
        class _Stub:
            async def get_pull_request(self, repo: str, number: int) -> PullRequest:
                seen["repo"], seen["number"] = repo, number
                return _stub_pr(str(number))

        import asyncio

        return asyncio.run(op(_Stub()))  # type: ignore[arg-type]

    monkeypatch.setattr(review_resolve, "github_call", fake_call)
    pr = review_resolve._gh_pr_view(tmp_path, "142")
    assert (seen["repo"], seen["number"]) == ("agent-lore/lithos-loom", 142)
    assert pr.head_sha == "h" * 40


def test_gh_pr_view_missing_pr_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(review_resolve, "repo_name_with_owner", lambda repo: "o/r")
    monkeypatch.setattr(review_resolve, "github_call", lambda op: None)
    with pytest.raises(RuntimeError, match="PR #999 not found in o/r"):
        review_resolve._gh_pr_view(tmp_path, "999")


def test_fork_pr_is_refused_before_any_fetch_when_forks_are_not_allowed(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #360 review F5: merge-gate must never fetch a third-party head into
    # the operator's checkout — the fork verdict comes from the PR metadata
    # GitHub already returned, before `pull/N/head` is fetched.
    monkeypatch.setattr(
        review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n, head_repo="x/fork")
    )
    change = review_resolve.resolve_change(tmp_path, "#142", allow_fork=False)
    assert change.is_fork is True
    assert change.head_sha == "h" * 40 and change.head_branch == "feature"
    assert change.base_sha == ""  # never derived: nothing was fetched
    assert stub_gh.fetches == [] and stub_gh.merge_base == []


def test_allow_fork_default_still_fetches_a_fork(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n, head_repo="x/fork")
    )
    change = review_resolve.resolve_change(tmp_path, "#142")
    assert change.is_fork is True
    # opus round 1 (M3): the base is fetched by EXPLICIT refspec (#390's
    # lesson — a narrowed remote.origin.fetch would otherwise leave
    # origin/<base> stale at exit 0); the PR head is read via its sha
    assert stub_gh.fetches == [
        ("pull/142/head", "+refs/heads/main:refs/remotes/origin/main")
    ]


def test_resolved_pr_carries_its_open_or_closed_state(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert review_resolve.resolve_change(tmp_path, "#142").is_closed is False
    monkeypatch.setattr(
        review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n, merged=True)
    )
    change = review_resolve.resolve_change(tmp_path, "#142")
    assert change.is_merged is True and change.is_closed is True


# ── PR #362 review F2: an autonomous run is pinned to the gate's repo ────────


def test_expect_repo_mismatch_is_refused_before_any_github_call_or_fetch(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A PR number resolves against the CHECKOUT's origin; a stale
    # [projects.<slug>].repo would trial-merge and push owner/wrong#42 for a
    # gate on owner/right#42. The pin fails closed before anything is fetched.
    monkeypatch.setattr(review_resolve, "repo_name_with_owner", lambda repo: "o/wrong")
    called: list[object] = []
    monkeypatch.setattr(
        review_resolve, "github_call", lambda op: called.append(op) or _stub_pr("142")
    )
    with pytest.raises(review_resolve.RepoMismatchError) as exc:
        review_resolve.resolve_change(tmp_path, "#142", expect_repo="o/right")
    assert exc.value.expected == "o/right" and exc.value.actual == "o/wrong"
    assert "o/right" in str(exc.value) and "o/wrong" in str(exc.value)
    assert called == [] and stub_gh.fetches == []  # no GitHub call, no fetch


def test_expect_repo_match_is_case_insensitive_and_resolves(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        review_resolve, "repo_name_with_owner", lambda repo: "Agent-Lore/Lithos-Loom"
    )
    change = review_resolve.resolve_change(
        tmp_path, "#142", expect_repo="agent-lore/lithos-loom"
    )
    assert change.head_sha == "h" * 40


def test_no_expect_repo_never_asks_the_checkout(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an operator's own `converge #142` keeps today's behaviour (no extra gh call)
    def boom(repo):
        raise AssertionError("repo_name_with_owner must not be called")

    monkeypatch.setattr(review_resolve, "repo_name_with_owner", boom)
    change = review_resolve.resolve_change(tmp_path, "#142")
    assert change.head_sha == "h" * 40


def test_expect_repo_checks_a_pr_urls_own_repo_too(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # self-review: only the checkout's origin was compared; a URL naming
    # another repository resolved its NUMBER in the checkout's repo.
    monkeypatch.setattr(review_resolve, "repo_name_with_owner", lambda repo: "o/right")
    with pytest.raises(review_resolve.RepoMismatchError) as exc:
        review_resolve.resolve_change(
            tmp_path, "https://github.com/o/other/pull/142", expect_repo="o/right"
        )
    assert exc.value.actual == "o/other"
    assert stub_gh.fetches == []
    # the matching URL still resolves
    change = review_resolve.resolve_change(
        tmp_path, "https://github.com/O/Right/pull/142", expect_repo="o/right"
    )
    assert change.head_sha == "h" * 40


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/other/pull/142/files",
        "http://github.com/o/other/pull/142",
        "https://github.com/o/other/pull/142#issuecomment-1",
        "https://www.github.com/o/other/pull/142",
    ],
)
def test_expect_repo_checks_every_url_shape_the_number_parser_accepts(
    stub_gh: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    # self-review: the number parser is lenient (trailing path, http, a
    # fragment, www.) while the canonical ref parser is strict — a shape
    # the first accepts and the second rejects skipped the URL-repo check.
    monkeypatch.setattr(review_resolve, "repo_name_with_owner", lambda repo: "o/right")
    with pytest.raises(review_resolve.RepoMismatchError) as exc:
        review_resolve.resolve_change(tmp_path, url, expect_repo="o/right")
    assert exc.value.actual == "o/other"
    assert stub_gh.fetches == []


# ── #392: the PR-head / base fetch tolerates a lost ref-lock race ────────────


def _lock_race(moved: str = "a" * 40) -> str:
    return (
        f"error: cannot lock ref 'refs/remotes/origin/main': is at {moved} but "
        f"expected {'0' * 40}\n"
    )


def _stub_pr_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    # the GitHub read and the merge-base are stubbed; the FETCH is real code
    # over a scripted `git.run_group` (the public seam every fetch shares)
    monkeypatch.setattr(review_resolve, "_gh_pr_view", lambda repo, n: _stub_pr(n))
    monkeypatch.setattr(review_resolve, "_merge_base", lambda repo, a, b: "m" * 40)


def test_resolving_a_pr_retries_the_fetch_once_on_a_lost_ref_lock_race(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#392: two fetches of a MOVED base in one repo race on the tracking
    ref's compare-and-swap (the sweep's merge-gate probe beside a
    story-develop worktree cut, #390); the loser exits 1 with `cannot lock
    ref … is at X but expected Y` although both write X. `_run_git` treated
    that as fatal — a `crashed` merge-gate key, a spent remediation round.
    The fetch now goes through `git.fetch_refspecs`, which retries once."""
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    calls: list[list[str]] = []

    def racing(argv, **kw):
        calls.append(list(argv))
        return (1, _lock_race()) if len(calls) == 1 else (0, "")

    monkeypatch.setattr(git, "run_group", racing)

    change = review_resolve.resolve_change(tmp_path, "#142")

    assert change.head_sha == "h" * 40
    assert len(calls) == 2
    assert calls[0][:2] == ["git", "fetch"] and "origin" in calls[0]
    assert calls[0][-2:] == [
        "pull/142/head",
        "+refs/heads/main:refs/remotes/origin/main",
    ]


def test_resolving_a_pr_still_raises_on_any_other_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # the callers' contract is unchanged: a real failure is fatal, unretried
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    calls: list[int] = []

    def failing(argv, **kw):
        calls.append(1)
        return 128, "fatal: couldn't find remote ref pull/142/head"

    monkeypatch.setattr(git, "run_group", failing)

    with pytest.raises(RuntimeError, match="couldn't find remote ref"):
        review_resolve.resolve_change(tmp_path, "#142")
    assert len(calls) == 1


# ── #431: a transient intake fetch failure is retried, then `infra_failed` ────


def _ssh_hiccup() -> str:
    return (
        "kex_exchange_identification: read: Connection reset by peer\n"
        "fatal: Could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )


def test_a_transient_intake_fetch_failure_is_retried_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#431: the daemon's SSH agent blinked for a second and the whole
    conflict-resolve run was recorded as `crashed`, re-armed only by the next
    daemon boot. A transport failure is now retried."""
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    monkeypatch.setattr(review_resolve, "FETCH_RETRY_BACKOFF_SECONDS", 0.0)
    calls: list[list[str]] = []

    def hiccup(argv, **kw):
        calls.append(list(argv))
        return (128, _ssh_hiccup()) if len(calls) == 1 else (0, "")

    monkeypatch.setattr(git, "run_group", hiccup)

    with caplog.at_level("WARNING"):
        change = review_resolve.resolve_change(tmp_path, "#142")

    assert change.head_sha == "h" * 40  # the intake proceeded
    assert len(calls) == 2  # one retry, not a daemon restart
    retries = [r for r in caplog.records if "failed transiently" in r.message]
    assert len(retries) == 1


def test_a_persistent_intake_fetch_failure_is_a_typed_infra_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # #431: bounded — three attempts, then the #377 verdict (NOT a crash) with
    # git's own `fatal:` line and where to look for it on the host.
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    monkeypatch.setattr(review_resolve, "FETCH_RETRY_BACKOFF_SECONDS", 0.0)
    calls: list[int] = []

    def failing(argv, **kw):
        calls.append(1)
        return 128, _ssh_hiccup()

    monkeypatch.setattr(git, "run_group", failing)

    with pytest.raises(review_resolve.FetchFailedError) as exc:
        review_resolve.resolve_change(tmp_path, "#142")

    assert len(calls) == review_resolve.FETCH_ATTEMPTS == 3
    assert exc.value.problem == "fatal: Could not read from remote repository."
    action = exc.value.host_action
    assert "Could not read from remote repository" in action
    assert "SSH agent" in action and "pull/142/head" in action


def test_an_unretryable_intake_fetch_failure_is_still_typed_and_unretried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # a ref that does not exist is not a hiccup: answered first time, but
    # still `infra_failed` material rather than an uncaught RuntimeError
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    calls: list[int] = []

    def missing(argv, **kw):
        calls.append(1)
        return 128, "fatal: couldn't find remote ref pull/142/head\n"

    monkeypatch.setattr(git, "run_group", missing)

    with pytest.raises(review_resolve.FetchFailedError) as exc:
        review_resolve.resolve_change(tmp_path, "#142")

    assert len(calls) == 1
    assert "couldn't find remote ref" in exc.value.host_action


# ── #431 review: classify on the WHOLE stderr, report the `fatal:` line ───────


def _rpc_early_eof() -> str:
    # git's real shape for a dropped transport: its own diagnosis on the
    # `error:` line, its verdict on the `fatal:` one
    return (
        "error: RPC failed; curl 92 HTTP/2 stream 5 was not closed cleanly\n"
        "fatal: early EOF\n"
    )


def test_a_transport_sign_under_gits_error_line_is_still_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review f-001: the reported reason is git's FIRST `error:`/`fatal:` line,
    so classifying on it alone missed `fatal: early EOF` under an
    `error: RPC failed …` — one attempt instead of the required three."""
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    monkeypatch.setattr(review_resolve, "FETCH_RETRY_BACKOFF_SECONDS", 0.0)
    calls: list[int] = []

    def flaky(argv, **kw):
        calls.append(1)
        return (128, _rpc_early_eof()) if len(calls) < 3 else (0, "")

    monkeypatch.setattr(git, "run_group", flaky)

    change = review_resolve.resolve_change(tmp_path, "#142")

    assert change.head_sha == "h" * 40
    assert len(calls) == 3  # two retries, not one attempt


def test_the_host_action_names_gits_fatal_line_not_its_error_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # review f-001: the acceptance asks for the first `fatal:` line — git's own
    # verdict — not the transport plumbing line above it
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    monkeypatch.setattr(review_resolve, "FETCH_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(git, "run_group", lambda argv, **kw: (128, _rpc_early_eof()))

    with pytest.raises(review_resolve.FetchFailedError) as exc:
        review_resolve.resolve_change(tmp_path, "#142")

    assert exc.value.problem == "fatal: early EOF"
    assert "fatal: early EOF" in exc.value.host_action
    assert "curl 92" not in exc.value.host_action


def test_a_fetch_that_cannot_even_be_spawned_is_an_infra_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review f-002: `run_group` spawned the child outside any OSError guard,
    so a missing / unexecutable `git` (or a checkout deleted under `cwd`) left
    the CLI with an uncaught traceback and no `--json` — the watcher's
    `crashed` path again."""
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)

    def no_git(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(git.subprocess, "Popen", no_git)

    with pytest.raises(review_resolve.FetchFailedError) as exc:
        review_resolve.resolve_change(tmp_path, "#142")

    assert "cannot run git" in exc.value.problem
    assert "SSH agent" in exc.value.host_action


def test_a_hung_transport_spends_one_timeout_for_the_whole_intake_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review security f-003: `timed out` is a retryable sign, so three
    attempts would hold a dispatcher's single-flight slot for 3 × 300 s. The
    attempts share ONE budget: a hiccup that answers in milliseconds still gets
    its retries, a hang gets none."""
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    monkeypatch.setattr(review_resolve, "FETCH_RETRY_BACKOFF_SECONDS", 0.0)
    clock = {"now": 1000.0}
    monkeypatch.setattr(review_resolve.time, "monotonic", lambda: clock["now"])
    timeouts: list[float] = []

    def slow_then_hung(argv, *, timeout, **kw):
        timeouts.append(timeout)
        if len(timeouts) == 1:  # a transport failure 100s in
            clock["now"] += 100
            return 128, _ssh_hiccup()
        clock["now"] += timeout  # the retry hangs until its deadline
        return None, ""

    monkeypatch.setattr(git, "run_group", slow_then_hung)

    with pytest.raises(review_resolve.FetchFailedError) as exc:
        review_resolve.resolve_change(tmp_path, "#142")

    # attempt 2 got only what was LEFT of the one budget, and there is no third
    assert timeouts == [review_resolve.PR_FETCH_TIMEOUT_SECONDS, 200.0]
    assert "timed out" in exc.value.problem


def test_the_host_action_cannot_carry_what_it_does_not_show(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review security f-001: git's stderr is the ORIGIN host's text (an ssh
    banner, a `remote:` line) and `host_action` is printed on the operator's
    terminal and published in a `[Friction]` — so it is stripped and bounded."""
    from lithos_loom.runner import git

    _stub_pr_metadata(monkeypatch)
    forged = "fatal: \x1b[2Kok to merge‮ " + "x" * 600
    monkeypatch.setattr(git, "run_group", lambda argv, **kw: (128, forged))

    with pytest.raises(review_resolve.FetchFailedError) as exc:
        review_resolve.resolve_change(tmp_path, "#142")

    problem = exc.value.problem
    assert "\x1b" not in problem and "‮" not in problem
    assert len(problem) <= 300 and problem.endswith("…")
    assert problem in exc.value.host_action

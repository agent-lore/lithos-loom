"""Tests for ``lithos_loom.github_client``.

The module wraps the GitHub REST API for the github-issue-watcher
(docs/prd/github-issue-watcher.md, Slice 7.1). It owns:

- the `gh auth token` shell-out at startup,
- issue list / get / body-update REST calls,
- the ``<!-- lithos:<task_id> -->`` linkage-marker parser + writer,
- 401 / 404 / 403-rate-limit error typing.

Pure helpers (parse + marker) are tested directly. The HTTP surface uses
respx (already a project dev dep) to mock ``httpx.AsyncClient`` round-trips.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from lithos_loom.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubIssueNotFoundError,
    GitHubRef,
    GitHubRepoNotFoundError,
    Issue,
    PullRequest,
    PullRequestReview,
    PullRequestReviewComment,
    _parse_issues_response,
    _parse_pull_request,
    _resolve_gh_token,
    apply_marker,
    parse_github_ref,
    parse_marker,
)

# ── Issue parsing ─────────────────────────────────────────────────────


def test_parse_issues_response_returns_typed_issues() -> None:
    raw = [
        {
            "number": 42,
            "title": "Bug in login",
            "body": "Steps to reproduce...",
            "state": "open",
            "state_reason": None,
            "labels": [{"name": "bug"}, {"name": "ui"}],
            "user": {"login": "alice"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": "https://github.com/agent-lore/lithos-loom/issues/42",
        },
    ]
    issues = _parse_issues_response(raw, repo="agent-lore/lithos-loom")
    assert len(issues) == 1
    iss = issues[0]
    assert isinstance(iss, Issue)
    assert iss.number == 42
    assert iss.title == "Bug in login"
    assert iss.body == "Steps to reproduce..."
    assert iss.state == "open"
    assert iss.state_reason is None
    assert iss.labels == ("bug", "ui")
    assert iss.author == "alice"
    assert iss.repo == "agent-lore/lithos-loom"
    assert iss.html_url == "https://github.com/agent-lore/lithos-loom/issues/42"
    assert iss.updated_at == datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)


def test_parse_issues_response_filters_pull_requests() -> None:
    """D53: PRs have a ``pull_request`` object on the same endpoint payload.

    GitHub's ``/issues`` REST endpoint returns both. Filter at parse time.
    """
    raw = [
        {
            "number": 1,
            "title": "Real issue",
            "body": "",
            "state": "open",
            "state_reason": None,
            "labels": [],
            "user": {"login": "alice"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": "https://github.com/x/y/issues/1",
        },
        {
            "number": 2,
            "title": "Pull request",
            "body": "",
            "state": "open",
            "state_reason": None,
            "labels": [],
            "user": {"login": "alice"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": "https://github.com/x/y/pull/2",
            "pull_request": {"url": "https://api.github.com/repos/x/y/pulls/2"},
        },
    ]
    issues = _parse_issues_response(raw, repo="x/y")
    assert [i.number for i in issues] == [1]


def test_parse_issues_response_handles_null_body() -> None:
    """GitHub returns ``body: null`` when issue body is empty."""
    raw = [
        {
            "number": 1,
            "title": "t",
            "body": None,
            "state": "open",
            "state_reason": None,
            "labels": [],
            "user": {"login": "x"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": "u",
        }
    ]
    issues = _parse_issues_response(raw, repo="x/y")
    assert issues[0].body == ""


def test_parse_issues_response_closed_state_reasons() -> None:
    raw = [
        {
            "number": 1,
            "title": "t",
            "body": "",
            "state": "closed",
            "state_reason": "completed",
            "labels": [],
            "user": {"login": "x"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": "u",
        },
        {
            "number": 2,
            "title": "t",
            "body": "",
            "state": "closed",
            "state_reason": "not_planned",
            "labels": [],
            "user": {"login": "x"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": "u",
        },
    ]
    issues = _parse_issues_response(raw, repo="x/y")
    assert issues[0].state == "closed" and issues[0].state_reason == "completed"
    assert issues[1].state == "closed" and issues[1].state_reason == "not_planned"


# ── Linkage marker parser + writer (D46) ──────────────────────────────


def test_parse_marker_finds_canonical_form_at_end() -> None:
    body = "Some issue text.\n\n<!-- lithos:abc-123 -->"
    assert parse_marker(body) == "abc-123"


def test_parse_marker_finds_marker_at_top() -> None:
    body = "<!-- lithos:abc-123 -->\n\nSome issue text."
    assert parse_marker(body) == "abc-123"


def test_parse_marker_is_case_insensitive() -> None:
    body = "x\n<!-- LITHOS:ABC-123 -->\ny"
    assert parse_marker(body) == "ABC-123"


def test_parse_marker_returns_none_when_missing() -> None:
    assert parse_marker("nothing here") is None
    assert parse_marker("") is None


def test_parse_marker_ignores_malformed_markers() -> None:
    """No task id, weird shapes — refuse to guess."""
    assert parse_marker("<!-- lithos: -->") is None
    assert parse_marker("<!-- lithos -->") is None


# ── parse_github_ref (the one home for the issue/PR URL grammar, ARCH-7) ──


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Canonical issue / PR URLs.
        (
            "https://github.com/agent-lore/lithos-loom/issues/42",
            GitHubRef("agent-lore/lithos-loom", 42, "issue"),
        ),
        (
            "https://github.com/o/r/pull/82",
            GitHubRef("o/r", 82, "pull"),
        ),
        # A single trailing slash is tolerated; surrounding whitespace stripped.
        ("https://github.com/o/r/issues/1/", GitHubRef("o/r", 1, "issue")),
        ("  https://github.com/o/r/pull/9  ", GitHubRef("o/r", 9, "pull")),
        # Rejected shapes → None.
        ("https://github.com/o/r", None),  # no kind / number
        ("https://github.com/o/r/pull/notanum", None),  # non-numeric id
        ("https://github.com/o/r/pull/82/files", None),  # trailing path segment
        ("https://github.com/o/r/discussions/3", None),  # unknown kind
        ("https://example.com/o/r/pull/1", None),  # wrong host
        ("http://github.com/o/r/pull/1", None),  # non-https scheme
        ("not a url", None),
        ("", None),
        (None, None),  # non-string
        (42, None),  # non-string
    ],
)
def test_parse_github_ref(url: object, expected: GitHubRef | None) -> None:
    assert parse_github_ref(url) == expected


def test_parse_github_ref_kind_is_singular() -> None:
    """The URL segment is ``issues``/``pull``; the ref normalises to the
    singular ``issue``/``pull`` so callers filter on one canonical vocabulary."""
    assert parse_github_ref("https://github.com/o/r/issues/1").kind == "issue"  # type: ignore[union-attr]
    assert parse_github_ref("https://github.com/o/r/pull/1").kind == "pull"  # type: ignore[union-attr]


def test_apply_marker_appends_when_absent() -> None:
    body = "Some text."
    out = apply_marker(body, "abc-123")
    assert out == "Some text.\n\n<!-- lithos:abc-123 -->"


def test_apply_marker_replaces_existing_marker() -> None:
    body = "Some text.\n\n<!-- lithos:old-id -->"
    out = apply_marker(body, "new-id")
    assert "old-id" not in out
    assert out.endswith("<!-- lithos:new-id -->")


def test_apply_marker_replaces_existing_marker_at_top() -> None:
    body = "<!-- lithos:old-id -->\nbody here"
    out = apply_marker(body, "new-id")
    assert "old-id" not in out
    assert "<!-- lithos:new-id -->" in out
    # Re-canonicalises: marker moves to the end.
    assert out.endswith("<!-- lithos:new-id -->")


def test_apply_marker_empty_body() -> None:
    assert apply_marker("", "abc") == "<!-- lithos:abc -->"
    assert apply_marker(None, "abc") == "<!-- lithos:abc -->"


# ── `gh auth token` resolver ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_gh_token_returns_token_on_success() -> None:
    fake_proc = _FakeProc(returncode=0, stdout=b"gho_fake_token\n", stderr=b"")
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        token = await _resolve_gh_token()
    assert token == "gho_fake_token"


@pytest.mark.asyncio
async def test_resolve_gh_token_raises_on_nonzero_exit() -> None:
    fake_proc = _FakeProc(returncode=1, stdout=b"", stderr=b"You are not logged in")
    with (
        patch("asyncio.create_subprocess_exec", return_value=fake_proc),
        pytest.raises(GitHubAuthError, match="not logged in"),
    ):
        await _resolve_gh_token()


@pytest.mark.asyncio
async def test_resolve_gh_token_raises_when_gh_missing() -> None:
    """If ``gh`` is not on PATH, the subprocess spawn raises FileNotFoundError."""
    with (
        patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError),
        pytest.raises(GitHubAuthError, match="gh"),
    ):
        await _resolve_gh_token()


# ── HTTP surface (respx-mocked) ───────────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_since_happy_path() -> None:
    route = respx.get(
        "https://api.github.com/repos/agent-lore/lithos-loom/issues"
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "number": 1,
                    "title": "Test",
                    "body": "body",
                    "state": "open",
                    "state_reason": None,
                    "labels": [{"name": "bug"}],
                    "user": {"login": "alice"},
                    "updated_at": "2026-05-29T10:00:00Z",
                    "html_url": "https://github.com/agent-lore/lithos-loom/issues/1",
                }
            ],
        )
    )
    repo = "agent-lore/lithos-loom"
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        issues = await client.list_issues_since(repo, since=None)
    assert len(issues) == 1
    assert issues[0].number == 1
    # Bearer token plumbed through.
    assert route.calls[0].request.headers["authorization"] == "Bearer fake"


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_since_sends_iso_cursor() -> None:
    route = respx.get("https://api.github.com/repos/x/y/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    cursor = datetime(2026, 5, 29, tzinfo=UTC)
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.list_issues_since("x/y", since=cursor)
    request_url = str(route.calls[0].request.url)
    assert (
        "since=2026-05-29T00%3A00%3A00%2B00%3A00" in request_url
        or "since=2026-05-29T00:00:00" in request_url
    )


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_since_defaults_to_state_all() -> None:
    """Regression for PR-review finding 1: previous default state="open"
    silently suppressed all close-event polls, so the GH→Lithos close
    mirror never fired. Default must be state="all"."""
    route = respx.get("https://api.github.com/repos/x/y/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.list_issues_since("x/y", since=None)
    request_url = str(route.calls[0].request.url)
    assert "state=all" in request_url


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_since_paginates_via_link_header() -> None:
    """Regression for PR-review finding (round 3): without pagination,
    a repo with >100 issues in scope would return only the oldest 100,
    leaving the source to crawl one page per poll interval before
    reaching live state. The watcher must drain every page via
    ``Link: rel="next"``.
    """

    def _issue(num: int) -> dict[str, Any]:
        return {
            "number": num,
            "title": f"issue {num}",
            "body": "",
            "state": "open",
            "state_reason": None,
            "labels": [],
            "user": {"login": "alice"},
            "updated_at": "2026-05-29T12:00:00Z",
            "html_url": f"https://github.com/x/y/issues/{num}",
        }

    page1_url = "https://api.github.com/repos/x/y/issues"
    page2_url = "https://api.github.com/repos/x/y/issues?page=2"
    page3_url = "https://api.github.com/repos/x/y/issues?page=3"

    respx.get(page1_url, params={"state": "all"}).mock(
        return_value=httpx.Response(
            200,
            headers={"Link": f'<{page2_url}>; rel="next", <{page3_url}>; rel="last"'},
            json=[_issue(1), _issue(2)],
        )
    )
    respx.get(page2_url).mock(
        return_value=httpx.Response(
            200,
            headers={"Link": f'<{page3_url}>; rel="next"'},
            json=[_issue(3), _issue(4)],
        )
    )
    respx.get(page3_url).mock(
        return_value=httpx.Response(200, json=[_issue(5)]),
    )

    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        issues = await client.list_issues_since("x/y", since=None)

    assert [i.number for i in issues] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_since_stops_when_no_next_link() -> None:
    """Single-page response: no Link header → no further requests."""
    route = respx.get("https://api.github.com/repos/x/y/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.list_issues_since("x/y", since=None)
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_since_passes_state_param() -> None:
    """The bootstrap path passes state="open" explicitly."""
    route = respx.get("https://api.github.com/repos/x/y/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.list_issues_since("x/y", since=None, state="open")
    request_url = str(route.calls[0].request.url)
    assert "state=open" in request_url


@pytest.mark.asyncio
@respx.mock
async def test_list_open_issues_404_raises_repo_not_found() -> None:
    respx.get("https://api.github.com/repos/missing/repo/issues").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubRepoNotFoundError, match="missing/repo"):
            await client.list_issues_since("missing/repo", since=None)


@pytest.mark.asyncio
@respx.mock
async def test_list_open_issues_401_raises_auth_error() -> None:
    respx.get("https://api.github.com/repos/x/y/issues").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubAuthError):
            await client.list_issues_since("x/y", since=None)


@pytest.mark.asyncio
@respx.mock
async def test_list_open_issues_rate_limit_sleeps_then_retries() -> None:
    """D44 / D70: on 403 + X-RateLimit-Remaining=0, back off until reset.

    The client should sleep until the reset epoch, then retry. The test pins
    ``asyncio.sleep`` to verify the wait duration without actually sleeping.
    """
    # First call: rate-limited. Second call: success.
    reset_epoch = int((datetime.now(UTC) + timedelta(seconds=30)).timestamp())
    respx.get("https://api.github.com/repos/x/y/issues").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(reset_epoch),
                },
                json={"message": "rate limit"},
            ),
            httpx.Response(200, json=[]),
        ]
    )
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with patch("lithos_loom.github_client.asyncio.sleep", _fake_sleep):
            issues = await client.list_issues_since("x/y", since=None)
    assert issues == []
    assert len(sleep_calls) == 1
    # Slept for ~30s (within a small tolerance).
    assert 20.0 <= sleep_calls[0] <= 35.0


@pytest.mark.asyncio
@respx.mock
async def test_list_open_issues_403_without_rate_limit_raises() -> None:
    """A 403 that is NOT a rate-limit signal must surface as an error.

    Distinguishing rate-limit from "permission denied" matters: a silent retry
    on a permanent 403 would spin forever.
    """
    # Remaining budget present → not a rate-limit 403, surfaces as auth error.
    respx.get("https://api.github.com/repos/x/y/issues").mock(
        return_value=httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "100"},
            json={"message": "Resource not accessible by integration"},
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubAuthError):
            await client.list_issues_since("x/y", since=None)


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_body_happy_path() -> None:
    route = respx.patch("https://api.github.com/repos/x/y/issues/42").mock(
        return_value=httpx.Response(200, json={"number": 42, "body": "new body"})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.update_issue_body("x/y", 42, "new body")
    request = route.calls[0].request
    import json as _json

    assert _json.loads(request.content) == {"body": "new body"}


@pytest.mark.asyncio
@respx.mock
async def test_get_issue_happy_path() -> None:
    respx.get("https://api.github.com/repos/x/y/issues/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "title": "t",
                "body": "b",
                "state": "open",
                "state_reason": None,
                "labels": [],
                "user": {"login": "alice"},
                "updated_at": "2026-05-29T12:00:00Z",
                "html_url": "u",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        issue = await client.get_issue("x/y", 42)
    assert issue is not None
    assert issue.number == 42


@pytest.mark.asyncio
@respx.mock
async def test_get_issue_404_returns_none() -> None:
    """A missing issue is not exceptional — the watcher routinely checks
    for the linked task's source issue and a 404 just means it was deleted."""
    respx.get("https://api.github.com/repos/x/y/issues/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        assert await client.get_issue("x/y", 999) is None


# ── get_pull_request (#87 PR-merge watcher) ───────────────────────────


def test_parse_pull_request_merged() -> None:
    pr = _parse_pull_request(
        {
            "number": 7,
            "state": "closed",
            "merged": True,
            "merged_at": "2026-06-13T12:00:00Z",
            "merge_commit_sha": "deadbeef",
        },
        repo="x/y",
    )
    assert pr == PullRequest(
        repo="x/y",
        number=7,
        state="closed",
        merged=True,
        merged_at=datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC),
        merge_commit_sha="deadbeef",
    )


def test_parse_pull_request_open_has_no_merge_fields() -> None:
    pr = _parse_pull_request(
        {"number": 7, "state": "open", "merged": False, "merged_at": None}, repo="x/y"
    )
    assert pr.merged is False and pr.merged_at is None and pr.merge_commit_sha is None


@pytest.mark.asyncio
@respx.mock
async def test_get_pull_request_merged() -> None:
    respx.get("https://api.github.com/repos/x/y/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 7,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-06-13T12:00:00Z",
                "merge_commit_sha": "abc123",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        pr = await GitHubClient(http=http, token="fake").get_pull_request("x/y", 7)
    assert pr is not None and pr.merged is True and pr.merge_commit_sha == "abc123"


@pytest.mark.asyncio
@respx.mock
async def test_get_pull_request_open() -> None:
    respx.get("https://api.github.com/repos/x/y/pulls/7").mock(
        return_value=httpx.Response(
            200, json={"number": 7, "state": "open", "merged": False, "merged_at": None}
        )
    )
    async with httpx.AsyncClient() as http:
        pr = await GitHubClient(http=http, token="fake").get_pull_request("x/y", 7)
    assert pr is not None and pr.state == "open" and pr.merged is False


@pytest.mark.asyncio
@respx.mock
async def test_get_pull_request_closed_unmerged() -> None:
    respx.get("https://api.github.com/repos/x/y/pulls/7").mock(
        return_value=httpx.Response(
            200,
            json={"number": 7, "state": "closed", "merged": False, "merged_at": None},
        )
    )
    async with httpx.AsyncClient() as http:
        pr = await GitHubClient(http=http, token="fake").get_pull_request("x/y", 7)
    assert pr is not None and pr.state == "closed" and pr.merged is False


@pytest.mark.asyncio
@respx.mock
async def test_get_pull_request_404_returns_none() -> None:
    respx.get("https://api.github.com/repos/x/y/pulls/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with httpx.AsyncClient() as http:
        assert (
            await GitHubClient(http=http, token="fake").get_pull_request("x/y", 999)
            is None
        )


# ── update_issue_fields (Slice 7.2) ───────────────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_fields_title_only() -> None:
    """Lithos→GH title push: PATCH carries only the title field, nothing else."""
    route = respx.patch("https://api.github.com/repos/x/y/issues/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "title": "renamed",
                "body": "b",
                "state": "open",
                "state_reason": None,
                "labels": [],
                "user": {"login": "alice"},
                "updated_at": "2026-05-29T12:00:00Z",
                "html_url": "u",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        issue = await client.update_issue_fields("x/y", 42, title="renamed")
    import json as _json

    assert _json.loads(route.calls[0].request.content) == {"title": "renamed"}
    assert issue is not None
    assert issue.title == "renamed"


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_fields_state_only_with_reason() -> None:
    """Lithos→GH close mirror: state="closed" + state_reason in one PATCH."""
    route = respx.patch("https://api.github.com/repos/x/y/issues/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "title": "t",
                "body": "b",
                "state": "closed",
                "state_reason": "completed",
                "labels": [],
                "user": {"login": "alice"},
                "updated_at": "2026-05-29T12:00:00Z",
                "html_url": "u",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        issue = await client.update_issue_fields(
            "x/y", 42, state="closed", state_reason="completed"
        )
    import json as _json

    assert _json.loads(route.calls[0].request.content) == {
        "state": "closed",
        "state_reason": "completed",
    }
    assert issue is not None
    assert issue.state == "closed"
    assert issue.state_reason == "completed"


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_fields_combined_payload() -> None:
    """All three fields in one PATCH — verifying nothing extra leaks in."""
    route = respx.patch("https://api.github.com/repos/x/y/issues/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 7,
                "title": "renamed",
                "body": "b",
                "state": "closed",
                "state_reason": "not_planned",
                "labels": [],
                "user": {"login": "alice"},
                "updated_at": "2026-05-29T12:00:00Z",
                "html_url": "u",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.update_issue_fields(
            "x/y",
            7,
            title="renamed",
            state="closed",
            state_reason="not_planned",
        )
    import json as _json

    assert _json.loads(route.calls[0].request.content) == {
        "title": "renamed",
        "state": "closed",
        "state_reason": "not_planned",
    }


@pytest.mark.asyncio
async def test_update_issue_fields_no_fields_is_noop() -> None:
    """Defensive: calling with every kwarg None must not issue a request.

    Avoids a wasted API call (and a wasted rate-limit slot) for handlers
    that compute "nothing changed" and call through anyway. The issue is
    re-fetched and returned (None signals "no PATCH and no fetch").
    """
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        result = await client.update_issue_fields("x/y", 42)
    assert result is None


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_fields_422_raises() -> None:
    """GitHub rejects an unknown state_reason with HTTP 422 (Unprocessable).

    Mapped to GitHubError so the handler can log + skip rather than crash.
    """
    respx.patch("https://api.github.com/repos/x/y/issues/42").mock(
        return_value=httpx.Response(
            422, json={"message": "Validation Failed: bad state_reason"}
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubError):
            await client.update_issue_fields("x/y", 42, state_reason="garbage")


@pytest.mark.asyncio
@respx.mock
async def test_patch_rate_limited_sleeps_then_retries() -> None:
    """PR-review finding 5 (2026-05-30): PATCH used to skip the rate-limit
    retry that GETs already had, turning a 403 + remaining=0 into a hard
    GitHubAuthError. PRD story #70 requires graceful backoff for all
    operations. Marker writes, title pushes, and close mirrors must all
    sleep until X-RateLimit-Reset and retry once.
    """
    reset_epoch = int((datetime.now(UTC) + timedelta(seconds=30)).timestamp())
    respx.patch("https://api.github.com/repos/x/y/issues/42").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(reset_epoch),
                },
                json={"message": "rate limit"},
            ),
            httpx.Response(
                200,
                json={
                    "number": 42,
                    "title": "t",
                    "body": "b",
                    "state": "closed",
                    "state_reason": "completed",
                    "labels": [],
                    "user": {"login": "alice"},
                    "updated_at": "2026-05-29T12:00:00Z",
                    "html_url": "u",
                },
            ),
        ]
    )
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with patch("lithos_loom.github_client.asyncio.sleep", _fake_sleep):
            issue = await client.update_issue_fields(
                "x/y", 42, state="closed", state_reason="completed"
            )
    assert issue is not None
    assert issue.state == "closed"
    assert len(sleep_calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_update_issue_fields_404_raises_issue_not_found() -> None:
    """Issue deleted by operator → issue-specific 404 (#69), NOT a repo-404. The
    push handler self-heals the orphaned link on this; mislabelling it
    GitHubRepoNotFoundError would conflate it with a deleted repo."""
    respx.patch("https://api.github.com/repos/x/y/issues/42").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubIssueNotFoundError) as excinfo:
            await client.update_issue_fields("x/y", 42, state="closed")
    assert excinfo.value.repo == "x/y"
    assert excinfo.value.number == 42
    # the issue error must NOT be a repo error (repo-drop path must never catch it)
    assert not isinstance(excinfo.value, GitHubRepoNotFoundError)


# ── Test plumbing ─────────────────────────────────────────────────────


class _FakeProc:
    """asyncio.subprocess.Process stand-in for the gh-auth-token resolver tests."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


# Smoke check that pytest-asyncio is configured for this file (mode=auto in pyproject).
@pytest.mark.asyncio
async def test_asyncio_marker_works() -> None:
    await asyncio.sleep(0)


# ── PR access surface (ARCH-7c: story-develop's PR ops on the typed seam) ──

_REPO = "agent-lore/lithos-loom"


def test_parse_pull_request_includes_refs_title_body() -> None:
    """The single-PR endpoint carries head/base refs + title/body — the review-only
    resolver (review_resolve) reads these off the same typed PullRequest the merge
    watcher uses for state/merged."""
    pr = _parse_pull_request(
        {
            "number": 142,
            "state": "open",
            "merged": False,
            "merged_at": None,
            "head": {"sha": "h" * 40, "ref": "feature"},
            "base": {"sha": "b" * 40, "ref": "main"},
            "title": "Add a thing",
            "body": "This PR adds a thing.",
        },
        repo=_REPO,
    )
    assert pr.head_sha == "h" * 40
    assert pr.base_ref == "main"
    assert pr.head_ref == "feature"
    assert pr.title == "Add a thing"
    assert pr.body == "This PR adds a thing."


def test_parse_pull_request_tolerates_missing_ref_fields() -> None:
    """A minimal row (the merge-watcher's older test shape) still parses — the new
    ref/title/body fields default to empty rather than KeyError-ing."""
    pr = _parse_pull_request(
        {"number": 7, "state": "closed", "merged": True, "merged_at": None}, repo=_REPO
    )
    assert pr.head_sha == "" and pr.base_ref == "" and pr.title == "" and pr.body == ""


@pytest.mark.asyncio
@respx.mock
async def test_get_pull_request_exposes_refs_and_body() -> None:
    route = respx.get(f"https://api.github.com/repos/{_REPO}/pulls/142").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 142,
                "state": "open",
                "merged": False,
                "merged_at": None,
                "head": {"sha": "a" * 40, "ref": "topic"},
                "base": {"sha": "c" * 40, "ref": "main"},
                "title": "T",
                "body": "B",
            },
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        pr = await client.get_pull_request(_REPO, 142)
    assert pr is not None
    assert pr.head_sha == "a" * 40 and pr.base_ref == "main" and pr.body == "B"
    assert route.calls[0].request.headers["authorization"] == "Bearer fake"


@pytest.mark.asyncio
@respx.mock
async def test_list_pull_request_reviews_happy_path() -> None:
    route = respx.get(f"https://api.github.com/repos/{_REPO}/pulls/9/reviews").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"user": {"login": "copilot[bot]"}, "body": "generated 3 comments"},
                {"user": {"login": "human"}, "body": "lgtm"},
            ],
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        reviews = await client.list_pull_request_reviews(_REPO, 9)
    assert reviews == [
        PullRequestReview(author="copilot[bot]", body="generated 3 comments"),
        PullRequestReview(author="human", body="lgtm"),
    ]
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_list_pull_request_reviews_paginates() -> None:
    base = f"https://api.github.com/repos/{_REPO}/pulls/9/reviews"
    # Register the page-2 route first (with an explicit ``page=2`` matcher) so
    # first-match-wins routes the second request here rather than re-matching
    # the paramless page-1 route (which would loop on its own Link header).
    respx.get(base, params={"page": "2"}).mock(
        return_value=httpx.Response(200, json=[{"user": {"login": "b"}, "body": "two"}])
    )
    respx.get(base).mock(
        return_value=httpx.Response(
            200,
            json=[{"user": {"login": "a"}, "body": "one"}],
            headers={"Link": f'<{base}?page=2>; rel="next"'},
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        reviews = await client.list_pull_request_reviews(_REPO, 9)
    assert [r.author for r in reviews] == ["a", "b"]


@pytest.mark.asyncio
@respx.mock
async def test_list_pull_request_review_comments_maps_fields() -> None:
    route = respx.get(f"https://api.github.com/repos/{_REPO}/pulls/9/comments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 111,
                    "user": {"login": "copilot[bot]"},
                    "path": "src/x.py",
                    "line": 12,
                    "body": "nit",
                    "in_reply_to_id": None,
                },
                {
                    "id": 112,
                    "user": {"login": "copilot[bot]"},
                    "path": "src/y.py",
                    "line": None,
                    "original_line": 5,
                    "body": "on an outdated line",
                    "in_reply_to_id": 111,
                },
            ],
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        comments = await client.list_pull_request_review_comments(_REPO, 9)
    assert comments[0] == PullRequestReviewComment(
        comment_id=111,
        author="copilot[bot]",
        path="src/x.py",
        line=12,
        body="nit",
        in_reply_to_id=None,
    )
    # line falls back to original_line for comments on since-changed lines.
    assert comments[1].line == 5 and comments[1].in_reply_to_id == 111
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_request_reviewers_posts_reviewers_body() -> None:
    route = respx.post(
        f"https://api.github.com/repos/{_REPO}/pulls/7/requested_reviewers"
    ).mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.request_reviewers(_REPO, 7, ["dave"])
    assert json.loads(route.calls[0].request.content) == {"reviewers": ["dave"]}


@pytest.mark.asyncio
@respx.mock
async def test_request_reviewers_422_raises_github_error_with_message() -> None:
    """A 422 (e.g. self-author) surfaces as a typed GitHubError whose message
    carries GitHub's text — the caller branches on 'pull request author'."""
    respx.post(
        f"https://api.github.com/repos/{_REPO}/pulls/7/requested_reviewers"
    ).mock(
        return_value=httpx.Response(
            422,
            json={"message": "Review cannot be requested from pull request author."},
        )
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubError, match="pull request author"):
            await client.request_reviewers(_REPO, 7, ["dave"])


@pytest.mark.asyncio
@respx.mock
async def test_add_assignees_posts_assignees_body() -> None:
    route = respx.post(f"https://api.github.com/repos/{_REPO}/issues/7/assignees").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.add_assignees(_REPO, 7, ["dave"])
    assert json.loads(route.calls[0].request.content) == {"assignees": ["dave"]}


@pytest.mark.asyncio
@respx.mock
async def test_create_review_comment_reply_posts_body() -> None:
    route = respx.post(
        f"https://api.github.com/repos/{_REPO}/pulls/7/comments/55/replies"
    ).mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.create_review_comment_reply(_REPO, 7, 55, "thanks")
    assert json.loads(route.calls[0].request.content) == {"body": "thanks"}


@pytest.mark.asyncio
@respx.mock
async def test_create_issue_comment_posts_body() -> None:
    route = respx.post(f"https://api.github.com/repos/{_REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        await client.create_issue_comment(_REPO, 7, "hello")
    assert json.loads(route.calls[0].request.content) == {"body": "hello"}


@pytest.mark.asyncio
@respx.mock
async def test_pr_write_maps_404_to_repo_not_found() -> None:
    """A repo-level 404 on a write routes through _raise_for_status like the
    read paths (the caller catches GitHubError)."""
    respx.post(f"https://api.github.com/repos/{_REPO}/issues/7/comments").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(http=http, token="fake")
        with pytest.raises(GitHubRepoNotFoundError):
            await client.create_issue_comment(_REPO, 7, "hello")

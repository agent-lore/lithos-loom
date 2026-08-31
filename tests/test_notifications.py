"""Tests for the needs-human push sinks (b91177d2, design D6).

Each sink is exercised through :meth:`Notifier.needs_human` against injected
subprocess / GitHub fakes. The contract under test: every configured sink fires
once with the notice, a failing / hanging / missing sink becomes a problem
string (or a silent skip) and never raises, and the operator's own gates are
not this module's business (that gating lives at the call site).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from lithos_loom import notifications
from lithos_loom.github_client import GitHubTransportError
from lithos_loom.notifications import NeedsHumanNotice, Notifier, notice_github_ref

_NOTICE = NeedsHumanNotice(
    gate_id="gate-0123456789",
    story_id="story-1",
    story_title="Wire the thing",
    project="lens",
    route="story-develop",
    reason="max_rounds",
    summary="round 5: NOT approved (max_rounds)",
    run_id="abcd1234",
    github_ref="https://github.com/agent-lore/lithos-lens/issues/42",
)


class _Proc:
    """A stand-in for an asyncio subprocess: records stdin, returns a status."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.stdin_data: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_data = input
        return b"", self._stderr


class _Spawner:
    def __init__(self, proc: _Proc | None = None, *, raises: Exception | None = None):
        self.proc = proc or _Proc()
        self.raises = raises
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> _Proc:
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.proc


class _Commenter:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.raises = raises

    async def create_issue_comment(self, repo: str, number: int, body: str) -> None:
        if self.raises is not None:
            raise self.raises
        self.calls.append((repo, number, body))


@pytest.fixture
def notify_send_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifications, "_which", lambda name: f"/usr/bin/{name}")


# ── desktop toast ────────────────────────────────────────────────────────


async def test_toast_runs_notify_send_with_title_and_body(
    monkeypatch: pytest.MonkeyPatch, notify_send_on_path: None
) -> None:
    spawner = _Spawner()
    monkeypatch.setattr(notifications, "_spawn_exec", spawner)

    problems = await Notifier(desktop_toast=True).needs_human(_NOTICE)

    assert problems == []
    ((args, _kwargs),) = spawner.calls
    assert args[0] == "/usr/bin/notify-send"
    assert "Loom needs you: Wire the thing" in args
    assert "max_rounds — round 5: NOT approved (max_rounds) (gate gate-012)" in args


async def test_toast_is_skipped_silently_when_notify_send_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifications, "_which", lambda name: None)
    spawner = _Spawner()
    monkeypatch.setattr(notifications, "_spawn_exec", spawner)

    problems = await Notifier(desktop_toast=True).needs_human(_NOTICE)

    assert problems == []
    assert spawner.calls == []


async def test_toast_disabled_does_not_spawn(
    monkeypatch: pytest.MonkeyPatch, notify_send_on_path: None
) -> None:
    spawner = _Spawner()
    monkeypatch.setattr(notifications, "_spawn_exec", spawner)

    await Notifier(desktop_toast=False).needs_human(_NOTICE)

    assert spawner.calls == []


async def test_toast_failure_is_a_problem_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, notify_send_on_path: None
) -> None:
    spawner = _Spawner(_Proc(returncode=1, stderr=b"no dbus session"))
    monkeypatch.setattr(notifications, "_spawn_exec", spawner)

    problems = await Notifier(desktop_toast=True).needs_human(_NOTICE)

    assert problems == ["notify-send exited 1: no dbus session"]


# ── operator command ─────────────────────────────────────────────────────


async def test_command_receives_the_notice_as_json_on_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _Spawner()
    monkeypatch.setattr(notifications, "_spawn_shell", spawner)

    problems = await Notifier(
        desktop_toast=False, command="curl -d @- https://ntfy.sh/loom"
    ).needs_human(_NOTICE)

    assert problems == []
    ((args, kwargs),) = spawner.calls
    assert args == ("curl -d @- https://ntfy.sh/loom",)
    assert kwargs["stdin"] is asyncio.subprocess.PIPE
    assert spawner.proc.stdin_data is not None
    sent = json.loads(spawner.proc.stdin_data)
    assert sent["gate_id"] == "gate-0123456789"
    assert sent["reason"] == "max_rounds"
    assert sent["story_title"] == "Wire the thing"
    assert sent["github_ref"] == _NOTICE.github_ref


async def test_command_nonzero_exit_is_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _Spawner(_Proc(returncode=7, stderr=b"curl: (6) could not resolve"))
    monkeypatch.setattr(notifications, "_spawn_shell", spawner)

    problems = await Notifier(desktop_toast=False, command="curl x").needs_human(
        _NOTICE
    )

    assert problems == ["on_needs_human command exited 7: curl: (6) could not resolve"]


async def test_command_spawn_error_is_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _Spawner(raises=OSError("fork failed"))
    monkeypatch.setattr(notifications, "_spawn_shell", spawner)

    problems = await Notifier(desktop_toast=False, command="x").needs_human(_NOTICE)

    assert problems == ["notify command failed: fork failed"]


async def test_no_command_configured_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawner = _Spawner()
    monkeypatch.setattr(notifications, "_spawn_shell", spawner)

    assert await Notifier(desktop_toast=False, command=None).needs_human(_NOTICE) == []
    assert spawner.calls == []


# ── github mention ───────────────────────────────────────────────────────


async def test_github_mention_comments_on_the_linked_issue() -> None:
    commenter = _Commenter()

    problems = await Notifier(
        desktop_toast=False, github_login="dave", github=commenter
    ).needs_human(_NOTICE)

    assert problems == []
    ((repo, number, body),) = commenter.calls
    assert (repo, number) == ("agent-lore/lithos-lens", 42)
    assert body.startswith("@dave [NeedsHuman]")
    assert "Wire the thing" in body
    assert "max_rounds" in body
    assert "gate-0123456789" in body
    assert "complete it to re-dispatch" in body


async def test_github_mention_targets_a_delivered_pr_too() -> None:
    commenter = _Commenter()
    notice = NeedsHumanNotice(
        **{
            **_NOTICE.__dict__,
            "github_ref": "https://github.com/agent-lore/lithos-lens/pull/37",
        }
    )

    await Notifier(
        desktop_toast=False, github_login="dave", github=commenter
    ).needs_human(notice)

    assert commenter.calls[0][:2] == ("agent-lore/lithos-lens", 37)


@pytest.mark.parametrize(
    "notifier_kwargs",
    [
        {"github_login": None, "github": _Commenter()},  # no login configured
        {"github_login": "dave", "github": None},  # no gh client on this host
    ],
)
async def test_github_mention_stands_down_without_login_or_client(
    notifier_kwargs: dict[str, Any],
) -> None:
    problems = await Notifier(desktop_toast=False, **notifier_kwargs).needs_human(
        _NOTICE
    )
    assert problems == []
    commenter = notifier_kwargs["github"]
    if commenter is not None:
        assert commenter.calls == []


async def test_github_mention_skipped_for_a_story_with_no_github_link() -> None:
    commenter = _Commenter()
    notice = NeedsHumanNotice(**{**_NOTICE.__dict__, "github_ref": None})

    problems = await Notifier(
        desktop_toast=False, github_login="dave", github=commenter
    ).needs_human(notice)

    assert problems == []
    assert commenter.calls == []


async def test_github_mention_failure_is_a_problem() -> None:
    commenter = _Commenter(
        raises=GitHubTransportError("https://api.github.com/x", OSError("reset"))
    )

    problems = await Notifier(
        desktop_toast=False, github_login="dave", github=commenter
    ).needs_human(_NOTICE)

    assert len(problems) == 1
    assert problems[0].startswith("github mention failed:")


# ── bounding + ordering ──────────────────────────────────────────────────


async def test_a_hanging_sink_times_out_and_the_others_still_fire(
    monkeypatch: pytest.MonkeyPatch, notify_send_on_path: None
) -> None:
    async def _hang(*args: Any, **kwargs: Any) -> _Proc:
        await asyncio.sleep(10)
        return _Proc()

    monkeypatch.setattr(notifications, "_spawn_exec", _hang)
    commenter = _Commenter()

    problems = await Notifier(
        desktop_toast=True,
        github_login="dave",
        github=commenter,
        sink_timeout_seconds=0.01,
    ).needs_human(_NOTICE)

    assert problems == ["desktop toast timed out after 0s"]
    assert len(commenter.calls) == 1  # the later sink was not skipped


def test_notice_github_ref_prefers_the_issue_then_the_delivered_pr() -> None:
    assert (
        notice_github_ref(
            {
                "github_issue_url": "https://github.com/o/r/issues/1",
                "develop_pr_url": "https://github.com/o/r/pull/2",
            }
        )
        == "https://github.com/o/r/issues/1"
    )
    assert (
        notice_github_ref({"develop_pr_url": "https://github.com/o/r/pull/2"})
        == "https://github.com/o/r/pull/2"
    )
    assert notice_github_ref({}) is None
    assert notice_github_ref(None) is None
    assert notice_github_ref({"github_issue_url": ""}) is None

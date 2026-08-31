"""Push notifications for loom-raised ``human`` gates (b91177d2, design D6).

Every surface a needs-human gate reaches — Obsidian ``tasks.md``, lens's Gates
section, ``lithos-loom gates``, the ``[NeedsHuman]`` finding — is *pull*: the
operator has to look, which is exactly the failure mode the escalation
convention exists to end ("every rescue caught because Dave looked"). This
module is the *push* half. It is deliberately **not** event-driven: the code
that raises a gate knows the moment it does, so it calls
:meth:`Notifier.needs_human` right after ``create_human_gate`` succeeds — the
route-runner for a failed run, the github-watcher for a stranded PR. One shot
per gate; lens's 24h attention rule is the reminder.

Three sinks today, each one function, configured by the ``[notifications]``
section (:class:`~lithos_loom.config.NotificationsConfig`):

- **desktop toast** — ``notify-send`` on the daemon's host, on by default,
  skipped silently when the binary is not on PATH;
- **GitHub mention** — an ``@<operator_github_login>`` comment on the story's
  linked issue or delivered PR, so GitHub's native notifications fire (the
  same channel #113 uses for a delivered PR); only stories carrying a GitHub
  link reach it;
- **operator command** — ``on_needs_human``, run through the shell with the
  notice as JSON on stdin: the transport-agnostic escape hatch (``curl`` to
  ntfy / Pushover / Slack, ``mail``, …).

Every sink is best-effort and bounded: a failure or timeout becomes a problem
string the caller folds into the ``[NeedsHuman]`` finding; nothing here ever
raises into the escalation path. Later sinks (Discord, Twilio, …) are one
config key + one sink function each.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from lithos_loom.github_client import parse_github_ref

__all__ = ["GitHubCommenter", "NeedsHumanNotice", "Notifier"]

logger = logging.getLogger(__name__)

# Injection seams for tests (monkeypatched in tests/test_notifications.py).
_spawn_exec = asyncio.create_subprocess_exec
_spawn_shell = asyncio.create_subprocess_shell
_which = shutil.which

_NOTIFY_SEND = "notify-send"
_STDERR_TAIL_CHARS = 200


@dataclass(frozen=True)
class NeedsHumanNotice:
    """What every sink renders: the gate, the story, and why loom stopped."""

    gate_id: str
    story_id: str
    story_title: str
    project: str | None
    route: str
    reason: str
    summary: str
    run_id: str | None = None
    github_ref: str | None = None
    """The story's linked GitHub issue or delivered PR url, when it has one —
    the target of the ``@mention`` sink."""

    def as_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @property
    def toast_title(self) -> str:
        return f"Loom needs you: {self.story_title}"

    @property
    def toast_body(self) -> str:
        return f"{self.reason} — {self.summary} (gate {self.gate_id[:8]})"

    def comment_body(self, login: str) -> str:
        return (
            f"@{login} [NeedsHuman] loom stopped on **{self.story_title}** "
            f"(`{self.reason}`): {self.summary}\n\n"
            f"Gate `{self.gate_id}` in Lithos — complete it to re-dispatch the "
            "story (edit the story first if the brief must change); cancel the "
            "story to abandon."
        )


class GitHubCommenter(Protocol):
    """The one GitHub call the mention sink needs (a structural subset of
    :class:`~lithos_loom.github_client.GitHubClient`)."""

    async def create_issue_comment(self, repo: str, number: int, body: str) -> None: ...


_Sink = Callable[[NeedsHumanNotice], Awaitable[str | None]]


@dataclass
class Notifier:
    """The configured sinks, applied in order; see the module docstring.

    Built once per daemon child from :class:`~lithos_loom.config.NotificationsConfig`
    plus the operator's GitHub login and a :class:`GitHubClient` (both optional —
    the mention sink stands down without them) and injected into the callers
    that raise gates, so tests substitute a fake.
    """

    desktop_toast: bool = True
    command: str | None = None
    github_login: str | None = None
    github: GitHubCommenter | None = None
    sink_timeout_seconds: float = 30.0

    async def needs_human(self, notice: NeedsHumanNotice) -> list[str]:
        """Fire every configured sink for *notice*; return the problems.

        Never raises. Each sink is bounded by ``sink_timeout_seconds`` and
        its failure is reported as a string, so the caller can note it on the
        ``[NeedsHuman]`` finding without the escalation itself depending on
        any transport being up.
        """
        problems: list[str] = []
        sinks: tuple[tuple[str, _Sink], ...] = (
            ("desktop toast", self._toast),
            ("notify command", self._command),
            ("github mention", self._github_mention),
        )
        for name, sink in sinks:
            try:
                problem = await asyncio.wait_for(
                    sink(notice), timeout=self.sink_timeout_seconds
                )
            except TimeoutError:
                problem = f"{name} timed out after {self.sink_timeout_seconds:.0f}s"
            except Exception as exc:  # a sink must never break escalation
                logger.exception(
                    "notifications: %s failed for gate %s", name, notice.gate_id
                )
                problem = f"{name} failed: {exc}"
            if problem:
                logger.warning(
                    "notifications: %s for gate %s: %s", name, notice.gate_id, problem
                )
                problems.append(problem)
        return problems

    async def _toast(self, notice: NeedsHumanNotice) -> str | None:
        if not self.desktop_toast:
            return None
        exe = _which(_NOTIFY_SEND)
        if exe is None:
            logger.debug("notifications: %s not on PATH; toast skipped", _NOTIFY_SEND)
            return None
        proc = await _spawn_exec(
            exe,
            "--app-name=lithos-loom",
            "--urgency=critical",
            notice.toast_title,
            notice.toast_body,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            return f"notify-send exited {proc.returncode}: {_tail(stderr)}"
        return None

    async def _command(self, notice: NeedsHumanNotice) -> str | None:
        if not self.command:
            return None
        proc = await _spawn_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate(notice.as_json().encode("utf-8"))
        if proc.returncode != 0:
            return f"on_needs_human command exited {proc.returncode}: {_tail(stderr)}"
        return None

    async def _github_mention(self, notice: NeedsHumanNotice) -> str | None:
        if self.github is None or not self.github_login or not notice.github_ref:
            return None
        ref = parse_github_ref(notice.github_ref)
        if ref is None:
            logger.debug(
                "notifications: story %s github ref %r unparseable; mention skipped",
                notice.story_id,
                notice.github_ref,
            )
            return None
        await self.github.create_issue_comment(
            ref.repo, ref.number, notice.comment_body(self.github_login)
        )
        return None


def _tail(stderr: bytes | None) -> str:
    text = (stderr or b"").decode("utf-8", errors="replace").strip()
    return text[-_STDERR_TAIL_CHARS:] if text else "no stderr"


def notice_github_ref(metadata: Any) -> str | None:
    """The story's GitHub link for the mention sink: its watcher-materialised
    issue first, else the PR a previous delivery opened."""
    if not isinstance(metadata, dict):
        return None
    for key in ("github_issue_url", "develop_pr_url"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None

"""What the stopped run says about itself, rendered fit to publish.

The third piece of ``lithos-loom develop deliver`` (beside
:mod:`cli._deliver_repo` and :mod:`cli._deliver_lithos`): read the run dir's
on-disk contract into :class:`RunFacts`, and compose the PR body from it. Pure
but for the run-dir read, so every rule below has a unit test.

Two of those rules are about **trust**, because this is the one place in the
command where text loom did not author crosses to a world-readable GitHub PR:

* **The handoff is agent-written.** ``handoff/`` is bind-mounted read-write
  into the coder's container, so the file's *contents* and its *type* are the
  agent's to choose. It is read with ``O_NOFOLLOW`` + a regular-file
  ``fstat`` (a symlink must not decide what a host-privileged process opens,
  and a FIFO must not hang the command), bounded, control-stripped, and
  published fenced with its markup defanged.
* **The failure reason is host diagnostics.** ``state.json``'s
  ``failure_reason`` is the first line of the agent CLI's error text, the
  subprocess stderr, or the tail of unparsed agent stdout. The provenance
  contract says the PR must carry *why the run stopped*, so it does — through
  :func:`redact_for_publication`, which takes the urls, paths and
  credential-shaped runs out and caps the rest. The operator's copy on the
  story stays whole.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lithos_loom.cli._deliver_lithos import StoryState
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.plugins.story_develop.pr_delivery import build_pr_body, closes_line

__all__ = [
    "RunFacts",
    "coder_summary",
    "defang_markup",
    "pr_body",
    "provenance_lines",
    "redact_for_publication",
    "reviews_summary",
    "run_facts",
]


@dataclass(frozen=True)
class RunFacts:
    """What the stopped run left on disk, for the PR body and the finding.

    Everything but *branch* and *story_id* is best-effort: ``--branch`` /
    ``--story`` delivers a branch whose run dir was reaped, and a run dir may
    hold a ``state.json`` without the newer fields. Absent facts are omitted
    from the PR body rather than guessed at.
    """

    story_id: str
    branch: str
    run_id: str = ""
    status: str = ""
    failure_reason: str = ""
    rounds: int | None = None
    cost_usd: float | None = None
    test_gate_verdict: str | None = None
    delivered_pr_url: str | None = None
    coder_summary: str = ""
    """The last round's coder handoff ``## Summary`` — what the branch does,
    in the author's own words (bounded + control-stripped: handoffs are
    agent-written)."""
    run_dir: str = ""


def _opt_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def _opt_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# Handoff files are bind-mounted RW into agent containers, so their bodies are
# agent-written: bound the read, strip terminal control bytes, and cap what
# reaches a PR body. (`cli/develop` bounds the same files for the terminal.)
_CODER_DONE_RE = re.compile(r"^round_(\d+)_coder_done\.md$")
_MAX_HANDOFF_BYTES = 1 << 20  # 1 MiB — handoffs are short markdown
_MAX_SUMMARY_CHARS = 600
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SUMMARY_HEADING_RE = re.compile(r"^\s*#{1,6}\s*summary\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")


def _read_regular_file(path: Path, limit: int) -> bytes | None:
    """Read at most *limit* bytes of *path*, or ``None`` if it is not a plain
    file.

    ``O_NOFOLLOW`` plus an ``fstat`` regular-file check on the **opened**
    descriptor, so the type cannot change between the check and the read. That
    one test rejects symlinks, FIFOs, devices and directories at once — a FIFO
    planted in the RW handoff mount would otherwise block the whole command
    (including ``--dry-run``) for ever on a read with no timeout.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        # back to blocking for the read itself: O_NONBLOCK was only there so
        # opening a FIFO with no writer cannot hang before the fstat
        os.set_blocking(fd, True)
        return os.read(fd, limit)
    except OSError:
        return None
    finally:
        os.close(fd)


def coder_summary(handoff_dir: Path) -> str:
    """The last round's coder handoff ``## Summary``, as one bounded line.

    What the branch's author said it does — the PR body's most useful
    sentence, and the one thing a reader cannot reconstruct from the run's
    metadata. Absent / unreadable / summary-less handoffs give ``""``; the
    body then simply omits the line.
    """
    best: tuple[int, Path] | None = None
    try:
        for path in handoff_dir.iterdir():
            m = _CODER_DONE_RE.match(path.name)
            if not m or path.is_symlink():
                # A symlink here is the agent choosing which host file this
                # host-privileged process opens (CWE-59) — the hazard
                # `config.py`'s artifacts_dir note already records for this
                # very directory. Skipped, not followed.
                continue
            if best is None or int(m.group(1)) > best[0]:
                best = (int(m.group(1)), path)
    except OSError:
        return ""
    if best is None:
        return ""
    raw = _read_regular_file(best[1], _MAX_HANDOFF_BYTES)
    if raw is None:
        return ""
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    body: list[str] = []
    collecting = False
    for line in lines:
        if _SUMMARY_HEADING_RE.match(line):
            collecting = True
            continue
        if collecting and _HEADING_RE.match(line):
            break
        if collecting:
            body.append(line)
    summary = " ".join(" ".join(body).split())
    summary = _CONTROL_CHARS_RE.sub("", summary)
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return defang_markup(summary)


# GitHub honours closing keywords anywhere in a PR *description*, and @-names
# notify real people. Agent-written text is quoted into the body, so neutralise
# both before it leaves the host (the fence in `build_pr_body` is the other
# half: keywords and mentions inside a code block are inert).
_CLOSES_RE = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)(\s+|\s*:\s*)(?=#\d|[\w.-]+/[\w.-]+#\d)",
    re.IGNORECASE,
)
_MENTION_RE = re.compile(r"(?<![\w`])@([A-Za-z0-9][-A-Za-z0-9/]*)")
# Backtick runs would break out of the fence that quotes this text.
_FENCE_RE = re.compile(r"`{3,}")


def defang_markup(text: str) -> str:
    """Neutralise the markup GitHub treats as *live* in a PR description.

    Agent-written text (the coder's handoff) and host diagnostics both reach
    the PR body. GitHub closes issues named by a closing keyword anywhere in a
    description and notifies every ``@name``, so a handoff line saying
    ``Closes #1337 cc @org/sec`` would close an unrelated issue on merge and
    ping strangers — under the operator's identity. Rewrite the keyword so it
    reads the same but binds nothing, quote the mention, and defuse backtick
    runs that would escape the fence the body wraps this in.
    """
    out = _CLOSES_RE.sub(lambda m: f"{m.group(1)} → ", text)
    out = _MENTION_RE.sub(r"`@\1`", out)
    return _FENCE_RE.sub("``", out)


def run_facts(run_dir: Path) -> RunFacts:
    """Read a run dir into :class:`RunFacts` (pure, tolerant of every absence).

    ``state.json`` carries the verdict, branch and round count; the run's
    ``result.json`` carries the ``escalation`` block the runner built its
    needs-human gate from — cost, test-gate verdict — bound to THIS run by
    ``run_id`` so a prior run's leftover is never read as this one's.
    """
    state = run_outcome.read_state(run_dir) or {}
    result = run_outcome.result_for_run(run_dir) or {}
    escalation = result.get("escalation")
    brief = escalation.get("brief") if isinstance(escalation, Mapping) else None
    brief = brief if isinstance(brief, Mapping) else {}
    return RunFacts(
        story_id=run_dir.parent.name,
        branch=_opt_str(state.get("branch")),
        run_id=_opt_str(state.get("run_id")) or run_dir.name,
        status=_opt_str(state.get("status")),
        failure_reason=_opt_str(state.get("failure_reason")),
        rounds=_opt_int(state.get("rounds")),
        cost_usd=_opt_float(brief.get("cost_usd")),
        test_gate_verdict=_opt_str(brief.get("test_gate_verdict")) or None,
        delivered_pr_url=run_outcome.delivered_pr_url(run_dir, state),
        coder_summary=coder_summary(run_dir / "handoff"),
        run_dir=str(run_dir),
    )


# Host/infra text that must never be published verbatim: absolute and home
# paths, urls (provider endpoints, proxies), and long opaque runs that look
# like credentials. `failure_reason` is built from the agent CLI's error text,
# the subprocess stderr, or the tail of unparsed agent stdout, so any of these
# can be in it — and a PR body is world-readable.
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_HOME_PATH_RE = re.compile(r"~[\w.-]*(?:/[^\s,;)'\"]*)+")
_ABS_PATH_RE = re.compile(r"(?<![\w/])/(?:[\w.@+-]+)(?:/[^\s,;)'\"]*)+")
_SECRETISH_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_MAX_REASON_CHARS = 200


def redact_for_publication(text: str) -> str:
    """A bounded, markup-inert rendering of host diagnostic text.

    The PR must say **why** the run stopped (the provenance contract), and the
    raw string must not be published as-is. So the structure survives and the
    parts that leak the host do not: urls, absolute / home paths and
    credential-shaped runs become placeholders, markup is defanged, and the
    result is capped. The operator still reads the untouched original on the
    story's ``[NeedsHuman]`` finding, in the gate brief and in ``--dry-run``.
    """
    out = " ".join(text.split())
    out = _URL_RE.sub("<url>", out)
    out = _HOME_PATH_RE.sub("<path>", out)
    out = _ABS_PATH_RE.sub("<path>", out)
    out = _SECRETISH_RE.sub("<redacted>", out)
    out = defang_markup(out)
    if len(out) > _MAX_REASON_CHARS:
        out = out[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    return out


def provenance_lines(facts: RunFacts) -> list[str]:
    """The PR body's ``## Provenance`` block: where this branch came from.

    Carries **why the run stopped** — its status plus its reason — but the
    reason goes through :func:`redact_for_publication` first: it is not a
    curated label (agent error text / stderr / unparsed stdout), and a PR body
    is world-readable. The operator's copy stays whole on the story.
    """
    lines = [
        "delivered by hand with `lithos-loom develop deliver` — the run that "
        "wrote this branch stopped before it could open a PR"
    ]
    if facts.run_id:
        stop = f"run `{facts.run_id}`"
        if facts.status:
            stop += f" stopped `{facts.status}`"
        reason = redact_for_publication(facts.failure_reason)
        if reason:
            stop += f": {reason}"
        stop += " (the story carries the full, unredacted reason)"
        lines.append(stop)
    lines.append(f"branch `{facts.branch}`")
    return lines


def reviews_summary(facts: RunFacts) -> str:
    """The Review section's verdict line: this branch was NOT panel-approved."""
    if facts.status and facts.status != run_outcome.APPROVED:
        return (
            f"not approved — the run stopped `{facts.status}` before the panel "
            "agreed; review this PR as you would any other"
        )
    return "not recorded — delivered by hand from a stopped run"


def pr_body(*, facts: RunFacts, story: StoryState, repo_name: str) -> str:
    """The generated body for a newly opened PR — the shared builder plus this
    delivery's provenance. Built lazily: an adopted PR needs none."""
    return build_pr_body(
        description=story.task_text,
        acceptance_criteria=story.acceptance_criteria,
        reviews_summary=reviews_summary(facts),
        rounds=facts.rounds,
        gate_verdict=facts.test_gate_verdict,
        cost_usd=facts.cost_usd,
        task_id=story.story_id,
        issue_closes=closes_line(story.github_issue_url, repo_name),
        provenance=provenance_lines(facts),
        # fenced, never inline: the handoff is written by the coder agent into
        # a RW mount, and a PR description is live markup (sec review f-006)
        provenance_quote=facts.coder_summary,
    )

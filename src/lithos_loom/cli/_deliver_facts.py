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

import math
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
    "sanitize_for_terminal",
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


def _opt_rounds(value: Any) -> int | None:
    """A round COUNT: a non-negative int, else unknown.

    Type-correct is not enough — ``-3`` would be published as a real negative
    round count. A value outside the domain is a malformed / partial
    ``state.json``, and "unknown" is the only honest rendering of it."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _opt_cost(value: Any) -> float | None:
    """A spend: a finite, non-negative number, else unknown.

    ``json.loads`` accepts ``NaN`` / ``Infinity``, so a malformed brief can
    otherwise reach the PR body as ``$nan`` / ``$inf`` — type-correct and
    meaningless. An arbitrary-precision **int** (``escalation.brief`` is
    free-form, and ``10**400`` is valid JSON) is outside the float domain
    altogether: ``float()`` raises rather than returning ``inf``, so the
    conversion is guarded — an unpublishable number is unknown, never a crash
    in the middle of resolving the facts."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _opt_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# Handoff files are bind-mounted RW into agent containers, so their bodies are
# agent-written: bound the read, strip terminal control bytes, and cap what
# reaches a PR body. (`cli/develop` bounds the same files for the terminal.)
_CODER_DONE_RE = re.compile(r"^round_(\d+)_coder_done\.md$")
_MAX_HANDOFF_BYTES = 1 << 20  # 1 MiB — handoffs are short markdown
_MAX_SUMMARY_CHARS = 600
# C0 / C1 *and* the Unicode characters that reorder or hide text without
# being control codes: bidi overrides + isolates (trojan source — GitHub warns
# about it in diffs) and the zero-width / invisible formatters. The hazard is
# the one this module already accepts for ANSI escapes: `--dry-run` is the
# screen the operator decides on, and a PR body is read by strangers, so a
# line must not be able to render differently from the text it carries.
_CONTROL_CHARS_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    "\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
_SUMMARY_HEADING_RE = re.compile(r"^\s*#{1,6}\s*summary\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")


def sanitize_for_terminal(text: str) -> str:
    """Strip terminal control / escape bytes (keeping TAB + LF) from text
    before it is echoed to the operator's terminal.

    The one helper for every ``develop`` surface that prints text loom did not
    author — a stop reason built from agent stdout / stderr, a story title a
    GitHub issue supplied, the ``gh`` stderr inside an exception string. None
    of those strippers may be skipped on a screen the operator uses to DECIDE:
    an ANSI escape can forge a "push: REFUSE" line, erase it, or retitle the
    window (CWE-117 / CWE-150).
    """
    return _CONTROL_CHARS_RE.sub("", text)


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
    # The same treatment the stop reason gets, at this section's own cap: the
    # handoff is agent-chosen text on its way to a world-readable PR body, so
    # host paths, urls and credential-shaped runs must not ride along. The
    # fence `pr_body` puts around it neutralises markup, never content.
    return redact_for_publication(" ".join(body), limit=_MAX_SUMMARY_CHARS)


# GitHub honours closing keywords anywhere in a PR *description*, and @-names
# notify real people. Agent-written text is quoted into the body, so neutralise
# both before it leaves the host (the fence in `build_pr_body` is the other
# half: keywords and mentions inside a code block are inert).
# `GH-123` is a closing ref exactly like `#123` (GitHub's documented
# `KEYWORD GH-ISSUE-NUMBER` form), so the lookahead admits it too.
_CLOSES_RE = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)(\s+|\s*:\s*)"
    r"(?=#\d|GH-\d|[\w.-]+/[\w.-]+#\d)",
    re.IGNORECASE,
)
# No `\`` in the lookbehind: a preceding backtick does NOT make a mention
# inert (a single unmatched backtick opens no code span in GFM), so exempting
# one would hand the author a one-character bypass.
_MENTION_RE = re.compile(r"(?<!\w)@([A-Za-z0-9][-A-Za-z0-9/]*)")
# Backtick runs would break out of the fence that quotes this text.
_FENCE_RE = re.compile(r"`{3,}")
# GitHub renders inline HTML in a PR description, so `<img src=…>` is a live
# off-site request (a tracking beacon on every viewer). Only a `<` that starts
# a tag is escaped — `a < b` stays readable.
_HTML_OPEN_RE = re.compile(r"<(?=[A-Za-z/!?])")
# `[text](target)` is a live link. Break the syntax at the bracket.
_LINK_RE = re.compile(r"\[(?=[^\]]*\]\()")


def defang_markup(text: str) -> str:
    """Neutralise the markup GitHub treats as *live* in a PR description.

    This is the SOLE defence on the unfenced provenance bullet (the redacted
    stop reason) and belt-and-braces behind the fence that quotes the coder's
    handoff, so it must stand alone: GitHub closes issues named by a closing
    keyword anywhere in a description, notifies every ``@name``, renders inline
    HTML, and follows markdown links — a line saying
    ``Closes #1337 cc @org/sec <img src=//evil.example/p.png>`` would close an
    unrelated issue on merge, ping strangers and fire an off-site request for
    every viewer, all under the operator's identity.

    Each construct is rewritten so it still READS the same and binds nothing:
    the keyword keeps its word, the mention's ``@`` becomes the entity that
    renders as one and notifies nobody, a tag-opening ``<`` and a link's ``[``
    are HTML-escaped, and backtick runs that would escape the fence are
    defused. **Nothing here leans on code spans**: quoting a mention in
    backticks only works while the backticks pair up, and the author of this
    text chooses how many of those it contains.

    Control bytes go first — C0/C1 *and* the bidi / zero-width formatters
    (:data:`_CONTROL_CHARS_RE`) — because a construct that renders in a
    different order than it was written defeats every rewrite below it.
    """
    out = _CONTROL_CHARS_RE.sub("", text)
    out = _CLOSES_RE.sub(lambda m: f"{m.group(1)} → ", out)
    out = _MENTION_RE.sub(r"&#64;\1", out)
    out = _HTML_OPEN_RE.sub("&lt;", out)
    out = _LINK_RE.sub("&#91;", out)
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
        rounds=_opt_rounds(state.get("rounds")),
        cost_usd=_opt_cost(brief.get("cost_usd")),
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
# A **protocol-relative** `//host/path` is as live as `https://host/path` and
# neither `_ABS_PATH_RE` (its lookbehind cannot match a second `/`) nor a
# scheme-anchored pattern would catch it.
# A **protocol-relative** `//host/path` is as live as `https://host/path`, and
# GFM autolinks a bare `www.` host — so an unredacted `www.evil.example/beacon`
# would render as the very clickable link `_LINK_RE` exists to prevent.
_URL_RE = re.compile(
    r"(?:\b[a-z][a-z0-9+.-]*://|(?<![\w:/])//|\bwww\.)\S+", re.IGNORECASE
)
# Internal names leak topology without looking like a url or a path.
_INTERNAL_HOST_RE = re.compile(
    r"\b[\w-]+(?:\.[\w-]+)*\.(?:internal|local|corp|lan|intranet)\b(?::\d+)?",
    re.IGNORECASE,
)
# The two host shapes agent / CLI stderr produces after a hostname: a literal
# IPv4 (a bare RFC1918 address is pure host topology) and a bare `host:port`.
# The `host:port` rule requires a `.` or `-` in the name so ordinary text like
# `Error:404` is left alone.
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b(?::\d{1,5}\b)?")
_HOST_PORT_RE = re.compile(r"\b(?=[\w.-]*[.-])[a-z0-9][\w.-]*:\d{2,5}\b", re.IGNORECASE)
_HOME_PATH_RE = re.compile(r"~[\w.-]*(?:/[^\s,;)'\"]*)+")
_ABS_PATH_RE = re.compile(r"(?<![\w/])/(?:[\w.@+-]+)(?:/[^\s,;)'\"]*)+")
_SECRETISH_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_MAX_REASON_CHARS = 200
# How much text the substitution pass is allowed to SEE, as a multiple of the
# caller's output cap. Several patterns above scan a `[\w.-]` run from every
# start position inside it, so their cost is quadratic in the input — and one
# caller's input is an agent-written handoff bounded only by 1 MiB, which at
# that shape spins for hours (a `--dry-run` that never returns). Capping the
# OUTPUT does not help: the cap is applied after the scan. So the input is
# bounded here, once, for every caller — generously, so that what survives
# still reads like the original and a credential-shaped run is never split
# below `_SECRETISH_RE`'s 24-character floor.
_REDACT_INPUT_SLACK = 8

# Written as prose, NOT as `<url>`: `url`, `host`, `path` and `redacted` are
# all valid HTML tag names, so an angle-bracketed placeholder is parsed as raw
# inline HTML and dropped by GitHub's sanitizer — the redaction would then be
# invisible on the one surface people read, and a reader could not tell a
# redacted reason from a truncated or empty one.
_URL_PLACEHOLDER = "(url redacted)"
_HOST_PLACEHOLDER = "(host redacted)"
_PATH_PLACEHOLDER = "(path redacted)"
_REDACTED_PLACEHOLDER = "(redacted)"


def redact_for_publication(text: str, *, limit: int = _MAX_REASON_CHARS) -> str:
    """A bounded, markup-inert rendering of host text that is about to be
    published.

    The PR must say **why** the run stopped (the provenance contract), and the
    raw string must not be published as-is. So the structure survives and the
    parts that leak the host do not: urls (scheme-ful, protocol-relative and
    ``www.``-autolinked), internal hostnames, IPv4 literals and bare
    ``host:port`` pairs, absolute / home paths and credential-shaped runs
    become placeholders, markup is defanged, and the result is capped at
    *limit* — which also bounds the text the patterns ever see
    (:data:`_REDACT_INPUT_SLACK`), since several of them cost O(n²) on a long
    dotted run and one caller's input is an agent-written file. The operator
    still reads the untouched original on the story's ``[NeedsHuman]``
    finding, in the gate brief and in ``--dry-run``.

    Every string this module publishes goes through it — the stop reason
    **and** the coder's handoff summary. The handoff is no less host-derived
    than the reason (the agent quotes the commands it ran, and its own inputs
    reach it from a public GitHub issue), and the fence around it neutralises
    markup, not content.
    """
    # Bound the INPUT before any pattern runs (see `_REDACT_INPUT_SLACK`):
    # only the first `limit` characters can survive the cap below, and the
    # slack leaves room for the substitutions to shorten the text first.
    out = defang_markup(" ".join(text.split())[: limit * _REDACT_INPUT_SLACK])
    out = _URL_RE.sub(_URL_PLACEHOLDER, out)
    out = _INTERNAL_HOST_RE.sub(_HOST_PLACEHOLDER, out)
    out = _IPV4_RE.sub(_HOST_PLACEHOLDER, out)
    out = _HOST_PORT_RE.sub(_HOST_PLACEHOLDER, out)
    out = _HOME_PATH_RE.sub(_PATH_PLACEHOLDER, out)
    out = _ABS_PATH_RE.sub(_PATH_PLACEHOLDER, out)
    out = _SECRETISH_RE.sub(_REDACTED_PLACEHOLDER, out)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
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

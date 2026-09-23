"""Tests for PR delivery + the Copilot review round (T9).

Pure builders are tested directly; ``deliver()`` is exercised with every
gh/git wrapper and the container/turn machinery monkeypatched — no network,
no Docker.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from lithos_loom.github_client import GitHubError, GitHubTransportError
from lithos_loom.plugins.story_develop import pr_delivery
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.develop import DevelopResult, ReviewOutcome
from lithos_loom.plugins.story_develop.pr_delivery import (
    AUTOMATED_MARKER,
    build_pr_body,
    closes_line,
    deliver,
    parse_issue_ref,
    pr_number_from_url,
    reply_body,
)
from lithos_loom.plugins.story_develop.publish_text import (
    CONTROL_CHARS_RE,
    MAX_SECTION_CHARS,
    MAX_TITLE_CHARS,
    MIN_SECTION_CHARS,
    defang_markup,
    fence_untrusted,
    publish_title,
)

# --- pure builders --------------------------------------------------------------


def test_parse_issue_ref() -> None:
    assert parse_issue_ref("https://github.com/o/r/issues/42") == ("o/r", 42)
    assert parse_issue_ref("https://github.com/o/r/issues/42/") == ("o/r", 42)
    assert parse_issue_ref("https://github.com/o/r/pull/42") is None
    assert parse_issue_ref("not a url") is None


def test_closes_line_same_and_cross_repo() -> None:
    assert closes_line("https://github.com/o/r/issues/7", "o/r") == "Closes #7"
    assert closes_line("https://github.com/O/R/issues/7", "o/r") == "Closes #7"
    assert (
        closes_line("https://github.com/other/repo/issues/7", "o/r")
        == "Closes other/repo#7"
    )
    assert closes_line(None, "o/r") == ""
    assert closes_line("garbage", "o/r") == ""


def test_build_pr_body_contents() -> None:
    body = build_pr_body(
        description="Add a flag\n\nDetails.",
        acceptance_criteria="1. works",
        reviews_summary="[cq]=LGTM",
        rounds=2,
        gate_verdict="GREEN",
        cost_usd=1.234,
        task_id="task-9",
        issue_closes="Closes #7",
    )
    assert "Closes #7" in body
    assert "## Acceptance criteria" in body and "1. works" in body
    assert "[cq]=LGTM" in body and "rounds: 2" in body
    assert "test gate: GREEN" in body
    assert "$1.23" in body
    assert "Lithos task: `task-9`" in body
    assert "squash-merge" in body


def test_build_pr_body_minimal() -> None:
    body = build_pr_body(
        description="x",
        acceptance_criteria=None,
        reviews_summary="[cq]=LGTM",
        rounds=1,
        gate_verdict=None,
        cost_usd=0.5,
        task_id=None,
        issue_closes="",
    )
    assert "Acceptance criteria" not in body
    assert "Closes" not in body
    assert "Lithos task" not in body


_FENCE_LINE_RE = re.compile(r"(`{3,}|~{3,})")


def _interior(quoted: str) -> str:
    """The quoted content of a `fence_untrusted` block — what *limit* bounds
    (the fence's own two lines are loom's markup, not the author's)."""
    return "\n".join(quoted.splitlines()[1:-1])


def _outside_fences(body: str) -> str:
    """The part of *body* GitHub renders as live markup.

    GFM: a fenced block opens on a run of three-or-more backticks **or** tildes
    indented at most three spaces, and closes on a run of the same character at
    least as long with nothing else on the line. Everything between is inert —
    which is exactly the claim the untrusted sections rest on, so the tests
    check it the way the renderer does rather than by looking for a literal
    ``` `` ` ``` ``` `` `.
    """
    live: list[str] = []
    fence = ""
    for line in body.splitlines():
        stripped = line.lstrip(" ")
        indented = len(line) - len(stripped) > 3
        match = None if indented else _FENCE_LINE_RE.match(stripped)
        run = match.group(1) if match else ""
        if fence:
            closes = (
                run
                and run[0] == fence[0]
                and len(run) >= len(fence)
                and not stripped[len(run) :].strip()
            )
            if closes:
                fence = ""
            continue
        if run:
            fence = run
            continue
        live.append(line)
    return "\n".join(live)


# A GitHub issue body is arbitrary markdown, so the fixture carries one of
# every live construct the reviews named: the keyword forms (plain, `GH-`,
# cross-repo, full-url, emphasised, `&nbsp;`-separated, reference-wrapped), a
# mention behind a lone backtick, inline HTML, inline / nested / reference /
# shortcut links and a reference IMAGE with its definitions, both fence
# characters, and two invisibles.
_HOSTILE_ISSUE_BODY = (
    "Please fix the parser.\n\n"
    "<!-- Closes #1 -->\n"
    "Closes GH-2 and fixes agent-lore/other#3\n"
    "Closes https://github.com/agent-lore/lithos-loom/issues/5\n"
    "**Closes** #6, Closes&nbsp;#8, and Closes [#9][r]\n"
    "cc @agent-lore/security and `@evil-user\n"
    "<img src=//evil.example/p.png>\n"
    "see [details](//evil.example/x) and [outer [inner]](//evil.example/y)\n"
    "[click here][a], ![beacon][b], [evil.example][]\n\n"
    "[a]: https://evil.example/phish\n"
    "> [c]: https://evil.example/quoted\n"
    "- [d\\]e]: https://evil.example/escaped\n"
    "[b]: https://evil.example/beacon.png\n"
    "[evil.example]: https://evil.example\n"
    "[r]: https://github.com/o/r/issues/9\n"
    "```\nnot a fence escape\n```\n"
    "reorder\u202ethis, smuggle\U000e0001d, so\u00adft\n"
    "~~~"
)


def _hostile_body(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "description": _HOSTILE_ISSUE_BODY,
        "acceptance_criteria": "1. parses. Resolves #4 — ask @octocat",
        "reviews_summary": "[cq]=LGTM",
        "rounds": 1,
        "gate_verdict": "GREEN",
        "cost_usd": 0.5,
        "task_id": "task-9",
        "issue_closes": "Closes #7",
    }
    kwargs.update(overrides)
    return build_pr_body(**kwargs)  # type: ignore[arg-type]


def test_build_pr_body_defangs_the_story_text_a_stranger_wrote() -> None:
    """security/f-004: for a story the github-issue watcher materialised, the
    description IS the external issue body (and the acceptance criteria can be
    too), so the body must publish neither as live markup under the operator's
    `gh` identity: a hidden `Closes #1` would close an unrelated issue on
    merge, an @mention pings real people, an HTML comment hides text and an
    `<img>` — or a reference image, which needs no `<` — is an off-site
    request for every viewer."""
    body = _hostile_body()

    # the text still READS as written…
    assert "Please fix the parser." in body and "1. parses." in body
    # …but nothing in it binds: no closing keyword survives next to its ref,
    # in any of its forms, in either section
    for live in (
        "Closes #1",
        "Closes GH-2",
        "fixes agent-lore/other#3",
        "Closes https://github.com/agent-lore/lithos-loom/issues/5",
        "Closes** #6",
        "Closes&nbsp;#8",
        "Closes [#9][r]",
        "Resolves #4",
    ):
        assert live not in body
    assert "#1" in body and "GH-2" in body  # the refs still read
    # …nobody is notified (a stray backtick is no exemption)…
    assert "@agent-lore/security" not in body and "&#64;agent-lore/security" in body
    assert "@evil-user" not in body and "@octocat" not in body
    # …no tag opens (the comment that hides text, the beacon that fires)…
    assert "<!--" not in body and "&lt;!--" in body
    assert "<img" not in body and "&lt;img" in body
    # …no link binds, in any form: inline, nested-label, and the reference
    # definitions every `[label][ref]` / `[ref][]` / `[ref]` resolves through
    # (broken, so each one degrades to the literal text of its label)…
    assert "[details](" not in body and "[details]&#40;" in body
    assert "[outer [inner]](" not in body and "[outer [inner]]&#40;" in body
    # …including the two spellings no label pattern reached: a definition
    # inside a container block (definitions are collected document-globally)
    # and a backslash-escaped `]` in the label
    for definition in ("[a]", "[b]", "[evil.example]", "[r]", "[c]", "[d\\]e]"):
        assert f"{definition}: " not in body
        assert f"{definition}&#58; " in body
    # …and no formatter reorders or hides what the reader sees
    assert "\u202e" not in body and "reorderthis" in body
    assert "\U000e0001" not in body and "smuggled" in body
    assert "\u00ad" not in body and "soft" in body
    # the one live closing keyword is the one loom composed itself
    assert "\nCloses #7\n" in body


def test_the_story_text_cannot_reach_out_of_its_fence() -> None:
    """correctness/f-001 + security/f-002: defanging enumerates GFM and GFM
    keeps growing (`~~~` opens a fence exactly like ``` `` ` ``` `` ` `` does,
    reference links need no `](`). So the untrusted sections are FENCED, with a
    fence measured against their own content — and everything loom composes
    around them stays live markup, above all the `Closes #7` that makes the
    delivered PR close its source issue on merge."""
    live = _outside_fences(_hostile_body())

    # loom's own sections are outside every fence — they still render, and the
    # closing keyword still binds
    assert "Closes #7" in live
    assert "## What" in live and "## Acceptance criteria" in live
    assert "- verdicts: [cq]=LGTM" in live and "- test gate: GREEN" in live
    assert "- Lithos task: `task-9`" in live
    # …while not one line the reporter wrote is
    for quoted in ("Please fix the parser.", "not a fence escape", "1. parses."):
        assert quoted not in live


def test_a_trailing_tilde_fence_cannot_swallow_the_rest_of_the_body() -> None:
    """security/f-002, minimal shape: an issue body that ends on `~~~` opened a
    code block that ran to the end of the document — taking loom's `Closes #N`,
    the panel verdicts and the test-gate result into it."""
    live = _outside_fences(
        _hostile_body(description="ok\n\n~~~", acceptance_criteria="```\nand")
    )

    assert "Closes #7" in live
    assert "- verdicts: [cq]=LGTM" in live and "- test gate: GREEN" in live


def test_fence_untrusted_cannot_be_closed_by_its_own_content() -> None:
    """The fence is measured against the text it quotes, so no line of that
    text can close it early and resume live markup."""
    quoted = fence_untrusted("try\n``````\nto escape\n~~~~~~\nor this")

    assert quoted.startswith("```")
    assert "try" in quoted and "to escape" in quoted
    assert _outside_fences(quoted) == ""  # every line of it is inert
    assert fence_untrusted("   ") == ""  # nothing to quote, no empty block


def test_an_untrusted_section_is_bounded_before_the_rewrites_run() -> None:
    """security/f-007: the reporter picks the length of their issue body, so
    they must not also pick how much regex work the host does on it — the
    input is cut before a single pattern runs, as `redact_for_publication`
    already cuts its own. 64 KiB of `[]` (GitHub's issue-body ceiling, and the
    shape that made a label-scanning lookahead quadratic) cost ~2s of delivery
    CPU per section; the cut plus a fixed-width pattern make it milliseconds."""
    for payload in (
        "[]" * 32768,
        # …and the shape the budget passes themselves invite: a run of spaces
        # that never reaches a line end, which the trailing-whitespace strip
        # rescans from every position in — quadratic (0.25 s at 10 KiB, 4x per
        # doubling) unless the run collapse has already capped it
        " " * 100_000 + "still here",
    ):
        start = time.perf_counter()
        quoted = fence_untrusted(payload)
        elapsed = time.perf_counter() - start

        assert elapsed < 1.0
        assert len(quoted) < MAX_SECTION_CHARS + 200


def test_padding_cannot_spend_the_whole_input_budget() -> None:
    """security/f-009 + f-010: the budget buys characters the published section
    can actually carry. What this pipeline drops or collapses anyway — the
    invisibles and a fence run — must be taken out BEFORE the input cut, or
    padding with it pushes every visible word past the slice and the section
    is published as nothing but the truncation note.

    Whitespace is NOT in that set and has its own test below: a fenced block
    carries it, so it is content, and padding with it earns an honest
    truncation rather than a silent reflow.
    """
    story = "The parser crashes on empty input."
    wall = MAX_SECTION_CHARS * 4
    for filler in (
        "\u200b" * wall,  # zero-widths, the tag block, a soft hyphen, a filler
        "\U000e0041" * wall,
        "\u00ad" * wall,
        "\u3164" * wall,
        "\u061c" * wall,  # …and the blocks the hand-written ranges had missed
        "\U000e0101" * wall,
        "`" * wall + "\n",
    ):
        quoted = fence_untrusted(filler + story)

        assert story in quoted
        assert "(truncated" not in quoted  # nothing the section carries was lost
        assert len(_interior(quoted)) < 100  # …and the padding is not it


def test_in_budget_whitespace_is_published_exactly_as_written() -> None:
    """[davesnowdon] Medium: a fenced code block preserves every space, tab and
    blank line in it, so the story's whitespace is CONTENT — deeply indented
    code, an exact-output fixture, an ASCII diagram, a whitespace-sensitive
    example. Silently capping space runs at 40, rewriting a mixed space/tab run
    as repetitions of its first character, stripping every line's trailing
    whitespace and cutting newline runs to three altered technical
    requirements while still presenting them as complete — and with no
    truncation marker to say anything had been touched."""
    fixture = (
        "Expected output:\n"
        + " " * 48  # deeper than any cap: the reporter's real indentation
        + "child\n"
        + "\t" * 12  # …and a mixed run, which the collapse rewrote to one char
        + " \t mixed\n"
        + "trailing   \n"  # …trailing whitespace, significant in a fixture
        + "\n" * 6  # …and more blank lines than the collapse allowed
        + "end"
    )

    assert _interior(fence_untrusted(fixture)) == fixture
    # the whole story still travels — nothing was lost, so nothing claims to be
    assert "(truncated" not in fence_untrusted(fixture)


def test_whitespace_padding_truncates_honestly_rather_than_silently() -> None:
    """[davesnowdon] Medium, the other half: whitespace is not free, so a
    reporter CAN still spend a section's budget on it — but the outcome is the
    explicit marker, never a section quietly squeezed to fit."""
    quoted = fence_untrusted(" " * (MAX_SECTION_CHARS * 4) + "story")

    assert "(truncated" in quoted
    assert len(_interior(quoted)) <= MAX_SECTION_CHARS


def test_the_stripped_class_tracks_the_unicode_property() -> None:
    """[davesnowdon] Low: the class is measured against Unicode's
    Default_Ignorable_Code_Point property rather than hand-listed, so it does
    not drift from it. U+061C (a bidi formatter, which also changes bidi
    rendering), U+2065, the deprecated U+206A–U+206F, U+FFF0–U+FFF8 and every
    tag / variation selector past U+E007F all survived the earlier ranges into
    a published body — and, since the budget is counted AFTER the strip, each
    was also a character an external author could pad a section down to its
    truncation marker with."""
    for missed in (
        "\u061c",
        "\u2065",
        "\u206a",
        "\u206f",
        "\ufff0",
        "\ufff8",
        "\U000e0101",
        "\U000e0fff",
        "\U0001bca0",
        "\U0001d173",
    ):
        assert CONTROL_CHARS_RE.sub("", missed) == ""
        assert defang_markup(f"vis{missed}ible") == "visible"

    # …and the only two whitespace characters a PR body renders stay untouched
    assert defang_markup("a\tb\nc") == "a\tb\nc"


def test_whitespace_at_a_section_boundary_is_published_too() -> None:
    """correctness/f-001: the contract is that whitespace is published exactly
    as written, and a section's EDGES are part of it. `.strip()` on the body
    was the last place this function still normalised silently — acceptance
    criteria that open on an indented exact-output line were published
    un-indented, and a final line's significant trailing spaces vanished, both
    with no truncation marker to say anything had gone."""
    for fixture in (
        "   expected   ",  # the reviewer's own repro
        # an exact-output fixture that OPENS on its indented line
        "    $ loom run --flag\n    ok\nthat is the whole output",
        "run it and compare:\n    exact   ",  # trailing spaces on the last line
        "\n\n  leading blank lines are a paragraph break\n\n",
        "\ttab-indented first line",
    ):
        assert _interior(fence_untrusted(fixture)) == fixture
        assert "(truncated" not in fence_untrusted(fixture)


def test_a_section_of_nothing_but_whitespace_is_still_no_section() -> None:
    """…while emptiness stays a question about the CONTENT: text that carries
    nothing publishes no fenced block at all, rather than an empty one."""
    for blank in ("", "   ", "\n\n", " \t \n \t "):
        assert fence_untrusted(blank) == ""


def test_fence_untrusted_publishes_no_more_than_its_limit() -> None:
    """correctness/f-002: *limit* is a cap on the quoted content, marker
    included — appending the note after the cut would publish 53 characters
    more than the caller asked for, and a cap with no room for the note (or a
    negative one, which Python's slicing reads as "keep nearly everything") is
    a bug in the caller, not a section to publish anyway."""
    for limit in (MIN_SECTION_CHARS, 100, MAX_SECTION_CHARS):
        interior = _interior(fence_untrusted("x" * (limit * 4), limit=limit))
        assert len(interior) <= limit
        assert interior.endswith("(truncated — the whole text is on the Lithos story)")
        assert interior.startswith("x")  # …and text still travels beside it

    # the default cap, exactly at and one past the boundary
    exact = _interior(fence_untrusted("x" * MAX_SECTION_CHARS))
    assert len(exact) == MAX_SECTION_CHARS and "(truncated" not in exact
    over = _interior(fence_untrusted("x" * (MAX_SECTION_CHARS + 1)))
    assert len(over) <= MAX_SECTION_CHARS and "(truncated" in over

    for unusable in (0, -1, -MAX_SECTION_CHARS, MIN_SECTION_CHARS - 1):
        with pytest.raises(ValueError, match="at least"):
            fence_untrusted("x" * 500, limit=unusable)


def test_publish_title_breaks_the_keyword_that_reaches_the_commit_subject() -> None:
    """security/f-001: the story's title is the external issue's title for a
    mirrored story, and with the repo default squash-merge message a
    multi-commit PR (loom's always are) takes its COMMIT SUBJECT from the PR
    title. GitHub honours closing keywords in commit messages on the default
    branch — a raw-text channel no fence reaches — so `Closes #1 parser
    crashes` would close issue #1 under the operator's account on merge."""
    for live, dead in (
        ("Closes #1 fix the parser", "Closes → #1 fix the parser"),
        ("Fixes GH-9 in the lexer", "Fixes → GH-9 in the lexer"),
        ("resolve agent-lore/other#3", "resolve → agent-lore/other#3"),
        (
            "Closes https://github.com/o/r/issues/5",
            "Closes → https://github.com/o/r/issues/5",
        ),
    ):
        assert publish_title(live) == dead

    # …nobody is notified either: a commit message DOES link and notify a
    # mention. Broken with a space, not an entity — a title is plain text, so
    # `&#64;` would be published literally instead of rendering as `@`.
    assert publish_title("crash, cc @agent-lore/security") == (
        "crash, cc @ agent-lore/security"
    )
    assert "&#" not in publish_title("<img> & [a](b) stay readable")

    # …and no formatter reorders the PR list, the notification email or the
    # commit subject
    assert publish_title("re\u202eorder me") == "reorder me"
    assert publish_title("smuggle\U000e0001d") == "smuggled"

    # the ordinary rules survive: first line only, trimmed, capped
    assert publish_title("  Add a flag\n\nDetails.  ") == "Add a flag"
    assert publish_title("") == ""  # the caller's cue to fall back
    assert len(publish_title("x" * 200)) == MAX_TITLE_CHARS


def test_delivery_hands_gh_a_title_that_binds_nothing(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    """security/f-001, the daemon half: `deliver()` takes the PR title straight
    off the task text, whose first line is the mirrored issue's title. The
    suite only ever asserted about `build_pr_body`'s return value, so this
    channel had no coverage at all."""
    mirrored = replace(
        config, description="Closes #1 parser crashes on empty input\n\nDetails."
    )
    state = _install(monkeypatch, mirrored)
    wt = _make_wt(mirrored)

    deliver(mirrored, _result(mirrored, wt))

    title = state["pr_kwargs"]["title"]
    assert title == "Closes → #1 parser crashes on empty input"
    assert "Closes #1" not in title


def test_an_oversized_story_description_is_bounded() -> None:
    """security/f-005 (CWE-770): a GitHub issue body may be 64 KiB and GitHub
    rejects a PR body over the same limit — an unbounded section would let a
    reporter fail `gh pr create` for every delivery of the story they filed,
    burning a run and an operator interrupt each time."""
    body = _hostile_body(description="x" * (MAX_SECTION_CHARS * 3))

    assert len(body) < MAX_SECTION_CHARS + 2000
    assert "(truncated" in body  # never silently misrepresented
    assert "Closes #7" in _outside_fences(body)


def test_reply_body_variants() -> None:
    fixed = reply_body(fixed=True, sha="abcdef12345", coder_response="tightened it")
    assert fixed.startswith("Fixed in abcdef1234 — tightened it")
    assert AUTOMATED_MARKER in fixed
    disputed = reply_body(fixed=False, sha=None, coder_response="intentional")
    assert disputed.startswith("Not changed — intentional")
    nodetail = reply_body(fixed=True, sha=None, coder_response="")
    assert "Addressed — (no further detail given)" in nodetail
    held = reply_body(
        fixed=False, sha=None, coder_response="adds a guard", held_back_verdict="RED"
    )
    assert "NOT pushed" in held and "RED" in held and "adds a guard" in held
    assert AUTOMATED_MARKER in held


def test_pr_number_from_url() -> None:
    assert pr_number_from_url("https://github.com/o/r/pull/82") == 82
    with pytest.raises(RuntimeError):
        pr_number_from_url("https://github.com/o/r")


# --- request_operator_review (#113) --------------------------------------------


class _RecordingGitHubClient:
    """A recording GitHubClient double for the request_operator_review branch
    tests: records request_reviewers / add_assignees calls and raises a preset
    typed error to drive the self-author-422 fallback logic. ``github_call(op)``
    runs ``op`` against it (see :func:`_patch_github_call`)."""

    def __init__(
        self,
        *,
        request_error: GitHubError | None = None,
        assign_error: GitHubError | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, int, tuple[str, ...]]] = []
        self._request_error = request_error
        self._assign_error = assign_error

    async def request_reviewers(
        self, repo: str, number: int, reviewers: list[str]
    ) -> None:
        self.calls.append(("request_reviewers", repo, number, tuple(reviewers)))
        if self._request_error is not None:
            raise self._request_error

    async def add_assignees(self, repo: str, number: int, assignees: list[str]) -> None:
        self.calls.append(("add_assignees", repo, number, tuple(assignees)))
        if self._assign_error is not None:
            raise self._assign_error


def _patch_github_call(
    monkeypatch: pytest.MonkeyPatch, fake: _RecordingGitHubClient
) -> None:
    """Route ``pr_delivery.github_call(op)`` through the recording fake client."""
    monkeypatch.setattr(pr_delivery, "github_call", lambda op: asyncio.run(op(fake)))


def test_request_operator_review_requests_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient()
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "review_requested"
    assert fake.calls == [("request_reviewers", "o/r", 7, ("dave",))]


def test_request_operator_review_assigns_when_author_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient(
        request_error=GitHubError(
            "GitHub 422 for o/r: Review cannot be requested from pull request author."
        )
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "assigned"
    assert ("add_assignees", "o/r", 7, ("dave",)) in fake.calls


def test_request_operator_review_non_author_failure_does_not_assign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient(
        request_error=GitHubError("GitHub 404 for o/r: Not Found")
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "failed"
    assert not any(c[0] == "add_assignees" for c in fake.calls)


def test_request_operator_review_non_author_422_does_not_assign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 422 that is NOT the self-author case (e.g. a non-collaborator / bad
    # login) must surface as a real failure, not a silent assignee downgrade.
    fake = _RecordingGitHubClient(
        request_error=GitHubError(
            "GitHub 422 for o/r: Reviews may only be requested from collaborators."
        )
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "ghost") == "failed"
    assert not any(c[0] == "add_assignees" for c in fake.calls)


def test_request_operator_review_failed_assign_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _RecordingGitHubClient(
        request_error=GitHubError(
            "GitHub 422 for o/r: cannot be requested from pull request author"
        ),
        assign_error=GitHubError("GitHub 500 for o/r: assign exploded"),
    )
    _patch_github_call(monkeypatch, fake)
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "failed"


# --- best-effort wrappers stay best-effort on a TRANSPORT failure --------------
# GitHubTransportError is a GitHubError, so a connect/read/reset error at the
# HTTP layer degrades to the same fallback the old `gh api` non-zero exit did —
# it must NOT escape and abort the post-PR delivery flow.


_TRANSPORT_ERR = GitHubTransportError(
    "https://api.github.com/repos/o/r/pulls/7", OSError("connection reset")
)


def _github_call_raises(exc: Exception) -> object:
    def _raise(op: object) -> object:
        raise exc

    return _raise


def test_request_copilot_returns_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.request_copilot("o/r", 7) is False


def test_request_operator_review_returns_failed_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transport error is not the self-author 422 → "failed", never aborts.
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.request_operator_review("o/r", 7, "dave") == "failed"


def test_post_pr_comment_returns_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_delivery, "github_call", _github_call_raises(_TRANSPORT_ERR))
    assert pr_delivery.post_pr_comment("o/r", 7, "body") is False


# --- deliver() orchestration ------------------------------------------------------


@pytest.fixture
def config(tmp_git_repo: Path, tmp_path: Path) -> DevelopConfig:
    cfg_dir = tmp_path / "fake-claude"
    cfg_dir.mkdir()
    return DevelopConfig(
        repo=tmp_git_repo,
        description="Add a flag",
        work_dir=tmp_path / "work",
        claude_config_dir=cfg_dir,
        test_gate=False,  # gate scenarios toggle this on explicitly
    )


def _result(config: DevelopConfig, wt: Path) -> DevelopResult:
    return DevelopResult(
        status="approved",
        run_id=config.run_id,
        worktree=wt,
        branch="my-branch",
        base_sha="0" * 40,
        commits=["c1"],
        rounds=2,
        handoff_present=True,
        coder_cost_usd=0.5,
        review_cost_usd=0.5,
        message="approved",
        reviews=(
            ReviewOutcome(
                reviewer="cq",
                status="LGTM",
                passed=True,
                max_severity=None,
            ),
        ),
        coder_session="sess-1",
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    config: DevelopConfig,
    *,
    request_ok: bool = True,
) -> dict:
    """Fake every side-effecting seam deliver() touches (slim since slice D:
    delivery is push + PR open + notify + the optional one-shot request)."""
    state: dict[str, Any] = {
        "pushes": 0,
        "copilot_requests": [],
    }
    config.handoff_dir.mkdir(parents=True, exist_ok=True)
    config.coder_config_dir.mkdir(parents=True, exist_ok=True)

    def fake_push(wt, b):
        state["pushes"] += 1

    monkeypatch.setattr(pr_delivery, "push_branch", fake_push)
    monkeypatch.setattr(pr_delivery, "repo_name_with_owner", lambda wt: "o/r")
    monkeypatch.setattr(
        pr_delivery,
        "create_pr",
        lambda wt, **kw: state.update(pr_kwargs=kw) or "https://github.com/o/r/pull/82",
    )

    def fake_request(repo, pr_number):
        state["copilot_requests"].append((repo, pr_number))
        return request_ok

    monkeypatch.setattr(pr_delivery, "request_copilot", fake_request)
    return state


def _make_wt(config: DevelopConfig) -> Path:
    from lithos_loom.runner import worktree

    config.worktree_parent.mkdir(parents=True, exist_ok=True)
    return worktree.create(
        config.repo, "main", "delivery test", parent=config.worktree_parent
    )


def test_deliver_default_requests_no_copilot(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # Gate 15690a0e: Copilot review is spent deliberately — the default is OFF.
    state = _install(monkeypatch, config)
    wt = _make_wt(config)
    out = deliver(config, _result(config, wt))
    assert out.pr_url.endswith("/pull/82") and out.pr_number == 82
    assert state["pushes"] == 1
    assert out.copilot_requested is False
    assert state["copilot_requests"] == []
    assert state["pr_kwargs"]["base"] == "main"


def test_deliver_copilot_review_hatch_fires_one_request(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # The develop_copilot_review hatch: ONE fire-and-forget request at PR open,
    # no wait, no fix turn — the sweep ingests whatever review lands.
    state = _install(monkeypatch, config)
    wt = _make_wt(config)
    out = deliver(config, _result(config, wt), copilot_review=True)
    assert out.copilot_requested is True
    assert state["copilot_requests"] == [("o/r", 82)]
    assert any("the sweep ingests it" in n for n in out.notes)


def test_deliver_unmonitored_request_note_names_the_response_surface(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # PR #348 review F2: a standalone run creates no pr gate, so the sweep can
    # never observe the requested review — the note must not promise ingestion
    # and instead points at the manual converge --from-github surface.
    _install(monkeypatch, config)
    wt = _make_wt(config)
    out = deliver(
        config, _result(config, wt), copilot_review=True, review_monitored=False
    )
    assert out.copilot_requested is True
    assert any("NOT auto-monitored" in n for n in out.notes)
    assert any("converge <pr> --from-github" in n for n in out.notes)
    assert not any("the sweep ingests it" in n for n in out.notes)


def test_deliver_copilot_request_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install(monkeypatch, config, request_ok=False)
    wt = _make_wt(config)
    out = deliver(config, _result(config, wt), copilot_review=True)
    assert out.pr_url.endswith("/pull/82")  # delivery still succeeded
    assert out.copilot_requested is False
    assert any("request failed" in n for n in out.notes)


def test_deliver_notifies_operator_when_configured(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    cfg = replace(config, notify_github_login="dave")
    _install(monkeypatch, cfg)
    calls: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        pr_delivery,
        "request_operator_review",
        lambda repo, pr_number, login: (
            calls.append((repo, pr_number, login)) or "review_requested"
        ),
    )
    wt = _make_wt(cfg)
    out = deliver(cfg, _result(cfg, wt))
    assert calls == [("o/r", 82, "dave")]
    assert any("requested review from @dave" in n for n in out.notes)


def test_deliver_preserves_pr_url_when_post_open_step_raises(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    # #192 review: once create_pr() returns, the PR exists. A later failure
    # (here the one-shot Copilot request) must NOT lose the url: deliver()
    # degrades to a delivered-with-notes outcome carrying it, so
    # build_result_payload still records pr_url and `attach` can render it
    # instead of stranding the operator with an approved run and no PR.
    _install(monkeypatch, config)
    wt = _make_wt(config)

    def boom(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("github flaked right after the PR was opened")

    monkeypatch.setattr(pr_delivery, "request_copilot", boom)

    out = deliver(config, _result(config, wt), copilot_review=True)
    assert out.pr_url.endswith("/pull/82") and out.pr_number == 82  # url preserved
    assert any("did not finish after opening the PR" in n for n in out.notes)


def test_deliver_skips_operator_notify_when_unset(
    monkeypatch: pytest.MonkeyPatch, config: DevelopConfig
) -> None:
    _install(monkeypatch, config)  # config.notify_github_login is None
    called = False

    def boom(*a, **k):
        nonlocal called
        called = True
        return "review_requested"

    monkeypatch.setattr(pr_delivery, "request_operator_review", boom)
    wt = _make_wt(config)
    deliver(config, _result(config, wt))
    assert called is False


def test_delivery_budget_is_the_flat_overhead() -> None:
    # #189, post slice D: delivery has NO agent phase (the inline Copilot
    # round is retired) — the budget is the push/PR/gh overhead margin alone,
    # so `develop attach` bounds a crashed delivery far sooner than the old
    # copilot+coder+gate sum while still never timing out a healthy one.
    from types import SimpleNamespace

    budget = pr_delivery.delivery_budget_seconds(SimpleNamespace(test_timeout=900))
    assert budget == pr_delivery._DELIVERY_OVERHEAD_SECONDS
    # config no longer contributes a term (no gate runs during delivery).
    wider = pr_delivery.delivery_budget_seconds(SimpleNamespace(test_timeout=1800))
    assert wider == budget


def test_delivery_fallback_exceeds_the_full_default_delivery_budget() -> None:
    # #189 cross-module invariant: run_outcome's flat fallback — used by `develop
    # attach` when a run recorded no delivery deadline (predates the marker, or its
    # write failed) — must comfortably exceed the LARGEST default delivery budget,
    # or it could false-fire on a healthy default-config run. This executes the
    # derivation that was previously only prose in cli/develop.py's
    # DELIVERY_FALLBACK_SECONDS comment (9000 > 6900): if a future phase widens the
    # budget past the fallback, this fails instead of silently under-bounding attach.
    from types import SimpleNamespace

    from lithos_loom.plugins.story_develop import run_outcome
    from lithos_loom.plugins.story_develop.config import (
        DEFAULT_TEST_TIMEOUT,
    )

    # The default budget is single-sourced from the module the parser uses —
    # so the invariant tracks a default that changes, rather than a hard-coded
    # copy that could silently pass against a stale value.
    default_budget = pr_delivery.delivery_budget_seconds(
        SimpleNamespace(test_timeout=DEFAULT_TEST_TIMEOUT)
    )
    assert default_budget < run_outcome.DELIVERY_FALLBACK_SECONDS


# --- deliver_guarded (ARCH-1.S3): the shared develop→deliver seam -----------------


def test_deliver_guarded_skips_when_nothing_to_deliver(
    config: DevelopConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (None, None) — and deliver() is never called — when open_pr is off OR the
    # run wasn't approved. No deadline marker is recorded in the skip case.
    approved = _result(config, tmp_path)
    not_approved = replace(approved, status="max_rounds")
    called = {"n": 0}
    monkeypatch.setattr(
        pr_delivery,
        "deliver",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    assert pr_delivery.deliver_guarded(
        config,
        not_approved,
        open_pr=True,  # approved gate fails → skip
        copilot_review=False,
        github_issue_url=None,
        task_id=None,
    ) == (None, None)
    assert pr_delivery.deliver_guarded(
        config,
        approved,
        open_pr=False,  # open_pr off → skip
        copilot_review=False,
        github_issue_url=None,
        task_id=None,
    ) == (None, None)
    assert called["n"] == 0  # deliver() untouched in either skip case
    assert not (config.run_dir / "delivery.json").exists()  # no deadline recorded


def test_deliver_guarded_returns_outcome_and_records_deadline_on_success(
    config: DevelopConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lithos_loom.plugins.story_develop.pr_delivery import DeliveryOutcome

    config.run_dir.mkdir(parents=True, exist_ok=True)  # develop() creates this first
    approved = _result(config, tmp_path)
    outcome = DeliveryOutcome(pr_url="https://github.com/o/r/pull/1", pr_number=1)
    seen: dict[str, bool] = {}

    def fake_deliver(cfg, result, **kw):
        # the #189 deadline must already be on disk when delivery starts (ordering)
        seen["marker_at_delivery"] = (cfg.run_dir / "delivery.json").is_file()
        return outcome

    monkeypatch.setattr(pr_delivery, "deliver", fake_deliver)
    delivery, error = pr_delivery.deliver_guarded(
        config,
        approved,
        open_pr=True,
        copilot_review=False,
        github_issue_url=None,
        task_id=None,
    )
    assert delivery is outcome and error is None
    assert seen["marker_at_delivery"] is True  # deadline recorded BEFORE delivery ran


def test_deliver_guarded_records_failure_and_returns_reason(
    config: DevelopConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #194: deliver() raising before a PR opens → (None, reason), and the private
    # delivery.json failure marker is written so attach can report it terminally.
    config.run_dir.mkdir(parents=True, exist_ok=True)
    approved = _result(config, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("gh pr create failed: HTTP 422")

    monkeypatch.setattr(pr_delivery, "deliver", boom)
    delivery, error = pr_delivery.deliver_guarded(
        config,
        approved,
        open_pr=True,
        copilot_review=False,
        github_issue_url=None,
        task_id=None,
    )
    assert delivery is None
    assert error is not None and "gh pr create failed" in error
    marker = json.loads((config.run_dir / "delivery.json").read_text(encoding="utf-8"))
    assert marker["failed"] is True


# --- push_to_pr_ref: guarded fast-forward push to a PR head ref (converge) -----


def _fake_run(
    ls_stdout: str,
    *,
    push_rc: int = 0,
    head_sha: str = "l" * 40,
    branch_sha: str | None = None,
    push_stderr: str = "",
    ancestor_rc: int = 0,
) -> Any:
    """A fake ``pr_delivery._run`` dispatching by git subcommand + a call log.

    ``rev-parse HEAD`` resolves to *head_sha*; ``rev-parse --verify <branch>``
    resolves to *branch_sha* (default == *head_sha*, i.e. the caller's branch IS
    the reviewed HEAD). ``ancestor_rc`` is the ``merge-base --is-ancestor`` exit
    code (0 = HEAD descends from the expected head, the normal case).
    """
    branch_sha = head_sha if branch_sha is None else branch_sha
    calls: list[list[str]] = []

    def run(args: list[str], *, cwd: Path, timeout: int = 120) -> Any:
        calls.append(args)
        if args[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(args, 0, stdout=ls_stdout, stderr="")
        if args[:2] == ["git", "rev-parse"]:
            sha = head_sha if args[-1] == "HEAD" else branch_sha
            return subprocess.CompletedProcess(args, 0, stdout=sha + "\n", stderr="")
        if args[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(args, ancestor_rc, stdout="", stderr="")
        if args[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                args, push_rc, stdout="", stderr=push_stderr
            )
        raise AssertionError(f"unexpected git call: {args}")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_push_to_pr_ref_fast_forwards_when_remote_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _fake_run("e" * 40 + "\trefs/heads/feature\n")
    monkeypatch.setattr(pr_delivery, "_run", run)
    pushed = pr_delivery.push_to_pr_ref(
        Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
    )
    assert pushed == "l" * 40  # the exact reviewed HEAD sha
    push = next(c for c in run.calls if c[:2] == ["git", "push"])
    # push the exact HEAD sha to the fully-qualified ref, leased to the expected
    # head — a guarded fast-forward, NOT a blind --force / -f.
    assert push == [
        "git",
        "push",
        "--force-with-lease=refs/heads/feature:" + "e" * 40,
        "origin",
        "l" * 40 + ":refs/heads/feature",
    ]
    assert "--force" not in push and "-f" not in push


def test_push_to_pr_ref_refuses_when_local_branch_is_not_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the source-ref safety boundary: local_branch must BE the reviewed HEAD.
    # A divergent branch (points elsewhere) is refused BEFORE pushing, so a
    # non-descendant local_branch can never force the PR branch backward even
    # though HEAD descends from expected and the lease matches (finding: checked
    # vs pushed object mismatch). Not a merge race — a caller/contract error.
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        head_sha="l" * 40,
        branch_sha="d" * 40,  # local_branch resolves to a DIFFERENT commit
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_raises_fork_when_ref_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _fake_run("")  # ls-remote finds nothing → head lives on a fork
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.ForkPushUnsupported):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_reads_exact_ref_among_suffix_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # git ls-remote treats a bare name as a tail pattern: `feature` also matches
    # refs/heads/a/feature, and output is ref-name-sorted so the collision comes
    # FIRST. The pre-fix code took the first line's sha → a permanent false
    # merge_race on a valid PR. The lookup must key on the exact fully-qualified
    # ref name, not line order.
    run = _fake_run(
        "z" * 40 + "\trefs/heads/a/feature\n" + "e" * 40 + "\trefs/heads/feature\n"
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    pushed = pr_delivery.push_to_pr_ref(
        Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
    )
    assert pushed == "l" * 40
    # and the preflight queries the fully-qualified ref, same as the lease/push
    ls = next(c for c in run.calls if c[:2] == ["git", "ls-remote"])
    assert ls[-1] == "refs/heads/feature"


def test_push_to_pr_ref_fork_when_only_a_suffix_collision_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the PR ref itself is absent; only a suffix-colliding branch matched the
    # pattern. That is a fork (ref not on origin), NOT a merge race against the
    # wrong branch's sha.
    run = _fake_run("z" * 40 + "\trefs/heads/a/feature\n")
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.ForkPushUnsupported):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_raises_merge_race_when_remote_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _fake_run("z" * 40 + "\trefs/heads/feature\n")  # remote moved
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    # never pushed, and never with --force (would clobber the concurrent commit)
    assert not any(c[:2] == ["git", "push"] for c in run.calls)
    assert not any("--force" in c or "-f" in c for c in run.calls)


def test_push_to_pr_ref_raises_on_push_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a non-race failure (auth) stays a generic RuntimeError, NOT a merge race
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr="fatal: Authentication failed for 'https://github.com/o/r'",
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)


def test_push_to_pr_ref_maps_non_fast_forward_push_to_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # TOCTOU: ls-remote matched, but the remote advanced before the push, which
    # git rejects as non-fast-forward. That must surface as merge_race (the same
    # outcome as the pre-check), never a force-push or a generic crash (finding #4).
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr=(
            " ! [rejected]        converge-abc -> feature (non-fast-forward)\n"
            "error: failed to push some refs to 'origin'"
        ),
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any("--force" in c or "-f" in c for c in run.calls)


def test_push_to_pr_ref_hook_rejection_is_not_a_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a branch-protection / pre-receive hook rejection also says "rejected" but
    # is NOT a non-fast-forward race — re-running converge won't help, so it must
    # stay a generic RuntimeError, not a merge_race (Copilot #272).
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr=(
            " ! [remote rejected] converge-abc -> feature "
            "(protected branch hook declined)\n"
            "error: failed to push some refs to 'origin'"
        ),
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)


def test_push_to_pr_ref_lease_rejection_is_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The ls-remote pre-check passed (ref at expected), but the remote then changed
    # (deleted / advanced / force-rewound) before the leased push, which git
    # rejects with "stale info". That atomic-CAS failure is the race the lease
    # exists to catch → merge_race, never a silent recreate/overwrite (finding #1).
    run = _fake_run(
        "e" * 40 + "\trefs/heads/feature\n",
        push_rc=1,
        push_stderr=(
            " ! [rejected] converge-abc -> feature (stale info)\n"
            "error: failed to push some refs to 'origin'"
        ),
    )
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    # the lease is pinned to the expected head, and never a blind --force
    push = next(c for c in run.calls if c[:2] == ["git", "push"])
    assert "--force-with-lease=refs/heads/feature:" + "e" * 40 in push
    assert "--force" not in push and "-f" not in push


def test_push_to_pr_ref_non_descendant_local_is_rejected_without_pushing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # append-only guard: if the local branch does NOT descend from the expected
    # head (a rewrite, not an append), refuse BEFORE pushing so the leased update
    # can only ever fast-forward — never rewind the contributor's history.
    run = _fake_run("e" * 40 + "\trefs/heads/feature\n", ancestor_rc=1)
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(pr_delivery.MergeRaceDetected):
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


def test_push_to_pr_ref_ancestry_check_fatal_error_is_not_a_merge_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # merge-base --is-ancestor exits 1 for "not an ancestor" but 128 for a fatal
    # error (bad object name, repo corruption). Only rc 1 is the race; a fatal
    # error must surface as a generic RuntimeError — "re-run converge" would be
    # misleading guidance for a broken repo (Copilot #272).
    run = _fake_run("e" * 40 + "\trefs/heads/feature\n", ancestor_rc=128)
    monkeypatch.setattr(pr_delivery, "_run", run)
    with pytest.raises(RuntimeError) as excinfo:
        pr_delivery.push_to_pr_ref(
            Path("/wt"), "converge-abc", "feature", expected_remote_sha="e" * 40
        )
    assert not isinstance(excinfo.value, pr_delivery.MergeRaceDetected)
    assert not any(c[:2] == ["git", "push"] for c in run.calls)  # never pushed


# --- push_to_pr_ref against a REAL local bare remote (finding: checked vs -----
# --- pushed object). Mocked _run can't exercise the actual git push semantics. -


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _remote_sha(bare: Path, ref: str) -> str:
    out = subprocess.run(
        ["git", "ls-remote", str(bare), f"refs/heads/{ref}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return out.split()[0] if out else ""


def _seed_pr_repo(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    """A bare 'origin' with `feature` at H (parent G), and a work repo whose HEAD
    is a descendant of H on branch `converge-x`. Returns (wt, bare, G, H, fixed)."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "init", "-b", "main", str(wt)], check=True, capture_output=True
    )
    _git(wt, "config", "user.email", "t@t")
    _git(wt, "config", "user.name", "t")
    _git(wt, "remote", "add", "origin", str(bare))
    (wt / "a.txt").write_text("G\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "G")
    g = _sha(wt)
    (wt / "a.txt").write_text("H\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "H")
    h = _sha(wt)
    _git(wt, "push", "origin", "HEAD:refs/heads/feature")  # feature at H
    _git(wt, "checkout", "-b", "converge-x")
    (wt / "b.txt").write_text("fix\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "fix")
    return wt, bare, g, h, _sha(wt)


def test_push_to_pr_ref_pushes_reviewed_head_real_git(tmp_path: Path) -> None:
    wt, bare, _g, h, fixed = _seed_pr_repo(tmp_path)
    pushed = pr_delivery.push_to_pr_ref(
        wt, "converge-x", "feature", expected_remote_sha=h
    )
    assert pushed == fixed  # returns the EXACT pushed sha (the reviewed HEAD)
    assert _remote_sha(bare, "feature") == fixed  # remote fast-forwarded to HEAD


def test_push_to_pr_ref_ignores_suffix_colliding_remote_branch_real_git(
    tmp_path: Path,
) -> None:
    # ls-remote pattern semantics: `feature` also matches refs/heads/a/feature,
    # which SORTS FIRST. The pre-fix code read the first returned sha → a false,
    # unresolvable merge_race for a valid PR whenever such a branch exists. Only
    # refs/heads/feature may govern the push; a/feature must be left untouched.
    wt, bare, g, h, fixed = _seed_pr_repo(tmp_path)
    _git(wt, "push", "origin", f"{g}:refs/heads/a/feature")  # collider at G ≠ H
    pushed = pr_delivery.push_to_pr_ref(
        wt, "converge-x", "feature", expected_remote_sha=h
    )
    assert pushed == fixed
    assert _remote_sha(bare, "feature") == fixed  # the real PR branch advanced
    assert _remote_sha(bare, "a/feature") == g  # the collider is untouched


def test_push_to_pr_ref_refuses_non_head_branch_real_git(tmp_path: Path) -> None:
    # a divergent local_branch pointing at an OLDER commit (G, an ancestor of the
    # remote's H) must NOT force the PR branch backward. The pre-fix code pushed
    # local_branch, so `stale`->feature would have rewound feature from H to G;
    # the fix pushes the reviewed HEAD and refuses a branch that isn't HEAD.
    wt, bare, g, h, _fixed = _seed_pr_repo(tmp_path)
    _git(wt, "branch", "stale", g)  # 'stale' at G, older than HEAD (and than H)
    with pytest.raises(RuntimeError):
        pr_delivery.push_to_pr_ref(wt, "stale", "feature", expected_remote_sha=h)
    assert _remote_sha(bare, "feature") == h  # remote UNCHANGED — never rewound

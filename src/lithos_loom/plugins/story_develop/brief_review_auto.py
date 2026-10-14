"""Brief review at dispatch (604fb936), phase 2: the structural check.

With ``develop_brief_review_auto_facts`` on, an addendum that holds **only
facts** may be appended to the story without its gate. The pass's own labels
are not enough for that: the pilot showed the fact/decision split is exactly
where the agent errs (W6's "the cancel routes go in a new module" sat under
Facts). So every fact must also pass a second check that is structural, pure
and deterministic — no judgment, no I/O:

1. **heading** — its bold title carries no prescriptive marker
   (:data:`HEADING_MARKERS`, word-bounded, case-insensitive);
2. **citation** — at least one backtick span is, in its entirety, a
   repository path with a known extension and an optional ``:N`` / ``:N-M``;
3. **marker** — no ``Basis:`` or ``Proposed:`` (a decision's lines).

Every failure direction is safe: a fact the check cannot read is treated as
a decision, and the story is held for the operator.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .brief_review import KIND_FACT, MODE_DELTA, MODE_FULL, Addendum, BriefItem
from .publish_text import fence_untrusted

__all__ = [
    "CITATION_EXTENSIONS",
    "HEADING_MARKERS",
    "AutoFactsVerdict",
    "check_auto_facts",
    "describe_verdict",
    "fact_headings",
    "fact_problems",
    "item_heading",
    "pr_section",
]

# The words that make a heading say what to build rather than what is there.
# The prompt (``prompts/brief_review.md``) names exactly these.
HEADING_MARKERS: tuple[str, ...] = (
    "must",
    "should",
    "shall",
    "needs to",
    "has to",
    "goes in",
    "go in",
    "put",
    "use",
    "add",
    "create",
    "move",
    "rename",
    "split",
    "extract",
    "make",
)
CITATION_EXTENSIONS: frozenset[str] = frozenset(
    {
        "py",
        "pyi",
        "md",
        "toml",
        "json",
        "yaml",
        "yml",
        "txt",
        "cfg",
        "ini",
        "sh",
        "js",
        "ts",
        "tsx",
        "css",
        "html",
        "sql",
    }
)

_HEADING_RE = re.compile(r"^\*\*[A-Z]\d+\.[ \t]*(?P<heading>[^*\n]*?)[ \t]*\*\*")
_MARKER_RES = tuple(
    (marker, re.compile(rf"\b{marker.replace(' ', r'\s+')}\b", re.IGNORECASE))
    for marker in HEADING_MARKERS
)
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_PATH_RE = re.compile(
    r"(?:[\w.-]+/)*[\w-][\w.-]*\.(?P<ext>[A-Za-z]+)(?::\d+(?:-\d+)?)?"
)
_DECISION_LINE_RE = re.compile(r"\b(?P<marker>Basis|Proposed):", re.IGNORECASE)


@dataclass(frozen=True)
class AutoFactsVerdict:
    """The check's answer for one addendum. *facts_only*: the pass labelled
    every item a fact (and there are some, with no stray line and no
    ``No change``). *problems*: one line per failed rule per fact."""

    facts_only: bool
    problems: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.facts_only and not self.problems

    def as_record(self) -> dict[str, object]:
        """The ``check`` sub-record every drafted ``brief_review`` entry carries."""
        return {
            "facts_only": self.facts_only,
            "passed": self.passed,
            "problems": list(self.problems),
        }


def item_heading(item: BriefItem) -> str | None:
    """The bold title after *item*'s id, up to the closing ``**`` — ``None``
    when the first line is not ``**<id>. <title>**``."""
    match = _HEADING_RE.match(item.text)
    if match is None or not match.group("heading"):
        return None
    return match.group("heading")


def fact_problems(item: BriefItem) -> list[str]:
    """Why *item* reads like a decision, one line per failed rule (empty: it
    reads as a fact)."""
    problems: list[str] = []
    heading = item_heading(item)
    if heading is None:
        problems.append(
            f"{item.id} reads like a decision: its heading could not be read — "
            f"start it `- **{item.id}. <title>.**`"
        )
    else:
        said = [m for m, pattern in _MARKER_RES if pattern.search(heading)]
        if said:
            words = ", ".join(f"`{m}`" for m in said)
            problems.append(f"{item.id} reads like a decision: heading says {words}")
    if not any(_is_path(span) for span in _SPAN_RE.findall(item.text)):
        problems.append(
            f"{item.id} reads like a decision: it cites no repository path "
            "(a backticked `path/file.py:123`)"
        )
    marked = sorted(
        {m.group("marker").capitalize() for m in _DECISION_LINE_RE.finditer(item.text)}
    )
    if marked:
        lines = ", ".join(f"`{m}:`" for m in marked)
        problems.append(f"{item.id} reads like a decision: it carries {lines}")
    return problems


def _is_path(span: str) -> bool:
    match = _PATH_RE.fullmatch(span)
    return match is not None and match.group("ext") in CITATION_EXTENSIONS


def check_auto_facts(addendum: Addendum) -> AutoFactsVerdict:
    """The structural check over *addendum* — run on every pass, so every
    drafted record carries it, whether or not the dial lets it skip a gate."""
    facts_only = (
        bool(addendum.items)
        and all(item.kind == KIND_FACT for item in addendum.items)
        and not addendum.stray
        and addendum.no_change is None
    )
    problems = tuple(p for item in addendum.facts for p in fact_problems(item))
    return AutoFactsVerdict(facts_only=facts_only, problems=problems)


def fact_headings(addendum: Addendum) -> tuple[str, ...]:
    """Each fact as ``"F13. W7 added no refusal hook."`` — its id and heading
    (the first line, for a fact whose heading cannot be read)."""
    return tuple(
        f"{item.id}. {item_heading(item) or item.text.splitlines()[0]}"
        for item in addendum.facts
    )


def pr_section(headings: tuple[str, ...]) -> str:
    """The PR body's ``## Brief review`` section for facts appended without a
    gate: loom's own heading and line, then *headings* fenced (the agent wrote
    them). ``""`` when there are none. ``## What`` is capped, and appended
    facts are its tail — the first text cut — so they are listed here too."""
    if not headings:
        return ""
    return "\n".join(
        [
            "## Brief review",
            "",
            "These facts were appended to the story at dispatch without a gate "
            "(facts only, structural check passed); their full text is in "
            "*What*.",
            "",
            fence_untrusted("\n".join(headings)),
        ]
    )


def describe_verdict(
    verdict: AutoFactsVerdict, *, counts: Mapping[str, int], mode: str = MODE_FULL
) -> str:
    """One line saying what the dispatch would do with this verdict. *counts*
    is the pass's ``{"scope_cut", "fact", "decision"}`` tally; *mode* full or
    delta (a recheck is governed by the check whatever the dial says)."""
    dial = "with develop_brief_review_auto_facts on"
    if not any(counts.values()):
        tail = (
            "would proceed without a gate"
            if mode == MODE_DELTA
            else f"would skip the gate {dial}"
        )
        return f"auto-facts check: no change — {tail}"
    if verdict.passed:
        if mode == MODE_DELTA:
            return "auto-facts check: would append without a gate"
        return f"auto-facts check: would skip the gate {dial}"
    if verdict.facts_only:
        return f"auto-facts check: would hold: {'; '.join(verdict.problems)}"
    other = [
        f"{n} {noun}{'' if n == 1 else 's'}"
        for n, noun in (
            (counts.get("scope_cut", 0), "scope cut"),
            (counts.get("decision", 0), "decision"),
        )
        if n
    ]
    return (
        f"auto-facts check: not facts only ({', '.join(other) or 'stray lines'}): "
        "would hold"
    )

"""Brief review at dispatch (604fb936): check a story's brief against its base.

A PRD's slices are all written before any of them is built, and every merge
moves the code the later briefs describe. The lens T3 hand pilot (W6–W8,
2026-10-05) showed what closes that gap: before the coder starts, one reader
checks the brief against the exact tree the coder will start from and writes
an **addendum** the operator approves. It caught defects that would otherwise
have shipped, and twice it changed scope materially.

This module is that reader and its format:

* **The format** — :func:`parse_addendum` / :func:`render_addendum`. Three
  kinds of item, each a top-level ``- **<id>. <title>.**`` bullet with its
  nested lines: **scope cuts** (``S#``), **facts** (``F#`` — they *describe*
  the code and never say what to build) and **decisions** (``D#`` —
  anything that prescribes, chooses or changes scope). The parse is lenient:
  it reads the pilot's own operator-approved addenda (the test fixtures), so
  approved text and draft can later be compared item by item.
* **The check on an agent's draft** — :func:`validate_addendum`, strict: ids
  well formed, unique and in the section their letter names, and every
  decision and scope cut carrying a ``Basis:`` line, so the operator can see
  what each prescription stands on.
* **The pass** — :func:`review_brief`, one read-only container turn at the
  base, the external-triage shape (``external_triage.py``), with one
  correction turn when the draft does not validate. Degrade, don't raise:
  every failure comes back as a result with a ``note`` and the raw text.

Story text reaches the agent only as **files** under the read-only artifacts
mount, never through a prompt slot: :func:`handoff.render_prompt` is a
sequential ``str.replace``, so a brief carrying ``{some_slot}`` would be
spliced into.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ...runner import git, worktree
from . import containers, engines, handoff, run_owner, turns
from .agent_session import build_run_cmd
from .config import HANDOFF_MOUNT_NAME, WORKSPACE_MOUNT, DevelopConfig

logger = logging.getLogger(__name__)

__all__ = [
    "BRIEF_REVIEW_HANDOFF_NAME",
    "INPUTS_DIR_NAME",
    "KIND_DECISION",
    "KIND_FACT",
    "KIND_SCOPE_CUT",
    "MODE_DELTA",
    "MODE_FULL",
    "Addendum",
    "BriefInputs",
    "BriefItem",
    "BriefReviewResult",
    "parse_addendum",
    "render_addendum",
    "review_brief",
    "validate_addendum",
]

# The file the agent writes into the handoff mount. ``round_00_`` like
# triage's, so attach's per-round regexes never mistake it for a round.
BRIEF_REVIEW_HANDOFF_NAME = "round_00_brief_review.md"
# Where the inputs land under the run's artifacts dir — mounted read-only at
# ``/workspace/.handoff/artifacts``, so the agent can read but never rewrite
# the brief it is checking.
INPUTS_DIR_NAME = "brief-review"

KIND_SCOPE_CUT = "scope_cut"
KIND_FACT = "fact"
KIND_DECISION = "decision"

MODE_FULL = "full"  # the brief has never been reviewed: check all of it
MODE_DELTA = "delta"  # an approved review exists at another base: what moved?

# The kind each section holds, and the id letter its items carry.
_SECTIONS: dict[str, str] = {
    "scope cuts": KIND_SCOPE_CUT,
    "facts": KIND_FACT,
    "decisions": KIND_DECISION,
}
_LETTER: dict[str, str] = {KIND_SCOPE_CUT: "S", KIND_FACT: "F", KIND_DECISION: "D"}
_SECTION_TITLE: dict[str, str] = {
    KIND_SCOPE_CUT: "Scope cuts",
    KIND_FACT: "Facts",
    KIND_DECISION: "Decisions",
}
# Render order: cuts first (they narrow what the rest pins down), as the pilot.
_ORDER = (KIND_SCOPE_CUT, KIND_FACT, KIND_DECISION)

# A section heading: ``## Facts`` (the agent's file) or ``**Facts** (…)``
# (the pilot's and the rendered addendum's). ``No change`` is the delta's
# nothing-to-report answer.
_HEADING_RE = re.compile(
    r"^(?:#{1,6}[ \t]*|\*\*)(?P<name>scope cuts|facts|decisions|no change)\b",
    re.IGNORECASE,
)
# A top-level item: ``- **F1. Title.** …`` at column 0. The id must be
# followed by a full stop; ``- **F1, exactly.**`` (a recheck's back-reference
# to an earlier item) is prose, not a new item.
_ITEM_RE = re.compile(r"^- \*\*(?P<id>[A-Z]\d+)\.")
# Anything that LOOKS like an item at column 0. One the parse cannot read
# (``- **D1: …**``, a colon for the full stop) is kept as stray content and
# reported, never silently dropped or folded into the item above it.
_ITEM_LIKE_RE = re.compile(r"^- \*\*")
_ID_RE = re.compile(r"^[SFD]\d+$")
_BASIS_RE = re.compile(
    r"^[ \t]*(?:-[ \t]*)?Basis:[ \t]*\S", re.IGNORECASE | re.MULTILINE
)
# The rendered no-change line: ``No change: <reason>``.
_NO_CHANGE_LINE_RE = re.compile(r"^No change:[ \t]*(?P<reason>.*\S)", re.MULTILINE)


@dataclass(frozen=True)
class BriefItem:
    """One addendum item. *text* is the item exactly as written, minus the
    leading ``- `` of its first line: the bold id and title, the body, and
    every nested line with its own indentation — so rendering it back is
    ``"- " + text``. *kind* is the kind of the section it was found under
    (``None``: it appeared before any heading)."""

    id: str
    kind: str | None
    text: str


@dataclass(frozen=True)
class Addendum:
    """The parsed items, in document order, or a delta's *no_change* reason
    (``""`` when the heading was there but no reason followed it). *stray*
    holds the lines the parse could not place: an item-like line it cannot
    read as one, or prose inside a section but outside any item."""

    items: tuple[BriefItem, ...] = ()
    no_change: str | None = None
    stray: tuple[str, ...] = ()

    def of_kind(self, kind: str) -> tuple[BriefItem, ...]:
        return tuple(i for i in self.items if i.kind == kind)

    @property
    def scope_cuts(self) -> tuple[BriefItem, ...]:
        return self.of_kind(KIND_SCOPE_CUT)

    @property
    def facts(self) -> tuple[BriefItem, ...]:
        return self.of_kind(KIND_FACT)

    @property
    def decisions(self) -> tuple[BriefItem, ...]:
        return self.of_kind(KIND_DECISION)


def parse_addendum(text: str) -> Addendum:
    """Read an addendum — an agent's draft, a rendered one, or the pilot's.

    Lenient by design: it never raises. The header paragraph before the
    first section is not an item; an item runs from its ``- **<id>.`` line
    to the next item or heading, blank lines and nested blocks included,
    trailing blank lines dropped. What it cannot place goes to ``stray``
    rather than vanishing: an item-like line it cannot read (kept in the
    open item's text too, so the text stays verbatim), and prose inside a
    section before its first item. Structure problems are
    :func:`validate_addendum`'s to report.
    """
    items: list[BriefItem] = []
    kind: str | None = None
    current: tuple[str, str | None, list[str]] | None = None
    no_change: str | None = None
    in_no_change = False
    no_change_lines: list[str] = []
    stray: list[str] = []

    def close() -> None:
        nonlocal current
        if current is not None:
            item_id, item_kind, lines = current
            while lines and not lines[-1].strip():
                lines.pop()
            items.append(BriefItem(id=item_id, kind=item_kind, text="\n".join(lines)))
            current = None

    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading is not None:
            close()
            name = heading.group("name").lower()
            in_no_change = name == "no change"
            kind = None if in_no_change else _SECTIONS[name]
            if in_no_change:
                no_change = ""
            continue
        item = _ITEM_RE.match(line)
        if item is not None:
            close()
            in_no_change = False
            current = (item.group("id"), kind, [line[2:]])
            continue
        if _ITEM_LIKE_RE.match(line):
            stray.append(line.strip())
        if current is not None:
            current[2].append(line)
        elif in_no_change and line.strip():
            no_change_lines.append(line.strip())
        elif kind is not None and line.strip() and not _ITEM_LIKE_RE.match(line):
            stray.append(line.strip())
    close()
    if no_change is None:
        rendered = _NO_CHANGE_LINE_RE.search(text)
        if rendered is not None:
            no_change = rendered.group("reason").strip()
    elif no_change_lines:
        no_change = " ".join(no_change_lines)
    return Addendum(items=tuple(items), no_change=no_change, stray=tuple(stray))


def validate_addendum(addendum: Addendum, *, strict: bool = True) -> list[str]:
    """What is wrong with *addendum* as a brief review's answer, one line each.

    Empty when it can stand. Always checked: there is something to say
    (items, or a reasoned ``No change`` — not both); every item sits under a
    section; ids are well formed and unique; each id's letter matches its
    section. *strict* (an agent's draft) also requires a ``Basis:`` line on
    every decision and scope cut — the operator approves prescriptions, and
    needs to see what each stands on. The pilot's hand-written addenda
    predate that rule, hence ``strict=False`` for reading them.

    Both modes take the same shape: a full review that finds the brief
    accurate answers ``No change`` too, and the operator still sees it.
    """
    problems: list[str] = []
    if addendum.no_change is not None:
        if not addendum.no_change:
            problems.append("`No change` must give its reason on the next line")
        if addendum.items:
            problems.append(
                "the file has both a `No change` section and items — keep one"
            )
    elif not addendum.items:
        problems.append(
            "no items: write at least one `- **F1. …**` item under a section "
            "heading, or a `## No change` section with its reason"
        )
    problems.extend(
        f"this line could not be read as part of an item: `{line}` — start "
        "every item with `- **<id>. ` (the id, then a full stop) at the start "
        "of a line, and keep any other text inside an item"
        for line in addendum.stray
    )
    seen: set[str] = set()
    for item in addendum.items:
        if item.kind is None:
            problems.append(f"{item.id} appears before any section heading")
            continue
        if not _ID_RE.match(item.id):
            problems.append(
                f"{item.id} is not a valid id (S<n> for scope cuts, F<n> for "
                "facts, D<n> for decisions)"
            )
        elif item.id[0] != _LETTER[item.kind]:
            problems.append(
                f"{item.id} is under {_SECTION_TITLE[item.kind]}; its letter "
                f"says otherwise — renumber it {_LETTER[item.kind]}<n> or move it"
            )
        if item.id in seen:
            problems.append(f"{item.id} appears twice — ids must be unique")
        seen.add(item.id)
        if (
            strict
            and item.kind in (KIND_DECISION, KIND_SCOPE_CUT)
            and not _BASIS_RE.search(item.text)
        ):
            problems.append(
                f"{item.id} has no `Basis:` line — name the fact ids, file:line "
                "or PRD / REQUIREMENTS section it stands on"
            )
    return problems


def render_addendum(
    addendum: Addendum,
    *,
    base_sha: str,
    on: date,
    mode: str,
    prior_base: str | None = None,
) -> str:
    """The canonical Markdown of *addendum*, as it is appended to a story.

    A one-paragraph header naming the base it was checked against (and, for
    a delta, the base the approved review stood on), then each non-empty
    section in cut → fact → decision order with every item rendered as
    written. No leading blank line: the appender owns the separation.
    """
    base = f"`{base_sha[:12]}`"
    if mode == MODE_DELTA:
        prior = f" from `{prior_base[:12]}`" if prior_base else ""
        header = (
            f"**Recheck against {base} ({on.isoformat()})** — the base moved"
            f"{prior} after this brief's review. Where an item below "
            "contradicts anything above it, the item wins."
        )
    else:
        header = (
            f"**Brief review against {base} ({on.isoformat()})** — checked "
            "against the tree the coder starts from, before dispatch. Facts "
            "describe the code at that base; scope cuts and decisions are "
            "binding. Where an item contradicts the brief or the PRD, the item "
            "wins."
        )
    parts = [header]
    if addendum.no_change is not None and not addendum.items:
        parts.append(f"No change: {addendum.no_change}")
    for kind in _ORDER:
        section = addendum.of_kind(kind)
        if not section:
            continue
        parts.append(_section_heading(kind, base))
        parts.append("\n".join("- " + item.text for item in section))
    return "\n\n".join(parts) + "\n"


def _section_heading(kind: str, base: str) -> str:
    if kind == KIND_SCOPE_CUT:
        return (
            "**Scope cuts** — they replace the matching parts of the brief "
            "and its acceptance:"
        )
    if kind == KIND_FACT:
        return f"**Facts** (the code at {base} — no judgment involved)"
    return "**Decisions**"


# --- the pass -----------------------------------------------------------------


@dataclass(frozen=True)
class BriefInputs:
    """What the reviewer reads about the story. *brief* is the description
    as dispatch would hand it to the coder (any approved addendum included);
    *acceptance_criteria* is the story's explicit
    ``metadata.acceptance_criteria`` (``lithos_io.explicit_acceptance_criteria``),
    which the coder's prompt carries as its own section — so the reviewer
    checks it too (PR #448 review); *written_at* is the story's
    ``created_at`` (ISO), the start of the history a full review reads;
    *prd* / *prd_sections* are the story's ``metadata.prd`` /
    ``metadata.prd_sections`` provenance."""

    story_id: str
    title: str
    brief: str
    prd: str | None = None
    prd_sections: str | None = None
    written_at: str | None = None
    acceptance_criteria: str | None = None


@dataclass(frozen=True)
class BriefReviewResult:
    """The pass's answer. *addendum* is ``None`` when it degraded; *note*
    then says why, and *raw* keeps whatever the agent wrote."""

    addendum: Addendum | None
    base_sha: str
    mode: str
    prior_base: str | None = None
    cost_usd: float = 0.0
    note: str = ""
    raw: str = ""


_CORRECTION_PROMPT = """\
Your file `{handoff_path}` did not pass validation:

{problems}

Rewrite the whole file in the required shape (see the instructions you were
given), fixing every problem listed. Do not shorten or drop items to make a
problem go away unless the item itself was the mistake. End your turn only
after the file is written.
"""


def review_brief(
    config: DevelopConfig,
    inputs: BriefInputs,
    *,
    base_sha: str,
    mode: str = MODE_FULL,
    prior_base: str | None = None,
    timeout: int = 1800,
) -> BriefReviewResult:
    """Run the read-only brief review at *base_sha* and return its addendum.

    Cuts a throwaway worktree detached at *base_sha* — the exact tree the
    coder will start from — writes the inputs as files, runs one fresh
    coder-engine turn in a read-only container, and validates the handoff.
    A draft that fails validation (or a missing file) gets **one**
    correction turn in the same session. *mode* :data:`MODE_DELTA` needs
    *prior_base*, the base the approved review stood on; the reviewer then
    reports only what the commits between the two bases change.

    Never raises once it starts (PR #448 review): a failed turn, no file, a
    draft still invalid after the correction, AND a runtime failure on the
    way — the worktree, the inputs, docker, a turn that raises — all come
    back as a degraded result whose ``note`` says what happened and whose
    ``raw`` keeps any text the agent had written. Only a caller's mistake
    (a delta with no *prior_base*) raises.
    """
    if mode == MODE_DELTA and not prior_base:
        raise ValueError("a delta review needs prior_base, the approved review's base")

    engine = engines.get_engine(config.coder)
    wt: Path | None = None
    name: str | None = None
    cost = 0.0
    raw = ""
    problems: list[str] = []
    addendum: Addendum | None = None
    try:
        config.run_dir.mkdir(parents=True, exist_ok=True)
        run_owner.record_owner(config.run_dir, turn_timeout_seconds=timeout)
        config.worktree_parent.mkdir(parents=True, exist_ok=True)
        config.coder_config_dir.mkdir(parents=True, exist_ok=True)
        handoff.seed_handoff_dir(config.handoff_dir)
        wt = worktree.create_at(
            config.repo, base_sha, config.description, parent=config.worktree_parent
        )
        # Read-only container: the handoff bind-mountpoint must pre-exist in
        # the worktree (docker cannot create it inside an RO /workspace).
        (wt / HANDOFF_MOUNT_NAME).mkdir(parents=True, exist_ok=True)
        _write_inputs(
            config, wt, inputs, base_sha=base_sha, mode=mode, prior_base=prior_base
        )
        prompt = handoff.render_prompt(
            handoff.load_prompt("brief_review.md"),
            mode_instructions=handoff.load_prompt(
                "brief_review_delta.md"
                if mode == MODE_DELTA
                else "brief_review_full.md"
            ),
            inputs_dir=_container_inputs_dir(),
            handoff_file=BRIEF_REVIEW_HANDOFF_NAME,
        )
        name, run_cmd = build_run_cmd(
            config,
            agent="brief-review",
            engine=engine,
            config_dir=config.coder_config_dir,
            wt=wt,
            read_only=True,
        )
        containers.start_container(run_cmd)
        session_id = str(uuid.uuid4())
        resume = False
        for attempt in (1, 2):
            turn = turns.run_turn(
                container=name,
                prompt=prompt,
                engine=engine,
                session_id=session_id,
                resume=resume,
                timeout=timeout,
                model=config.coder_model,
                effort=config.coder_effort,
            )
            cost += turn.cost_usd or 0.0
            if not turn.succeeded:
                return _degraded(
                    f"the brief-review turn failed (attempt {attempt})",
                    base_sha,
                    mode,
                    prior_base,
                    cost,
                    raw,
                )
            raw, addendum, problems = _read_draft(config)
            if not problems:
                break
            session_id, resume = turn.session_id or session_id, True
            prompt = handoff.render_prompt(
                _CORRECTION_PROMPT,
                handoff_path=f"{WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}/{BRIEF_REVIEW_HANDOFF_NAME}",
                problems="\n".join(f"- {p}" for p in problems),
            )
    except Exception as exc:  # noqa: BLE001 — degrade, never raise (see above)
        logger.exception("brief review %s could not run", config.run_id)
        return _degraded(
            f"the brief review could not run: {exc}",
            base_sha,
            mode,
            prior_base,
            cost,
            raw,
        )
    finally:
        if name is not None:
            containers.stop_container(name)
        if wt is not None:
            try:
                worktree.remove(wt, force=True)
            except Exception:  # noqa: BLE001 — cleanup only
                logger.warning("brief review: failed to remove worktree %s", wt)

    if problems:
        joined = "; ".join(problems)
        what = (
            "wrote no brief-review file"
            if not raw
            else f"wrote a draft that did not validate: {joined}"
        )
        return _degraded(
            f"the reviewer {what} (after one correction turn)",
            base_sha,
            mode,
            prior_base,
            cost,
            raw,
        )
    logger.info(
        "brief review %s (%s at %s): %d cut(s) / %d fact(s) / %d decision(s), $%.2f",
        config.run_id,
        mode,
        base_sha[:12],
        len(addendum.scope_cuts) if addendum else 0,
        len(addendum.facts) if addendum else 0,
        len(addendum.decisions) if addendum else 0,
        cost,
    )
    return BriefReviewResult(
        addendum=addendum,
        base_sha=base_sha,
        mode=mode,
        prior_base=prior_base,
        cost_usd=cost,
        raw=raw,
    )


def _container_inputs_dir() -> str:
    return f"{WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}/artifacts/{INPUTS_DIR_NAME}/"


def _write_inputs(
    config: DevelopConfig,
    wt: Path,
    inputs: BriefInputs,
    *,
    base_sha: str,
    mode: str,
    prior_base: str | None,
) -> None:
    """Write the reviewer's inputs under the read-only artifacts mount."""
    out = config.artifacts_dir / INPUTS_DIR_NAME
    out.mkdir(parents=True, exist_ok=True)
    brief = f"# {inputs.title}\n\n{inputs.brief.rstrip()}\n"
    if inputs.acceptance_criteria:
        # The section the coder's round-1 prompt gets (rounds.py), in the
        # same words: the criteria are part of what the coder builds to.
        brief += f"\n## Acceptance criteria\n\n{inputs.acceptance_criteria.strip()}\n"
    (out / "brief.md").write_text(brief, encoding="utf-8")
    story = [
        "# The story under review",
        "",
        f"- story id: {inputs.story_id}",
        f"- base (the tree at /workspace): {base_sha}",
        f"- PRD: {inputs.prd or '(none recorded)'}",
        f"- PRD sections: {inputs.prd_sections or '(none recorded)'}",
        f"- brief written: {inputs.written_at or '(unknown)'}",
        f"- mode: {mode}",
    ]
    if mode == MODE_DELTA and prior_base:
        story.append(f"- the approved review's base: {prior_base}")
    (out / "story.md").write_text("\n".join(story) + "\n", encoding="utf-8")

    if mode == MODE_DELTA and prior_base:
        log = _git_or_note(lambda: git.log_between(wt, prior_base, "HEAD"))
        stat = _git_or_note(lambda: git.diff_stat(wt, prior_base))
        history = (
            f"# Commits from {prior_base[:12]} to {base_sha[:12]} (first-parent, "
            f"oldest first)\n\n{log or '(none)'}\n\n# Files changed\n\n"
            f"{stat or '(none)'}\n"
        )
    elif inputs.written_at:
        log = _git_or_note(lambda: git.log_since(wt, inputs.written_at or "", "HEAD"))
        history = (
            f"# Merged since the brief was written ({inputs.written_at}), "
            f"oldest first\n\n{log or '(none)'}\n"
        )
    else:
        history = "# History\n\n(the brief's creation time is unknown)\n"
    (out / "history.md").write_text(history, encoding="utf-8")


def _git_or_note(read: Callable[[], str]) -> str:
    """Run a git read for the inputs; a failure becomes a visible note, never
    a raise — the reviewer can still read the tree."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 — an input, not the verdict
        logger.warning("brief review: git read failed: %s", exc)
        return f"(could not read the history: {exc})"


def _read_draft(config: DevelopConfig) -> tuple[str, Addendum | None, list[str]]:
    """``(raw text, parsed addendum, problems)`` of the agent's handoff."""
    try:
        raw = handoff.read_handoff(config.handoff_dir / BRIEF_REVIEW_HANDOFF_NAME)
    except OSError:
        return (
            "",
            None,
            [
                f"no file at {WORKSPACE_MOUNT}/{HANDOFF_MOUNT_NAME}/"
                f"{BRIEF_REVIEW_HANDOFF_NAME} — write it before ending your turn"
            ],
        )
    raw = handoff.sanitize_agent_text(raw)
    addendum = parse_addendum(raw)
    return raw, addendum, validate_addendum(addendum)


def _degraded(
    note: str,
    base_sha: str,
    mode: str,
    prior_base: str | None,
    cost: float,
    raw: str,
) -> BriefReviewResult:
    logger.warning("brief review: %s", note)
    return BriefReviewResult(
        addendum=None,
        base_sha=base_sha,
        mode=mode,
        prior_base=prior_base,
        cost_usd=cost,
        note=note,
        raw=raw,
    )

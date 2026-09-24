"""Handoff directory, filenames, and structured-finding parsing.

A handoff is the structured-markdown sign-off an agent writes per turn (see
``prompts/FORMAT.md``). T1 only seeded the dir; T2 adds parsing + validation of
the reviewer's findings block and the LGTM / severity-threshold verdict.

The parser is deliberately line-based and tolerant rather than strict YAML —
agent output varies, and a malformed handoff should be *re-promptable*, not a
crash. Validation raises :class:`HandoffError` with a human message that is fed
back to the agent as a correction prompt.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .publish_text import CONTROL_CHARS_RE

_PROMPTS = "lithos_loom.plugins.story_develop.prompts"

# --- severity (ported from Ralph++ tools/base.py) --------------------------

_SEVERITIES = ("minor", "major", "critical")
_SEVERITY_ORDER = {s: i for i, s in enumerate(_SEVERITIES)}

# Finding lifecycle states (T7 enforces transitions; T2 only parses/validates).
# `out-of-scope` (819370e5) is the reviewer's escape for a finding that is REAL
# but not this story's to fix (pre-existing on the base, a harness/pipeline
# fault, another story's agreed work): resolved — so it never blocks — and
# spun out as its own Lithos task at run end (see lithos_io.spawn_deferred_tasks)
# instead of burning the round budget. It REQUIRES a `deferral_reason` saying
# why (the parse rejects it otherwise): the disposition is a licence to
# not-block, and the stated why is its counterweight. The why lives in its OWN
# key — never in `rationale`, which keeps describing WHAT the defect is — so
# the spawned task always carries both texts (PR #342 re-review P1).
# `needs-decision` (9d5ebca6) is the CODER's escape for a finding neither side
# can settle by re-reading the code — the acceptance names something the
# product does not have, or asks for a guarantee the platform cannot give.
# It is a dispute PLUS a question for the operator: open (so it still blocks),
# carrying a `decision_question` / `decision_options` block. A reviewer that
# can show the finding is in scope contests it with `decision_contest:` (the
# acceptance line it meets) and it degrades to an ordinary `disputed`.
_OPEN_STATES = frozenset({"open", "disputed", "needs-clarification", "needs-decision"})
_RESOLVED_STATES = frozenset(
    {"fixed", "accepted", "superseded", "merged", "out-of-scope"}
)
_ALL_STATES = _OPEN_STATES | _RESOLVED_STATES
# The reviewer's answer to a pending `needs-decision` (security/f-003): an
# explicit act, recorded in the ledger, never inferred from an absent key.
_DECISION_VERDICTS = frozenset({"contest", "concede"})
DECISION_VERDICTS = _DECISION_VERDICTS
# Public alias: the eval harness validates retained-report finding statuses
# against the canonical set (PR #342 review P2) without reaching for the
# private name.
ALL_FINDING_STATES = _ALL_STATES


def severity_at_or_above(severity: str, threshold: str) -> bool:
    """True if *severity* meets or exceeds *threshold* (minor < major < critical)."""
    return _SEVERITY_ORDER[severity.lower()] >= _SEVERITY_ORDER[threshold.lower()]


def max_severity(severities: list[str]) -> str | None:
    """Highest severity in the list, or ``None`` when empty."""
    if not severities:
        return None
    return max((s.lower() for s in severities), key=lambda s: _SEVERITY_ORDER[s])


# Handoff bodies are agent-written (the dir is bind-mounted RW into the agent
# containers), so every free-text field parsed out of one is untrusted input on
# its way to screens an operator DECIDES on: the reviewer prompt, the terminal
# epilogue, the `[ReviewDispute]` / `[NeedsHuman]` findings, the needs-human
# gate's description. Strip the bytes that let text render differently from
# what it carries — C0/C1 (ANSI escapes forge or erase a line, CWE-150/CWE-117)
# plus the bidi overrides / isolates and the zero-width formatters (trojan
# source) — keeping TAB and LF, since folded scalars are multi-line.
#
# The class is `publish_text.CONTROL_CHARS_RE`, the ONE definition of it
# (security/f-002). It used to be copied here, and into `cli/develop._sanitize`
# and `cli/_deliver_facts.sanitize_for_terminal`, as three literals declared
# identical by comment — and they drifted: the canonical one now covers
# `Default_Ignorable_Code_Point` in full, and the copies did not, so a
# `rationale` of nothing but U+E0001 tag characters or U+061C sanitised to
# itself and passed the non-blank check below (a follow-up task whose rationale
# renders as nothing — exactly the hazard that check exists for). `publish_text`
# is this package's own module, so there is no cross-component edge to spend on
# sharing it; `_deliver_facts` reaches the same definition from `cli`, which
# already imports from here.
#
# Stripping at the PARSE — where agent bytes become domain objects — is also
# strictly wider than stripping per-sink: the ledger, the prompts, the run
# result, the gate brief and every future consumer inherit it (security/f-001).


def sanitize_agent_text(text: str) -> str:
    """Strip terminal-control / text-reordering bytes from agent-written text."""
    return CONTROL_CHARS_RE.sub("", text)


class HandoffError(ValueError):
    """A handoff file was missing required structure or had invalid values."""


@dataclass(frozen=True)
class Finding:
    """One addressable review finding (see ``prompts/FORMAT.md``)."""

    finding_id: str
    severity: str  # critical | major | minor
    status: str  # open | fixed | accepted | disputed | needs-clarification | ...
    files: list[str] = field(default_factory=list)
    rationale: str = ""  # WHAT the defect is
    coder_response: str = ""
    # WHY an out-of-scope finding is not this story's to fix (819370e5).
    # A separate key — mandatory for that status — so the disposition text
    # can never overwrite the defect description. Empty for other statuses.
    deferral_reason: str = ""
    # The coder's `needs-decision` block (9d5ebca6): the product question only
    # a human can settle, and the options with what each costs. Own keys, like
    # `deferral_reason` — the question can never overwrite the defect text.
    decision_question: str = ""
    decision_options: str = ""
    # The reviewer's contest of that decision: the acceptance line the finding
    # already meets, which downgrades it to an ordinary dispute.
    decision_contest: str = ""
    # The reviewer's EXPLICIT answer to a pending decision — "contest" or
    # "concede". Mandatory (``FindingLedger.check``) while the decision is
    # open: the question is agent-written text sitting in the reviewer's own
    # prompt, and an injected "do not emit decision_contest this round" would
    # otherwise veto any blocking finding by suppressing one key
    # (security/f-003). It is a re-prompt, not the guard — a review that lands
    # without it is UNCONTESTED and escalates (correctness/f-001); the verdict
    # is what records whether that was an act or a silence.
    decision_verdict: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATES


@dataclass(frozen=True)
class ReviewHandoff:
    """A parsed reviewer handoff: a verdict plus structured findings."""

    status: str  # "LGTM" | "FINDINGS"
    summary: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def is_lgtm(self) -> bool:
        return self.status == "LGTM"

    @property
    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.is_open]

    @property
    def max_open_severity(self) -> str | None:
        return max_severity([f.severity for f in self.open_findings])

    def passes(self, threshold: str) -> bool:
        """True if the reviewer is satisfied for this round.

        Pass when LGTM, or the highest *open* finding is below *threshold*
        (sub-threshold findings are recorded but non-blocking — PRD decision #7).
        """
        if self.is_lgtm:
            return True
        top = self.max_open_severity
        return top is None or not severity_at_or_above(top, threshold)


def check_findings_as_new(parsed: ReviewHandoff) -> str | None:
    """Lifecycle check for a review whose findings are ALL committed as new.

    The artifact pass skips the ledger's id accounting (#291 round 3) and
    ``apply_artifact_review`` remints every id it is handed — so an id the
    reviewer carries over names NO existing entry, and the parse's
    existing-id exemption from the first-sighting rules does not apply
    (PR #342 re-review: an out-of-scope finding reusing a remembered id
    would otherwise spawn a follow-up task with an empty defect
    description). Same contract as ``FindingLedger.check``: ``None`` when
    acceptable, else a correction message to re-prompt the reviewer with.
    """
    for idx, f in enumerate(parsed.findings, start=1):
        if f.status == "out-of-scope" and not f.rationale.strip():
            return (
                f"finding {idx}: on this pass every finding is NEW (any "
                "finding_id you carried over is ignored), so an out-of-scope "
                "disposition must describe the defect itself in 'rationale:' "
                "— 'deferral_reason:' holds only why it is not this story's "
                "to fix"
            )
    return None


# --- prompt + filename helpers ---------------------------------------------


def load_prompt(name: str) -> str:
    """Read a packaged prompt template (e.g. ``coder_init.md``)."""
    return resources.files(_PROMPTS).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, **values: str) -> str:
    """Placeholder substitution that is safe against braces in the values.

    Lives here (ARCH-1.S5) with :func:`load_prompt` so a caller that only needs
    to render a prompt (e.g. ``pr_delivery``) never has to import the reviewer
    panel machinery. ``develop`` re-exports it as ``_render`` until S7.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def render_findings(findings: list[Finding]) -> str:
    """Render a reviewer's findings as a compact block for the coder's prompt."""
    if not findings:
        return "(no structured findings were listed)"
    lines: list[str] = []
    for f in findings:
        files = ", ".join(f.files) if f.files else "(unspecified)"
        lines.append(f"- [{f.finding_id}] severity={f.severity} status={f.status}")
        lines.append(f"  files: {files}")
        if f.rationale:
            lines.append(f"  rationale: {f.rationale}")
        if f.deferral_reason:
            lines.append(f"  deferral_reason: {f.deferral_reason}")
        if f.decision_contest:
            # 9d5ebca6: the reviewer contested the coder's needs-decision —
            # the coder must see the acceptance line it was shown, since the
            # finding is now an ordinary dispute under the usual guard.
            #
            # The citation is the REVIEWER's own text arriving in the coder's
            # prompt — the mirror of the leg `render_open` already quotes, and
            # the privileged direction: the coder edits the tree. Multi-line
            # here is the NORMAL case, not the adversarial one (the field is
            # defined as a quoted acceptance clause and `reviewer_rereview.md`
            # asks for one, while the fold parser joins with "\n"), so rendered
            # bare its lines 2+ land at column 0 and can forge another
            # `- [id] …` entry of this very block, or a heading above loom's
            # own (security/f-001). Quoted line-by-line, they cannot.
            lines.append("  decision_contest (AGENT INPUT — quoted data):")
            lines += quote_agent_block("cites", f.decision_contest)
    return "\n".join(lines)


def quote_agent_block(label: str, text: str) -> list[str]:
    """*text* as quoted, indented lines under *label* — one prompt line per
    source line, so multi-line agent text cannot leave the block it was put in.

    Both legs of the panel's agent-to-agent channel use it: the coder's
    question / options / response on their way into the adjudicating
    reviewer's prompt (:meth:`~.findings.FindingLedger.render_open`,
    security/f-003 / f-006) and the reviewer's ``decision_contest:`` citation
    on its way into the coder's (:func:`render_findings`, security/f-001).
    Lives here because it belongs to the prompt rendering, and because
    :mod:`.findings` imports this module (never the reverse).
    """
    body = text.strip().splitlines() or [""]
    return [f"    {label}> {line}" for line in body]


def coder_handoff_name(round_no: int) -> str:
    """Filename for the coder's handoff in a given round (1-based)."""
    return f"round_{round_no:02d}_coder_done.md"


def reviewer_handoff_name(round_no: int, reviewer: str) -> str:
    """Filename for a reviewer's handoff in a given round."""
    return f"round_{round_no:02d}_review_{reviewer}.md"


def _read_or_missing(path: Path) -> str:
    """Body of a handoff file, or a placeholder if it was never written."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return "_(no handoff file was written)_"


def _blockquote(text: str) -> str:
    """Quote *text* so its own ``##`` headings nest under the log's structure.

    Handoffs are markdown with top-level ``## Status`` / ``## Summary`` headings;
    inlined raw they would become siblings of the log's ``## Round N`` headings.
    A blockquote keeps them visually subordinate, and (unlike a code fence)
    cannot collide with fences inside the handoff body.
    """
    return "\n".join(f"> {line}".rstrip() for line in text.splitlines())


def render_log_section(
    handoff_dir: Path, header: str, entries: Sequence[tuple[str, str]]
) -> list[str]:
    """Render one conversation-log section as a list of lines (the caller joins).

    *header* is the section heading (e.g. ``"## Round 2"``); each ``(label,
    filename)`` entry becomes a ``### {label} — `{filename}` `` sub-heading
    followed by that handoff file's blockquoted body (or a placeholder when the
    file was never written, so gaps stay visible). The section shape — heading +
    read-or-missing + blockquote — lives here so :func:`conversation_log` (per
    develop round) shares one renderer
    instead of each reaching the private helpers (ARCH-1.S7).
    """
    parts = [header, ""]
    for label, filename in entries:
        parts += [
            f"### {label} — `{filename}`",
            "",
            _blockquote(_read_or_missing(handoff_dir / filename)),
            "",
        ]
    return parts


def conversation_log(handoff_dir: Path, rounds: int, reviewers: Sequence[str]) -> str:
    """Assemble an ordered, human-readable log of every round's handoffs.

    For each round 1..*rounds* it inlines the coder's done-handoff followed by
    each reviewer's review (panel order), so the whole implement→review→fix
    dialogue reads top to bottom. Missing files (e.g. a round where the coder
    turn failed) are rendered as a placeholder rather than omitted, so gaps
    stay visible. Handoff bodies are blockquoted so their own headings don't
    break the log's ``## Round N`` structure.
    """
    parts = ["# story-develop conversation log", ""]
    for r in range(1, rounds + 1):
        entries = [("Coder", coder_handoff_name(r))]
        entries += [
            (f"Reviewer [{reviewer}]", reviewer_handoff_name(r, reviewer))
            for reviewer in reviewers
        ]
        # #283/#291: an artifact-review pass writes its verdict to its own
        # per-reviewer file — the review that actually controlled approval
        # belongs in the audit trail. Optional: rendered only when present.
        for reviewer in reviewers:
            name = reviewer_handoff_name(r, f"{reviewer}_artifacts")
            if (handoff_dir / name).is_file():
                entries.append((f"Reviewer [{reviewer}] (artifact pass)", name))
        parts += render_log_section(handoff_dir, f"## Round {r}", entries)
    return "\n".join(parts) + "\n"


def seed_handoff_dir(handoff_dir: Path) -> Path:
    """Create *handoff_dir* and write ``FORMAT.md`` into it.

    *handoff_dir* lives outside the git worktree and is mounted into the
    container at ``/workspace/.handoff``. Returns the directory path.
    """
    handoff_dir.mkdir(parents=True, exist_ok=True)
    (handoff_dir / "FORMAT.md").write_text(load_prompt("FORMAT.md"), encoding="utf-8")
    return handoff_dir


# --- parsing ----------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*#{1,6}\s+(.*?)\s*$")
_STATUS_RE = re.compile(r"status\s*:\s*([A-Za-z_-]+)", re.IGNORECASE)
_ITEM_RE = re.compile(r"^\s*-\s*(.*)$")
_KV_RE = re.compile(r"^\s*([A-Za-z_]+)\s*:\s*(.*)$")


def _sections(text: str) -> dict[str, str]:
    """Split markdown into ``{lowercased-header: body}`` by ``##`` headers."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            # normalise a trailing colon: "## Findings:" -> "findings" (tolerant).
            header = re.sub(r"\s*:\s*$", "", m.group(1).strip().lower())
            current = header
            sections.setdefault(header, [])
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _parse_status(sections: dict[str, str], full: str) -> str:
    """Resolve the LGTM/FINDINGS verdict from the ``Status`` header or text."""
    raw = ""
    for key, body in sections.items():
        if key.startswith("status"):
            # header may be "status: lgtm" or body may hold it
            raw = key[len("status") :].lstrip(": ").strip() or body.strip()
            break
    if not raw:
        m = _STATUS_RE.search(full)
        raw = m.group(1) if m else ""
    token = raw.strip().lower()
    if "lgtm" in token:
        return "LGTM"
    if "finding" in token:
        return "FINDINGS"
    raise HandoffError("missing or invalid '## Status:' — must be 'LGTM' or 'FINDINGS'")


def _split_files(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    parts = [p.strip().strip("\"'") for p in value.split(",")]
    return [p for p in parts if p]


_FOLD_MARKERS = (">", "|", ">-", "|-", ">+", "|+")


def _parse_findings(block: str) -> list[Finding]:
    """Parse the ``## Findings`` body into a list of :class:`Finding`.

    Supports YAML-style folded/literal scalars (``rationale: >`` / ``|`` with
    indented continuation lines) — reviewers write them in practice, and
    dropping the text silently would starve the lifecycle ledger (T7) and the
    coder prompts of the rationale.

    A finding with no id is left with ``finding_id=""`` — canonical ids are
    assigned by the orchestrator's ledger, never invented here (a per-file
    fallback would collide across rounds).
    """
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    fold_key: str | None = None  # key currently accumulating folded lines
    fold_indent = 0
    fold_lines: list[str] = []

    def _flush_fold() -> None:
        nonlocal fold_key, fold_lines
        if current is not None and fold_key is not None:
            current[fold_key] = "\n".join(fold_lines).strip()
        fold_key, fold_lines = None, []

    def _start_kv(target: dict[str, str], key: str, value: str, indent: int) -> None:
        nonlocal fold_key, fold_indent, fold_lines
        if value in _FOLD_MARKERS:
            fold_key, fold_indent, fold_lines = key, indent, []
        else:
            target[key] = value

    for line in block.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if fold_key is not None:
            # Inside a folded scalar, indentation alone decides: any
            # MORE-indented line is content — including bullet lists, which
            # are common in YAML text blocks. A new finding item ("- ...")
            # sits at or left of the key's indent and ends the fold below.
            if line.strip() and indent > fold_indent:
                fold_lines.append(line.strip())
                continue
            if not line.strip():
                fold_lines.append("")
                continue
            _flush_fold()
        item = _ITEM_RE.match(line)
        if item:  # new list entry: "- finding_id: ..."
            current = {}
            items.append(current)
            rest = item.group(1)
            kv = _KV_RE.match(rest)
            if kv:
                _start_kv(current, kv.group(1).lower(), kv.group(2).strip(), indent)
            continue
        if current is None:
            continue
        kv = _KV_RE.match(line)
        if kv:
            _start_kv(current, kv.group(1).lower(), kv.group(2).strip(), indent)
    _flush_fold()

    findings: list[Finding] = []
    for idx, item in enumerate(items, start=1):
        # Sanitize BEFORE any mandatory-field check (correctness/f-004): a
        # `rationale` that is only U+200B and a `deferral_reason` that is only
        # U+202E both pass a `.strip()` non-blank test and then sanitize to the
        # empty string — the parse would admit a deferral that spawns a
        # follow-up task carrying neither the defect nor the why. Validating
        # the CLEANED values is the same rule the fields already state, applied
        # to the text that will actually exist.
        raw = {k: sanitize_agent_text(v) for k, v in item.items()}
        severity = raw.get("severity", "").strip().lower()
        if severity not in _SEVERITY_ORDER:
            raise HandoffError(
                f"finding {idx}: severity must be one of "
                f"{', '.join(_SEVERITIES)} (got {severity!r})"
            )
        status = (raw.get("status") or "open").strip().lower()
        if status not in _ALL_STATES:
            raise HandoffError(
                f"finding {idx}: invalid status {status!r} "
                f"(allowed: {', '.join(sorted(_ALL_STATES))})"
            )
        if status == "out-of-scope":
            if not raw.get("deferral_reason", "").strip():
                raise HandoffError(
                    f"finding {idx}: an out-of-scope disposition must carry a "
                    "'deferral_reason:' stating WHY the finding is not this "
                    "story's to fix — e.g. pre-existing on the base, a "
                    "harness/pipeline fault, or another story's agreed work. "
                    "Keep 'rationale:' describing WHAT the defect is."
                )
            is_new = not (raw.get("finding_id") or raw.get("id") or "").strip()
            if is_new and not raw.get("rationale", "").strip():
                # An existing id already has a defect description in the
                # ledger; a first sighting has nowhere else to get one, and a
                # deferral whose spawned task names only the why is
                # unactionable (PR #342 re-review P1).
                raise HandoffError(
                    f"finding {idx}: a NEW finding deferred as out-of-scope "
                    "must still describe the defect itself in 'rationale:' — "
                    "that text is what the spawned follow-up task carries; "
                    "'deferral_reason:' holds only why it is not this "
                    "story's to fix"
                )
        verdict = raw.get("decision_verdict", "").strip().lower()
        if verdict and verdict not in _DECISION_VERDICTS:
            raise HandoffError(
                f"finding {idx}: invalid decision_verdict {verdict!r} "
                f"(allowed: {', '.join(sorted(_DECISION_VERDICTS))})"
            )
        findings.append(
            Finding(
                finding_id=(raw.get("finding_id") or raw.get("id") or "").strip(),
                severity=severity,
                status=status,
                files=_split_files(raw.get("files", "")),
                rationale=raw.get("rationale", ""),
                coder_response=raw.get("coder_response", ""),
                deferral_reason=raw.get("deferral_reason", ""),
                decision_question=raw.get("decision_question", ""),
                decision_options=raw.get("decision_options", ""),
                decision_contest=raw.get("decision_contest", ""),
                decision_verdict=verdict,
            )
        )
    return findings


def parse_review_handoff(text: str) -> ReviewHandoff:
    """Parse + validate a reviewer handoff. Raises :class:`HandoffError`.

    The error message is suitable to feed back to the agent as a correction.
    """
    if not text.strip():
        raise HandoffError("handoff file is empty")
    sections = _sections(text)
    status = _parse_status(sections, text)
    summary = sections.get("summary", "").strip()
    findings = (
        _parse_findings(sections.get("findings", "")) if "findings" in sections else []
    )
    if status == "FINDINGS" and not findings:
        raise HandoffError(
            "Status is FINDINGS but no '## Findings' entries were parsed"
        )
    return ReviewHandoff(status=status, summary=summary, findings=findings)


def file_fingerprint(path: Path) -> str | None:
    """Content identity of a handoff file (``None`` = absent / unreadable).

    Salvage provenance (#298 / PR #299 review; the coder twin in slice B): a
    failed attempt may only salvage an artifact it *itself* created or
    rewrote, so each attempt snapshots the file before running and compares
    after.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None

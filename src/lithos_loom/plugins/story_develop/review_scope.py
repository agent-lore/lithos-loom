"""The operational model findings are judged in (review-convergence R1).

Five runs stalled the same way in 2026-09 ($495 between them): the correctness
reviewer judged against an implicit model in which any constructible
interleaving is a defect — a second host, a concurrent invocation of a
hand-run command, an outage longer than the claim TTL — and the coder, with
no exit that fit an operational-model question, fixed each one. Every fix
added protocol; the protocol was the next round's surface.

``develop_review_scope`` (project-context doc, task-overridable) states the
model once per project. It renders under the acceptance criteria in every
reviewer, triage and coder prompt; the reviewer block makes a finding that
needs an actor the model and the criteria do not name **minor at most**, and
the coder route sends such a finding to the operator as a ``needs-decision``
(story-develop) or a dispute quoting the model (converge, which has no
operator gate behind it). Unset, every block renders as nothing.
"""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

from .handoff import sanitize_agent_text

if TYPE_CHECKING:
    from .config import DevelopConfig

# Operator-authored, but quoted verbatim into every prompt of every round —
# a model is a paragraph, not a document.
MAX_REVIEW_SCOPE_CHARS = 4000
# A ``{name}`` shape: :func:`.handoff.render_prompt` is a sequential
# ``str.replace`` per slot, so a model containing ``{findings}`` would have a
# LATER slot's value spliced into it (R1 review).
_SLOT_RE = re.compile(r"\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}")


def parse_review_scope(value: object, *, where: str) -> str | None:
    """Validate a ``develop_review_scope`` text, or ``None`` (layer unset).

    A non-empty string, stripped and control-byte-cleaned, at most
    :data:`MAX_REVIEW_SCOPE_CHARS`, with no ``{name}`` slot shape in it.
    Raises :class:`ValueError` (the shared parser contract the settings
    resolver turns into a friction).
    """
    if value is None:
        return None
    text = sanitize_agent_text(value).strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(
            f"{where}: develop_review_scope must be non-empty text (got {value!r})"
        )
    slot = _SLOT_RE.search(text)
    if slot:
        raise ValueError(
            f"{where}: develop_review_scope contains {slot.group(0)!r}, which has "
            "the shape of a prompt slot and would be substituted — reword it"
        )
    if len(text) > MAX_REVIEW_SCOPE_CHARS:
        raise ValueError(
            f"{where}: develop_review_scope is {len(text)} characters — at most "
            f"{MAX_REVIEW_SCOPE_CHARS}"
        )
    return text


def reviewer_block(config: DevelopConfig) -> str:
    """The model + how findings are judged in it, for reviewer/triage prompts.

    Appended to the acceptance-criteria line (``{acceptance_criteria}{review_scope}``)
    so it sits directly under the criteria and an unset scope leaves no trace.
    """
    if not config.review_scope:
        return ""
    return (
        "\n\n## Operational model (the scope findings are judged in)\n\n"
        f"{config.review_scope}\n\n"
        "Judge every finding inside this model. A defect whose failure needs an "
        "actor or condition that neither the acceptance criteria nor this model "
        "places in scope — one the model excludes, or one neither mentions — is "
        "**minor at most**; say in its rationale which actor or condition it "
        "needs. An explicit exclusion in the model is evidence FOR that cap, "
        "never a reason to block, unless the story's acceptance criteria bring "
        "that actor into scope. A defect inside the model — including a "
        "lifecycle the acceptance criteria describe — keeps its full severity. "
        "When the coder disputes a finding as needing an actor outside the "
        "model, accept the dispute unless you can quote the criterion or model "
        "line that brings that actor into scope."
    )


def coder_block(config: DevelopConfig) -> str:
    """The model, for the coder: what to build for."""
    if not config.review_scope:
        return ""
    return (
        "\n\n## Operational model (the scope your change is judged in)\n\n"
        f"{config.review_scope}\n\n"
        "Build for this model; do not add protocol for actors or conditions it "
        "excludes or leaves out. The story's acceptance criteria can widen it "
        "for this story."
    )


def coder_route(config: DevelopConfig, *, decisions_enabled: bool) -> str:
    """The coder's exit for a finding outside the model.

    Story-develop (and resume) put the question to the operator as a
    ``needs-decision``; converge has no operator gate behind it (its ledger
    records the mark as a dispute), so there the exit is a dispute that
    quotes the model.
    """
    if not config.review_scope:
        return ""
    head = (
        "**A finding outside the operational model.** When a finding's failure "
        "needs an actor or condition that neither the acceptance criteria nor the "
        "operational model above places in scope (one the model excludes, or one "
        "neither mentions)"
    )
    if not decisions_enabled:
        return _list_paragraph(
            f"{head}, do not add protocol for it: mark it `disputed`, and in "
            "`coder_response:` quote the operational model line it falls outside "
            "and name the actor or condition the failure needs."
        )
    return _list_paragraph(
        f"{head} — or when the criteria do not settle how deep an in-model "
        "contract goes (does it cover one more failure on top of the ones they "
        "name?) — neither fix it nor merely dispute it. Mark it `needs-decision` "
        'with `decision_question:` "Is <actor or condition> in scope for <the '
        'command or behaviour>?" (or "How deep does <contract> go — does it cover '
        '<case>?") and `decision_options:` (a) widen the model and fix — what '
        "that costs; (b) keep the model and record the limit — what that costs. "
        "That question is the operator's to answer, not the code's."
    )


def _list_paragraph(text: str) -> str:
    """*text* as a paragraph inside the coder prompts' numbered step 1.

    Indented like the needs-decision fragment it sits beside, so it stays part
    of the list item rather than ending it; it is appended to the line before
    it (``{decision_escape}{scope_route}``, ``…forever.{scope_route}``), so an
    unset scope leaves the template's render byte-identical.
    """
    lines = textwrap.wrap(
        text, width=77, break_long_words=False, break_on_hyphens=False
    )
    return "\n\n" + "\n".join(f"   {line}" for line in lines)

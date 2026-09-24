"""How ``develop deliver`` reports itself — the terminal and the story.

The fourth piece of the command (beside :mod:`cli._deliver_facts`,
:mod:`cli._deliver_repo` and :mod:`cli._deliver_lithos`): the ``--dry-run``
plan, the end-of-run render, and the ``[ManualDelivery]`` summary posted on the
story. The first three are pure functions of the facts and the record, so what the
operator is told has tests of its own and the command module stays the five
steps and their flags; :func:`file_record` is the fourth surface — the
``--json`` record the operator scripts against, whose absence is itself
reported as a partial result.

One rule runs through them: **say what was actually done**. A gate attempt that
failed is not a hand-off the operator chose; a gate kept open says whose it is;
a write whose outcome could not be read is neither "done" nor "nothing". And
every line loom did not author (a stop reason built from agent stdout, a story
title mirrored from a GitHub issue, ``gh`` stderr inside an exception) goes
through :func:`~lithos_loom.cli._deliver_facts.sanitize_for_terminal` before it
reaches the terminal.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import typer

from lithos_loom.cli._deliver_converge import ConvergeChain
from lithos_loom.cli._deliver_facts import (
    RunFacts,
    approval_unbound,
    sanitize_for_terminal,
    story_reason,
)
from lithos_loom.cli._deliver_lithos import GateOutcome, GateRetirement, StoryState
from lithos_loom.cli._deliver_repo import (
    PUSH_CREATE,
    PUSH_DIVERGED,
    PUSH_FAST_FORWARD,
    PUSH_UP_TO_DATE,
    PRPlan,
    RemoteState,
    pr_plan,
    remote_state,
)
from lithos_loom.plugins.story_develop import run_outcome

__all__ = [
    "MANUAL_DELIVERY",
    "delivery_finding",
    "converge_lines",
    "echo_plan",
    "file_record",
    "preview",
    "quoted_block",
    "render",
]


# Stable, machine-parseable finding prefix (see AGENTS.md): a stopped run's
# branch was delivered as a PR by hand. Distinct from `[DevelopResult]` (a run
# reporting its own outcome) because nothing ran here — no rounds, no spend,
# no verdict; the operator moved existing work onto the maintained path, and
# an operator grepping for how a PR came to exist wants that difference.
MANUAL_DELIVERY = "[ManualDelivery]"


def delivery_finding(
    *,
    facts: RunFacts,
    record: Mapping[str, Any],
    outcome: GateOutcome | None,
    notes: Sequence[str],
    no_gate: bool,
    live_gate_id: str | None = None,
) -> str:
    """The ``[ManualDelivery]`` summary posted on the story (pure).

    *notes* is the ONE source of friction — the caller folds every gate-phase
    problem into it as it lands, so reading ``outcome.problems`` here too would
    name each of them twice; *outcome* says only what the gate phase achieved.

    *live_gate_id* is the open ``pr`` gate the STORY already carries for this
    PR, if any. It is what keeps the record from contradicting itself: a
    ``--no-gate`` pass over an already-gated PR raises nothing but must not
    call that PR unmonitored.
    """
    verb = "adopted" if record.get("adopted") else "opened"
    parts = [
        f"{MANUAL_DELIVERY} run {facts.run_id or '(unknown)'} delivered by hand: "
        f"{verb} {record.get('pr_url')}"
    ]
    sha = str(record.get("pushed_sha") or "")[:12]
    # Always name the commit this delivery put behind the PR — an audit that
    # says only "branch X" cannot be checked later. The verb distinguishes a
    # push this run made from a ref that was already on origin.
    if record.get("pushed"):
        parts.append(f"pushed {sha} to branch {facts.branch}")
    elif sha:
        parts.append(f"branch {facts.branch} already on origin at {sha}")
    else:
        parts.append(f"branch {facts.branch}")
    # The stop reason, whole. The PR body publishes only a redacted 200-char
    # rendering and tells its reader the story carries the rest — and the path
    # this command exists for is exactly the one where nothing else on the
    # story does (a daemon that died before posting its [NeedsHuman] finding),
    # so this finding is what makes that pointer true.
    reason = story_reason(facts).text
    if facts.status == run_outcome.APPROVED:
        # the approved salvage path (#194 / #189): the panel DID approve, and
        # what stopped is the run's own delivery — "stopped approved" is neither
        said = "the run was approved and its own PR delivery never completed"
        # …but the approval was given on a revision and a story, and the audit
        # copy says so whenever this delivery is not that pair (f-004): the
        # story's record must not read as a review of what was delivered here.
        # Against the head GitHub reports for the PR — never against the sha
        # this delivery pushed. The claim is about the revision the PR
        # actually delivers (deliver.py step 2b), so a head that could not be
        # read leaves it UNBOUND ("") and the approval is downgraded; falling
        # back to what we pushed would turn "could not ask" into a match.
        unbound = approval_unbound(
            facts, delivered_head=str(record.get("pr_head_sha") or "")
        )
        if unbound:
            said += f" — but it is NOT confirmed for this revision: {unbound}"
        parts.append(f"{said}: {reason}" if reason else said)
    elif facts.status:
        parts.append(
            f"the run had stopped {facts.status}: {reason}"
            if reason
            else f"the run had stopped {facts.status}"
        )
    # The mode is the operator's flag, NOT "did we end up with an outcome
    # object": a gate attempt that failed leaves `outcome` None too, and
    # recording that as a deliberate hand-off would tell the operator they
    # chose the unmonitored PR the command in fact failed to gate.
    if no_gate and live_gate_id:
        # The operator asked for a PR only, but this PR is already behind its
        # own gate from an earlier pass: say what HOLDS, not what was skipped.
        parts.append(
            f"no pr gate was raised by this run (--no-gate); pr gate "
            f"{live_gate_id} already blocks the story and tracks this PR"
        )
    elif no_gate:
        parts.append(
            "no pr gate was created (--no-gate): this PR is UNMONITORED — no "
            "merge tracking, no external-review ingestion, no re-gate"
        )
    elif (outcome is None or outcome.pr_gate_id is None) and live_gate_id:
        parts.append(
            f"this run raised no pr gate (the attempt did not complete — see "
            f"the friction below), but pr gate {live_gate_id} already blocks "
            "the story and tracks this PR"
        )
    elif outcome is None or outcome.pr_gate_id is None:
        parts.append(
            "the pr gate was NOT raised (the attempt did not complete — see "
            "the friction below): this PR is UNMONITORED until it is gated, "
            "and re-running finishes the delivery"
        )
    else:
        if outcome.pr_gate_id and outcome.gate_created:
            parts.append(f"pr gate {outcome.pr_gate_id} now blocks the story")
        elif outcome.pr_gate_id:
            parts.append(f"pr gate {outcome.pr_gate_id} already blocks the story")
        for gate_id in outcome.human_gates_completed:
            parts.append(f"needs-human gate {gate_id} completed")
    if outcome is not None:
        for described in outcome.human_gates_retained:
            # Not friction: a gate this delivery does not supersede is still
            # somebody's open decision, and the description carries the why.
            parts.append(f"needs-human gate {described} left OPEN")
    summary = "; ".join(parts)
    if notes:
        summary += "\n\n[Friction] " + "; ".join(notes)
    return summary


# How untrusted text is rendered on a terminal line loom owns. Stripping the
# escape bytes (`sanitize_for_terminal`) is only half of it: LF and TAB survive
# by design, and a newline lands the next word at COLUMN 0 — a line the
# operator reads as loom's own. `--dry-run` is the screen the publish decision
# is made on (and, since the handoff quote ships in the body, the control that
# stands in for a confidentiality boundary), so nothing it displays may be able
# to forge a line of it. Every continuation is therefore indented behind a
# marker, and the whole block is bounded: volume scrolls a plan off a terminal
# as surely as a forged line replaces one.
_BLOCK_WIDTH = 72
_BLOCK_MAX_LINES = 8
_BLOCK_MARKER = "| "


def quoted_block(
    text: str, *, width: int | None = _BLOCK_WIDTH, limit: int = _BLOCK_MAX_LINES
) -> list[str]:
    """*text* as bounded display lines, each safe to print behind an indent.

    No line reaches the caller carrying its own break, and the result is capped
    at *limit* lines with a tail saying how many were dropped — the caller
    prefixes each one, so the block can only occupy the space it is given.

    *width* wraps long lines too, which is what the ``--dry-run`` screen wants:
    it is read as a fixed-shape plan, so a line long enough to soft-wrap at the
    terminal's own column 0 is the same forgery in slower motion. The
    end-of-run report passes ``None`` — its notes are sentences the operator
    greps, and only their embedded breaks are a hazard there.
    """
    out: list[str] = []
    for line in text.splitlines() or [""]:
        out.extend((textwrap.wrap(line, width=width) or [""]) if width else [line])
    if len(out) > limit:
        dropped = len(out) - limit
        out = out[:limit] + [f"… {dropped} more line(s) — see the story's record"]
    return out


def converge_lines(chain: ConvergeChain, *, pr: str, verb: str) -> list[str]:
    """``<verb> <pr> under <source>`` plus **the criteria themselves**.

    A chained converge spends money and pushes commits against this text, and
    for a mirrored story it is the GitHub issue body — anyone's to write, and
    re-synchronised by the mirror after an operator's own edit. Naming only
    the *source* ("under the story's description") would have the operator
    approve a hand-off they have never read, under a UI that tells them they
    are re-reviewing under the acceptance THEY revised. So the head of the
    text travels with the headline, bounded and shaped like every other quote
    on this screen (security/f-004). ``--ac-file`` has nothing to show: the
    file is the operator's own and converge reads it by path.
    """
    lines = [f"{verb} {pr} under {chain.ac_source}"]
    if chain.acceptance:
        lines.extend(
            f"{_BLOCK_MARKER}{line}" for line in quoted_block(chain.acceptance)
        )
    return lines


def echo_plan(
    *,
    facts: RunFacts,
    story: StoryState,
    repo: Path,
    repo_name: str,
    plan: PRPlan | None,
    state: RemoteState,
    title: str,
    no_gate: bool,
    retirement: GateRetirement,
    converge: Sequence[str] = (),
) -> None:
    push_words = {
        PUSH_CREATE: f"create origin/{facts.branch} at {state.local_sha[:12]}",
        PUSH_UP_TO_DATE: f"nothing — origin/{facts.branch} is already "
        f"{state.local_sha[:12]}",
        PUSH_FAST_FORWARD: f"fast-forward origin/{facts.branch} "
        f"{state.remote_sha[:12]} → {state.local_sha[:12]}",
        PUSH_DIVERGED: f"REFUSE — origin/{facts.branch} ({state.remote_sha[:12]}) "
        f"has diverged from {state.local_sha[:12]}",
    }

    # Every line goes through `sanitize_for_terminal`: the stop reason is agent
    # stdout / stderr and the story title can be a GitHub issue's, so an ANSI
    # escape could otherwise forge or erase the very lines the operator reads
    # to decide whether to publish this branch.
    def echo(line: str) -> None:
        typer.echo(sanitize_for_terminal(line))

    def echo_labelled(label: str, text: str, *, empty: str = "") -> None:
        """``<label><text>``, with *text* shaped to the space beside *label*.

        The label is part of the line, so wrapping the text at the block width
        alone still leaves a line the terminal soft-wraps — and a soft-wrapped
        continuation starts at column 0 just like an embedded newline does
        (:func:`quoted_block`). Sizing the first segment to what is left after
        the label is what keeps the whole visual line inside the block.
        """
        lines = quoted_block(text, width=max(24, _BLOCK_WIDTH - len(label)))
        echo(f"{label}{lines[0] if lines and lines[0] else empty}")
        for line in lines[1:]:
            echo(f"          {_BLOCK_MARKER}{line}")

    echo(f"deliver {facts.run_id or facts.branch}: dry run, nothing written")
    echo(f"  repo:   {repo} → {repo_name}")
    # A mirrored story's title is a GitHub issue title: anyone's to write, and
    # up to 256 characters of it. Shaped like every other untrusted string on
    # this screen — the break half AND the length half, since a title padded
    # to the terminal's width soft-wraps into a row at column 0 that reads as
    # a plan line of loom's own.
    echo_labelled(f"  story:  {story.story_id} [{story.status}] — ", story.title)
    # The unredacted reason stays on the operator's terminal (the PR body
    # carries only the redacted classification — see `provenance_lines`), but
    # it is `gh` / `git` stderr and arrives multi-line and unbounded: it gets
    # the story copy's own bound (`story_reason`) and this screen's line
    # shaping. An approved run has no `failure_reason` — its reason is why its
    # own delivery never landed.
    echo_labelled(
        f"  run:    {facts.status or '?'} — ", story_reason(facts).text, empty="—"
    )
    unbound = (
        approval_unbound(facts, delivered_head=state.local_sha)
        if facts.status == run_outcome.APPROVED
        else ""
    )
    if unbound:
        # the approval is about a revision + a story, and this delivery is not
        # that pair: the PR would publish the verdict downgraded, and the
        # operator decides to publish HERE
        echo(f"          approval NOT confirmed for this revision: {unbound}")
    echo(f"  1 push: {push_words[state.action]}")
    if plan is None:
        # the push above is refused, so step 2 is never reached: naming an
        # adoption decision here would describe a delivery that cannot happen
        echo("  2 PR:   not reached — the push above is refused")
    elif plan.existing is not None:
        # a projected adoption names BOTH shas: the PR is at origin's current
        # tip and step 1 above is what carries it to the delivered revision
        at = (
            f"head {plan.existing.head_sha[:12]} → {state.local_sha[:12]} "
            "after the push above"
            if plan.projected
            else f"head {plan.existing.head_sha[:12]}"
        )
        echo(
            f"  2 PR:   adopt #{plan.existing.number} {plan.existing.url} "
            f"({at}, base {plan.base}) — no body is written"
        )
    elif plan.refusal:
        # the refusal quotes GitHub's own fields back (PR titles, base refs)
        echo_labelled("  2 PR:   REFUSE — ", plan.refusal)
    else:
        echo(f"  2 PR:   open a new PR onto {plan.base}")
        # the PR title is the story's first line capped at 90 — still the
        # issue author's text, so it is shaped like the rest of it (the repr
        # quotes what is left, and the block bounds the visual line)
        echo_labelled("          title ", repr(title))
        if facts.coder_summary:
            # The one thing in the body loom did not author, shown AS IT WOULD
            # BE PUBLISHED. The redaction it has been through removes
            # recognisable shapes (urls, paths, credential-like runs); it is
            # not a confidentiality boundary — the coder agent chooses this
            # text and the encoding it is in — so the operator's read of it
            # here is the last check before it is world-readable for good.
            echo("          it quotes the coder's handoff summary, as published:")
            for line in quoted_block(facts.coder_summary):
                echo(f"            {_BLOCK_MARKER}{line}")
    if no_gate:
        echo("  3 gate: skipped (--no-gate) — the PR would be UNMONITORED")
        echo("  4 human gates: left open (no pr gate would hold the story)")
    else:
        echo("  3 gate: create a pr gate on the story + record pr_gate_id")
        gates = ", ".join(g.gate_id for g in retirement.superseded) or "none"
        echo(f"  4 human gates to complete after it: {gates}")
        for described in retirement.retained:
            echo(f"          leaving {described} open")
    echo(f"  5 post: {MANUAL_DELIVERY} on {story.story_id}")
    if converge:
        # `echo` strips and shapes: after the loom-authored headline these are
        # the story's own text, which for a mirrored story is an outside
        # issue body
        echo(f"  6 converge: {converge[0]}")
        for line in converge[1:]:
            echo(f"              {line}")


def preview(
    *,
    facts: RunFacts,
    story: StoryState,
    repo: Path,
    repo_name: str,
    base: str | None,
    title: str,
    no_gate: bool,
    routes: Sequence[str],
    converge: ConvergeChain | None,
) -> None:
    """The ``--dry-run`` screen: every fact RESOLVED, and nothing written.

    The base and the adopt / open / refuse decision come from the SAME reads
    step 2 makes (:func:`pr_plan`), so the screen the operator approves is the
    decision the delivery takes — not a placeholder base and a generic "adopt
    or open". The plan is skipped only when the push above is refused, which
    is where the real invocation stops too: a decision it would never reach is
    not a fact about this delivery.
    """
    state = remote_state(repo, facts.branch)
    plan = (
        None
        if state.action == PUSH_DIVERGED
        else pr_plan(
            repo,
            branch=facts.branch,
            repo_name=repo_name,
            base=base,
            head_sha=state.local_sha,
            # the preview runs BEFORE the push, and a PR's head is whatever
            # origin/<branch> points at: a fast-forward carries an open PR at
            # the current remote sha to the delivered one, so the plan reads
            # it as the adoption the real step 2 makes
            moves_to_ours=(
                state.remote_sha if state.action == PUSH_FAST_FORWARD else ""
            ),
        )
    )
    # the PR number only when the plan already knows it (an adoption); a PR
    # this run would OPEN has none yet, and inventing one would be the guess
    # the whole plan exists to avoid
    pr_label = (
        f"#{plan.existing.number}"
        if plan is not None and plan.existing is not None
        else "the PR opened above"
    )
    echo_plan(
        facts=facts,
        story=story,
        repo=repo,
        repo_name=repo_name,
        plan=plan,
        state=state,
        title=title,
        no_gate=no_gate,
        retirement=story.retirement(run_id=facts.run_id, dispatch_routes=routes),
        converge=(
            ()
            if converge is None
            else converge_lines(converge, pr=pr_label, verb="would converge")
        ),
    )


def render(record: Mapping[str, Any]) -> list[str]:
    label = record["run_id"] or record["branch"]
    pr_url = record["pr_url"]
    uncertain = record.get("push_uncertain")
    # Three ways to end without a url, and only one of them may say "NO PR":
    # an unverifiable `gh pr create` leaves a PR that MAY exist, and a headline
    # asserting its absence is what makes an operator open a second one. The
    # push half is stated the same way — never "PUSHED" over a run that pushed
    # nothing (an already-equal remote), never an absence that was not read.
    if pr_url:
        headline = f"deliver {label}: {pr_url}"
    elif uncertain:
        headline = f"deliver {label}: PUSH UNCERTAIN — the delivery is unfinished"
    elif record.get("pr_uncertain"):
        headline = (
            f"deliver {label}: PR UNCERTAIN — a PR may have been opened; "
            "the delivery is unfinished"
        )
    elif record["pushed"]:
        headline = f"deliver {label}: PUSHED, NO PR — the delivery is unfinished"
    else:
        headline = f"deliver {label}: NO PR — the delivery is unfinished"
    lines = [headline]
    if uncertain:
        lines.append(
            f"  origin/{record['branch']} could not be read — it may or may not "
            f"hold {record['pushed_sha'][:12]}"
        )
    elif record["pushed"]:
        lines.append(
            f"  pushed {record['pushed_sha'][:12]} → origin/{record['branch']}"
        )
    else:
        lines.append(f"  origin/{record['branch']} already up to date")
    if pr_url:
        verb = "adopted" if record["adopted"] else "opened"
        number = record["pr_number"]
        lines.append(
            f"  {verb} PR #{number}" if number is not None else f"  {verb} the PR"
        )
    if record["pr_gate_id"]:
        # "blocks", not "now blocks": the id may be a gate an earlier pass
        # raised and this one only found (a `--no-gate` re-run over a gated PR).
        lines.append(f"  pr gate {record['pr_gate_id']} blocks {record['story_id']}")
    for gate_id in record["human_gates_completed"]:
        lines.append(f"  completed needs-human gate {gate_id}")
    for described in record["human_gates_retained"]:
        lines.append(
            f"  left needs-human gate {described} open — a different "
            "escalation, not this delivery's to retire"
        )
    if not record["changed"]:
        lines.append("  nothing changed — this branch was already delivered")
    for note in record["notes"]:
        # `notes` carry `str(exc)` over git / gh stderr: same treatment, so a
        # continuation line cannot present itself as a report line of its own
        shaped = quoted_block(note, width=None)
        lines.append(f"  [Friction] {shaped[0] if shaped else ''}")
        lines.extend(f"    {_BLOCK_MARKER}{line}" for line in shaped[1:])
    return lines


def file_record(
    json_out: Path | None, record: dict[str, Any], notes: list[str]
) -> None:
    """Write the ``--json`` record, if one was asked for.

    A record the operator asked for and did not get is a partial result, not a
    footnote: the delivery stands, but the output they will script against is
    missing, so it lowers ``complete`` (exit 2) as well as leaving a note.
    """
    if json_out is None:
        return
    try:
        _write_json(json_out, record)
    except OSError as exc:
        notes.append(f"could not write the JSON record to {json_out} ({exc})")
        record["complete"] = False


def _write_json(json_out: Path | None, record: Mapping[str, Any]) -> None:
    if json_out is None:
        return
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(dict(record), indent=2), encoding="utf-8")

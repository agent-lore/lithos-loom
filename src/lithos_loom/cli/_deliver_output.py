"""How ``develop deliver`` reports itself — the terminal and the story.

The fourth piece of the command (beside :mod:`cli._deliver_facts`,
:mod:`cli._deliver_repo` and :mod:`cli._deliver_lithos`): the ``--dry-run``
plan, the end-of-run render, and the ``[ManualDelivery]`` summary posted on the
story. All three are pure functions of the facts and the record, so what the
operator is told has tests of its own and the command module stays the five
steps and their flags.

One rule runs through them: **say what was actually done**. A gate attempt that
failed is not a hand-off the operator chose; a gate kept open says whose it is;
a write whose outcome could not be read is neither "done" nor "nothing". And
every line loom did not author (a stop reason built from agent stdout, a story
title mirrored from a GitHub issue, ``gh`` stderr inside an exception) goes
through :func:`~lithos_loom.cli._deliver_facts.sanitize_for_terminal` before it
reaches the terminal.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import typer

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
)
from lithos_loom.plugins.story_develop import run_outcome

__all__ = ["MANUAL_DELIVERY", "delivery_finding", "echo_plan", "render"]


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
        # against the head GitHub reports for the PR when it could be read,
        # and only otherwise against the sha this delivery pushed: the claim
        # is about the revision the PR actually delivers (deliver.py step 2b).
        unbound = approval_unbound(
            facts,
            delivered_head=str(
                record.get("pr_head_sha") or record.get("pushed_sha") or ""
            ),
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

    echo(f"deliver {facts.run_id or facts.branch}: dry run, nothing written")
    echo(f"  repo:   {repo} → {repo_name}")
    echo(f"  story:  {story.story_id} [{story.status}] — {story.title}")
    # the raw failure reason stays on the operator's terminal; the PR body
    # carries only the classification (see `provenance_lines`). An approved run
    # has no `failure_reason` — its reason is why its own delivery never landed.
    reason = facts.failure_reason or facts.delivery_failure
    echo(f"  run:    {facts.status or '?'} — {reason or '—'}")
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
        echo(
            f"  2 PR:   adopt #{plan.existing.number} {plan.existing.url} "
            f"(head {plan.existing.head_sha[:12]} → {plan.base}) — no body is "
            "written"
        )
    elif plan.refusal:
        echo(f"  2 PR:   REFUSE — {plan.refusal}")
    else:
        echo(f"  2 PR:   open a new PR onto {plan.base}")
        echo(f"          title {title!r}")
        if facts.coder_summary:
            # The one thing in the body loom did not author, shown AS IT WOULD
            # BE PUBLISHED. The redaction it has been through removes
            # recognisable shapes (urls, paths, credential-like runs); it is
            # not a confidentiality boundary — the coder agent chooses this
            # text and the encoding it is in — so the operator's read of it
            # here is the last check before it is world-readable for good.
            echo("          it quotes the coder's handoff summary, as published:")
            for line in textwrap.wrap(
                facts.coder_summary, width=72, initial_indent="", subsequent_indent=""
            ):
                echo(f"            | {line}")
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
        lines.append(f"  [Friction] {note}")
    return lines

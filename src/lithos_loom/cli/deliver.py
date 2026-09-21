"""``lithos-loom develop deliver`` — deliver a stopped run's branch by hand.

A story-develop run that stops without delivering (`disputed`, `stalled`,
`max_rounds`, `cost_exceeded`, a coder / reviewer death) leaves a committable
branch behind and raises a loom ``human`` gate on the story. Every surface that
could carry that work forward wants a **PR**: ``develop converge`` refuses a
bare branch, the github-watcher's dispatchers key on a ``pr`` gate, and the one
automated way onward — ticking the human gate — re-dispatches the story *from
scratch* on a fresh branch, discarding the rounds that already landed. This
command is the third choice: turn the branch that exists into a monitored PR.

Five steps, each idempotent, in this order:

1. **Push, append-only.** Absent remote ref → ``git push -u``; already equal →
   nothing; remote an ancestor of the local branch → a plain fast-forward push;
   **diverged → refused**, naming both shas. Never a force: the branch may
   carry someone else's commits.
2. **Open or adopt the PR** through the same ``pr_delivery`` seam story-develop
   uses on approval — but only *this branch's own* PR: same repository, head at
   the sha just pushed (``gh pr list --head`` matches on the branch NAME, so a
   fork's PR is otherwise indistinguishable). A second invocation adopts and
   changes nothing; anything else refuses. Every ``gh`` call is pinned to the
   ``origin`` the branch was pushed to.
3. **Raise the ``pr`` gate** — or adopt the one already watching THIS PR — and
   record it on the story through
   :func:`~lithos_loom.subscriptions.delivery_gate.record_delivery_on_story` —
   the same write the daemon's delivering exit makes, so a hand-delivered story
   is indistinguishable from a daemon-delivered one to every later sweep, made
   whenever the live story does not already say it (a partial first pass is
   repaired, not skipped).
4. **Complete the stop's loom ``human`` gate(s)**, found from the story's
   incoming ``waits_on_gate`` edges (never from the ``needs_human_gate_id``
   provenance key, which can be stale). **After** step 3, so the story is never
   momentarily on the ready frontier: the runner's readiness check then defers
   it, because a story behind a ``pr`` gate is absent from ``task_ready``.
5. **Post ``[ManualDelivery]``** on the story — the delivered sha, the PR, the
   gate that now holds the story, the gates that were retired — one-shot via a
   marker written on the gate after the post, so a lost finding is re-posted
   next run rather than computed away as "nothing changed".

From there the PR is a first-class PR-maintenance object: landability, external
review ingestion, the base-move re-gate, the conflict resolver, merge → story
completed + dependents nudged, and the S6 admission count.

**Failure is phase-aware.** Everything up to and including the PR open may
refuse (exit 1) — nothing is written yet. From the moment the PR exists, no
failure may lose its url: the gate swap, the finding and the ``--json`` record
each degrade into a ``[Friction]`` note and the command exits 2, saying what is
owed. The whole delivery runs under a short-lived ``deliver`` claim on the
story, so two invocations cannot interleave into two gates.

**The repo, not the worktree.** ``state.json`` names the branch, and the branch
ref lives in the project's own checkout whether or not the run's worktree still
exists (a salvage may well have removed it). So the run dir is used only to
*find* the branch and the story; everything else is ``git -C <repo>`` + ``gh`` +
Lithos. ``--branch`` / ``--story`` is the explicit fallback for a host with
``retain_failed_workdirs = false``.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import typer

from lithos_loom.cli._deliver_lithos import (
    DELIVER_ASPECT,
    DeliverRefused,
    GateOutcome,
    StoryState,
    claim_story,
    post_finding,
    read_story_sync,
    release_story,
    run_gate_delivery,
)
from lithos_loom.cli._deliver_repo import (
    PUSH_CREATE,
    PUSH_DIVERGED,
    PUSH_FAST_FORWARD,
    PUSH_UP_TO_DATE,
    RemoteState,
    open_or_adopt,
    origin_repo_name,
    push_branch,
    remote_state,
)
from lithos_loom.config import LoomConfig, load_config
from lithos_loom.errors import LithosLoomError
from lithos_loom.plugins.story_develop import run_outcome
from lithos_loom.plugins.story_develop.pr_delivery import (
    build_pr_body,
    closes_line,
    pr_number_from_url,
    request_operator_review,
)

__all__ = ["EXIT_CODES", "MANUAL_DELIVERY", "deliver_command"]

# Stable, machine-parseable finding prefix (see AGENTS.md): a stopped run's
# branch was delivered as a PR by hand. Distinct from `[DevelopResult]` (a run
# reporting its own outcome) because nothing ran here — no rounds, no spend,
# no verdict; the operator moved existing work onto the maintained path, and
# an operator grepping for how a PR came to exist wants that difference.
MANUAL_DELIVERY = "[ManualDelivery]"

# 0 delivered (or adopted, or a dry-run plan); 1 refused/failed with nothing
# written; 2 the PR is open but the gate half did not complete — a PARTIAL
# delivery the operator must finish, so it never shares an exit code with a
# refusal that wrote nothing.
EXIT_CODES = {"delivered": 0, "refused": 1, "ungated": 2}


# ── run facts (pure; read off the on-disk contract) ─────────────────────


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
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), path)
    except OSError:
        return ""
    if best is None:
        return ""
    try:
        with best[1].open("rb") as fh:
            raw = fh.read(_MAX_HANDOFF_BYTES)
    except OSError:
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
    return summary


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


def provenance_lines(facts: RunFacts) -> list[str]:
    """The PR body's ``## Provenance`` block: where this branch came from.

    The run's ``failure_reason`` is deliberately NOT published here. It is not
    a curated label: for the reason-bearing statuses it is the first line of
    the agent CLI's error text, the subprocess stderr, or the tail of unparsed
    agent stdout — host paths, provider endpoints and auth-error payloads all
    reach it. A PR body is world-readable on a public repo, whereas the
    operator already has the full reason on the story's ``[NeedsHuman]``
    finding, in the gate brief, and in ``--dry-run``. So the PR carries the
    **classification** (a closed vocabulary) and nothing more.
    """
    lines = [
        "delivered by hand with `lithos-loom develop deliver` — the run that "
        "wrote this branch stopped before it could open a PR"
    ]
    if facts.run_id:
        stop = f"run `{facts.run_id}`"
        if facts.status:
            stop += f" stopped `{facts.status}` (see the story for why)"
        lines.append(stop)
    lines.append(f"branch `{facts.branch}`")
    if facts.coder_summary:
        lines.append(f"coder's final handoff: {facts.coder_summary}")
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
    )


def delivery_finding(
    *,
    facts: RunFacts,
    record: Mapping[str, Any],
    outcome: GateOutcome | None,
    notes: Sequence[str],
) -> str:
    """The ``[ManualDelivery]`` summary posted on the story (pure)."""
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
    if facts.status:
        parts.append(f"the run had stopped {facts.status}")
    if outcome is None:
        parts.append(
            "no pr gate was created (--no-gate): this PR is UNMONITORED — no "
            "merge tracking, no external-review ingestion, no re-gate"
        )
    else:
        if outcome.pr_gate_id and outcome.gate_created:
            parts.append(f"pr gate {outcome.pr_gate_id} now blocks the story")
        elif outcome.pr_gate_id:
            parts.append(f"pr gate {outcome.pr_gate_id} already blocks the story")
        for gate_id in outcome.human_gates_completed:
            parts.append(f"needs-human gate {gate_id} completed")
    summary = "; ".join(parts)
    problems = [*(outcome.problems if outcome else ()), *notes]
    if problems:
        summary += "\n\n[Friction] " + "; ".join(problems)
    return summary


# ── the command ─────────────────────────────────────────────────────────


def _echo_plan(
    *,
    facts: RunFacts,
    story: StoryState,
    repo: Path,
    repo_name: str,
    base: str,
    state: RemoteState,
    title: str,
    no_gate: bool,
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
    typer.echo(f"deliver {facts.run_id or facts.branch}: dry run, nothing written")
    typer.echo(f"  repo:   {repo} → {repo_name}")
    typer.echo(f"  story:  {story.story_id} [{story.status}] — {story.title}")
    # the raw failure reason stays on the operator's terminal; the PR body
    # carries only the classification (see `provenance_lines`)
    typer.echo(f"  run:    {facts.status or '?'} — {facts.failure_reason or '—'}")
    typer.echo(f"  1 push: {push_words[state.action]}")
    typer.echo(f"  2 PR:   adopt the open PR for the branch, else open onto {base}")
    typer.echo(f"          title {title!r}")
    if no_gate:
        typer.echo("  3 gate: skipped (--no-gate) — the PR would be UNMONITORED")
        typer.echo("  4 human gates: left open (no pr gate would hold the story)")
    else:
        typer.echo("  3 gate: create a pr gate on the story + record pr_gate_id")
        gates = ", ".join(story.human_gate_ids) or "none"
        typer.echo(f"  4 human gates to complete after it: {gates}")
    typer.echo(f"  5 post: {MANUAL_DELIVERY} on {story.story_id}")


def deliver_command(
    run: str | None = typer.Argument(
        None,
        help="The stopped run to deliver: a run id or a task id (its newest "
        "run). Omit only with --branch and --story.",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="Deliver this branch instead of resolving one from a run dir "
        "(with --story). The fallback for a host that does not retain failed "
        "work dirs.",
    ),
    story: str | None = typer.Option(
        None,
        "--story",
        help="Lithos task id of the story this branch implements (default: the "
        "run dir's task). Required with --branch.",
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Base branch for the PR (default: the repo's default branch).",
    ),
    no_gate: bool = typer.Option(
        False,
        "--no-gate",
        help="Open the PR only: no pr gate, and the needs-human gate is left "
        "open. The PR is then UNMONITORED — no merge tracking, no "
        "external-review ingestion, no base-move re-gate.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan with every fact resolved and write nothing — no "
        "push, no gh call, no Lithos write.",
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured JSON record to this path."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Push a stopped run's branch, open its PR, and swap the needs-human gate
    for a pr gate."""
    try:
        record = _deliver(
            load_config(config),
            run=run,
            branch=branch,
            story_id=story,
            base=base,
            no_gate=no_gate,
            dry_run=dry_run,
            json_out=json_out,
        )
    except LithosLoomError as exc:
        # DeliverRefused (a precondition), or a config that would not load —
        # either way nothing was written.
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(EXIT_CODES["refused"]) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        # git / gh could not be run at all (missing binary, timeout): nothing
        # was written by the step that raised, so this is a refusal too.
        typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(EXIT_CODES["refused"]) from exc
    if record is None:  # --dry-run: the plan was printed, nothing to record
        raise typer.Exit(EXIT_CODES["delivered"])
    for line in _render(record):
        typer.echo(line)
    # The PR is open either way; a gate half that did not complete is a
    # PARTIAL delivery (the story is unguarded, or still double-blocked) and
    # says so with its own exit code.
    raise typer.Exit(
        EXIT_CODES["delivered"] if record["gate_complete"] else EXIT_CODES["ungated"]
    )


def _deliver(
    host: LoomConfig,
    *,
    run: str | None,
    branch: str | None,
    story_id: str | None,
    base: str | None,
    no_gate: bool,
    dry_run: bool,
    json_out: Path | None,
) -> dict[str, Any] | None:
    """The command body. Returns the JSON record, or ``None`` for a dry run.

    **Phase-aware failure.** Everything up to and including the PR open may
    raise :class:`DeliverRefused` — nothing is written yet, so the command
    exits 1 with a plain message. From the moment ``create_pr`` returns, the
    PR exists: no later failure may lose its url or hide what is owed, so
    every step after it degrades into ``notes`` and the command exits 2 with
    the url printed and the `[Friction]` posted.
    """
    facts = _resolve_facts(host, run=run, branch=branch, story_id=story_id)
    agent = host.orchestrator.agent_id
    url = host.orchestrator.lithos_url
    story = read_story_sync(url, agent, facts.story_id)
    if story.status != "open" and not no_gate:
        raise DeliverRefused(
            f"story {story.story_id} is {story.status}, not open — a terminal "
            "story takes no pr gate (the PR would be the operator's own). "
            "Re-run with --no-gate to open the PR alone"
        )
    repo = _resolve_repo(host, story)
    repo_name = origin_repo_name(repo)
    state = remote_state(repo, facts.branch)
    # the same title rule story-develop's own delivery applies
    heading = story.title.strip()
    title = heading.splitlines()[0][:90] if heading else facts.branch
    if dry_run:
        _echo_plan(
            facts=facts,
            story=story,
            repo=repo,
            repo_name=repo_name,
            base=base or "the repo's default branch",
            state=state,
            title=title,
            no_gate=no_gate,
        )
        return None

    # A terminal story cannot be claimed, and with --no-gate on one there is
    # no gate work to serialise. Otherwise the claim is the cross-process
    # guard: two deliveries of one story must not interleave, or both read
    # "no pr gate" before either writes one and the story ends up with two.
    claimed = story.status == "open"
    if claimed and not claim_story(url, agent, story.story_id):
        raise DeliverRefused(
            f"another `develop deliver` holds the {DELIVER_ASPECT} claim on "
            f"{story.story_id} — it is delivering this story right now. Wait "
            "for it to finish (the claim's TTL is short) and re-run if needed"
        )
    try:
        return _deliver_claimed(
            host,
            facts=facts,
            story=story,
            repo=repo,
            repo_name=repo_name,
            state=state,
            title=title,
            base=base,
            no_gate=no_gate,
            json_out=json_out,
        )
    finally:
        if claimed:
            release_story(url, agent, story.story_id)


def _deliver_claimed(
    host: LoomConfig,
    *,
    facts: RunFacts,
    story: StoryState,
    repo: Path,
    repo_name: str,
    state: RemoteState,
    title: str,
    base: str | None,
    no_gate: bool,
    json_out: Path | None,
) -> dict[str, Any]:
    """Steps 1-5, under the story's ``deliver`` claim."""
    agent = host.orchestrator.agent_id
    url = host.orchestrator.lithos_url

    # 1 — push, append-only (a diverged ref raises before anything is written).
    push_branch(repo, facts.branch, state)

    # 2 — adopt this branch's own open PR, or open one. The last step that may
    # refuse: after it, the PR exists.
    pr_url, adopted = open_or_adopt(
        repo,
        branch=facts.branch,
        repo_name=repo_name,
        base=base,
        head_sha=state.local_sha,
        title=title,
        body=lambda: pr_body(facts=facts, story=story, repo_name=repo_name),
    )

    notes: list[str] = []
    record: dict[str, Any] = {
        "run_id": facts.run_id,
        "story_id": story.story_id,
        "branch": facts.branch,
        "pushed": state.action in (PUSH_CREATE, PUSH_FAST_FORWARD),
        "pushed_sha": state.local_sha,
        "pr_url": pr_url,
        "pr_number": None,
        "adopted": adopted,
        "pr_gate_id": None,
        "human_gates_completed": [],
        "gate_complete": True,
        "changed": True,
        "notes": notes,
    }
    try:
        record["pr_number"] = pr_number_from_url(pr_url)
    except RuntimeError as exc:
        notes.append(f"could not read the PR number from {pr_url} ({exc})")

    # Best-effort notify (#113). Never fatal, and never before the PR exists.
    section = getattr(host, "story_develop", None)
    login = getattr(section, "operator_github_login", None) if section else None
    pr_number = record["pr_number"]
    notify = bool(login) and not adopted and isinstance(pr_number, int)
    if notify and request_operator_review(repo_name, int(pr_number), str(login)) == (
        "failed"
    ):
        notes.append(f"could not notify @{login} of the PR")

    # 3 + 4 — the gate swap. A transport failure here is NOT a refusal: the PR
    # is open, so it degrades to a note and the partial exit code.
    outcome: GateOutcome | None = None
    if not no_gate:
        try:
            outcome = run_gate_delivery(
                url, agent, story=story, pr_url=pr_url, run_id=facts.run_id
            )
        except DeliverRefused as exc:
            notes.append(
                f"the PR is open but the gate could not be raised ({exc}) — the "
                "story is NOT gated: nothing tracks this PR's merge and the "
                "needs-human gate still holds the story. Re-run to finish"
            )
            record["gate_complete"] = False
        else:
            record["pr_gate_id"] = outcome.pr_gate_id
            record["human_gates_completed"] = list(outcome.human_gates_completed)
            record["gate_complete"] = not outcome.problems
            notes.extend(outcome.problems)
    # --no-gate needs no note here: it is a choice, not friction, and
    # `delivery_finding` says UNMONITORED in the finding's own body. Keeping
    # `notes` to real problems is what lets a repeat invocation stay silent.

    # 5 — the provenance finding, when this run changed something OR the gate
    # does not yet record that a finding was posted for this delivery. The
    # marker (written on the gate, after the post) is what makes a crash
    # between steps 4 and 5 recoverable: the next run sees it missing and
    # posts, rather than computing "nothing changed" and losing the audit.
    owed = (
        outcome is not None
        and outcome.pr_gate_id is not None
        and not (outcome.finding_marked)
    )
    changed = bool(
        record["pushed"]
        or not adopted
        or (
            outcome is not None
            and (
                outcome.gate_created
                or outcome.story_recorded
                or outcome.human_gates_completed
            )
        )
    )
    record["changed"] = changed
    if changed or owed or notes:
        summary = delivery_finding(
            facts=facts, record=record, outcome=outcome, notes=notes
        )
        try:
            post_finding(
                url,
                agent,
                story.story_id,
                summary,
                gate_id=record["pr_gate_id"],
                pr_url=pr_url,
                run_id=facts.run_id,
            )
        except DeliverRefused as exc:
            notes.append(
                f"could not post {MANUAL_DELIVERY} on the story ({exc}); re-run "
                "to leave the provenance"
            )
            record["gate_complete"] = False
    if json_out is not None:
        try:
            _write_json(json_out, record)
        except OSError as exc:
            # The delivery already happened; a record we could not file is a
            # note, not a failure of the delivery.
            notes.append(f"could not write the JSON record to {json_out} ({exc})")
    return record


def _resolve_facts(
    host: LoomConfig, *, run: str | None, branch: str | None, story_id: str | None
) -> RunFacts:
    """Resolve the run (or the explicit branch + story) into :class:`RunFacts`."""
    if run is None:
        if not (branch and story_id):
            raise DeliverRefused(
                "name a run (`develop deliver <run-id|task-id>`) or pass both "
                "--branch and --story"
            )
        return RunFacts(story_id=story_id, branch=branch)
    run_dir = run_outcome.resolve_run_dir(host.orchestrator.work_dir, run)
    if run_dir is None:
        if branch and story_id:
            # the work dir was reaped / never retained: the operator's own
            # branch + story stand in for it, minus the run's provenance
            return RunFacts(story_id=story_id, branch=branch)
        raise DeliverRefused(
            f"no run state for {run!r} under {host.orchestrator.work_dir} "
            "(`lithos-loom develop list` shows what is there). If the work dir "
            "is gone, pass --branch and --story"
        )
    facts = run_facts(run_dir)
    if branch:
        facts = RunFacts(**{**asdict(facts), "branch": branch})
    if story_id:
        facts = RunFacts(**{**asdict(facts), "story_id": story_id})
    if facts.delivered_pr_url:
        raise DeliverRefused(
            f"run {facts.run_id} already delivered {facts.delivered_pr_url} — "
            "there is nothing to deliver by hand"
        )
    if not facts.branch:
        raise DeliverRefused(
            f"run {facts.run_id} recorded no branch (it stopped before its "
            "worktree was cut); pass --branch if you know it"
        )
    return facts


def _resolve_repo(host: LoomConfig, story: StoryState) -> Path:
    """The project checkout holding the branch — ``[projects.<slug>].repo``."""
    slug = story.project
    if slug is None:
        raise DeliverRefused(
            f"story {story.story_id} names no project (`metadata.project`), so "
            "the checkout holding its branch is unknown"
        )
    project = host.projects.get(slug)
    if project is None:
        raise DeliverRefused(
            f"project {slug!r} is not mapped in this host's config — add a "
            f"[projects.{slug}] stanza with its `repo` path"
        )
    return project.repo


# ── output ──────────────────────────────────────────────────────────────


def _write_json(json_out: Path | None, record: Mapping[str, Any]) -> None:
    if json_out is None:
        return
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(dict(record), indent=2), encoding="utf-8")


def _render(record: Mapping[str, Any]) -> list[str]:
    verb = "adopted" if record["adopted"] else "opened"
    lines = [f"deliver {record['run_id'] or record['branch']}: {record['pr_url']}"]
    if record["pushed"]:
        lines.append(
            f"  pushed {record['pushed_sha'][:12]} → origin/{record['branch']}"
        )
    else:
        lines.append(f"  origin/{record['branch']} already up to date")
    number = record["pr_number"]
    lines.append(f"  {verb} PR #{number}" if number is not None else f"  {verb} the PR")
    if record["pr_gate_id"]:
        lines.append(
            f"  pr gate {record['pr_gate_id']} now blocks {record['story_id']}"
        )
    for gate_id in record["human_gates_completed"]:
        lines.append(f"  completed needs-human gate {gate_id}")
    if not record["changed"]:
        lines.append("  nothing changed — this branch was already delivered")
    for note in record["notes"]:
        lines.append(f"  [Friction] {note}")
    return lines

"""The ``--converge`` chain ``develop deliver`` runs after a successful delivery.

The chaining piece of the command (beside :mod:`cli._deliver_facts`,
:mod:`cli._deliver_repo`, :mod:`cli._deliver_lithos`,
:mod:`cli._deliver_session`, :mod:`cli._deliver_output` and
:mod:`cli._deliver_preflight`): what to converge, under which acceptance
criteria, and the seam that runs it.

Delivering a stopped run and re-reviewing what it delivered are two halves of
one operator gesture — a run that stopped on an acceptance dispute is fixed by
revising the acceptance and reviewing the branch against it, and doing that by
hand is the #99 / #101 / #423 sequence three commands long. Chaining it here
costs the delivery nothing: the converge run starts only after steps 1-5 have
finished, on the PR they produced, and its exit code becomes the command's.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import typer.main

from lithos_loom.cli._deliver_facts import sanitize_for_terminal
from lithos_loom.cli._deliver_lithos import DeliverRefused, StoryState
from lithos_loom.cli.converge import converge_command

__all__ = ["ConvergeChain", "converge_chain", "run_chained_converge", "run_converge"]


@dataclass(frozen=True)
class ConvergeChain:
    """What ``--converge`` runs once the delivery has landed.

    The #99 / #101 / #423 sequence in ONE command: a run that stopped on an
    acceptance dispute is delivered and then re-reviewed under the acceptance
    the operator revised while deciding. So the intake is the PR this delivery
    produced, ``--story`` is the story (converge then resolves the project's
    and the story's own ``develop_*`` settings exactly as the daemon path
    does), and the criteria are the story's **current** ones — never the PR
    body's copy, which was written from the same story a moment ago and goes
    stale the instant the operator edits it, which is precisely what a dispute
    makes them do. ``--ac-file`` overrides.
    """

    story_id: str
    repo: Path
    expect_repo: str
    """The ``owner/name`` the delivery resolved from the checkout's ``origin``."""
    acceptance: str
    """The criteria, as ONE snapshot: the text shown to the operator IS the
    text handed to converge (``--ac``). Never a path — a path is read twice,
    once for the preview and once by converge after the delivery, and the two
    reads can disagree about content, encoding and emptiness alike."""
    ac_source: str
    """Where that snapshot came from, for the report and the dry-run plan."""
    profile: str | None
    config: Path | None

    def argv(self, pr: str, *, head: str = "") -> list[str]:
        """The ``develop converge`` argv for *pr* (a number or a PR url).

        **Pinned**, like every other loom-initiated converge dispatch (the
        remediation and conflict-resolve dispatchers, ``merge-gate``):
        ``--expect-repo`` because a bare ``#N`` otherwise resolves against
        whatever ``--repo``'s ``origin`` says at that moment, and
        ``--expect-head`` because the delivery's own head read-back (step 2b)
        is a point-in-time observation — converge re-resolves the head itself,
        and an actor who advances ``origin/<branch>`` in between would have a
        paid round spend, and loom's own push land, on a revision no step of
        this command verified. Converge's refusals turn both into a no-op.
        """
        argv = [pr, "--story", self.story_id, "--repo", str(self.repo)]
        if self.expect_repo:
            argv += ["--expect-repo", self.expect_repo]
        if head:
            argv += ["--expect-head", head]
        # Always the snapshot, never `--ac-file`: converge resolves `--ac` to
        # exactly this string (`resolve_acceptance_criteria` strips both the
        # same way), so what the operator approved on screen is what the panel
        # and the coder are given. In-process, so there is no argv size limit
        # to trade against.
        argv += ["--ac", self.acceptance]
        if self.profile:
            argv += ["--profile", self.profile]
        if self.config is not None:
            argv += ["--config", str(self.config)]
        return argv


def run_converge(argv: Sequence[str]) -> int:
    """Run ``develop converge`` in THIS process and return its exit code.

    Through converge's own parser — the seam the CLI and the watcher's
    dispatchers both use — rather than a re-implementation of its flag
    resolution: every ``develop_*`` layer, model policy and check-table merge
    behind ``--story`` is the one an operator would get by typing the command
    themselves. In-process, so the run inherits this terminal and this
    process's lifetime; standalone mode is kept so a usage error renders the
    way converge renders it, and the ``SystemExit`` it raises is converted
    back into the exit code deliver adopts as its own.
    """
    app = typer.Typer(add_completion=False)
    app.command("converge")(converge_command)
    command = typer.main.get_command(app)
    try:
        command.main(args=list(argv), prog_name="lithos-loom develop converge")
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def run_chained_converge(
    chain: ConvergeChain,
    *,
    record: dict[str, Any],
    notes: list[str],
    pr_url: str,
    describe: Callable[[str], Sequence[str]],
    run: Callable[[Sequence[str]], int],
) -> None:
    """Step 2c of ``develop deliver``: the chained converge, run on *record*'s
    PR — after the PR exists, BEFORE the ``pr`` gate.

    Runs only while the delivery is still complete: an unverified or moved PR
    head is owed a re-run first, and converge would spend on it and hide that
    behind its own exit code — then ``record["converge"]`` is ``None`` ("asked
    for, skipped"). Otherwise the outcome is recorded as ``{"exit_code",
    "error"}``: converge's own exit code, or ``1`` plus the message when the
    seam raised (a crash out of the run, not a verdict on the PR — the gate
    swap that follows still monitors it, and a note says so). The criteria
    are shown before the paid, pushing run starts, stripped like every other
    line; the run is pinned to the head the delivery VERIFIED behind the PR
    (step 2b), not the sha it chose to push, so converge refuses outright if
    the branch moved in between. *describe* renders the lines shown for the
    PR label and *run* is the converge seam — both the caller's, so the
    output module (which imports this one) is not imported back, and the
    seam is the one name (``cli.deliver.run_converge``) tests stub.
    """
    if not record["complete"]:
        record["converge"] = None
        return
    number = record["pr_number"]
    # converge takes `#142` / `142` / a url; the url is the fallback for a PR
    # whose number could not be read back (already a note on the record)
    pr = str(number) if number is not None else str(pr_url)
    label = f"#{number}" if number is not None else pr
    for line in describe(label):
        typer.echo(sanitize_for_terminal(f"  {line}"))
    try:
        code = run(chain.argv(pr, head=str(record["pr_head_sha"])))
    except Exception as exc:  # noqa: BLE001 — a crash out of the seam
        record["converge"] = {"exit_code": 1, "error": str(exc)}
        notes.append(
            f"the chained converge run crashed ({exc}) — not a verdict on the "
            "PR, which is gated below and stands as delivered; re-review it "
            "with `lithos-loom develop converge`"
        )
    else:
        record["converge"] = {"exit_code": int(code), "error": ""}


def _read_ac_file(path: Path) -> str:
    """*path* read WHOLE, strictly, once — or a refusal.

    Read HERE, before the push, and then carried as the snapshot: a path read
    twice is two different inputs. The delivery would print one and converge
    judge the other if anything edited the file in between, and the two reads
    would not even agree on what counts as readable — a lenient, bounded
    preview passes an invalid byte (or one past its bound) that converge's
    strict full read then rejects, after the PR is pushed and gated. So this
    read is converge's own: whole, ``utf-8`` strict, and stripped as
    ``resolve_acceptance_criteria`` strips it. Every way it can fail —
    missing, a directory, unreadable, not UTF-8 — refuses while nothing has
    been written.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeliverRefused(
            f"--ac-file {path} could not be read ({exc}) — it is the "
            "acceptance the chained converge run would be judged against, so "
            "nothing is delivered until it resolves"
        ) from exc
    except UnicodeDecodeError as exc:
        raise DeliverRefused(
            f"--ac-file {path} is not valid UTF-8 ({exc}) — converge would "
            "refuse it, so nothing is delivered until it resolves"
        ) from exc
    return text


def converge_chain(
    story: StoryState,
    *,
    repo: Path,
    repo_name: str,
    acceptance_file: Path | None,
    profile: str | None,
    config_path: Path | None,
) -> ConvergeChain:
    """The ``--converge`` chain for *story*, with its acceptance criteria
    resolved from the live story read (never from the PR body).

    The criteria are the story's **current description** (title + body) —
    the text the operator edits when a dispute sends them to the story, and
    the one thing guaranteed to be what they just revised. Deliberately NOT
    ``metadata.acceptance_criteria``: a story can carry an older value there
    while its description has been rewritten, and preferring it would have
    the one-command workflow silently re-review against the stale copy — the
    exact failure this chain exists to remove. ``--ac-file`` overrides, read
    WHOLE and strictly here and then carried as the snapshot both the preview
    and converge use (see :func:`_read_ac_file`).

    Either way the criteria are resolved BEFORE the push, so criteria converge
    would refuse — unreadable, not UTF-8, empty — cost nothing instead of
    being discovered with the PR already pushed and gated.
    """
    source = (
        f"--ac-file {acceptance_file}"
        if acceptance_file is not None
        else "the story's description"
    )
    criteria = (
        _read_ac_file(acceptance_file)
        if acceptance_file is not None
        else story.task_text
    ).strip()
    if not criteria:
        raise DeliverRefused(
            f"the acceptance criteria for the converge run are empty "
            f"({source}) — converge refuses a run with no criteria, so "
            "nothing is delivered until they resolve"
        )
    return ConvergeChain(
        story_id=story.story_id,
        repo=repo,
        expect_repo=repo_name,
        acceptance=criteria,
        ac_source=source,
        profile=profile,
        config=config_path,
    )

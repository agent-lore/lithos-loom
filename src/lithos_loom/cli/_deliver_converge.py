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

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import typer
import typer.main

from lithos_loom.cli._deliver_lithos import DeliverRefused, StoryState
from lithos_loom.cli.converge import converge_command

__all__ = ["ConvergeChain", "converge_chain", "run_converge"]


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
    """The criteria as they will be judged — shown to the operator before a
    paid, pushing agent acts on them. For ``--ac-file`` this is the file's
    text read for the PREVIEW; converge reads the file itself by path, which
    stays the authoritative read."""
    acceptance_file: Path | None
    ac_source: str
    """How the criteria were resolved, for the report and the dry-run plan."""
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
        if self.acceptance_file is not None:
            argv += ["--ac-file", str(self.acceptance_file)]
        else:
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


# Enough of the file to show the operator what the run will be judged
# against; converge re-reads it whole by path. Bounded because this read
# happens on the terminal path (`--dry-run` included) only to print ~8 lines.
_AC_FILE_PREVIEW_BYTES = 64 * 1024


def _read_ac_file(path: Path) -> str:
    """The head of *path*, for the preview — or a refusal.

    Read HERE, before the push, so an ``--ac-file`` that is missing, a
    directory, or unreadable costs nothing: today the same mistake delivers
    the PR first and is only discovered when converge exits on it, with the
    branch already pushed and gated.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(_AC_FILE_PREVIEW_BYTES)
    except OSError as exc:
        raise DeliverRefused(
            f"--ac-file {path} could not be read ({exc}) — it is the "
            "acceptance the chained converge run would be judged against, so "
            "nothing is delivered until it resolves"
        ) from exc


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
    exact failure this chain exists to remove. ``--ac-file`` overrides: the file is
    read here for the PREVIEW (and an unreadable one refuses before anything
    is delivered), while the path is what travels to converge, whose own read
    stays authoritative.
    """
    if acceptance_file is not None:
        return ConvergeChain(
            story_id=story.story_id,
            repo=repo,
            expect_repo=repo_name,
            acceptance=_read_ac_file(acceptance_file),
            acceptance_file=acceptance_file,
            ac_source=f"--ac-file {acceptance_file}",
            profile=profile,
            config=config_path,
        )
    return ConvergeChain(
        story_id=story.story_id,
        repo=repo,
        expect_repo=repo_name,
        acceptance=story.task_text,
        acceptance_file=None,
        ac_source="the story's description",
        profile=profile,
        config=config_path,
    )

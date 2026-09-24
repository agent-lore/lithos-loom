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

from lithos_loom.cli._deliver_lithos import StoryState
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
    acceptance: str
    acceptance_file: Path | None
    ac_source: str
    """How the criteria were resolved, for the report and the dry-run plan."""
    profile: str | None
    config: Path | None

    def argv(self, pr: str) -> list[str]:
        """The ``develop converge`` argv for *pr* (a number or a PR url)."""
        argv = [pr, "--story", self.story_id, "--repo", str(self.repo)]
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


def converge_chain(
    story: StoryState,
    *,
    repo: Path,
    acceptance_file: Path | None,
    profile: str | None,
    config_path: Path | None,
) -> ConvergeChain:
    """The ``--converge`` chain for *story*, with its acceptance criteria
    resolved from the live story read (never from the PR body).

    ``metadata.acceptance_criteria`` when the story carries them — the same
    field story-develop's own runs review against — else the story's
    description (title + body), which is what the coder was given when there
    is no separate AC field. ``--ac-file`` wins over both and is passed on as
    the path, so converge reads the operator's file itself.
    """
    if acceptance_file is not None:
        return ConvergeChain(
            story_id=story.story_id,
            repo=repo,
            acceptance="",
            acceptance_file=acceptance_file,
            ac_source=f"--ac-file {acceptance_file}",
            profile=profile,
            config=config_path,
        )
    criteria = story.acceptance_criteria
    return ConvergeChain(
        story_id=story.story_id,
        repo=repo,
        acceptance=criteria or story.task_text,
        acceptance_file=None,
        ac_source=(
            "the story's acceptance criteria" if criteria else "the story's description"
        ),
        profile=profile,
        config=config_path,
    )

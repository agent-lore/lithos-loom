"""``lithos-loom develop converge`` — converge an existing PR to review-green.

On-demand review-convergence loop (converge / ADR 0003 §9 "Shape 1"): resolve a
PR, run loom's in-container reviewer panel + deterministic gate against it, have
a coder fix the PR branch, re-review, and loop until the panel LGTMs **and** the
gate floor is clean — then fast-forward-push the fixed branch back to the PR
head, ready for the human merge gate. Exits 0 when the PR is review-green
(already-clean or converged), non-zero otherwise.

A thin wrapper over :func:`converge_pr`; it shares the intake + fix loop with
``develop review`` (same panel primitive) and story-develop (same ``develop()``
loop) — see :mod:`lithos_loom.plugins.story_develop.converge`. The acceptance-
criteria precedence and reviewer/profile resolution are the ``review`` command's,
reused verbatim (no second implementation).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path

import typer

from lithos_loom.cli.review import (
    apply_model_policy,
    host_default_models,
    resolve_acceptance_criteria,
    resolve_check_commands,
    resolve_check_states,
    resolve_reviewers,
)
from lithos_loom.config import GitHubWatcherConfig, load_config
from lithos_loom.plugins.story_develop import engines
from lithos_loom.plugins.story_develop.config import (
    DEFAULT_IMAGE,
    DEFAULT_TEST_TIMEOUT,
    DevelopConfig,
    parse_artifacts_path,
    parse_image,
    parse_parity_command,
    parse_test_command,
)
from lithos_loom.plugins.story_develop.converge import ConvergeResult, converge_pr
from lithos_loom.plugins.story_develop.external_reviews import (
    ExternalFinding,
    GitHubError,
    ReplyMode,
    fetch_external_findings,
    issue_comment_reply_body,
    pr_number_from_spec,
)
from lithos_loom.plugins.story_develop.github_access import repo_name_with_owner
from lithos_loom.plugins.story_develop.pr_delivery import (
    post_pr_comment,
    post_thread_reply,
    reply_body,
)
from lithos_loom.plugins.story_develop.profiles import UnknownProfileError, get_profile
from lithos_loom.plugins.story_develop.review_resolve import resolve_change

# status -> process exit code. Review-green (nothing left for the operator to do)
# is 0; a bad-input refusal (fork) is 2; everything else that needs a human is 1.
_EXIT_CODES = {
    "already_clean": 0,
    "converged": 0,
    "triage_rejected": 0,
    "fork_unsupported": 2,
    "merged": 2,
    "not_converged": 1,
    "merge_race": 1,
    "failed": 1,
}


def converge_command(
    change: str = typer.Argument(
        ...,
        help="The PR to converge: #142 / 142 / a GitHub PR URL. "
        "converge pushes fixes to the PR branch, so a bare range / branch is rejected.",
    ),
    profile: str = typer.Option(
        "standard",
        "--profile",
        "-p",
        help="Review profile (selects panel + check-set).",
    ),
    reviewer: list[str] | None = typer.Option(
        None, "--reviewer", help="Override the panel personas (repeatable)."
    ),
    acceptance: str | None = typer.Option(
        None, "--ac", help="Acceptance criteria text (the PR's intent)."
    ),
    acceptance_file: Path | None = typer.Option(
        None, "--ac-file", help="Read acceptance criteria from a file."
    ),
    base: str | None = typer.Option(
        None, "--base", help="Override the diff base (default: the PR merge-base)."
    ),
    check_command: list[str] | None = typer.Option(
        None,
        "--check-command",
        help="Override a gate check's command as NAME=COMMAND (repeatable), e.g. "
        "--check-command typecheck='make typecheck'. Runs the repo's own command "
        "verbatim instead of the catalog default (which can over-scope and force "
        "extra fix rounds). Overridable: lint / typecheck / sast / dep-audit / "
        "coverage / semgrep (the `test` check uses --test-command).",
    ),
    check_state: list[str] | None = typer.Option(
        None,
        "--check-state",
        help="Override a gate check's blocking state as NAME=STATE (repeatable): "
        "required | informational | off, e.g. --check-state sast=off. `off` drops "
        "the check cleanly. Stateable: lint / typecheck / test / sast / dep-audit / "
        "coverage / semgrep.",
    ),
    test_command: str | None = typer.Option(
        None,
        "--test-command",
        help="Command for the `test` gate check (overrides auto-detection). The `test` "
        "check has bespoke detection, so it takes this dedicated flag rather than "
        "--check-command.",
    ),
    parity_command: str | None = typer.Option(
        None,
        "--parity-command",
        help="The repo's aggregate verification command (e.g. 'make check'), run once "
        "as a required `repo-parity` gate check so the converged tree passes what CI "
        "enforces beyond the structured check-set (diagram drift, codegen, docs lint). "
        "Primary gate for ecosystems the catalog doesn't model (C/C++).",
    ),
    image: str = typer.Option(
        DEFAULT_IMAGE,
        "--image",
        help="Sandbox container image for the agents and the gate. Match the "
        "project's develop_image — converge does not read project metadata, so "
        "without this it runs the default image and a gate needing tooling that "
        "image lacks (e.g. a browser) can never pass.",
    ),
    artifacts_path: str | None = typer.Option(
        None,
        "--artifacts-path",
        help="Repo-relative dir a gate check writes rendered output to (the "
        "project's develop_artifacts_path). Enables the artifact review pass.",
    ),
    coder: str | None = typer.Option(
        None, "--coder", help="Coder engine for the fix turns (claude / codex)."
    ),
    max_rounds: int | None = typer.Option(
        None, "--max-rounds", help="Cap the implement→review→fix rounds."
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help="Soft phase-boundary ceiling on total agent spend (USD) — intake "
        "review + fix loop. In-flight turns may overshoot, and a same-round "
        "approval is still delivered.",
    ),
    test_timeout: int = typer.Option(
        DEFAULT_TEST_TIMEOUT,
        "--test-timeout",
        help="Max seconds for one gate check run (the test check, other check-set "
        "checks, and autoformat). Raise it for a repo whose suite exceeds the "
        "default — otherwise the gate floor can never clear and converge stalls.",
    ),
    no_push: bool = typer.Option(
        False, "--no-push", help="Converge locally but do not push to the PR branch."
    ),
    from_github: bool = typer.Option(
        False,
        "--from-github",
        help="Ingest the PR's external review findings (reviews + inline "
        "comments + conversation comments) instead of running the local-panel "
        "intake: trusted ones (allowlisted bots + write/admin humans) are "
        "triaged and, if they survive, seed the fix loop directly; untrusted "
        "ones are printed but never fed to an agent. Replies are posted for "
        "what was fixed or rejected (on the thread, or on the conversation "
        "for a conversation comment).",
    ),
    repo: Path | None = typer.Option(
        None, "--repo", help="Repository to converge in (default: current directory)."
    ),
    json_out: Path | None = typer.Option(
        None, "--json", help="Write the structured JSON summary to this path."
    ),
    config: Path | None = typer.Option(None, "--config", help="Host config path."),
) -> None:
    """Converge an existing PR to review-green (panel + gate), then push."""
    # Fail closed on an unknown profile / coder before spending any containers,
    # through the same single known-set seams the rest of the code uses.
    try:
        get_profile(profile)
    except UnknownProfileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if coder is not None and not engines.is_supported(coder):
        raise typer.BadParameter(
            f"unsupported coder {coder!r}: expected {engines.supported_tools_phrase()}"
        )
    # Validate the numeric bounds before any container work — a nonsensical
    # ceiling / round cap must fail fast, not after spending on the intake review.
    # NaN compares False against everything, so `<= 0` alone would let
    # `--max-cost nan` through as an effectively-unlimited budget.
    if max_cost is not None and (not math.isfinite(max_cost) or max_cost <= 0):
        raise typer.BadParameter("--max-cost must be a finite value greater than 0")
    if test_timeout < 1:
        raise typer.BadParameter("--test-timeout must be at least 1 second")
    if max_rounds is not None and max_rounds < 1:
        raise typer.BadParameter("--max-rounds must be at least 1")
    check_commands = resolve_check_commands(check_command)
    check_states = resolve_check_states(check_state)
    # Validate --test-command through the shared normaliser: a blank / whitespace-only
    # value would otherwise reach `sh -c` unmodified, do no work, exit 0, and
    # false-green the required `test` check without running tests (#278 review).
    try:
        test_command = parse_test_command(test_command, where="--test-command")
        parity_command = parse_parity_command(parity_command, where="--parity-command")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    repo = repo or Path.cwd()
    host = load_config(config)

    resolved = resolve_change(repo, change, base_branch="main", base_override=base)

    # converge pushes fixes onto the PR head ref, so it needs a PR (a range /
    # branch spec has no pushable head branch). Reject those up front.
    if not resolved.head_branch:
        raise typer.BadParameter(
            f"converge requires a PR (it pushes fixes to the PR branch); "
            f"{change!r} resolved to a range / branch with no pushable head. "
            "Use `develop review` for a read-only review of an arbitrary range."
        )

    criteria = resolve_acceptance_criteria(acceptance, acceptance_file, resolved.body)
    if not criteria:
        typer.secho(
            "error: no acceptance criteria for the converge run — pass --ac / "
            "--ac-file (a PR's body is used automatically, but this PR has none).",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    # Fail closed on a blank image before any spend: a whitespace value would
    # reach `docker run` and die deep in the first container start.
    try:
        resolved_image = parse_image(image, where="--image") or DEFAULT_IMAGE
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        resolved_artifacts = parse_artifacts_path(
            artifacts_path, where="--artifacts-path"
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    reviewers = resolve_reviewers(profile, reviewer)

    external_findings = None
    gh_repo: str | None = None
    pr_number: int | None = None
    if from_github:
        gh_repo = repo_name_with_owner(repo)
        pr_number = pr_number_from_spec(change)
        if pr_number is None:
            raise typer.BadParameter(
                f"--from-github needs a PR number/URL; {change!r} has none"
            )
        watcher = getattr(host, "github_watcher", None)
        trusted_bots = (
            watcher.trusted_bots
            if watcher is not None
            else GitHubWatcherConfig.trusted_bots
        )
        try:
            trusted, untrusted = fetch_external_findings(
                gh_repo, pr_number, trusted_bots=trusted_bots
            )
        except GitHubError as exc:
            typer.secho(
                f"error: fetching external reviews for {gh_repo}#{pr_number} "
                f"failed: {exc}",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1) from exc
        for f in untrusted:
            # ADR 0011 decision 8: reported, never fed to an agent.
            typer.secho(
                f"untrusted (reported only, not remediated): {f.author} — "
                f"{' '.join(f.body.split())[:120]} ({f.thread_url})",
                fg=typer.colors.YELLOW,
            )
        if not trusted:
            typer.echo(
                "nothing to ingest — no live trusted external findings on "
                f"{gh_repo}#{pr_number}"
            )
            raise typer.Exit(0)
        external_findings = tuple(trusted)

    overrides: dict = {}
    if coder is not None:
        overrides["coder"] = coder
    if max_rounds is not None:
        overrides["max_rounds"] = max_rounds

    develop_config = DevelopConfig(
        repo=repo,
        description=resolved.title or f"Converge {resolved.head_ref}",
        work_dir=host.orchestrator.work_dir / "converge",
        acceptance_criteria=criteria,
        review_profile=profile,
        reviewers=reviewers,
        base_branch=base or "main",
        max_cost_usd=max_cost,
        test_command=test_command,
        test_timeout=test_timeout,
        check_commands=check_commands,
        check_states=check_states,
        parity_command=parity_command,
        image=resolved_image,
        artifacts_path=resolved_artifacts,
        **overrides,
    )
    develop_config = apply_model_policy(
        develop_config,
        where="develop converge",
        default_models=host_default_models(host),
        include_coder=True,
    )

    result = converge_pr(
        develop_config,
        resolved,
        no_push=no_push,
        external_findings=external_findings,
    )

    if from_github and gh_repo is not None and pr_number is not None:
        _post_external_replies(result, repo=gh_repo, pr_number=pr_number)

    typer.echo(_render(result))
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(result.to_json(), indent=2), encoding="utf-8")

    raise typer.Exit(_EXIT_CODES.get(result.status, 1))


def _render(result: ConvergeResult) -> str:
    """Human-readable one-block summary of the converge outcome."""
    change = result.change
    lines = [f"converge {change.head_ref}: {result.status}"]
    if result.message:
        lines.append(f"  {result.message}")
    dev = result.develop_result
    if dev is not None:
        lines.append(
            f"  rounds: {dev.rounds}   fixer commits: {len(result.fixer_commits)}"
        )
    if result.pushed:
        lines.append(f"  pushed {result.pushed_sha[:10]} → {change.head_branch}")
    for o in result.external_outcomes:
        where = f" ({o.finding.path}:{o.finding.line})" if o.finding.path else ""
        detail = f" — {o.detail}" if o.detail else ""
        lines.append(
            f"  external [{o.finding_id}] by {o.finding.author}{where}: "
            f"{o.disposition}{detail}"
        )
    for f in result.deferred_findings:
        # 819370e5 (PR #342 review): converge has no Lithos source task, so
        # nothing spawns — an unsurfaced deferral would be lost.
        because = (
            f" (deferred because: {f.deferral_reason})" if f.deferral_reason else ""
        )
        lines.append(
            f"  deferred: [{f.reviewer}/{f.finding_id}] {f.severity} — "
            f"out-of-scope; NO task spawned by converge, file manually: "
            f"{f.rationale}{because}"
        )
    return "\n".join(lines)


# One transport per reply capability (PR #356 re-review): the epilogue routes
# on the finding's ``reply_mode`` — never on its stream — so a new stream
# that picks an existing capability in its adapter row is answered here
# unchanged, and a new capability is a new member + a row here. Exhaustive
# over ReplyMode (checked at import); ``None`` = nothing to answer on.
Transport = Callable[[str, int, ExternalFinding, str], bool]


def _reply_on_thread(repo: str, pr_number: int, f: ExternalFinding, body: str) -> bool:
    return post_thread_reply(repo, pr_number, f.activity_id, body)


def _reply_on_conversation(
    repo: str, pr_number: int, f: ExternalFinding, body: str
) -> bool:
    return post_pr_comment(
        repo, pr_number, issue_comment_reply_body(body, f.thread_url)
    )


REPLY_TRANSPORTS: dict[ReplyMode, Transport | None] = {
    ReplyMode.NONE: None,
    ReplyMode.THREAD: _reply_on_thread,
    ReplyMode.CONVERSATION: _reply_on_conversation,
}
if set(REPLY_TRANSPORTS) != set(ReplyMode):
    raise RuntimeError("REPLY_TRANSPORTS must cover every ReplyMode")


def _reply_transport(mode: ReplyMode) -> Transport | None:
    try:
        return REPLY_TRANSPORTS[mode]
    except KeyError:
        raise LookupError(f"no reply transport for {mode!r}") from None


def _post_external_replies(
    result: ConvergeResult, *, repo: str, pr_number: int
) -> None:
    """Answer each external finding where it was raised, by its reply mode.

    Only what actually happened is asserted: a *fixed* reply is posted only
    when the branch was pushed (its sha is the proof — an unpushed fix must
    not claim to have landed); rejections and disputes reply regardless.
    Findings whose mode is ``NONE`` (a summary review has no thread) are
    left to the rendered summary. Best-effort: a failed reply logs via the
    poster and the rest continue.
    """
    posted = 0
    for o in result.external_outcomes:
        transport = _reply_transport(o.finding.reply_mode)
        if transport is None:
            continue
        if o.disposition == "rejected":
            body = reply_body(
                fixed=False, sha=None, coder_response=f"triage: {o.detail}"
            )
        elif o.disposition == "disputed":
            body = reply_body(fixed=False, sha=None, coder_response=o.detail)
        elif o.disposition == "fixed" and result.pushed:
            body = reply_body(
                fixed=True, sha=result.pushed_sha, coder_response=o.detail
            )
        else:
            continue  # unaddressed, or a fix that never landed — assert nothing
        if transport(repo, pr_number, o.finding, body):
            posted += 1
    if posted:
        typer.echo(f"posted {posted} external review repl(ies) on {repo}#{pr_number}")

"""Re-gate delivered PRs against their base's current tip (PRD S3, watcher half).

A delivered PR's own CI green is a statement about the base it was cut
from. On every still-open ``pr`` gate the reconcile sweep asks the other
question — *will the base break if this merges now?* — by spawning
``lithos-loom develop merge-gate <pr> --story <id>`` as a crash-isolated
subprocess (:mod:`lithos_loom.cli.merge_gate`): a trial merge of the base's
current tip in a throwaway worktree, the project's **current** check-set on
the result, and — green and behind — the merge commit pushed onto the PR
branch (append-only, ADR 0011 decision 2). Zero agent tokens.

What this module owns, all sweep-side (ADR 0011 decision 3 — single writer):

- **The re-run key** ``(head_sha, base_sha, settings fingerprint)`` in
  ``metadata.merge_gate`` on the GATE, url-scoped like every other gate
  marker. A moved head or base re-gates. An unchanged pair re-gates only
  when the story's resolved gate settings changed — probed each sweep with
  ``merge-gate --resolve-only`` (no fetch, no merge, no checks), since the
  check-set fingerprint needs a worktree the sweep does not have. A
  conflict / fork / closed record does not depend on the settings and is
  never probed.
- **One in-flight run per project** (a check-set run is minutes; the sweep
  must not block); a second gate in the same project simply dispatches on
  a later sweep, no state needed.
- **Mutual hold with remediation** on the same PR: either may push to the
  branch, and a leased push beside another push loses (a wasted round).
- **Every outcome is a one-shot record.** Green records (and a pushed merge
  commit is recorded as loom's own on the S5b budget, else the next sweep
  would read it as a human push and reset the remediation counter). A green
  verdict whose push FAILED is ``push_failed`` — a ``[Friction]`` with the
  push error and a bounded retry, never a settled green (PR #362 review F1).
  Red / errored posts ``[MergeGateFailed]`` naming the check. A conflict
  widens S1's ``[PRConflicted]`` with the conflicting paths — the trial
  merge is the only source of that list. An unresolvable config, an
  unmapped project, a fork, a checkout that is not the gate's repo
  (``--expect-repo``, review F2), or a crash posts ``[Friction]`` on the
  story — never silent, never gated with built-ins.
- **The settings probe never blocks the sweep** (review F5): it runs as a
  background task per gate, and only a changed fingerprint starts a run
  (under the project's slot, or on a later sweep if that slot is busy).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark
from lithos_loom.subscriptions._project_settings import (
    OriginRead,
    origin_read,
    origin_repo,
    parse_origin,
    read_project_flag,
    resolve_project_repo,
)
from lithos_loom.subscriptions._subprocess import spawn_command
from lithos_loom.subscriptions.merge_gate_outcome import (
    post_checkout_unresolved,
    post_config_unresolved,
    post_conflict,
    post_crashed,
    post_failed,
    post_push_failed,
    post_repo_mismatch,
    record_green,
    value_of,
    write_record,
)
from lithos_loom.subscriptions.merge_gate_record import (
    MAX_ATTEMPTS_PER_KEY,
    MERGE_GATE_FAILED,
    MERGE_GATE_KEY,
    MergeGateRecord,
    read_record,
)
from lithos_loom.subscriptions.remediation_budget import RemediationBudget, read_budget

__all__ = [
    "MERGE_GATE_FAILED",
    "MERGE_GATE_KEY",
    "MERGE_GATE_SETTING",
    "MergeGateDispatch",
    "MergeGateRecord",
    "MergeGateSettings",
    "OriginRead",
    "origin_read",
    "origin_repo",
    "parse_origin",
    "read_record",
    "spawn_merge_gate",
]

# Project-context metadata key: per-project dial for the base-move re-gate.
MERGE_GATE_SETTING = "develop_merge_gate"

# Wall-clock cap on one run (a full check-set in a container) and on the
# settings probe (config load + two Lithos reads — seconds, not minutes).
RUN_TIMEOUT_SECONDS = 2 * 3600
PROBE_TIMEOUT_SECONDS = 120

# Record statuses retried on the SAME key, up to MAX_ATTEMPTS_PER_KEY (see
# merge_gate_record): the run produced no verdict to stand on.
_RETRYABLE: frozenset[str] = frozenset({"crashed", "push_failed", "repo_mismatch"})
# ...and of those, the ones a daemon RESTART re-arms with a fresh pair: a
# crash or a push that keeps failing may be loom's own bug, and the restart
# is the operator's fix attempt (a repo mismatch settles on the origin read)
_REBOOT_REARMS: frozenset[str] = frozenset({"crashed", "push_failed"})

# Refusals about the mapped checkout, settled on (repo_path, origin_seen).
_CHECKOUT_REFUSALS: frozenset[str] = frozenset({"repo_mismatch", "checkout_unresolved"})

# The findings quote at most this much subprocess output.
_OUTPUT_TAIL_CHARS = 600

# Record statuses whose outcome depends on the gate settings: an unchanged
# sha pair re-gates when the probed fingerprint differs from the recorded
# one. Everything else (conflict, fork, closed, no project, a crash or a
# mismatch past its retries) is settled by the shas alone.
_SETTINGS_DEPENDENT: frozenset[str] = frozenset(
    {"green", "red", "errored", "no_checks", "config_unresolved", "push_failed"}
)

Spawn = Callable[[list[str]], Awaitable[tuple[int, str]]]
# Whether a remediation run is in flight on a PR url — re-checked the moment
# a background probe would start a run, not only when it was scheduled.
Hold = Callable[[str], bool]


async def spawn_merge_gate(cmd: list[str]) -> tuple[int, str]:
    """Default spawn: the merge-gate CLI, capped by whichever timeout the
    argv shape calls for (:func:`_subprocess.spawn_command`)."""
    if "--resolve-only" in cmd:
        return await spawn_command(
            cmd, timeout=PROBE_TIMEOUT_SECONDS, label="merge-gate settings probe"
        )
    return await spawn_command(cmd, timeout=RUN_TIMEOUT_SECONDS, label="merge-gate run")


@dataclass(frozen=True)
class MergeGateSettings:
    """Host-side knobs the watcher child threads in from its config."""

    enabled: bool = True
    projects: Mapping[str, Path] = field(default_factory=dict)
    work_dir: Path = Path(".")
    # Forwarded to the subprocess as `--config` so it loads the same host
    # config this child did; None lets it fall back to env/CWD discovery.
    config_path: Path | None = None


class MergeGateDispatch:
    """Owns the per-project single-flight dispatch of ``develop merge-gate``.

    One instance per watcher child. ``merge_gate`` marker writes happen
    either in the sweep while no run is in flight for that PR, or inside
    the run task — never both at once for one gate.
    """

    def __init__(
        self,
        settings: MergeGateSettings,
        *,
        spawn: Spawn | None = None,
        hold: Hold | None = None,
        boot_id: str | None = None,
    ):
        self._settings = settings
        # one per daemon boot: a crashed key re-arms once per restart
        self._boot_id = boot_id or uuid.uuid4().hex
        self._spawn: Spawn = spawn if spawn is not None else spawn_merge_gate
        self._hold = hold
        self._tasks: dict[str, asyncio.Task[None]] = {}  # project slug → run
        self._in_flight: dict[str, str] = {}  # project slug → pr url
        self._probes: dict[str, asyncio.Task[None]] = {}  # gate id → probe

    # ── in-flight state ────────────────────────────────────────────────

    def busy_for(self, slug: str) -> bool:
        task = self._tasks.get(slug)
        return task is not None and not task.done()

    def busy_on(self, pr_url: str) -> bool:
        """Whether a run is in flight on *pr_url* — remediation's hold: a
        merge commit may land on that branch at any moment."""
        return any(
            url == pr_url and self.busy_for(slug)
            for slug, url in self._in_flight.items()
        )

    def pending_probes(self) -> int:
        """In-flight settings probes (finished ones are pruned)."""
        return sum(1 for t in self._probes.values() if not t.done())

    def _live(self) -> list[asyncio.Task[None]]:
        return [
            t for t in (*self._probes.values(), *self._tasks.values()) if not t.done()
        ]

    async def drain(self) -> None:
        """Await every in-flight probe and run (tests; the sweep never
        waits). A probe may start a run as it finishes, so loop."""
        while live := self._live():
            await asyncio.gather(*live, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel + await the in-flight probes and runs; the
        cancellation-safe spawn terminates each merge-gate child, so a
        stopped loom never leaves an orphan pushing merge commits."""
        live = self._live()
        for task in live:
            task.cancel()
        if live:
            await asyncio.gather(*live, return_exceptions=True)

    # ── the decision ───────────────────────────────────────────────────

    async def consider(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        pr: Any,
        ctx: SubscriptionContext,
        *,
        hold: bool = False,
    ) -> str:
        """Decide whether this sweep re-gates the PR. Returns a label for
        the sweep log; ``"dispatched"`` means a run task started. Never
        raises. *hold* is remediation's in-flight signal for this PR.
        """
        if not self._settings.enabled:
            return "disabled"
        if story_id is None:
            return "no_story"  # `--story` is how the run resolves the config
        head = getattr(pr, "head_sha", "") or ""
        base = getattr(pr, "base_sha", "") or ""
        if not head or not base:
            return "unknown_shas"
        prior = read_record(gate, spec.pr_url)
        same_key = (
            prior is not None and prior.head_sha == head and prior.base_sha == base
        )

        head_repo = getattr(pr, "head_repo", "") or ""
        base_repo = getattr(pr, "base_repo", "") or ""
        if head_repo and base_repo and head_repo != base_repo:
            # PRD S3: a third-party head is never fetched into the
            # operator's checkout; recorded once per key, not re-warned.
            if same_key and prior is not None and prior.status == "fork_unsupported":
                return "unchanged"
            await post_finding_then_mark(
                ctx,
                task_id=story_id,
                summary=(
                    f"[Friction] merge-gate: delivered PR {spec.pr_url} is a fork "
                    f"PR ({head_repo} → {base_repo}); not re-gated against its base "
                    f"(forks are out of scope for S3 — a third-party head is never "
                    f"fetched into the operator's checkout); story {story_id} "
                    f"remains blocked on gate {gate.id} for a human merge."
                ),
                marker={
                    MERGE_GATE_KEY: MergeGateRecord(
                        spec.pr_url, head, base, status="fork_unsupported"
                    ).as_marker()
                },
                subsystem="merge-gate",
                retry_hint="will retry next sweep",
                marker_task_id=gate.id,
            )
            return "fork_unsupported"

        project = await resolve_project_repo(
            gate, story_id, self._settings.projects, ctx
        )
        if project is None:
            if same_key and prior is not None and prior.status == "no_project":
                return "unchanged"
            # On the story, once (PRD S3: "skipped with a one-shot [Friction],
            # never silently" — the operator action lives in Lithos / Lens,
            # not the host log; PR #362 review F3).
            await post_finding_then_mark(
                ctx,
                task_id=story_id,
                summary=(
                    f"[Friction] merge-gate: no project repo resolvable for gate "
                    f"{gate.id} (PR {spec.pr_url}, story {story_id}); the PR is "
                    f"not re-gated against its base — map the project under "
                    f"[projects] in the host config, or record metadata.project "
                    f"on the story, then restart loom."
                ),
                marker={
                    MERGE_GATE_KEY: MergeGateRecord(
                        spec.pr_url, head, base, status="no_project"
                    ).as_marker()
                },
                subsystem="merge-gate",
                retry_hint="will retry next sweep",
                marker_task_id=gate.id,
            )
            return "no_project"
        slug, repo = project
        if same_key and prior is not None and prior.status == "no_project":
            # the record was a one-shot friction, not a verdict: the project
            # is mapped now, so these shas have never been gated
            same_key = False
        enabled = await read_project_flag(
            slug, MERGE_GATE_SETTING, ctx, subsystem="merge-gate"
        )
        if enabled is None:
            ctx.logger.warning(
                "[Friction] merge-gate: cannot read project %r settings to check "
                "%s; failing closed — no run this sweep",
                slug,
                MERGE_GATE_SETTING,
            )
            return "project_settings_unavailable"
        if not enabled:
            ctx.logger.debug(
                "merge-gate: project %r disables %s; %s not re-gated",
                slug,
                MERGE_GATE_SETTING,
                spec.pr_url,
            )
            return "project_disabled"

        if hold:
            ctx.logger.info(
                "merge-gate: a remediation run is in flight on %s; deferring",
                spec.pr_url,
            )
            return "deferred_remediation"
        if self.busy_for(slug):
            ctx.logger.info(
                "merge-gate: a run is in flight for project %r; %s waits for a "
                "later sweep",
                slug,
                spec.pr_url,
            )
            return "deferred_busy"

        # The cheap origin read (PR #362 re-review 2): a checkout that is not
        # the gate's repo costs no subprocess, and a settled mismatch is a
        # fresh key the moment the mapping (path) or the origin changes —
        # the operator's fix must re-arm the same shas.
        read = await origin_read(repo)
        origin = read.repo
        seen = (origin or "").lower()
        matches = origin is not None and seen == spec.repo.lower()
        if (
            same_key
            and prior is not None
            and prior.status in _CHECKOUT_REFUSALS
            and (prior.repo_path != str(repo) or seen != prior.origin_seen)
        ):
            # A refusal about the checkout — a mismatch from the sweep's own
            # read or the CLI's authoritative gh check, or a read that could
            # not answer — settles on what the SWEEP observes: the two checks
            # can disagree (a rename gh follows), so "the read now matches"
            # is never by itself a fresh key (self-review: that looped a
            # spawn every sweep); the path or the read moving is.
            same_key = False
        if origin is None:
            # PR #362 re-review 3 F1: "cannot resolve" was permission to
            # dispatch, and the child then died in gh before any structured
            # refusal — two crashed attempts no mapping fix could re-arm.
            if (
                same_key
                and prior is not None
                and prior.status == "checkout_unresolved"
                and prior.origin_reason == read.reason
            ):
                return "unchanged"
            await post_checkout_unresolved(
                gate.id,
                story_id,
                spec,
                MergeGateRecord(
                    spec.pr_url,
                    head,
                    base,
                    status="checkout_unresolved",
                    repo_path=str(repo),
                    origin_reason=read.reason,
                ),
                ctx,
            )
            return "checkout_unresolved"
        if not matches:
            if same_key and prior is not None and prior.status == "repo_mismatch":
                return "unchanged"
            await post_repo_mismatch(
                gate.id,
                story_id,
                spec,
                MergeGateRecord(
                    spec.pr_url,
                    head,
                    base,
                    status="repo_mismatch",
                    repo_path=str(repo),
                    actual_repo=origin,
                    origin_seen=seen,
                ),
                {"expected_repo": spec.repo, "actual_repo": origin},
                ctx,
            )
            return "repo_mismatch"

        attempts = 1
        # The S5b budget as of dispatch: remediation is held on this PR
        # while the run is in flight and observe_head goes inert, so no
        # other writer moves it — the fallback when the end-of-run re-read
        # fails (a pushed merge commit MUST land as loom's own sha).
        budget = read_budget(gate, spec.pr_url)
        if same_key and prior is not None:
            retry = prior.status in _RETRYABLE and prior.attempts < MAX_ATTEMPTS_PER_KEY
            # a crash from a previous boot: the restart IS the fix attempt
            rebooted = prior.status in _REBOOT_REARMS and prior.boot_id != self._boot_id
            if retry or rebooted:
                attempts = 1 if rebooted else prior.attempts + 1
            elif prior.status not in _SETTINGS_DEPENDENT:
                return "unchanged"  # settled by the shas; waits for a move
            else:
                # Probe in the background (PR #362 review F5): the sweep must
                # not wait on a subprocess per unchanged gate. A changed
                # fingerprint starts the run from inside the probe task.
                probe = self._probes.get(gate.id)
                if probe is not None and not probe.done():
                    return "probing"
                # prune finished probes so a long-lived daemon's map stays
                # bounded by the gates currently probing, not ever probed
                self._probes = {k: t for k, t in self._probes.items() if not t.done()}
                self._probes[gate.id] = asyncio.create_task(
                    self._probe_then_run(
                        gate.id,
                        story_id,
                        spec,
                        slug,
                        repo,
                        head,
                        base,
                        prior,
                        budget,
                        ctx,
                    ),
                    name=f"merge-gate-probe-{spec.pr_number}",
                )
                return "probing"

        # A new key supersedes any probe still out for this gate (it captured
        # the old shas; its answer could only start a stale run).
        stale = self._probes.get(gate.id)
        if stale is not None and not stale.done():
            stale.cancel()
        self._start_run(
            gate.id, story_id, spec, slug, repo, head, base, attempts, budget, ctx
        )
        return "dispatched"

    def _start_run(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        slug: str,
        repo: Path,
        head: str,
        base: str,
        attempts: int,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        ctx.logger.info(
            "merge-gate: dispatching merge-gate for %s (head %s, base %s, attempt %d)",
            spec.pr_url,
            head[:12],
            base[:12],
            attempts,
        )
        self._in_flight[slug] = spec.pr_url
        self._tasks[slug] = asyncio.create_task(
            self._run(gate_id, story_id, spec, repo, head, base, attempts, budget, ctx),
            name=f"merge-gate-{spec.pr_number}",
        )

    async def _probe_then_run(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        slug: str,
        repo: Path,
        head: str,
        base: str,
        prior: MergeGateRecord,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        """The background probe: a changed fingerprint starts a run under
        the project's slot; a busy slot leaves it for a later sweep (which
        re-probes — cheap, and no state to get stale). Never raises."""
        try:
            label, fingerprint = await self._probe(spec, repo, story_id, gate_id)
        except Exception:  # noqa: BLE001 — a probe must never take the sweep down
            ctx.logger.exception(
                "merge-gate: settings probe for %s raised", spec.pr_url
            )
            return
        if fingerprint is None:
            ctx.logger.warning(
                "[Friction] merge-gate: settings probe for %s failed (%s); "
                "will retry next sweep",
                spec.pr_url,
                label,
            )
            return
        if fingerprint == prior.settings_fingerprint:
            return
        ctx.logger.info(
            "merge-gate: settings for %s changed (%s → %s); re-gating",
            spec.pr_url,
            prior.settings_fingerprint or "(unresolved)",
            fingerprint or "(unresolved)",
        )
        if self._hold is not None and self._hold(spec.pr_url):
            # The hold was clear when the probe was scheduled; a remediation
            # run started on this PR meanwhile (self-review) — either may
            # push, so the run waits for a later sweep, which re-probes.
            ctx.logger.info(
                "merge-gate: a remediation run started on %s while its settings "
                "were probed; re-gate deferred to a later sweep",
                spec.pr_url,
            )
            return
        if self.busy_for(slug):
            ctx.logger.info(
                "merge-gate: a run is in flight for project %r; %s re-gates on a "
                "later sweep",
                slug,
                spec.pr_url,
            )
            return
        # The probe captured the key when it was scheduled; a newer run may
        # have written a newer record meanwhile (re-review 2 F3). Act only on
        # the record we probed for.
        try:
            fresh = await ctx.lithos.task_get(task_id=gate_id)
        except LithosClientError:
            fresh = None
        current = None if fresh is None else read_record(fresh, spec.pr_url)
        if current is None or (current.head_sha, current.base_sha, current.status) != (
            prior.head_sha,
            prior.base_sha,
            prior.status,
        ):
            ctx.logger.info(
                "merge-gate: the record for %s moved while its settings were "
                "probed; nothing started (the next sweep decides afresh)",
                spec.pr_url,
            )
            return
        self._start_run(gate_id, story_id, spec, slug, repo, head, base, 1, budget, ctx)

    # ── subprocess plumbing ────────────────────────────────────────────

    def command(
        self,
        spec: PrGateSpec,
        repo: Path,
        json_path: Path,
        story_id: str,
        *,
        resolve_only: bool = False,
    ) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "lithos_loom",
            "develop",
            "merge-gate",
            str(spec.pr_number),
            # The story: the run resolves the project's + task's develop_*
            # settings (profile, check-set, image, test command, parity) —
            # the CURRENT config defending the base — strictly.
            "--story",
            story_id,
            "--repo",
            str(repo),
            # The checkout is pinned to the gate's repo (PR #362 review F2):
            # a PR number resolves against the checkout's origin, so a stale
            # [projects.<slug>].repo would otherwise trial-merge AND push
            # owner/other#N.
            "--expect-repo",
            spec.repo,
            "--json",
            str(json_path),
        ]
        if resolve_only:
            cmd.append("--resolve-only")
        if self._settings.config_path is not None:
            cmd += ["--config", str(self._settings.config_path)]
        return cmd

    def _json_path(self, gate_id: str, *, probe: bool) -> Path:
        kind = "probe" if probe else "run"
        path = (
            self._settings.work_dir
            / "github-watcher"
            / f"merge-gate-{gate_id}-{kind}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        return path

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _probe(
        self, spec: PrGateSpec, repo: Path, story_id: str, gate_id: str
    ) -> tuple[str, str | None]:
        """``(label, fingerprint)``: the story's current settings fingerprint,
        ``""`` when the config is unresolvable (exit 4 — that IS a state the
        key compares), ``None`` when the probe itself failed."""
        path = self._json_path(gate_id, probe=True)
        rc, output = await self._spawn(
            self.command(spec, repo, path, story_id, resolve_only=True)
        )
        if rc == 4:
            return "config_unresolved", ""
        data = self._load(path)
        fingerprint = None if data is None else data.get("settings_fingerprint")
        if rc != 0 or not isinstance(fingerprint, str):
            tail = output[-_OUTPUT_TAIL_CHARS:] if output else "(no output)"
            return f"exit {rc}: {tail}", None
        return "resolved", fingerprint

    # ── the run itself ─────────────────────────────────────────────────

    async def _run(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        head: str,
        base: str,
        attempts: int,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        """Run one merge-gate subprocess and record its outcome. Never raises."""
        try:
            await self._run_inner(
                gate_id, story_id, spec, repo, head, base, attempts, budget, ctx
            )
        except Exception as exc:  # noqa: BLE001 — the slot must always free cleanly
            ctx.logger.exception("merge-gate: run for %s raised", spec.pr_url)
            await post_crashed(
                gate_id,
                story_id,
                spec,
                MergeGateRecord(
                    spec.pr_url, head, base, attempts=attempts, boot_id=self._boot_id
                ),
                f"raised {type(exc).__name__}: {exc}",
                ctx,
            )

    async def _run_inner(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        head: str,
        base: str,
        attempts: int,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        path = self._json_path(gate_id, probe=False)
        rc, output = await self._spawn(self.command(spec, repo, path, story_id))
        data = self._load(path)
        # the settle key for a repo mismatch: the sweep's own read, so a CLI
        # refusal re-arms only when the mapping or the remote url moves
        origin_seen = ((await origin_read(repo)).repo or "").lower()
        record = MergeGateRecord(
            spec.pr_url,
            head,
            base,
            attempts=attempts,
            repo_path=str(repo),
            origin_seen=origin_seen,
            boot_id=self._boot_id,
        )
        tail = output[-_OUTPUT_TAIL_CHARS:] if output else "(no output)"
        if data is None and rc == 4:
            # config_unresolved: the CLI exits before any run and writes no
            # record — the exit code IS the record (PRD S3: skipped loudly,
            # never gated with built-ins).
            await post_config_unresolved(
                gate_id,
                story_id,
                spec,
                replace(record, status="config_unresolved"),
                tail,
                ctx,
            )
            return
        if data is None:
            await post_crashed(
                gate_id,
                story_id,
                spec,
                record,
                f"failed (exit {rc}) without a record; output tail: {tail}",
                ctx,
            )
            return

        status = data.get("status")
        status = status if isinstance(status, str) and status else "unknown"
        verdict = data.get("verdict")
        record = replace(
            record,
            status=status,
            settings_fingerprint=value_of(data, "settings_fingerprint"),
            verdict=verdict if isinstance(verdict, str) else None,
            merge_sha=value_of(data, "merge_sha"),
            pushed_sha=value_of(data, "pushed_sha") if data.get("pushed") else "",
            config_fingerprint=value_of(data, "config_fingerprint"),
            behind=data.get("behind") is True,
            push_error=value_of(data, "push_error"),
            actual_repo=value_of(data, "actual_repo"),
        )
        if status == "green" and record.behind and not record.pushed_sha:
            # A green verdict whose merge commit did NOT land (the CLI reports
            # the push beside the verdict, never folded into it): the PR is
            # still behind. Never a settled green (PR #362 review F1).
            status = "push_failed"
            record = replace(record, status=status)
        base_ref = value_of(data, "base_ref") or "the base branch"
        ctx.logger.info(
            "merge-gate: run for %s finished: %s (verdict %s, exit %d)%s",
            spec.pr_url,
            status,
            record.verdict or "none",
            rc,
            f", pushed {record.pushed_sha[:12]}" if record.pushed_sha else "",
        )

        if status == "green":
            await record_green(gate_id, record, budget, ctx)
        elif status == "push_failed":
            await post_push_failed(gate_id, story_id, spec, record, base_ref, ctx)
        elif status == "repo_mismatch":
            await post_repo_mismatch(gate_id, story_id, spec, record, data, ctx)
        elif status in ("red", "errored"):
            await post_failed(gate_id, story_id, spec, record, data, base_ref, ctx)
        elif status == "conflict":
            await post_conflict(gate_id, story_id, spec, record, data, base_ref, ctx)
        elif status == "config_unresolved":
            await post_config_unresolved(gate_id, story_id, spec, record, tail, ctx)
        elif status == "pr_closed":
            # The sweep's merge poll owns closed / merged; the run saying so
            # is a race, not an event. Recording it on these shas would
            # freeze a PR reopened without a push, so write nothing — the
            # sweep asks again only while GitHub reports the PR open.
            ctx.logger.info(
                "merge-gate: %s was closed or merged by the time the run resolved "
                "it; nothing recorded",
                spec.pr_url,
            )
        else:
            # no_checks / fork_unsupported / anything new: a record, not an
            # event.
            if status not in ("no_checks", "fork_unsupported"):
                ctx.logger.warning(
                    "[Friction] merge-gate: unrecognised status %r for %s; recorded",
                    status,
                    spec.pr_url,
                )
            await write_record(gate_id, record, ctx)

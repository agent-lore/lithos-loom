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
  would read it as a human push and reset the remediation counter). Red /
  errored posts ``[MergeGateFailed]`` naming the check. A conflict widens
  S1's ``[PRConflicted]`` with the conflicting paths — the trial merge is
  the only source of that list. An unresolvable config or a crash posts
  ``[Friction]`` on the story — never silent, never gated with built-ins.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import post_finding_then_mark, write_marker
from lithos_loom.subscriptions._project_settings import (
    read_project_flag,
    resolve_project_repo,
)
from lithos_loom.subscriptions._subprocess import spawn_command
from lithos_loom.subscriptions.pr_landability import PR_CONFLICTED
from lithos_loom.subscriptions.remediation_budget import (
    REMEDIATION_KEY,
    RemediationBudget,
    read_budget,
)

__all__ = [
    "MERGE_GATE_FAILED",
    "MERGE_GATE_KEY",
    "MERGE_GATE_SETTING",
    "MergeGateDispatch",
    "MergeGateRecord",
    "MergeGateSettings",
    "read_record",
    "spawn_merge_gate",
]

# Stable, machine-parseable finding prefix (see AGENTS.md): the project's
# current check-set went red (or could not verify) on the trial merge of a
# delivered PR into its base's current tip — merging it now would break the
# base.
MERGE_GATE_FAILED = "[MergeGateFailed]"

# Gate-metadata key holding the last re-gate record (the re-run key + outcome).
MERGE_GATE_KEY = "merge_gate"

# Project-context metadata key: per-project dial for the base-move re-gate.
MERGE_GATE_SETTING = "develop_merge_gate"

# Wall-clock cap on one run (a full check-set in a container) and on the
# settings probe (config load + two Lithos reads — seconds, not minutes).
RUN_TIMEOUT_SECONDS = 2 * 3600
PROBE_TIMEOUT_SECONDS = 120

# A crashed run (no record) is retried this many times on the SAME key, then
# waits for the key to move — a host problem must not become an hourly loop.
MAX_CRASH_ATTEMPTS = 2

# The findings quote at most this much subprocess output.
_OUTPUT_TAIL_CHARS = 600

# Record statuses whose outcome depends on the gate settings: an unchanged
# sha pair re-gates when the probed fingerprint differs from the recorded
# one. Everything else (conflict, fork, closed, no project) is settled by the
# shas alone.
_SETTINGS_DEPENDENT: frozenset[str] = frozenset(
    {"green", "red", "errored", "no_checks", "config_unresolved"}
)

Spawn = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def spawn_merge_gate(cmd: list[str]) -> tuple[int, str]:
    """Default spawn: the merge-gate CLI, capped by whichever timeout the
    argv shape calls for (:func:`_subprocess.spawn_command`)."""
    if "--resolve-only" in cmd:
        return await spawn_command(
            cmd, timeout=PROBE_TIMEOUT_SECONDS, label="merge-gate settings probe"
        )
    return await spawn_command(cmd, timeout=RUN_TIMEOUT_SECONDS, label="merge-gate run")


@dataclass(frozen=True)
class MergeGateRecord:
    """The gate's parsed ``merge_gate`` marker: the re-run key + the outcome.

    ``head_sha`` / ``base_sha`` are the shas the SWEEP observed when it
    dispatched (the key it compares next pass), not the run's own view.
    ``attempts`` counts runs on this key (a crash retries a bounded number
    of times); a fresh key starts at 1.
    """

    pr_url: str
    head_sha: str
    base_sha: str
    settings_fingerprint: str = ""
    status: str = ""
    verdict: str | None = None
    merge_sha: str = ""
    pushed_sha: str = ""
    config_fingerprint: str = ""
    attempts: int = 0

    def as_marker(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "settings_fingerprint": self.settings_fingerprint,
            "status": self.status,
            "verdict": self.verdict,
            "merge_sha": self.merge_sha,
            "pushed_sha": self.pushed_sha,
            "config_fingerprint": self.config_fingerprint,
            "attempts": self.attempts,
        }


def read_record(gate: Any, pr_url: str) -> MergeGateRecord | None:
    """Parse the gate's record; ``None`` for an absent / foreign-url marker
    (a replacement PR re-evaluates from scratch)."""
    raw = gate.metadata.get(MERGE_GATE_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return None

    def _s(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    attempts = raw.get("attempts")
    verdict = raw.get("verdict")
    return MergeGateRecord(
        pr_url=pr_url,
        head_sha=_s("head_sha"),
        base_sha=_s("base_sha"),
        settings_fingerprint=_s("settings_fingerprint"),
        status=_s("status"),
        verdict=verdict if isinstance(verdict, str) else None,
        merge_sha=_s("merge_sha"),
        pushed_sha=_s("pushed_sha"),
        config_fingerprint=_s("config_fingerprint"),
        attempts=attempts if isinstance(attempts, int) and attempts >= 0 else 0,
    )


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

    def __init__(self, settings: MergeGateSettings, *, spawn: Spawn | None = None):
        self._settings = settings
        self._spawn: Spawn = spawn if spawn is not None else spawn_merge_gate
        self._tasks: dict[str, asyncio.Task[None]] = {}  # project slug → run
        self._in_flight: dict[str, str] = {}  # project slug → pr url

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

    async def drain(self) -> None:
        """Await every in-flight run (tests; the sweep never waits)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Cancel + await the in-flight runs; the cancellation-safe spawn
        terminates each merge-gate child, so a stopped loom never leaves an
        orphan pushing merge commits."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

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
            await self._record(
                gate.id,
                MergeGateRecord(spec.pr_url, head, base, status="fork_unsupported"),
                ctx,
            )
            ctx.logger.warning(
                "[Friction] merge-gate: %s is a fork PR (%s → %s); not re-gated "
                "(forks are out of scope for S3)",
                spec.pr_url,
                head_repo,
                base_repo,
            )
            return "fork_unsupported"

        project = await resolve_project_repo(
            gate, story_id, self._settings.projects, ctx
        )
        if project is None:
            if same_key and prior is not None and prior.status == "no_project":
                return "unchanged"
            await self._record(
                gate.id,
                MergeGateRecord(spec.pr_url, head, base, status="no_project"),
                ctx,
            )
            ctx.logger.warning(
                "[Friction] merge-gate: no project repo resolvable for gate %s "
                "(%s); cannot re-gate — map the project under [projects] or "
                "record metadata.project",
                gate.id,
                spec.pr_url,
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

        attempts = 1
        if same_key and prior is not None:
            if prior.status == "crashed":
                if prior.attempts >= MAX_CRASH_ATTEMPTS:
                    return "unchanged"  # waits for the key to move
                attempts = prior.attempts + 1
            elif prior.status not in _SETTINGS_DEPENDENT:
                return "unchanged"
            else:
                label, fingerprint = await self._probe(spec, repo, story_id, gate.id)
                if fingerprint is None:
                    ctx.logger.warning(
                        "[Friction] merge-gate: settings probe for %s failed (%s); "
                        "will retry next sweep",
                        spec.pr_url,
                        label,
                    )
                    return "probe_failed"
                if fingerprint == prior.settings_fingerprint:
                    return "unchanged"
                ctx.logger.info(
                    "merge-gate: settings for %s changed (%s → %s); re-gating",
                    spec.pr_url,
                    prior.settings_fingerprint or "(unresolved)",
                    fingerprint or "(unresolved)",
                )

        ctx.logger.info(
            "merge-gate: dispatching merge-gate for %s (head %s, base %s, attempt %d)",
            spec.pr_url,
            head[:12],
            base[:12],
            attempts,
        )
        self._in_flight[slug] = spec.pr_url
        # The S5b budget as of dispatch: remediation is held on this PR
        # while the run is in flight and observe_head goes inert, so no
        # other writer moves it — the fallback when the end-of-run re-read
        # fails (a pushed merge commit MUST land as loom's own sha).
        budget = read_budget(gate, spec.pr_url)
        self._tasks[slug] = asyncio.create_task(
            self._run(gate.id, story_id, spec, repo, head, base, attempts, budget, ctx),
            name=f"merge-gate-{spec.pr_number}",
        )
        return "dispatched"

    # ── subprocess plumbing ────────────────────────────────────────────

    def _command(
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
            self._command(spec, repo, path, story_id, resolve_only=True)
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
            await self._crashed(
                gate_id,
                story_id,
                spec,
                MergeGateRecord(spec.pr_url, head, base, attempts=attempts),
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
        rc, output = await self._spawn(self._command(spec, repo, path, story_id))
        data = self._load(path)
        record = MergeGateRecord(spec.pr_url, head, base, attempts=attempts)
        tail = output[-_OUTPUT_TAIL_CHARS:] if output else "(no output)"
        if data is None and rc == 4:
            # config_unresolved: the CLI exits before any run and writes no
            # record — the exit code IS the record (PRD S3: skipped loudly,
            # never gated with built-ins).
            await self._config_unresolved(
                gate_id,
                story_id,
                spec,
                replace(record, status="config_unresolved"),
                tail,
                ctx,
            )
            return
        if data is None:
            await self._crashed(
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
            settings_fingerprint=self._str(data, "settings_fingerprint"),
            verdict=verdict if isinstance(verdict, str) else None,
            merge_sha=self._str(data, "merge_sha"),
            pushed_sha=self._str(data, "pushed_sha") if data.get("pushed") else "",
            config_fingerprint=self._str(data, "config_fingerprint"),
        )
        base_ref = self._str(data, "base_ref") or "the base branch"
        ctx.logger.info(
            "merge-gate: run for %s finished: %s (verdict %s, exit %d)%s",
            spec.pr_url,
            status,
            record.verdict or "none",
            rc,
            f", pushed {record.pushed_sha[:12]}" if record.pushed_sha else "",
        )

        if status == "green":
            await self._green(gate_id, record, budget, ctx)
        elif status in ("red", "errored"):
            await self._failed(gate_id, story_id, spec, record, data, base_ref, ctx)
        elif status == "conflict":
            await self._conflict(gate_id, story_id, spec, record, data, base_ref, ctx)
        elif status == "config_unresolved":
            await self._config_unresolved(gate_id, story_id, spec, record, tail, ctx)
        else:
            # no_checks / pr_closed / fork_unsupported / anything new: a
            # record, not an event — the merge poll owns closed and merged.
            if status not in ("no_checks", "pr_closed", "fork_unsupported"):
                ctx.logger.warning(
                    "[Friction] merge-gate: unrecognised status %r for %s; recorded",
                    status,
                    spec.pr_url,
                )
            await self._record(gate_id, record, ctx)

    @staticmethod
    def _str(data: Mapping[str, Any], key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    async def _record(
        self, gate_id: str, record: MergeGateRecord, ctx: SubscriptionContext
    ) -> None:
        await write_marker(
            ctx,
            task_id=gate_id,
            marker={MERGE_GATE_KEY: record.as_marker()},
            subsystem="merge-gate",
        )

    async def _green(
        self,
        gate_id: str,
        record: MergeGateRecord,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        """Record a green gate; a pushed merge commit is loom's own push on
        the S5b budget (else observe_head reads it as a human push and
        resets the remediation counter — the invariant S5b exists for).

        The push has already HAPPENED by now, so this must not fail into a
        bare crash record. Prefer the gate's current budget (a fresh read);
        fall back to the dispatch-time snapshot when Lithos will not answer
        — no other writer moved it meanwhile: remediation is held on this
        PR and observe_head is inert while the run is in flight. Record and
        budget land in ONE write. The residual: that one write itself
        failing (write_marker swallows) loses the sha, and the next sweep
        resets the budget — rare, and it errs toward more headroom.
        """
        marker: dict[str, Any] = {MERGE_GATE_KEY: record.as_marker()}
        if record.pushed_sha:
            try:
                fresh = await ctx.lithos.task_get(task_id=gate_id)
            except LithosClientError as exc:
                ctx.logger.warning(
                    "[Friction] merge-gate: re-reading gate %s to record loom's "
                    "push %s failed (%s); recording from the dispatch-time budget",
                    gate_id,
                    record.pushed_sha[:12],
                    exc,
                )
                fresh = None
            if fresh is not None:
                budget = read_budget(fresh, record.pr_url)
            marker[REMEDIATION_KEY] = replace(
                budget, last_loom_pushed_sha=record.pushed_sha
            ).as_marker()
        await write_marker(ctx, task_id=gate_id, marker=marker, subsystem="merge-gate")

    async def _failed(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        record: MergeGateRecord,
        data: Mapping[str, Any],
        base_ref: str,
        ctx: SubscriptionContext,
    ) -> None:
        rows = data.get("checks")
        failing = [
            c for c in (rows if isinstance(rows, list) else []) if not c.get("passed")
        ]
        named = "; ".join(
            f"{c.get('name')} ({c.get('command')}) — "
            + (
                "errored, not verified"
                if c.get("outcome") == "errored"
                else "timed out"
                if c.get("timed_out")
                else f"exit {c.get('exit_code')}"
            )
            for c in failing
        )
        why = (
            f"went {record.verdict}"
            if record.verdict
            else "could not be verified (a required check errored)"
        )
        await post_finding_then_mark(
            ctx,
            task_id=story_id,
            summary=(
                f"{MERGE_GATE_FAILED} merge-gate: delivered PR {spec.pr_url} would "
                f"break {base_ref}: the project's current check-set {why} on the "
                f"trial merge {record.merge_sha[:12]} (head {record.head_sha[:12]} "
                f"+ base {record.base_sha[:12]}) — {named or 'no check named'}; "
                f"story {story_id} remains blocked on gate {gate_id}. Fix on the PR "
                f"branch (never rebase a delivered branch); the next sweep re-gates "
                f"at the new head."
            ),
            marker={MERGE_GATE_KEY: record.as_marker()},
            subsystem="merge-gate",
            retry_hint="will retry next sweep",
            marker_task_id=gate_id,
        )

    async def _conflict(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        record: MergeGateRecord,
        data: Mapping[str, Any],
        base_ref: str,
        ctx: SubscriptionContext,
    ) -> None:
        raw = data.get("conflicting_paths")
        paths = [
            p for p in (raw if isinstance(raw, list) else []) if isinstance(p, str)
        ]
        await post_finding_then_mark(
            ctx,
            task_id=story_id,
            summary=(
                f"{PR_CONFLICTED} merge-gate: delivered PR {spec.pr_url} conflicts "
                f"with {base_ref} @ {record.base_sha[:12]} (head "
                f"{record.head_sha[:12]}) in {len(paths)} path(s): "
                f"{', '.join(paths) or '(unnamed)'}; story {story_id} remains "
                f"blocked on gate {gate_id}. Resolve by merging {base_ref} into the "
                f"PR branch (never rebase a delivered branch) and pushing; the next "
                f"sweep re-evaluates at the new head."
            ),
            marker={MERGE_GATE_KEY: record.as_marker()},
            subsystem="merge-gate",
            retry_hint="will retry next sweep",
            marker_task_id=gate_id,
        )

    async def _config_unresolved(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        record: MergeGateRecord,
        tail: str,
        ctx: SubscriptionContext,
    ) -> None:
        await post_finding_then_mark(
            ctx,
            task_id=story_id,
            summary=(
                f"[Friction] merge-gate: the current config for story {story_id} "
                f"(PR {spec.pr_url}) could not be resolved — nothing was gated "
                f"(S3 gates with the project's current config or not at all): "
                f"{tail}"
            ),
            marker={MERGE_GATE_KEY: record.as_marker()},
            subsystem="merge-gate",
            retry_hint="will retry next sweep",
            marker_task_id=gate_id,
        )

    async def _crashed(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        record: MergeGateRecord,
        detail: str,
        ctx: SubscriptionContext,
    ) -> None:
        record = replace(record, status="crashed")
        ctx.logger.warning(
            "merge-gate: run for %s crashed (attempt %d/%d): %s",
            spec.pr_url,
            record.attempts,
            MAX_CRASH_ATTEMPTS,
            detail,
        )
        await post_finding_then_mark(
            ctx,
            task_id=story_id,
            summary=(
                f"[Friction] merge-gate: develop merge-gate for {spec.pr_url} "
                f"(story {story_id}) {detail} (attempt {record.attempts}/"
                f"{MAX_CRASH_ATTEMPTS}"
                + (
                    "; retried next sweep)"
                    if record.attempts < MAX_CRASH_ATTEMPTS
                    else "; waits for a head or base move)"
                )
            ),
            marker={MERGE_GATE_KEY: record.as_marker()},
            subsystem="merge-gate",
            retry_hint="will retry next sweep",
            marker_task_id=gate_id,
        )

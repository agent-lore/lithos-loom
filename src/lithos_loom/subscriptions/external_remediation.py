"""Autonomous external-review remediation (PRD S2 slice C + the S5b budget).

When the reconcile sweep's detection half (:mod:`.external_reviews`) posts an
``[ExternalReview]`` batch, this module decides whether loom also *acts*:
dispatch ``lithos-loom develop converge <pr> --from-github`` as a subprocess
(crash-isolated; converge's own CLI does the trust filter, S5a triage,
injection, push and thread replies) and record the outcome back on the story.

The guard rails, all sweep-owned (ADR 0011 decision 3 — single writer):

- **S5b budget** — ``metadata.external_remediation`` on the GATE:
  ``{pr_url, rounds_used, last_loom_pushed_sha, last_seen_head_sha}``,
  url-scoped like every other gate marker (a replacement PR re-evaluates).
  The counter **never resets on a loom-authored push** (head moved to
  ``last_loom_pushed_sha`` — the two-bot ping-pong this exists to bound) and
  **resets on a human push** (head moved to any sha other than loom's
  recorded push; an empty previous sighting is first-time initialization
  only, so a PR whose spending rounds never pushed still resets — PR #346
  review F2). Exhaustion stops *dispatch only* — detection keeps posting,
  with the exhaustion stated inside the finding body
  (:meth:`ExternalRemediation.exhaustion_note`).
- **Single-flight** — one in-flight remediation globally (the serial-runner
  philosophy); while one runs, a batch that would have dispatched parks a
  durable **pending trigger** on the gate (its high-water marks are consumed
  when it posts, so a later quiet sweep must resume it explicitly — PR #346
  review F1), and :meth:`~ExternalRemediation.observe_head` goes inert (a
  head move while loom's own converge may push at any moment cannot be
  attributed).
- **Own-sha skip** — material reviewing loom's own pushed sha is reported,
  never auto-remediated (it is almost always a re-review of the fix in
  flight).
- **Trust** — only allowlisted bots / write-admin humans' material triggers a
  dispatch (converge re-applies the same line to what it feeds the coder).
- **Per-project dial** — context-doc ``develop_external_review_converge``
  (default **on**, ADR 0011 decision 6 — default-off would regress against
  the inline round slice D retires).

Failure economics: a converge run that produced a JSON result spent agent
time and keeps its budget round (``triage_rejected`` included); an exit-0
run with no JSON found nothing live to ingest (suppression drift between the
sweep's view and the CLI's re-fetch) and gives the round back; a non-zero
exit with no JSON posts a ``[Friction]`` finding and keeps the round. The
pre-run reservation is **strict** (PR #346 review F3): a budget write that
did not demonstrably land never spawns — the bound only exists if the
increment does.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lithos_loom.errors import LithosClientError
from lithos_loom.gates import PrGateSpec
from lithos_loom.github_client import GitHubClient, GitHubError
from lithos_loom.subscriptions import SubscriptionContext
from lithos_loom.subscriptions._findings import write_marker
from lithos_loom.subscriptions.external_reviews import EXTERNAL_REVIEW, IngestResult

__all__ = [
    "CONVERGE_SETTING",
    "PENDING_KEY",
    "REMEDIATION_KEY",
    "ExternalRemediation",
    "RemediationBudget",
    "RemediationSettings",
    "read_budget",
    "spawn_converge",
]

# Gate-metadata key holding the S5b budget state (see the module docstring).
# A separate key from `external_review_seen` and the merge marker — no marker
# may trip another's skip logic.
REMEDIATION_KEY = "external_remediation"

# Gate-metadata key parking a batch deferred behind the busy single-flight
# slot (PR #346 review F1): ingestion's high-water marks consume the batch,
# so without a durable trigger a deferred dispatch would never happen if the
# PR then went quiet. Url-scoped; consumed atomically with the budget
# reservation on dispatch; survives restarts.
PENDING_KEY = "external_remediation_pending"

# Project-context metadata key: per-project dial for autonomous dispatch.
CONVERGE_SETTING = "develop_external_review_converge"

# Hard wall-clock cap on one converge subprocess, so a hung run can never hold
# the global single-flight slot forever. Generous: a thorough multi-round
# converge is an hours-scale run.
RUN_TIMEOUT_SECONDS = 4 * 3600

_TRUSTED_PERMISSIONS = frozenset({"admin", "write"})

# The completion/friction findings quote at most this much subprocess output.
_OUTPUT_TAIL_CHARS = 600

Spawn = Callable[[list[str]], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class RemediationBudget:
    """The gate's parsed S5b budget state (fresh when absent / foreign-url)."""

    pr_url: str
    rounds_used: int = 0
    last_loom_pushed_sha: str = ""
    last_seen_head_sha: str = ""

    def as_marker(self) -> dict[str, Any]:
        return {
            "pr_url": self.pr_url,
            "rounds_used": self.rounds_used,
            "last_loom_pushed_sha": self.last_loom_pushed_sha,
            "last_seen_head_sha": self.last_seen_head_sha,
        }


def read_budget(gate: Any, pr_url: str) -> RemediationBudget:
    """Parse the gate's budget marker; fresh state for a foreign / absent url."""
    raw = gate.metadata.get(REMEDIATION_KEY)
    if not isinstance(raw, dict) or raw.get("pr_url") != pr_url:
        return RemediationBudget(pr_url=pr_url)
    rounds = raw.get("rounds_used")
    loom_sha = raw.get("last_loom_pushed_sha")
    seen_sha = raw.get("last_seen_head_sha")
    return RemediationBudget(
        pr_url=pr_url,
        rounds_used=rounds if isinstance(rounds, int) and rounds >= 0 else 0,
        last_loom_pushed_sha=loom_sha if isinstance(loom_sha, str) else "",
        last_seen_head_sha=seen_sha if isinstance(seen_sha, str) else "",
    )


@dataclass(frozen=True)
class RemediationSettings:
    """Host-side knobs the watcher child threads in from its config."""

    trusted_bots: tuple[str, ...]
    budget: int
    projects: Mapping[str, Path] = field(default_factory=dict)
    work_dir: Path = Path(".")
    # Forwarded to the subprocess as `-c` so it loads the same host config;
    # None lets it fall back to env/CWD discovery (the child's own mode).
    config_path: Path | None = None


async def _end_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate → grace → kill; tolerant of an already-exited child."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def spawn_converge(cmd: list[str]) -> tuple[int, str]:
    """Default spawn: run the converge CLI, return ``(returncode, output)``.

    A run past :data:`RUN_TIMEOUT_SECONDS` is ended and reported as rc -1 —
    the single-flight slot must never be held by a hung container. And a
    **cancellation** (watcher shutdown, PR #346 review F5) ends the child
    too before re-raising: an orphaned converge could keep fixing and
    pushing after loom stopped, and a restarted watcher would violate the
    global single-flight against it.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT_SECONDS)
    except TimeoutError:
        await _end_process(proc)
        return -1, f"converge run exceeded {RUN_TIMEOUT_SECONDS}s and was killed"
    except asyncio.CancelledError:
        await _end_process(proc)
        raise
    return proc.returncode if proc.returncode is not None else -1, out.decode(
        "utf-8", errors="replace"
    )


class ExternalRemediation:
    """Owns the single-flight dispatch of ``develop converge --from-github``.

    One instance per watcher child; ``_task`` is the global in-flight slot.
    All ``external_remediation`` marker writes happen either in the sweep
    while the slot is idle, or inside the run task while it is busy — never
    both at once — so the no-CAS ``task_update`` merge stays race-free.
    """

    def __init__(self, settings: RemediationSettings, *, spawn: Spawn | None = None):
        self._settings = settings
        self._spawn: Spawn = spawn if spawn is not None else spawn_converge
        self._task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def observe_head(
        self, gate: Any, spec: PrGateSpec, pr: Any, ctx: SubscriptionContext
    ) -> RemediationBudget:
        """Track the PR head and apply the human-push reset. Never raises.

        Inert while a run is in flight: loom's own converge may push at any
        moment, so a moved head cannot be attributed (the run's completion
        records its own push before the slot frees). If the run crashes
        after pushing but before recording, the next sweep misattributes
        that one push as human and resets — rare, and it errs toward more
        remediation headroom, never a stuck loop.
        """
        budget = read_budget(gate, spec.pr_url)
        head = getattr(pr, "head_sha", "") or ""
        if self.busy or not head or head == budget.last_seen_head_sha:
            return budget
        # A moved head is a HUMAN push unless it matches loom's own recorded
        # push; an empty previous sighting is first-time initialization only
        # (PR #346 review F2 — gating the reset on a non-empty
        # last_loom_pushed_sha left a PR whose spending rounds never pushed
        # permanently exhausted: a human push could then never reset it).
        if budget.last_seen_head_sha and head != budget.last_loom_pushed_sha:
            ctx.logger.info(
                "external-remediation: head of %s moved to %s (not loom's %s) — "
                "human push, resetting budget (was %d round(s) used)",
                spec.pr_url,
                head[:12],
                budget.last_loom_pushed_sha[:12] or "(never pushed)",
                budget.rounds_used,
            )
            budget = dataclasses.replace(budget, rounds_used=0, last_seen_head_sha=head)
        else:
            budget = dataclasses.replace(budget, last_seen_head_sha=head)
        await write_marker(
            ctx,
            task_id=gate.id,
            marker={REMEDIATION_KEY: budget.as_marker()},
            subsystem="external-remediation",
        )
        return budget

    def exhaustion_note(self, budget: RemediationBudget) -> str | None:
        """The S5b exhaustion sentence for the ``[ExternalReview]`` body.

        ``None`` while rounds remain — and when the budget is 0 (the operator
        disabled autonomous dispatch on purpose; that is not an exhaustion).
        """
        limit = self._settings.budget
        if limit <= 0 or budget.rounds_used < limit:
            return None
        return (
            f"remediation budget exhausted ({budget.rounds_used}/{limit} "
            "round(s) used) — findings will be reported but not auto-fixed "
            "until a human pushes or merges"
        )

    async def consider(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        budget: RemediationBudget,
        ingest: IngestResult,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> str:
        """Decide whether the just-posted batch dispatches a converge run.

        Returns a label for the sweep log; ``"dispatched"`` means the budget
        reservation landed and the run task started. Never raises.

        The pending trigger this decision leans on is parked by INGESTION,
        atomically with the batch's high-water marks (PR #346 review F1 +
        re-review 1 — the marks consume the batch, so its dispatch debt must
        become durable in the same write; a post-hoc write here could fail
        after the marks landed and lose the batch). consider() then shapes
        the trigger's fate: an undispatchable batch clears it, a busy slot
        leaves it for :meth:`resume_pending`, a dispatch consumes it with
        the reservation.
        """
        settings = self._settings
        if settings.budget <= 0:
            return "disabled"
        if budget.rounds_used >= settings.budget:
            return "exhausted"  # the note already rode out on the finding
        if story_id is None:
            return "no_story"  # nowhere to record the outcome

        dispatchable = await self._dispatchable(ingest, spec.repo, budget, github, ctx)
        if dispatchable != "yes":
            # This batch will never dispatch — drop the trigger ingestion
            # parked with its marks, or a pointless no-op run fires later.
            await self._clear_pending(gate.id, ctx)
            return dispatchable

        if self.busy:
            ctx.logger.info(
                "external-remediation: a run is already in flight for another "
                "batch; %s stays parked on its pending trigger (detection "
                "continues; a later sweep resumes it)",
                spec.pr_url,
            )
            return "deferred_busy"

        return await self._dispatch(gate, spec, story_id, budget, ctx)

    def pending_marker(
        self, spec: PrGateSpec, story_id: str | None
    ) -> dict[str, Any] | None:
        """The pending-trigger entry the sweep folds into ingestion's atomic
        marker write (PR #346 re-review 1).

        Minted for every actionable batch a dispatch could ever serve
        (budget on, a story to record to) — atomicity over precision: trust
        isn't known pre-ingest, so an over-parked trigger is cleared by
        consider() or resolved by one refunded no-op resume.
        """
        if self._settings.budget <= 0 or story_id is None:
            return None
        return {PENDING_KEY: {"pr_url": spec.pr_url}}

    async def _clear_pending(self, gate_id: Any, ctx: SubscriptionContext) -> None:
        """Drop a parked trigger for an outcome that will never dispatch.

        Best-effort, and unconditional (the caller's gate snapshot predates
        ingestion's parking write): a failed or needless clear costs at most
        one refunded no-op resume.
        """
        await write_marker(
            ctx,
            task_id=gate_id,
            marker={PENDING_KEY: None},
            subsystem="external-remediation",
        )

    async def resume_pending(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str | None,
        budget: RemediationBudget,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> str | None:
        """Fire a parked pending trigger on a sweep with no new batch.

        ``None`` when no trigger is parked for this PR url. The resumed
        dispatch skips the batch pre-filter (the material is gone — its marks
        were advanced when it posted); converge re-fetches the PR's live
        findings and re-applies the trust line itself, and a run that finds
        nothing live exits 0-without-JSON, refunding the round. The trigger
        survives a busy slot and an exhausted budget (a human push resets the
        budget and the trigger then fires) and is consumed atomically with
        the budget reservation on dispatch.
        """
        raw = gate.metadata.get(PENDING_KEY)
        if not isinstance(raw, dict) or raw.get("pr_url") != spec.pr_url:
            return None
        if self._settings.budget <= 0:
            return "disabled"
        if self.busy:
            return "deferred_busy"  # trigger stays parked
        if budget.rounds_used >= self._settings.budget:
            return "exhausted"  # trigger stays parked for after a reset
        if story_id is None:
            return "no_story"
        ctx.logger.info(
            "external-remediation: resuming the pending trigger for %s",
            spec.pr_url,
        )
        return await self._dispatch(gate, spec, story_id, budget, ctx)

    async def _dispatch(
        self,
        gate: Any,
        spec: PrGateSpec,
        story_id: str,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> str:
        """The shared dispatch tail: project resolve → reserve → spawn."""
        repo_path = await self._project_repo(gate, story_id, ctx)
        if repo_path is None:
            ctx.logger.warning(
                "[Friction] external-remediation: no project repo resolvable "
                "for gate %s (%s); cannot dispatch converge — map the project "
                "under [projects] or record metadata.project",
                gate.id,
                spec.pr_url,
            )
            await self._clear_pending(gate.id, ctx)
            return "no_project"
        slug, repo = repo_path
        enabled = await self._project_converge_enabled(slug, ctx)
        if enabled is None:
            # PR #346 re-review 2: the context doc may hold an explicit
            # opt-out we could not read — an unknown dial must never
            # authorize an autonomous code-pushing run. Fail closed and
            # LEAVE the pending trigger parked so the decision retries.
            ctx.logger.warning(
                "[Friction] external-remediation: cannot read project %r "
                "settings to check %s; failing closed — no dispatch, the "
                "pending trigger (if parked) retries next sweep",
                slug,
                CONVERGE_SETTING,
            )
            return "project_settings_unavailable"
        if not enabled:
            ctx.logger.info(
                "external-remediation: project %r disables %s; reporting only",
                slug,
                CONVERGE_SETTING,
            )
            await self._clear_pending(gate.id, ctx)
            return "project_disabled"

        # Reserve the round BEFORE the run (crash-safe: a lost refund wastes
        # one round; a lost increment would allow an unbounded retry loop) —
        # and STRICTLY (PR #346 review F3): write_marker swallows failures,
        # so the bound only exists if this write demonstrably landed. The
        # same update consumes any pending trigger (per-key merge: None
        # deletes), so trigger and reservation move together.
        budget = dataclasses.replace(budget, rounds_used=budget.rounds_used + 1)
        try:
            await ctx.lithos.task_update(
                task_id=gate.id,
                metadata={REMEDIATION_KEY: budget.as_marker(), PENDING_KEY: None},
            )
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] external-remediation: budget reservation for gate "
                "%s failed (%s); not dispatching — will retry next sweep",
                gate.id,
                exc,
            )
            return "reservation_failed"
        ctx.logger.info(
            "external-remediation: dispatching converge --from-github for %s "
            "(round %d/%d)",
            spec.pr_url,
            budget.rounds_used,
            self._settings.budget,
        )
        self._task = asyncio.create_task(
            self._run(gate.id, story_id, spec, repo, budget, ctx),
            name=f"external-remediation-{spec.pr_number}",
        )
        return "dispatched"

    async def shutdown(self) -> None:
        """Cancel + await the in-flight run (PR #346 review F5).

        The cancellation-safe spawn terminates the converge subprocess, so
        loom's stop never orphans a child that could keep pushing — and a
        restarted watcher's single-flight slot starts truly empty.
        """
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # ── decision helpers ───────────────────────────────────────────────

    async def _dispatchable(
        self,
        ingest: IngestResult,
        repo: str,
        budget: RemediationBudget,
        github: GitHubClient,
        ctx: SubscriptionContext,
    ) -> str:
        """``"yes"`` / ``"no_trusted"`` / ``"own_sha_only"`` for the batch."""
        items = [(r.author, r.commit_id) for r in ingest.actionable_reviews] + [
            (c.author, c.commit_id or c.original_commit_id)
            for c in ingest.actionable_comments
        ]
        trusted_cache: dict[str, bool] = {}
        any_trusted = False
        for author, sha in items:
            trusted = trusted_cache.get(author)
            if trusted is None:
                trusted = await self._trusted(author, repo, github, ctx)
                trusted_cache[author] = trusted
            if not trusted:
                continue
            any_trusted = True
            if budget.last_loom_pushed_sha and sha == budget.last_loom_pushed_sha:
                continue  # a re-review of loom's own fix in flight
            return "yes"
        return "own_sha_only" if any_trusted else "no_trusted"

    async def _trusted(
        self, author: str, repo: str, github: GitHubClient, ctx: SubscriptionContext
    ) -> bool:
        if author in self._settings.trusted_bots:
            return True
        try:
            permission = await github.get_collaborator_permission(repo, author)
        except GitHubError:
            # Fail closed for dispatch: an unverifiable author never triggers
            # an agent run. Detection already reported the material.
            return False
        return permission in _TRUSTED_PERMISSIONS

    async def _project_repo(
        self, gate: Any, story_id: str, ctx: SubscriptionContext
    ) -> tuple[str, Path] | None:
        """``(slug, repo_path)`` via gate metadata, falling back to the story's
        (gate creation records ``project`` only conditionally)."""
        slug = gate.metadata.get("project")
        if not isinstance(slug, str) or not slug:
            slug = None
            try:
                story = await ctx.lithos.task_get(task_id=story_id)
            except LithosClientError:
                story = None
            if story is not None:
                candidate = story.metadata.get("project")
                if isinstance(candidate, str) and candidate:
                    slug = candidate
        if slug is None:
            return None
        repo = self._settings.projects.get(slug)
        return None if repo is None else (slug, repo)

    async def _project_converge_enabled(
        self, slug: str, ctx: SubscriptionContext
    ) -> bool | None:
        """The per-project dial, default **on** (ADR 0011 decision 6).

        Reads the context doc's metadata directly (canonical path, then the
        smallest ``project-context``-tagged doc — the same resolution
        ``daemon_io._fetch_context_metadata`` applies; kept as a local
        seven-liner rather than importing the Plugins component into
        Subscriptions, which would add a cross-component edge for one read).

        Tri-state (PR #346 re-review 2): a READABLE doc with the key absent
        or malformed is the documented default-on (warned when malformed);
        an UNREADABLE doc returns ``None`` — the project may hold an
        explicit opt-out we cannot see, and an unknown safety dial must
        never authorize a run (the caller fails closed and retries).
        """
        meta: Mapping[str, Any] | None = None
        try:
            note = await ctx.lithos.note_read(
                path=f"projects/{slug}/{slug}-project-context.md"
            )
            if note is not None:
                meta = note.metadata
            else:
                candidates = await ctx.lithos.note_list(
                    path_prefix=f"projects/{slug}/", tags=["project-context"]
                )
                if candidates:
                    meta = min(candidates, key=lambda n: n.path).metadata
        except LithosClientError:
            return None  # unreadable ≠ unset — the caller fails closed
        raw = None if meta is None else meta.get(CONVERGE_SETTING)
        if raw is None:
            return True
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
            return raw.strip().lower() == "true"
        ctx.logger.warning(
            "[Friction] external-remediation: project %r has malformed %s=%r; "
            "treating as enabled",
            slug,
            CONVERGE_SETTING,
            raw,
        )
        return True

    # ── the run itself ─────────────────────────────────────────────────

    def _command(self, spec: PrGateSpec, repo: Path, json_path: Path) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "lithos_loom",
            "develop",
            "converge",
            str(spec.pr_number),
            "--from-github",
            "--repo",
            str(repo),
            "--json",
            str(json_path),
        ]
        if self._settings.config_path is not None:
            cmd += ["-c", str(self._settings.config_path)]
        return cmd

    async def _run(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        """Run one converge subprocess and record its outcome. Never raises."""
        try:
            await self._run_inner(gate_id, story_id, spec, repo, budget, ctx)
        except Exception:  # noqa: BLE001 — the slot must always free cleanly
            ctx.logger.exception("external-remediation: run for %s raised", spec.pr_url)

    async def _run_inner(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        repo: Path,
        budget: RemediationBudget,
        ctx: SubscriptionContext,
    ) -> None:
        json_path = (
            self._settings.work_dir / "github-watcher" / f"remediation-{gate_id}.json"
        )
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.unlink(missing_ok=True)

        rc, output = await self._spawn(self._command(spec, repo, json_path))

        data: dict[str, Any] | None = None
        try:
            import json as _json

            data = _json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None

        if data is not None:
            await self._record_result(gate_id, story_id, spec, budget, data, ctx)
            return
        if rc == 0:
            # "nothing to ingest": no agent time spent — give the round back.
            ctx.logger.info(
                "external-remediation: converge for %s found nothing live to "
                "ingest; returning the budget round",
                spec.pr_url,
            )
            refund = dataclasses.replace(
                budget, rounds_used=max(0, budget.rounds_used - 1)
            )
            await write_marker(
                ctx,
                task_id=gate_id,
                marker={REMEDIATION_KEY: refund.as_marker()},
                subsystem="external-remediation",
            )
            return
        tail = output[-_OUTPUT_TAIL_CHARS:] if output else "(no output)"
        await self._post_finding(
            story_id,
            f"[Friction] external-remediation: converge --from-github for "
            f"{spec.pr_url} failed (exit {rc}) without a result; the round is "
            f"spent ({budget.rounds_used}/{self._settings.budget}). Output "
            f"tail: {tail}",
            ctx,
        )

    async def _record_result(
        self,
        gate_id: str,
        story_id: str,
        spec: PrGateSpec,
        budget: RemediationBudget,
        data: dict[str, Any],
        ctx: SubscriptionContext,
    ) -> None:
        status = data.get("status", "unknown")
        pushed_sha = data.get("pushed_sha") or ""
        if data.get("pushed") and pushed_sha:
            # Loom's own push: recorded so the next sweep's head observation
            # attributes it (no human-push reset) and own-sha material skips.
            updated = dataclasses.replace(
                budget,
                last_loom_pushed_sha=pushed_sha,
                last_seen_head_sha=pushed_sha,
            )
            await write_marker(
                ctx,
                task_id=gate_id,
                marker={REMEDIATION_KEY: updated.as_marker()},
                subsystem="external-remediation",
            )

        lines = [
            f"{EXTERNAL_REVIEW} remediation outcome for delivered PR "
            f"{spec.pr_url}: {status} "
            f"(round {budget.rounds_used}/{self._settings.budget})"
        ]
        if data.get("message"):
            lines.append(f"- {data['message']}")
        if pushed_sha:
            lines.append(f"- pushed {pushed_sha[:12]} to the PR branch")
        for o in data.get("external_outcomes") or []:
            if not isinstance(o, dict):
                continue
            where = f" ({o['thread_url']})" if o.get("thread_url") else ""
            detail = f" — {o['detail']}" if o.get("detail") else ""
            lines.append(
                f"- {o.get('finding_id', '?')} by {o.get('author', '?')}: "
                f"{o.get('disposition', '?')}{detail}{where}"
            )
        cost = data.get("total_cost_usd")
        if isinstance(cost, int | float):
            lines.append(f"- spend ${cost:.2f}")
        await self._post_finding(story_id, "\n".join(lines), ctx)

    async def _post_finding(
        self, story_id: str, summary: str, ctx: SubscriptionContext
    ) -> None:
        """Best-effort finding post (the story may have completed mid-run)."""
        try:
            await ctx.lithos.finding_post(task_id=story_id, summary=summary)
        except LithosClientError as exc:
            ctx.logger.warning(
                "[Friction] external-remediation: posting outcome for story %s "
                "failed (%s)",
                story_id,
                exc,
            )

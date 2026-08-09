"""Deterministic gate execution for ``story-develop`` (ARCH-1.S2).

The Review-Profile check-set — *which* deterministic checks run, how each is
resolved against the container image, how one round's commit is exported and each
check run in its own throwaway container, and how a **required** check's verdict
decides the approval floor — all lives here, behind a small public surface:

* :func:`build_check_set` — the profile-selected, ecosystem-resolved checks;
* :func:`run_check_set` — run an ordered check-set against one round commit;
* :func:`check_result_blocks` / :func:`gate_floor_blocks` — the required-check
  floor decision (shared verbatim by ``develop`` and review-only, #154);
* :func:`merge_check_sets` — the fast + approval-candidate merge (#140);
* :func:`load_gate_ledger` / :func:`persist_gate_ledger` — the run's
  deterministic-finding ledger (#132), reloaded on resume;
* :func:`run_delivery_test_gate` — the *delivery* regression gate (test-only,
  ledger-less) — the intentional delivery-vs-develop divergence (#140), now a
  named policy function instead of an inline filter in :mod:`pr_delivery`.

This module is engine-blind and imports no ``develop`` symbols (``develop``
imports *this*): the round pipeline drives these functions, review-only reuses
them, and delivery calls the one policy wrapper — one implementation, no drift.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from ...runner import detection
from . import check_catalog, containers, gate_adapters, profiles, test_gate
from .check_set import (
    Check,
    CheckResult,
    CheckSetResult,
    CheckState,
    Stage,
    classify_execution,
)
from .config import DEFAULT_BLOCK_THRESHOLD, DevelopConfig
from .gate_findings import GateLedger
from .test_gate import GateResult

logger = logging.getLogger(__name__)

# #273 slice 3: the stable name of the aggregate repo-parity gate check (`make check`
# and the like). Not a per-ecosystem catalog check — an extra candidate-stage check
# appended when ``config.parity_command`` is set. A distinct, greppable check name so
# its gate output / findings are attributable.
PARITY_CHECK_NAME = "repo-parity"


def _resolve_test_command(config: DevelopConfig, wt: Path) -> str | None:
    """Pick the command the ``test`` check will run, or ``None`` when none is
    runnable.

    An explicit ``test_command`` is trusted as-is; otherwise candidates are
    auto-detected from the worktree and the first one whose tool exists in the
    container image wins (the image may lack e.g. ``make`` — see
    :mod:`...runner.detection`). #133 swaps in per-ecosystem resolution behind
    this one call.
    """
    if config.test_command:
        return config.test_command
    candidates = detection.detect_test_commands(wt)
    if not candidates:
        logger.info(
            "story-develop %s: test gate skipped (no test command detected)",
            config.run_id,
        )
        return None
    tools = list(dict.fromkeys(c.split()[0] for c in candidates))
    chosen = test_gate.select_command(
        candidates, test_gate.probe_tools(config.image, tools)
    )
    if chosen is None:
        logger.warning(
            "story-develop %s: test gate skipped — none of %s runnable in %s; "
            "set --test-command explicitly",
            config.run_id,
            candidates,
            config.image,
        )
    return chosen


EffectiveState = Literal["required", "informational", "off"]


def effective_check_state(
    config: DevelopConfig, name: str, profile_state: CheckState
) -> EffectiveState:
    """The check's blocking state after operator overrides (#273 slice 2).

    Precedence: an explicit ``config.check_states[name]`` wins; otherwise, for the
    ``test`` check, the legacy ``test_gate=false`` is honoured as ``off`` (back-compat
    sugar for ``check_states={"test": "off"}``); else the profile's declared state.
    Returns ``required`` / ``informational`` / ``off`` — ``off`` means the caller drops
    the check (runs nothing, records nothing), distinct from a tool-absent required
    check (which still blocks). Values are validated at parse time
    (:func:`config.parse_check_states`), so the cast is safe.
    """
    override = config.check_states.get(name)
    if override is not None:
        return cast(EffectiveState, override)
    if name == "test" and not config.test_gate:
        return "off"
    # A profile only declares required / informational for a real gate check, both of
    # which are valid effective states.
    return cast(EffectiveState, profile_state)


def build_check_set(config: DevelopConfig, wt: Path) -> tuple[Check, ...]:
    """The Review-Profile-selected check-set for this run (#140, ADR §3/§4).

    The resolved profile (``config.review_profile`` -> :func:`profiles.get_profile`)
    selects WHICH deterministic checks run, and each resulting :class:`Check` is
    tagged with its profile ``stage`` (``fast`` every round / ``candidate`` on the
    approval candidate — the round-loop filter in :func:`develop` acts on it).

    The ``test`` check keeps its ``test_gate`` (include/exclude) / ``test_command``
    semantics (#127/#159, ADR §10), with its blocking ``state`` from the profile's
    ``ProfileCheck("test", ...)`` like every other check. Every *other* profile
    check now runs at its **declared ``state``** (#140 floor slice): a ``required``
    check blocks approval (its verdict read from the finding ledger's severity for
    adapter tools, or the raw exit code otherwise — see :func:`gate_floor_blocks`),
    while an ``informational`` check is surfaced-only. A *required* check whose tool is
    absent from the image is an expected-but-absent **blocking placeholder**, not a
    silent drop; an *informational* absent check is dropped. Where a check's result is
    *surfaced* depends on its stage (see :func:`develop`): a ``fast`` check runs before
    the panel each round and feeds the coder + reviewer prompts (ADR §6), while a
    ``candidate`` check runs only on the approval candidate and so reaches the gate
    ledger + ``[DevelopResult]`` but — on the common approve-immediately path — not the
    panel. The ``format`` check is declared by the profile but is not run as a
    standalone gate check — its live pass is the :mod:`autoformat` write-mode pass
    (#134), which reformats the round commit before the gate + panel. Checks are in
    profile order.
    """
    profile = profiles.get_profile(config.review_profile)
    ecosystems = detection.detect_ecosystems(wt)
    # #273 slice 2 coupling guard (ADR/#273 Refinement 3): on a compiled-language repo
    # the compiler / test run IS the type check (there is no separate `typecheck`
    # check), so turning the `test` check off silently removes type checking too. Warn
    # loudly rather than let it pass unnoticed.
    if effective_check_state(config, "test", "required") == "off" and any(
        e in ("rust", "go") for e in ecosystems
    ):
        logger.warning(
            "story-develop %s: the `test` check is off on a compiled-language repo "
            "(%s) — the compiler / test run is the type check there, so type errors "
            "will no longer be caught by the gate",
            config.run_id,
            ", ".join(ecosystems),
        )
    # Group the resolved profile checks back by bare name (a polyglot check is emitted
    # once per ecosystem as ``<check>.<ecosystem>``), so they can be slotted into
    # profile order alongside the specially-built ``test`` check.
    by_name: dict[str, list[Check]] = {}
    for c in _build_profile_checks(config, profile, ecosystems, wt):
        by_name.setdefault(c.name.split(".")[0], []).append(c)
    checks: list[Check] = []
    for pc in profile.checks:
        if pc.name == "test":
            # #273 slice 2: apply the effective state (check_states / legacy test_gate);
            # `off` drops the test check, mirroring the old `test_gate=false` behaviour.
            state = effective_check_state(config, "test", pc.state)
            if state != "off":
                checks.extend(_build_test_check(config, state, ecosystems, wt))
        else:
            checks.extend(by_name.get(pc.name, []))
    # #273 slice 3: the repo-parity aggregate check, appended last at CANDIDATE stage
    # (cost — once on the approval candidate, not every round). Runs the repo's own
    # aggregate command (`config.parity_command`, e.g. `make check`) VERBATIM and reads
    # its raw exit (`raw_exit` — no adapter, blocks on non-zero, raw output tail to the
    # coder). It runs regardless of detected ecosystems, so it is the PRIMARY gate for a
    # repo the per-check catalog can't model (C/C++). None → no parity check.
    if config.parity_command:
        checks.append(
            Check(
                name=PARITY_CHECK_NAME,
                command=config.parity_command,
                state="required",
                stage="candidate",
                raw_exit=True,
            )
        )
    return tuple(checks)


def _build_test_check(
    config: DevelopConfig,
    state: CheckState,
    ecosystems: Sequence[detection.Ecosystem],
    wt: Path,
) -> list[Check]:
    """The ``test`` check, with its **effective** ``state`` (#127/#159/#273, ADR §4).

    ``state`` is the effective blocking state resolved by
    :func:`effective_check_state` (``check_states["test"]`` / the legacy
    ``test_gate=false`` / the profile default) — the caller has already dropped the
    check when that resolves to ``off`` (the old ``test_gate=false`` escape hatch, now a
    special case of the per-check 3-state). Its blocking is that ``state``: ``required``
    (a RED run blocks + feeds the coder) vs ``informational`` (recorded, non-blocking),
    the single source of truth, like every other check. #133/ADR §4: when no command is
    runnable but the detected ecosystem expects tests, a *required* test check is an
    **expected-but-absent** blocking placeholder (empty command; the runner records it
    ``absent``), not a silent skip.
    """
    command = _resolve_test_command(config, wt)
    if command is not None:
        return [Check(name="test", command=command, state=state)]
    if state == "required" and check_catalog.applies("test", ecosystems):
        return [Check(name="test", command="", state="required")]
    return []


def _build_profile_checks(
    config: DevelopConfig,
    profile: profiles.ReviewProfile,
    ecosystems: Sequence[detection.Ecosystem],
    wt: Path,
) -> list[Check]:
    """Resolve every *non-test* profile check for the detected ecosystem(s), honouring
    each check's **declared ``state``** — #140 floor slice.

    A profile check carries its own ``state`` (``required`` blocks, ``informational``
    is surfaced-only). It is resolved against the catalog and the **real** image
    availability so the catalog's designed classification applies: ``required`` +
    tool-present -> a real command; ``required`` + tool-absent -> an expected-but-absent
    **blocking placeholder** (empty command; the runner records ``absent``);
    ``informational`` + tool-absent -> dropped (a silent skip). The image is probed
    **once** (a first pass with every tool assumed present enumerates the candidate
    tools); surviving real commands are machine-ified — a finding-producing tool
    (ruff / bandit / pip-audit) emits JSON parsed into the gate ledger by
    :func:`run_check_set`, a no-adapter tool (pyright / coverage / semgrep) runs
    as-is. Each resulting :class:`Check` carries its profile ``stage``. ``format`` is
    skipped here (its live pass is the :mod:`autoformat` write-mode pass, #134). Empty
    for a markerless repo.
    """
    if not ecosystems:
        return []
    # Env-dependent checks (typecheck/dep-audit/coverage) run via `uv run` on a
    # uv-managed repo so they resolve against the project venv in the gate container,
    # like the `test` check already does (#165). Bare, pyright/pip-audit see the
    # container's empty environment and false-positive.
    uv_managed = detection.is_uv_managed(wt)
    # A profile declares its checks ecosystem-agnostically, but several are
    # language-specific (typecheck → pyright/tsc, sast → bandit/semgrep — python/node
    # only). A required such check on a repo whose ecosystem has no analogue (e.g.
    # `typecheck` on Rust/Go) is **not** an operator error — it is simply N/A for that
    # language. Pre-filter to checks that apply to a detected ecosystem so the canonical
    # default profile degrades gracefully, rather than letting `resolve_check_set` raise
    # `CheckApplicabilityError` (its error is reserved for a hand-curated desired set
    # that explicitly requires an unsupported check, #133 AC3).
    # #273 slice 2: each check runs at its EFFECTIVE state (check_states override →
    # legacy → profile). ``off`` drops it entirely — a clean skip (nothing runs, nothing
    # records), distinct from a required tool-absent check (an expected-but-absent
    # blocking placeholder). ``required`` / ``informational`` become the check's state.
    all_desired: list[check_catalog.DesiredCheck] = []
    for pc in profile.checks:
        if pc.name in ("test", "format") or not check_catalog.applies(
            pc.name, ecosystems
        ):
            continue
        eff_state = effective_check_state(config, pc.name, pc.state)
        if eff_state == "off":
            continue
        # eff_state is now `required` | `informational` — both valid CheckStates.
        all_desired.append(check_catalog.DesiredCheck(pc.name, eff_state))
    stage_by_name: dict[str, Stage] = {pc.name: pc.stage for pc in profile.checks}
    default_stage: Stage = "fast"
    out: list[Check] = []
    # #273: a check the repo overrides with its own command runs that command VERBATIM
    # — trusted as-is (no uv-wrap, no tool-probe, no catalog lookup, and **not**
    # machine-ified — exactly like `test_command`) — and beats catalog discovery. This
    # is the fix for a canonical command that over-scopes vs the repo's real policy
    # (bare `uv run pyright` scanning a test tree full of pre-existing type debt vs the
    # repo's scoped `make typecheck`). `raw_exit=True` makes the check read its raw exit
    # code end-to-end (the ledger-apply + floor both skip the adapter path), so an
    # override whose command *begins* with an adapter tool (`ruff …`) is still run
    # exactly as written — no JSON/exit-zero flags appended to the operator's string —
    # at the cost of opting that check out of structured findings. The override applies
    # only where the canonical check applies to a detected ecosystem (an override for an
    # N/A check is inert). Only the non-overridden remainder go through catalog
    # resolution below.
    overridden = [d for d in all_desired if d.name in config.check_commands]
    desired = [d for d in all_desired if d.name not in config.check_commands]
    for d in overridden:
        out.append(
            Check(
                name=d.name,
                command=config.check_commands[d.name],
                state=d.state,
                stage=stage_by_name.get(d.name, default_stage),
                raw_exit=True,
            )
        )
    # Pass 1: enumerate candidate commands (every tool assumed present) so the image
    # can be probed once for the tools this profile would run.
    candidates = check_catalog.resolve_check_set(
        desired, ecosystems, tool_available=lambda _t: True, uv_managed=uv_managed
    )
    available = set(
        test_gate.probe_tools(
            config.image, [c.command.split()[0] for c in candidates if c.command]
        )
    )
    # Pass 2: resolve with the real availability — now a *required* absent tool becomes
    # an empty-command blocking placeholder and an *informational* absent tool is
    # dropped (the catalog's own classification, not a hand-rolled post-filter).
    resolved = check_catalog.resolve_check_set(
        desired,
        ecosystems,
        tool_available=lambda t: t in available,
        uv_managed=uv_managed,
    )
    for c in resolved:
        stage = stage_by_name.get(c.name.split(".")[0], "fast")
        if c.command:
            # Resolve the real tool past any `uv run` prefix so a uv-wrapped adapter
            # (e.g. `uv run pip-audit`) is still machine-ified (#165).
            command = gate_adapters.machine_command(
                gate_adapters.command_tool(c.command), c.command
            )
            out.append(replace(c, command=command, stage=stage))
        else:
            # Expected-but-absent blocking placeholder: keep the empty command (the
            # runner records ``absent``); never machine-ify "" (no ``"".split()[0]``).
            out.append(replace(c, stage=stage))
    return out


def merge_check_sets(
    base: CheckSetResult | None, extra: CheckSetResult | None
) -> CheckSetResult | None:
    """Append *extra*'s results to *base* (the approval-candidate merge, #140).

    Either side may be ``None`` (no fast checks ran, or the candidate export
    errored); the result preserves order — fast results then candidate results.
    """
    if base is None:
        return extra
    if extra is None:
        return base
    return CheckSetResult(results=base.results + extra.results)


def check_result_blocks(
    r: CheckResult,
    gate_ledger: GateLedger | None,
    threshold: str = DEFAULT_BLOCK_THRESHOLD,
) -> bool:
    """Whether a single **required** check holds approval (#140, ADR §4/§5).

    The per-result core of :func:`gate_floor_blocks`, factored out so review-only
    mode (#154) can report each check's block decision with the *same* logic the
    floor uses — one decision, no drift between the develop loop and review-only.

    Unlike :meth:`CheckResult.passed` (raw exit code), an adapter-backed required
    check (ruff / bandit / pip-audit) reads its verdict from the finding
    **ledger's mapped severity** at *threshold* — the exit code never decides
    approval for a finding-producing tool (ADR §5/#132 finding-2); an adapter that
    exited red with no open findings is treated as having failed to run and blocks
    (#167 floor-liveness). A check with no adapter (pyright / pytest / coverage /
    semgrep) still reads the raw exit code. An **informational** check never blocks
    (returns ``False``) even though its findings share *gate_ledger*. An
    expected-but-absent or timed-out required check blocks structurally; an infra
    ``errored`` skip and a declared ``n_a`` never block.
    """
    if r.check.state != "required":
        return False
    outcome = r.execution_outcome
    if outcome in ("errored", "n_a"):
        return False
    if outcome in ("absent", "timed_out"):
        return True
    # Ran: the tool decides how to read the verdict. Resolve the real tool past
    # any `uv run` prefix (#165) so a uv-wrapped adapter (`uv run pip-audit`) is
    # detected — `command_tool` is "" for an empty command, so a reorder can never
    # hit ``"".split()[0]``.
    tool = gate_adapters.command_tool(r.check.command)
    # #273: a verbatim override (raw_exit) reads its raw exit code even when its command
    # begins with an adapter tool — the operator ran their own command, not the JSON
    # adapter form, so there is no finding ledger to read; skip straight to raw exit.
    if (
        not r.check.raw_exit
        and gate_ledger is not None
        and tool in gate_adapters.SUPPORTED_TOOLS
    ):
        if any(f.check == r.check.name for f in gate_ledger.blocking(threshold)):
            return True
        # Floor-liveness (#167): an adapter exits clean via `--exit-zero` / a clean
        # scan, so a required adapter check that exited RED with NO open findings
        # for it FAILED TO RUN (spawn / crash / un-parseable output) and must block
        # — the ledger-severity read alone can't tell "ran clean" (exit 0, empty)
        # from "failed to run" (red, empty). apply_round closes a check's findings
        # only when it ran, so a failed run leaves zero open findings; a clean
        # exit-0 run or below-threshold open findings (the tool ran) still pass.
        ran_ok = r.gate is not None and r.gate.passed
        has_open = any(f.check == r.check.name for f in gate_ledger.open_findings())
        return not ran_ok and not has_open
    return r.gate is None or not r.gate.passed


def gate_floor_blocks(
    check_set: CheckSetResult | None,
    gate_ledger: GateLedger | None,
    threshold: str = DEFAULT_BLOCK_THRESHOLD,
) -> bool:
    """Whether the deterministic floor blocks approval (#140, ADR §4/§5).

    Returns ``True`` iff any **required** check blocks — see
    :func:`check_result_blocks` for the per-check rule. A ``None`` check-set
    (markerless repo) never blocks. Only ``required`` checks count, so an
    **informational** check never blocks even though its findings share
    *gate_ledger* — this is what keeps `sast` (bandit), informational on the
    default `standard` profile, from blocking the default.
    """
    if check_set is None:
        return False
    return any(
        check_result_blocks(r, gate_ledger, threshold) for r in check_set.results
    )


def _write_check_output(round_dir: Path, check: Check, gate: GateResult | None) -> None:
    """Write a check's container output for operator inspection. The ``test``
    check writes ``output.txt`` (back-compat path); any other check writes
    ``output_<name>.txt``. Nothing is written when the check never ran."""
    if gate is None:
        return
    fname = "output.txt" if check.name == "test" else f"output_{check.name}.txt"
    (round_dir / fname).write_text(
        f"$ {gate.command}\nexit: {gate.exit_code} ({gate.verdict})\n\n"
        f"{gate.output_tail}\n",
        encoding="utf-8",
    )


def _collect_check_artifacts(
    config: DevelopConfig, tree: Path, round_no: int, check_name: str
) -> None:
    """Rescue a check's artifacts dir from its doomed tree export (#283).

    The per-check export is deleted right after the check runs (#282
    isolation), destroying anything the check rendered there — e.g. the e2e
    screenshots a repo-parity ``make e2e`` writes. When the project declares
    ``develop_artifacts_path``, copy that dir into the run handoff (the only
    per-run dir mounted into agent containers) under
    ``artifacts/round_NN/<check>/`` so reviewers can see it. Unconditional on
    the check's verdict — a RED e2e run's screenshots are exactly what a
    reviewer needs. Best-effort: a collection failure is logged, never fatal,
    and never blocks the tree cleanup that follows.
    """
    if not config.artifacts_path:
        return
    src = tree / config.artifacts_path
    try:
        if not src.is_dir() or not any(src.iterdir()):
            return
        dest = config.handoff_dir / "artifacts" / f"round_{round_no:02d}" / check_name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        logger.info(
            "story-develop %s: round %d %s artifacts collected to %s",
            config.run_id,
            round_no,
            check_name,
            dest,
        )
    except OSError as exc:
        logger.warning(
            "story-develop %s: round %d %s artifact collection failed (continuing): %s",
            config.run_id,
            round_no,
            check_name,
            exc,
        )


def run_check_set(
    config: DevelopConfig,
    wt: Path,
    sha: str,
    round_no: int,
    checks: tuple[Check, ...],
    gate_ledger: GateLedger | None = None,
) -> CheckSetResult | None:
    """Run an ordered check-set against one round commit.

    The committed tree is exported **fresh for every check** (#282): each check
    runs in its own throwaway container (no shell-chaining — each keeps its own
    verdict), but the tree mounts read-write, so without per-check re-export a
    check that mutates it (a test suite regenerating ``docs/generated``, a
    ``coverage`` pytest run) would leak that state into the NEXT check's input —
    order-dependent verdicts, and in the worst case a false-green ``repo-parity``
    judging an already-regenerated tree instead of the committed one. Each export
    lands in a **never-reused** per-check directory, so establishing a fresh tree
    never depends on deleting the previous check's (which a check can render
    undeletable — a mode-000 directory, other-UID files from a custom image);
    each tree is cleaned up best-effort afterwards (a retained one is logged —
    it can no longer affect correctness, but it is repo-plus-venv of disk).
    This makes every
    check's verdict independent of earlier checks' **workspace-tree** mutations
    (the persistent tool-cache mount is deliberately shared across checks and
    rounds — that is what makes the per-check venv re-sync cheap).

    Infra errors skip rather than fail the run (the gate is an independent
    check, not a dependency): a cache-dir failure returns ``None`` for the whole
    set, and a per-check export/run failure yields a ``CheckResult`` with
    ``execution_outcome="errored"`` and ``gate=None``. Returns ``None`` when
    there are no checks.

    #132: a finding-producing check (one whose tool has an adapter) has its full
    output parsed into *gate_ledger* (``apply_round`` per check that ran, so a
    green re-run closes its prior findings); the full output is then dropped so it
    never propagates into the result.
    """
    if not checks:
        return None
    round_dir = config.gate_dir / f"round_{round_no:02d}"
    try:
        cache = config.gate_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "story-develop %s: round %d gate cache dir errored (skipping): %s",
            config.run_id,
            round_no,
            exc,
        )
        return None
    results: list[CheckResult] = []
    for check in checks:
        # #133: an empty command is a non-running placeholder — an
        # expected-but-absent check (records ``absent``; a required one blocks)
        # or a declared not-applicable check (records ``n_a``). Neither runs a
        # container; their state in :class:`CheckResult.passed` decides blocking.
        if not check.command:
            outcome = "n_a" if check.state == "not_applicable" else "absent"
            results.append(
                CheckResult(check=check, execution_outcome=outcome, gate=None)
            )
            continue
        name = containers.container_name(
            config.run_id, f"gate-{check.name}-r{round_no}"
        )
        # #282 re-review: a NEVER-REUSED export dir per check. Establishing this
        # check's pristine tree must not depend on deleting the previous check's
        # (a check can leave undeletable state — a mode-000 directory, files
        # from a custom image running as another UID); with one shared path,
        # that rmtree failure recorded the NEXT required check as a
        # non-blocking infra error — skipped-as-satisfied, another false-green
        # route. The nonce also keeps a resume's re-run of the same check name
        # apart from a poisoned earlier attempt.
        tree = round_dir / f"tree-{check.name}-{uuid.uuid4().hex}"
        try:
            # #282: fresh export per check — this check's input is the committed
            # tree, never a predecessor's mutations or untracked droppings.
            test_gate.export_tree(wt, sha, tree)
            gate_cmd = test_gate.build_gate_command(
                name=name,
                image=config.image,
                tree=tree,
                cache_dir=cache,
                command=check.command,
            )
            gate = test_gate.run_gate_container(
                gate_cmd, name=name, command=check.command, timeout=config.test_timeout
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "story-develop %s: round %d %s check errored (skipping): %s",
                config.run_id,
                round_no,
                check.name,
                exc,
            )
            gate = None
        finally:
            _collect_check_artifacts(config, tree, round_no, check.name)
            # Best-effort: an undeletable tree just stays on disk — it is never
            # mounted again, so it cannot affect any later check — but loom is a
            # long-running daemon, so a retained export (repo + venv, per check)
            # is logged loudly rather than leaking silently.
            try:
                shutil.rmtree(tree)
            except FileNotFoundError:
                pass  # export failed before creating the dir
            except OSError as exc:
                logger.warning(
                    "story-develop %s: round %d %s export dir not cleaned "
                    "(retained at %s): %s",
                    config.run_id,
                    round_no,
                    check.name,
                    tree,
                    exc,
                )
        _write_check_output(round_dir, check, gate)
        if gate is not None:
            tool = gate_adapters.command_tool(check.command)
            if gate_ledger is not None:
                if check.raw_exit:
                    # #273: a verbatim override (raw_exit) has no structured
                    # findings — it ran the operator's command, not the adapter's
                    # JSON form. Retire the whole check FAMILY (bare name +
                    # polyglot-qualified `<name>.<eco>`, #278) so any findings a
                    # PRIOR adapter-backed round left open for this check (on a
                    # resume where it flipped to a raw override) stop surfacing as
                    # authoritative in the coder / reviewer prompts +
                    # ``[DevelopResult]`` (all read ``open_findings``) — matching the
                    # floor, which reads the raw exit for a raw check.
                    gate_ledger.retire_check_family(check.name, round_no)
                elif tool in gate_adapters.SUPPORTED_TOOLS:
                    # #132: structure a finding-producing check's output into the
                    # ledger, then drop the full output so it never propagates into
                    # the result. Resolve the real adapter tool past a `uv run` prefix
                    # or a pipeline producer (#167: `uv export … | pip-audit …` →
                    # pip-audit), like the build (#166) + floor sites — a bare
                    # `split()[0]` sees `uv` and skips the ledger, so dep-audit
                    # findings would never be structured.
                    gate_ledger.apply_round(
                        check.name,
                        gate_adapters.parse_findings(
                            check.name, tool, gate.full_output
                        ),
                        round_no,
                    )
            gate = replace(gate, full_output="")
            logger.info(
                "story-develop %s: round %d %s check %s (`%s`, exit %d)",
                config.run_id,
                round_no,
                check.name,
                gate.verdict,
                gate.command,
                gate.exit_code,
            )
        results.append(
            CheckResult(
                check=check, execution_outcome=classify_execution(gate), gate=gate
            )
        )
    return CheckSetResult(results=tuple(results))


def reconcile_off_check_states(config: DevelopConfig, gate_ledger: GateLedger) -> bool:
    """Retire any persisted findings for checks the operator has turned **off**
    (#273 slice 2 / #280 review).

    An ``off`` check is removed from the built check-set, so :func:`run_check_set`
    never runs it and thus never retires its findings. On a **resume** where an
    adapter-backed check (``lint`` / ``sast`` / ``dep-audit``) flipped to ``off``, its
    prior open findings would otherwise linger in the reloaded ledger and surface in the
    coder / reviewer prompts + ``[DevelopResult]`` (all read ``open_findings``) — the
    exact leak #278 closed for the adapter→raw-command transition. Called once at ledger
    load; retires the whole check **family** (bare + polyglot ``<name>.<eco>``). The
    ``test`` check carries no adapter findings, so its retire is a harmless no-op.
    Returns ``True`` when anything was retired (the caller re-persists the ledger).
    """
    off = {name for name, state in config.check_states.items() if state == "off"}
    if not config.test_gate:
        off.add("test")
    if not off:
        return False
    before = len(gate_ledger.open_findings())
    for name in off:
        gate_ledger.retire_check_family(name, 0)  # round 0 = a pre-round reconciliation
    return len(gate_ledger.open_findings()) != before


def _gate_ledger_path(config: DevelopConfig) -> Path:
    return config.gate_dir / "gate_ledger.json"


def load_gate_ledger(config: DevelopConfig) -> GateLedger:
    """The run's deterministic-finding ledger (#132) — reloaded from disk on a
    resume (a re-dispatched run reuses ``gate_dir``), else a fresh ledger."""
    path = _gate_ledger_path(config)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return GateLedger.from_jsonable(data)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning(
                "story-develop %s: gate ledger reload failed; starting fresh",
                config.run_id,
            )
    return GateLedger()


def persist_gate_ledger(config: DevelopConfig, ledger: GateLedger) -> None:
    """Write the gate ledger so closure survives across rounds + a resume.
    Best-effort: a write failure must not fail the run."""
    path = _gate_ledger_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(ledger.to_jsonable(), indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(
            "story-develop %s: gate ledger persist failed: %s", config.run_id, exc
        )


def run_delivery_test_gate(
    config: DevelopConfig, wt: Path, sha: str, round_no: int
) -> GateResult | None:
    """The *delivery* regression gate: run ONLY the ``test`` check on a fix commit.

    Delivery holds the push on ANY red ``test`` fix regardless of the profile's
    declared blocking config, so this reads the raw ``test`` :class:`GateResult` —
    NOT :func:`gate_floor_blocks` (which would honour an *informational* ``test``
    and push a RED fix) — and passes **no gate ledger**. #140: the profile set now
    also carries informational + candidate checks, but delivery keys only on
    ``test``; running the advisory / candidate checks here would burn containers
    without affecting the push decision (or wrongly hold it).

    This is the intentional delivery-vs-develop gate divergence — #140 put
    informational + candidate checks into the profile set, so delivery must key
    only on ``test`` or it would push a RED fix an informational ``test`` allowed.
    It is now a named policy function instead of an inline filter in
    :mod:`pr_delivery`, so a change to the develop-side gate can no longer silently
    skip (or accidentally rewire) the delivery gate. No dedicated ADR records this
    policy; the required/informational check-state model it rests on is ADR 0003 §4.

    #273 slice 2 interaction — the two ways to relax the ``test`` check differ here on
    purpose: ``check_states={"test": "informational"}`` keeps the check in the built
    set, so delivery STILL reads its raw verdict and holds a RED fix (the divergence
    above), whereas ``check_states={"test": "off"}`` (≡ the legacy ``test_gate=false``)
    DROPS the check entirely, so delivery has nothing to run and returns ``None`` — no
    red-fix hold. So ``informational`` weakens the *develop floor* but not delivery's
    regression safety; ``off`` opts out of both.

    Returns the ``test`` check's :class:`GateResult`, or ``None`` when no ``test``
    check is runnable (``test`` state ``off`` / ``develop_test_gate=false`` / no command
    / absent) or the tree export errored.
    """
    checks = tuple(c for c in build_check_set(config, wt) if c.name == "test")
    if not checks:
        return None
    cs = run_check_set(config, wt, sha, round_no, checks)  # NO gate ledger
    return cs.test_gate if cs is not None else None

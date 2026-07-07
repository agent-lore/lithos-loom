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
  ledger-less) — the intentional delivery-vs-develop divergence ADR 0004 named,
  now a named policy function instead of an inline filter in :mod:`pr_delivery`.

This module is engine-blind and imports no ``develop`` symbols (``develop``
imports *this*): the round pipeline drives these functions, review-only reuses
them, and delivery calls the one policy wrapper — one implementation, no drift.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ...runner import detection
from . import check_catalog, containers, gate_adapters, profiles, test_gate
from .check_set import (
    Check,
    CheckResult,
    CheckSetResult,
    CheckState,
    classify_execution,
)
from .config import DEFAULT_BLOCK_THRESHOLD, DevelopConfig
from .gate_findings import GateLedger
from .test_gate import GateResult

logger = logging.getLogger(__name__)


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
    # Group the resolved profile checks back by bare name (a polyglot check is emitted
    # once per ecosystem as ``<check>.<ecosystem>``), so they can be slotted into
    # profile order alongside the specially-built ``test`` check.
    by_name: dict[str, list[Check]] = {}
    for c in _build_profile_checks(config, profile, ecosystems, wt):
        by_name.setdefault(c.name.split(".")[0], []).append(c)
    checks: list[Check] = []
    for pc in profile.checks:
        if pc.name == "test":
            checks.extend(_build_test_check(config, pc.state, ecosystems, wt))
        else:
            checks.extend(by_name.get(pc.name, []))
    return tuple(checks)


def _build_test_check(
    config: DevelopConfig,
    state: CheckState,
    ecosystems: Sequence[detection.Ecosystem],
    wt: Path,
) -> list[Check]:
    """The ``test`` check, with ``state`` from the resolved profile's
    ``ProfileCheck("test", ...)`` (#127/#159, ADR §4/§10).

    ``develop_test_gate=false`` excludes it entirely (a test escape hatch, never a
    whole-gate kill switch — the rest of the profile set still runs). Its blocking is
    the profile's ``state`` — ``required`` (a RED run blocks + feeds the coder) vs
    ``informational`` (recorded, non-blocking) — the single source of truth, like every
    other check (the legacy ``block_on_red`` knob is removed; the floor governs).
    #133/ADR §4: when no command is runnable but the detected ecosystem expects tests, a
    *required* test check is an **expected-but-absent** blocking placeholder (empty
    command; the runner records it ``absent``), not a silent skip.
    """
    if not config.test_gate:
        return []
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
    desired = [
        check_catalog.DesiredCheck(pc.name, pc.state)
        for pc in profile.checks
        if pc.name not in ("test", "format")
        and check_catalog.applies(pc.name, ecosystems)
    ]
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
    stage_by_name = {pc.name: pc.stage for pc in profile.checks}
    out: list[Check] = []
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
    if gate_ledger is not None and tool in gate_adapters.SUPPORTED_TOOLS:
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


def run_check_set(
    config: DevelopConfig,
    wt: Path,
    sha: str,
    round_no: int,
    checks: tuple[Check, ...],
    gate_ledger: GateLedger | None = None,
) -> CheckSetResult | None:
    """Run an ordered check-set against one round commit.

    The committed tree is exported **once**; each check then runs in its own
    throwaway container (no shell-chaining — each keeps its own verdict).
    Infra errors skip rather than fail the run (the gate is an independent
    check, not a dependency): an export failure returns ``None`` for the whole
    set, and a per-check run failure yields a ``CheckResult`` with
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
        test_gate.export_tree(wt, sha, round_dir / "tree")
        cache = config.gate_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "story-develop %s: round %d gate export errored (skipping): %s",
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
        try:
            gate_cmd = test_gate.build_gate_command(
                name=name,
                image=config.image,
                tree=round_dir / "tree",
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
        _write_check_output(round_dir, check, gate)
        if gate is not None:
            # #132: structure a finding-producing check's output into the ledger,
            # then drop the full output so it never propagates into the result.
            # Resolve the real adapter tool past a `uv run` prefix or a pipeline
            # producer (#167: `uv export … | pip-audit …` → pip-audit), exactly like
            # the build (#166) and floor sites — a bare `split()[0]` would see `uv`
            # and skip the ledger, so dep-audit findings would never be structured.
            tool = gate_adapters.command_tool(check.command)
            if gate_ledger is not None and tool in gate_adapters.SUPPORTED_TOOLS:
                gate_ledger.apply_round(
                    check.name,
                    gate_adapters.parse_findings(check.name, tool, gate.full_output),
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

    This is the intentional delivery-vs-develop gate divergence ADR 0004 warned
    about — now a named policy function instead of an inline filter in
    :mod:`pr_delivery`, so a change to the develop-side gate can no longer silently
    skip (or accidentally rewire) the delivery gate.

    Returns the ``test`` check's :class:`GateResult`, or ``None`` when no ``test``
    check is runnable (``develop_test_gate=false`` / no command / absent) or the
    tree export errored.
    """
    checks = tuple(c for c in build_check_set(config, wt) if c.name == "test")
    if not checks:
        return None
    cs = run_check_set(config, wt, sha, round_no, checks)  # NO gate ledger
    return cs.test_gate if cs is not None else None

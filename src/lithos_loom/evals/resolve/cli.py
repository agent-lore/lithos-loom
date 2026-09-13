"""``lithos-loom eval resolve`` — measure the S5 conflict-resolution path on a
real conflicting merge, against an executable oracle.

On-demand, host-only, spends tokens (one full ``converge --resolve-conflicts``
run per sample: a coder round, the project's check-set, the panel, more
rounds if the panel blocks). NOT part of ``make check``; the shipped
fixtures are preflighted by ``tests/test_eval_resolve_shipped.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path

import typer

from ...plugins.story_develop import engines
from ...plugins.story_develop.config import (
    DEFAULT_MAX_ROUNDS,
    ReviewerSpec,
    parse_effort,
)
from ...plugins.story_develop.daemon_io import load_tool_default_models
from ...plugins.story_develop.model_policy import (
    apply_panel_default_models,
    require_agent_models,
)
from ..review.app import discover_cases, eval_app, require_rate
from ..review.cli import panel_phrase
from ..review.overrides import parse_reviewer_overrides, resolve_panel
from ..review.report import ci_band, err_suffix
from .case import ResolveCase, load_resolve_case
from .harness import (
    DEFAULT_BAR,
    DEFAULT_K,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_TIMEOUT,
    FixtureError,
    OracleError,
    ResolveCaseResult,
    ResolveSink,
    Trees,
    expected_fingerprint,
    live_resolve,
    materialise_trees,
    run_probe,
    run_resolve_case,
)

DEFAULT_RESOLVE_CASES_DIR = Path("evals/resolve/cases")
# Retained per-sample material lands beside the sample JSON under these names.
_RETAINED_FILES = ("merge.diff", "final.diff", "conversation.md")


def _positive(flag: str, value: int) -> None:
    if value < 1:
        raise typer.BadParameter(f"{flag} must be a positive integer (got {value})")


@eval_app.command("resolve")
def resolve(
    case: str | None = typer.Option(
        None, "--case", help="Run only this case id (default: all)."
    ),
    k: int = typer.Option(DEFAULT_K, "-k", "--samples", help="S5 runs per case."),
    bar: float = typer.Option(
        DEFAULT_BAR,
        "--bar",
        help="Pipeline-right rate (final tree satisfies every probe) a case must "
        "reach; an unsafe approval fails it regardless.",
    ),
    tool: str = typer.Option(
        "claude", "--tool", help="Agent that resolves the merge (claude | codex)."
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Explicit model for the coder (default: the loom config's "
        "\\[story_develop.default_models] entry for --tool; required either way).",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="Coder effort lever (low|medium|high|xhigh|max), if the engine has one.",
    ),
    max_rounds: int = typer.Option(
        DEFAULT_MAX_ROUNDS,
        "--max-rounds",
        help="Round budget per run (round 1 resolves; later rounds answer the panel).",
    ),
    coder_timeout: int = typer.Option(
        DEFAULT_TIMEOUT, "--coder-timeout", help="Seconds per coder turn."
    ),
    reviewer_timeout: int = typer.Option(
        DEFAULT_TIMEOUT, "--reviewer-timeout", help="Seconds per reviewer turn."
    ),
    probe_timeout: int = typer.Option(
        DEFAULT_PROBE_TIMEOUT, "--probe-timeout", help="Seconds per probe command."
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Override every case's profile: panel := its personas, "
        "check-set := its checks.",
    ),
    reviewer: list[str] | None = typer.Option(
        None,
        "--reviewer",
        help="Explicitly enumerate the panel (repeatable canonical persona "
        "names); wins over --profile's panel.",
    ),
    reviewer_override: list[str] | None = typer.Option(
        None,
        "--reviewer-override",
        help="PERSONA.FIELD=VALUE with FIELD in model|effort|tool "
        "(repeatable); applies where the persona is in the effective panel.",
    ),
    report_dir: Path | None = typer.Option(
        None,
        "--report-dir",
        help="Retain each sample's score, diffs and conversation log + a "
        "summary under this dir.",
    ),
    cases_dir: Path = typer.Option(
        DEFAULT_RESOLVE_CASES_DIR,
        "--cases-dir",
        help="Directory of resolve case folders.",
    ),
) -> None:
    """Measure S5 conflict resolution on real conflicting merges (PRD S8).

    Each sample is ONE ``converge --resolve-conflicts`` run (no push) on the
    case's merge: the coder resolves it, the check-set and the panel judge
    the composed tree. The case's probes — an executable oracle validated on
    its known-good / known-bad controls before anything is paid — then say
    whether the round-1 resolution (**coder-right**) and the final tree
    (**pipeline-right**) are correct, beside the panel's verdict. **UNSAFE**
    = approved AND wrong: the merge S5 would have pushed. One fails the
    case; otherwise the case passes at ``--bar`` on pipeline-right. A FAIL is
    the measurement; exit 1 only when a case has no valid sample, or when
    the fixture cannot be measured at all (an oracle that does not
    discriminate its controls, a merge the intake refuses).
    """
    require_rate("--bar", bar)
    _positive("--max-rounds", max_rounds)
    _positive("--coder-timeout", coder_timeout)
    _positive("--reviewer-timeout", reviewer_timeout)
    _positive("--probe-timeout", probe_timeout)
    case_dirs = discover_cases(cases_dir, case)

    # Fail closed before any paid run (#304): the coder's model must be
    # explicit, and so must every reviewer's — the panel decides whether a
    # wrong merge is pushed.
    if not engines.is_supported(tool):
        raise typer.BadParameter(
            f"--tool {tool!r} is not a supported agent tool "
            f"(known: {', '.join(sorted(engines.supported_tools()))})"
        )
    try:
        overrides = parse_reviewer_overrides(reviewer_override or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    default_models, frictions = load_tool_default_models()
    for friction in frictions:
        typer.echo(f"[Friction] {friction}", err=True)
    # A blank --model is "not given", never "the empty model".
    resolved_model = (model.strip() if model else "") or default_models.get(tool)
    try:
        effort = parse_effort(effort, where="eval resolve --effort")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    prepared: list[tuple[ResolveCase, str, tuple[ReviewerSpec, ...]]] = []
    for case_dir in case_dirs:
        loaded = load_resolve_case(case_dir)
        try:
            eff_profile, panel = resolve_panel(
                loaded, profile=profile, reviewers=reviewer, overrides=overrides
            )
            panel = apply_panel_default_models(panel, default_models)
            require_agent_models(
                panel=panel,
                coder=tool,
                coder_model=resolved_model,
                default_models=default_models,
                where=f"case {loaded.id}",
            )
        except ValueError as exc:
            raise typer.BadParameter(f"{exc} (or pass --model)") from exc
        prepared.append((loaded, eff_profile, panel))
    assert resolved_model is not None  # require_agent_models raised otherwise

    coder_info = {"tool": tool, "model": resolved_model, "effort": effort}
    results: list[ResolveCaseResult] = []
    for loaded, eff_profile, panel in prepared:
        typer.echo(
            f"resolving {loaded.id} × {k} … [{loaded.tree_label}; coder="
            f"{tool}/{resolved_model}{'/' + effort if effort else ''}; "
            f"profile={eff_profile}; panel={panel_phrase(panel)}; "
            f"max_rounds={max_rounds}]",
            err=True,
        )
        trees_seen: dict[str, Trees] = {}
        try:
            result = run_resolve_case(
                loaded,
                k=k,
                bar=bar,
                resolve_fn=partial(
                    live_resolve,
                    tool=tool,
                    model=resolved_model,
                    effort=effort,
                    reviewers=panel,
                    profile=eff_profile,
                    max_rounds=max_rounds,
                    coder_timeout=coder_timeout,
                    reviewer_timeout=reviewer_timeout,
                    default_models=dict(default_models),
                ),
                probe_runner=partial(run_probe, timeout=probe_timeout),
                sink=_make_sink(report_dir) if report_dir is not None else None,
                materialise=_recording(trees_seen),
            )
        except (OracleError, FixtureError) as exc:
            typer.secho(f"error: {exc}", err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        results.append(result)
        if report_dir is not None:
            _write_summary(
                report_dir,
                loaded,
                result,
                bar=bar,
                coder_info=coder_info,
                profile=eff_profile,
                panel=panel,
                max_rounds=max_rounds,
                trees=trees_seen.get("trees"),
            )

    print_resolve_table(results)
    no_valid = [r.case_id for r in results if r.n_valid == 0]
    if no_valid:
        typer.echo(
            "no valid samples (every run died before a verdict): "
            + ", ".join(no_valid),
            err=True,
        )
        for r in results:
            if r.n_valid == 0:
                for i, msg in enumerate(r.message_per_sample):
                    typer.echo(f"  {r.case_id} sample {i}: {msg}", err=True)
        raise typer.Exit(1)


def _recording(
    store: dict[str, Trees],
) -> Callable[[ResolveCase], tuple[Trees, Callable[[], None]]]:
    """The live tree builder, remembering the trees for the summary."""

    def materialise(case: ResolveCase) -> tuple[Trees, Callable[[], None]]:
        trees, cleanup = materialise_trees(case)
        store["trees"] = trees
        return trees, cleanup

    return materialise


def _make_sink(report_dir: Path) -> ResolveSink:
    def sink(case_id: str, i: int, payload: dict) -> None:
        out = report_dir / case_id
        out.mkdir(parents=True, exist_ok=True)
        retained = payload.pop("retained", {})
        (out / f"sample-{i}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        for name in _RETAINED_FILES:
            if name in retained:
                (out / f"sample-{i}.{name}").write_text(
                    retained[name], encoding="utf-8"
                )

    return sink


def _write_summary(
    report_dir: Path,
    case: ResolveCase,
    r: ResolveCaseResult,
    *,
    bar: float,
    coder_info: dict,
    profile: str,
    panel: tuple[ReviewerSpec, ...],
    max_rounds: int,
    trees: Trees | None,
) -> None:
    out = report_dir / case.id
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "case": case.id,
        "repo": case.repo,
        "tree": case.tree_label,
        "trees": trees.payload() if trees is not None else None,
        "expected_fingerprint": expected_fingerprint(case),
        "bar": bar,
        "coder": coder_info,
        "profile": profile,
        "panel": [
            {"name": s.name, "tool": s.tool, "model": s.model, "effort": s.effort}
            for s in panel
        ],
        "max_rounds": max_rounds,
        "n": r.n,
        "n_valid": r.n_valid,
        "resolved": r.resolved,
        "gate_green": r.gate_green,
        "approved": r.approved,
        "correct_first": r.correct_first,
        "correct_final": r.correct_final,
        "unsafe": r.unsafe,
        "wasted": r.wasted,
        "resolved_rate": r.resolved_rate,
        "correct_first_rate": r.correct_first_rate,
        "correct_final_rate": r.correct_final_rate,
        "correct_final_ci": list(r.correct_final_ci),
        "approved_rate": r.approved_rate,
        "unsafe_rate": r.unsafe_rate,
        "unsafe_ci": list(r.unsafe_ci),
        "passed": r.passed,
        "status_per_sample": list(r.status_per_sample),
        "message_per_sample": list(r.message_per_sample),
        "errored_per_sample": list(r.errored_per_sample),
        "resolved_per_sample": list(r.resolved_per_sample),
        "approved_per_sample": list(r.approved_per_sample),
        "correct_first_per_sample": list(r.correct_first_per_sample),
        "correct_final_per_sample": list(r.correct_final_per_sample),
        "unsafe_per_sample": list(r.unsafe_per_sample),
        "rounds_per_sample": list(r.rounds_per_sample),
        "cost_usd_per_sample": list(r.cost_usd_per_sample),
    }
    (out / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _cell(count: int, n: int) -> str:
    return f"{count}/{n}" if n else "—"


def print_resolve_table(results: Sequence[ResolveCaseResult]) -> None:
    header = (
        f"{'case':<26} {'n':>3} {'valid':>5} {'resolved':>8} {'coder-right':>11} "
        f"{'pipeline-right (95% CI)':>24} {'approved':>8} {'UNSAFE':>6} "
        f"{'cost':>8}  result"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in results:
        cost = sum(r.cost_usd_per_sample)
        mark = ("PASS" if r.passed else "FAIL") + err_suffix(sum(r.errored_per_sample))
        if r.wasted:
            mark += f" (wasted {r.wasted})"
        final = (
            f"{_cell(r.correct_final, r.n_valid)} {ci_band(*r.correct_final_ci)}"
            if r.n_valid
            else "—"
        )
        typer.echo(
            f"{r.case_id:<26} {r.n:>3} {r.n_valid:>5} "
            f"{_cell(r.resolved, r.n_valid):>8} "
            f"{_cell(r.correct_first, r.n_valid):>11} "
            f"{final:>24} {_cell(r.approved, r.n_valid):>8} "
            f"{_cell(r.unsafe, r.n_valid):>6} {'$' + format(cost, '.2f'):>8}  {mark}"
        )

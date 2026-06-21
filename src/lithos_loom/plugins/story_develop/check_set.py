"""The multi-check check-set abstraction for story-develop's deterministic gate (#131).

ADR 0003 §4 reframed the gate from a single test command into an ordered set of
named **checks**, each with a *state* (required / optional / informational /
not_applicable) and an *execution outcome* (did the tool run) that is kept
separate from whether its result *blocks* approval.

This module is the pure-data layer: the :class:`Check` spec, the per-check
:class:`CheckResult`, the aggregate :class:`CheckSetResult`, and the
``(exit_code, output) -> execution_outcome`` adapter :func:`classify_execution`.
The container mechanics live in :mod:`test_gate`; the orchestration (building the
default set, running it per round) lives in :mod:`develop`.

#131 ships exactly one check — ``test`` — so the default set is degenerate and
behaviour is identical to the old single-command gate. The structure is what the
follow-on slices extend: #132 turns ``CheckResult.gate`` into a finding ledger,
#133 adds per-ecosystem applicability, #136 renders the aggregate into prompts,
#139 lets a Review Profile select the set.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .test_gate import GateResult

if TYPE_CHECKING:
    from .gate_findings import GateFinding, GateLedger

# A check's role in the floor (ADR §4). #131 only emits "required"/"informational"
# (mapped from the legacy block_on_red flag, ADR §10); "optional"/"not_applicable"
# are reserved for #133/#139.
CheckState = Literal["required", "optional", "informational", "not_applicable"]

# "Did the tool run", kept separate from "did its result block" (ADR §4). A RED
# run is still ``ran``; ``errored`` is an infra failure (the tool never executed).
ExecutionOutcome = Literal["ran", "absent", "errored", "timed_out", "n_a"]

_NON_BLOCKING_STATES: frozenset[str] = frozenset(
    {"informational", "optional", "not_applicable"}
)
# Outcomes that produced no verdict but still must not block: an infra skip
# (``errored``) and a declared not-applicable (``n_a``). ``absent`` is NOT here —
# a *required* check that is expected-but-absent blocks (#133, ADR §4); for a
# non-required check, its state already short-circuits to non-blocking above.
_NON_VERDICT_OUTCOMES: frozenset[str] = frozenset({"errored", "n_a"})


@dataclass(frozen=True)
class Check:
    """The spec for one deterministic check — what to run and how it counts.

    Carries no result state. A Review Profile (#139) is a list of these; #131
    ships exactly one (the ``test`` check). ``state`` is the §4 axis that #133
    (applicability) and #139 (profiles) extend.
    """

    name: str
    command: str
    state: CheckState


@dataclass(frozen=True)
class CheckResult:
    """The outcome of running one :class:`Check` against a round commit.

    ``gate`` is the raw container outcome (the input #132's severity adapter will
    consume); it is ``None`` when the check never executed.
    """

    check: Check
    execution_outcome: ExecutionOutcome
    gate: GateResult | None

    @property
    def passed(self) -> bool:
        """Whether this check is satisfied *for approval* (i.e. does not block).

        Non-blocking states (informational / optional / not_applicable) always
        pass. An infra skip (``errored``) or declared not-applicable (``n_a``)
        never blocks. A *required* check that is **expected-but-absent**
        (``absent`` — its tool/target should be present but isn't) **blocks**
        (#133, ADR §4): it is distinct from an infra error, which skips. Otherwise
        a check passes iff it ran green.
        """
        if self.check.state in _NON_BLOCKING_STATES:
            return True
        if self.execution_outcome in _NON_VERDICT_OUTCOMES:
            return True
        return self.gate is not None and self.gate.passed


@dataclass(frozen=True)
class CheckSetResult:
    """The aggregate outcome of running an ordered check-set for one round."""

    results: tuple[CheckResult, ...]

    @property
    def test_result(self) -> CheckResult | None:
        """The ``test`` check's result, if the set contained one."""
        return next((r for r in self.results if r.check.name == "test"), None)

    @property
    def test_gate(self) -> GateResult | None:
        """The ``test`` check's raw :class:`GateResult` — the back-compat view that
        ``DevelopResult.test_gate`` / ``pr_delivery`` / ``_gate_note`` still read.
        ``None`` when the test check didn't run (or there was none)."""
        tr = self.test_result
        return tr.gate if tr is not None else None

    @property
    def blocking_passed(self) -> bool:
        """True when no check blocks approval (every result ``passed``)."""
        return all(r.passed for r in self.results)

    @property
    def aggregate_verdict(self) -> str | None:
        """The worst verdict across checks that produced one, or ``None`` when none
        ran. Feeds the run summary / PR body. For the ``{test}`` set this is exactly
        the test check's verdict."""
        gates = [r.gate for r in self.results if r.gate is not None]
        if not gates:
            return None
        if any(g.timed_out for g in gates):
            return "TIMEOUT"
        return "RED" if any(not g.passed for g in gates) else "GREEN"


def classify_execution(gate: GateResult | None) -> ExecutionOutcome:
    """Map a raw container outcome onto the ``execution_outcome`` axis.

    ``None`` (the infra-error path) -> ``errored``; a timed-out run -> ``timed_out``;
    everything else (GREEN *or* RED) -> ``ran`` (a RED run still executed).
    """
    if gate is None:
        return "errored"
    if gate.timed_out:
        return "timed_out"
    return "ran"


_GATE_FINDINGS_CAP = 25


def _open_gate_findings(
    check_name: str, gate_ledger: GateLedger | None
) -> list[GateFinding]:
    """This check's currently-open deterministic findings (empty when no ledger)."""
    if gate_ledger is None:
        return []
    return [f for f in gate_ledger.open_findings() if f.check == check_name]


def _gate_finding_lines(findings: list[GateFinding]) -> list[str]:
    """One bullet per finding (id, severity, rule, locus, message), capped."""
    lines: list[str] = []
    for f in findings[:_GATE_FINDINGS_CAP]:
        if f.file:
            locus = f" [{f.file}:{f.line}]" if f.line is not None else f" [{f.file}]"
        elif f.package:
            locus = f" [{f.package}]"
        else:
            locus = ""
        line = f"- {f.finding_id} ({f.severity}): {f.rule}{locus} {f.message}"
        lines.append(line.rstrip())
    extra = len(findings) - _GATE_FINDINGS_CAP
    if extra > 0:
        lines.append(f"- (+{extra} more)")
    return lines


def render_check_summary(
    check_set: CheckSetResult | None,
    *,
    for_coder: bool,
    gate_ledger: GateLedger | None = None,
) -> str:
    """Render the round's check-set for prompt injection (ADR §6).

    ``for_coder=True`` grows a section per **failing** check (RED / TIMEOUT) with
    the authoritative "fix it" framing, plus a section per check that has open
    **deterministic findings** (#132) — and the empty string when there is
    nothing to surface. For the single-``test`` set with no ledger this reproduces
    the old note (heading + output tail) byte-for-byte.

    ``for_coder=False`` (reviewers) lists **every** check's verdict, then for each
    check renders its structured deterministic findings (#132) when present, else
    its raw output tail on failure. A missing / empty gate is stated explicitly.
    ``gate_ledger=None`` disables the structured enrichment (identical old output).
    """
    if for_coder:
        parts: list[str] = []
        results = check_set.results if check_set is not None else ()
        for r in results:
            findings = _open_gate_findings(r.check.name, gate_ledger)
            if findings:
                body = "\n".join(_gate_finding_lines(findings))
                parts.append(
                    f"\n## Independent {r.check.name} gate findings\n\n"
                    f"The orchestrator ran `{r.check.name}` against your last "
                    "commit in a clean container and recorded these deterministic "
                    f"findings (authoritative — address them):\n\n{body}\n"
                )
                continue
            g = r.gate
            if g is None:
                # A required check that is expected-but-absent (#133) blocks but
                # ran no container, so it has no output tail — surface it so the
                # coder knows what to add. (errored / n_a don't block, so skip.)
                if r.execution_outcome == "absent" and not r.passed:
                    parts.append(
                        f"\n## Independent {r.check.name} gate "
                        "(EXPECTED BUT ABSENT)\n\n"
                        f"No runnable `{r.check.name}` command was found for this "
                        f"repo's ecosystem, yet the `{r.check.name}` check is "
                        "required — this blocks approval. Add the missing "
                        "tests/tool so the check can run.\n"
                    )
                continue
            if g.passed:
                continue
            how = "timed out" if g.timed_out else f"exit {g.exit_code}"
            parts.append(
                f"\n## Independent {r.check.name} gate (FAILED)\n\n"
                f"The orchestrator independently ran `{g.command}` against your "
                f"last commit in a clean container and it failed ({how}). This "
                "result is authoritative — fix the failures regardless of how it "
                "behaved in your own environment. Output tail:\n\n"
                "```\n" + g.output_tail + "\n```\n"
            )
        return "".join(parts)

    if check_set is None or not check_set.results:
        return "_(no deterministic gate ran this round)_"
    lines = [
        "## Deterministic gate (this commit)",
        "",
        # Explicit ``+`` (not implicit adjacency) so this reads unambiguously as
        # one wrapped sentence in the list literal — no silent missing-comma risk.
        "The orchestrator ran these checks on the exact commit you are reviewing. "
        + "Use them to focus on what tools cannot catch:",
        "",
    ]
    for r in check_set.results:
        verdict = r.gate.verdict if r.gate is not None else r.execution_outcome.upper()
        lines.append(f"- `{r.check.name}` ({r.check.state}): **{verdict}**")
    agg = check_set.aggregate_verdict
    if agg is not None:
        lines += ["", f"Overall: **{agg}**"]
    for r in check_set.results:
        findings = _open_gate_findings(r.check.name, gate_ledger)
        if findings:
            lines += ["", f"`{r.check.name}` deterministic findings:"]
            lines += _gate_finding_lines(findings)
            continue
        g = r.gate
        if g is None or g.passed:
            continue
        lines += ["", f"`{r.check.name}` output tail:", "```", g.output_tail, "```"]
    return "\n".join(lines)

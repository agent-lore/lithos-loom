"""First-class deterministic-finding ledger for gate checks (#132, ADR 0003 §5).

Static-tool (gate check) output becomes **deterministic findings** with their own
lifecycle, **parallel to — not folded into** — the reviewer
:class:`~.findings.FindingLedger`:

- **Stable ids**, namespaced per check: ``gate/<check>-NNN`` (a monotonic counter
  per check).
- **Owner = the gate.** There is no API for the coder to close a ``gate/*``
  finding by assertion; closure is :meth:`GateLedger.apply_round` re-running the
  check and the finding no longer appearing.
- **Severity is already mapped** to loom's ``minor|major|critical`` by the
  per-tool adapter (:mod:`.gate_adapters`); whether a finding blocks is the same
  ``severity_at_or_above`` + threshold call the reviewer ledger uses.
- **Closure only by re-running green** — a finding whose *fingerprint* is absent
  from a later round of the **same** check is marked ``fixed``; a fingerprint that
  reappears keeps its original id and re-opens.
- **Suppression, not dispute** — a finding the tool reports as suppressed (e.g. a
  ``# noqa`` / baseline) is recorded ``suppressed`` (non-blocking); the
  suppression itself is a code diff the reviewer panel sees.

This module is the pure data layer (model + ledger + persistence); the per-tool
parsers live in :mod:`.gate_adapters`, and wiring it into the round loop +
prompts is #132's integration slice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from .handoff import severity_at_or_above

# A finding's lifecycle status. ``open`` is the only blocking state; ``fixed``
# (gone on re-run) and ``suppressed`` (tool-ignored) never block.
_OPEN = "open"
_FIXED = "fixed"
_SUPPRESSED = "suppressed"

# Cross-round identity of a gate finding: the check it came from, the tool's
# native rule code, and the location. Deliberately id- and severity-independent
# so the same violation keeps its id across rounds. (Line is part of identity for
# the MVP; a snippet-hash that survives unrelated edits above it is a follow-up.)
Fingerprint = tuple[str, str, str, "int | None"]


@dataclass(frozen=True)
class GateFinding:
    """One deterministic finding from a gate check (already severity-mapped)."""

    check: str
    tool: str
    rule: str
    severity: str  # minor | major | critical (mapped by the adapter)
    message: str
    file: str = ""
    line: int | None = None
    finding_id: str = ""  # gate/<check>-NNN, assigned by the ledger
    status: str = _OPEN  # open | fixed | suppressed

    @property
    def fingerprint(self) -> Fingerprint:
        return (self.check, self.rule, self.file, self.line)

    @property
    def is_open(self) -> bool:
        return self.status == _OPEN

    def blocks(self, threshold: str) -> bool:
        """Open and at/above the threshold — same rule as the reviewer ledger."""
        return self.is_open and severity_at_or_above(self.severity, threshold)


@dataclass
class _Entry:
    """One finding's life across rounds (mutable; owned by the ledger)."""

    finding: GateFinding
    first_round: int
    last_seen_round: int


class GateLedger:
    """Gate-owned registry of deterministic findings with stable per-check ids.

    Unlike the reviewer ledger there is **no coder-facing mutation**: the coder
    cannot close a finding. Identity is the finding's :attr:`GateFinding.fingerprint`
    (not a tool-assigned id), so a violation that re-appears keeps its id and a
    violation that vanishes is closed.
    """

    def __init__(self) -> None:
        self._entries: dict[Fingerprint, _Entry] = {}
        self._counters: dict[str, int] = {}  # next id per check

    def apply_round(
        self, check: str, findings: Sequence[GateFinding], round_no: int
    ) -> None:
        """Fold one round's parsed findings for *check* into the ledger.

        New fingerprints get the next ``gate/<check>-NNN`` id. A fingerprint seen
        again keeps its id and is (re-)opened (or ``suppressed`` if the tool now
        reports it suppressed). A fingerprint of **this check** that is absent
        from *findings* is closed ``fixed`` — closure is scoped to the check that
        ran, so a check that did not run this round closes nothing.
        """
        seen: set[Fingerprint] = set()
        for f in findings:
            fp = f.fingerprint
            seen.add(fp)
            status = _SUPPRESSED if f.status == _SUPPRESSED else _OPEN
            existing = self._entries.get(fp)
            if existing is not None:
                existing.finding = replace(
                    f, finding_id=existing.finding.finding_id, status=status
                )
                existing.last_seen_round = round_no
            else:
                self._counters[check] = self._counters.get(check, 0) + 1
                fid = f"gate/{check}-{self._counters[check]:03d}"
                self._entries[fp] = _Entry(
                    finding=replace(f, finding_id=fid, status=status),
                    first_round=round_no,
                    last_seen_round=round_no,
                )
        for fp, entry in self._entries.items():
            if (
                entry.finding.check == check
                and fp not in seen
                and entry.finding.status != _FIXED
            ):
                entry.finding = replace(entry.finding, status=_FIXED)

    # --- queries ------------------------------------------------------------

    def all_findings(self) -> list[GateFinding]:
        return [e.finding for e in self._entries.values()]

    def open_findings(self) -> list[GateFinding]:
        return [e.finding for e in self._entries.values() if e.finding.is_open]

    def blocking(self, threshold: str) -> list[GateFinding]:
        return [
            e.finding for e in self._entries.values() if e.finding.blocks(threshold)
        ]

    def blocking_passed(self, threshold: str) -> bool:
        """True when no deterministic finding blocks at *threshold*."""
        return not self.blocking(threshold)

    # --- persistence (cross-round + resume) ---------------------------------

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "counters": dict(self._counters),
            "entries": [
                {
                    "finding": asdict(e.finding),
                    "first_round": e.first_round,
                    "last_seen_round": e.last_seen_round,
                }
                for e in self._entries.values()
            ],
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> GateLedger:
        ledger = cls()
        counters = data.get("counters", {})
        ledger._counters = {str(k): int(v) for k, v in counters.items()}
        for entry in data.get("entries", []):
            finding = GateFinding(**entry["finding"])
            ledger._entries[finding.fingerprint] = _Entry(
                finding=finding,
                first_round=int(entry["first_round"]),
                last_seen_round=int(entry["last_seen_round"]),
            )
        return ledger

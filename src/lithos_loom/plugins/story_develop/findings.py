"""Plugin-enforced finding lifecycle (T7, PRD decision #7).

The orchestrator — not the reviewer — owns finding identity. Each reviewer
has a :class:`FindingLedger` that assigns **monotonic ids** (``f-001`` …) to
new findings and tracks every finding's status across rounds. Reviewers must
account for each previously-open finding by id (update its status or keep it
open); a handoff that invents an unknown id or silently drops an open one is
rejected and the reviewer is re-prompted — that validation is what makes the
stall and dispute guards trustworthy, because they key off finding identity.

The reviewer's verdict statuses stay canonical for blocking; the coder's
handoff may mark a finding ``disputed`` (with ``coder_response``), which the
ledger records separately — a coder-disputed finding the reviewer keeps
blocking feeds the dispute guard in :mod:`develop`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .handoff import Finding, ReviewHandoff, severity_at_or_above

# Open (= potentially blocking) states; mirrors handoff._OPEN_STATES.
_OPEN_STATES = frozenset({"open", "disputed", "needs-clarification"})


@dataclass
class LedgerEntry:
    """One finding's life across rounds (mutable; owned by the ledger)."""

    finding_id: str
    reviewer: str
    severity: str
    status: str  # reviewer-owned; canonical for blocking
    files: list[str] = field(default_factory=list)
    rationale: str = ""
    coder_response: str = ""
    first_round: int = 0
    last_updated_round: int = 0
    coder_disputed: bool = False  # the coder pushed back (its handoff)
    # consecutive rounds the reviewer kept this blocking AFTER the coder
    # disputed it; >= 2 triggers the dispute guard.
    blocked_while_disputed: int = 0

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATES

    def blocks(self, threshold: str) -> bool:
        return self.is_open and severity_at_or_above(self.severity, threshold)


class FindingLedger:
    """Per-reviewer finding registry with plugin-assigned monotonic ids."""

    def __init__(self, reviewer: str) -> None:
        self.reviewer = reviewer
        self.entries: dict[str, LedgerEntry] = {}
        self._next = 1

    # --- validation (pure — safe to call before committing anything) -------

    def check(self, parsed: ReviewHandoff) -> str | None:
        """Validate a parsed review against the ledger; None when acceptable.

        The error message is suitable as a correction re-prompt. LGTM is
        always acceptable (it closes everything). A FINDINGS handoff must not
        reference unknown ids and must account for every currently-open id.
        """
        if parsed.is_lgtm:
            return None
        known = set(self.entries)
        referenced: set[str] = set()
        for f in parsed.findings:
            if f.finding_id:
                if f.finding_id not in known:
                    return (
                        f"finding id {f.finding_id!r} does not exist — reference "
                        "only ids you were given, and leave finding_id blank for "
                        "genuinely new findings (the orchestrator assigns ids)"
                    )
                if f.finding_id in referenced:
                    return f"finding id {f.finding_id!r} appears more than once"
                referenced.add(f.finding_id)
        open_ids = {fid for fid, e in self.entries.items() if e.is_open}
        dropped = sorted(open_ids - referenced)
        if dropped:
            return (
                f"these open finding ids were not accounted for: "
                f"{', '.join(dropped)} — every open finding must appear with an "
                "updated status (fixed / accepted / open / superseded / merged)"
            )
        return None

    # --- mutations ----------------------------------------------------------

    def apply_review(self, parsed: ReviewHandoff, round_no: int) -> list[Finding]:
        """Commit a (checked) review into the ledger; returns canonical findings.

        New findings get the next monotonic id. LGTM closes every open entry
        (status ``accepted`` — the reviewer is satisfied). The returned list
        carries ledger-canonical ids for downstream rendering.
        """
        if parsed.is_lgtm:
            for entry in self.entries.values():
                if entry.is_open:
                    entry.status = "accepted"
                    entry.last_updated_round = round_no
            return []
        canonical: list[Finding] = []
        for f in parsed.findings:
            if f.finding_id and f.finding_id in self.entries:
                entry = self.entries[f.finding_id]
                entry.severity = f.severity
                entry.status = f.status
                if f.files:
                    entry.files = f.files
                if f.rationale:
                    entry.rationale = f.rationale
                entry.last_updated_round = round_no
            else:
                fid = f"f-{self._next:03d}"
                self._next += 1
                entry = LedgerEntry(
                    finding_id=fid,
                    reviewer=self.reviewer,
                    severity=f.severity,
                    status=f.status,
                    files=f.files,
                    rationale=f.rationale,
                    first_round=round_no,
                    last_updated_round=round_no,
                )
                self.entries[fid] = entry
            canonical.append(
                Finding(
                    finding_id=entry.finding_id,
                    severity=entry.severity,
                    status=entry.status,
                    files=entry.files,
                    rationale=entry.rationale,
                    coder_response=entry.coder_response,
                )
            )
        # Track dispute persistence: a coder-disputed entry the reviewer just
        # kept open counts another blocked round; resolving it clears it.
        for entry in self.entries.values():
            if entry.coder_disputed and entry.is_open:
                entry.blocked_while_disputed += 1
            elif not entry.is_open:
                entry.blocked_while_disputed = 0
        return canonical

    def record_coder_updates(self, findings: list[Finding], round_no: int) -> None:
        """Record the coder's handoff findings (dispute marks + responses).

        The coder cannot change reviewer-owned statuses; only its dispute flag
        and ``coder_response`` are recorded. Unknown ids are ignored (the
        coder mis-typing an id must not crash the run).
        """
        for f in findings:
            entry = self.entries.get(f.finding_id)
            if entry is None:
                continue
            if f.coder_response:
                entry.coder_response = f.coder_response
            if f.status == "disputed":
                if not entry.coder_disputed:
                    entry.coder_disputed = True
                    entry.blocked_while_disputed = 0
                entry.last_updated_round = round_no

    # --- queries ------------------------------------------------------------

    def open_entries(self) -> list[LedgerEntry]:
        return [e for e in self.entries.values() if e.is_open]

    def blocking_signature(self, threshold: str) -> frozenset[tuple[str, str]]:
        """The stall-guard key: open-and-blocking ids with their statuses."""
        return frozenset(
            (e.finding_id, e.status)
            for e in self.entries.values()
            if e.blocks(threshold)
        )

    def disputed_deadlocks(self, threshold: str, *, rounds: int = 2) -> list[str]:
        """Ids the reviewer kept blocking >= *rounds* rounds after a dispute."""
        return sorted(
            e.finding_id
            for e in self.entries.values()
            if e.blocks(threshold) and e.blocked_while_disputed >= rounds
        )

    def render_open(self) -> str:
        """The open findings as a prompt block (ids the reviewer must address)."""
        entries = self.open_entries()
        if not entries:
            return "(none)"
        lines: list[str] = []
        for e in sorted(entries, key=lambda x: x.finding_id):
            lines.append(
                f"- finding_id: {e.finding_id} (severity {e.severity}, "
                f"status {e.status})"
            )
            if e.rationale:
                lines.append(f"  your rationale: {e.rationale}")
            if e.coder_response:
                lines.append(f"  coder response: {e.coder_response}")
        return "\n".join(lines)

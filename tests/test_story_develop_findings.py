"""Unit tests for the plugin-enforced finding lifecycle (T7)."""

from __future__ import annotations

from lithos_loom.plugins.story_develop.findings import FindingLedger
from lithos_loom.plugins.story_develop.handoff import Finding, ReviewHandoff


def _f(fid: str = "", severity: str = "major", status: str = "open", **kw) -> Finding:
    return Finding(finding_id=fid, severity=severity, status=status, **kw)


def _review(*findings: Finding, lgtm: bool = False) -> ReviewHandoff:
    return ReviewHandoff(
        status="LGTM" if lgtm else "FINDINGS", summary="", findings=list(findings)
    )


def test_new_findings_get_monotonic_ids() -> None:
    ledger = FindingLedger("cq")
    out = ledger.apply_review(_review(_f(), _f(severity="minor")), 1)
    assert [f.finding_id for f in out] == ["f-001", "f-002"]
    out2 = ledger.apply_review(
        _review(_f("f-001", status="fixed"), _f("f-002"), _f()), 2
    )
    assert [f.finding_id for f in out2] == ["f-001", "f-002", "f-003"]


def test_check_rejects_unknown_id() -> None:
    ledger = FindingLedger("cq")
    err = ledger.check(_review(_f("f-042")))
    assert err is not None and "f-042" in err and "does not exist" in err


def test_check_rejects_duplicate_id() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    err = ledger.check(_review(_f("f-001"), _f("f-001")))
    assert err is not None and "more than once" in err


def test_check_rejects_dropped_open_id() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f(), _f()), 1)  # f-001, f-002 open
    err = ledger.check(_review(_f("f-001", status="fixed"), _f()))  # f-002 dropped
    assert err is not None and "f-002" in err and "not accounted for" in err


def test_check_accepts_full_accounting_and_lgtm() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    assert ledger.check(_review(_f("f-001", status="fixed"))) is None
    assert ledger.check(_review(lgtm=True)) is None


def test_resolved_findings_need_no_accounting() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    ledger.apply_review(_review(_f("f-001", status="fixed")), 2)
    # round 3 raises a new finding without mentioning the fixed f-001 — fine.
    assert ledger.check(_review(_f())) is None


def test_lgtm_closes_all_open() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f(), _f()), 1)
    out = ledger.apply_review(_review(lgtm=True), 2)
    assert out == []
    assert ledger.open_entries() == []
    assert all(e.status == "accepted" for e in ledger.entries.values())


def test_blocking_signature_respects_threshold() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f(severity="minor"), _f(severity="major")), 1)
    assert ledger.blocking_signature("major") == frozenset({("f-002", "open")})
    assert ledger.blocking_signature("minor") == frozenset(
        {("f-001", "open"), ("f-002", "open")}
    )


def test_coder_dispute_then_reviewer_keeps_blocking() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    # coder disputes after round 1's review
    ledger.record_coder_updates(
        [_f("f-001", status="disputed", coder_response="intentional")], 2
    )
    assert ledger.disputed_deadlocks("major") == []
    # round 2: reviewer keeps it open -> blocked-while-disputed = 1
    ledger.apply_review(_review(_f("f-001")), 2)
    assert ledger.disputed_deadlocks("major") == []
    # round 3: reviewer blocks again -> 2 -> deadlock
    ledger.apply_review(_review(_f("f-001")), 3)
    assert ledger.disputed_deadlocks("major") == ["f-001"]
    assert ledger.entries["f-001"].coder_response == "intentional"


def test_dispute_clears_when_reviewer_accepts() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    ledger.record_coder_updates([_f("f-001", status="disputed")], 2)
    ledger.apply_review(_review(_f("f-001", status="accepted")), 2)
    assert ledger.disputed_deadlocks("major") == []
    assert ledger.entries["f-001"].blocked_while_disputed == 0


def test_coder_cannot_change_reviewer_status() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    ledger.record_coder_updates([_f("f-001", status="fixed")], 2)
    assert ledger.entries["f-001"].status == "open"  # reviewer-owned


def test_coder_unknown_id_ignored() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f()), 1)
    ledger.record_coder_updates([_f("f-099", status="disputed")], 2)
    assert ledger.disputed_deadlocks("major") == []


def test_render_open_lists_ids_and_context() -> None:
    ledger = FindingLedger("cq")
    ledger.apply_review(_review(_f(rationale="why it matters")), 1)
    ledger.record_coder_updates(
        [_f("f-001", status="disputed", coder_response="nope")], 2
    )
    text = ledger.render_open()
    assert "finding_id: f-001" in text
    assert "why it matters" in text
    assert "coder response: nope" in text
    assert FindingLedger("x").render_open() == "(none)"


# ── out-of-scope disposition (819370e5) ────────────────────────────────


def test_out_of_scope_resolves_in_the_ledger() -> None:
    # Reviewer defers an open finding: it leaves the blocking signature (so
    # the stall guard sees progress) and open_entries, like any resolved state.
    ledger = FindingLedger("correctness")
    ledger.apply_review(_review(_f(severity="major")), round_no=1)
    assert ledger.blocking_signature("major")

    deferred = ReviewHandoff(
        status="FINDINGS",
        summary="s",
        findings=[
            Finding(
                finding_id="f-001",
                severity="major",
                status="out-of-scope",
                deferral_reason="pre-existing on the base",
            )
        ],
    )
    assert ledger.check(deferred) is None  # accounts for the open id
    ledger.apply_review(deferred, round_no=2)
    assert ledger.blocking_signature("major") == frozenset()
    assert ledger.open_entries() == []
    assert ledger.entries["f-001"].status == "out-of-scope"


def test_coder_cannot_defer_a_finding_out_of_scope() -> None:
    # The disposition is reviewer-owned (819370e5): the coder's handoff cannot
    # move a reviewer-owned status, so a coder claiming "out of scope" changes
    # nothing about blocking — its route is the dispute flag, as before.
    ledger = FindingLedger("correctness")
    ledger.apply_review(_review(_f(severity="major")), round_no=1)

    coder_says = [
        Finding(
            finding_id="f-001",
            severity="major",
            status="out-of-scope",
            rationale="coder thinks it is not its problem",
        )
    ]
    ledger.record_coder_updates(coder_says, round_no=2)
    assert ledger.entries["f-001"].status == "open"  # unchanged
    assert ledger.blocking_signature("major")  # still blocks


def test_collect_deferred_survives_a_later_lgtm_round() -> None:
    # The reason collection reads the LEDGERS: a finding deferred in round 2
    # produces no trace in round 3's LGTM outcome, and the summary's
    # open-findings section filters on is_open — either view would lose it.
    from lithos_loom.plugins.story_develop.findings import collect_deferred

    ledger = FindingLedger("correctness")
    ledger.apply_review(_review(_f(severity="major")), round_no=1)
    ledger.apply_review(
        _review(
            _f(
                fid="f-001",
                severity="major",
                status="out-of-scope",
                deferral_reason="harness fault",
            )
        ),
        round_no=2,
    )
    ledger.apply_review(_review(lgtm=True), round_no=3)

    deferred = collect_deferred([ledger])
    assert len(deferred) == 1
    assert deferred[0].finding_id == "f-001"
    assert deferred[0].deferral_reason == "harness fault"
    assert deferred[0].reviewer == "correctness"


def test_deferral_preserves_the_defect_description() -> None:
    # PR #342 review P1: the (mandatory) disposition text must not overwrite
    # what the defect IS — the spawned task needs both texts. The why arrives
    # in its own `deferral_reason` key (the parse mandates it), so a deferral
    # that names no new rationale leaves the round-1 defect text untouched.
    from lithos_loom.plugins.story_develop.findings import collect_deferred

    ledger = FindingLedger("correctness")
    ledger.apply_review(
        _review(_f(severity="major", rationale="Button text overlaps the icon")),
        round_no=1,
    )
    ledger.apply_review(
        _review(
            _f(
                fid="f-001",
                severity="major",
                status="out-of-scope",
                deferral_reason="pre-existing on the base",
            )
        ),
        round_no=2,
    )

    d = collect_deferred([ledger])[0]
    assert d.rationale == "Button text overlaps the icon"
    assert d.deferral_reason == "pre-existing on the base"


def test_direct_out_of_scope_filing_preserves_both_texts() -> None:
    # PR #342 re-review P1: a NEW finding filed directly as out-of-scope (the
    # common shape — a pre-existing defect first noticed mid-review) carries
    # the defect in `rationale` and the why in `deferral_reason`; the ledger
    # must keep them separate all the way to the spawned task.
    from lithos_loom.plugins.story_develop.findings import collect_deferred

    ledger = FindingLedger("correctness")
    ledger.apply_review(
        _review(
            _f(
                severity="major",
                status="out-of-scope",
                rationale="Button text overlaps the icon",
                deferral_reason="pre-existing on the base",
            )
        ),
        round_no=1,
    )

    assert ledger.blocking_signature("major") == frozenset()
    d = collect_deferred([ledger])[0]
    assert d.rationale == "Button text overlaps the icon"
    assert d.deferral_reason == "pre-existing on the base"

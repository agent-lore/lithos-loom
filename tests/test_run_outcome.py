"""Unit tests for the develop-run on-disk contract (``story_develop.run_outcome``).

Pure classify / read / capture over a manually-built run dir — no docker, no CLI.
Moved from ``test_cli_develop.py`` when the contract was extracted out of
``cli/develop.py`` (ARCH-3.R1); ``run_phase`` now takes a ``containers_running``
bool signal (decoupled from the CLI's ``ContainerStatus`` docker type) and
``delivery_timed_out`` takes elapsed ``delivering_seconds`` instead of a poll
count.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import run_outcome


def test_run_phase_classification(tmp_path: Path) -> None:
    rd = tmp_path / "t-1" / "run"
    rd.mkdir(parents=True)
    none: dict | None = None  # no recorded outcome yet
    approved = {"status": "approved"}
    failed = {"status": "failed"}
    # docker present: live while a container runs
    assert (
        run_outcome.run_phase(rd, none, containers_running=True, seen_container=True)
        == "running"
    )
    # startup window: zero containers and none seen yet → keep following (the old
    # container-liveness check exited here, the bug run_phase fixes)
    assert (
        run_outcome.run_phase(rd, none, containers_running=False, seen_container=False)
        == "running"
    )
    # a seen container gone with no recorded outcome yet → ambiguous (teardown
    # window vs crash); the caller grace-polls before deciding
    assert (
        run_outcome.run_phase(rd, none, containers_running=False, seen_container=True)
        == "vanished"
    )
    # docker absent (None): live until the outcome lands (or the dir is reaped)
    assert (
        run_outcome.run_phase(rd, none, containers_running=None, seen_container=False)
        == "running"
    )
    # a NON-approved terminal status has no post-dialogue work (deliver() runs only
    # for approved) → terminal the moment its state.json lands.
    assert (
        run_outcome.run_phase(rd, failed, containers_running=False, seen_container=True)
        == "terminal"
    )
    # an APPROVED verdict is NOT terminal while PR delivery is still pending — in
    # daemon mode deliver() runs AFTER develop() writes state.json, so keying
    # terminal on the bare verdict re-opens the #171 false-done window.
    for cr, seen in ((False, True), (True, True), (None, False)):
        assert (
            run_outcome.run_phase(
                rd, approved, containers_running=cr, seen_container=seen
            )
            == "delivering"
        )
    # this run's result.json (succeeded) lands in the shared per-task dir → done
    (rd.parent / "result.json").write_text(
        json.dumps({"status": "succeeded", "run_id": "run"})
    )
    assert (
        run_outcome.run_phase(
            rd, approved, containers_running=None, seen_container=False
        )
        == "terminal"
    )
    # docker absent + the run dir reaped on success → terminal (nothing to read)
    shutil.rmtree(rd)
    assert (
        run_outcome.run_phase(rd, none, containers_running=None, seen_container=False)
        == "terminal"
    )


def test_run_phase_approved_ignores_stale_result_from_a_prior_run(
    tmp_path: Path,
) -> None:
    # The shared per-task result.json can be a stale leftover from a PRIOR run.
    # #198 binds on run_id: a prior run's result (succeeded OR failed) is not THIS
    # run's delivery.
    rd = tmp_path / "t-1" / "r2"
    rd.mkdir(parents=True)
    (rd / "state.json").write_text(json.dumps({"status": "approved"}))
    approved = {"status": "approved"}
    # a PRIOR run's succeeded result.json (run_id r1) must not read as r2's delivery
    (rd.parent / "result.json").write_text(
        json.dumps({"status": "succeeded", "run_id": "r1"})
    )
    assert (
        run_outcome.run_phase(
            rd, approved, containers_running=None, seen_container=False
        )
        == "delivering"
    )
    # this run's own succeeded result.json (run_id r2) → terminal
    (rd.parent / "result.json").write_text(
        json.dumps({"status": "succeeded", "run_id": "r2"})
    )
    assert (
        run_outcome.run_phase(
            rd, approved, containers_running=None, seen_container=False
        )
        == "terminal"
    )


def test_run_phase_approved_failed_result_is_terminal_without_marker(
    tmp_path: Path,
) -> None:
    # #198 (Hole 2): when deliver() raises, the daemon writes a failed result.json
    # but the private delivery.json marker write is best-effort. If the marker is
    # missing, THIS run's failed result.json (run_id-bound, category delivery) is
    # still the terminal signal.
    rd = tmp_path / "t-1" / "r2"
    rd.mkdir(parents=True)
    (rd / "state.json").write_text(json.dumps({"status": "approved"}))
    (rd.parent / "result.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "run_id": "r2",
                "error": {
                    "category": "delivery",
                    "message": "PR delivery failed: boom",
                },
            }
        )
    )
    approved = {"status": "approved"}
    assert (
        run_outcome.run_phase(
            rd, approved, containers_running=None, seen_container=False
        )
        == "terminal"
    )
    assert run_outcome.delivery_failed(rd) == "PR delivery failed: boom"


def test_run_phase_approved_delivery_failed_is_terminal(tmp_path: Path) -> None:
    # #194: an approved run whose delivery FAILED (its private delivery.json marks
    # failed) is terminal at once — not stuck in "delivering" until the #189
    # deadline.
    rd = tmp_path / "t-1" / "r1"
    rd.mkdir(parents=True)
    approved = {"status": "approved"}
    assert (
        run_outcome.run_phase(
            rd, approved, containers_running=None, seen_container=False
        )
        == "delivering"
    )
    (rd / "delivery.json").write_text(json.dumps({"failed": True, "reason": "boom"}))
    assert (
        run_outcome.run_phase(
            rd, approved, containers_running=None, seen_container=False
        )
        == "terminal"
    )


def test_delivery_failed_helper(tmp_path: Path) -> None:
    # #194: the daemon records a delivery failure in the run's PRIVATE
    # delivery.json (not the shared result.json — so a prior run's leftover can't
    # be mistaken for this one). delivery_failed surfaces the reason.
    rd = tmp_path / "t-1" / "r1"
    rd.mkdir(parents=True)
    assert run_outcome.delivery_failed(rd) is None  # no marker
    (rd / "delivery.json").write_text(
        json.dumps({"deadline": "2026-01-01T00:00:00+00:00"})
    )
    assert run_outcome.delivery_failed(rd) is None  # deadline only, not a failure
    (rd / "delivery.json").write_text(
        json.dumps({"failed": True, "reason": "push rejected"})
    )
    assert run_outcome.delivery_failed(rd) == "push rejected"


def _write_delivery_deadline(run_dir: Path, deadline: datetime) -> None:
    (run_dir / "delivery.json").write_text(
        json.dumps({"deadline": deadline.isoformat()})
    )


def test_delivery_deadline_and_timeout_helpers(tmp_path: Path) -> None:
    # #189: attach bounds delivery on the daemon's recorded deadline, never a
    # fixed guess. Past deadline → timed out; future deadline → not (whatever the
    # elapsed); no marker → a generous flat fallback on elapsed seconds.
    rd = tmp_path / "t-1" / "r1"
    rd.mkdir(parents=True)
    assert run_outcome.delivery_deadline(rd) is None  # no marker yet
    assert (
        run_outcome.delivery_timed_out(rd, delivering_seconds=2.0) is False
    )  # fallback not yet reached

    _write_delivery_deadline(rd, datetime.now(UTC) - timedelta(seconds=1))
    assert (
        run_outcome.delivery_timed_out(rd, delivering_seconds=2.0) is True
    )  # past deadline

    _write_delivery_deadline(rd, datetime.now(UTC) + timedelta(hours=1))
    assert (
        run_outcome.delivery_timed_out(rd, delivering_seconds=1_000_000) is False
    )  # within budget, whatever the elapsed

    (rd / "delivery.json").unlink()  # no marker → flat fallback kicks in
    assert (
        run_outcome.delivery_timed_out(
            rd, delivering_seconds=run_outcome.DELIVERY_FALLBACK_SECONDS
        )
        is True
    )


def test_capture_outcome_records_delivery_failure(tmp_path: Path) -> None:
    # #194: capture reads the run's private marker and sets delivery_failed + the
    # reason (and no pr_url) so the summary reports the failure honestly.
    rd = tmp_path / "t-1" / "r1"
    rd.mkdir(parents=True)
    (rd / "delivery.json").write_text(
        json.dumps({"failed": True, "reason": "push rejected"})
    )
    outcome = run_outcome.RunOutcome()
    run_outcome.capture_outcome(outcome, rd, {"status": "approved", "rounds": 2})
    assert outcome.delivery_failed is True
    assert outcome.failure_reason == "push rejected"
    assert outcome.pr_url is None


def test_capture_outcome_stashes_pr_url_and_failure_reason(tmp_path: Path) -> None:
    # the offline reader pulls the failure reason from this run's state.json and
    # the delivered PR url from its (succeeded) result.json.
    run_dir = tmp_path / "t-1" / "r1"
    (run_dir / "handoff").mkdir(parents=True)
    (run_dir.parent / "result.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "run_id": "r1",
                "pr_url": "https://github.com/o/r/pull/170",
            }
        )
    )
    outcome = run_outcome.RunOutcome()
    run_outcome.capture_outcome(outcome, run_dir, {"status": "approved", "rounds": 1})
    assert outcome.pr_url == "https://github.com/o/r/pull/170"

    failed = run_outcome.RunOutcome()
    run_outcome.capture_outcome(
        failed, run_dir, {"status": "failed", "failure_reason": "boom"}
    )
    assert failed.failure_reason == "boom"
    assert failed.pr_url is None  # a non-approved run shows no PR url


def test_recover_reaped_outcome_delegates_to_completion_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # correctness/f-003: a reaped (success-cleaned) run's outcome is recovered
    # from the completion store, keyed by this run id (the run-id binding lives in
    # idempotency.lookup_completed_for_run).
    run_dir = tmp_path / "t-1" / "r1"
    run_dir.mkdir(parents=True)
    seen: dict[str, tuple[str, str]] = {}

    def fake_lookup(task_id: str, run_id: str) -> dict | None:
        seen["args"] = (task_id, run_id)
        return {"task_id": task_id, "status": "succeeded"}

    monkeypatch.setattr(run_outcome, "lookup_completed_for_run", fake_lookup)
    assert run_outcome.recover_reaped_outcome(run_dir) == {"status": "approved"}
    assert seen["args"] == ("t-1", "r1")  # looked up by task id + run id
    # no matching record → nothing to recover
    monkeypatch.setattr(run_outcome, "lookup_completed_for_run", lambda t, r: None)
    assert run_outcome.recover_reaped_outcome(run_dir) is None


def test_recover_reaped_outcome_carries_pr_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #188: a reaped success recovers its PR url from the completion-store payload
    # (this run's result.json), so a write-then-reap between polls still surfaces
    # the PR.
    run_dir = tmp_path / "t-1" / "r1"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        run_outcome,
        "lookup_completed_for_run",
        lambda t, r: {"status": "succeeded", "pr_url": "https://github.com/o/r/pull/9"},
    )
    recovered = run_outcome.recover_reaped_outcome(run_dir)
    assert recovered == {
        "status": "approved",
        "pr_url": "https://github.com/o/r/pull/9",
    }


def test_recover_reaped_outcome_carries_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #196 (Gap A2): the recovered outcome includes the round count (from the
    # completion record's result.json), so a reaped success's summary shows rounds
    # — not just the verdict + PR.
    run_dir = tmp_path / "t-1" / "r1"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(
        run_outcome,
        "lookup_completed_for_run",
        lambda t, r: {
            "status": "succeeded",
            "rounds": 4,
            "pr_url": "https://github.com/o/r/pull/9",
        },
    )
    recovered = run_outcome.recover_reaped_outcome(run_dir)
    assert recovered == {
        "status": "approved",
        "rounds": 4,
        "pr_url": "https://github.com/o/r/pull/9",
    }

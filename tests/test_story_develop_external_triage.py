"""Tests for ``plugins.story_develop.external_triage`` (PRD S5a, slice B).

Triage is a separate cheap read-only step, not a sceptical clause in the
fixer prompt: one container turn checks each external claim against the code
and may REJECT it only with cited evidence. The load-bearing property —
pinned hardest here because a prompt re-tune is most likely to regress it —
is **default-to-act**: anything short of an explicit, evidenced REJECT
proceeds to the fixer (actioning a false positive is recoverable; ignoring a
true one is the failure the PRD exists to fix).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.plugins.story_develop import external_triage as triage_mod
from lithos_loom.plugins.story_develop.config import DevelopConfig
from lithos_loom.plugins.story_develop.external_triage import (
    parse_triage_verdicts,
    triage_external_findings,
)
from lithos_loom.plugins.story_develop.handoff import Finding
from lithos_loom.plugins.story_develop.panel import ReviewOutcome
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange

# ── the verdict parser (pure) ──────────────────────────────────────────


def test_parser_reads_proceed_and_evidenced_reject() -> None:
    text = (
        "## Verdicts\n"
        "- f-001: PROCEED\n"
        "- f-002: REJECT — util.py:14 already guards None; the claimed crash "
        "cannot occur\n"
    )
    verdicts = parse_triage_verdicts(text, ["f-001", "f-002"])
    assert verdicts.proceed == ("f-001",)
    assert "util.py:14" in verdicts.rejections["f-002"]


def test_reject_without_evidence_proceeds() -> None:
    """Rejection requires CITED evidence — a bare REJECT is not enough."""
    text = "- f-001: REJECT\n- f-002: REJECT —   \n"
    verdicts = parse_triage_verdicts(text, ["f-001", "f-002"])
    assert verdicts.proceed == ("f-001", "f-002")
    assert verdicts.rejections == {}


def test_unmentioned_and_garbled_ids_proceed() -> None:
    """Default-to-act: a finding the triage output never mentions (or mangles)
    is seeded, not dropped."""
    text = "- f-001: REJECT — src/x.py:12 refutes it\n- something unparseable\n"
    verdicts = parse_triage_verdicts(text, ["f-001", "f-002", "f-003"])
    assert verdicts.proceed == ("f-002", "f-003")
    assert set(verdicts.rejections) == {"f-001"}


def test_unknown_ids_in_output_are_ignored() -> None:
    text = "- f-099: REJECT — not even a real finding\n"
    verdicts = parse_triage_verdicts(text, ["f-001"])
    assert verdicts.proceed == ("f-001",)
    assert verdicts.rejections == {}


# ── the container step (boundary-stubbed) ──────────────────────────────


def _config(tmp_path: Path) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path / "repo",
        description="A PR",
        work_dir=tmp_path / "work",
        acceptance_criteria="do the thing",
    )


def _change() -> ResolvedChange:
    return ResolvedChange(
        base_sha="b" * 40, head_sha="h" * 40, head_ref="#62", head_branch="feat"
    )


def _outcome() -> ReviewOutcome:
    return ReviewOutcome(
        reviewer="external",
        status="FINDINGS",
        passed=False,
        max_severity="minor",
        findings=[
            Finding(
                finding_id="f-001",
                severity="minor",
                status="open",
                rationale="claim one",
            ),
            Finding(
                finding_id="f-002",
                severity="minor",
                status="open",
                rationale="claim two",
            ),
        ],
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    verdict_text: str | None,
    turn_succeeds: bool = True,
    cost: float = 0.05,
    tracked: frozenset[str] = frozenset({"src/x.py"}),
) -> dict:
    captured: dict = {}
    wt = tmp_path / "wt"
    wt.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        triage_mod.worktree, "create_at", lambda *a, **k: captured.setdefault("wt", wt)
    )

    def fake_tracked(path: Path) -> frozenset[str]:
        captured["tracked_wt"] = path
        return tracked

    monkeypatch.setattr(triage_mod, "_tracked_files", fake_tracked)
    monkeypatch.setattr(
        triage_mod.worktree,
        "remove",
        lambda p, force=False: captured.setdefault("removed", p),
    )
    monkeypatch.setattr(
        triage_mod,
        "build_run_cmd",
        lambda config, **k: ("triage-container", ["docker", "run"]),
    )
    monkeypatch.setattr(
        triage_mod.containers,
        "start_container",
        lambda cmd: captured.setdefault("started", cmd),
    )
    monkeypatch.setattr(
        triage_mod.containers,
        "stop_container",
        lambda name: captured.setdefault("stopped", name),
    )

    def fake_run_turn(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["read_only_turn"] = True
        if verdict_text is not None:
            handoff_dir = captured["config"].handoff_dir
            handoff_dir.mkdir(parents=True, exist_ok=True)
            (handoff_dir / triage_mod.TRIAGE_HANDOFF_NAME).write_text(
                verdict_text, encoding="utf-8"
            )
        return SimpleNamespace(
            succeeded=turn_succeeds,
            completed=turn_succeeds,
            session_id="s",
            cost_usd=cost,
            result_text="",
        )

    monkeypatch.setattr(triage_mod.turns, "run_turn", fake_run_turn)
    return captured


def test_triage_runs_read_only_and_returns_verdicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(
        monkeypatch,
        tmp_path,
        verdict_text="- f-001: PROCEED\n- f-002: REJECT — src/x.py:12 refutes it\n",
    )
    captured["config"] = config

    result = triage_external_findings(config, _change(), _outcome(), timeout=600)

    assert result.proceed == ("f-001",)
    assert "src/x.py:12" in result.rejections["f-002"]
    assert result.cost_usd == 0.05
    assert result.note == ""
    assert "claim one" in captured["prompt"]
    assert captured["stopped"] == "triage-container"
    assert captured["removed"] == captured["wt"]
    # The citation referent check reads the snapshot of THIS worktree.
    assert captured["tracked_wt"] == captured["wt"]


def test_step_rejection_citing_unknown_file_proceeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #345 re-review 3, at the step level: a rejection whose only
    citation names no tracked file (`HTTP:404`) is uncited and proceeds."""
    config = _config(tmp_path)
    captured = _install(
        monkeypatch,
        tmp_path,
        verdict_text=(
            "- f-001: REJECT — HTTP:404 is expected here\n"
            "- f-002: REJECT — src/x.py:12 refutes it\n"
        ),
    )
    captured["config"] = config

    result = triage_external_findings(config, _change(), _outcome(), timeout=600)

    assert result.proceed == ("f-001",)
    assert set(result.rejections) == {"f-002"}


def test_turn_failure_defaults_to_act(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed/absent triage turn proves nothing about the claims — ALL
    findings proceed, with the degradation on record."""
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, verdict_text=None, turn_succeeds=False)
    captured["config"] = config

    result = triage_external_findings(config, _change(), _outcome(), timeout=600)

    assert result.proceed == ("f-001", "f-002")
    assert result.rejections == {}
    assert result.note != ""


def test_missing_verdict_file_defaults_to_act(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, verdict_text=None, turn_succeeds=True)
    captured["config"] = config

    result = triage_external_findings(config, _change(), _outcome(), timeout=600)

    assert result.proceed == ("f-001", "f-002")
    assert result.note != ""


def test_reject_with_uncited_prose_proceeds() -> None:
    """PR #345 review F2: the cited-evidence rule is enforced by the PARSER,
    not just the prompt — a vague rejection ("nope", "seems fine") must not
    silently discard a claim. Evidence counts only when it cites a
    ``file:line`` location."""
    text = (
        "- f-001: REJECT — nope\n"
        "- f-002: REJECT — the reviewer misread the intent, this is fine\n"
        "- f-003: REJECT — src/util.py:14 already guards the None case\n"
        "- f-004: REJECT — Makefile:12 sets the flag before the target runs\n"
    )
    verdicts = parse_triage_verdicts(text, ["f-001", "f-002", "f-003", "f-004"])
    assert verdicts.proceed == ("f-001", "f-002")
    assert set(verdicts.rejections) == {"f-003", "f-004"}


def test_reject_with_dotted_prose_or_lineless_file_proceeds() -> None:
    """PR #345 re-review 2: a dotted token is not a citation. ``v1.2`` is
    version prose, and a bare filename (``README.md``) names no line whose
    behaviour could refute anything — both were reproduced slipping through
    the extension-based pattern. Only a ``file:line`` token counts."""
    text = (
        "- f-001: REJECT — reviewer misunderstood version v1.2\n"
        "- f-002: REJECT — README.md says nothing about this behavior\n"
        "- f-003: REJECT — 12:30 is when the cron fires, not a race\n"
        "- f-004: REJECT — external_triage.py:67 anchors the verdict lines\n"
    )
    verdicts = parse_triage_verdicts(text, ["f-001", "f-002", "f-003", "f-004"])
    assert verdicts.proceed == ("f-001", "f-002", "f-003")
    assert set(verdicts.rejections) == {"f-004"}


def test_reject_citing_a_token_outside_the_repo_proceeds() -> None:
    """PR #345 re-review 3: citation-SHAPED prose (``HTTP:404``,
    ``timeout:30``, ``RFC:7231``) must not suppress a finding. With the
    worktree's tracked-file snapshot supplied, a REJECT counts only when a
    cited path resolves to a real file in the repo. The boundary is
    deliberate and ends here: referent yes; line-existence and content no —
    no shape check can tell a true claim from a false one, so semantic
    triage quality is measured by the S8 eval fixtures, not the parser."""
    repo_files = frozenset({"src/util.py", "Makefile"})
    text = (
        "- f-001: REJECT — HTTP:404 is expected here\n"
        "- f-002: REJECT — timeout:30 already covers it\n"
        "- f-003: REJECT — RFC:7231 defines this behavior\n"
        "- f-004: REJECT — src/util.py:14 already guards the None case\n"
        "- f-005: REJECT — other/place.py:3 does the guard\n"
    )
    verdicts = parse_triage_verdicts(
        text,
        ["f-001", "f-002", "f-003", "f-004", "f-005"],
        repo_files=repo_files,
    )
    assert verdicts.proceed == ("f-001", "f-002", "f-003", "f-005")
    assert set(verdicts.rejections) == {"f-004"}


def test_citation_paths_are_normalised_to_the_repo_root() -> None:
    """The triage agent reads the tree at ``/workspace``, so container-rooted
    and ``./``-relative spellings of a real file still count."""
    repo_files = frozenset({"src/util.py"})
    text = (
        "- f-001: REJECT — /workspace/src/util.py:14 guards it\n"
        "- f-002: REJECT — ./src/util.py:14 guards it\n"
    )
    verdicts = parse_triage_verdicts(text, ["f-001", "f-002"], repo_files=repo_files)
    assert set(verdicts.rejections) == {"f-001", "f-002"}

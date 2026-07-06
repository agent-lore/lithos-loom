"""CLI-boundary validation tests for the story-develop entry point.

These exercise ``main()``'s fail-fast guards, which return before any Docker /
agent work happens.
"""

from __future__ import annotations

import json
from pathlib import Path

from lithos_loom.plugins.story_develop.__main__ import main


def test_main_rejects_empty_description(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "   "])
    assert rc == 2
    assert "description must not be empty" in capsys.readouterr().err


def test_main_rejects_non_git_repo(tmp_path: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_path), "--description", "do a thing"])
    assert rc == 2
    assert "not a git repository" in capsys.readouterr().err


def test_main_rejects_invalid_reviewer_name(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "code quality",
        ]
    )
    assert rc == 2
    assert "invalid --reviewer" in capsys.readouterr().err


def test_main_rejects_invalid_coder_effort(tmp_git_repo: Path, capsys) -> None:
    # Standalone errors (rc 2) on an off-canonical level. Validated by
    # parse_effort, NOT argparse choices — so the same flag can friction-degrade
    # in daemon mode (see test_story_develop_daemon). `minimal` is an
    # OpenCode/Codex level, not in Loom's canonical (Claude) set.
    argv = ["--repo", str(tmp_git_repo), "--description", "x"]
    argv += ["--coder-effort", "minimal"]
    rc = main(argv)
    assert rc == 2
    assert "effort must be one of" in capsys.readouterr().err


def test_main_rejects_invalid_reviewer_effort(tmp_git_repo: Path, capsys) -> None:
    argv = ["--repo", str(tmp_git_repo), "--description", "x"]
    argv += ["--reviewer-effort", "lo"]
    rc = main(argv)
    assert rc == 2
    assert "effort must be one of" in capsys.readouterr().err


def test_main_rejects_whitespace_coder_model(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--coder-model", "   "]
    )
    assert rc == 2
    assert "model must be a non-empty string" in capsys.readouterr().err


def test_main_rejects_whitespace_reviewer_model(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--reviewer-model", "  "]
    )
    assert rc == 2
    assert "model must be a non-empty string" in capsys.readouterr().err


def test_main_rejects_blank_notify_login(tmp_git_repo: Path, capsys) -> None:
    # #113: the CLI must reject a blank --notify-github-login (matching the TOML
    # surface), not silently disable notifications.
    rc = main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--notify-github-login", ""]
    )
    assert rc == 2
    assert "--notify-github-login must not be empty" in capsys.readouterr().err


def test_main_rejects_whitespace_notify_login(tmp_git_repo: Path, capsys) -> None:
    # "   " is truthy, so without normalization it would reach gh as a bogus login.
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--notify-github-login",
            "   ",
        ]
    )
    assert rc == 2
    assert "--notify-github-login must not be empty" in capsys.readouterr().err


def test_main_rejects_reviewer_model_with_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'cq'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer-model",
            "opus",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "cannot be combined with --develop-config" in capsys.readouterr().err


def test_main_threads_model_and_effort_into_config(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """--coder/--reviewer model+effort flags land on the resolved config."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["config"] = config
        return DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.0,
            review_cost_usd=0.0,
            message="m",
        )

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "do a thing",
            "--coder-model",
            "  opus  ",  # normalised (stripped) into the config
            "--coder-effort",
            "xhigh",
            "--reviewer",
            "code-quality",
            "--reviewer",
            "security",
            "--reviewer-model",
            "sonnet",
            "--reviewer-effort",
            "high",
        ]
    )
    assert rc == 0
    cfg = captured["config"]
    assert cfg.coder_model == "opus" and cfg.coder_effort == "xhigh"
    assert {s.name for s in cfg.reviewers} == {"code-quality", "security"}
    for spec in cfg.reviewers:
        assert spec.model == "sonnet" and spec.effort == "high"


def test_main_standalone_halts_on_unknown_review_profile(
    tmp_git_repo: Path, monkeypatch, capsys
) -> None:
    """An explicit-but-unknown --review-profile fails closed before develop (#139)."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    def _boom(*a, **k):
        raise AssertionError("develop must not run on a fail-closed halt")

    monkeypatch.setattr(main_mod, "develop", _boom)
    monkeypatch.setattr(
        main_mod, "load_review_profile_policy", lambda: (None, "halt", ())
    )
    rc = main_mod.main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--review-profile", "nope"]
    )
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "halting" in err
    assert "nope" in err  # the [Friction] line names the bad profile


def test_main_standalone_known_review_profile_runs(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """A known --review-profile does not halt — the run proceeds to develop (#139)
    and the resolved name threads onto the config (#140)."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["ran"] = True
        captured["config"] = config
        return DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.0,
            review_cost_usd=0.0,
            message="m",
        )

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(
        main_mod, "load_review_profile_policy", lambda: (None, "halt", ())
    )
    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--review-profile",
            "thorough",
        ]
    )
    assert rc == 0
    assert captured.get("ran") is True
    assert captured["config"].review_profile == "thorough"


def _approved(tmp_path: Path):
    from lithos_loom.plugins.story_develop.develop import DevelopResult

    return DevelopResult(
        status="approved",
        run_id="r1",
        worktree=tmp_path,
        branch="b",
        base_sha="0" * 40,
        commits=["c"],
        rounds=1,
        handoff_present=True,
        coder_cost_usd=0.0,
        review_cost_usd=0.0,
        message="m",
    )


def test_main_standalone_profile_drives_panel(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """#140 slice 2: with no --reviewer, the profile's personas become the panel."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["config"] = config
        return _approved(tmp_path)

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(
        main_mod, "load_review_profile_policy", lambda: (None, "halt", ())
    )
    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--review-profile",
            "thorough",
        ]
    )
    assert rc == 0
    assert [s.name for s in captured["config"].reviewers] == [
        "correctness",
        "security",
        "architecture",
        "test-quality",
        "dependency-hygiene",
    ]


def test_main_standalone_explicit_reviewer_overrides_profile(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """An explicit --reviewer wins; the profile does NOT substitute the panel."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["config"] = config
        return _approved(tmp_path)

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(
        main_mod, "load_review_profile_policy", lambda: (None, "halt", ())
    )
    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--review-profile",
            "standard",
            "--reviewer",
            "security",
        ]
    )
    assert rc == 0
    assert [s.name for s in captured["config"].reviewers] == ["security"]


def test_main_standalone_minimal_keeps_default_reviewer_with_friction(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """minimal is gate-only; until the floor slice it keeps the built-in reviewer +
    a gate-only friction (no rubber-stamp)."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["config"] = config
        return _approved(tmp_path)

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(
        main_mod, "load_review_profile_policy", lambda: (None, "halt", ())
    )
    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--review-profile",
            "minimal",
        ]
    )
    assert rc == 0
    assert [s.name for s in captured["config"].reviewers] == ["code-quality"]
    assert "gate-only" in capsys.readouterr().err.lower()


def test_main_standalone_profile_panel_gets_reviewer_cli_layering(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """#140 slice 2 (review fix): --reviewer-model/-effort/-fallback layer onto the
    profile's persona panel — filling only where a persona leaves it unset, mirroring
    daemon mode (a persona's own effort, e.g. security=xhigh, is preserved)."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["config"] = config
        return _approved(tmp_path)

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(
        main_mod, "load_review_profile_policy", lambda: (None, "halt", ())
    )
    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--review-profile",
            "standard",
            "--reviewer-model",
            "opus",
            "--reviewer-effort",
            "high",
            "--reviewer-fallback",
            "claude",
        ]
    )
    assert rc == 0
    by_name = {s.name: s for s in captured["config"].reviewers}
    assert sorted(by_name) == ["correctness", "security"]
    # model unset on both personas -> filled from --reviewer-model
    assert by_name["correctness"].model == "opus"
    assert by_name["security"].model == "opus"
    # effort filled where unset; security's own xhigh is respected
    assert by_name["correctness"].effort == "high"
    assert by_name["security"].effort == "xhigh"
    # personas carry no fallback chain -> the route --reviewer-fallback applies
    assert by_name["correctness"].fallback_chain == ("claude",)
    assert by_name["security"].fallback_chain == ("claude",)


def test_main_accepts_codex_coder(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch
) -> None:
    """#94: ``--coder codex`` is a valid choice and lands on the config."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult

    captured: dict = {}

    def fake_develop(config, **kw):
        captured["config"] = config
        return DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.0,
            review_cost_usd=0.0,
            message="m",
        )

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    rc = main_mod.main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--coder", "codex"]
    )
    assert rc == 0
    assert captured["config"].coder == "codex"


def test_main_rejects_bad_max_rounds(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--description", "x", "--max-rounds", "0"])
    assert rc == 2
    assert "--max-rounds must be >= 1" in capsys.readouterr().err


def test_main_rejects_duplicate_reviewers(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "cq",
            "--reviewer",
            "cq",
        ]
    )
    assert rc == 2
    assert "duplicate --reviewer" in capsys.readouterr().err


def test_main_rejects_reviewer_with_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'cq'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--reviewer",
            "other",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_main_rejects_bad_develop_config(
    tmp_git_repo: Path, tmp_path: Path, capsys
) -> None:
    cfg = tmp_path / "develop.toml"
    cfg.write_text("[[reviewers]]\nname = 'Bad Name'\n")
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--develop-config",
            str(cfg),
        ]
    )
    assert rc == 2
    assert "must be a lowercase" in capsys.readouterr().err


def test_main_rejects_zero_pause_poll(tmp_git_repo: Path, capsys) -> None:
    # 0 would spin forever on zero-second pauses; negative would crash sleep()
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--pause-poll-minutes",
            "0",
        ]
    )
    assert rc == 2
    assert "--pause-poll-minutes must be >= 1" in capsys.readouterr().err


def test_main_rejects_bad_max_cost(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        ["--repo", str(tmp_git_repo), "--description", "x", "--max-cost-usd", "0"]
    )
    assert rc == 2
    assert "--max-cost-usd must be > 0" in capsys.readouterr().err


def test_main_rejects_negative_max_pause(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--max-pause-minutes",
            "-1",
        ]
    )
    assert rc == 2
    assert "--max-pause-minutes must be >= 0" in capsys.readouterr().err


def test_main_rejects_task_id_with_no_lithos(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo), "--task-id", "t-1", "--no-lithos"])
    assert rc == 2
    assert "incompatible" in capsys.readouterr().err


def test_main_requires_description_or_task_id(tmp_git_repo: Path, capsys) -> None:
    rc = main(["--repo", str(tmp_git_repo)])
    assert rc == 2
    assert "one of --description or --task-id" in capsys.readouterr().err


def test_main_rejects_missing_ac_file(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--acceptance-criteria",
            "@/nonexistent/ac.md",
        ]
    )
    assert rc == 2
    assert "cannot read --acceptance-criteria" in capsys.readouterr().err


def test_main_rejects_blank_ac(tmp_git_repo: Path, capsys) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--acceptance-criteria",
            "  ",
        ]
    )
    assert rc == 2
    assert "--acceptance-criteria must not be empty" in capsys.readouterr().err


def test_main_task_id_resolves_description_and_posts(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """--task-id alone: task text becomes the description, metadata AC flows
    into the config, and results are posted back after the run."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.lithos_io import TaskContext

    captured: dict = {}

    def fake_fetch(url, task_id):
        captured["fetched"] = (url, task_id)
        return TaskContext(
            task_id=task_id,
            title="Add a flag",
            description="Body.",
            acceptance_criteria="must have tests",
            metadata={},
        )

    def fake_develop(config, **kw):
        captured["config"] = config
        return DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.1,
            review_cost_usd=0.1,
            message="ok",
        )

    def fake_post(url, task_id, result, **kw):
        captured["posted"] = (url, task_id, result.status)
        return True

    monkeypatch.setattr(main_mod, "fetch_task_context", fake_fetch)
    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(main_mod, "post_results", fake_post)

    rc = main_mod.main(["--repo", str(tmp_git_repo), "--task-id", "t-9"])
    assert rc == 0
    assert captured["fetched"][1] == "t-9"
    cfg = captured["config"]
    assert cfg.description == "Add a flag\n\nBody."
    assert cfg.acceptance_criteria == "must have tests"
    assert captured["posted"] == ("http://localhost:8765", "t-9", "approved")
    out = capsys.readouterr().out
    assert "developing Lithos task t-9" in out
    assert "results posted to task t-9" in out


def test_main_rejects_task_id_with_description(tmp_git_repo: Path, capsys) -> None:
    # The task IS the description — a mixed source would let the audit trail
    # claim task X while developing unrelated text.
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--task-id",
            "t-1",
            "--description",
            "something else entirely",
        ]
    )
    assert rc == 2
    assert "--task-id and --description are incompatible" in capsys.readouterr().err


def test_main_rejects_complete_on_approval_without_task_id(
    tmp_git_repo: Path, capsys
) -> None:
    rc = main(
        [
            "--repo",
            str(tmp_git_repo),
            "--description",
            "x",
            "--complete-on-approval",
        ]
    )
    assert rc == 2
    assert "--complete-on-approval requires --task-id" in capsys.readouterr().err


def test_main_complete_on_approval_completes_task(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.lithos_io import TaskContext

    captured: dict = {}

    def fake_fetch(url, task_id):
        return TaskContext(
            task_id=task_id,
            title="T",
            description="",
            acceptance_criteria=None,
            metadata={},
        )

    def _fake_result(status: str) -> DevelopResult:
        return DevelopResult(
            status=status,
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.1,
            review_cost_usd=0.1,
            message="m",
        )

    monkeypatch.setattr(main_mod, "fetch_task_context", fake_fetch)
    monkeypatch.setattr(main_mod, "post_results", lambda *a, **kw: True)
    monkeypatch.setattr(
        main_mod, "complete_task", lambda *a: captured.setdefault("completed", True)
    )

    # approved + flag -> completes
    monkeypatch.setattr(main_mod, "develop", lambda c, **kw: _fake_result("approved"))
    rc = main_mod.main(
        ["--repo", str(tmp_git_repo), "--task-id", "t-1", "--complete-on-approval"]
    )
    assert rc == 0 and captured.get("completed") is True
    assert "marked completed" in capsys.readouterr().out

    # NOT approved + flag -> no completion
    captured.clear()
    monkeypatch.setattr(main_mod, "develop", lambda c, **kw: _fake_result("stalled"))
    rc = main_mod.main(
        ["--repo", str(tmp_git_repo), "--task-id", "t-1", "--complete-on-approval"]
    )
    assert rc == 1 and "completed" not in captured

    # approved WITHOUT the flag -> no completion (default behaviour)
    captured.clear()
    monkeypatch.setattr(main_mod, "develop", lambda c, **kw: _fake_result("approved"))
    rc = main_mod.main(["--repo", str(tmp_git_repo), "--task-id", "t-1"])
    assert rc == 0 and "completed" not in captured


def test_main_open_pr_passes_issue_link_to_delivery(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """--task-id + --open-pr: the task's github_issue_url reaches deliver()
    (for the PR's Closes line) and the PR URL flows into the Lithos post."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.lithos_io import TaskContext
    from lithos_loom.plugins.story_develop.pr_delivery import DeliveryOutcome

    captured: dict = {}

    monkeypatch.setattr(
        main_mod,
        "fetch_task_context",
        lambda url, tid: TaskContext(
            task_id=tid,
            title="T",
            description="",
            acceptance_criteria=None,
            metadata={"github_issue_url": "https://github.com/o/r/issues/9"},
        ),
    )
    monkeypatch.setattr(
        main_mod,
        "develop",
        lambda c, **kw: DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.1,
            review_cost_usd=0.1,
            message="m",
        ),
    )

    def fake_deliver(config, result, **kw):
        captured["deliver_kwargs"] = kw
        return DeliveryOutcome(pr_url="https://github.com/o/r/pull/12", pr_number=12)

    def fake_post(url, task_id, result, *, pr_url=None, delivery=None):
        captured["posted_pr_url"] = delivery.pr_url if delivery else pr_url
        return True

    # deliver() is called from pr_delivery.deliver_guarded now — patch it there.
    monkeypatch.setattr(
        "lithos_loom.plugins.story_develop.pr_delivery.deliver", fake_deliver
    )
    monkeypatch.setattr(main_mod, "post_results", fake_post)

    rc = main_mod.main(["--repo", str(tmp_git_repo), "--task-id", "t-1", "--open-pr"])
    assert rc == 0
    assert (
        captured["deliver_kwargs"]["github_issue_url"]
        == "https://github.com/o/r/issues/9"
    )
    assert captured["posted_pr_url"] == "https://github.com/o/r/pull/12"
    assert "pull/12" in capsys.readouterr().out


def test_main_open_pr_delivery_failure_skips_completion_and_exits_nonzero(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    """#194 parity (ARCH-1.S3): an approved STANDALONE run whose PR delivery RAISES
    produced no PR — so main() must NOT mark the task done and must exit non-zero,
    matching daemon mode. Before this fix it printed DELIVERY FAILED but still ran
    --complete-on-approval and returned 0. It must also write the private
    delivery.json failure marker so `develop attach` reports it offline."""
    from lithos_loom.plugins.story_develop import __main__ as main_mod
    from lithos_loom.plugins.story_develop import pr_delivery
    from lithos_loom.plugins.story_develop.develop import DevelopResult
    from lithos_loom.plugins.story_develop.lithos_io import TaskContext

    captured: dict = {}
    monkeypatch.setattr(
        main_mod,
        "fetch_task_context",
        lambda url, tid: TaskContext(
            task_id=tid,
            title="T",
            description="",
            acceptance_criteria=None,
            metadata={},
        ),
    )

    def fake_develop(config, **kw):
        # the real develop() creates the run dir before delivery; mirror that so
        # the best-effort marker write has somewhere to land.
        config.run_dir.mkdir(parents=True, exist_ok=True)
        return DevelopResult(
            status="approved",
            run_id="r1",
            worktree=tmp_path,
            branch="b",
            base_sha="0" * 40,
            commits=["c"],
            rounds=1,
            handoff_present=True,
            coder_cost_usd=0.1,
            review_cost_usd=0.1,
            message="m",
        )

    monkeypatch.setattr(main_mod, "develop", fake_develop)
    monkeypatch.setattr(main_mod, "post_results", lambda *a, **kw: True)
    monkeypatch.setattr(
        main_mod, "complete_task", lambda *a: captured.setdefault("completed", True)
    )

    seen: dict = {}

    def boom_deliver(config, result, **kw):
        seen["run_dir"] = config.run_dir
        raise RuntimeError("gh pr create failed: HTTP 422")

    # delivery orchestration lives in pr_delivery.deliver_guarded, so patch the
    # deliver() it calls there (not the name imported into __main__).
    monkeypatch.setattr(pr_delivery, "deliver", boom_deliver)

    rc = main_mod.main(
        [
            "--repo",
            str(tmp_git_repo),
            "--task-id",
            "t-1",
            "--open-pr",
            "--complete-on-approval",
        ]
    )
    assert rc == 1  # NOT 0 — an approved run with no PR is not a clean success
    assert "completed" not in captured  # the task must NOT be marked done
    assert "DELIVERY FAILED" in capsys.readouterr().err
    # the private failure marker is written for offline `develop attach` visibility
    marker = json.loads((seen["run_dir"] / "delivery.json").read_text(encoding="utf-8"))
    assert marker["failed"] is True
    assert "gh pr create failed" in marker["reason"]

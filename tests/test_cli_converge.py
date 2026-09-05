"""Tests for the ``lithos-loom develop converge`` command (converge PR 3/3).

``converge_pr`` (the heavy orchestration) is stubbed; these tests cover the CLI
wiring: PR resolution, the non-PR rejection, acceptance-criteria precedence,
reviewer/profile/coder selection, ``--no-push`` / ``--max-rounds`` threading,
``--json`` output, and the exit code following the converge status.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import converge as converge_cli
from lithos_loom.cli.develop import develop_app
from lithos_loom.plugins.story_develop.config import DEFAULT_IMAGE
from lithos_loom.plugins.story_develop.converge import ConvergeResult
from lithos_loom.plugins.story_develop.review_resolve import ResolvedChange

runner = CliRunner()

# Note on asserting fail-closed: a `typer.BadParameter` renders through Typer's
# Rich error Console, which exits 2 and writes a panel whose word-wrapping is
# width-dependent and (in click 8.3) not reliably captured in `result.output`
# under CI (a hyphenated flag like `--max-cost` folds across lines). So the
# fail-closed tests below assert on the exit code (2 = UsageError) + the
# orchestrator never being reached, not on the (non-deterministic) panel text.


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    captured: dict = {}

    # #304/#305: default models come from converge's own loaded host config
    # (honouring --config), applied via review.apply_model_policy.
    monkeypatch.setattr(
        converge_cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=tmp_path / "work"),
            story_develop=SimpleNamespace(
                default_models={"codex": "gpt-test", "claude": "claude-test"}
            ),
        ),
    )

    def fake_resolve(repo, spec, *, base_branch="main", base_override=None):
        captured["resolve"] = {"spec": spec, "base_override": base_override}
        return ResolvedChange(
            base_sha="b" * 40,
            head_sha="h" * 40,
            head_ref=spec,
            title="A PR title",
            body=captured.get("pr_body", "the intent"),
            head_branch=captured.get("head_branch", "feature"),
            is_fork=captured.get("is_fork", False),
        )

    monkeypatch.setattr(converge_cli, "resolve_change", fake_resolve)

    def fake_converge_pr(config, change, *, no_push=False, external_findings=None):
        captured["config"] = config
        captured["no_push"] = no_push
        captured["external_findings"] = external_findings
        return ConvergeResult(
            status=captured.get("status", "converged"),
            change=change,
            fixer_commits=("fix1",),
            pushed=not no_push,
            pushed_sha="p" * 40,
            message="converged and pushed to feature",
        )

    monkeypatch.setattr(converge_cli, "converge_pr", fake_converge_pr)
    return captured


def test_coder_and_panel_get_default_models(stubs: dict) -> None:
    # #304: converge runs a coder too — it and every reviewer must carry the
    # EXACT per-tool default from the loaded host config.
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    config = stubs["config"]
    assert config.coder == "claude" and config.coder_model == "claude-test"
    assert {s.tool: s.model for s in config.reviewers} == {
        "codex": "gpt-test",
        "claude": "claude-test",
    }


def test_missing_default_model_fails_closed(
    stubs: dict, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        converge_cli,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=tmp_path / "work"),
            story_develop=None,
        ),
    )
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == 2  # UsageError — see the fail-closed note above
    assert "config" not in stubs  # failed before converge_pr ran


def test_resolves_pr_and_passes_ac(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "make it correct"])
    assert result.exit_code == 0, result.output
    assert stubs["resolve"]["spec"] == "#142"
    assert stubs["config"].acceptance_criteria == "make it correct"


def test_pr_body_is_default_ac(stubs: dict) -> None:
    stubs["pr_body"] = "Fix the leak so the handle closes on error."
    result = runner.invoke(develop_app, ["converge", "#142"])
    assert result.exit_code == 0, result.output
    assert "handle closes on error" in stubs["config"].acceptance_criteria


def test_non_pr_spec_is_rejected(stubs: dict) -> None:
    # a range / branch has no pushable head branch — converge pushes to a PR.
    stubs["head_branch"] = ""
    result = runner.invoke(develop_app, ["converge", "abc..def", "--ac", "x"])
    assert result.exit_code == 2  # UsageError — fails closed before orchestration
    assert "config" not in stubs  # never entered the orchestrator


def test_no_push_flag_threads_through(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x", "--no-push"])
    assert result.exit_code == 0, result.output
    assert stubs["no_push"] is True


def test_coder_and_max_rounds_override_config(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--coder", "codex", "--max-rounds", "3"],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].coder == "codex"
    assert stubs["config"].max_rounds == 3


def test_test_timeout_overrides_config(stubs: dict) -> None:
    # A repo whose non-integration suite exceeds the 900s default can never
    # converge without this escape hatch (issue #275). Thread it into the config.
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--test-timeout", "3600"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].test_timeout == 3600


def test_test_timeout_defaults_to_900(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].test_timeout == 900


def test_non_positive_test_timeout_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--test-timeout", "0"]
    )
    assert result.exit_code == 2  # click UsageError (BadParameter)
    assert "config" not in stubs  # never entered the orchestrator


def test_check_command_override_threads_through(stubs: dict) -> None:
    # #273: repeatable `--check-command NAME=CMD` reaches config.check_commands so a
    # slow-suite / over-scoping repo can point a check at its own command.
    result = runner.invoke(
        develop_app,
        [
            "converge",
            "#142",
            "--ac",
            "x",
            "--check-command",
            "typecheck=make typecheck",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_commands == {"typecheck": "make typecheck"}


def test_check_command_defaults_to_empty(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_commands == {}


def test_test_command_threads_through(stubs: dict) -> None:
    # #273 review finding 3: --test-command is the dedicated `test`-check surface the
    # --check-command help/error points to — it must exist and reach the config.
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--test-command", "make test"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].test_command == "make test"


def test_whitespace_test_command_fails_closed(stubs: dict) -> None:
    # #278 review finding 2: a blank / whitespace --test-command would false-green the
    # required test check (reaches `sh -c`, does nothing, exits 0). Reject it up front.
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--test-command", "   "]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_check_state_override_threads_through(stubs: dict) -> None:
    # #273 slice 2: --check-state NAME=STATE reaches config.check_states.
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--check-state", "sast=off"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].check_states == {"sast": "off"}


def test_bad_check_state_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--check-state", "sast=advisory"],
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_parity_command_threads_through(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--parity-command", "make check"]
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].parity_command == "make check"


def test_image_threads_through(stubs: dict) -> None:
    """Without --image, converge silently ran DEFAULT_IMAGE regardless of the
    project's develop_image — so a project whose gate needs a browser could
    never pass one. The flag is the operator's way to match the delivering run."""
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--image", "ralph-sandbox:python-ui"],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].image == "ralph-sandbox:python-ui"


def test_image_defaults_when_not_given(stubs: dict) -> None:
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == 0, result.output
    assert stubs["config"].image == DEFAULT_IMAGE


def test_blank_image_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--image", "   "]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_artifacts_path_threads_through(stubs: dict) -> None:
    """The artifact review pass only fires when a project declares where its
    checks write rendered output; converge had no way to say so."""
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--artifacts-path", "e2e/artifacts"],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].artifacts_path == "e2e/artifacts"


def test_whitespace_parity_command_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--parity-command", "   "]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_malformed_check_command_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--check-command", "make typecheck"],
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_unknown_check_command_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--ac", "x", "--check-command", "typcheck=x"],
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_unsupported_coder_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--coder", "gpt5"]
    )
    assert result.exit_code == 2  # UsageError
    assert "config" not in stubs


def test_non_positive_max_cost_fails_closed(stubs: dict) -> None:
    # validated before any container work — a nonsensical ceiling must fail fast.
    # Assert on the UsageError exit code + orchestrator-not-reached (not the Rich
    # panel text, which doesn't render deterministically under CI — see top note).
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--max-cost", "0"]
    )
    assert result.exit_code == 2  # click UsageError (BadParameter)
    assert "config" not in stubs  # never entered the orchestrator


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_max_cost_fails_closed(stubs: dict, bad: str) -> None:
    # NaN compares False against everything, so a plain `<= 0` check would let
    # `--max-cost nan` through as an effectively-unlimited budget rendered as
    # $nan (Copilot #272). Non-finite ceilings are nonsense — fail closed.
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--max-cost", bad]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_max_rounds_below_one_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--max-rounds", "0"]
    )
    assert result.exit_code == 2
    assert "config" not in stubs


def test_unknown_profile_fails_closed(stubs: dict) -> None:
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--profile", "thorogh"]
    )
    assert result.exit_code == 2  # UsageError
    assert "config" not in stubs


def test_reviewer_override_and_profile(stubs: dict) -> None:
    result = runner.invoke(
        develop_app,
        [
            "converge",
            "#142",
            "--ac",
            "x",
            "--profile",
            "thorough",
            "--reviewer",
            "correctness",
        ],
    )
    assert result.exit_code == 0, result.output
    assert stubs["config"].review_profile == "thorough"
    specs = stubs["config"].reviewers
    assert [r.name for r in specs] == ["correctness"]
    assert specs[0].tool == "codex"


def test_json_summary_written(stubs: dict, tmp_path: Path) -> None:
    out = tmp_path / "converge.json"
    result = runner.invoke(
        develop_app, ["converge", "#142", "--ac", "x", "--json", str(out)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["status"] == "converged"
    assert data["head_branch"] == "feature"
    assert data["pushed"] is True


@pytest.mark.parametrize(
    "status,code",
    [
        ("already_clean", 0),
        ("converged", 0),
        ("not_converged", 1),
        ("merge_race", 1),
        ("failed", 1),
        ("fork_unsupported", 2),
    ],
)
def test_exit_code_follows_status(stubs: dict, status: str, code: int) -> None:
    stubs["status"] = status
    result = runner.invoke(develop_app, ["converge", "#142", "--ac", "x"])
    assert result.exit_code == code, result.output


# --- --from-github (PRD S2 slice B) ------------------------------------------


def _ext(comment_id, *, author="dave", trusted=True, body="a claim"):
    from lithos_loom.plugins.story_develop.external_reviews import ExternalFinding

    return ExternalFinding(
        author=author,
        source="human",
        trusted=trusted,
        review_id=None,
        comment_id=comment_id,
        thread_url=f"https://example/t/{comment_id}",
        head_sha="h" * 40,
        path="src/x.py",
        line=12,
        body=body,
    )


@pytest.fixture
def github_stubs(monkeypatch: pytest.MonkeyPatch, stubs: dict) -> dict:
    stubs["replies"] = []
    monkeypatch.setattr(converge_cli, "repo_name_with_owner", lambda repo: "o/r")

    def fake_fetch(repo, pr_number, *, trusted_bots):
        stubs["fetch"] = {"repo": repo, "pr": pr_number, "bots": tuple(trusted_bots)}
        return stubs.get("trusted", []), stubs.get("untrusted", [])

    monkeypatch.setattr(converge_cli, "fetch_external_findings", fake_fetch)
    monkeypatch.setattr(
        converge_cli,
        "post_thread_reply",
        lambda repo, pr, cid, body: stubs["replies"].append((cid, body)) or True,
    )
    return stubs


def test_from_github_threads_findings_and_replies(
    github_stubs: dict, tmp_path: Path
) -> None:
    from lithos_loom.plugins.story_develop.converge import ConvergeResult
    from lithos_loom.plugins.story_develop.external_reviews import ExternalOutcome

    trusted = [_ext(7), _ext(8)]
    github_stubs["trusted"] = trusted
    github_stubs["untrusted"] = [_ext(9, author="stranger", trusted=False)]

    # converge_pr stub returns per-finding outcomes: one fixed, one rejected.
    def fake_converge_pr(config, change, *, no_push=False, external_findings=None):
        github_stubs["external_findings"] = external_findings
        return ConvergeResult(
            status="converged",
            change=change,
            pushed=True,
            pushed_sha="p" * 40,
            external_outcomes=(
                ExternalOutcome("f-001", trusted[0], "fixed", detail="guarded it"),
                ExternalOutcome("f-002", trusted[1], "rejected", detail="x.py:12"),
            ),
            message="converged and pushed to feature",
        )

    import lithos_loom.cli.converge as cli_mod

    cli_mod.converge_pr, saved = fake_converge_pr, cli_mod.converge_pr
    try:
        result = runner.invoke(
            develop_app,
            ["converge", "#142", "--repo", str(tmp_path), "--ac", "do it"],
            catch_exceptions=False,
        )
    finally:
        cli_mod.converge_pr = saved
    assert result.exit_code == 0  # no --from-github: fetch untouched
    assert "fetch" not in github_stubs

    cli_mod.converge_pr, saved = fake_converge_pr, cli_mod.converge_pr
    try:
        result = runner.invoke(
            develop_app,
            [
                "converge",
                "#142",
                "--repo",
                str(tmp_path),
                "--ac",
                "do it",
                "--from-github",
            ],
            catch_exceptions=False,
        )
    finally:
        cli_mod.converge_pr = saved

    assert result.exit_code == 0
    assert github_stubs["fetch"] == {
        "repo": "o/r",
        "pr": 142,
        "bots": ("copilot-pull-request-reviewer[bot]",),
    }
    assert github_stubs["external_findings"] == tuple(trusted)
    assert "stranger" in result.output  # untrusted reported, and…
    # …the fixed reply carries the pushed sha; the rejection replies too.
    bodies = dict(github_stubs["replies"])
    assert "Fixed in pppppppppp" in bodies[7]
    assert "triage: x.py:12" in bodies[8]


def test_from_github_nothing_to_ingest_exits_clean(
    github_stubs: dict, tmp_path: Path
) -> None:
    github_stubs["trusted"] = []
    github_stubs["untrusted"] = [_ext(9, author="stranger", trusted=False)]

    result = runner.invoke(
        develop_app,
        [
            "converge",
            "#142",
            "--repo",
            str(tmp_path),
            "--ac",
            "do it",
            "--from-github",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "nothing to ingest" in result.output
    assert "config" not in github_stubs  # converge_pr never ran


def test_from_github_unpushed_fix_asserts_nothing_on_the_thread(
    github_stubs: dict, tmp_path: Path
) -> None:
    """A fix that never landed must not claim to have (reply only on push)."""
    from lithos_loom.plugins.story_develop.converge import ConvergeResult
    from lithos_loom.plugins.story_develop.external_reviews import ExternalOutcome

    trusted = [_ext(7)]
    github_stubs["trusted"] = trusted

    def fake_converge_pr(config, change, *, no_push=False, external_findings=None):
        return ConvergeResult(
            status="not_converged",
            change=change,
            pushed=False,
            external_outcomes=(
                ExternalOutcome("f-001", trusted[0], "fixed", detail="tried"),
            ),
            message="loop ended not_converged",
        )

    import lithos_loom.cli.converge as cli_mod

    cli_mod.converge_pr, saved = fake_converge_pr, cli_mod.converge_pr
    try:
        result = runner.invoke(
            develop_app,
            [
                "converge",
                "#142",
                "--repo",
                str(tmp_path),
                "--ac",
                "do it",
                "--from-github",
            ],
            catch_exceptions=False,
        )
    finally:
        cli_mod.converge_pr = saved

    assert result.exit_code == 1
    assert github_stubs["replies"] == []


def test_triage_rejected_exits_zero(stubs: dict, tmp_path: Path) -> None:
    stubs["status"] = "triage_rejected"
    result = runner.invoke(
        develop_app,
        ["converge", "#142", "--repo", str(tmp_path), "--ac", "do it"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0


def test_from_github_answers_a_conversation_finding_on_the_conversation(
    github_stubs: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation-comment finding (#353) has no thread to reply on; the
    epilogue answers it with a conversation comment that names its target —
    the shape the sweep and the fetch use to prove it handled."""
    from lithos_loom.plugins.story_develop.converge import ConvergeResult
    from lithos_loom.plugins.story_develop.external_reviews import (
        ExternalFinding,
        ExternalOutcome,
    )

    url = "https://github.com/o/r/pull/142#issuecomment-5551158842"
    finding = ExternalFinding(
        author="davesnowdon",
        source="human",
        trusted=True,
        review_id=None,
        comment_id=None,
        thread_url=url,
        head_sha="",
        body="Verdict: two P1 gaps",
        issue_comment_id=5551158842,
    )
    github_stubs["trusted"] = [finding]
    pr_comments: list[tuple[int, str]] = []
    monkeypatch.setattr(
        converge_cli,
        "post_pr_comment",
        lambda repo, pr, body: pr_comments.append((pr, body)) or True,
    )

    def fake_converge_pr(config, change, *, no_push=False, external_findings=None):
        return ConvergeResult(
            status="converged",
            change=change,
            pushed=True,
            pushed_sha="p" * 40,
            external_outcomes=(
                ExternalOutcome("f-001", finding, "fixed", detail="bounded the work"),
            ),
            message="converged and pushed to feature",
        )

    import lithos_loom.cli.converge as cli_mod

    cli_mod.converge_pr, saved = fake_converge_pr, cli_mod.converge_pr
    try:
        result = runner.invoke(
            develop_app,
            [
                "converge",
                "#142",
                "--repo",
                str(tmp_path),
                "--ac",
                "do it",
                "--from-github",
            ],
            catch_exceptions=False,
        )
    finally:
        cli_mod.converge_pr = saved

    assert result.exit_code == 0
    assert github_stubs["replies"] == []  # no thread to reply on
    ((pr, body),) = pr_comments
    assert pr == 142
    assert body.startswith("Fixed in pppppppppp")
    assert f"replying to {url}" in body

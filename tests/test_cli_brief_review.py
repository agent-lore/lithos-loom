"""``lithos-loom develop brief-review`` (604fb936): the pass on demand, writing
nothing to Lithos.

The command resolves what dispatch would: the story's project checkout, its
develop settings (the coder's engine and model run the review), and the base —
``origin/main`` fetched now, exactly as a coder's worktree is cut — then prints
the rendered addendum. ``--brief-file`` re-runs a brief without the addendum
already appended to it (how the pilot slices are compared); ``--delta-from``
is the recheck.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import brief_review as brief_mod
from lithos_loom.main import app
from lithos_loom.plugins.story_develop.brief_review import (
    MODE_DELTA,
    MODE_FULL,
    BriefReviewResult,
    parse_addendum,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_DRAFT = """\
## Facts

- **F1. The funnel classifies a raise as the write's failure.** `write_funnel.py:412`.

## Decisions

- **D1. Let `perform` answer a refusal.** It returns a `WriteProblem`.
  - Basis: F1.
"""


def _plain(text: str) -> str:
    return " ".join(_ANSI.sub("", text).split())


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "f.txt").write_text("w6\n")
    run("add", "-A")
    run("commit", "-m", "W6")
    first = run("rev-parse", "HEAD")
    (repo / "f.txt").write_text("w7\n")
    run("commit", "-am", "W7")
    return repo, first, run("rev-parse", "HEAD")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    repo, first, head = _repo(tmp_path)
    seen: dict[str, Any] = {"repo": repo, "first": first, "head": head}
    monkeypatch.setattr(
        brief_mod,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(
                work_dir=tmp_path / "work", lithos_url="http://lithos.test"
            ),
            projects={"lithos-lens": SimpleNamespace(repo=repo)},
            story_develop=SimpleNamespace(default_models={"claude": "test-model"}),
        ),
    )
    story = SimpleNamespace(
        id="24ad5f91-8d51-44e2-84dc-713ffa9dfc1d",
        title="T3-W8: Add a dependency",
        description="Slice W8. Build the relation sentences.",
        metadata={
            "project": "lithos-lens",
            "prd": "docs/prd/t3-curated-write-actions.md",
            "prd_sections": "D11, Routes",
        },
        created_at=datetime(2026, 10, 1, 8, 29, 44, tzinfo=UTC),
    )
    seen["story"] = story

    def fake_fetch(url: str, task_id: str) -> SimpleNamespace:
        seen["fetched"] = (url, task_id)
        return story

    monkeypatch.setattr(brief_mod, "fetch_task", fake_fetch)
    monkeypatch.setattr(
        brief_mod,
        "story_settings_for",
        lambda host, sid, **k: ({"coder": "claude", "coder_model": "m-1"}, None),
    )

    def fake_review(config, inputs, *, base_sha, mode, prior_base, timeout):
        seen.update(
            config=config,
            inputs=inputs,
            base_sha=base_sha,
            mode=mode,
            prior_base=prior_base,
            timeout=timeout,
        )
        return seen.get("result") or BriefReviewResult(
            addendum=parse_addendum(_DRAFT),
            base_sha=base_sha,
            mode=mode,
            prior_base=prior_base,
            cost_usd=0.42,
            raw=_DRAFT,
        )

    monkeypatch.setattr(brief_mod, "review_brief", fake_review)
    return seen


def _invoke(*args: str):
    return CliRunner().invoke(app, ["develop", "brief-review", *args])


def test_prints_the_rendered_addendum_reviewed_at_the_given_base(
    env: dict[str, Any],
) -> None:
    result = _invoke("24ad5f91", "--base", "HEAD")

    assert result.exit_code == 0, result.output
    out = _ANSI.sub("", result.stdout)
    assert out.startswith(f"**Brief review against `{env['head'][:12]}`")
    assert "- **F1. The funnel classifies" in out
    assert "**Decisions**" in out
    assert env["fetched"] == ("http://lithos.test", "24ad5f91")
    assert env["base_sha"] == env["head"]  # a ref resolves to its full sha
    assert env["mode"] == MODE_FULL and env["prior_base"] is None
    inputs = env["inputs"]
    assert inputs.story_id == env["story"].id
    assert inputs.brief == "Slice W8. Build the relation sentences."
    assert inputs.prd == "docs/prd/t3-curated-write-actions.md"
    assert inputs.prd_sections == "D11, Routes"
    assert inputs.written_at == "2026-10-01T08:29:44+00:00"
    config = env["config"]
    assert config.repo == env["repo"]
    assert config.coder_model == "m-1"
    assert "1 fact" in _plain(result.stderr) and "$0.42" in result.stderr


def test_the_default_base_is_origin_main_as_a_coder_would_cut_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str]] = []

    def fake_current(repo: Path, branch: str) -> str:
        calls.append((repo, branch))
        return env["first"]

    monkeypatch.setattr(brief_mod.worktree, "current_base_ref", fake_current)

    result = _invoke("24ad5f91")

    assert result.exit_code == 0, result.output
    assert calls == [(env["repo"], "main")]
    assert env["base_sha"] == env["first"]


def test_a_brief_file_replaces_the_description(
    env: dict[str, Any], tmp_path: Path
) -> None:
    brief = tmp_path / "w8-brief.md"
    brief.write_text("The brief as written on 2026-10-01.\n")

    result = _invoke("24ad5f91", "--base", "HEAD", "--brief-file", str(brief))

    assert result.exit_code == 0, result.output
    assert env["inputs"].brief == "The brief as written on 2026-10-01.\n"


def test_delta_from_runs_a_recheck_between_two_resolved_bases(
    env: dict[str, Any],
) -> None:
    result = _invoke("24ad5f91", "--base", "HEAD", "--delta-from", "HEAD~1")

    assert result.exit_code == 0, result.output
    assert env["mode"] == MODE_DELTA
    assert env["prior_base"] == env["first"]
    assert env["base_sha"] == env["head"]
    assert _ANSI.sub("", result.stdout).startswith("**Recheck against")


def test_an_unknown_ref_is_refused_before_any_review(env: dict[str, Any]) -> None:
    result = _invoke("24ad5f91", "--base", "no-such-ref")

    assert result.exit_code == 2
    assert "no-such-ref" in _plain(result.output)
    assert "config" not in env  # the pass never ran


def test_a_story_whose_project_is_not_mapped_is_refused(
    env: dict[str, Any],
) -> None:
    env["story"].metadata = {"project": "elsewhere"}

    result = _invoke("24ad5f91", "--base", "HEAD")

    assert result.exit_code == 2
    assert "[projects.elsewhere]" in _plain(result.output)
    assert "config" not in env


def test_a_degraded_review_exits_1_with_its_note_and_the_raw_text(
    env: dict[str, Any],
) -> None:
    env["result"] = BriefReviewResult(
        addendum=None,
        base_sha="x" * 40,
        mode=MODE_FULL,
        note=(
            "the reviewer wrote a draft that did not validate: D1 has no `Basis:` line"
        ),
        raw="## Decisions\n\n- **D1. No basis.** do it\n",
    )

    result = _invoke("24ad5f91", "--base", "HEAD")

    assert result.exit_code == 1
    assert "did not validate" in _plain(result.stderr)
    assert "- **D1. No basis.** do it" in result.stdout

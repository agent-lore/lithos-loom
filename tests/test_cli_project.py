"""Tests for ``lithos-loom project list`` (Slice 3 / US31 / D23 + D30).

Two layers:

1. Pure-function tests for ``_merge_lithos_with_toml`` and
   ``_rows_from_toml`` — no I/O, fast, locks the join semantic.
2. CLI integration tests via ``CliRunner``. Lithos-source tests
   stub ``LithosClient`` in the ``cli/project`` module namespace so
   no real HTTP round trip happens. ``--source toml`` tests don't
   need the stub — they bypass Lithos entirely.

The JSON output shape is invariant across both sources (an array of
slug strings, alphabetical) so the capture macro's existing
``JSON.parse(project list --format json)`` consumer keeps working
regardless of which source the operator chose.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom.cli import project as project_cli
from lithos_loom.cli.project import (
    _merge_lithos_with_toml,
    _ProjectRow,
    _rows_from_toml,
)
from lithos_loom.config import ProjectConfig, load_config
from lithos_loom.lithos_client import NoteSummary
from lithos_loom.main import app

runner = CliRunner()


def _write_config(tmp_path: Path, projects_toml: str) -> Path:
    """Write a minimal config with the given [projects.*] block."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            {projects_toml}
            """
        )
    )
    return config_path


def _summary(
    *,
    id_: str = "doc-1",
    slug: str = "lithos-loom",
    status: str | None = "active",
    path: str | None = None,
    tags: tuple[str, ...] = ("project-context",),
) -> NoteSummary:
    return NoteSummary(
        id=id_,
        title=slug,
        version=1,
        updated_at=datetime(2026, 5, 24, 14, 30, tzinfo=UTC),
        tags=tags,
        status=status,
        note_type="concept",
        path=path or f"projects/{slug}/context.md",
        slug=slug,
    )


# ── Pure: _merge_lithos_with_toml ──────────────────────────────────────


def test_merge_marks_local_when_toml_has_slug() -> None:
    """A Lithos doc whose slug matches a TOML entry shows
    ``local=True`` with the repo path populated."""
    summaries = [_summary(slug="lithos-loom")]
    toml = {"lithos-loom": ProjectConfig(name="lithos-loom", repo=Path("/repo/loom"))}
    rows = _merge_lithos_with_toml(summaries, toml)
    assert rows == [
        _ProjectRow(slug="lithos-loom", status="active", local=True, repo="/repo/loom")
    ]


def test_merge_marks_not_local_when_toml_lacks_slug() -> None:
    """A Lithos doc whose slug isn't in TOML shows ``local=False``
    and ``repo=None`` — the operator can still see the project
    exists but no automation is configured for it on this host."""
    summaries = [_summary(slug="influx")]
    toml: dict[str, ProjectConfig] = {}
    rows = _merge_lithos_with_toml(summaries, toml)
    assert rows == [_ProjectRow(slug="influx", status="active", local=False, repo=None)]


def test_merge_collapses_multiple_docs_per_slug() -> None:
    """A project with both context.md and architecture.md under the
    same slug → one row (not two). The slug is the unit of operator
    interest here; per-doc visibility is a separate concern."""
    summaries = [
        _summary(id_="doc-1", slug="loom", path="projects/loom/context.md"),
        _summary(id_="doc-2", slug="loom", path="projects/loom/architecture.md"),
    ]
    rows = _merge_lithos_with_toml(summaries, {})
    assert len(rows) == 1
    assert rows[0].slug == "loom"


def test_merge_status_comes_from_canonical_doc_regardless_of_order() -> None:
    """Reviewer-finding regression: when a slug has multiple docs,
    the displayed status MUST reflect
    ``projects/<slug>/<slug>-project-context.md`` (the prod-convention
    canonical project registry entry), not the first doc in Lithos's
    response. Without this, the status flipped between ``active`` and
    ``archived`` depending on Lithos's response order.

    The picker convention matches what real prod docs use today
    (e.g. ``projects/lithos-loom/lithos-loom-project-context.md``);
    see the design discussion in
    ``examples/slice-4-test/MANUAL_TEST.md`` ("Open design question").

    Both orderings exercised so the test fails if the picker reverts
    to "first wins": the supplementary architecture doc is archived,
    the canonical context doc is active, the operator must see
    ``active``."""
    canonical_doc = NoteSummary(
        id="doc-context",
        title="loom project context",
        version=2,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path="projects/loom/loom-project-context.md",
        slug="loom",
    )
    architecture_doc = NoteSummary(
        id="doc-arch",
        title="loom",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="archived",
        note_type="concept",
        path="projects/loom/architecture.md",
        slug="loom",
    )

    # Order 1: architecture first (would mis-pick "archived" under
    # the previous first-wins rule, AND under the lex-min fallback
    # since ``architecture.md`` < ``loom-project-context.md``).
    rows = _merge_lithos_with_toml([architecture_doc, canonical_doc], {})
    assert rows[0].status == "active"

    # Order 2: canonical first (would coincidentally pick "active"
    # under first-wins; the test still pins the rule).
    rows = _merge_lithos_with_toml([canonical_doc, architecture_doc], {})
    assert rows[0].status == "active"


def test_merge_falls_back_to_lex_min_path_when_no_canonical_doc() -> None:
    """When no ``<slug>-project-context.md`` exists for the slug
    (operator structured the project with only supplementary docs,
    or it's a test fixture), the picker falls back to the
    lexicographically-smallest path so the choice is deterministic
    regardless of Lithos's response order."""
    arch = NoteSummary(
        id="doc-arch",
        title="loom",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path="projects/loom/architecture.md",
        slug="loom",
    )
    roadmap = NoteSummary(
        id="doc-roadmap",
        title="loom",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="archived",
        note_type="concept",
        path="projects/loom/roadmap.md",
        slug="loom",
    )

    # ``architecture.md`` < ``roadmap.md`` lexicographically →
    # architecture's status wins both ways.
    rows = _merge_lithos_with_toml([roadmap, arch], {})
    assert rows[0].status == "active"
    rows = _merge_lithos_with_toml([arch, roadmap], {})
    assert rows[0].status == "active"


def test_merge_prefers_canonical_over_lex_min_when_both_present() -> None:
    """Sanity: the canonical-name preference must beat the lex-min
    fallback. Architecture sorts lex-min over the canonical doc
    (``architecture.md`` < ``loom-project-context.md``); the picker
    must still pick the canonical doc by name, not the lex-min."""
    canonical = NoteSummary(
        id="doc-context",
        title="loom project context",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="active",
        note_type="concept",
        path="projects/loom/loom-project-context.md",
        slug="loom",
    )
    architecture = NoteSummary(
        id="doc-arch",
        title="loom",
        version=1,
        updated_at=datetime(2026, 5, 24, tzinfo=UTC),
        tags=("project-context",),
        status="archived",
        note_type="concept",
        path="projects/loom/architecture.md",  # lex-min over the canonical
        slug="loom",
    )

    rows = _merge_lithos_with_toml([architecture, canonical], {})
    assert rows[0].status == "active"
    rows = _merge_lithos_with_toml([canonical, architecture], {})
    assert rows[0].status == "active"


def test_merge_drops_empty_slug_entries() -> None:
    """Docs whose path didn't parse to a slug (degenerate paths)
    are dropped — there's nothing for the operator to act on."""
    summaries = [
        _summary(slug="", path="something-weird"),
        _summary(slug="loom", path="projects/loom/context.md"),
    ]
    rows = _merge_lithos_with_toml(summaries, {})
    assert [r.slug for r in rows] == ["loom"]


def test_merge_orders_rows_alphabetically_by_slug() -> None:
    """Stable ordering for human reading + scripted consumers."""
    summaries = [
        _summary(slug="zeta", path="projects/zeta/context.md"),
        _summary(slug="alpha", path="projects/alpha/context.md"),
        _summary(slug="middle", path="projects/middle/context.md"),
    ]
    rows = _merge_lithos_with_toml(summaries, {})
    assert [r.slug for r in rows] == ["alpha", "middle", "zeta"]


# ── Pure: _rows_from_toml ──────────────────────────────────────────────


def test_rows_from_toml_uses_repo_as_local_path(tmp_path: Path) -> None:
    """``--source toml`` rows carry ``status=None`` (can't tell
    without Lithos) and the repo path from the TOML stanza."""
    cfg = load_config(
        _write_config(
            tmp_path,
            f'[projects.alpha]\nrepo = "{tmp_path / "alpha"}"',
        )
    )
    rows = _rows_from_toml(cfg)
    assert rows == [
        _ProjectRow(
            slug="alpha",
            status=None,
            local=True,
            repo=str(tmp_path / "alpha"),
        )
    ]


def test_rows_from_toml_empty_projects_returns_empty_list(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path, "# no projects"))
    rows = _rows_from_toml(cfg)
    assert rows == []


# ── CLI integration: --source toml (offline path) ──────────────────────


def test_project_list_toml_source_json(tmp_path: Path) -> None:
    """``--source toml --format json`` returns the alphabetised TOML
    slug array — same shape capture-macro consumes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_config(
        tmp_path,
        f'[projects.zeta]\nrepo = "{repo}"\n\n[projects.alpha]\nrepo = "{repo}"\n',
    )

    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == ["alpha", "zeta"]


def test_project_list_toml_source_text(tmp_path: Path) -> None:
    """``--source toml --format text`` renders the three-column
    table — same shape as the Lithos source, status column is ``—``
    because we don't query Lithos."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_config(tmp_path, f'[projects.alpha]\nrepo = "{repo}"\n')
    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
        ],
    )
    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    # Header + one data row.
    assert lines[0].startswith("slug")
    assert "alpha" in lines[1]
    assert str(repo) in lines[1]
    # Status column is em-dash because TOML source doesn't know.
    assert "—" in lines[1]


def test_project_list_toml_source_empty_text(tmp_path: Path) -> None:
    """Empty TOML + ``--source toml`` → empty stdout (no header).
    Same shape as Lithos-empty for scripted callers."""
    config_path = _write_config(tmp_path, "# no projects")
    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
        ],
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_project_list_toml_source_empty_json(tmp_path: Path) -> None:
    """Empty TOML + ``--source toml --format json`` → empty array.
    The macro relies on this exact shape to detect 'no projects'."""
    config_path = _write_config(tmp_path, "# no projects")
    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == []


# ── CLI integration: --source lithos (default) ─────────────────────────


class _StubLithosClient:
    """Async-context-manager stand-in for ``LithosClient``. Records
    ``note_list`` invocations and returns a scripted response."""

    def __init__(
        self,
        *args: Any,
        responses: list[list[NoteSummary]] | None = None,
        **kwargs: Any,
    ) -> None:
        # Class-level scripted response so we can monkeypatch the
        # symbol in cli.project and have the inner ``async with``
        # construct the right stub. See ``_install_lithos_stub``
        # for the wrapper.
        self._args = args
        self._kwargs = kwargs

    async def __aenter__(self) -> _StubLithosClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def note_list(
        self,
        *,
        path_prefix: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
    ) -> list[NoteSummary]:
        return list(_stub_state["response"])


_stub_state: dict[str, Any] = {"response": []}


@pytest.fixture
def lithos_stub(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``LithosClient`` in cli.project with our stub. Tests
    set ``state['response']`` to control what ``note_list`` returns."""
    _stub_state["response"] = []
    monkeypatch.setattr(project_cli, "LithosClient", _StubLithosClient)
    return _stub_state


def test_project_list_lithos_source_default_text(
    tmp_path: Path, lithos_stub: dict[str, Any]
) -> None:
    """Default ``--source lithos --format text``: three-column
    table showing slug + Lithos status + local-overlay marker."""
    repo = tmp_path / "loom"
    repo.mkdir()
    config_path = _write_config(tmp_path, f'[projects.lithos-loom]\nrepo = "{repo}"\n')
    lithos_stub["response"] = [
        _summary(slug="lithos-loom"),
        _summary(slug="influx", id_="doc-2", path="projects/influx/context.md"),
    ]

    result = runner.invoke(app, ["project", "list", "--config", str(config_path)])
    assert result.exit_code == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("slug")
    # influx comes first alphabetically; no TOML entry → ✗.
    assert "influx" in lines[1]
    assert "✗" in lines[1]
    # lithos-loom has a TOML entry → ✓ + repo path.
    assert "lithos-loom" in lines[2]
    assert str(repo) in lines[2]
    assert "✓" in lines[2]


def test_project_list_lithos_source_json_is_slug_array(
    tmp_path: Path, lithos_stub: dict[str, Any]
) -> None:
    """JSON shape is stable across sources: alphabetised array of
    slug strings. The capture macro depends on this exact shape."""
    config_path = _write_config(tmp_path, "# no toml projects needed")
    lithos_stub["response"] = [
        _summary(slug="zeta", path="projects/zeta/context.md"),
        _summary(slug="alpha", path="projects/alpha/context.md"),
    ]

    result = runner.invoke(
        app,
        ["project", "list", "--config", str(config_path), "--format", "json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == ["alpha", "zeta"]


def test_project_list_lithos_source_passes_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Lithos query MUST be ``path_prefix='projects/'`` AND
    ``tags=['project-context']`` — anything broader would list
    non-project-context docs in the operator's project enumeration."""
    config_path = _write_config(tmp_path, "")
    captured: dict[str, Any] = {}

    class _CapturingClient(_StubLithosClient):
        async def note_list(
            self,
            *,
            path_prefix: str | None = None,
            tags: list[str] | None = None,
            limit: int = 100,
        ) -> list[NoteSummary]:
            captured["path_prefix"] = path_prefix
            captured["tags"] = tags
            return []

    monkeypatch.setattr(project_cli, "LithosClient", _CapturingClient)
    result = runner.invoke(app, ["project", "list", "--config", str(config_path)])
    assert result.exit_code == 0
    assert captured["path_prefix"] == "projects/"
    assert captured["tags"] == ["project-context"]


def test_project_list_lithos_source_empty_text(
    tmp_path: Path, lithos_stub: dict[str, Any]
) -> None:
    """Empty Lithos result + text format → empty stdout."""
    config_path = _write_config(tmp_path, "")
    lithos_stub["response"] = []
    result = runner.invoke(app, ["project", "list", "--config", str(config_path)])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_project_list_lithos_source_empty_json(
    tmp_path: Path, lithos_stub: dict[str, Any]
) -> None:
    """Empty Lithos result + json format → empty array."""
    config_path = _write_config(tmp_path, "")
    lithos_stub["response"] = []
    result = runner.invoke(
        app,
        ["project", "list", "--config", str(config_path), "--format", "json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == []


# ── CLI integration: error paths ────────────────────────────────────────


def test_project_list_unknown_format(tmp_path: Path) -> None:
    """Unknown ``--format`` exits 2 with a clear message."""
    config_path = _write_config(tmp_path, "")
    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "toml",
            "--format",
            "yaml",
        ],
    )
    assert result.exit_code == 2
    assert "yaml" in result.stderr


def test_project_list_unknown_source(tmp_path: Path) -> None:
    """Unknown ``--source`` exits 2 with a clear message."""
    config_path = _write_config(tmp_path, "")
    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(config_path),
            "--source",
            "elsewhere",
        ],
    )
    assert result.exit_code == 2
    assert "elsewhere" in result.stderr


def test_project_list_missing_config(tmp_path: Path) -> None:
    """A bogus config path exits non-zero with a clear message."""
    result = runner.invoke(
        app,
        [
            "project",
            "list",
            "--config",
            str(tmp_path / "nope.toml"),
            "--source",
            "toml",
        ],
    )
    assert result.exit_code != 0


def test_project_list_uses_env_var_when_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lithos_stub: dict[str, Any]
) -> None:
    """``LITHOS_LOOM_CONFIG`` is the env-var seam; CLI honors it
    when ``--config`` is omitted. Pinned via the Lithos source
    (default) so the test exercises the env path through the
    actual default code path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = _write_config(tmp_path, f'[projects.demo]\nrepo = "{repo}"\n')
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(config_path))
    lithos_stub["response"] = [_summary(slug="demo", path="projects/demo/context.md")]

    result = runner.invoke(app, ["project", "list", "--format", "json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout.strip()) == ["demo"]


def test_project_list_lithos_unreachable_exits_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport failure surfaces with a hint to try ``--source toml``
    — operators on flaky networks shouldn't have to dig for the fallback."""
    config_path = _write_config(tmp_path, "")

    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            raise OSError("simulated network blip")

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(project_cli, "LithosClient", _FailingClient)
    result = runner.invoke(app, ["project", "list", "--config", str(config_path)])
    assert result.exit_code == 1
    assert "--source toml" in result.stderr

"""Smoke tests for the Typer CLI dispatcher."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from typer.testing import CliRunner

from lithos_loom import main as main_module
from lithos_loom.errors import LithosClientError
from lithos_loom.lithos_client import Task
from lithos_loom.main import app
from tests.support import FakeLithosClient

runner = CliRunner()


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("run", "doctor", "validate-config", "config"):
        assert sub in result.stdout


def test_validate_config_succeeds(loom_config_env: Path) -> None:
    result = runner.invoke(app, ["validate-config"])
    assert result.exit_code == 0
    assert "lithos-orchestrator-test" in result.stdout
    assert "prd-decompose" in result.stdout


def test_validate_config_fails_clearly_when_missing(tmp_path: Path) -> None:
    """A bogus config path must exit non-zero with a useful message."""
    result = runner.invoke(
        app, ["validate-config", "--config", str(tmp_path / "nope.toml")]
    )
    assert result.exit_code != 0


# ── validate-config --dry-run ──────────────────────────────────────────


def _task(
    id_: str,
    *,
    tags: tuple[str, ...] = (),
    status: str = "open",
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Task:
    return Task(
        id=id_,
        title=title or f"Task {id_}",
        status=status,
        tags=tags,
        metadata=metadata or {},
        claims=(),
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake: FakeLithosClient) -> None:
    """Patch the LithosClient symbol the CLI imports with a factory."""

    def factory(*args: object, **kwargs: object) -> FakeLithosClient:
        return fake

    monkeypatch.setattr(main_module, "LithosClient", factory)


def test_dry_run_lists_matched_routes_per_task(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task whose tags match a configured route is reported as 'would fire'."""
    fake = FakeLithosClient(
        tasks=[_task("abc123", tags=("trigger:prd-decompose",), title="Decompose me")]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "abc123" in result.output
    assert "route:prd-decompose" in result.output
    assert fake.calls_to("task_list") == [
        {"status": "open", "with_claims": True, "resolved_since": None}
    ]


def test_dry_run_flags_orphan_tasks(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open task with no matching route or subscription is listed as orphan."""
    fake = FakeLithosClient(
        tasks=[_task("orph-1", tags=("unrouted",), title="Nobody wants me")]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "orph-1" in result.output
    assert "orphan" in result.output.lower()


def test_dry_run_flags_dead_routes(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured route that matches no open task is flagged as dead config."""
    fake = FakeLithosClient(tasks=[])  # no open tasks → every route is dead
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dead" in result.output.lower()
    assert "prd-decompose" in result.output


def test_dry_run_does_not_call_mutating_lithos_methods(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run is non-mutating: no claim, complete, update, release, finding_post."""
    fake = FakeLithosClient(tasks=[_task("abc123", tags=("trigger:prd-decompose",))])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    forbidden = {
        "task_claim",
        "task_release",
        "task_renew",
        "task_complete",
        "task_update",
        "finding_post",
    }
    assert not (set(fake.mutating_calls) & forbidden), fake.mutating_calls


def test_dry_run_rejects_unknown_subscription_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelled subscription action fails the dry-run loudly.

    The dry-run validates each config action against SUBSCRIPTION_ACTIONS
    (via build_runners' handler-map check), so a typo like
    ``obsidian-projction`` surfaces as an unknown handler + non-zero exit
    instead of a silently inert subscription (ARCH-6)."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            f"""
            [orchestrator]
            agent_id = "typo-test"
            lithos_url = "http://localhost:8765"
            work_dir = "{tmp_path / "work"}"
            max_concurrency = 2
            log_level = "info"

            [[subscriptions]]
            name = "typo-sub"
            on = ["lithos.task.created"]
            action = "obsidian-projction"
            """
        )
    )
    _patch_client(monkeypatch, FakeLithosClient(tasks=[]))

    result = runner.invoke(
        app, ["validate-config", "--dry-run", "--config", str(config_path)]
    )

    assert result.exit_code == 1, result.output
    assert "unknown handler" in result.stderr
    assert "obsidian-projction" in result.stderr


def test_dry_run_clear_error_when_lithos_unreachable(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Lithos session cannot be opened, fail with a clear message that
    points the operator at ``lithos-loom doctor`` for follow-up.
    """

    _patch_client(
        monkeypatch, FakeLithosClient(fail_connect=OSError("connection refused"))
    )

    result = runner.invoke(app, ["validate-config", "--dry-run"])
    assert result.exit_code != 0
    assert "doctor" in result.output.lower() or "doctor" in (
        result.stderr if result.stderr else ""
    )


def test_dry_run_matches_subscription_with_where_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscriptions with a where expression are evaluated during dry-run.

    Pins that the dry-run uses the same matcher machinery the bus uses at
    runtime, so the table reflects what would actually fire.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [[subscriptions]]
            name = "high-priority-only"
            on = "lithos.task.created"
            action = "noop"
            where = "task.get('title') == 'urgent'"
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    fake = FakeLithosClient(
        tasks=[
            _task("hi", title="urgent"),
            _task("lo", title="meh"),
        ]
    )
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    # The where predicate fires for "hi" but not for "lo".
    lines = result.output.splitlines()
    high_lines = [
        line for line in lines if "hi" in line and "high-priority-only" in line
    ]
    low_lines = [
        line for line in lines if "lo" in line and "high-priority-only" in line
    ]
    assert any("would fire" in line.lower() or "✓" in line for line in high_lines)
    # "lo" appears in the orphan list, but should NOT show a "would fire"
    # against the where-gated subscription.
    for line in low_lines:
        assert "would fire" not in line.lower() and "✓" not in line


def test_dry_run_subscription_with_updated_event_type_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscription with on='lithos.task.updated' must show as 'would fire'
    when its filter matches the task — the dry-run must test the sub
    against every type in its on-list, not hard-code lithos.task.created.
    """
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        dedent(
            """
            [orchestrator]
            agent_id = "lithos-orchestrator-test"
            lithos_url = "http://localhost:8765"

            [[subscriptions]]
            name = "updated-only"
            on = "lithos.task.updated"
            action = "noop"
            match.tags = ["any-tag"]
            """
        )
    )
    monkeypatch.setenv("LITHOS_LOOM_CONFIG", str(cfg_path))
    fake = FakeLithosClient(tasks=[_task("t1", tags=("any-tag",))])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    sub_lines = [
        line
        for line in result.output.splitlines()
        if "subscription:updated-only" in line
    ]
    assert sub_lines, result.output
    assert any("would fire" in line.lower() or "✓" in line for line in sub_lines)


def _route_row(output: str, task_id: str, route: str = "prd-decompose") -> str:
    """The route row printed under ``task_id``'s heading."""
    lines = output.splitlines()
    idx = next(i for i, line in enumerate(lines) if line.startswith(task_id))
    return next(line for line in lines[idx + 1 : idx + 5] if f"route:{route}" in line)


def test_dry_run_route_deferred_when_task_is_blocked(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US7: a tag-matching open task Lithos does not consider ready must NOT
    be reported as 'would fire'. Readiness comes from lithos_task_ready — the
    dry-run no longer mirrors the scheduler client-side (US5)."""
    fake = FakeLithosClient(
        tasks=[
            _task("blocked", tags=("trigger:prd-decompose",)),
            _task("upstream", status="open"),
        ],
    )
    fake.add_edge(from_task_id="upstream", to_task_id="blocked", type="blocks")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    row = _route_row(result.output, "blocked")
    assert "✓" not in row, row
    assert "deferred" in row.lower(), row
    # The dry-run asked Lithos rather than resolving deps itself.
    assert fake.called("task_ready")
    assert not fake.called("task_get")


def test_dry_run_shows_structured_blocker_reason(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US7: the operator sees WHICH predecessor holds the task, and of what
    kind — not a flat 'deps not complete' string."""
    fake = FakeLithosClient(
        tasks=[
            _task("blocked", tags=("trigger:prd-decompose",)),
            _task("upstream", status="open"),
        ],
    )
    fake.add_edge(from_task_id="upstream", to_task_id="blocked", type="blocks")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    # kind + which predecessor + its status — not a flat "deps not complete".
    assert "deferred (task: upstream (open))" in _route_row(result.output, "blocked")


def test_dry_run_cancelled_blocker_reported_as_unsatisfiable(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The epic-G precondition, surfaced to the operator: a CANCELLED blocker
    keeps its dependent blocked, and the reason says it needs intervention
    rather than looking like an ordinary wait."""
    fake = FakeLithosClient(
        tasks=[
            _task("blocked", tags=("trigger:prd-decompose",)),
            _task("upstream", status="cancelled"),
        ],
    )
    fake.add_edge(from_task_id="upstream", to_task_id="blocked", type="blocks")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    row = _route_row(result.output, "blocked")
    assert "✓" not in row, row
    # Distinct from an ordinary wait: this one needs intervention.
    assert "deferred (blocker_unsatisfiable: upstream (cancelled))" in row, row


def test_dry_run_route_fires_when_task_is_ready(
    loom_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate doesn't over-correct: an unblocked task still fires."""
    fake = FakeLithosClient(tasks=[_task("ready", tags=("trigger:prd-decompose",))])
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["validate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "✓" in _route_row(result.output, "ready")


# Sentinel that keeps LithosClientError importable in this module so tests
# referencing it don't lose to import pruning even when the symbol isn't
# actively used.
_ = LithosClientError


# ── doctor CLI integration (US15) ──────────────────────────────────────


def _write_doctor_config(
    tmp_path: Path,
    *,
    vault_path: Path | None,
) -> Path:
    """Write a minimal config.toml; include [obsidian_sync] only when
    ``vault_path`` is provided."""
    config_path = tmp_path / "config.toml"
    parts = [
        "[orchestrator]",
        'agent_id = "lithos-orchestrator-test"',
        'lithos_url = "http://localhost:8765"',
        f'work_dir = "{tmp_path / "work"}"',
        "max_concurrency = 2",
        "",
    ]
    if vault_path is not None:
        parts.extend(
            [
                "[obsidian_sync]",
                f'vault_path = "{vault_path}"',
                'tasks_file = "_lithos/tasks.md"',
                "",
            ]
        )
    config_path.write_text("\n".join(parts))
    return config_path


def test_doctor_succeeds_on_healthy_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three vault checks + the task-graph probe pass."""
    vault = tmp_path / "vault"
    vault.mkdir()
    config = _write_doctor_config(tmp_path, vault_path=vault)
    _patch_client(monkeypatch, FakeLithosClient())

    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "vault_path_exists" in result.output
    assert "lithos_subdir_creatable" in result.output
    assert "probe_write_read_roundtrip" in result.output
    assert "task_graph_extension" in result.output
    assert "OK: 4 passed, 0 failed" in result.output
    # Probe file cleaned up.
    assert not (vault / "_lithos" / ".doctor-probe.tmp").exists()


def test_doctor_fails_with_exit_1_on_missing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """vault_path pointing at a nonexistent dir → ✗ + FAIL + exit 1
    (task-graph probe still passes against the fake)."""
    missing_vault = tmp_path / "no-such-vault"
    config = _write_doctor_config(tmp_path, vault_path=missing_vault)
    _patch_client(monkeypatch, FakeLithosClient())

    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 1, result.output
    assert "vault_path_exists" in result.output
    assert "does not exist" in result.output
    assert "FAIL: 1 passed, 1 failed" in result.output


def test_doctor_skips_vault_probes_when_no_obsidian_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No [obsidian_sync] → skip vault; the task-graph probe still runs."""
    config = _write_doctor_config(tmp_path, vault_path=None)
    _patch_client(monkeypatch, FakeLithosClient())

    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert "vault probe skipped" in result.output
    assert "OK: 1 passed, 0 failed" in result.output


def test_doctor_skips_project_probe_when_no_projects_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 4 replaced the US-35 placeholder with the actual project
    probe. When ``[projects]`` is empty the probe is skipped cleanly
    (no Lithos round-trip, no failure) — the operator sees a clear
    ⊘ line rather than a fail-cascade."""
    config = _write_doctor_config(tmp_path, vault_path=None)
    _patch_client(monkeypatch, FakeLithosClient())
    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert "project probe skipped" in result.output
    assert "[projects] table is empty" in result.output


def test_doctor_fails_with_exit_nonzero_on_missing_config(tmp_path: Path) -> None:
    """Bogus config path → exit ≠ 0 via _load_or_exit (matches
    validate-config behavior)."""
    result = runner.invoke(
        app, ["doctor", "--config", str(tmp_path / "no-such-config.toml")]
    )
    assert result.exit_code != 0


# ── doctor task-graph probe + run boot gate (Epic G US1) ───────────────


def test_doctor_fails_when_task_graph_extension_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Lithos that errors on the graph tools fails the doctor run (exit 1)."""
    config = _write_doctor_config(tmp_path, vault_path=None)
    fake = FakeLithosClient()
    fake.raise_on["task_ready"] = LithosClientError("unknown_tool", "no such tool")
    _patch_client(monkeypatch, fake)

    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 1, result.output
    assert "task_graph_extension" in result.output
    assert "FAIL" in result.output


def test_doctor_reports_lithos_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connect failure surfaces as lithos_unreachable, not a crash."""
    config = _write_doctor_config(tmp_path, vault_path=None)
    _patch_client(monkeypatch, FakeLithosClient(fail_connect=OSError("refused")))

    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 1, result.output
    assert "lithos_unreachable" in result.output


def _stub_supervisor(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace Supervisor with a stub recording construction; ``run()`` → 0.

    Lets the boot-gate tests assert whether ``run`` reached the supervisor
    without spawning real child processes."""
    constructed: list = []

    class _StubSupervisor:
        def __init__(self, *args: object, **kwargs: object) -> None:
            constructed.append((args, kwargs))

        async def run(self) -> int:
            return 0

    monkeypatch.setattr(main_module, "Supervisor", _StubSupervisor)
    return constructed


def test_run_refuses_to_boot_without_task_graph_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_doctor_config(tmp_path, vault_path=None)
    fake = FakeLithosClient()
    fake.raise_on["task_ready"] = LithosClientError("unknown_tool", "no such tool")
    _patch_client(monkeypatch, fake)
    constructed = _stub_supervisor(monkeypatch)

    result = runner.invoke(app, ["run", "--config", str(config)])
    assert result.exit_code == 1, result.output
    assert "refusing to start" in result.output
    assert constructed == []  # never reached the supervisor


def test_run_refuses_to_boot_when_lithos_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_doctor_config(tmp_path, vault_path=None)
    # Model the real connect failure: LithosClient.__aenter__ re-raises the
    # anyio-wrapped ExceptionGroup (not a bare OSError), so the boot gate must
    # catch that too rather than crash with an unhandled exception.
    boom = ExceptionGroup("connect failed", [ConnectionError("refused")])
    _patch_client(monkeypatch, FakeLithosClient(fail_connect=boom))
    constructed = _stub_supervisor(monkeypatch)

    result = runner.invoke(app, ["run", "--config", str(config)])
    assert result.exit_code == 1, result.output
    assert "lithos_unreachable" in result.output
    assert constructed == []


def test_run_boots_when_task_graph_extension_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_doctor_config(tmp_path, vault_path=None)
    _patch_client(monkeypatch, FakeLithosClient())
    constructed = _stub_supervisor(monkeypatch)

    result = runner.invoke(app, ["run", "--config", str(config)])
    assert result.exit_code == 0, result.output
    assert len(constructed) == 1  # boot gate passed → supervisor started

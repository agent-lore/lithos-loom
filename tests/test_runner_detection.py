"""Tests for test-command auto-detection (salvaged from Ralph++, adapted).

Divergence from Ralph++ under test: candidates are returned as an ordered list
with fallbacks (Makefile target first, then one language-ecosystem command), so
the gate can fall back when the container image lacks a tool.
"""

from __future__ import annotations

from lithos_loom.runner.detection import (
    detect_ecosystems,
    detect_test_commands,
    is_uv_managed,
)


def test_makefile(tmp_path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\techo hi\n\ntest:\n\tpytest\n")
    assert detect_test_commands(tmp_path) == ["make test"]


def test_makefile_test_target_first_line(tmp_path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    assert detect_test_commands(tmp_path) == ["make test"]


def test_makefile_without_test_target(tmp_path) -> None:
    (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
    assert detect_test_commands(tmp_path) == []


def test_pyproject_pytest(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_pyproject_pytest_with_uv_lock(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "uv.lock").write_text("")
    assert detect_test_commands(tmp_path) == ["uv run pytest"]


def test_is_uv_managed_true_with_uv_lock(tmp_path) -> None:
    # A repo is uv-managed iff it has a uv.lock — the same signal the test check
    # already uses (`uv run pytest`), now reusable for every env-dependent check (#165).
    (tmp_path / "uv.lock").write_text("")
    assert is_uv_managed(tmp_path) is True


def test_is_uv_managed_false_without_uv_lock(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert is_uv_managed(tmp_path) is False


def test_pytest_ini(tmp_path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_setup_cfg(tmp_path) -> None:
    (tmp_path / "setup.cfg").write_text("[tool:pytest]\n")
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_pyproject_without_pytest_section(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_test_commands(tmp_path) == []


def test_node(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"name": "test"}')
    assert detect_test_commands(tmp_path) == ["npm test"]


def test_rust(tmp_path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_commands(tmp_path) == ["cargo test"]


def test_go(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    assert detect_test_commands(tmp_path) == ["go test ./..."]


def test_empty_dir(tmp_path) -> None:
    assert detect_test_commands(tmp_path) == []


def test_makefile_first_with_language_fallback(tmp_path) -> None:
    """Makefile target leads, but the ecosystem command follows as a fallback
    for images without ``make`` (divergence from Ralph++'s first-match-only)."""
    (tmp_path / "Makefile").write_text("test:\n\tpytest && ruff check .\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "uv.lock").write_text("")
    assert detect_test_commands(tmp_path) == ["make test", "uv run pytest"]


def test_polyglot_first_ecosystem_only(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "package.json").write_text('{"name": "test"}')
    assert detect_test_commands(tmp_path) == ["pytest"]


def test_polyglot_node_and_rust(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"name": "test"}')
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_commands(tmp_path) == ["npm test"]


# --- detect_ecosystems (#133): polyglot-aware ecosystem markers ----------------


def test_detect_ecosystems_python_from_pyproject(tmp_path) -> None:
    # Unlike detect_test_commands, ecosystem detection needs no [tool.pytest] —
    # a bare pyproject.toml makes the repo Python.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert detect_ecosystems(tmp_path) == ("python",)


def test_detect_ecosystems_python_from_setup_cfg_and_pytest_ini(tmp_path) -> None:
    (tmp_path / "setup.cfg").write_text("[metadata]\n")
    assert detect_ecosystems(tmp_path) == ("python",)
    (tmp_path / "setup.cfg").unlink()
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert detect_ecosystems(tmp_path) == ("python",)


def test_detect_ecosystems_node_rust_go(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"name": "x"}')
    assert detect_ecosystems(tmp_path) == ("node",)
    (tmp_path / "package.json").unlink()
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_ecosystems(tmp_path) == ("rust",)
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    assert detect_ecosystems(tmp_path) == ("go",)


def test_detect_ecosystems_polyglot_in_fixed_order(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    # Fixed precedence python, node, rust, go — independent of creation order.
    assert detect_ecosystems(tmp_path) == ("python", "node", "go")


def test_detect_ecosystems_markerless_is_empty(tmp_path) -> None:
    assert detect_ecosystems(tmp_path) == ()

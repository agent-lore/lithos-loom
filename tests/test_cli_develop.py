"""Tests for ``lithos-loom develop`` (issue #88).

The filesystem layer (run discovery / round + reviewer parsing / resolution) is
pure and tested directly. The docker layer goes through the ``_docker`` seam,
monkeypatched with canned ``docker ps`` / ``docker top`` output — including a
codex process, the salvage fix the bash prototype missed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.cli import develop


def _make_run(
    work_dir: Path,
    *,
    task_id: str = "t-1",
    run_id: str = "abc123",
    title: str = "Do the thing",
    rounds: dict[int, list[str]] | None = None,
    conversation: str | None = None,
) -> Path:
    run_dir = work_dir / task_id / run_id
    (run_dir / "handoff").mkdir(parents=True)
    (work_dir / task_id / "task.json").write_text(
        json.dumps({"task": {"id": task_id, "title": title}})
    )
    for rnd, reviewers in (rounds or {}).items():
        hd = run_dir / "handoff"
        (hd / f"round_{rnd:02d}_coder_done.md").write_text(
            f"## Status: LGTM\nround {rnd} coder"
        )
        for rv in reviewers:
            (hd / f"round_{rnd:02d}_review_{rv}.md").write_text(
                f"## Status: LGTM\n{rv} round {rnd}"
            )
    if conversation is not None:
        (run_dir / "conversation.md").write_text(conversation)
    return run_dir


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Monkeypatch load_config → a fake cfg rooted at tmp_path; docker absent."""
    monkeypatch.setattr(
        develop,
        "load_config",
        lambda config=None: SimpleNamespace(
            orchestrator=SimpleNamespace(work_dir=tmp_path)
        ),
    )
    monkeypatch.setattr(develop, "_docker", lambda args: None)  # docker absent
    return tmp_path


# ── filesystem layer (pure) ────────────────────────────────────────────


def test_run_info_extracts_id_title_round_reviewers(tmp_path: Path) -> None:
    run_dir = _make_run(
        tmp_path,
        task_id="t-9",
        run_id="ff00",
        title="Add multiply()",
        rounds={1: ["code-quality", "security"], 2: ["code-quality"]},
    )
    info = develop._run_info(run_dir)
    assert info.run_id == "ff00"
    assert info.task_id == "t-9"
    assert info.title == "Add multiply()"
    assert info.round == 2  # highest round with any handoff
    assert info.reviewers == ("code-quality", "security")


def test_iter_run_dirs_finds_runs_and_ignores_non_runs(tmp_path: Path) -> None:
    _make_run(tmp_path, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    # a task dir with a stray file (no handoff/) is not a run dir
    (tmp_path / "t-2").mkdir()
    (tmp_path / "t-2" / "notes.txt").write_text("x")
    runs = develop._iter_run_dirs(tmp_path)
    assert [r.name for r in runs] == ["r1"]


def test_round_zero_when_no_handoffs_yet(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, rounds={})  # handoff/ exists but empty
    assert develop._round_and_reviewers(run_dir / "handoff") == (0, ())


def test_resolve_by_run_id_task_id_and_miss(tmp_path: Path) -> None:
    _make_run(tmp_path, task_id="t-1", run_id="run-aaa", rounds={1: ["cq"]})
    by_run = develop._resolve(tmp_path, "run-aaa")
    by_task = develop._resolve(tmp_path, "t-1")
    assert by_run is not None and by_run.name == "run-aaa"  # by run id
    assert by_task is not None and by_task.name == "run-aaa"  # by task id
    assert develop._resolve(tmp_path, "nope") is None


def test_task_title_missing_json_is_blank(tmp_path: Path) -> None:
    run_dir = tmp_path / "t-x" / "r"
    (run_dir / "handoff").mkdir(parents=True)  # no task.json sibling
    assert develop._task_title(run_dir) == ""


# ── docker seam ────────────────────────────────────────────────────────


def test_run_containers_filters_by_run_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps = (
        "loom-develop-abc-coder\tUp 2 minutes\n"
        "loom-develop-abc-review-security\tExited (0) 1 minute ago\n"
        "loom-develop-OTHER-coder\tUp 5 minutes\n"
        "unrelated-container\tUp 1 hour\n"
    )
    monkeypatch.setattr(develop, "_docker", lambda args: ps)
    cs = develop._run_containers("abc")
    assert {c.agent for c in cs} == {"coder", "review-security"}
    assert {c.agent: c.running for c in cs} == {"coder": True, "review-security": False}


def test_active_agent_detects_codex_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The codex reviewer's container has a live `codex` process — the prototype
    # only matched `claude` and would report this run as idle.
    containers = [
        develop.ContainerStatus("loom-develop-x-coder", "coder", "Up", True),
        develop.ContainerStatus("loom-develop-x-review-cq", "review-cq", "Up", True),
    ]

    def fake_docker(args: list[str]) -> str | None:
        if args[:1] == ["top"] and args[1] == "loom-develop-x-review-cq":
            return "UID PID CMD\nroot 12 codex exec resume abc --json"
        if args[:1] == ["top"]:
            return "UID PID CMD\nroot 9 sleep infinity"  # coder idle
        return None

    monkeypatch.setattr(develop, "_docker", fake_docker)
    assert develop._active_agent(containers) == "review-cq"


def test_active_agent_none_when_no_busy_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    containers = [develop.ContainerStatus("loom-develop-x-coder", "coder", "Up", True)]
    monkeypatch.setattr(develop, "_docker", lambda args: "root 9 sleep infinity")
    assert develop._active_agent(containers) is None


def test_docker_absent_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(develop, "_docker", lambda args: None)
    assert develop._run_containers("abc") == []
    assert develop._active_agent([]) is None


# ── commands ───────────────────────────────────────────────────────────


def test_dump_prints_conversation_md_when_present(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_run(patched, task_id="t-1", run_id="r1", conversation="FINAL ASSEMBLED LOG")
    develop.develop_dump(key="t-1", config=None)
    assert "FINAL ASSEMBLED LOG" in capsys.readouterr().out


def test_dump_assembles_from_handoffs_when_no_conversation(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["code-quality"]})
    develop.develop_dump(key="r1", config=None)
    out = capsys.readouterr().out
    # routed through story_develop.handoff.conversation_log
    assert "story-develop conversation log" in out
    assert "round 1 coder" in out and "code-quality round 1" in out


def test_dump_no_run_exits_1(patched: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        develop.develop_dump(key="missing", config=None)
    assert exc.value.code == 1


def test_list_json_shape(patched: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _make_run(patched, task_id="t-7", run_id="rr", title="A task", rounds={1: ["cq"]})
    develop.develop_list(config=None, output_format="json")
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "rr"
    assert rows[0]["task_id"] == "t-7"
    assert rows[0]["round"] == 1
    assert rows[0]["active"] == "done"  # docker absent → no containers


def test_list_text_table_and_empty(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    develop.develop_list(config=None, output_format="text")
    assert "no story-develop runs" in capsys.readouterr().out
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    develop.develop_list(config=None, output_format="text")
    out = capsys.readouterr().out
    assert "run" in out and "active" in out and "r1" in out


def test_attach_once_snapshot(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["code-quality"]})
    develop.develop_attach(key="t-1", config=None, once=True)
    out = capsys.readouterr().out
    assert "attached to run r1" in out
    assert "round_01_coder_done.md" in out  # handoff printed in the snapshot

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
    run_title: str | None = None,
    rounds: dict[int, list[str]] | None = None,
    conversation: str | None = None,
) -> Path:
    run_dir = work_dir / task_id / run_id
    (run_dir / "handoff").mkdir(parents=True)
    # the shared per-task task.json (runner-written; overwritten each dispatch)
    (work_dir / task_id / "task.json").write_text(
        json.dumps({"task": {"id": task_id, "title": title}})
    )
    # the per-run snapshot the plugin writes at run start (#88)
    if run_title is not None:
        (run_dir / "task.json").write_text(
            json.dumps({"task": {"id": task_id, "title": run_title}})
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


def test_title_is_per_run_not_per_task(tmp_path: Path) -> None:
    # A task re-dispatched after its title changed: run-1 was started as "Old
    # title", run-2 as "New title". The shared per-task task.json now holds the
    # NEWEST title; each run's own snapshot must win so the historical run isn't
    # mislabelled (#88 review).
    run1 = _make_run(
        tmp_path,
        task_id="t-1",
        run_id="run-1",
        title="New title",
        run_title="Old title",
    )
    run2 = _make_run(
        tmp_path,
        task_id="t-1",
        run_id="run-2",
        title="New title",
        run_title="New title",
    )
    assert develop._run_info(run1).title == "Old title"  # not the shared newest
    assert develop._run_info(run2).title == "New title"


def test_title_falls_back_to_per_task_when_no_snapshot(tmp_path: Path) -> None:
    # An in-flight run (or one predating the snapshot) has no per-run task.json;
    # the per-task file is then its own current title.
    run_dir = _make_run(tmp_path, task_id="t-2", run_id="r", title="Current")
    assert develop._task_title(run_dir) == "Current"


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
    assert cs is not None
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


def test_run_containers_none_when_docker_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # None (docker unavailable) is DISTINCT from [] (docker works, no
    # containers) — callers must not conflate "can't tell" with "done".
    monkeypatch.setattr(develop, "_docker", lambda args: None)
    assert develop._run_containers("abc") is None
    assert develop._active_agent([]) is None


def test_agent_state_distinguishes_no_docker_from_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    info = develop.RunInfo("r", "t", "", 1, (), str(tmp_path))
    monkeypatch.setattr(develop, "_run_containers", lambda rid: None)
    assert develop._agent_state(info) == "—"  # docker absent → can't tell
    monkeypatch.setattr(develop, "_run_containers", lambda rid: [])
    assert develop._agent_state(info) == "done"  # docker present, no containers


def test_still_running_predicate(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    running = [develop.ContainerStatus("n", "coder", "Up", True)]
    exited = [develop.ContainerStatus("n", "coder", "Exited", False)]
    assert develop._still_running(rd, running) is True
    assert develop._still_running(rd, exited) is False
    # docker absent (None): live until conversation.md appears (or dir reaped)
    assert develop._still_running(rd, None) is True
    (rd / "conversation.md").write_text("done")
    assert develop._still_running(rd, None) is False


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
    assert rows[0]["active"] == "—"  # docker absent → can't tell (not "done")


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


def test_attach_follows_handoffs_when_docker_absent(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: docker absent must NOT make attach exit instantly as "done".
    # A finished run (conversation.md present) still prints its handoffs and
    # terminates via the file-based end signal.
    _make_run(
        patched,
        task_id="t-1",
        run_id="r1",
        rounds={1: ["code-quality"]},
        conversation="done",
    )
    develop.develop_attach(key="r1", config=None, once=False)
    out = capsys.readouterr().out
    assert "round_01_coder_done.md" in out  # handoffs followed despite no docker
    assert "not running" in out


def test_prune_removes_finished_keeps_inflight_when_docker_absent(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # docker absent → finished is the file signal (conversation.md present).
    finished = _make_run(
        patched, task_id="t-1", run_id="done", conversation="end", rounds={1: ["cq"]}
    )
    inflight = _make_run(patched, task_id="t-2", run_id="live", rounds={1: ["cq"]})
    develop.develop_prune(config=None, dry_run=False, output_format="text")
    out = capsys.readouterr().out
    assert "removed done" in out and "removed 1 finished run" in out
    assert not finished.exists()  # finished run dir gone
    assert not finished.parent.exists()  # empty task dir reaped too
    assert inflight.exists()  # in-flight run untouched (can't probe → keep)


def test_prune_dry_run_deletes_nothing(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", conversation="end")
    develop.develop_prune(config=None, dry_run=True, output_format="text")
    out = capsys.readouterr().out
    assert "would remove r1" in out and "would remove 1 finished run" in out
    assert run_dir.exists()  # dry-run keeps everything on disk


def test_prune_keeps_startup_window_run_with_docker_present(
    patched: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (f-001): a genuinely in-flight run during its startup window has
    # its handoff dir seeded but no containers yet — and agent containers run
    # with `--rm`, so docker reports zero containers exactly as a finished run
    # would. Pruning on "no running container" would delete the live run dir out
    # from under the daemon. Only the terminal conversation.md marks a run done.
    startup = _make_run(patched, task_id="t-1", run_id="boot", rounds={})
    finished = _make_run(patched, task_id="t-2", run_id="done", conversation="end")
    # docker present, but no containers for either run (startup-window / reaped).
    monkeypatch.setattr(develop, "_run_containers", lambda rid: [])
    develop.develop_prune(config=None, dry_run=False, output_format="text")
    assert startup.exists()  # no conversation.md → in flight → kept
    assert not finished.exists()  # terminal marker → pruned


def test_prune_keeps_run_with_marker_but_live_container(
    patched: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Defensive: even with a conversation.md present, a still-live agent
    # container means the run is not finished and must be kept.
    run = _make_run(patched, task_id="t-1", run_id="r1", conversation="end")
    monkeypatch.setattr(
        develop,
        "_run_containers",
        lambda rid: [
            develop.ContainerStatus("loom-develop-r1-coder", "coder", "Up", True)
        ],
    )
    develop.develop_prune(config=None, dry_run=False, output_format="text")
    assert run.exists()


def test_prune_reports_failed_deletion_and_exits_nonzero(
    patched: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression (f-002): a deletion that fails must be reported as an error and
    # exit non-zero — never silently swallowed and reported as a success.
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", conversation="end")

    def boom(path: object, *a: object, **k: object) -> None:
        raise OSError("EBUSY")

    monkeypatch.setattr(develop.shutil, "rmtree", boom)
    with pytest.raises(SystemExit) as exc:
        develop.develop_prune(config=None, dry_run=False, output_format="json")
    assert exc.value.code == 1
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["run_id"] == "r1"
    assert rows[0]["pruned"] is False  # not falsely claimed removed
    assert "EBUSY" in rows[0]["error"]
    assert run_dir.exists()  # still on disk — caller can retry


def test_prune_json_shape_and_empty(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    develop.develop_prune(config=None, dry_run=False, output_format="json")
    assert json.loads(capsys.readouterr().out) == []
    _make_run(patched, task_id="t-7", run_id="rr", conversation="end")
    develop.develop_prune(config=None, dry_run=True, output_format="json")
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["run_id"] == "rr" and rows[0]["pruned"] is False


def test_attach_follows_until_containers_stop(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(develop.time, "sleep", lambda s: None)  # no real wait
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["code-quality"]})
    polls = {"n": 0}

    def fake_containers(run_id: str) -> list[develop.ContainerStatus]:
        polls["n"] += 1
        running = polls["n"] == 1  # live on poll 1, stopped on poll 2
        return [
            develop.ContainerStatus(
                "loom-develop-r1-coder", "coder", "Up" if running else "Exited", running
            )
        ]

    monkeypatch.setattr(develop, "_run_containers", fake_containers)
    monkeypatch.setattr(develop, "_active_agent", lambda cs: "coder")
    develop.develop_attach(key="r1", config=None, once=False)
    out = capsys.readouterr().out
    assert "coder working" in out
    assert "round_01_coder_done.md" in out
    assert "not running" in out

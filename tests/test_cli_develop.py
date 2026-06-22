"""Tests for ``lithos-loom develop`` (issue #88).

The filesystem layer (run discovery / round + reviewer parsing / resolution) is
pure and tested directly. The docker layer goes through the ``_docker`` seam,
monkeypatched with canned ``docker ps`` / ``docker top`` output — including a
codex process, the salvage fix the bash prototype missed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import fields
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
    status: str | None = None,
    branch: str | None = None,
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
    if status is not None:
        # the terminal state.json the plugin writes alongside conversation.md
        max_round = max(rounds or {0: []}, default=0)
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "run_id": run_id,
                    "rounds": max_round,
                    "branch": branch or f"feat/{task_id}",
                }
            )
        )
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


def test_run_phase_classification(tmp_path: Path) -> None:
    rd = tmp_path / "run"
    rd.mkdir()
    running = [develop.ContainerStatus("n", "coder", "Up", True)]
    exited = [develop.ContainerStatus("n", "coder", "Exited", False)]
    none: dict | None = None  # no recorded outcome yet
    approved = {"status": "approved"}
    # docker present: live while a container runs
    assert develop._run_phase(rd, running, none, seen_container=True) == "running"
    # startup window: zero containers and none seen yet → keep following (the
    # old container-liveness check exited here, the bug this fixes)
    assert develop._run_phase(rd, [], none, seen_container=False) == "running"
    # a seen container gone with no recorded outcome yet → ambiguous (teardown
    # window vs crash); the caller grace-polls before deciding
    assert develop._run_phase(rd, exited, none, seen_container=True) == "vanished"
    # docker absent (None): live until the outcome lands (or the dir is reaped)
    assert develop._run_phase(rd, None, none, seen_container=False) == "running"
    # the recorded OUTCOME (state.json with a status) is the graceful terminal —
    # regardless of container state, and NOT conversation.md / liveness.
    assert develop._run_phase(rd, exited, approved, seen_container=True) == "terminal"
    assert develop._run_phase(rd, running, approved, seen_container=True) == "terminal"
    assert develop._run_phase(rd, None, approved, seen_container=False) == "terminal"
    # docker absent + the run dir reaped on success → terminal (nothing to read)
    rd.rmdir()
    assert develop._run_phase(rd, None, none, seen_container=False) == "terminal"


def test_recover_reaped_outcome_binds_to_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # correctness/f-003: a reaped (success-cleaned) run's outcome is recovered
    # from the completion store, bound to THIS run via the recorded
    # conversation-log path's run id so a prior success of the same task can't
    # be claimed.
    run_dir = tmp_path / "t-1" / "r1"
    run_dir.mkdir(parents=True)
    record = {
        "task_id": "t-1",
        "status": "succeeded",
        "artifacts": {"conversation_log": str(run_dir / "conversation.md")},
    }
    monkeypatch.setattr(
        develop, "lookup_completed", lambda k, expected_task_id=None: record
    )
    assert develop._recover_reaped_outcome(run_dir) == {"status": "approved"}
    # a record for a DIFFERENT run of the same task must NOT be claimed as ours
    stale = {
        "task_id": "t-1",
        "status": "succeeded",
        "artifacts": {
            "conversation_log": str(tmp_path / "t-1" / "OLD" / "conversation.md")
        },
    }
    monkeypatch.setattr(
        develop, "lookup_completed", lambda k, expected_task_id=None: stale
    )
    assert develop._recover_reaped_outcome(run_dir) is None
    # no record at all → nothing to recover
    monkeypatch.setattr(
        develop, "lookup_completed", lambda k, expected_task_id=None: None
    )
    assert develop._recover_reaped_outcome(run_dir) is None


def test_iter_new_handoffs_caps_count_per_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # security/f-003: a flood of agent-written handoff files must not all be read
    # in one poll; the batch is capped and the remainder surfaces on later polls.
    monkeypatch.setattr(develop, "_MAX_HANDOFFS_PER_POLL", 3)
    hd = tmp_path / "handoff"
    hd.mkdir()
    for i in range(5):
        (hd / f"round_01_review_r{i}.md").write_text("body")
    pairs = develop._iter_new_handoffs(hd, set())
    real = [(n, b) for n, b in pairs if n.startswith("round_")]
    assert len(real) == 3  # capped, not all 5 slurped at once
    assert any("more handoffs" in n for n, _ in pairs)  # overflow notice present
    # the remainder surfaces once the first batch is marked seen (no loss)
    seen = {n for n, _ in real}
    more = [
        n for n, _ in develop._iter_new_handoffs(hd, seen) if n.startswith("round_")
    ]
    assert len(more) == 2


def test_read_handoff_bounds_size_and_decodes_leniently(tmp_path: Path) -> None:
    # f-002: an oversized agent-written handoff must be read bounded, not slurped.
    p = tmp_path / "h.md"
    p.write_bytes(b"A" * (develop._MAX_HANDOFF_BYTES + 5000))
    body = develop._read_handoff(p)
    assert "truncated" in body
    assert body.count("A") == develop._MAX_HANDOFF_BYTES  # capped, not the full file
    # invalid utf-8 (an agent can write arbitrary bytes) must not raise
    p.write_bytes(b"\xff\xfe ok")
    assert "ok" in develop._read_handoff(p)


def test_sanitize_strips_control_keeps_tab_and_newline() -> None:
    # f-001: ESC (0x1b) and BEL (0x07) stripped; TAB/LF preserved.
    assert develop._sanitize("a\x1b[31mb\x07\n\tc") == "a[31mb\n\tc"


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
    # the timestamp column is exposed raw (epoch float) for machine consumers,
    # and is confined to `list` — it is NOT a field on the shared RunInfo model.
    assert isinstance(rows[0]["mtime"], float)
    assert rows[0]["mtime"] > 0
    assert "mtime" not in {f.name for f in fields(develop.RunInfo)}


def test_list_text_table_and_empty(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    develop.develop_list(config=None, output_format="text")
    assert "no story-develop runs" in capsys.readouterr().out
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    develop.develop_list(config=None, output_format="text")
    out = capsys.readouterr().out
    assert "run" in out and "active" in out and "r1" in out
    # the timestamp column is present, and the data row carries a full
    # wall-clock value of the documented YYYY-MM-DD HH:MM:SS shape.
    assert "updated" in out
    data_row = next(line for line in out.splitlines() if line.startswith("r1"))
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", data_row)


def test_prune_json_omits_list_only_mtime(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `mtime` is a `list`-only projection; it must not leak into `prune`'s JSON.
    _make_run(patched, task_id="t-1", run_id="r1", conversation="done")
    develop.develop_prune(config=None, dry_run=True, output_format="json")
    rows = json.loads(capsys.readouterr().out)
    assert rows and "mtime" not in rows[0]
    assert "pruned" in rows[0]


def test_latest_mtime_tracks_handoff_writes(tmp_path: Path) -> None:
    # Regression for the stale-parent bug: a handoff written into run_dir/handoff/
    # bumps the handoff dir, not the parent run_dir, so the parent mtime alone is
    # stale. _latest_mtime must reflect the newer handoff activity.
    run_dir = _make_run(tmp_path, task_id="t-1", run_id="r1")  # seed only, no round
    parent_mtime = run_dir.stat().st_mtime
    later = parent_mtime + 1000
    handoff = run_dir / "handoff" / "round_01_coder_done.md"
    handoff.write_text("## Status: LGTM\n")
    os.utime(handoff, (later, later))
    os.utime(run_dir / "handoff", (later, later))
    # parent run_dir is untouched, yet the run's last activity is `later`
    assert run_dir.stat().st_mtime == pytest.approx(parent_mtime)
    assert develop._latest_mtime(run_dir) == pytest.approx(later)


def test_list_output_reflects_latest_handoff_mtime(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Command-level guard (not just the helper): develop_list must surface the
    # *handoff* activity, not the stale parent run-dir mtime, in both json + text.
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    later = run_dir.stat().st_mtime + 10_000
    os.utime(run_dir / "handoff" / "round_01_coder_done.md", (later, later))
    os.utime(run_dir / "handoff", (later, later))
    # the parent run dir stays stale; only the handoff moved forward
    assert run_dir.stat().st_mtime < later

    develop.develop_list(config=None, output_format="json")
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["mtime"] == pytest.approx(later)

    develop.develop_list(config=None, output_format="text")
    data_row = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("r1")
    )
    assert develop._format_mtime(later) in data_row


def test_list_text_survives_poisoned_handoff_mtime(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A single run with an out-of-range handoff mtime must not crash the whole
    # text listing — the operator still gets every run (security/f-001).
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    handoff = run_dir / "handoff" / "round_01_coder_done.md"
    try:
        os.utime(handoff, (9e18, 9e18))
    except (OverflowError, OSError):
        pytest.skip("platform rejects an out-of-range utime")
    develop.develop_list(config=None, output_format="text")  # must not raise
    out = capsys.readouterr().out
    assert "r1" in out and "—" in out


def test_format_mtime_zero_and_out_of_range_render_dash() -> None:
    assert develop._format_mtime(0.0) == "—"
    # A poisoned mtime renders as "—" instead of crashing: handoff files are
    # bind-mounted RW into agent containers, so an agent can set an arbitrary
    # out-of-range utime; time.localtime would otherwise raise and abort the
    # whole text listing (security/f-001).
    assert develop._format_mtime(9e18) == "—"
    assert develop._format_mtime(-9e18) == "—"


def test_format_mtime_exact_string_under_fixed_tz() -> None:
    # exact wall-clock string under a pinned timezone for a known epoch.
    # Restore the process-global tz afterwards (tzset mutates C-library state),
    # so we don't leak UTC into later tests on a non-UTC host (test-quality/f-003).
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "UTC"
        time.tzset()
        assert develop._format_mtime(1_700_000_000.0) == "2023-11-14 22:13:20"
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
            develop._format_mtime(1_700_000_000.0),
        )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_attach_once_snapshot(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["code-quality"]})
    develop.develop_attach(key="t-1", config=None, once=True, wait=False, stream=False)
    out = capsys.readouterr().out
    assert "attached to run r1" in out
    assert "round_01_coder_done.md" in out  # handoff printed in the snapshot


def test_attach_follows_handoffs_when_docker_absent(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression: docker absent must NOT make attach exit instantly as "done".
    # A finished run (conversation.md present) still prints its handoffs and
    # terminates via the file-based end signal, then its outcome summary.
    _make_run(
        patched,
        task_id="t-1",
        run_id="r1",
        rounds={1: ["code-quality"]},
        conversation="done",
        status="approved",
    )
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=False)
    out = capsys.readouterr().out
    assert "round_01_coder_done.md" in out  # handoffs followed despite no docker
    assert "approved" in out  # terminal-state outcome summary


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


def test_prune_keeps_unseeded_startup_run_in_shared_task_dir(
    patched: Path,
) -> None:
    # Regression (f-001 r3): a task with an old finished run AND a brand-new
    # dispatch whose run dir exists but hasn't seeded handoff/ yet. Pruning the
    # old run must not reap the shared task dir out from under the unseeded
    # startup run (which `_is_run_dir` doesn't recognise without handoff/).
    old = _make_run(patched, task_id="t-1", run_id="old", conversation="end")
    new = patched / "t-1" / "new"  # created by __main__ before develop() seeds it
    new.mkdir()
    (new / "task.json").write_text("{}")  # snapshot copied at run start
    develop.develop_prune(config=None, dry_run=False, output_format="text")
    assert not old.exists()  # finished run pruned
    assert new.exists()  # unseeded startup run kept
    assert (patched / "t-1").exists()  # task dir not reaped


def test_prune_reaps_task_dir_when_only_files_remain(patched: Path) -> None:
    # The cleanup still fires when the task's last run is gone and only the
    # stale per-task task.json remains (no run subdirs left).
    run = _make_run(patched, task_id="t-1", run_id="only", conversation="end")
    develop.develop_prune(config=None, dry_run=False, output_format="text")
    assert not run.exists()
    assert not (patched / "t-1").exists()  # emptied task dir reaped


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


def test_attach_stops_when_seen_containers_vanish_without_marker(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A run whose seen agent containers disappear and never record an outcome (a
    # hard crash) must not hang the follow forever: after the teardown grace
    # window elapses with no state.json, it stops and reports the missing
    # outcome rather than looping.
    monkeypatch.setattr(develop.time, "sleep", lambda s: None)  # no real wait
    monkeypatch.setattr(develop, "_TEARDOWN_GRACE_POLLS", 2)  # short grace for the test
    _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["code-quality"]})
    polls = {"n": 0}

    def fake_containers(run_id: str) -> list[develop.ContainerStatus]:
        polls["n"] += 1
        running = polls["n"] == 1  # live on poll 1, gone (and never returns) after
        return [
            develop.ContainerStatus(
                "loom-develop-r1-coder", "coder", "Up" if running else "Exited", running
            )
        ]

    monkeypatch.setattr(develop, "_run_containers", fake_containers)
    monkeypatch.setattr(develop, "_active_agent", lambda cs: "coder")
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=False)
    out = capsys.readouterr().out
    assert "coder working" in out
    assert "round_01_coder_done.md" in out
    assert "without recording an outcome" in out  # crash outcome, not a hang


def test_attach_waits_through_teardown_window_for_recorded_outcome(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (correctness/f-001): the plugin force-removes its agent
    # containers BEFORE it computes commits and writes the terminal state.json,
    # so a normally-completing run spends a window with no containers and no
    # outcome. attach must grace-poll through that window and report the real
    # recorded outcome — not declare a crash on container disappearance.
    monkeypatch.setattr(develop.time, "sleep", lambda s: None)
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    polls = {"n": 0}

    def fake_containers(run_id: str) -> list[develop.ContainerStatus]:
        polls["n"] += 1
        if polls["n"] == 1:  # agent working
            return [
                develop.ContainerStatus("loom-develop-r1-coder", "coder", "Up", True)
            ]
        if polls["n"] in (2, 3):  # teardown window: containers gone, no outcome yet
            return []
        # the plugin finishes writing the terminal outcome
        (run_dir / "conversation.md").write_text("log")
        (run_dir / "state.json").write_text(
            json.dumps({"status": "approved", "rounds": 1, "branch": "feat/x"})
        )
        return []

    monkeypatch.setattr(develop, "_run_containers", fake_containers)
    monkeypatch.setattr(develop, "_active_agent", lambda cs: "coder" if cs else None)
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=False)
    out = capsys.readouterr().out
    assert "approved" in out and "after 1 round" in out  # real outcome
    assert "without recording an outcome" not in out  # NOT misreported as a crash


def test_attach_wait_waits_for_recorded_outcome_not_just_the_log(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (correctness/f-002): conversation.md is written BEFORE
    # state.json. --wait must wait for the recorded outcome (state.json), not
    # stop at the log and exit non-zero for an approved run.
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})
    (run_dir / "conversation.md").write_text("log")  # log present, NO state.json yet
    sleeps = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 2:  # the outcome lands a couple polls later
            (run_dir / "state.json").write_text(
                json.dumps({"status": "approved", "rounds": 1, "branch": "feat/x"})
            )

    monkeypatch.setattr(develop.time, "sleep", fake_sleep)
    # docker absent (patched) → terminal keys on the recorded outcome, not the log.
    develop.develop_attach(key="r1", config=None, once=False, wait=True, stream=False)
    out = capsys.readouterr().out
    assert "approved" in out  # exited 0 (no SystemExit) and reported the outcome


def test_attach_follows_through_startup_window_to_terminal_state(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The headline behaviour: attach follows to TERMINAL STATE, not agent
    # liveness. During the startup window docker reports zero containers — the
    # old check exited instantly as "done". Now it keeps polling through startup
    # → working → teardown, then prints the recorded outcome summary.
    monkeypatch.setattr(develop.time, "sleep", lambda s: None)
    run_dir = _make_run(patched, task_id="t-1", run_id="r1")  # no handoffs yet
    polls = {"n": 0}

    def fake_containers(run_id: str) -> list[develop.ContainerStatus]:
        polls["n"] += 1
        if polls["n"] == 1:
            return []  # startup window: handoff dir seeded, containers not up
        if polls["n"] == 2:
            (run_dir / "handoff" / "round_01_coder_done.md").write_text(
                "## Status: LGTM\nwork"
            )
            return [
                develop.ContainerStatus("loom-develop-r1-coder", "coder", "Up", True)
            ]
        # run reaches terminal state: marker + state written, containers reaped
        (run_dir / "conversation.md").write_text("log")
        (run_dir / "state.json").write_text(
            json.dumps({"status": "approved", "rounds": 1, "branch": "feat/x"})
        )
        return []

    monkeypatch.setattr(develop, "_run_containers", fake_containers)
    monkeypatch.setattr(develop, "_active_agent", lambda cs: "coder" if cs else None)
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=False)
    out = capsys.readouterr().out
    assert "starting up" in out  # did NOT exit during the startup window
    assert "coder working" in out
    assert "round_01_coder_done.md" in out
    assert "approved" in out and "after 1 round" in out  # outcome summary


def test_attach_wait_is_quiet_and_summarises_outcome(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --wait suppresses the play-by-play and prints only the outcome line.
    _make_run(
        patched,
        task_id="t-1",
        run_id="r1",
        rounds={1: ["cq"]},
        conversation="log",
        status="approved",
        branch="feat/win",
    )
    develop.develop_attach(key="r1", config=None, once=False, wait=True, stream=False)
    out = capsys.readouterr().out
    assert "round_01_coder_done.md" not in out  # no streamed handoffs
    assert "approved" in out and "feat/win" in out


def test_attach_wait_exits_nonzero_when_not_approved(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_run(patched, task_id="t-1", run_id="r1", conversation="log", status="failed")
    with pytest.raises(SystemExit) as exc:
        develop.develop_attach(
            key="r1", config=None, once=False, wait=True, stream=False
        )
    assert exc.value.code == 1
    assert "failed" in capsys.readouterr().out


def test_attach_wait_captures_outcome_before_workdir_is_reaped(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (correctness/f-003): the route-runner reaps an approved run's
    # work dir immediately after the plugin exits. attach must capture the
    # outcome at terminal-detection time, not re-read a since-deleted run_dir —
    # which would misreport approved as a crash and make --wait exit 1.
    run_dir = _make_run(
        patched,
        task_id="t-1",
        run_id="r1",
        rounds={1: ["cq"]},
        conversation="log",
        status="approved",
    )
    real_read = develop._read_state
    calls = {"n": 0}

    def reaping_read(rd: Path) -> dict | None:
        calls["n"] += 1
        state = real_read(rd)
        if calls["n"] == 1 and state and state.get("status"):
            shutil.rmtree(rd, ignore_errors=True)  # the success reap, mid-follow
        return state

    monkeypatch.setattr(develop, "_read_state", reaping_read)
    # approved → must NOT raise SystemExit (exit 0); reported from the snapshot
    develop.develop_attach(key="r1", config=None, once=False, wait=True, stream=False)
    out = capsys.readouterr().out
    assert "approved" in out
    assert "without recording an outcome" not in out
    assert not run_dir.exists()  # the dir really was reaped during the follow


def test_attach_wait_recovers_reaped_success_when_state_never_seen(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (correctness/f-003 r4): attach can miss state.json entirely — a
    # fast approved run writes it and the route-runner reaps the whole work dir
    # between two polls. The outcome is then recovered from the host-persistent
    # completion store (a source the route-runner never removes), so --wait
    # reports approved and exits 0 instead of misreporting a crash + exit 1.
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})

    # state.json is NEVER written into the run dir (attach never observes it).
    def reaping_sleep(_seconds: float) -> None:
        shutil.rmtree(run_dir.parent, ignore_errors=True)  # route-runner success reap

    monkeypatch.setattr(develop.time, "sleep", reaping_sleep)
    monkeypatch.setattr(
        develop,
        "lookup_completed",
        lambda key, expected_task_id=None: {
            "task_id": "t-1",
            "status": "succeeded",
            "artifacts": {"conversation_log": str(run_dir / "conversation.md")},
        },
    )
    # approved (recovered) → must NOT raise SystemExit and must report approved
    develop.develop_attach(key="r1", config=None, once=False, wait=True, stream=False)
    out = capsys.readouterr().out
    assert "approved" in out
    assert "without recording an outcome" not in out
    assert not run_dir.exists()  # the work dir really was reaped mid-follow


def test_attach_wait_reaped_without_recoverable_record_is_not_a_false_crash(
    patched: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reaped run with no recoverable success record (e.g. a non-default
    # idempotency key, or a failure reaped under retain_failed_workdirs=False)
    # reports "work dir reaped" rather than the misleading "crashed?" line.
    run_dir = _make_run(patched, task_id="t-1", run_id="r1", rounds={1: ["cq"]})

    def reaping_sleep(_seconds: float) -> None:
        shutil.rmtree(run_dir.parent, ignore_errors=True)

    monkeypatch.setattr(develop.time, "sleep", reaping_sleep)
    monkeypatch.setattr(
        develop, "lookup_completed", lambda key, expected_task_id=None: None
    )
    with pytest.raises(SystemExit) as exc:
        develop.develop_attach(
            key="r1", config=None, once=False, wait=True, stream=False
        )
    assert exc.value.code == 1  # success unconfirmed → non-zero
    out = capsys.readouterr().out
    assert "work dir reaped" in out
    assert "crashed" not in out


def test_attach_stream_emits_jsonl_events(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_run(
        patched,
        task_id="t-1",
        run_id="r1",
        rounds={1: ["cq"]},
        conversation="log",
        status="approved",
    )
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=True)
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    kinds = [e["event"] for e in events]
    assert "handoff" in kinds
    assert kinds[-1] == "outcome"  # terminal event last
    outcome = events[-1]
    assert outcome["status"] == "approved" and outcome["rounds"] == 1


def test_attach_rejects_conflicting_modes(patched: Path) -> None:
    # --once / --wait / --stream are mutually exclusive.
    with pytest.raises(SystemExit) as exc:
        develop.develop_attach(key="r1", config=None, once=True, wait=True)
    assert exc.value.code == 2


def test_attach_text_output_strips_handoff_escape_sequences(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Regression (security/f-001): handoff name + body are agent-writable, so the
    # text view must strip terminal control/escape bytes (no forged/hidden
    # output on the operator's terminal) while preserving the visible content.
    run_dir = _make_run(
        patched, task_id="t-1", run_id="r1", conversation="log", status="approved"
    )
    hd = run_dir / "handoff"
    (hd / "round_01_coder_done.md").write_text("clean\x1b[2Jspoofed")
    # ESC also smuggled into the reviewer-name segment of the filename
    (hd / "round_01_review_cq\x1b[31m.md").write_text("review\x1b]0;title\x07body")
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=False)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out  # no escapes reach the terminal
    assert "clean" in out and "spoofed" in out  # content preserved, just de-fanged


def test_attach_stream_handoff_body_is_escape_safe_via_json(
    patched: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --stream stays raw-fidelity but escape-safe: json.dumps encodes control
    # bytes as \uXXXX, so no literal ESC reaches the consumer's terminal either.
    run_dir = _make_run(
        patched, task_id="t-1", run_id="r1", conversation="log", status="approved"
    )
    (run_dir / "handoff" / "round_01_coder_done.md").write_text("x\x1by")
    develop.develop_attach(key="r1", config=None, once=False, wait=False, stream=True)
    out = capsys.readouterr().out
    assert "\x1b" not in out
    handoff_events = [
        json.loads(line)
        for line in out.splitlines()
        if line.strip() and json.loads(line).get("event") == "handoff"
    ]
    assert any(e["body"] == "x\x1by" for e in handoff_events)  # decoded value intact

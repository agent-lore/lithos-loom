"""Brief review at dispatch (604fb936), PR 1: the addendum format and the pass.

The format is the lens T3 pilot's (W6–W8, hand-written and operator-approved
on 2026-10-05): scope cuts, facts and decisions, each item a top-level
``- **<id>. <title>.**`` bullet carrying its nested lines. The three pilot
addenda are the fixtures: whatever the pass emits has to parse the same way
the operator's own approved text does, because phase 1 diffs the approved
text against the draft item by item.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from lithos_loom.plugins.story_develop import brief_review as br
from lithos_loom.plugins.story_develop.brief_review import (
    KIND_DECISION,
    KIND_FACT,
    KIND_SCOPE_CUT,
    MODE_DELTA,
    MODE_FULL,
    Addendum,
    BriefInputs,
    parse_addendum,
    render_addendum,
    review_brief,
    validate_addendum,
)
from lithos_loom.plugins.story_develop.config import DevelopConfig

FIXTURES = Path(__file__).parent / "fixtures" / "brief_review"
BASE = "392301b" + "0" * 33
PRIOR = "1b8c59e" + "0" * 33


def _fixture(name: str) -> str:
    return (FIXTURES / f"lens-t3-{name}.md").read_text(encoding="utf-8")


# --- parsing the pilot addenda --------------------------------------------


@pytest.mark.parametrize(
    ("name", "scope_cuts", "facts", "decisions"),
    [("w6", 0, 10, 11), ("w7", 5, 12, 18), ("w8", 5, 12, 10)],
)
def test_the_pilot_addenda_parse_into_their_items(
    name: str, scope_cuts: int, facts: int, decisions: int
) -> None:
    addendum = parse_addendum(_fixture(name))

    assert len(addendum.scope_cuts) == scope_cuts
    assert len(addendum.facts) == facts
    assert len(addendum.decisions) == decisions
    assert addendum.no_change is None
    # every id lands in the section its letter names
    assert {i.id[0] for i in addendum.scope_cuts} <= {"S"}
    assert {i.id[0] for i in addendum.facts} == {"F"}
    assert {i.id[0] for i in addendum.decisions} == {"D"}
    assert validate_addendum(addendum, strict=False) == []


def test_an_item_keeps_its_nested_lines_and_paragraphs() -> None:
    items = {i.id: i for i in parse_addendum(_fixture("w8")).items}

    d3 = items["D3"].text
    assert d3.startswith("**D3. The write.**")
    # the numbered sub-list AND the indented paragraph after a blank line
    assert "  1. reads the focal task's edges fresh;" in d3
    assert "**If the fresh read fails, nothing is written:**" in d3
    assert "precheck_failed" in d3
    # the next item is not swallowed
    assert "D4" not in d3
    s1 = items["S1"].text
    assert "A mis-drawn edge is removed outside Lens" in s1
    assert items["S1"].kind == KIND_SCOPE_CUT
    assert items["F2"].kind == KIND_FACT
    assert items["D3"].kind == KIND_DECISION


@pytest.mark.parametrize("name", ["w6", "w7", "w8"])
def test_render_then_parse_round_trips_the_items(name: str) -> None:
    addendum = parse_addendum(_fixture(name))

    rendered = render_addendum(
        addendum, base_sha=BASE, on=date(2026, 10, 5), mode=MODE_FULL
    )

    assert parse_addendum(rendered).items == addendum.items


def test_render_states_the_base_and_omits_empty_sections() -> None:
    addendum = parse_addendum(_fixture("w6"))  # no scope cuts

    rendered = render_addendum(
        addendum, base_sha=BASE, on=date(2026, 10, 5), mode=MODE_FULL
    )

    first = rendered.splitlines()[0]
    assert first.startswith("**Brief review against `392301b00000` (2026-10-05)**")
    assert "**Scope cuts**" not in rendered
    assert rendered.index("**Facts**") < rendered.index("**Decisions**")
    assert not rendered.startswith("\n")


def test_a_delta_names_both_bases_and_a_no_change_renders_its_reason() -> None:
    addendum = Addendum(items=(), no_change="nothing merged touches the funnel")

    rendered = render_addendum(
        addendum,
        base_sha=BASE,
        on=date(2026, 10, 5),
        mode=MODE_DELTA,
        prior_base=PRIOR,
    )

    assert rendered.startswith("**Recheck against `392301b00000` (2026-10-05)**")
    assert "`1b8c59e00000`" in rendered
    assert "No change: nothing merged touches the funnel" in rendered
    assert parse_addendum(rendered).no_change == "nothing merged touches the funnel"


# --- validating what the agent wrote (strict) -----------------------------

_VALID = """\
## Facts

- **F1. The funnel classifies a raise as the write's failure.**
  `src/lithos_lens/write_funnel.py:412` catches everything `perform` raises.
- **F2. Budgets.** `max_module_lines` is 875; `write_funnel.py` is 871 lines.

## Decisions

- **D1. Let `perform` answer a refusal.**
  - `perform` may return a `WriteProblem`, which the funnel answers as is.
  - Basis: F1; the brief's "Refusals" section.
"""


def test_a_well_formed_draft_validates() -> None:
    addendum = parse_addendum(_VALID)

    assert [i.id for i in addendum.items] == ["F1", "F2", "D1"]
    assert validate_addendum(addendum) == []


def test_validation_names_every_problem() -> None:
    text = """\
## Facts

- **F1. A fact.** something
- **D7. A decision filed under facts.** do this
  - Basis: F1

## Decisions

- **D1. No basis.** do that
- **F1. Duplicate id.** again
  - Basis: F1

## Scope cuts

- **S1. Drop removal.** cut it
"""

    problems = validate_addendum(parse_addendum(text))

    joined = "\n".join(problems)
    assert "D7 is under Facts" in joined
    assert "F1 is under Decisions" in joined
    assert "F1 appears twice" in joined
    assert "D1 has no `Basis:` line" in joined
    assert "S1 has no `Basis:` line" in joined


def test_a_file_with_no_items_and_no_no_change_is_invalid() -> None:
    problems = validate_addendum(parse_addendum("I checked everything.\n"))

    assert problems and "no items" in problems[0]


def test_no_change_needs_a_reason_and_excludes_items() -> None:
    bare = parse_addendum("## No change\n")
    both = parse_addendum("## No change\nNothing moved.\n\n" + _VALID)

    assert any("reason" in p for p in validate_addendum(bare))
    assert any("both" in p for p in validate_addendum(both))
    reasoned = parse_addendum("## No change\nNothing merged touches the brief.\n")
    assert validate_addendum(reasoned) == []


def test_an_item_before_any_section_is_reported() -> None:
    text = "- **F1. Orphan.** text\n\n" + _VALID.replace("F1", "F9", 1)

    problems = validate_addendum(parse_addendum(text))

    assert any("F1 appears before any section" in p for p in problems)


# PR #448 review, finding 2: a model's ordinary formatting slip — a colon for
# the id's full stop — must reach the correction turn, never vanish. The lenient
# parse cannot read such a line as an item, so it is kept as STRAY content and
# validation reports it.
_COLON_ID = """\
## Facts

- **F1. The funnel classifies a raise.** `write_funnel.py:412`.

## Decisions

- **D1: Add a new module.** Put the routes in `edge_routes.py`.
  - Basis: F1.
"""


def test_a_malformed_item_line_with_no_open_item_is_reported_not_dropped() -> None:
    addendum = parse_addendum(_COLON_ID)

    problems = validate_addendum(addendum)

    assert [i.id for i in addendum.items] == ["F1"]
    assert any(
        "- **D1: Add a new module.**" in p and "could not be read" in p
        for p in problems
    ), problems


def test_a_malformed_item_line_after_a_valid_item_is_reported() -> None:
    # The slip here would be swallowed into D1's text, not dropped: the D2
    # decision would be approved as a nested line of D1.
    text = _VALID + "- **D2: Second decision.** do that\n  - Basis: F2.\n"

    problems = validate_addendum(parse_addendum(text))

    assert any("- **D2: Second decision.**" in p for p in problems), problems


def test_prose_in_a_section_outside_any_item_is_reported() -> None:
    text = "## Facts\n\nTwo facts matter here.\n\n- **F1. One.** `a.py:1`.\n"

    problems = validate_addendum(parse_addendum(text))

    assert any("Two facts matter here." in p for p in problems), problems


def test_the_pilot_addenda_carry_no_unreadable_lines() -> None:
    for name in ("w6", "w7", "w8"):
        assert parse_addendum(_fixture(name)).stray == (), name


# --- the pass ---------------------------------------------------------------


def _config(tmp_path: Path) -> DevelopConfig:
    return DevelopConfig(
        repo=tmp_path / "repo",
        description="T3-W8: Add a dependency",
        work_dir=tmp_path / "work",
    )


def _inputs(**overrides: object) -> BriefInputs:
    values: dict[str, object] = {
        "story_id": "24ad5f91-8d51-44e2-84dc-713ffa9dfc1d",
        "title": "T3-W8: Add a dependency",
        "brief": "Slice W8 of `docs/prd/t3.md`. Build {relation sentences}.",
        "prd": "docs/prd/t3-curated-write-actions.md",
        "prd_sections": "D11, Routes",
        "written_at": "2026-10-01T08:29:44+00:00",
    }
    values.update(overrides)
    return BriefInputs(**values)  # type: ignore[arg-type]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config: DevelopConfig,
    *,
    drafts: list[str | None],
    turns_succeed: bool = True,
    cost: float = 0.25,
) -> dict:
    """Fake the container + turns. *drafts* is what each successive turn
    writes into the handoff file (``None``: writes nothing)."""
    captured: dict = {"turns": [], "git": []}
    wt = tmp_path / "wt"
    wt.mkdir(parents=True, exist_ok=True)

    def fake_create_at(repo, ref, name, *, parent=None):
        captured["create_at"] = (repo, ref)
        return wt

    monkeypatch.setattr(br.worktree, "create_at", fake_create_at)
    monkeypatch.setattr(
        br.worktree, "remove", lambda p, force=False: captured.setdefault("removed", p)
    )

    def fake_build_run_cmd(cfg, **kwargs):
        captured["run_cmd_kwargs"] = kwargs
        return ("brief-review-container", ["docker", "run"])

    monkeypatch.setattr(br, "build_run_cmd", fake_build_run_cmd)
    monkeypatch.setattr(
        br.containers,
        "start_container",
        lambda cmd: captured.setdefault("started", cmd),
    )
    monkeypatch.setattr(
        br.containers,
        "stop_container",
        lambda name: captured.setdefault("stopped", name),
    )
    monkeypatch.setattr(
        br.git,
        "log_since",
        lambda w, since, head="HEAD", **k: (
            captured["git"].append(("log_since", since, head))
            or "abc1234 2026-10-03 feat: W5\n"
        ),
    )
    monkeypatch.setattr(
        br.git,
        "log_between",
        lambda w, base, head="HEAD": (
            captured["git"].append(("log_between", base, head)) or "abc1234 feat: W7\n"
        ),
    )
    monkeypatch.setattr(
        br.git,
        "diff_stat",
        lambda w, base: (
            captured["git"].append(("diff_stat", base)) or " src/x.py | 3 ++-\n"
        ),
    )
    pending = list(drafts)

    def fake_run_turn(**kwargs):
        captured["turns"].append(kwargs)
        draft = pending.pop(0) if pending else None
        if draft is not None:
            config.handoff_dir.mkdir(parents=True, exist_ok=True)
            (config.handoff_dir / br.BRIEF_REVIEW_HANDOFF_NAME).write_text(
                draft, encoding="utf-8"
            )
        return SimpleNamespace(
            succeeded=turns_succeed,
            completed=turns_succeed,
            session_id="sess-1",
            cost_usd=cost,
            result_text="",
        )

    monkeypatch.setattr(br.turns, "run_turn", fake_run_turn)
    return captured


def test_the_pass_runs_read_only_at_the_base_and_returns_the_addendum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is not None
    assert [i.id for i in result.addendum.items] == ["F1", "F2", "D1"]
    assert result.note == ""
    assert result.base_sha == BASE and result.mode == MODE_FULL
    assert result.cost_usd == pytest.approx(0.25)
    assert captured["create_at"] == (config.repo, BASE)
    assert captured["run_cmd_kwargs"]["read_only"] is True
    assert captured["run_cmd_kwargs"]["agent"] == "brief-review"
    assert captured["stopped"] == "brief-review-container"
    assert captured["removed"] == tmp_path / "wt"
    assert len(captured["turns"]) == 1


def test_story_text_reaches_the_agent_as_files_never_as_prompt_slots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    review_brief(config, _inputs(), base_sha=BASE)

    prompt = captured["turns"][0]["prompt"]
    inputs_dir = config.artifacts_dir / br.INPUTS_DIR_NAME
    brief = (inputs_dir / "brief.md").read_text(encoding="utf-8")
    story = (inputs_dir / "story.md").read_text(encoding="utf-8")
    # The brief holds `{relation sentences}`: rendered into a slot, a later
    # `.replace` could splice into it. It must arrive only as a file.
    assert "{relation sentences}" in brief
    assert "{relation sentences}" not in prompt
    assert brief.startswith("# T3-W8: Add a dependency")
    assert "docs/prd/t3-curated-write-actions.md" in story
    assert "D11, Routes" in story
    assert BASE in story
    assert "/workspace/.handoff/artifacts/brief-review/" in prompt
    assert "{" + "inputs_dir" + "}" not in prompt  # every slot was filled


def test_full_mode_gives_the_history_since_the_brief_was_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    review_brief(config, _inputs(), base_sha=BASE)

    assert captured["git"] == [("log_since", "2026-10-01T08:29:44+00:00", "HEAD")]
    history = (config.artifacts_dir / br.INPUTS_DIR_NAME / "history.md").read_text()
    assert "feat: W5" in history


def test_delta_mode_gives_the_commits_between_the_two_bases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(
        monkeypatch,
        tmp_path,
        config,
        drafts=["## No change\nNothing merged since touches this brief.\n"],
    )

    result = review_brief(
        config, _inputs(), base_sha=BASE, mode=MODE_DELTA, prior_base=PRIOR
    )

    assert ("log_between", PRIOR, "HEAD") in captured["git"]
    assert ("diff_stat", PRIOR) in captured["git"]
    assert result.addendum is not None
    assert result.addendum.no_change == "Nothing merged since touches this brief."
    assert result.prior_base == PRIOR
    assert "delta" in captured["turns"][0]["prompt"].lower()


def test_delta_mode_without_a_prior_base_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="prior_base"):
        review_brief(_config(tmp_path), _inputs(), base_sha=BASE, mode=MODE_DELTA)


def test_an_invalid_draft_gets_one_correction_turn_in_the_same_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    bad = "## Decisions\n\n- **D1. No basis.** do it\n"
    captured = _install(monkeypatch, tmp_path, config, drafts=[bad, _VALID])

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is not None and result.note == ""
    assert len(captured["turns"]) == 2
    second = captured["turns"][1]
    assert second["resume"] is True and second["session_id"] == "sess-1"
    assert "D1 has no `Basis:` line" in second["prompt"]
    assert result.cost_usd == pytest.approx(0.5)


def test_a_draft_still_invalid_after_correction_degrades_with_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    bad = "## Decisions\n\n- **D1. No basis.** do it\n"
    _install(monkeypatch, tmp_path, config, drafts=[bad, bad])

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is None
    assert "did not validate" in result.note
    assert "D1 has no `Basis:` line" in result.note
    assert result.raw.startswith("## Decisions")


def test_a_failed_turn_degrades_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(
        monkeypatch, tmp_path, config, drafts=[None], turns_succeed=False
    )

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is None
    assert "turn failed" in result.note
    assert captured["stopped"] == "brief-review-container"
    assert captured["removed"] == tmp_path / "wt"


def test_a_missing_handoff_file_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    _install(monkeypatch, tmp_path, config, drafts=[None, None])

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is None
    assert "wrote no" in result.note


def test_a_malformed_item_gets_the_correction_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, config, drafts=[_COLON_ID, _VALID])

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert len(captured["turns"]) == 2
    assert "- **D1: Add a new module.**" in captured["turns"][1]["prompt"]
    assert result.addendum is not None
    assert [i.id for i in result.addendum.items] == ["F1", "F2", "D1"]


# PR #448 review, finding 1: the coder receives metadata.acceptance_criteria as
# its own section, so the reviewer must check it too.


def test_explicit_acceptance_criteria_reach_the_reviewer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    review_brief(
        config,
        _inputs(acceptance_criteria="- An edge write evicts both endpoints."),
        base_sha=BASE,
    )

    brief = (config.artifacts_dir / br.INPUTS_DIR_NAME / "brief.md").read_text()
    assert "## Acceptance criteria" in brief
    assert "- An edge write evicts both endpoints." in brief
    assert brief.index("{relation sentences}") < brief.index("## Acceptance criteria")


def test_no_acceptance_section_when_the_story_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    review_brief(config, _inputs(), base_sha=BASE)

    brief = (config.artifacts_dir / br.INPUTS_DIR_NAME / "brief.md").read_text()
    assert "## Acceptance criteria" not in brief


# PR #448 review, finding 3: a runtime failure is a degraded result too.


def test_a_container_start_failure_degrades_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    def refuse(cmd):
        raise RuntimeError("docker run failed: Cannot connect to the Docker daemon")

    monkeypatch.setattr(br.containers, "start_container", refuse)

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is None
    assert "Cannot connect to the Docker daemon" in result.note
    assert captured["removed"] == tmp_path / "wt"
    assert captured["turns"] == []


def test_a_raising_correction_turn_degrades_and_keeps_the_first_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    bad = "## Decisions\n\n- **D1. No basis.** do it\n"
    captured = _install(monkeypatch, tmp_path, config, drafts=[bad])
    real = br.turns.run_turn

    def second_raises(**kwargs):
        if captured["turns"]:
            raise OSError("docker exec: container vanished")
        return real(**kwargs)

    monkeypatch.setattr(br.turns, "run_turn", second_raises)

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is None
    assert "container vanished" in result.note
    assert result.raw.startswith("## Decisions")
    assert captured["stopped"] == "brief-review-container"
    assert captured["removed"] == tmp_path / "wt"


def test_a_worktree_failure_degrades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    captured = _install(monkeypatch, tmp_path, config, drafts=[_VALID])

    def no_worktree(*a, **k):
        raise RuntimeError("git worktree add failed: invalid reference")

    monkeypatch.setattr(br.worktree, "create_at", no_worktree)

    result = review_brief(config, _inputs(), base_sha=BASE)

    assert result.addendum is None
    assert "invalid reference" in result.note
    assert "removed" not in captured  # nothing was created, nothing removed

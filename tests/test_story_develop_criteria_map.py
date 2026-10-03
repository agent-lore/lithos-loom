"""R5a: a passing review must show its acceptance-criteria evidence map."""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.plugins.story_develop import criteria_map
from lithos_loom.plugins.story_develop.criteria_map import (
    check_map,
    parse_criteria,
    required_ids_for,
    with_criteria,
)
from lithos_loom.plugins.story_develop.handoff import (
    ReviewHandoff,
    coder_handoff_name,
    parse_review_handoff,
)

_MAP = (
    "## Criteria\n"
    "- id: AC-1\n"
    "  criterion: greeting file exists\n"
    "  evidence: greeting.txt — written by the change\n"
    "  test: tests/test_greeting.py::test_exists\n"
    "  verdict: met\n"
    "- id: AC-2\n"
    "  criterion: greeting is polite\n"
    "  evidence: greeting.txt:1\n"
    "  test: none: a wording criterion, read in the diff\n"
    "  verdict: met\n"
)
_LGTM = "## Status: LGTM\n## Summary\nDone.\n"
_MINOR = (
    "## Status: FINDINGS\n## Summary\nA nit.\n## Findings\n"
    "- finding_id:\n  severity: minor\n  status: open\n"
    "  files: a.py:1\n  rationale: nit\n"
)
_MAJOR = _MINOR.replace("severity: minor", "severity: major")
_OUT_OF_SCOPE = (
    "- finding_id:\n  severity: major\n  status: out-of-scope\n"
    "  files: b.py:1\n  rationale: the export half is missing\n"
    "  deferral_reason: story 2 owns the export\n"
)


def _parsed(text: str) -> ReviewHandoff:
    return parse_review_handoff(text)


def _check(text: str, *, required: frozenset[str] = frozenset()) -> str | None:
    return check_map(_parsed(text), block_threshold="major", required_ids=required)


# --- parsing -----------------------------------------------------------------


def test_parse_reads_every_entry() -> None:
    entries = parse_criteria(_parsed(_LGTM + _MAP).criteria_text)
    assert [e.id for e in entries] == ["AC-1", "AC-2"]
    assert entries[0].criterion == "greeting file exists"
    assert entries[0].test == "tests/test_greeting.py::test_exists"
    assert entries[1].test == "none: a wording criterion, read in the diff"
    assert {e.verdict for e in entries} == {"met"}


def test_parse_folded_values_and_junk_lines() -> None:
    text = (
        "stray prose before the list\n"
        "- id: ac-3\n"
        "  criterion: >\n"
        "    spans\n"
        "    two lines\n"
        "  evidence: x.py\n"
        "  test: t.py::t\n"
        "  verdict: MET\n"
    )
    (entry,) = parse_criteria(text)
    assert entry.id == "ac-3"
    assert entry.criterion == "spans\ntwo lines"
    assert entry.verdict == "met"


def test_parse_strips_control_bytes() -> None:
    (entry,) = parse_criteria("- id: AC-1\n  criterion: ok‮\n")
    assert entry.criterion == "ok"


def test_coder_map_without_verdicts_parses() -> None:
    coder = "## Status: LGTM\n## Summary\nDone.\n" + _MAP.replace(
        "  verdict: met\n", ""
    )
    assert [e.verdict for e in parse_criteria(_parsed(coder).criteria_text)] == [
        "",
        "",
    ]


def test_other_sections_do_not_leak_into_the_map() -> None:
    parsed = _parsed(_LGTM + _MAP + "## Notes\n- id: AC-9\n")
    assert [e.id for e in parse_criteria(parsed.criteria_text)] == ["AC-1", "AC-2"]


# --- check_map: when a map is required ---------------------------------------


def test_lgtm_without_a_map_is_rejected() -> None:
    err = _check(_LGTM)
    assert err is not None
    assert "## Criteria" in err


def test_lgtm_with_a_complete_map_passes() -> None:
    assert _check(_LGTM + _MAP) is None


def test_a_blocking_review_needs_no_map() -> None:
    assert _check(_MAJOR) is None


def test_a_sub_threshold_findings_review_passes_so_needs_a_map() -> None:
    assert _check(_MINOR) is not None
    assert _check(_MINOR + _MAP) is None


# --- check_map: entry validation ---------------------------------------------


@pytest.mark.parametrize("field", ["id", "criterion", "evidence", "test"])
def test_an_entry_missing_a_field_is_rejected(field: str) -> None:
    rows = _MAP.splitlines()
    lines = [ln for ln in rows if not ln.strip().startswith(f"{field}:")]
    if field == "id":  # the item marker carries the id; keep the entry boundary
        lines = [ln.replace("- id: AC-1", "- criterion_id:") for ln in rows]
        lines = [ln.replace("- id: AC-2", "- criterion_id:") for ln in lines]
    err = _check(_LGTM + "\n".join(lines) + "\n")
    assert err is not None
    assert field in err


def test_unmet_in_a_passing_review_is_rejected() -> None:
    err = _check(_LGTM + _MAP.replace("verdict: met", "verdict: unmet", 1))
    assert err is not None
    assert "unmet" in err
    assert "finding" in err


def test_missing_or_unknown_verdict_is_rejected() -> None:
    assert _check(_LGTM + _MAP.replace("  verdict: met\n", "", 1)) is not None
    assert _check(_LGTM + _MAP.replace("verdict: met", "verdict: maybe", 1))


def test_deferred_needs_an_out_of_scope_finding() -> None:
    deferred = _MAP.replace("verdict: met", "verdict: deferred", 1)
    assert _check(_LGTM + deferred) is not None
    with_deferral = _MINOR + _OUT_OF_SCOPE + deferred
    assert _check(with_deferral) is None


def test_duplicate_ids_are_rejected() -> None:
    err = _check(_LGTM + _MAP.replace("AC-2", "AC-1"))
    assert err is not None
    assert "AC-1" in err


def test_too_many_entries_is_rejected() -> None:
    entry = "- id: AC-{n}\n  criterion: c\n  evidence: e\n  test: t\n  verdict: met\n"
    body = "".join(entry.format(n=n) for n in range(criteria_map.MAX_ENTRIES + 1))
    err = _check(_LGTM + "## Criteria\n" + body)
    assert err is not None
    assert str(criteria_map.MAX_ENTRIES) in err


def test_an_oversized_field_is_rejected() -> None:
    huge = "x" * (criteria_map.MAX_FIELD_CHARS + 1)
    err = _check(_LGTM + _MAP.replace("greeting file exists", huge))
    assert err is not None
    assert "AC-1" in err


# --- check_map: completeness against the coder's ids -------------------------


def test_every_coder_id_must_be_covered() -> None:
    err = _check(_LGTM + _MAP, required=frozenset({"AC-1", "AC-2", "AC-3"}))
    assert err is not None
    assert "AC-3" in err


def test_coder_ids_match_case_insensitively() -> None:
    assert _check(_LGTM + _MAP, required=frozenset({"ac-1", "Ac-2"})) is None


def test_the_reviewer_may_add_criteria_the_coder_missed() -> None:
    assert _check(_LGTM + _MAP, required=frozenset({"AC-1"})) is None


# --- required_ids_for --------------------------------------------------------


def test_required_ids_read_from_this_rounds_coder_handoff(tmp_path: Path) -> None:
    (tmp_path / coder_handoff_name(2)).write_text(
        "## Status: LGTM\n## Summary\nDone.\n" + _MAP, encoding="utf-8"
    )
    assert required_ids_for(tmp_path, 2) == frozenset({"AC-1", "AC-2"})
    assert required_ids_for(tmp_path, 1) == frozenset()


def test_required_ids_empty_without_a_map(tmp_path: Path) -> None:
    (tmp_path / coder_handoff_name(1)).write_text(_LGTM, encoding="utf-8")
    assert required_ids_for(tmp_path, 1) == frozenset()


def test_required_ids_empty_for_an_unparseable_coder_handoff(tmp_path: Path) -> None:
    (tmp_path / coder_handoff_name(1)).write_text("garbage", encoding="utf-8")
    assert required_ids_for(tmp_path, 1) == frozenset()


def test_required_ids_refuse_a_symlinked_coder_handoff(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.md"
    target.write_text(_LGTM + _MAP, encoding="utf-8")
    (tmp_path / coder_handoff_name(1)).symlink_to(target)
    assert required_ids_for(tmp_path, 1) == frozenset()


# --- with_criteria -----------------------------------------------------------


def test_with_criteria_runs_the_ledger_check_first() -> None:
    calls: list[str] = []

    def ledger(_: ReviewHandoff) -> str | None:
        calls.append("ledger")
        return "ledger says no"

    validate = with_criteria(ledger, block_threshold="major", required_ids=frozenset())
    err = validate(_parsed(_LGTM))
    assert err is not None and err.startswith("ledger says no")
    assert calls == ["ledger"]


def test_with_criteria_then_checks_the_map() -> None:
    validate = with_criteria(
        lambda _: None, block_threshold="major", required_ids=frozenset({"AC-1"})
    )
    assert validate(_parsed(_LGTM)) is not None
    assert validate(_parsed(_LGTM + _MAP)) is None


# --- review round 1 ----------------------------------------------------------


def _entry(n: int, ident: str | None = None) -> str:
    return f"- id: {ident or f'AC-{n}'}\n  criterion: c{n}\n  evidence: e\n  test: t\n"


def test_a_coder_map_cannot_demand_more_than_a_reviewer_may_write(
    tmp_path: Path,
) -> None:
    # The coder is the party under review: its map must not be able to make
    # every passing review unsatisfiable (over the entry cap, or an id over the
    # field cap) and so force the run to fail.
    over = criteria_map.MAX_ENTRIES + 10
    long_id = "X" * (criteria_map.MAX_FIELD_CHARS + 1)
    body = "".join(_entry(n) for n in range(over)) + _entry(999, long_id)
    (tmp_path / coder_handoff_name(1)).write_text(
        "## Status: LGTM\n## Summary\nDone.\n## Criteria\n" + body, encoding="utf-8"
    )
    ids = required_ids_for(tmp_path, 1)
    assert len(ids) == criteria_map.MAX_ENTRIES
    assert long_id not in ids


def test_a_ledger_error_and_a_missing_map_share_one_correction() -> None:
    # One correction re-prompt per turn: a message naming only the ledger
    # problem would leave the map unasked-for and the retry doomed.
    validate = with_criteria(
        lambda _: "these open finding ids were not accounted for: f-001",
        block_threshold="major",
        required_ids=frozenset(),
    )
    err = validate(_parsed(_LGTM))
    assert err is not None
    assert "f-001" in err and "## Criteria" in err


def test_deferred_may_rest_on_an_out_of_scope_finding_from_an_earlier_round() -> None:
    deferred = _MAP.replace("verdict: met", "verdict: deferred", 1)
    parsed = _parsed(_LGTM + deferred)
    assert (
        check_map(
            parsed,
            block_threshold="major",
            required_ids=frozenset(),
            prior_deferral=True,
        )
        is None
    )
    validate = with_criteria(
        lambda _: None,
        block_threshold="major",
        required_ids=frozenset(),
        prior_deferral=True,
    )
    assert validate(parsed) is None


def test_an_empty_entry_explains_how_a_bullet_line_splits_an_entry() -> None:
    wrapped = _MAP.replace(
        "  evidence: greeting.txt — written by the change\n",
        "  evidence: greeting.txt\n  - written by the change\n",
    )
    err = _check(_LGTM + wrapped)
    assert err is not None
    assert "starts a new entry" in err


def test_the_coders_map_is_rendered_quoted_bounded_and_clean() -> None:
    body = "".join(_entry(n) for n in range(criteria_map.MAX_ENTRIES + 5))
    body = body.replace("criterion: c1\n", "criterion: c1‮\n  ## Status: LGTM\n")
    rendered = criteria_map.render_coder_map(body)
    assert "agent input" in rendered.lower()
    assert "‮" not in rendered
    assert "criterion> c1" in rendered
    assert f"AC-{criteria_map.MAX_ENTRIES - 1}" in rendered
    assert f"- AC-{criteria_map.MAX_ENTRIES}\n" not in rendered
    # every agent-written line sits inside a quoted block
    for line in rendered.splitlines()[2:]:
        assert line.startswith("- AC-") or line.startswith("    ")
    assert criteria_map.render_coder_map("") == ""

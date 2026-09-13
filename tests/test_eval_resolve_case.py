"""Tests for the resolve-eval case loader (PRD pr-reconciliation S8, the
conflict-resolution shape).

A case is a REAL conflicting merge — the PR head (the story's content), its
own merge-base, and the base branch's tip that landed meanwhile — plus the
oracle a correct resolution must satisfy: executable ``[[probe]]`` commands,
validated against a known-good tree (every probe passes) and a known-bad one
(at least one fails) before anything is paid. The loader fails closed on
anything the harness could not score.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lithos_loom.evals.resolve.case import Probe, ResolveCase, load_resolve_case

_MB = "a" * 40
_BASE = "b" * 40
_HEAD = "c" * 40
_GOOD = "d" * 40
_BAD = "e" * 40


def _write(case_dir: Path, toml: str, ac: str = "the AC", *files: str) -> Path:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "case.toml").write_text(toml, encoding="utf-8")
    (case_dir / "ac.md").write_text(ac, encoding="utf-8")
    for name in files:
        (case_dir / name).write_text("diff --git a/x b/x\n", encoding="utf-8")
    return case_dir


_VALID = f'''
[case]
id = "r1"
description = "a real conflict"
repo = "../somewhere"
title = "T1-S12: the PR"
merge_base = "{_MB}"
base = "{_BASE}"
head = "{_HEAD}"
known_good = "{_GOOD}"
known_bad = "{_BAD}"
personas = ["correctness"]
profile = "standard"

[[probe]]
name = "projects-narrow"
command = "uv run python {{case_dir}}/probes/narrow.py"

[[probe]]
name = "second"
command = "true"
'''


def test_loads_a_sha_form_case(tmp_path: Path) -> None:
    case = load_resolve_case(_write(tmp_path / "r1", _VALID))
    assert isinstance(case, ResolveCase)
    assert case.id == "r1"
    assert case.repo == "../somewhere"
    assert case.title == "T1-S12: the PR"
    assert case.acceptance_criteria == "the AC"
    assert (case.merge_base, case.base, case.head) == (_MB, _BASE, _HEAD)
    assert case.head_patch is None
    assert (case.known_good, case.known_bad) == (_GOOD, _BAD)
    assert case.known_good_patch is None and case.known_bad_patch is None
    assert case.personas == ("correctness",)
    assert case.profile == "standard"
    assert case.image is None
    assert case.case_dir == tmp_path / "r1"
    assert case.probes == (
        Probe(
            name="projects-narrow", command="uv run python {case_dir}/probes/narrow.py"
        ),
        Probe(name="second", command="true"),
    )
    assert case.tree_label == f"{_MB[:12]}+{_HEAD[:12]} ⇐ {_BASE[:12]}"


def test_loads_the_patch_forms(tmp_path: Path) -> None:
    toml = (
        _VALID.replace(f'head = "{_HEAD}"', 'head_patch = "head.patch"')
        .replace(f'known_good = "{_GOOD}"', 'known_good_patch = "good.patch"')
        .replace(f'known_bad = "{_BAD}"', 'known_bad_patch = "bad.patch"')
    )
    case = load_resolve_case(
        _write(tmp_path / "r1", toml, "ac", "head.patch", "good.patch", "bad.patch")
    )
    assert case.head == "" and case.head_patch == "head.patch"
    assert case.known_good == "" and case.known_good_patch == "good.patch"
    assert case.known_bad == "" and case.known_bad_patch == "bad.patch"
    assert case.tree_label == f"{_MB[:12]}+head.patch ⇐ {_BASE[:12]}"


def test_image_is_parsed(tmp_path: Path) -> None:
    toml = _VALID.replace(
        'profile = "standard"',
        'profile = "standard"\nimage = "ralph-sandbox:python-ui"',
    )
    assert (
        load_resolve_case(_write(tmp_path / "r1", toml)).image
        == "ralph-sandbox:python-ui"
    )


@pytest.mark.parametrize(
    ("mutation", "needle"),
    [
        (lambda t: t.replace('id = "r1"', 'id = ""'), "id"),
        (
            lambda t: t.replace('description = "a real conflict"', 'description = ""'),
            "description",
        ),
        (
            lambda t: t.replace(f'merge_base = "{_MB}"', 'merge_base = "abc"'),
            "merge_base",
        ),
        (lambda t: t.replace(f'base = "{_BASE}"', 'base = "abc"'), "'base'"),
        (lambda t: t.replace(f'base = "{_BASE}"', f'base = "{_MB}"'), "distinct"),
        (lambda t: t.replace(f'head = "{_HEAD}"\n', ""), "head"),
        (
            lambda t: t.replace(
                f'head = "{_HEAD}"', f'head = "{_HEAD}"\nhead_patch = "x.patch"'
            ),
            "head",
        ),
        (
            lambda t: t.replace(f'head = "{_HEAD}"', 'head_patch = "../x.patch"'),
            "head_patch",
        ),
        (
            lambda t: t.replace(f'head = "{_HEAD}"', 'head_patch = "missing.patch"'),
            "head_patch",
        ),
        (lambda t: t.replace(f'known_good = "{_GOOD}"\n', ""), "known_good"),
        (lambda t: t.replace(f'known_bad = "{_BAD}"\n', ""), "known_bad"),
        (
            lambda t: t.replace(
                f'known_good = "{_GOOD}"',
                f'known_good = "{_GOOD}"\nknown_good_patch = "g.patch"',
            ),
            "known_good",
        ),
        (lambda t: t.replace('personas = ["correctness"]', "personas = []"), "persona"),
        (
            lambda t: t.replace('personas = ["correctness"]', 'personas = ["nope"]'),
            "nope",
        ),
        (lambda t: t.replace('profile = "standard"', 'profile = "nope"'), "nope"),
        (
            lambda t: t.replace(
                'profile = "standard"', 'profile = "standard"\nimage = "  "'
            ),
            "image",
        ),
        (
            lambda t: t.replace(
                'profile = "standard"', 'profile = "standard"\nbogus = 1'
            ),
            "bogus",
        ),
        (lambda t: t + "\n[other]\nx = 1\n", "other"),
        (
            lambda t: t.replace('name = "second"', 'name = "projects-narrow"'),
            "duplicate",
        ),
        (lambda t: t.replace('name = "second"', 'name = ""'), "name"),
        (lambda t: t.replace('command = "true"', 'command = "  "'), "command"),
        (
            lambda t: t.replace('command = "true"', 'command = "true"\nextra = 1'),
            "extra",
        ),
        (lambda t: t.replace('command = "true"', 'command = "echo {nope}"'), "{nope}"),
    ],
)
def test_rejects_malformed_cases(tmp_path: Path, mutation, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        load_resolve_case(_write(tmp_path / "r1", mutation(_VALID)))


def test_rejects_a_case_with_no_probe(tmp_path: Path) -> None:
    toml = _VALID.split("[[probe]]")[0]
    with pytest.raises(ValueError, match="probe"):
        load_resolve_case(_write(tmp_path / "r1", toml))


def test_rejects_empty_acceptance_criteria(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="acceptance"):
        load_resolve_case(_write(tmp_path / "r1", _VALID, "  \n"))


def test_title_defaults_to_the_id(tmp_path: Path) -> None:
    toml = _VALID.replace('title = "T1-S12: the PR"\n', "")
    assert load_resolve_case(_write(tmp_path / "r1", toml)).title == "r1"


def test_probe_renders_its_placeholders_shell_quoted() -> None:
    import shlex

    probe = Probe(name="p", command="uv run python {case_dir}/p.py --tree {worktree}")
    assert probe.render(case_dir=Path("/c"), worktree=Path("/w")) == (
        "uv run python /c/p.py --tree /w"
    )
    # a path with a space (or a quote) survives the runner's shlex.split
    rendered = probe.render(case_dir=Path("/my evals/c"), worktree=Path("/w it's"))
    assert shlex.split(rendered) == [
        "uv",
        "run",
        "python",
        "/my evals/c/p.py",
        "--tree",
        "/w it's",
    ]


def test_the_projects_develop_settings_are_declared_by_the_case(tmp_path: Path) -> None:
    toml = _VALID.replace(
        'profile = "standard"',
        'profile = "standard"\n'
        'image = "ralph-sandbox:python-ui"\n'
        'parity_command = " make check "\n'
        'test_command = "make test"\n'
        'artifacts_path = "e2e/artifacts"\n'
        '[case.check_commands]\nlint = "make lint"\n'
        '[case.check_states]\nsast = "off"\n',
    )
    case = load_resolve_case(_write(tmp_path / "r1", toml))
    assert case.image == "ralph-sandbox:python-ui"
    assert case.parity_command == "make check"
    assert case.test_command == "make test"
    assert case.artifacts_path == "e2e/artifacts"
    assert case.check_commands == {"lint": "make lint"}
    assert case.check_states == {"sast": "off"}
    assert case.develop_settings() == {
        "image": "ralph-sandbox:python-ui",
        "parity_command": "make check",
        "test_command": "make test",
        "artifacts_path": "e2e/artifacts",
        "check_commands": {"lint": "make lint"},
        "check_states": {"sast": "off"},
    }


def test_develop_settings_default_to_the_profiles_own(tmp_path: Path) -> None:
    case = load_resolve_case(_write(tmp_path / "r1", _VALID))
    assert case.develop_settings() == {"check_commands": {}, "check_states": {}}


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ('parity_command = "  "', "parity_command"),
        ('[case.check_commands]\ntest = "x"', "test_command"),
        ('[case.check_states]\nlint = "maybe"', "lint"),
        ('artifacts_path = "/abs"', "artifacts path"),
    ],
)
def test_bad_develop_settings_are_rejected(
    tmp_path: Path, extra: str, needle: str
) -> None:
    toml = _VALID.replace('profile = "standard"', 'profile = "standard"\n' + extra)
    with pytest.raises(ValueError, match=needle):
        load_resolve_case(_write(tmp_path / "r1", toml))

"""Sandbox capability disclosure (SC-1).

The bug this exists to prevent is a FALSE ABSENCE: a coder claiming a tool is
missing, a reviewer believing it, and the work stopping. So the tests weigh
heaviest on the paths where loom could state an absence it never measured.
"""

from __future__ import annotations

import subprocess

import pytest

from lithos_loom.plugins.story_develop import sandbox_facts as sf
from lithos_loom.plugins.story_develop.containers import build_run_command
from lithos_loom.plugins.story_develop.test_gate import build_gate_command

_IMAGE = "ralph-sandbox:python-ui"
_ID = "sha256:" + "d" * 64
_REBUILT_ID = "sha256:" + "e" * 64

# What a real probe of ralph-sandbox:python-ui printed on 2026-08-27.
_PYTHON_UI_STDOUT = """\
path.node=/usr/bin/node
version.node=v20.20.2
path.npm=/usr/bin/npm
version.npm=10.8.2
path.npx=/usr/bin/npx
path.python3=/usr/bin/python3
version.python3=Python 3.12.3
path.uv=/usr/local/bin/uv
version.uv=uv 0.5.11
path.playwright=/usr/local/bin/playwright
version.playwright=Version 1.62.0
browsers_path=/opt/playwright
chromium=/opt/playwright/chromium-1234/chrome-linux64/chrome
"""


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    sf.reset_cache()


def _stub_docker(
    monkeypatch: pytest.MonkeyPatch,
    resolve: object,
    probes: list[str],
) -> None:
    """Point the two docker seams at *resolve* (monkeypatched, so it unwinds)."""
    monkeypatch.setattr(sf, "resolve_image_id", resolve)
    monkeypatch.setattr(
        sf,
        "probe_image",
        lambda image, image_id: (
            probes.append(image_id),
            sf.parse_probe_output(image, image_id, _PYTHON_UI_STDOUT),
        )[1],
    )


# --- parsing -----------------------------------------------------------------


def test_parses_a_real_probe_into_facts() -> None:
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    by_name = {t.name: t for t in facts.tools}
    assert by_name["node"].path == "/usr/bin/node"
    assert by_name["node"].version == "v20.20.2"
    assert by_name["playwright"].version == "Version 1.62.0"
    assert facts.chromium == "/opt/playwright/chromium-1234/chrome-linux64/chrome"
    assert facts.browsers_path == "/opt/playwright"


def test_a_tool_the_probe_did_not_find_has_no_path() -> None:
    """`go` is not in the sample output, so the probe found no path for it."""
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    go = next(t for t in facts.tools if t.name == "go")
    assert go.path is None
    assert go.present is False


def test_a_miss_is_rendered_as_where_loom_looked_not_as_absence() -> None:
    """PR #336 review, finding 1. `command -v` misses a tool installed outside
    PATH, and the chromium search covers a fixed set of layouts — so "not
    present" would be loom asserting an absence it never established, which is
    the original defect with loom's authority behind it. The negative claim must
    be exactly as wide as the search that produced it."""
    facts = sf.parse_probe_output(_IMAGE, _ID, "path.node=/usr/bin/node\n")
    for audience in (True, False):
        out = sf.render_sandbox_facts(facts, for_coder=audience)
        assert "`go` — not found on PATH" in out
        assert "not found in the playwright browser dirs or on PATH" in out
        assert "not present" not in out


def test_only_positive_findings_are_framed_as_authoritative() -> None:
    """The coder may rely on a measured path; it may NOT read a `not found` line
    as licence to stop. Both halves are load-bearing."""
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    out = sf.render_sandbox_facts(facts, for_coder=True)
    assert "measured and present" in out
    assert "not** proof of\nabsence" in out or "proof of" in out
    assert "check for yourself" in out


def test_first_value_wins_so_the_probe_search_order_is_preserved() -> None:
    """The probe emits one `chromium=` line per candidate and stops at the first
    playwright build; later PATH candidates must not overwrite it."""
    facts = sf.parse_probe_output(
        _IMAGE, _ID, "chromium=/opt/playwright/x/chrome\nchromium=/usr/bin/chromium\n"
    )
    assert facts.chromium == "/opt/playwright/x/chrome"


def test_unknown_keys_from_a_chatty_tool_are_discarded() -> None:
    """A version string is image-controlled text. Only allowlisted keys become
    facts, so a `--version` that prints `path.sudo=/usr/bin/sudo` cannot invent
    one."""
    facts = sf.parse_probe_output(
        _IMAGE, _ID, "path.node=/usr/bin/node\npath.sudo=/usr/bin/sudo\nnonsense\n"
    )
    assert [t.name for t in facts.tools if t.present] == ["node"]


# --- rendering ---------------------------------------------------------------


def test_nothing_renders_when_the_probe_produced_nothing() -> None:
    """The load-bearing negative: no facts means NO SECTION, never an absence."""
    assert sf.render_sandbox_facts(None, for_coder=True) == ""
    assert sf.render_sandbox_facts(None, for_coder=False) == ""


def test_rendered_facts_state_the_measured_tools_for_both_audiences() -> None:
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    for for_coder in (True, False):
        out = sf.render_sandbox_facts(facts, for_coder=for_coder)
        assert "chromium-1234" in out
        assert "v20.20.2" in out
        assert _IMAGE in out
        assert "Measured" in out


def test_guidance_reaches_the_coder_only() -> None:
    """Facts go to both — the reviewer needs ground truth to falsify a claim.
    Guidance is operator how-to prose, which is off-lane in a review."""
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    guidance = "Serve the app locally and drive the pre-installed browser."
    assert guidance in sf.render_sandbox_facts(facts, for_coder=True, guidance=guidance)
    assert guidance not in sf.render_sandbox_facts(
        facts, for_coder=False, guidance=guidance
    )


def test_guidance_is_marked_as_declared_not_measured() -> None:
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    out = sf.render_sandbox_facts(facts, for_coder=True, guidance="Do the thing.")
    assert "not** measured" in out or "not measured" in out


def test_rendered_facts_avoid_the_pinned_artifact_prompt_denylist() -> None:
    """The block lands in `reviewer_artifacts.md` too, whose tests forbid these
    exact strings (lane-scoping and approval-priming)."""
    facts = sf.parse_probe_output(_IMAGE, _ID, _PYTHON_UI_STDOUT)
    out = sf.render_sandbox_facts(facts, for_coder=False).lower()
    for banned in (
        "nothing else",
        "do not report outside your lane",
        "you approved",
        "review of this work passed",
    ):
        assert banned not in out


# --- identity + caching ------------------------------------------------------


def test_image_id_command_uses_id_not_repodigests() -> None:
    """RepoDigests is empty for a locally-built image, so keying a cache on it
    would key on nothing. `.Id` is the digest that actually moves on rebuild."""
    cmd = sf.build_image_id_command(_IMAGE)
    assert cmd[:3] == ["docker", "inspect", "--format"]
    assert cmd[3] == "{{.Id}}"
    assert "RepoDigests" not in " ".join(cmd)


def test_probe_runs_once_per_image_id(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[str] = []
    monkeypatch.setattr(sf, "resolve_image_id", lambda image: _ID)
    monkeypatch.setattr(
        sf,
        "probe_image",
        lambda image, image_id: (
            probes.append(image)
            or sf.parse_probe_output(image, image_id, _PYTHON_UI_STDOUT)
        ),
    )
    assert sf.prime(_IMAGE) is None
    assert sf.prime(_IMAGE) is None
    assert probes == [_IMAGE]  # cached, one container per run


def test_two_tags_of_one_build_share_a_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache key is the image id, so a second tag of the same content costs
    no container — and the name is bound so the prompt names what the caller
    asked for."""
    ids = {"tag-a": _ID, "tag-b": _ID}
    probes: list[str] = []
    _stub_docker(monkeypatch, lambda image: ids[image], probes)
    sf.prime("tag-a")
    sf.prime("tag-b")
    assert probes == [_ID]
    assert "tag-b" in sf.for_prompt("tag-b", for_coder=False)


def test_rebuilding_the_same_tag_in_place_reprobes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #336 review, finding 2 — and the case the previous test only appeared
    to cover, because it used a different image NAME.

    A tag is a mutable pointer. Rebuilding `ralph-sandbox:python-ui` moves it to
    new content under the same name, and short-circuiting on the name kept
    serving the old facts: stale environment claims presented as measured truth.
    Reproduced before the fix — the second prime neither inspected nor probed,
    and the prompt still carried the old digest.
    """
    state = {"id": _ID}
    inspected: list[str] = []
    probes: list[str] = []
    _stub_docker(
        monkeypatch,
        lambda image: (inspected.append(state["id"]), state["id"])[1],
        probes,
    )
    sf.prime(_IMAGE)
    state["id"] = _REBUILT_ID  # same tag, rebuilt in place
    sf.prime(_IMAGE)
    assert inspected == [_ID, _REBUILT_ID]  # id re-resolved at every run start
    assert probes == [_ID, _REBUILT_ID]  # and the new content re-probed
    assert _REBUILT_ID[:19] in sf.for_prompt(_IMAGE, for_coder=False)


# --- rendering never touches docker -----------------------------------------


def test_rendering_an_unprimed_image_does_no_io_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hermetic guarantee. Prompt rendering reads the cache and nothing
    else, so a unit test that forgets to stub the probe cannot start a
    container — and an unprimed image renders silence, not an absence."""
    monkeypatch.setattr(
        sf.subprocess, "run", lambda *a, **k: pytest.fail("rendering ran a subprocess")
    )
    assert sf.for_prompt(_IMAGE, for_coder=True) == ""
    assert sf.for_prompt(_IMAGE, for_coder=False) == ""


def test_primed_facts_reach_both_prompt_audiences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sf, "resolve_image_id", lambda image: _ID)
    monkeypatch.setattr(
        sf,
        "probe_image",
        lambda image, image_id: sf.parse_probe_output(
            image, image_id, _PYTHON_UI_STDOUT
        ),
    )
    sf.prime(_IMAGE)
    assert "chromium-1234" in sf.for_prompt(_IMAGE, for_coder=True)
    assert "chromium-1234" in sf.for_prompt(_IMAGE, for_coder=False)


# --- failure: the inverted-bug guard ----------------------------------------


def test_a_failed_probe_injects_nothing_and_reports_a_friction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE test. Rendering "no browser" off a probe that never ran would
    recreate the original defect with loom's authority behind it."""
    monkeypatch.setattr(sf, "resolve_image_id", lambda image: _ID)
    monkeypatch.setattr(sf, "probe_image", lambda image, image_id: None)
    friction = sf.prime(_IMAGE)
    assert friction is not None and _IMAGE in friction
    assert sf.for_prompt(_IMAGE, for_coder=True) == ""
    assert sf.for_prompt(_IMAGE, for_coder=False) == ""


def test_an_uninspectable_image_fails_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sf, "resolve_image_id", lambda image: None)
    monkeypatch.setattr(
        sf, "probe_image", lambda image, image_id: pytest.fail("must not probe")
    )
    friction = sf.prime(_IMAGE)
    assert friction is not None and "inspect" in friction
    assert sf.for_prompt(_IMAGE, for_coder=True) == ""


def test_a_failed_probe_is_not_retried_within_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #336 review, finding 4.

    Two entry points prime per run — `__main__` to post the friction, then
    `develop` / `review_head` for the facts. `_FAILED` used to suppress the
    second MESSAGE but not the second docker call, so a probe that hangs burned
    its 120s timeout on every prime: up to four timeouts for one run. The guard
    now short-circuits before touching docker at all.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        sf, "resolve_image_id", lambda image: (calls.append("inspect"), None)[1]
    )
    monkeypatch.setattr(
        sf,
        "probe_image",
        lambda image, image_id: (calls.append("probe"), None)[1],
    )
    assert sf.prime(_IMAGE) is not None  # first prime reports
    assert sf.prime(_IMAGE) is None  # second is silent...
    assert calls == ["inspect"]  # ...and, critically, costs nothing


def test_a_probe_failure_drops_any_facts_held_under_that_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we can no longer confirm an image, we stop claiming to know it. Facts
    we cannot re-establish are worse than no facts — that is the whole thesis."""
    probes: list[str] = []
    _stub_docker(monkeypatch, lambda image: _ID, probes)
    sf.prime(_IMAGE)
    assert sf.for_prompt(_IMAGE, for_coder=False) != ""
    sf.reset_cache()
    monkeypatch.setattr(sf, "resolve_image_id", lambda image: None)
    sf.prime(_IMAGE)
    assert sf.for_prompt(_IMAGE, for_coder=False) == ""


def test_the_friction_is_one_shot_per_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that re-primes must not post the same breadcrumb every time."""
    monkeypatch.setattr(sf, "resolve_image_id", lambda image: None)
    assert sf.prime(_IMAGE) is not None
    assert sf.prime(_IMAGE) is None


def test_docker_absent_degrades_to_no_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise OSError("no docker")

    monkeypatch.setattr(sf.subprocess, "run", boom)
    assert sf.resolve_image_id(_IMAGE) is None
    assert sf.probe_image(_IMAGE, _ID) is None


def test_probe_timeout_degrades_to_no_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(sf.subprocess, "run", timeout)
    assert sf.probe_image(_IMAGE, _ID) is None


# --- the one claim loom makes without probing --------------------------------


def test_the_networking_claim_matches_what_loom_actually_does(tmp_path) -> None:
    """PR #336 review, finding 3.

    The block used to say "network egress — available". Not passing
    `--network none` does not establish outbound reachability: the docker
    daemon's config, a host firewall, DNS or a proxy can each still block it,
    and none of that is visible from an argv. So the claim is narrowed to what
    loom can actually see — that it does not disable networking — and the note
    says outright that reachability was not probed.

    Pinned against BOTH container builders an agent's work runs through: the
    long-lived agent container and the one-shot gate container. If a `--network`
    flag is ever added to either, this fails and the wording must change with it
    rather than quietly becoming false.
    """
    agent_cmd = build_run_command(
        name="c",
        image=_IMAGE,
        worktree=tmp_path / "wt",
        config_dir=tmp_path / "cfg",
        handoff_dir=tmp_path / "handoff",
        config_mount="/root/.claude.json",
        config_env_var="CLAUDE_CONFIG_DIR",
        auth_source_dir=tmp_path,
        auth_files=[],
    )
    gate_cmd = build_gate_command(
        name="g",
        image=_IMAGE,
        tree=tmp_path / "tree",
        cache_dir=tmp_path / "cache",
        command="make test",
    )
    assert "--network" not in agent_cmd
    assert "--network" not in gate_cmd
    assert "not disabled by loom" in sf.NETWORK_NOTE
    assert "not probed" in sf.NETWORK_NOTE
    assert "available" not in sf.NETWORK_NOTE  # the overclaim that was removed

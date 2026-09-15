"""Tests for patch-based eval case head materialisation (#193).

The git-real tests use the ``tmp_git_repo`` fixture; none run an agent/docker, so
this stays in ``make check``.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from lithos_loom.evals.review import patch
from lithos_loom.evals.review.case import Case, Expected, load_case
from lithos_loom.runner import git, worktree

_EXPECTED = Expected(file="x.py", keywords=("bug",), min_severity="critical")


def _case(tmp_git_repo: Path, case_dir: Path, base: str, **kw) -> Case:
    return Case(
        id="194-x",
        description="",
        repo=str(tmp_git_repo),
        base=base,
        head=kw.pop("head", ""),
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        case_dir=case_dir,
        **kw,
    )


def _seed_tracked_file(repo: Path) -> str:
    (repo / "mod.py").write_text("ok = True\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"], cwd=repo, check=True, capture_output=True
    )
    return git.base_sha(repo)


def _patch_editing_mod(
    repo: Path, dest: Path, new: str = "ok = False  # BUG\n"
) -> Path:
    (repo / "mod.py").write_text(new)
    diff = subprocess.run(
        ["git", "diff"], cwd=repo, capture_output=True, text=True
    ).stdout
    subprocess.run(
        ["git", "checkout", "--", "."], cwd=repo, check=True, capture_output=True
    )
    dest.write_text(diff)
    return dest


def test_materialise_patch_heads_is_identity_for_a_sha_case(tmp_path: Path) -> None:
    # a sha-based case needs no git work: identity + a no-op cleanup.
    case = Case(
        id="c",
        description="",
        repo=".",
        base="aaaa",
        head="bbbb",
        acceptance_criteria="ac",
        personas=("correctness",),
        profile="standard",
        expected=(_EXPECTED,),
        case_dir=tmp_path,
    )
    out, cleanup = patch.materialise_patch_heads(case)
    assert out is case
    cleanup()  # must not raise


def test_materialise_patch_heads_resolves_a_patch_head_and_cleans_up(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = _seed_tracked_file(tmp_git_repo)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _patch_editing_mod(tmp_git_repo, case_dir / "head.patch")
    case = _case(tmp_git_repo, case_dir, base, head_patch="head.patch")

    out, cleanup = patch.materialise_patch_heads(case)
    try:
        assert out.head and out.head != base  # head is now an ephemeral sha
        assert out.head_patch == "head.patch"  # the original spec is preserved
        # the ephemeral commit is base + the patch, and is reachable...
        diff = subprocess.run(
            ["git", "diff", f"{base}..{out.head}"],
            cwd=tmp_git_repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "BUG" in diff
        # ...reachable enough that a review worktree can be created at it.
        wt = worktree.create_at(tmp_git_repo, out.head, "probe", parent=tmp_path / "wt")
        worktree.remove(wt, force=True)
    finally:
        cleanup()


def test_materialise_patch_heads_resolves_known_good_patch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = _seed_tracked_file(tmp_git_repo)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _patch_editing_mod(tmp_git_repo, case_dir / "head.patch")
    _patch_editing_mod(
        tmp_git_repo, case_dir / "clean.patch", new="ok = True  # tidy\n"
    )
    case = _case(
        tmp_git_repo,
        case_dir,
        base,
        head_patch="head.patch",
        known_good_head_patch="clean.patch",
    )

    out, cleanup = patch.materialise_patch_heads(case)
    try:
        assert out.head and out.known_good_head
        assert out.head != out.known_good_head
    finally:
        cleanup()


def test_materialise_patch_heads_rejects_patches_that_build_identical_trees(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # PR #306 review (Medium): two DIFFERENT patch files can produce the same
    # tree. The commit shas still differ (different commit messages), so a
    # sha-distinctness check passes while the known-good arm reviews byte-
    # identical code — a false-positive rate measuring nothing.
    base = _seed_tracked_file(tmp_git_repo)
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    _patch_editing_mod(tmp_git_repo, case_dir / "head.patch", new="ok = False\n")
    _patch_editing_mod(tmp_git_repo, case_dir / "clean.patch", new="ok = False\n")
    case = _case(
        tmp_git_repo,
        case_dir,
        base,
        head_patch="head.patch",
        known_good_head_patch="clean.patch",
    )

    with pytest.raises(ValueError, match="identical"):
        patch.materialise_patch_heads(case)


# ── sha-only PAIRED cases (#306 review 2) ──────────────────────────────────
# These build no patches, so they take the identity path — but they are still
# paired, and a case supplied via --cases-dir never meets the shipped-case
# preflight. The runtime function has to carry the check itself.


def test_materialise_rejects_a_sha_pair_sharing_one_tree(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # two REAL, distinct commits with the same tree (an empty commit is the
    # simplest construction): load_case sees two different sha strings and
    # passes it, so only a tree comparison catches the known-good arm
    # reviewing byte-identical code.
    base = _seed_tracked_file(tmp_git_repo)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "same tree"],
        cwd=tmp_git_repo,
        check=True,
        capture_output=True,
    )
    empty = git.base_sha(tmp_git_repo)
    assert empty != base
    case = _case(tmp_git_repo, tmp_path, base, head=base, known_good_head=empty)

    with pytest.raises(ValueError, match="identical"):
        patch.materialise_patch_heads(case)


def test_materialise_rejects_two_refs_naming_one_commit(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # equivalent refs with different declaration strings — a tag beside the sha
    base = _seed_tracked_file(tmp_git_repo)
    subprocess.run(
        ["git", "tag", "known-good"], cwd=tmp_git_repo, check=True, capture_output=True
    )
    case = _case(tmp_git_repo, tmp_path, base, head=base, known_good_head="known-good")

    with pytest.raises(ValueError, match="same commit"):
        patch.materialise_patch_heads(case)


def test_materialise_keeps_a_valid_sha_pair_on_the_identity_path(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # the check must not disturb a legitimate pinned pair: still identity, still
    # a no-op cleanup, no ephemeral commits built.
    base = _seed_tracked_file(tmp_git_repo)
    (tmp_git_repo / "mod.py").write_text("ok = False  # BUG\n")
    subprocess.run(
        ["git", "add", "-A"], cwd=tmp_git_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "bug"],
        cwd=tmp_git_repo,
        check=True,
        capture_output=True,
    )
    buggy = git.base_sha(tmp_git_repo)
    case = _case(tmp_git_repo, tmp_path, base, head=buggy, known_good_head=base)

    out, cleanup = patch.materialise_patch_heads(case)
    assert out is case
    cleanup()  # must not raise


def test_materialise_defers_when_neither_head_resolves(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # a fully scripted case (the hermetic aggregation tests drive fake shas
    # against a real checkout) needs no git and must stay identity
    case = _case(
        tmp_git_repo,
        tmp_path,
        "aaaaaaa",
        head="nope-buggy",
        known_good_head="nope-good",
    )
    out, cleanup = patch.materialise_patch_heads(case)
    assert out is case
    cleanup()


def test_materialise_rejects_a_pair_whose_known_good_ref_is_missing(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # PR #306 review 3: run_case reviews EVERY buggy sample before it touches
    # the known-good head, so a typo'd known-good ref would burn K reviewer
    # turns and their judge calls before anything noticed. Exactly one side
    # resolving is a broken case, not a scripted one.
    base = _seed_tracked_file(tmp_git_repo)
    case = _case(
        tmp_git_repo, tmp_path, base, head=base, known_good_head="typo-not-a-ref"
    )
    with pytest.raises(ValueError, match="known-good head 'typo-not-a-ref'"):
        patch.materialise_patch_heads(case)


def test_materialise_rejects_a_pair_whose_defect_ref_is_missing(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = _seed_tracked_file(tmp_git_repo)
    case = _case(
        tmp_git_repo, tmp_path, base, head="typo-not-a-ref", known_good_head=base
    )
    with pytest.raises(ValueError, match="defect head 'typo-not-a-ref'"):
        patch.materialise_patch_heads(case)


def test_run_case_spends_nothing_when_the_known_good_ref_is_missing(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    # the property that actually matters: not one reviewer call happens.
    from lithos_loom.evals.review.harness import run_case

    base = _seed_tracked_file(tmp_git_repo)
    case = _case(
        tmp_git_repo, tmp_path, base, head=base, known_good_head="typo-not-a-ref"
    )
    calls: list[str] = []

    def spy(case_, head):
        calls.append(head)
        return {"reviewers": []}

    with pytest.raises(ValueError, match="does not resolve"):
        run_case(case, k=5, review_fn=spy)
    assert calls == []


def test_materialise_patch_heads_works_with_a_relative_case_dir(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shipped cases pass a case_dir RELATIVE to the launch cwd
    # (`evals/review/cases/<id>`); `git apply` runs with cwd=build-worktree, so the
    # patch path must be made absolute or it's not found (a live-eval regression).
    base = _seed_tracked_file(tmp_git_repo)
    (tmp_path / "case").mkdir()
    _patch_editing_mod(tmp_git_repo, tmp_path / "case" / "head.patch")
    monkeypatch.chdir(tmp_path)
    case = _case(tmp_git_repo, Path("case"), base, head_patch="head.patch")

    out, cleanup = patch.materialise_patch_heads(case)
    try:
        assert out.head and out.head != base
    finally:
        cleanup()


def testmaterialise_patched_head_raises_when_patch_nets_no_change(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a patch that applies but changes nothing must NOT silently make head == base.
    base = git.base_sha(tmp_git_repo)
    monkeypatch.setattr(patch.git, "apply_patch", lambda wt, p: None)  # apply nothing
    with pytest.raises(ValueError, match="no change"):
        patch.materialise_patched_head(
            tmp_git_repo, base, tmp_path / "x.patch", parent=tmp_path / "p"
        )


def testmaterialise_patched_head_raises_on_unapplyable_patch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = git.base_sha(tmp_git_repo)
    bad = tmp_path / "bad.patch"
    bad.write_text("--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-x\n+y\n")
    with pytest.raises(RuntimeError):
        patch.materialise_patched_head(tmp_git_repo, base, bad, parent=tmp_path / "p")


# ── shipped cases: patches must MATERIALISE, not just load (#292 finding 3) ──
# test_every_shipped_case_loads (test_eval_review_case.py) proves TOML/AC
# structure; this proves the base commit exists and the patch applies — so a
# drifted patch fails the gate, not the paid live eval hours later. Guards keep
# it runnable everywhere it can't be exercised for real: the in-sandbox gate
# tree has no .git (skip), a shallow CI clone lacks the base (skip — CI's
# checkout uses fetch-depth: 0 precisely so same-repo cases DO run there), and
# cross-repo cases skip on hosts without the sibling checkout.

_SHIPPED_CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "review" / "cases"


def _shipped_patch_case_dirs() -> list[Path]:
    return sorted(
        d for d in _SHIPPED_CASES_DIR.iterdir() if (d / "case.toml").is_file()
    )


def _commit_exists(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo,
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.mark.parametrize("case_dir", _shipped_patch_case_dirs(), ids=lambda d: d.name)
def test_shipped_patch_cases_materialise(
    case_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_case(case_dir)
    has_patch = bool(case.head_patch or case.known_good_head_patch)
    # A sha-based case has nothing to materialise, but a PAIRED one still owes
    # the distinctness check below (#306 review): two pinned shas can describe
    # identical trees, and nothing at load time can see that.
    if not has_patch and not case.known_good_head:
        pytest.skip("sha-based case with no known-good pair — nothing to check")
    # Same resolution rule as materialise_patch_heads: cwd-relative.
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(
            f"base {case.base[:12]} not present (shallow clone?) — "
            "preflight from a full clone before a live eval run"
        )
    # git commit needs an identity; CI runners have none configured.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        if has_patch:
            assert resolved.head
            assert resolved.head != case.base
        if resolved.known_good_head:
            # PR #306 review (Medium): "it applied" is not "it is a known-good
            # state". Distinct CONTENT is the weakest property the pairing
            # needs — pin it for every shipped paired case, not just lens22.
            assert resolved.known_good_head != resolved.head
            if _commit_exists(repo, resolved.head) and _commit_exists(
                repo, resolved.known_good_head
            ):
                assert _tree_of(repo, resolved.head) != _tree_of(
                    repo, resolved.known_good_head
                )
    finally:
        cleanup()


def _tree_of(repo: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"{sha}^{{tree}}"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _blob_at(repo: Path, sha: str, path: str) -> str:
    """The file's content at *sha*, or ``""`` when it does not exist there.

    An empty *sha* is rejected rather than passed through: ``git show :path`` is
    the **index** form, so an unresolved head (a case whose ``[known_good]`` was
    dropped or never added) would silently yield the checkout's current content
    — which for a fixture pinned against an already-merged fix is exactly the
    text the assertions look for. A false green in the test that exists to stop
    a fixture going invalid.
    """
    assert sha, f"no commit resolved for {path} — cannot pin a head that is not there"
    done = subprocess.run(
        ["git", "show", f"{sha}:{path}"], cwd=repo, capture_output=True, text=True
    )
    return done.stdout if done.returncode == 0 else ""


def _css_at(repo: Path, sha: str) -> str:
    return _blob_at(repo, sha, "src/lithos_lens/static/lens.css")


@pytest.mark.parametrize("read", [_css_at, lambda r, s: _blob_at(r, s, "any.py")])
def test_fixture_blob_readers_reject_an_unresolved_ref(read, tmp_path: Path) -> None:
    # PR #320 review: every semantic fixture pin resolves its known-good head as
    # `resolved.known_good_head or ""`, so a dropped [known_good] hands the
    # reader an empty ref. `git show :path` is the INDEX form and would answer
    # with the working checkout — which, for a fixture pinned against a fix that
    # has since merged upstream, is exactly the text the pin asserts. Both
    # readers must fail loudly instead; one of them silently did not.
    with pytest.raises(AssertionError, match="cannot pin a head that is not there"):
        read(tmp_path, "")


def _rule_body(css: str, selector: str) -> str:
    """The declarations inside ``<selector> { … }``."""
    start = css.index(f"{selector} {{")
    return css[start : css.index("}", start)]


def test_lens22_artifact_fixture_pins_its_buggy_and_fixed_css(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR #306 review (Medium): known-good.patch is a 679-line external fixture
    # whose benchmark validity rests on ONE css hunk. Swapping it for the
    # defect patch — or losing the fix in a re-generation — would still apply
    # cleanly and still materialise, silently corrupting every later FP number.
    # Assert the semantic state each head is supposed to represent.
    case_dir = _SHIPPED_CASES_DIR / "lens22-artifact-prewrap"
    case = load_case(case_dir)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        buggy_css = _css_at(repo, resolved.head)
        fixed_css = _css_at(repo, resolved.known_good_head or "")
        # the defect: rendered markdown inherits the plaintext <pre> whitespace
        assert "white-space: pre-wrap" in _rule_body(buggy_css, ".markdown-body")
        # the fix (lens 734e5ef): dropped from .markdown-body, scoped to its pre
        assert "white-space: pre-wrap" not in _rule_body(fixed_css, ".markdown-body")
        assert "white-space: pre-wrap" in _rule_body(fixed_css, ".markdown-body pre")
    finally:
        cleanup()


_STORY_DEVELOP = "src/lithos_loom/plugins/story_develop"


def test_289_fixture_pins_both_symlink_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RH-1: 289's two [[expected]] defects are the two DIRECTIONS of one symlink
    # escape, and the case's whole deficit is the write one — so the pairing is
    # only a valid control if the known-good head closes both. known-good.patch
    # is the merged PR head (978 lines across 16 files): it would apply cleanly,
    # materialise, and differ in tree even if it were the wrong commit entirely.
    # Assert the semantic state each head represents, per direction.
    case_dir = _SHIPPED_CASES_DIR / "289-symlink-artifacts"
    case = load_case(case_dir)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        good_head = resolved.known_good_head or ""
        runner_py = f"{_STORY_DEVELOP}/check_runner.py"
        collector_py = f"{_STORY_DEVELOP}/check_artifacts.py"
        buggy_runner = _blob_at(repo, resolved.head, runner_py)
        good_collector = _blob_at(repo, good_head, collector_py)

        # the defect, source side: a host-side copytree over check-controlled
        # content, with symlinks left at their following default
        assert "shutil.copytree(src, dest, dirs_exist_ok=True)" in buggy_runner
        assert "symlinks=" not in buggy_runner
        # the defect, destination side: the copy lands in the handoff dir, which
        # is mounted READ-WRITE into agent containers
        assert 'config.handoff_dir / "artifacts"' in buggy_runner
        # ...and the collector at that head is unguarded in both directions
        assert "is_symlink" not in buggy_runner
        assert not _blob_at(repo, resolved.head, collector_py)

        # the fix (#289 review), source side: no link is ever followed or copied
        assert "followlinks=False" in good_collector
        assert "src.is_symlink()" in good_collector
        assert "entry.is_symlink()" in good_collector
        assert "src.resolve().is_relative_to(tree.resolve())" in good_collector
        # the fix, destination side: a HOST-controlled root, so there is no
        # host-privileged write into a directory agents can pre-plant
        assert "config.artifacts_dir" in good_collector
        assert "handoff_dir" not in good_collector
    finally:
        cleanup()


_LENS_METADATA = "src/lithos_lens/knowledge_metadata.py"


def _format_confidence_body(module: str) -> str:
    """The source of ``_format_confidence``, up to the next top-level def."""
    start = module.index("def _format_confidence(")
    rest = module[start:]
    end = rest.index("\ndef ", 1)
    return rest[:end]


def test_lens33_fixture_pins_both_confidence_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RH-1: lens33's two [[expected]] defects are the two symptom forms of ONE
    # unvalidated value — a finite out-of-range value renders "200%", a
    # non-finite one makes round() raise mid-render — and its whole 0/5 deficit
    # is the range form. The pairing is only a valid control if the known-good
    # head closes BOTH, which lens 87c9560 does with a single comparison guard
    # (NaN/inf fail `0 <= v <= 1` for free). Assert the semantic state of each
    # head per form, scoped to the function: the module is 300+ lines and a
    # module-wide substring search would pass on an unrelated range check.
    case_dir = _SHIPPED_CASES_DIR / "lens33-confidence-crash"
    case = load_case(case_dir)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        buggy = _format_confidence_body(_blob_at(repo, resolved.head, _LENS_METADATA))
        good = _format_confidence_body(
            _blob_at(repo, resolved.known_good_head or "", _LENS_METADATA)
        )

        # the defect: the documented 0..1 domain is asserted in the docstring and
        # enforced nowhere — the guard validates the TYPE and nothing else, so
        # every finite float formats and NaN/inf reach round().
        assert "``0..1``" in buggy
        assert "isinstance(value, (int, float))" in buggy
        assert "round(value * 100)" in buggy
        assert "<= 1" not in buggy and "math.isfinite" not in buggy

        # the fix (lens 87c9560): one comparison closes both forms. Pin the
        # range guard AND that it precedes the round() the non-finite form
        # crashes on — a guard added after it would still raise.
        assert "not 0 <= value <= 1" in good
        assert good.index("not 0 <= value <= 1") < good.index("round(value * 100)")
    finally:
        cleanup()


_LENS_FRONTIER = "src/lithos_lens/frontier.py"


def test_lens34_fixture_pins_both_read_skew_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # RH-1: lens34's known-good is the benchmark's first SYNTHETIC fix — it is
    # not the project's own commit, because the delivered head is pre-rebase and
    # the authentic fix shares only the base with it (34 files / 1822 lines).
    # That makes these pins load-bearing in a way the others are not: nothing
    # upstream will ever re-assert this behaviour, so a regenerated patch that
    # quietly drops a hunk would leave a known-good that still HAS the defect,
    # and every false-positive number after it would be measuring the defect.
    case_dir = _SHIPPED_CASES_DIR / "lens34-truncation"
    case = load_case(case_dir)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        good_head = resolved.known_good_head or ""
        buggy = _blob_at(repo, resolved.head, _LENS_FRONTIER)
        good = _blob_at(repo, good_head, _LENS_FRONTIER)

        # defect #0: truncation inferred from unclassified rows, limit unchecked
        assert 'truncated=frontier_ok and bool(partition["unclassified"])' in buggy
        assert ">= frontier_limit" not in buggy
        # defect #1: ready is tested before blocked, with no overlap branch, so
        # a row on BOTH frontiers is silently Ready
        assert "task.id in ready_ids and task.id in blocked_map" not in buggy
        assert buggy.index("elif task.id in ready_ids:") < buggy.index(
            "elif task.id in blocked_map:"
        )

        # fix #0: the limit fact gates the banner, and it is the ROW COUNT — a
        # len(ready_ids) check would undercount a duplicated row.
        assert "ready_rows >= frontier_limit" in good
        assert "len(blocked_records) >= frontier_limit" in good
        assert "ready_rows = len(ready_tasks)" in good
        # fix #1: overlap is detected EXPLICITLY, ahead of the ready branch, and
        # is reported — the expected requires it stop being resolved *silently*,
        # so a bare reorder would not close it.
        overlap = "elif task.id in ready_ids and task.id in blocked_map:"
        assert overlap in good
        assert good.index(overlap) < good.index("elif task.id in ready_ids:")
        # substring chosen to survive the source's line wrapping
        assert "came back on both the ready" in good
        # PR #327 review: the warning must be RENDER-EFFECTIVE. Computed from
        # the raw responses it fired for claimed rows (which render In progress)
        # and for query-filtered rows (which render nowhere) — a fresh defect
        # inside the control, of the same "claims something about rendering
        # without checking what rendered" class as the seeded ones. Pin that it
        # intersects the partition and is computed after it.
        assert 'shown_blocked = {row.task.id for row in partition["blocked"]}' in good
        assert "contested_ids & shown_blocked" in good
        assert good.index("partition = classify_open_tasks") < good.index(
            "contested_ids ="
        )
        # ...and the third suppression path, which happens AFTER the partition
        # so intersecting with it is not enough: hiding the Open status group
        # blanks every open section (PR #327 re-review).
        assert "if show_open and contested_shown:" in good

        # The synthetic fix has no upstream commit to re-assert it, so the
        # regression tests that pin its behaviour are themselves part of the
        # fixture: a regenerated patch that drops them must fail here.
        good_tests = _blob_at(repo, good_head, "tests/test_frontier.py")
        for name in (
            "test_load_dashboard_does_not_call_read_skew_truncation",
            "test_load_dashboard_resolves_ready_blocked_overlap_conservatively",
            "test_overlap_warning_is_render_effective_for_a_claimed_task",
            "test_overlap_warning_is_render_effective_for_a_filtered_task",
            "test_overlap_warning_is_render_effective_when_open_status_hidden",
        ):
            assert f"def {name}(" in good_tests, f"known-good lost its {name} pin"
        # ...including that the conservative reclassification keeps the chips.
        assert "[chip.target_id for chip in row.blockers]" in good_tests
    finally:
        cleanup()


_LOOM_EXTERNAL_REVIEWS = "src/lithos_loom/subscriptions/external_reviews.py"


def _materialised_external_reviews(
    monkeypatch: pytest.MonkeyPatch, case_id: str
) -> tuple[str, str]:
    """(buggy, known-good) blobs of the sweep module for a shipped 344 case."""
    case = load_case(_SHIPPED_CASES_DIR / case_id)
    repo = Path(case.repo).resolve()
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")
    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        buggy = _blob_at(repo, resolved.head, _LOOM_EXTERNAL_REVIEWS)
        good = _blob_at(repo, resolved.known_good_head or "", _LOOM_EXTERNAL_REVIEWS)
    finally:
        cleanup()
    return buggy, good


def test_344_backfill_fixture_pins_the_missing_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Escape review (PR #344 round 1): the defect head has NO handled-root
    # suppression at all — a markerless gate's first sweep re-posts history
    # the inline round already remediated — and the known-good is exactly the
    # round-1 fix (handled_roots + the one-second since overlap). A
    # regenerated patch from the wrong ref would corrupt every later number.
    buggy, good = _materialised_external_reviews(monkeypatch, "344-backfill-history")

    assert "handled_roots" not in buggy
    assert "timedelta(seconds=1)" not in buggy
    assert "handled_roots" in good
    assert "timedelta(seconds=1)" in good


def test_344_suppression_fixture_pins_the_marker_only_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Escape review (PR #344 rounds 2+3): the defect head suppresses on the
    # bare automated-reply marker (no landed-fix shape, no authenticated
    # author); the known-good requires BOTH halves of the proof.
    buggy, good = _materialised_external_reviews(
        monkeypatch, "344-reply-suppression-proof"
    )

    # marker-only proof at the defect head: suppression exists...
    assert "handled_roots" in buggy
    # ...but neither the landed-fix shape nor the author check does.
    assert "_FIXED_REPLY_PREFIX" not in buggy
    assert "get_collaborator_permission" not in buggy
    # the fix: both halves, and failure of the probe stays untrusted
    # (the helper moved to github_models in a later slice; at this head the
    # landed-fix shape is the local _FIXED_REPLY_PREFIX check).
    assert '_FIXED_REPLY_PREFIX = "Fixed in "' in good
    assert "get_collaborator_permission" in good


_LENS43_FRONTIER = "src/lithos_lens/frontier.py"
_LENS43_TASKS = "src/lithos_lens/tasks.py"


def test_lens43_fixture_pins_both_contract_violations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Escape review (lens PR #43 / PRD pr-reconciliation "Failure 1"): the
    # delivered head keeps the graph surface on a transient frontier read
    # failure (§14 requires the flat fallback) and lets the healthy stripe
    # ignore the open rows classify_open_tasks skips. The known-good is the
    # human-fixed pre-squash tip (the 289 merged-head shape).
    case = load_case(_SHIPPED_CASES_DIR / "lens43-degraded-contract")
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        buggy_frontier = _blob_at(repo, resolved.head, _LENS43_FRONTIER)
        good_frontier = _blob_at(repo, resolved.known_good_head or "", _LENS43_FRONTIER)
        buggy_tasks = _blob_at(repo, resolved.head, _LENS43_TASKS)
        good_tasks = _blob_at(repo, resolved.known_good_head or "", _LENS43_TASKS)

        # Defect 1: a failed frontier read keeps the graph surface (the error
        # is appended, frontier_ok drops, rows stay) — no per-load flat
        # fallback distinct from the cached tools-absent verdict.
        assert "Could not load the ready frontier." in buggy_frontier
        assert "frontier_fallback" not in buggy_frontier
        # The human fix (lens 6907906/0347ec4) extracts + wires the fallback.
        assert "frontier_fallback" in good_frontier

        # Defect 2: the healthy stripe never accounts for the open rows
        # classify_open_tasks skips (non-"task" task_types).
        assert "def healthy" in buggy_tasks
        assert "rolled_up_only" not in buggy_tasks
        # The fix (lens 8f8d24b): rolled_up_only names the epics/gates-only
        # condition and the healthy derivation withholds the claim on it.
        assert "def rolled_up_only" in good_tasks
        assert "not self.rolled_up_only" in good_tasks
    finally:
        cleanup()


_LENS43C_FILTERING = "src/lithos_lens/task_filtering.py"
_LENS43C_TASKS = "src/lithos_lens/tasks.py"
_LENS43C_WEB = "src/lithos_lens/web.py"
_LENS43C_MVP_TESTS = "tests/test_tasks_mvp.py"
_LENS43C_FRONTIER_TESTS = "tests/test_frontier.py"
_LENS43C_TERM = "or bool(filters.projects)"
_LENS43C_DOC = "``project`` narrows like tag/agent"
_LENS43C_METRICS = ["docs/generated/metrics.json", "docs/generated/metrics.md"]
# lens PR #43's squash merge — the tree the known-good is derived from.
_LENS43C_SQUASH = "1e34d4457749b5705fa800d1c968302d3119dfdc"


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def _tests_mentioning(repo: Path, sha: str, needle: str) -> list[str]:
    # `git grep -l` exits 1 on no match — not an error here.
    proc = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", needle, sha, "--", "tests"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return proc.stdout.split()


def test_lens43_composed_fixture_pins_the_missing_projects_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PRD pr-reconciliation S8 (composed-tree review): the seeded head is the
    # merged lens #43 content on top of lens main AFTER #44/#45 landed, minus
    # the one term the operator's conflict resolution added — `projects`
    # never counts as narrowing, so a ?project= board makes the healthy
    # stripe's system-wide claim. The known-good is that resolution (lens
    # 41a43c8, tree-identical to the squash merge) with two post-merge
    # giveaway lines neutralised in BOTH heads. Pinned here, as a pair:
    # the base already carries the projects filter (what makes the defect
    # compositional); no ADDED line in the diff names it (else a catch would
    # be an in-diff inconsistency, not tree-reading); the two heads differ in
    # nothing but the resolution + regenerated metrics; the known-good is the
    # squash merge but for the neutralisation; and no test at either head
    # touches the helper, so the case is panel-only by construction.
    case = load_case(_SHIPPED_CASES_DIR / "lens43-composed-projects")
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    # Compositional: the base (post-T1-S9) already has the projects filter on
    # TaskFilters specifically (DashboardData carries a same-named field —
    # slice the class so that one cannot satisfy this).
    base_tasks = _blob_at(repo, case.base, _LENS43C_TASKS)
    task_filters = base_tasks.partition("class TaskFilters")[2].partition("class ")[0]
    assert "projects: tuple[str, ...] = ()" in task_filters
    # ...and the story's diff never ADDS a line naming the project filter
    # (`project:` is the tag convention, not the filter).
    assert case.case_dir is not None
    defect_patch = (case.case_dir / "feature-with-defect.patch").read_text()
    added = [
        ln
        for ln in defect_patch.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    ]
    stripped = [re.sub(r"[Pp]roject:", "", ln) for ln in added]
    assert not [ln for ln in stripped if "project" in ln.lower()]

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        good_head = resolved.known_good_head or ""
        buggy = _blob_at(repo, resolved.head, _LENS43C_FILTERING)
        good = _blob_at(repo, good_head, _LENS43C_FILTERING)
        assert "def filters_narrow_the_board" in buggy
        assert "def filters_narrow_the_board" in good
        # The defect: the helper exists at both heads; only the known-good
        # counts projects as narrowing (and says why in its docstring).
        assert _LENS43C_TERM not in buggy
        assert _LENS43C_DOC not in buggy
        assert _LENS43C_TERM in good
        assert _LENS43C_DOC in good

        # Minimal pair: nothing but the resolution and lens's regenerated
        # metrics separates the two heads.
        changed = _git_out(repo, "diff", "--name-only", resolved.head, good_head)
        assert sorted(changed.split()) == sorted(
            [*_LENS43C_METRICS, _LENS43C_FILTERING]
        )

        # The known-good is the squash merge but for the neutralised giveaways
        # (+ the metrics they move) — a regeneration from another ref pair
        # cannot satisfy this.
        if _commit_exists(repo, _LENS43C_SQUASH):
            vs_squash = _git_out(
                repo, "diff", "--name-only", _LENS43C_SQUASH, good_head
            )
            assert sorted(vs_squash.split()) == sorted(
                [*_LENS43C_METRICS, _LENS43C_WEB, _LENS43C_MVP_TESTS]
            )
        for sha in (resolved.head, good_head):
            web = _blob_at(repo, sha, _LENS43C_WEB)
            assert "project/tag/agent" not in web
            assert "tag/agent ride along" not in web  # the under-enumerating reword
            assert "the active filters ride along" in web
            # (the base's own ?project=influx tests stay — only the card-link
            # test's parameter + assertion were removed)
            mvp_tests = _blob_at(repo, sha, _LENS43C_MVP_TESTS)
            assert "agent=planner&project=influx" not in mvp_tests
            assert 'assert "project=influx" in href' not in mvp_tests

        # Panel-only: no test at either head references the helper at all,
        # and the narrowed-board regression test is parametrised over
        # tags / agent / status only — lens's check-set passes the seeded
        # head exactly as it passes the known-good (verified live while
        # authoring).
        for sha in (resolved.head, good_head):
            assert _tests_mentioning(repo, sha, "filters_narrow_the_board") == []
            tests_blob = _blob_at(repo, sha, _LENS43C_FRONTIER_TESTS)
            narrowed = "def test_healthy_is_withheld_on_a_narrowed_board"
            assert narrowed in tests_blob
            before = tests_blob.partition(narrowed)[0]
            assert "@pytest.mark.parametrize" in before
            params = before.rsplit("@pytest.mark.parametrize", 1)[1]
            assert "projects" not in params
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# lens83-gate-row-panel-contract (escape review, lens PR #83): the panel
# contract reached one renderer of a board row and not the other.

_LENS83_TASKS_JS = "src/lithos_lens/static/tasks.js"
_LENS83_GATE_ROW = "src/lithos_lens/templates/tasks/gate_row.html"
_LENS83_ROW = "src/lithos_lens/templates/tasks/row.html"
_LENS83_PANEL = "src/lithos_lens/templates/tasks/panel.html"
_LENS83_CONTRACT = 'data-panel-url="{{ panel_fragment_url(request, gate.task.id) }}"'
_LENS83_SELECTOR = 'const PANEL_ROW = "[data-panel-url][data-task-id]";'
_LENS83_FIX_FILES = [
    "docs/SPECIFICATION.md",
    "docs/generated/metrics.json",
    "docs/generated/metrics.md",
    "e2e/tests/smoke.spec.ts",
    _LENS83_TASKS_JS,
    _LENS83_GATE_ROW,
    "tests/test_task_panel.py",
    "tests/test_tasks_js.py",
]
# The delivered r5 head and the first remediation commit, when the checkout
# has them: the rebuilt trees must be theirs exactly.
_LENS83_DELIVERED = "68af5b47eb4c2e57b85a294788efea66e3ac3630"
_LENS83_FIX = "0ab1eb758f1c1f2a99425db257faf096230c86dc"


def test_lens83_fixture_pins_the_gate_row_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Escape review (README §"Escape review", bucket 1): the story wired "a
    # row click" to the panel through tasks/row.html + a `[data-task-row]`
    # click handler and never touched tasks/gate_row.html, the board's other
    # row renderer — so gate rows carry no panel URL and the handler cannot
    # reach them. The known-good is loom's own remediation commit 0ab1eb7
    # (converge --from-github round 1), a descendant of the defect head
    # carrying exactly the fix. Pinned as a pair: the contract is NEW in the
    # diff (neither template has it on the base); the defect patch never
    # touches gate_row.html; the fix puts the server-built URL on the gate
    # row and switches the handler to the shared selector; the heads differ
    # in nothing but the fix commit's files; both rebuilt trees are the real
    # commits'; the three declared-as-residue defects are present at BOTH
    # heads (shared → noise, never fp); and no test at the defect head clicks
    # a gate row (panel-only by construction).
    case = load_case(_SHIPPED_CASES_DIR / "lens83-gate-row-panel-contract")
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")

    # The panel contract is the story's: on the base neither row template
    # carries it, and the gate row already exists as a second renderer.
    for path in (_LENS83_ROW, _LENS83_GATE_ROW):
        assert "data-panel-url" not in _blob_at(repo, case.base, path)
    assert "data-gate-row" in _blob_at(repo, case.base, _LENS83_GATE_ROW)
    # ...and the defect patch adds it to row.html while never touching
    # gate_row.html — the defect is the relation to an untouched file.
    assert case.case_dir is not None
    defect_patch = (case.case_dir / "feature-with-defect.patch").read_text()
    assert f"diff --git a/{_LENS83_GATE_ROW}" not in defect_patch
    assert f"diff --git a/{_LENS83_ROW}" in defect_patch

    resolved, cleanup = patch.materialise_patch_heads(case)
    try:
        good_head = resolved.known_good_head or ""
        buggy_js = _blob_at(repo, resolved.head, _LENS83_TASKS_JS)
        good_js = _blob_at(repo, good_head, _LENS83_TASKS_JS)
        buggy_gate = _blob_at(repo, resolved.head, _LENS83_GATE_ROW)
        good_gate = _blob_at(repo, good_head, _LENS83_GATE_ROW)
        # The defect head: row.html has the contract, gate_row.html does not,
        # and the handler resolves rows by the SSE hook alone.
        assert "data-panel-url" in _blob_at(repo, resolved.head, _LENS83_ROW)
        assert "data-panel-url" not in buggy_gate
        assert 'target.closest("[data-task-row]")' in buggy_js
        assert _LENS83_SELECTOR not in buggy_js
        # The fix: the server-built URL on the gate row, one shared selector
        # for both halves of the interaction, the <summary> bail-out.
        assert _LENS83_CONTRACT in good_gate
        assert _LENS83_SELECTOR in good_js
        assert "target.closest(PANEL_ROW)" in good_js
        assert 'target.closest("summary")' in good_js

        # Minimal pair: the fix commit's files and nothing else.
        changed = _git_out(repo, "diff", "--name-only", resolved.head, good_head)
        assert sorted(changed.split()) == sorted(_LENS83_FIX_FILES)
        # Tree pins against the real commits, where the checkout has them.
        for real, rebuilt in (
            (_LENS83_DELIVERED, resolved.head),
            (_LENS83_FIX, good_head),
        ):
            if _commit_exists(repo, real):
                assert _git_out(repo, "diff", "--name-only", real, rebuilt) == ""

        # Residue declared in the description — present at BOTH heads, so a
        # finding about it is shared noise and never a defect-scoped fp.
        for sha in (resolved.head, good_head):
            js = _blob_at(repo, sha, _LENS83_TASKS_JS)
            assert "return url.pathname + url.search;" in js  # anchor dropped
            assert "selectionIn(window.location.href) !== taskId" not in js  # dup push
            assert "(no project)" not in _blob_at(repo, sha, _LENS83_PANEL)

        # Panel-only: no test at the defect head clicks a gate row; the fix
        # adds that coverage (it is how the reviewer proved the gap).
        needle = "clicking_a_gate_row"
        assert _tests_mentioning(repo, resolved.head, needle) == []
        assert _tests_mentioning(repo, good_head, needle) != []
    finally:
        cleanup()


# ---------------------------------------------------------------------------
# The lens T2 escape corpus (escape review, lens PRs #78 / #79 / #81; PR #401
# review, Medium): each pair pinned semantically — the mechanism is present at
# the defect head, closed at the known-good head, the heads differ in the fix
# commit's files alone, and both rebuilt trees are the real commits'. The
# generic materialise test proves only that a patch applies; the first cut of
# lens81 applied cleanly while omitting the screenshot its manual referenced.


def _function_body(blob: str, name: str) -> str:
    """The source of one top-level ``def`` / ``async def`` up to the next."""
    start = re.search(rf"^(?:async )?def {re.escape(name)}\(", blob, re.MULTILINE)
    assert start is not None, name
    nxt = re.search(r"^(?:async )?def ", blob[start.end() :], re.MULTILINE)
    return blob[start.start() : start.end() + (nxt.start() if nxt else len(blob))]


def _pin_pair(
    monkeypatch: pytest.MonkeyPatch, case_id: str, delivered: str, fix: str
) -> tuple[Path, Case, Callable[[], None]]:
    case = load_case(_SHIPPED_CASES_DIR / case_id)
    repo = Path(case.repo).resolve()
    if not (repo / ".git").exists():
        pytest.skip(f"repo {case.repo!r} is not a git checkout here")
    if not _commit_exists(repo, case.base):
        pytest.skip(f"base {case.base[:12]} not present (shallow clone?)")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "loom-eval-preflight@localhost")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "loom-eval-preflight")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "loom-eval-preflight@localhost")
    resolved, cleanup = patch.materialise_patch_heads(case)
    # Tree pins against the real commits, where the checkout has them.
    for real, rebuilt in ((delivered, resolved.head), (fix, resolved.known_good_head)):
        if rebuilt and _commit_exists(repo, real):
            assert _git_out(repo, "diff", "--name-only", real, rebuilt) == ""
    return repo, resolved, cleanup


_LENS78_SCOPE = "src/lithos_lens/graph_scope.py"
_LENS78_FANOUT = "src/lithos_lens/graph_fanout.py"
_LENS78_FIX_FILES = [
    "docs/REQUIREMENTS.md",
    "docs/SPECIFICATION.md",
    "docs/architecture.toml",
    "docs/generated/components/TaskGraph.md",
    "docs/generated/metrics.json",
    "docs/generated/metrics.md",
    "docs/prd/t2-task-relationship-graphs.md",
    "pyproject.toml",
    _LENS78_FANOUT,
    _LENS78_SCOPE,
    "tests/test_graph_scope.py",
]
_LENS78_DELIVERED = "2694092478b677552a3ec310846746e63af73465"
_LENS78_FIX = "52e52c2ed3f0b6ebaa18124128f535ce3f8d091e"


def test_lens78_fixture_pins_the_fanout_work_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bucket 1 ×2 (resource-bound completeness): (1) the exact node guard is
    # evaluated only after every far endpoint was resolved and nothing bounds
    # that work; (2) the epic membership reads run outside the graph session
    # reservation. The fix (the operator's hand commit) adds a candidate
    # budget + phase deadline refusing with REFUSAL_CLASSIFICATION, puts the
    # epic reads under graph_fanout_gate(), and extracts the fan-out half to
    # graph_fanout.py for the line budget.
    repo, resolved, cleanup = _pin_pair(
        monkeypatch, "lens78-fanout-work-bounds", _LENS78_DELIVERED, _LENS78_FIX
    )
    try:
        good = resolved.known_good_head or ""
        # The base predates the slice: no scope module at all.
        assert (
            _git_out(repo, "ls-tree", "--name-only", resolved.base, "--", _LENS78_SCOPE)
            == ""
        )
        buggy = _blob_at(repo, resolved.head, _LENS78_SCOPE)
        fixed = _blob_at(repo, good, _LENS78_SCOPE)
        # (1) defect: the far-endpoint resolution has no candidate budget or
        # phase deadline, and no classification refusal exists.
        assert "_resolve_far_endpoints" in buggy
        assert "REFUSAL_CLASSIFICATION" not in buggy
        assert "MAX_GHOST_RESOLUTION_READS" not in buggy
        assert "GHOST_RESOLUTION_BUDGET_S" not in buggy
        # (2) defect: epic membership reads outside the reservation.
        epic = _function_body(buggy, "epic_scope_tasks")
        assert "task_children" in epic and "graph_fanout_gate" not in epic
        # The fix: both bounds + the refusal, and the epic reads gated.
        assert "REFUSAL_CLASSIFICATION" in fixed
        assert "graph_fanout_gate" in _function_body(fixed, "epic_scope_tasks")
        fanout = _blob_at(repo, good, _LENS78_FANOUT)
        assert "MAX_GHOST_RESOLUTION_READS = " in fanout
        assert "GHOST_RESOLUTION_BUDGET_S = " in fanout
        assert (
            _git_out(
                repo, "ls-tree", "--name-only", resolved.head, "--", _LENS78_FANOUT
            )
            == ""
        )
        # The fix commit's files and nothing else.
        changed = _git_out(repo, "diff", "--name-only", resolved.head, good)
        assert sorted(changed.split()) == sorted(_LENS78_FIX_FILES)
    finally:
        cleanup()


_LENS79_LAYOUT = "src/lithos_lens/graph_layout.py"
_LENS79_FIX_FILES = [
    "docs/SPECIFICATION.md",
    "docs/generated/metrics.json",
    "docs/generated/metrics.md",
    _LENS79_LAYOUT,
    "tests/test_graph_layout.py",
]
_LENS79_DELIVERED = "0891cb6f05244fdd3a97a4bb3c757b986ada4fe4"
_LENS79_FIX = "908d4c3129e7c3669f62a1966077d56655b66823"


def test_lens79_fixture_pins_the_active_projection_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bucket 1 ×2: (1) `_active_condensed` reuses the all-edge SCC
    # condensation (`member_of` from topology.condensations) and drops active
    # edges inside one; (2) the `through=` chain's tie-break walks the
    # PREDECESSOR map backward and reverses. The fix (remediation round 1)
    # gives the active projection its own Tarjan and compares complete
    # forward prefixes.
    repo, resolved, cleanup = _pin_pair(
        monkeypatch, "lens79-active-projection-chain", _LENS79_DELIVERED, _LENS79_FIX
    )
    try:
        good = resolved.known_good_head or ""
        assert (
            _git_out(
                repo, "ls-tree", "--name-only", resolved.base, "--", _LENS79_LAYOUT
            )
            == ""
        )
        buggy = _blob_at(repo, resolved.head, _LENS79_LAYOUT)
        fixed = _blob_at(repo, good, _LENS79_LAYOUT)
        b_active = _function_body(buggy, "_active_condensed")
        assert "for condensation in topology.condensations" in b_active  # reused
        assert "member_of[edge.from_task_id] != member_of[edge.to_task_id]" in b_active
        assert "Tarjan" not in b_active and "_tarjan" not in b_active
        b_chain = _function_body(buggy, "longest_blocking_chain")
        assert "_longest_from(order, predecessors, key)" in b_chain  # backward
        assert "tuple(reversed(upward))" in b_chain
        f_active = _function_body(fixed, "_active_condensed")
        assert 'edge.state == "active"' in f_active
        assert "for condensation in topology.condensations" not in f_active
        assert (
            "Tarjan" in f_active or "_tarjan" in f_active or "_adjacency(" in f_active
        )
        f_chain = _function_body(fixed, "longest_blocking_chain")
        assert "_longest_from(order, predecessors, key)" not in f_chain
        changed = _git_out(repo, "diff", "--name-only", resolved.head, good)
        assert sorted(changed.split()) == sorted(_LENS79_FIX_FILES)
    finally:
        cleanup()


_LENS81_CYCLES = "src/lithos_lens/graph_cycles.py"
_LENS81_PAGE = "src/lithos_lens/graph_page.py"
_LENS81_SCREENSHOT = "docs/user-manual/screenshots/graph.png"
_LENS81_FIX_FILES = [
    "docs/SPECIFICATION.md",
    "docs/generated/domain_model.md",
    "docs/generated/metrics.json",
    "docs/generated/metrics.md",
    "docs/user-manual/manual.md",
    _LENS81_CYCLES,
    _LENS81_PAGE,
    "tests/test_graph_page.py",
]
_LENS81_DELIVERED = "b0368ed2bf9f1e62046c672fce7b5b852911ed1d"
_LENS81_FIX = "8b7f6c844ef1c1d0bbd9c52e87cb2e3ba7a0e0d3"


def test_lens81_fixture_pins_the_cycle_authority_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Declared (authority coverage): `_signal` folds every scoped-read row
    # into the page's cycle authority — no in-scope filter, no `verdicts`.
    # Present-but-undeclared (lens78's class): the read plan is handed to
    # gather with no cap or phase deadline. The fix (remediation round 1)
    # adds `verdicts` filtered to in-scope non-ghost ids and the
    # MAX_CYCLE_READ_PROJECTS / CYCLE_READ_BUDGET_S bounds (the cap the
    # merged head later replaced with the deadline alone — recorded in the
    # case as pairing noise). Both patches carry the manual's screenshot
    # (binary): the first cut omitted it and drew a shared broken-image
    # minor on every sample.
    repo, resolved, cleanup = _pin_pair(
        monkeypatch, "lens81-cycle-authority-coverage", _LENS81_DELIVERED, _LENS81_FIX
    )
    try:
        good = resolved.known_good_head or ""
        assert (
            _git_out(
                repo, "ls-tree", "--name-only", resolved.base, "--", _LENS81_CYCLES
            )
            == ""
        )
        buggy = _blob_at(repo, resolved.head, _LENS81_CYCLES)
        fixed = _blob_at(repo, good, _LENS81_CYCLES)
        assert "verdicts" not in buggy
        assert "in_scope" not in _function_body(buggy, "_signal")
        assert "asyncio.gather(*(read(*call) for call in plan))" in buggy
        assert (
            "MAX_CYCLE_READ_PROJECTS" not in buggy
            and "CYCLE_READ_BUDGET_S" not in buggy
        )
        assert "verdicts: tuple[BlockedTaskRecord, ...]" in fixed
        assert "in_scope" in _function_body(fixed, "_signal")
        assert "MAX_CYCLE_READ_PROJECTS = 64" in fixed
        assert "CYCLE_READ_BUDGET_S = " in fixed
        assert "def _read_plan(" in fixed
        for sha in (resolved.head, good):
            listed = _git_out(
                repo, "ls-tree", "--name-only", sha, "--", _LENS81_SCREENSHOT
            )
            assert listed.strip() == _LENS81_SCREENSHOT
        changed = _git_out(repo, "diff", "--name-only", resolved.head, good)
        assert sorted(changed.split()) == sorted(_LENS81_FIX_FILES)
    finally:
        cleanup()

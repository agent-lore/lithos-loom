"""Tests for patch-based eval case head materialisation (#193).

The git-real tests use the ``tmp_git_repo`` fixture; none run an agent/docker, so
this stays in ``make check``.
"""

from __future__ import annotations

import subprocess
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


def test_materialise_patched_head_raises_when_patch_nets_no_change(
    tmp_git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a patch that applies but changes nothing must NOT silently make head == base.
    base = git.base_sha(tmp_git_repo)
    monkeypatch.setattr(patch.git, "apply_patch", lambda wt, p: None)  # apply nothing
    with pytest.raises(ValueError, match="no change"):
        patch._materialise_patched_head(
            tmp_git_repo, base, tmp_path / "x.patch", parent=tmp_path / "p"
        )


def test_materialise_patched_head_raises_on_unapplyable_patch(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    base = git.base_sha(tmp_git_repo)
    bad = tmp_path / "bad.patch"
    bad.write_text("--- a/nope.txt\n+++ b/nope.txt\n@@ -1 +1 @@\n-x\n+y\n")
    with pytest.raises(RuntimeError):
        patch._materialise_patched_head(tmp_git_repo, base, bad, parent=tmp_path / "p")


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


def _css_at(repo: Path, sha: str) -> str:
    return subprocess.run(
        ["git", "show", f"{sha}:src/lithos_lens/static/lens.css"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


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

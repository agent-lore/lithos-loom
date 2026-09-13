"""Resolve-eval case loader (PRD pr-reconciliation S8, conflict-resolution shape).

A case is a **real conflicting merge** plus the **oracle** a correct
resolution must satisfy:

- ``merge_base`` — the PR's own diff base (what the story was developed
  against; a reachable commit);
- ``head`` / ``head_patch`` — the PR head: the story's content as delivered,
  a sha or ``merge_base + patch`` applied at run time (a delivered head is
  usually on no surviving branch);
- ``base`` — the base branch's tip that landed meanwhile, the commit S5
  merges INTO the head (a reachable ``main`` ancestor). The harness refuses a
  case whose merge is clean, or conflicts in a shape the S5 mode cannot
  resolve by editing — that would measure nothing.
- ``[[probe]]`` — executable checks run with cwd = a tree under test
  (``{worktree}``; ``{case_dir}`` is the case directory, so a probe can be a
  script shipped beside ``case.toml``). Exit 0 = the property holds. The
  probes are the oracle: a resolution is *correct* when every probe passes.
- ``known_good`` / ``known_good_patch`` (on ``base``) — a tree every probe
  passes on (the operator's own resolution, typically); ``known_bad`` /
  ``known_bad_patch`` — a plausible WRONG resolution at least one probe
  fails on. Both are required: an oracle that cannot discriminate would
  score every resolution the same way, and the harness validates the pair
  before any paid sample (the pin, not the patch, is what makes the number
  mean something).

``personas`` / ``profile`` name the panel and check-set the S5 loop runs on
the composed tree, exactly as ``converge --resolve-conflicts`` would field
them; ``title`` + ``ac.md`` are the PR's intent (the conflict brief and the
coder's acceptance criteria). The project's own develop settings — ``image``,
``parity_command``, ``test_command``, ``check_commands``, ``check_states``,
``artifacts_path`` — are declared by the case (what a ``--story`` run would
resolve from the project's context doc), so the gate is the project's and
the fixture is hermetic.
"""

from __future__ import annotations

import re
import shlex
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ...plugins.story_develop.config import (
    parse_artifacts_path,
    parse_check_commands,
    parse_check_states,
    parse_image,
    parse_parity_command,
    parse_test_command,
)
from ...plugins.story_develop.personas import canonical_personas
from ...plugins.story_develop.profiles import UnknownProfileError, get_profile

__all__ = ["Probe", "ResolveCase", "load_resolve_case"]

_TOP_LEVEL_KEYS = frozenset({"case", "probe"})
_CASE_KEYS = frozenset(
    {
        "id",
        "description",
        "repo",
        "title",
        "merge_base",
        "base",
        "head",
        "head_patch",
        "known_good",
        "known_good_patch",
        "known_bad",
        "known_bad_patch",
        "personas",
        "profile",
        "acceptance_criteria_file",
        "image",
        "parity_command",
        "test_command",
        "check_commands",
        "check_states",
        "artifacts_path",
    }
)
_PROBE_KEYS = frozenset({"name", "command"})
_PROBE_PLACEHOLDERS = frozenset({"case_dir", "worktree"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Probe:
    """One executable check of the oracle: exit 0 on the tree = holds."""

    name: str
    command: str

    def render(self, *, case_dir: Path, worktree: Path) -> str:
        """The argv-ready command: each placeholder shell-quoted, so a path with
        a space or a quote survives the ``shlex.split`` the runner does."""
        return self.command.format(
            case_dir=shlex.quote(str(case_dir)), worktree=shlex.quote(str(worktree))
        )


@dataclass(frozen=True)
class ResolveCase:
    id: str
    description: str
    repo: str
    title: str
    acceptance_criteria: str
    merge_base: str
    base: str
    probes: tuple[Probe, ...]
    personas: tuple[str, ...]
    profile: str
    head: str = ""  # sha form
    head_patch: str | None = None  # patch form: merge_base + patch
    known_good: str = ""
    known_good_patch: str | None = None  # patch form: base + patch
    known_bad: str = ""
    known_bad_patch: str | None = None  # patch form: base + patch
    image: str | None = None
    # The project's develop settings a production `converge --story` run would
    # resolve from its context doc — declared HERE so the fixture is hermetic
    # and the gate the eval runs is the project's, not the profile catalog's
    # auto-detection alone (three of lens #43's conflicted paths are generated
    # docs; only the parity drift check sees a hand-merged one).
    parity_command: str | None = None
    test_command: str | None = None
    check_commands: Mapping[str, str] = field(default_factory=dict)
    check_states: Mapping[str, str] = field(default_factory=dict)
    artifacts_path: str | None = None
    case_dir: Path | None = None

    def develop_settings(self) -> dict:
        """The declared project settings, as ``DevelopConfig`` kwargs."""
        out: dict = {
            "check_commands": dict(self.check_commands),
            "check_states": dict(self.check_states),
        }
        for key in ("image", "parity_command", "test_command", "artifacts_path"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @property
    def tree_label(self) -> str:
        """A short human label: ``merge_base+head ⇐ base``."""
        head = self.head[:12] if self.head else str(self.head_patch)
        return f"{self.merge_base[:12]}+{head} ⇐ {self.base[:12]}"


def load_resolve_case(case_dir: Path) -> ResolveCase:
    """Load and validate ``case.toml`` + the AC file in *case_dir*."""
    data = tomllib.loads((case_dir / "case.toml").read_text(encoding="utf-8"))
    name = case_dir.name
    unknown_top = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown_top:
        raise ValueError(f"case {name}: unknown top-level table(s) {unknown_top}")
    case = data.get("case", {})
    unknown = sorted(set(case) - _CASE_KEYS)
    if unknown:
        raise ValueError(f"case {name}: unknown [case] key(s) {unknown}")
    cid = str(case.get("id") or "")
    if not cid:
        raise ValueError(f"case {name}: missing required field 'id'")
    description = str(case.get("description") or "").strip()
    if not description:
        raise ValueError(f"case {cid}: missing required field 'description'")
    repo = str(case.get("repo", "."))
    title = str(case.get("title") or cid).strip() or cid

    merge_base = _sha(cid, case, "merge_base")
    base = _sha(cid, case, "base")
    if merge_base == base:
        raise ValueError(
            f"case {cid}: 'merge_base' and 'base' must be distinct commits — a base "
            "that never moved has nothing to merge"
        )
    head, head_patch = _tree_spec(cid, case_dir, case, "head", "head_patch")
    known_good, known_good_patch = _tree_spec(
        cid, case_dir, case, "known_good", "known_good_patch"
    )
    known_bad, known_bad_patch = _tree_spec(
        cid, case_dir, case, "known_bad", "known_bad_patch"
    )
    where = f"case {cid}"
    try:
        image = parse_image(case.get("image"), where=where)
        parity_command = parse_parity_command(case.get("parity_command"), where=where)
        test_command = parse_test_command(case.get("test_command"), where=where)
        check_commands = parse_check_commands(case.get("check_commands"), where=where)
        check_states = parse_check_states(case.get("check_states"), where=where)
        artifacts_path = parse_artifacts_path(case.get("artifacts_path"), where=where)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    ac_file = str(case.get("acceptance_criteria_file", "ac.md"))
    acceptance = (case_dir / ac_file).read_text(encoding="utf-8").strip()
    if not acceptance:
        raise ValueError(f"case {cid}: empty acceptance criteria")

    # Fail closed on a typo'd profile / persona: a silent fallback would field
    # a different panel or check-set than the case declares (cf. eval review).
    profile = str(case.get("profile", "standard"))
    try:
        get_profile(profile)
    except UnknownProfileError as exc:
        raise ValueError(f"case {cid}: {exc}") from exc
    personas = tuple(str(p) for p in case.get("personas", ()))
    if not personas:
        raise ValueError(
            f"case {cid}: declare at least one persona (the panel that judges "
            "the composed tree)"
        )
    registry = canonical_personas()
    unknown_personas = [p for p in personas if p not in registry]
    if unknown_personas:
        raise ValueError(
            f"case {cid}: unknown persona(s) {unknown_personas}; "
            f"known: {', '.join(sorted(registry))}"
        )

    raw_probes = data.get("probe", [])
    if not raw_probes:
        raise ValueError(
            f"case {cid}: at least one [[probe]] is required — the oracle a "
            "correct resolution must satisfy"
        )
    probes = tuple(_parse_probe(cid, p) for p in raw_probes)
    names = [p.name for p in probes]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"case {cid}: duplicate probe name(s) {dupes}")

    return ResolveCase(
        id=cid,
        description=description,
        repo=repo,
        title=title,
        acceptance_criteria=acceptance,
        merge_base=merge_base,
        base=base,
        probes=probes,
        personas=personas,
        profile=profile,
        head=head,
        head_patch=head_patch,
        known_good=known_good,
        known_good_patch=known_good_patch,
        known_bad=known_bad,
        known_bad_patch=known_bad_patch,
        image=image,
        parity_command=parity_command,
        test_command=test_command,
        check_commands=check_commands,
        check_states=check_states,
        artifacts_path=artifacts_path,
        case_dir=case_dir,
    )


def _sha(cid: str, case: dict, key: str) -> str:
    value = str(case.get(key) or "")
    if not _SHA_RE.match(value):
        raise ValueError(
            f"case {cid}: '{key}' must be a full 40-hex commit sha (got {value!r})"
        )
    return value


def _tree_spec(
    cid: str, case_dir: Path, case: dict, sha_key: str, patch_key: str
) -> tuple[str, str | None]:
    """Exactly one of ``<sha_key>`` (a full sha) / ``<patch_key>`` (a file in
    the case dir, applied at run time)."""
    sha = str(case.get(sha_key) or "")
    patch = case.get(patch_key)
    if bool(sha) == bool(patch):
        raise ValueError(
            f"case {cid}: declare exactly one of '{sha_key}' / '{patch_key}'"
        )
    if sha:
        if not _SHA_RE.match(sha):
            raise ValueError(
                f"case {cid}: '{sha_key}' must be a full 40-hex commit sha "
                f"(got {sha!r})"
            )
        return sha, None
    patch_name = str(patch)
    if "/" in patch_name or not (case_dir / patch_name).is_file():
        raise ValueError(
            f"case {cid}: {patch_key} {patch_name!r} must name a file in the case dir"
        )
    return "", patch_name


def _parse_probe(cid: str, raw: dict) -> Probe:
    unknown = sorted(set(raw) - _PROBE_KEYS)
    if unknown:
        raise ValueError(f"case {cid}: unknown [[probe]] key(s) {unknown}")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError(f"case {cid}: a [[probe]] needs a non-empty name")
    command = str(raw.get("command") or "").strip()
    if not command:
        raise ValueError(f"case {cid}/{name}: a [[probe]] needs a non-empty command")
    placeholders = {
        field for _, field, _, _ in string.Formatter().parse(command) if field
    }
    unknown_ph = sorted(placeholders - _PROBE_PLACEHOLDERS)
    if unknown_ph:
        raise ValueError(
            f"case {cid}/{name}: unknown placeholder(s) in command: "
            + ", ".join("{" + p + "}" for p in unknown_ph)
            + " (known: {case_dir}, {worktree})"
        )
    return Probe(name=name, command=command)

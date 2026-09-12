"""Triage-eval case loader (PRD pr-reconciliation S8, triage shape).

A case is **one tree plus a batch of external-shaped findings** — exactly
what ``converge --from-github`` hands the S5a step: the batch goes through
the production intake (``external_intake_reviews``), so the ids, the
``[author]`` prefix, the single ``path:line`` anchor and the severity the
triage agent sees are the production ones, never the eval's. Every finding
declares the verdict a correct triage gives it:

- ``expected = "proceed"``: known-true (a real defect at that tree), or
  ``ambiguous = true`` (a design judgement, a documented deferral, a disputed
  AC reading). Both proceed under default-to-act; the eval scores them the
  same because the step must not distinguish them — but records which is
  which, so a rejection can be attributed. Rejecting one is
  **over-suppression**, the failure mode RH-1's lens34 result says to watch.
- ``expected = "reject"``: known-false — a claim the code refutes. It carries
  ``refutation``: the repo-relative files, optionally with a ``:start-end``
  line range, a correct rejection must cite. The production parser demands
  *a* resolving ``file:line``; the eval demands the *right* one, and a range
  keeps the claim's own anchor from scoring as evidence. A known-false may be
  ``synthetic`` (a closed question written to have an exact refutation) —
  say so in ``provenance``.

The tree is a ``sha`` or ``base`` + ``head_patch`` (applied at run time, so
a tree on no branch — a pre-squash tip — needs no kept-alive commit).
Finding ids are **positional** (``f-001`` first): the external ledger
assigns them in order, and a mismatch would silently score the wrong claim.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ...plugins.story_develop.config import parse_image

__all__ = ["Refutation", "TriageCase", "TriageFinding", "load_triage_case"]

_EXPECTED = ("proceed", "reject")
# external: a real reviewer's words; panel: loom's own panel wrote it (a
# known-good-arm finding from `eval review`); synthetic: written for the eval.
_PROVENANCES = ("external", "panel", "synthetic")
_TOP_LEVEL_KEYS = frozenset({"case", "finding"})
_CASE_KEYS = frozenset(
    {
        "id",
        "description",
        "repo",
        "sha",
        "base",
        "head_patch",
        "acceptance_criteria_file",
        "image",
    }
)
_FINDING_KEYS = frozenset(
    {
        "id",
        "author",
        "path",
        "line",
        "body",
        "expected",
        "ambiguous",
        "refutation",
        "provenance",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REFUTATION_RE = re.compile(r"^(?P<path>[^:]+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")
DEFAULT_AUTHOR = "reviewer"


@dataclass(frozen=True)
class Refutation:
    """A file (optionally a line range) a correct rejection must cite."""

    path: str
    start: int | None = None
    end: int | None = None

    def covers(self, path: str, line: int) -> bool:
        if path != self.path:
            return False
        if self.start is None:
            return True
        return self.start <= line <= (self.end if self.end is not None else self.start)

    @property
    def spec(self) -> str:
        if self.start is None:
            return self.path
        if self.end is None or self.end == self.start:
            return f"{self.path}:{self.start}"
        return f"{self.path}:{self.start}-{self.end}"


@dataclass(frozen=True)
class TriageFinding:
    """One claim in the batch + the verdict a correct triage gives it."""

    finding_id: str
    author: str
    path: str
    line: int | None
    body: str
    expected: str  # proceed | reject
    ambiguous: bool = False  # proceed only: a judgement, not a known-true
    refutation: tuple[Refutation, ...] = ()  # reject only
    provenance: str = "external"

    @property
    def anchor(self) -> str:
        if not self.path:
            return ""
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass(frozen=True)
class TriageCase:
    id: str
    description: str
    repo: str
    acceptance_criteria: str
    findings: tuple[TriageFinding, ...]
    sha: str = ""  # the sha form
    base: str = ""  # the patch form: base + head_patch (applied at run time)
    head_patch: str | None = None
    image: str | None = None
    case_dir: Path | None = None

    @property
    def known_true(self) -> tuple[TriageFinding, ...]:
        """Every finding that must PROCEED (known-true and ambiguous alike)."""
        return tuple(f for f in self.findings if f.expected == "proceed")

    @property
    def known_false(self) -> tuple[TriageFinding, ...]:
        return tuple(f for f in self.findings if f.expected == "reject")

    @property
    def tree_label(self) -> str:
        """A short human label for the tree under triage."""
        if self.sha:
            return self.sha[:12]
        return f"{self.base[:12]}+{self.head_patch}"


def load_triage_case(case_dir: Path) -> TriageCase:
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
    sha, base, head_patch = _tree_spec(cid, case_dir, case)
    repo = str(case.get("repo", "."))
    try:
        image = parse_image(case.get("image"), where=f"case {cid}")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    ac_file = str(case.get("acceptance_criteria_file", "ac.md"))
    acceptance = (case_dir / ac_file).read_text(encoding="utf-8").strip()
    if not acceptance:
        raise ValueError(f"case {cid}: empty acceptance criteria")

    raw_findings = data.get("finding", [])
    if not raw_findings:
        raise ValueError(f"case {cid}: at least one [[finding]] is required")
    findings = tuple(_parse_finding(cid, i, f) for i, f in enumerate(raw_findings))

    return TriageCase(
        id=cid,
        description=description,
        repo=repo,
        acceptance_criteria=acceptance,
        findings=findings,
        sha=sha,
        base=base,
        head_patch=head_patch,
        image=image,
        case_dir=case_dir,
    )


def _tree_spec(cid: str, case_dir: Path, case: dict) -> tuple[str, str, str | None]:
    sha = str(case.get("sha") or "")
    base = str(case.get("base") or "")
    head_patch = case.get("head_patch")
    if sha and (base or head_patch):
        raise ValueError(
            f"case {cid}: declare exactly one tree — 'sha', or 'base' + 'head_patch'"
        )
    if sha:
        if not _SHA_RE.match(sha):
            raise ValueError(
                f"case {cid}: 'sha' must be a full 40-hex commit sha (got {sha!r})"
            )
        return sha, "", None
    if not base or not head_patch:
        raise ValueError(
            f"case {cid}: declare exactly one tree — 'sha', or 'base' + 'head_patch'"
        )
    if not _SHA_RE.match(base):
        raise ValueError(
            f"case {cid}: 'base' must be a full 40-hex commit sha (got {base!r})"
        )
    patch_name = str(head_patch)
    if "/" in patch_name or not (case_dir / patch_name).is_file():
        raise ValueError(
            f"case {cid}: head_patch {patch_name!r} must name a file in the case dir"
        )
    return "", base, patch_name


def _parse_refutation(cid: str, fid: str, raw: object) -> Refutation:
    spec = str(raw)
    m = _REFUTATION_RE.match(spec)
    if not m or not m.group("path").strip():
        raise ValueError(
            f"case {cid}/{fid}: refutation entry must be 'path', 'path:LINE' or "
            f"'path:START-END' (got {spec!r})"
        )
    start = int(m.group("start")) if m.group("start") else None
    end = int(m.group("end")) if m.group("end") else None
    if start is not None and start < 1:
        raise ValueError(f"case {cid}/{fid}: refutation lines start at 1 ({spec!r})")
    if end is not None and end < (start or 1):
        raise ValueError(f"case {cid}/{fid}: refutation range is inverted ({spec!r})")
    return Refutation(path=m.group("path"), start=start, end=end)


def _parse_finding(cid: str, index: int, f: dict) -> TriageFinding:
    unknown = sorted(set(f) - _FINDING_KEYS)
    if unknown:
        raise ValueError(f"case {cid}: unknown [[finding]] key(s) {unknown}")
    fid = str(f.get("id") or "")
    want = f"f-{index + 1:03d}"
    if fid != want:
        raise ValueError(
            f"case {cid}: finding #{index + 1} must be id {want!r} — ids are "
            f"positional, the external ledger assigns them in order (got {fid!r})"
        )
    body = " ".join(str(f.get("body") or "").split())
    if not body:
        raise ValueError(f"case {cid}/{fid}: empty body")
    author = str(f.get("author") or DEFAULT_AUTHOR).strip()
    if not author:
        raise ValueError(f"case {cid}/{fid}: empty author")
    path = str(f.get("path") or "")
    line = f.get("line")
    if line is not None:
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ValueError(f"case {cid}/{fid}: line must be a positive integer")
        if not path:
            raise ValueError(f"case {cid}/{fid}: line needs a path")
    expected = str(f.get("expected") or "")
    if expected not in _EXPECTED:
        raise ValueError(
            f"case {cid}/{fid}: expected must be one of {_EXPECTED}, got {expected!r}"
        )
    ambiguous = f.get("ambiguous", False)
    if not isinstance(ambiguous, bool):
        raise ValueError(f"case {cid}/{fid}: ambiguous must be a boolean")
    if ambiguous and expected != "proceed":
        raise ValueError(
            f'case {cid}/{fid}: ambiguous is only meaningful with expected = "proceed"'
        )
    refutation = tuple(_parse_refutation(cid, fid, r) for r in f.get("refutation", ()))
    if expected == "reject" and not refutation:
        raise ValueError(
            f"case {cid}/{fid}: a known-false finding needs refutation — the repo "
            "files (with line ranges) a correct rejection must cite"
        )
    if expected == "proceed" and refutation:
        raise ValueError(
            f'case {cid}/{fid}: refutation is only meaningful with expected = "reject"'
        )
    provenance = str(f.get("provenance", "external"))
    if provenance not in _PROVENANCES:
        raise ValueError(
            f"case {cid}/{fid}: provenance must be one of {_PROVENANCES}, "
            f"got {provenance!r}"
        )
    return TriageFinding(
        finding_id=fid,
        author=author,
        path=path,
        line=line,
        body=body,
        expected=expected,
        ambiguous=ambiguous,
        refutation=refutation,
        provenance=provenance,
    )

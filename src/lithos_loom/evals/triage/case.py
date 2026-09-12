"""Triage-eval case loader (PRD pr-reconciliation S8, triage shape).

A case is **one repo sha plus a batch of findings** — the shape
:func:`~lithos_loom.plugins.story_develop.external_triage.triage_external_findings`
consumes — where every finding declares the verdict a correct triage gives it:

- ``expected = "proceed"``: known-true (a real defect at that sha), or
  *ambiguous* (a design judgement, a deferral, a disputed AC reading). Both
  proceed under default-to-act; the eval does not distinguish them because
  the triage step must not either. Rejecting one is **over-suppression**, the
  failure mode RH-1's lens34 result says to watch.
- ``expected = "reject"``: known-false — a claim the code refutes. It carries
  ``refutation_files``, the repo-relative files a correct rejection must cite
  (the parser already demands a resolving ``file:line``; the eval demands the
  *right* file). A known-false may be ``synthetic`` (a closed question written
  to have an exact refutation) — say so in ``provenance``.

Finding ids MUST be ``f-<digits>``: that is the only id shape the production
verdict parser recognises, so any other id could never be rejected and would
silently read as a correct PROCEED. The loader fails closed on that and on
every other field it cannot score.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TriageCase", "TriageFinding", "load_triage_case"]

_EXPECTED = ("proceed", "reject")
_SEVERITIES = ("critical", "major", "minor")
# external: a real reviewer's words; panel: loom's own panel wrote it (a
# known-good-arm finding from `eval review`); synthetic: written for the eval.
_PROVENANCES = ("external", "panel", "synthetic")
_TOP_LEVEL_KEYS = frozenset({"case", "finding"})
_CASE_KEYS = frozenset(
    {"id", "description", "repo", "sha", "acceptance_criteria_file", "image"}
)
_FINDING_KEYS = frozenset(
    {
        "id",
        "severity",
        "files",
        "rationale",
        "expected",
        "refutation_files",
        "provenance",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_FID_RE = re.compile(r"^f-\d+$")


@dataclass(frozen=True)
class TriageFinding:
    """One claim in the batch + the verdict a correct triage gives it."""

    finding_id: str
    severity: str
    files: tuple[str, ...]
    rationale: str
    expected: str  # proceed | reject
    refutation_files: tuple[str, ...] = ()  # reject only: files a rejection must cite
    provenance: str = "external"


@dataclass(frozen=True)
class TriageCase:
    id: str
    description: str
    repo: str
    sha: str
    acceptance_criteria: str
    findings: tuple[TriageFinding, ...]
    image: str | None = None
    case_dir: Path | None = None

    @property
    def known_true(self) -> tuple[TriageFinding, ...]:
        """Every finding that must PROCEED (known-true and ambiguous alike)."""
        return tuple(f for f in self.findings if f.expected == "proceed")

    @property
    def known_false(self) -> tuple[TriageFinding, ...]:
        return tuple(f for f in self.findings if f.expected == "reject")


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
    sha = str(case.get("sha") or "")
    if not _SHA_RE.match(sha):
        raise ValueError(
            f"case {cid}: 'sha' must be a full 40-hex commit sha (got {sha!r})"
        )
    repo = str(case.get("repo", "."))
    image = case.get("image")
    if image is not None and not str(image).strip():
        raise ValueError(f"case {cid}: 'image' must be a non-empty string when given")

    ac_file = str(case.get("acceptance_criteria_file", "ac.md"))
    acceptance = (case_dir / ac_file).read_text(encoding="utf-8").strip()
    if not acceptance:
        raise ValueError(f"case {cid}: empty acceptance criteria")

    raw_findings = data.get("finding", [])
    if not raw_findings:
        raise ValueError(f"case {cid}: at least one [[finding]] is required")
    findings = tuple(_parse_finding(cid, f) for f in raw_findings)
    ids = [f.finding_id for f in findings]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"case {cid}: duplicate finding id(s) {dupes}")

    return TriageCase(
        id=cid,
        description=description,
        repo=repo,
        sha=sha,
        acceptance_criteria=acceptance,
        findings=findings,
        image=str(image) if image is not None else None,
        case_dir=case_dir,
    )


def _parse_finding(cid: str, f: dict) -> TriageFinding:
    unknown = sorted(set(f) - _FINDING_KEYS)
    if unknown:
        raise ValueError(f"case {cid}: unknown [[finding]] key(s) {unknown}")
    fid = str(f.get("id") or "")
    if not _FID_RE.match(fid):
        raise ValueError(
            f"case {cid}: finding id must be f-<digits> (the verdict parser's id "
            f"shape — anything else can never be rejected), got {fid!r}"
        )
    severity = str(f.get("severity") or "")
    if severity not in _SEVERITIES:
        raise ValueError(
            f"case {cid}/{fid}: severity must be one of {_SEVERITIES}, got {severity!r}"
        )
    rationale = str(f.get("rationale") or "").strip()
    if not rationale:
        raise ValueError(f"case {cid}/{fid}: empty rationale")
    expected = str(f.get("expected") or "")
    if expected not in _EXPECTED:
        raise ValueError(
            f"case {cid}/{fid}: expected must be one of {_EXPECTED}, got {expected!r}"
        )
    refutation = tuple(str(p) for p in f.get("refutation_files", ()))
    if expected == "reject" and not refutation:
        raise ValueError(
            f"case {cid}/{fid}: a known-false finding needs refutation_files — the "
            "repo files a correct rejection must cite"
        )
    if expected == "proceed" and refutation:
        raise ValueError(
            f"case {cid}/{fid}: refutation_files is only meaningful with "
            'expected = "reject"'
        )
    provenance = str(f.get("provenance", "external"))
    if provenance not in _PROVENANCES:
        raise ValueError(
            f"case {cid}/{fid}: provenance must be one of {_PROVENANCES}, "
            f"got {provenance!r}"
        )
    return TriageFinding(
        finding_id=fid,
        severity=severity,
        files=tuple(str(p) for p in f.get("files", ())),
        rationale=rationale,
        expected=expected,
        refutation_files=refutation,
        provenance=provenance,
    )

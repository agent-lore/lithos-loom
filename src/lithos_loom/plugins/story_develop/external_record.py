"""What a ``converge --from-github`` run injected, kept on disk for later.

An external-mode converge run answers each reviewer where they raised their
finding — but a run that stops exhausted (``max_rounds`` / ``stalled`` /
``disputed`` / ``cost_exceeded``) can only answer *part* of the batch: it says
what triage refuted and what the coder disputed, and it cannot assert a fix,
because it pushed nothing. Its rounds sit on a local branch, and the material
they were about — which rows were injected, under which ids, and what triage
had already refuted — lives only in the process that died.

``develop converge-push`` is the operator's decision to keep those rounds, and
the push makes the rest of that batch answerable. So the intake is written
here, beside the handoffs the epilogue already reads, the moment triage has
settled and before the fix loop starts: the id→row map, triage's verdicts, and
the generated paths the tree comparison must ignore. Everything else the
epilogue needs (the acknowledgements, the worktree, the round count) is
already durable. ``converge-push`` reads the batch twice — as the run left it
and as the push makes it — and answers only the difference, so no thread is
replied to twice.

Best-effort on write — a converge run that cannot record this still converges
and still answers its threads itself; only the salvage path loses the replies,
and it says so rather than guessing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lithos_loom.github_review_activity import ReviewStream
from lithos_loom.github_review_streams import ReplyMode

from .external_reviews import ExternalFinding

logger = logging.getLogger(__name__)

__all__ = [
    "EXTERNAL_RECORD",
    "ExternalIntake",
    "read_external_intake",
    "record_external_intake",
]

EXTERNAL_RECORD = "external.json"


@dataclass(frozen=True)
class ExternalIntake:
    """The injected batch, as the fix loop received it."""

    id_map: dict[str, ExternalFinding]
    rejections: dict[str, str]
    nothing_to_remediate: dict[str, str]
    surviving_ids: tuple[str, ...]
    generated_paths: tuple[str, ...] = ()


def _finding_to_json(f: ExternalFinding) -> dict:
    return {
        "author": f.author,
        "source": f.source,
        "trusted": f.trusted,
        "stream": f.stream.value,
        "activity_id": f.activity_id,
        "reply_mode": f.reply_mode.value,
        "thread_url": f.thread_url,
        "head_sha": f.head_sha,
        "path": f.path,
        "line": f.line,
        "body": f.body,
        "severity": f.severity,
        "review_state": f.review_state,
    }


def _finding_from_json(data: Mapping) -> ExternalFinding:
    """Rebuild one finding. Raises ``ValueError`` on anything unreadable —
    a half-understood row would be answered on the wrong thread."""
    try:
        return ExternalFinding(
            author=str(data["author"]),
            source=str(data["source"]),
            trusted=bool(data["trusted"]),
            stream=ReviewStream(data["stream"]),
            activity_id=int(data["activity_id"]),
            reply_mode=ReplyMode(data["reply_mode"]),
            thread_url=str(data["thread_url"]),
            head_sha=str(data.get("head_sha", "")),
            path=str(data.get("path", "")),
            line=data.get("line") if isinstance(data.get("line"), int) else None,
            body=str(data.get("body", "")),
            severity=str(data.get("severity", "minor")),
            review_state=str(data.get("review_state", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unreadable external finding record: {exc}") from exc


def record_external_intake(
    run_dir: Path,
    *,
    id_map: Mapping[str, ExternalFinding],
    rejections: Mapping[str, str],
    nothing_to_remediate: Mapping[str, str],
    surviving_ids: Sequence[str],
    generated_paths: Sequence[str] = (),
) -> None:
    """Write the injected batch into *run_dir* (best-effort)."""
    payload = {
        "findings": {fid: _finding_to_json(f) for fid, f in id_map.items()},
        "rejections": dict(rejections),
        "nothing_to_remediate": dict(nothing_to_remediate),
        "surviving_ids": list(surviving_ids),
        "generated_paths": list(generated_paths),
    }
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / EXTERNAL_RECORD).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(
            "could not record the external intake in %s (%s); a later "
            "`develop converge-push` will have no threads to answer",
            run_dir,
            exc,
        )


def read_external_intake(run_dir: Path) -> ExternalIntake | None:
    """The injected batch, or ``None`` — no record, or one we cannot read.

    ``None`` is the local-panel case too (no external material was ever
    injected), which is why an absent record is not an error: the caller
    simply has no thread to answer.
    """
    try:
        data = json.loads((run_dir / EXTERNAL_RECORD).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), dict):
        return None
    id_map: dict[str, ExternalFinding] = {}
    for fid, raw in data["findings"].items():
        if not isinstance(raw, Mapping):
            return None
        try:
            id_map[str(fid)] = _finding_from_json(raw)
        except ValueError as exc:
            # All or nothing: a partially-read batch would answer some
            # threads and silently drop others.
            logger.warning("external intake in %s is unreadable: %s", run_dir, exc)
            return None
    return ExternalIntake(
        id_map=id_map,
        rejections=_str_map(data.get("rejections")),
        nothing_to_remediate=_str_map(data.get("nothing_to_remediate")),
        surviving_ids=tuple(
            str(i) for i in data.get("surviving_ids") or () if isinstance(i, str)
        ),
        generated_paths=tuple(
            str(p) for p in data.get("generated_paths") or () if isinstance(p, str)
        ),
    )


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): str(v) for k, v in value.items()}

"""``develop merge-gate`` subprocess plumbing for the watcher's base-move
re-gate dispatcher (:mod:`.merge_gate_dispatch`, PRD S3): the argv — pinned
to the story (the CURRENT ``develop_*`` settings defending the base) and to
the gate's repo (PR #362 review F2) — the per-gate ``--json`` paths, the
default spawn with its two wall-clock caps, and the settings probe
(``--resolve-only``) whose fingerprint keys the re-gate. Split out of the
dispatcher so the decision logic stays within the module budget.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lithos_loom.gates import PrGateSpec
from lithos_loom.subscriptions._subprocess import spawn_command

__all__ = [
    "OUTPUT_TAIL_CHARS",
    "PROBE_TIMEOUT_SECONDS",
    "RUN_TIMEOUT_SECONDS",
    "MergeGateSettings",
    "Spawn",
    "build_command",
    "json_path_for",
    "load_json",
    "output_tail",
    "probe_settings",
    "spawn_merge_gate",
]

# Wall-clock cap on one run (a full check-set in a container) and on the
# settings probe (config load + two Lithos reads — seconds, not minutes).
RUN_TIMEOUT_SECONDS = 2 * 3600
PROBE_TIMEOUT_SECONDS = 120

# The findings quote at most this much subprocess output.
OUTPUT_TAIL_CHARS = 600

Spawn = Callable[[list[str]], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class MergeGateSettings:
    """Host-side knobs the watcher child threads in from its config."""

    enabled: bool = True
    projects: Mapping[str, Path] = field(default_factory=dict)
    work_dir: Path = Path(".")
    # Forwarded to the subprocess as `--config` so it loads the same host
    # config this child did; None lets it fall back to env/CWD discovery.
    config_path: Path | None = None


async def spawn_merge_gate(cmd: list[str]) -> tuple[int, str]:
    """Default spawn: the merge-gate CLI, capped by whichever timeout the
    argv shape calls for (:func:`_subprocess.spawn_command`)."""
    if "--resolve-only" in cmd:
        return await spawn_command(
            cmd, timeout=PROBE_TIMEOUT_SECONDS, label="merge-gate settings probe"
        )
    return await spawn_command(cmd, timeout=RUN_TIMEOUT_SECONDS, label="merge-gate run")


def build_command(
    settings: MergeGateSettings,
    spec: PrGateSpec,
    repo: Path,
    json_path: Path,
    story_id: str,
    *,
    resolve_only: bool = False,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "lithos_loom",
        "develop",
        "merge-gate",
        str(spec.pr_number),
        # The story: the run resolves the project's + task's develop_*
        # settings (profile, check-set, image, test command, parity) —
        # the CURRENT config defending the base — strictly.
        "--story",
        story_id,
        "--repo",
        str(repo),
        # The checkout is pinned to the gate's repo (PR #362 review F2):
        # a PR number resolves against the checkout's origin, so a stale
        # [projects.<slug>].repo would otherwise trial-merge AND push
        # owner/other#N.
        "--expect-repo",
        spec.repo,
        "--json",
        str(json_path),
    ]
    if resolve_only:
        cmd.append("--resolve-only")
    if settings.config_path is not None:
        cmd += ["--config", str(settings.config_path)]
    return cmd


def json_path_for(settings: MergeGateSettings, gate_id: str, *, probe: bool) -> Path:
    kind = "probe" if probe else "run"
    path = settings.work_dir / "github-watcher" / f"merge-gate-{gate_id}-{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    return path


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def output_tail(output: str) -> str:
    return output[-OUTPUT_TAIL_CHARS:] if output else "(no output)"


async def probe_settings(
    spawn: Spawn,
    settings: MergeGateSettings,
    spec: PrGateSpec,
    repo: Path,
    story_id: str,
    gate_id: str,
) -> tuple[str, str | None]:
    """``(label, fingerprint)``: the story's current settings fingerprint,
    ``""`` when the config is unresolvable (exit 4 — that IS a state the
    key compares), ``None`` when the probe itself failed."""
    path = json_path_for(settings, gate_id, probe=True)
    rc, output = await spawn(
        build_command(settings, spec, repo, path, story_id, resolve_only=True)
    )
    if rc == 4:
        return "config_unresolved", ""
    data = load_json(path)
    fingerprint = None if data is None else data.get("settings_fingerprint")
    if rc != 0 or not isinstance(fingerprint, str):
        return f"exit {rc}: {output_tail(output)}", None
    return "resolved", fingerprint

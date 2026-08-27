"""Sandbox capability disclosure — what the container actually has (SC-1).

Agents do not know what their sandbox contains, so they assert absence and stop,
and reviewers believe them. On lens T1-S11 the coder declined to verify its own
rendering because "there is no Node/Chromium in the sandbox" and the reviewer
accepted it verbatim; the image had ``node``, ``playwright`` and a chromium
binary, and had already driven that browser to produce the very screenshots
under review.

The fix is to state the environment in the prompt. Two properties matter:

**Facts are MEASURED, never declared.** A hand-written capability block is
exactly the kind of unverified environment claim that caused the failure, and it
goes stale silently the first time the image is rebuilt. So they are probed from
the image, cached per image **id** — ``RepoDigests`` is empty for a locally-built
image, so the config digest (``docker inspect --format '{{.Id}}'``) is the only
stable identity available, and it changes on every rebuild, which is the
invalidation we want.

**A probe failure injects nothing.** Absence is only ever reported when the probe
ran and the tool was not there. :func:`probe_image` returns ``None`` for "could
not tell", distinct from a :class:`SandboxFacts` whose tool has no path — because
rendering "no browser" off a failed probe would recreate the original defect with
loom's authority behind it.

Layered like :mod:`test_gate`: pure builders/parsers/renderers (unit-tested
without Docker) plus thin side-effecting wrappers (monkeypatched in tests).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# One probe container per image, so the cost is a single ~1s container start
# against a run costing tens of dollars. Bounded by the number of distinct
# images a process touches (one, in practice).
PROBE_TIMEOUT = 120

# The probed catalog, deliberately small and fixed rather than an open-ended
# shell: (tool, argv that prints its version). ``go`` is the odd one out — it
# takes a subcommand, not a flag.
_TOOLS: tuple[tuple[str, str], ...] = (
    ("node", "node --version"),
    ("npm", "npm --version"),
    ("npx", ""),
    ("python3", "python3 --version"),
    ("uv", "uv --version"),
    ("go", "go version"),
    ("playwright", "playwright --version"),
)

# Where a playwright-managed chromium lands, and the plain-PATH alternatives.
# Globbed inside the container because the version segment moves per release.
_CHROMIUM_GLOBS = (
    "$PLAYWRIGHT_BROWSERS_PATH/chromium-*/chrome-linux64/chrome",
    "$PLAYWRIGHT_BROWSERS_PATH/chromium-*/chrome-linux/chrome",
)
_CHROMIUM_ON_PATH = ("chromium", "chromium-browser", "google-chrome", "chrome")

# Agent and gate containers take docker's default bridge — ``--network none``
# appears only in ``autoformat`` (the formatter, which reads no prompt). Loom
# builds that argv, so egress is a fact it KNOWS rather than one it must probe;
# probing it would mean a real outbound request from a throwaway container.
# ``test_sandbox_facts.py`` pins the claim against ``build_run_command``.
NETWORK_EGRESS_NOTE = (
    "network egress — available (agent and gate containers run on the default bridge)"
)


@dataclass(frozen=True)
class ToolFact:
    """One probed tool. ``path is None`` means probed and NOT present."""

    name: str
    path: str | None
    version: str | None = None

    @property
    def present(self) -> bool:
        return self.path is not None


@dataclass(frozen=True)
class SandboxFacts:
    """What one probe of one image found. Only ever built from a probe."""

    image: str
    image_id: str
    tools: tuple[ToolFact, ...]
    browsers_path: str | None = None
    chromium: str | None = None


def build_image_id_command(image: str) -> list[str]:
    """Resolve *image* to its content-addressed config digest.

    ``.Id`` and not ``.RepoDigests``: locally-built images are never pushed, so
    their ``RepoDigests`` is ``[]`` and a cache keyed on it would key on nothing.
    """
    return ["docker", "inspect", "--format", "{{.Id}}", image]


def build_capability_probe_command(image: str) -> list[str]:
    """One-shot ``docker run`` printing ``key=value`` capability lines.

    Same hardened, mount-free shape as :func:`test_gate.build_probe_command` —
    no workspace, no cache, no ``--shm-size``: this container runs no workload.
    Every version is truncated to its first line in-container so a multi-line
    ``--version`` cannot smuggle extra ``key=value`` lines into the output.
    """
    parts: list[str] = []
    for tool, version_cmd in _TOOLS:
        line = f'p=$(command -v {tool} 2>/dev/null) && printf "path.{tool}=%s\\n" "$p"'
        if version_cmd:
            line += (
                f' && printf "version.{tool}=%s\\n" '
                f'"$({version_cmd} 2>/dev/null | head -n 1)"'
            )
        parts.append(line)
    parts.append(
        '[ -n "$PLAYWRIGHT_BROWSERS_PATH" ] && '
        'printf "browsers_path=%s\\n" "$PLAYWRIGHT_BROWSERS_PATH"'
    )
    # First hit wins: a playwright-managed build, else a browser on PATH.
    chromium_probe = (
        "for c in "
        + " ".join(_CHROMIUM_GLOBS)
        + '; do [ -x "$c" ] && printf \'chromium=%s\\n\' "$c" && break; done'
    )
    parts.append(chromium_probe)
    for name in _CHROMIUM_ON_PATH:
        parts.append(
            f'c=$(command -v {name} 2>/dev/null) && printf "chromium=%s\\n" "$c"'
        )
    script = "; ".join(f"{{ {p}; }}" for p in parts) + "; true"
    return [
        "docker",
        "run",
        "--rm",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "sh",
        image,
        "-c",
        script,
    ]


def parse_probe_output(image: str, image_id: str, stdout: str) -> SandboxFacts:
    """Fold probe stdout into :class:`SandboxFacts` (pure, table-driven).

    Only allowlisted keys are read and only the FIRST value for each is kept —
    the probe emits one ``chromium=`` line per candidate and stops at the first
    playwright build, so "first wins" is the search order, and an unrecognised
    key from a chatty tool is discarded rather than becoming a fact.
    """
    seen: dict[str, str] = {}
    known = (
        {f"path.{t}" for t, _ in _TOOLS}
        | {f"version.{t}" for t, _ in _TOOLS}
        | {"browsers_path", "chromium"}
    )
    for raw in stdout.splitlines():
        key, sep, value = raw.partition("=")
        key = key.strip()
        if not sep or key not in known or key in seen:
            continue
        if value.strip():
            seen[key] = value.strip()
    tools = tuple(
        ToolFact(
            name=tool,
            path=seen.get(f"path.{tool}"),
            version=seen.get(f"version.{tool}"),
        )
        for tool, _ in _TOOLS
    )
    return SandboxFacts(
        image=image,
        image_id=image_id,
        tools=tools,
        browsers_path=seen.get("browsers_path"),
        chromium=seen.get("chromium"),
    )


def render_sandbox_facts(
    facts: SandboxFacts | None,
    *,
    for_coder: bool,
    guidance: str | None = None,
) -> str:
    """The ``{sandbox_facts}`` prompt section (empty when nothing was probed).

    ``for_coder`` switches audience exactly as
    :func:`check_set.render_check_summary` does. Both audiences get the measured
    facts — the reviewer needs them to falsify an environment claim. Only the
    coder gets *guidance*, which is operator how-to prose rather than a
    measurable property: instructions in a reviewer prompt would be off-lane.
    """
    if facts is None:
        return ""
    lines: list[str] = []
    for tool in facts.tools:
        if tool.path is None:
            lines.append(f"- `{tool.name}` — not present")
        elif tool.version:
            lines.append(f"- `{tool.name}` — `{tool.path}` ({tool.version})")
        else:
            lines.append(f"- `{tool.name}` — `{tool.path}`")
    if facts.chromium:
        lines.append(f"- chromium binary — `{facts.chromium}`")
    else:
        lines.append("- chromium binary — not present")
    if facts.browsers_path:
        lines.append(f"- `PLAYWRIGHT_BROWSERS_PATH` — `{facts.browsers_path}`")
    lines.append(f"- {NETWORK_EGRESS_NOTE}")

    out = [
        "## Sandbox environment",
        "",
        "**Measured**, by probing the image this run executes in — not declared, "
        "not assumed. Image `" + facts.image + "` (`" + facts.image_id[:19] + "…`).",
        "",
        *lines,
        "",
    ]
    if for_coder:
        out += [
            "Treat this list as authoritative for what the sandbox has. If a "
            "capability is listed here, it is available to you — check before "
            "reporting that something cannot be done for want of a tool.",
            "",
        ]
        if guidance:
            out += [
                "### Operator guidance for this image",
                "",
                "Declared by the operator, **not** measured — unlike the list "
                "above it describes how to use the environment, not what it "
                "contains.",
                "",
                guidance.strip(),
                "",
            ]
    return "\n".join(out)


# --- thin side-effecting wrappers (monkeypatched in unit tests) -------------


def resolve_image_id(image: str) -> str | None:
    """The image's config digest, or ``None`` when docker cannot tell us."""
    try:
        proc = subprocess.run(
            build_image_id_command(image),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def probe_image(image: str, image_id: str) -> SandboxFacts | None:
    """Probe *image* once, or ``None`` if the probe could not be run.

    ``None`` is "could not tell", NOT "nothing is there" — the caller must
    render nothing rather than an absence it never measured.
    """
    try:
        proc = subprocess.run(
            build_capability_probe_command(image),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return parse_probe_output(image, image_id, proc.stdout)


# Probe results, keyed BOTH by image id (so two tags of one build share a probe)
# and by the image ref the caller names (so a prompt site can look facts up
# knowing only the ref). Populated exclusively by :func:`prime`.
_FACTS_BY_ID: dict[str, SandboxFacts] = {}
_FACTS_BY_IMAGE: dict[str, SandboxFacts] = {}
_FAILED: set[str] = set()


def prime(image: str) -> str | None:
    """Probe *image* once and cache it; returns a friction line on failure.

    **The only function here that touches docker.** Entry points call it at run
    start, where a failure still has somewhere to be reported; prompt rendering
    then reads the cache and performs no I/O — which is what keeps ``make check``
    hermetic and means a forgotten stub can never start a container in a unit
    test.

    An image that was never primed simply renders nothing, the same as a failed
    probe: silence is the safe direction, an invented absence is not.
    """
    if image in _FACTS_BY_IMAGE:
        return None
    image_id = resolve_image_id(image)
    if image_id is None:
        return _fail_once(image, f"cannot inspect sandbox image {image!r}")
    facts = _FACTS_BY_ID.get(image_id) or probe_image(image, image_id)
    if facts is None:
        return _fail_once(image, f"capability probe failed for sandbox image {image!r}")
    _FACTS_BY_ID[image_id] = facts
    _FACTS_BY_IMAGE[image] = facts
    _FAILED.discard(image)
    return None


def for_prompt(image: str, *, for_coder: bool, guidance: str | None = None) -> str:
    """The ``{sandbox_facts}`` section for *image* (pure — reads the cache).

    Empty whenever *image* was never primed or its probe failed, so a caller can
    never render an absence nobody measured.
    """
    return render_sandbox_facts(
        _FACTS_BY_IMAGE.get(image), for_coder=for_coder, guidance=guidance
    )


def _fail_once(image: str, message: str) -> str | None:
    """Emit *message* the first time *image* fails; stay quiet after that."""
    full = (
        f"sandbox capability probe: {message}; agent prompts will not state "
        "its capabilities"
    )
    if image in _FAILED:
        logger.debug("%s (already reported)", full)
        return None
    _FAILED.add(image)
    logger.warning("%s", full)
    return full


def reset_cache() -> None:
    """Drop the probe cache (tests; a long-lived process re-probes on rebuild)."""
    _FACTS_BY_ID.clear()
    _FACTS_BY_IMAGE.clear()
    _FAILED.clear()

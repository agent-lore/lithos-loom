"""story-implement entry point.

Invoked by the daemon as::

    python -m lithos_loom.plugins.story_implement \\
        --task-json <path> --work-dir <path> --result-file <path>

Stub — superseded by ``story-develop`` and slated for removal; see US-2 in
docs/prd/orchestration.md.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub — superseded by story-develop; see US-2 in docs/prd/orchestration.md."""
    raise NotImplementedError(
        "story-implement is superseded by story-develop and slated for removal (US-2)"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

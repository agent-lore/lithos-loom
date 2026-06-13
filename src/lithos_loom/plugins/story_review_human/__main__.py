"""story-review-human entry point.

Invoked by the daemon as::

    python -m lithos_loom.plugins.story_review_human \\
        --task-json <path> --work-dir <path> --result-file <path>

Stub — superseded by ``story-develop`` + the pr-gate model and slated for
removal; see US-2 in docs/prd/orchestration.md.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub — superseded by story-develop; see US-2 in docs/prd/orchestration.md."""
    raise NotImplementedError(
        "story-review-human is superseded by story-develop + the pr-gate (US-2)"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

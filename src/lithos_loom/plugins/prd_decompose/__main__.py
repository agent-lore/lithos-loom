"""prd-decompose entry point.

Invoked by the daemon as::

    python -m lithos_loom.plugins.prd_decompose \\
        --task-json <path> --work-dir <path> --result-file <path>

Stub — see the `prd-decompose` story (US-22) in docs/prd/orchestration.md.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Stub — implement per the prd-decompose story in docs/prd/orchestration.md."""
    raise NotImplementedError("prd-decompose plugin — not yet implemented")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

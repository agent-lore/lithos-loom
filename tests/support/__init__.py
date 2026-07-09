"""Shared test-support helpers (ARCH-4).

Not collected as tests (no ``test_*`` modules); imported via
``from tests.support import FakeLithosClient, make_task, ...``.
"""

from __future__ import annotations

from tests.support.fake_lithos import (
    Call,
    FakeLithosClient,
    make_note,
    make_task,
)

__all__ = ["Call", "FakeLithosClient", "make_note", "make_task"]

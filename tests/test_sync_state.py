"""Direct unit tests for the concern-scoped projection sync states.

Most coordination behaviour is exercised through the projection / watcher
integration tests; this module pins the one piece of :mod:`sync_state`
that carries its own logic rather than being a plain data holder — the
:class:`ArchiveGateState` flush hook promoted to an explicit interface in
ARCH-10 (install once, request-flush is a no-op until then).
"""

from __future__ import annotations

from lithos_loom.sync_state import ArchiveGateState


async def test_request_flush_is_a_noop_when_no_hook_installed() -> None:
    """The archiver runs standalone when no projection wired the hook —
    ``request_flush`` must simply return without raising."""
    gate = ArchiveGateState()

    await gate.request_flush()  # no hook installed → silent no-op


async def test_request_flush_awaits_the_installed_hook() -> None:
    """Once a projection installs its flush-scheduler, ``request_flush``
    delegates to it (and awaits it)."""
    calls: list[str] = []

    async def _hook() -> None:
        calls.append("flushed")

    gate = ArchiveGateState()
    gate.install_flush_hook(_hook)

    await gate.request_flush()
    await gate.request_flush()

    assert calls == ["flushed", "flushed"]


async def test_install_flush_hook_replaces_a_prior_hook() -> None:
    """The last installer wins — exactly one projection owns the hook, so a
    re-install must fully supersede the previous callback."""
    calls: list[str] = []

    async def _first() -> None:
        calls.append("first")

    async def _second() -> None:
        calls.append("second")

    gate = ArchiveGateState()
    gate.install_flush_hook(_first)
    gate.install_flush_hook(_second)

    await gate.request_flush()

    assert calls == ["second"]

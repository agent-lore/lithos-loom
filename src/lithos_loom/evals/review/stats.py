"""Pure statistics helpers for the review eval (#182).

The harness reports a *point* catch-rate (caught / K); over K samples that point
estimate carries real sampling error. :func:`wilson_interval` gives the 95%
Wilson score interval so a catch-rate is read with its error bars — the
prerequisite for *measuring* (and later reducing) review-panel variance. Wilson
(not the normal approximation) is used so the bounds stay in ``[0, 1]`` and
behave at the extremes: ``20/20`` yields an upper bound that clamps to 1.0 but a
lower bound of ~0.84 (i.e. a single-pass miss-rate up to ~16% is not excluded).
"""

from __future__ import annotations

# Two-sided 95% normal quantile (z_{0.975}).
_Z_95 = 1.959963984540054


def wilson_interval(successes: int, n: int, *, z: float = _Z_95) -> tuple[float, float]:
    """95% Wilson score interval ``(lo, hi)`` in ``[0, 1]`` for ``successes`` / ``n``.

    Returns ``(0.0, 0.0)`` when ``n <= 0`` (no samples → no estimate).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))

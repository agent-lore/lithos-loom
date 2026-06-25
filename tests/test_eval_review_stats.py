"""Tests for the Wilson confidence interval helper (#182).

A catch-rate of caught/K is a point estimate with real sampling error; the
Wilson score interval gives its 95% bounds so the eval reports error bars, not a
bare percentage. These pin the known values + the edge behaviour the variance
work relies on (e.g. "20/20 still admits up to ~16% miss-rate").
"""

from __future__ import annotations

from lithos_loom.evals.review.stats import wilson_interval


def test_zero_n_returns_zero_interval() -> None:
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_known_midpoint_five_of_ten() -> None:
    lo, hi = wilson_interval(5, 10)  # p = 0.5
    assert abs(lo - 0.2366) < 0.005
    assert abs(hi - 0.7634) < 0.005


def test_all_success_lower_bound_is_below_one() -> None:
    # 20/20 never claims certainty: the upper bound clamps to 1.0, but the lower
    # bound (~0.84) means we can only say the miss-rate is below ~16%.
    lo, hi = wilson_interval(20, 20)
    assert hi == 1.0
    assert 0.83 < lo < 0.85


def test_all_failure_upper_bound_is_above_zero() -> None:
    # 0/20 symmetrically: lower bound 0.0, upper bound ~0.16.
    lo, hi = wilson_interval(0, 20)
    assert lo == 0.0
    assert 0.0 < hi < 0.2


def test_interval_brackets_the_point_estimate() -> None:
    lo, hi = wilson_interval(3, 5)  # p = 0.6
    assert 0.0 < lo < 0.6 < hi < 1.0

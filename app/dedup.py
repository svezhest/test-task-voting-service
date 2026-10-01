import math

import numpy
from scipy.optimize import brentq
from scipy.stats import poisson

HONEST_QUANTILE = 0.9999
LOG_BINS = 200
BISECTION_STEPS = 60


# STUB: simplification — docs suggest a precomputed table over a λ grid; we call scipy directly (exact, slower).
def poisson_limit(lam: float) -> int:
    return 1 + int(poisson.ppf(HONEST_QUANTILE, lam))


# STUB: simplification — the same: scipy directly instead of a precomputed table over a λ grid.
def poisson_limits(lam: numpy.ndarray) -> numpy.ndarray:
    return 1 + poisson.ppf(HONEST_QUANTILE, lam).astype(numpy.int64)


# exact, over the full array (stage 2 uses estimate_people_many)
def estimate_people(d: int, p: numpy.ndarray) -> float:
    if d == 0:
        return 0.0
    if d == 1:
        return 1.0
    if d >= len(p):  # all fingerprints of the poll are on this IP: no root
        return float(d)
    log_one_minus_p = numpy.log1p(-numpy.asarray(p, dtype=float))

    def expected_minus_observed(n):
        return -numpy.expm1(n * log_one_minus_p).sum() - d

    upper = 2.0
    while expected_minus_observed(upper) < 0:
        upper *= 2
    return brentq(expected_minus_observed, 0.0, upper)


def estimate_people_many(d: numpy.ndarray, p: numpy.ndarray) -> numpy.ndarray:
    """estimate_people for many d at once: p folded into log bins (mean p per bin), bisection per distinct d."""
    log_p = numpy.log(p)
    bin_sizes, bin_edges = numpy.histogram(log_p, bins=LOG_BINS)
    bin_p_sums = numpy.histogram(log_p, bins=bin_edges, weights=p)[0]
    non_empty = bin_sizes > 0
    bin_sizes = bin_sizes[non_empty]
    log_one_minus_bin_p = numpy.log1p(-bin_p_sums[non_empty] / bin_sizes)

    distinct_d, index_in_distinct = numpy.unique(d, return_inverse=True)
    solvable = (distinct_d > 1) & (distinct_d < len(p))
    target_d = numpy.where(solvable, distinct_d, 0)  # others have no root to find

    def expected_minus_observed(n):
        return -(bin_sizes * numpy.expm1(n[:, None] * log_one_minus_bin_p)).sum(axis=1) - target_d

    lower = numpy.zeros(len(distinct_d))
    upper = numpy.full(len(distinct_d), 2.0)
    upper_too_low = expected_minus_observed(upper) < 0
    while upper_too_low.any():
        upper[upper_too_low] *= 2
        upper_too_low = expected_minus_observed(upper) < 0
    for _ in range(BISECTION_STEPS):
        middle = (lower + upper) / 2
        root_above_middle = expected_minus_observed(middle) < 0
        lower = numpy.where(root_above_middle, middle, lower)
        upper = numpy.where(root_above_middle, upper, middle)
    estimate_per_distinct_d = numpy.where(solvable, (lower + upper) / 2, distinct_d)
    return estimate_per_distinct_d[index_in_distinct].astype(float)


def key_limit(n_limit: int, distinct_voters: int, r: int) -> int:
    return max(n_limit, distinct_voters - r)


def repeat_budget(window_seconds: float) -> int:
    return math.floor(window_seconds / 5)


def ip_ceiling(window_seconds: float) -> int:
    return 5000 * math.ceil(window_seconds / 60)

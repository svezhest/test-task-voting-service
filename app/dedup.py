import math

import numpy
from scipy.optimize import brentq
from scipy.stats import poisson


# STUB: simplification — docs suggest a precomputed table over a λ grid; we call scipy directly (exact, slower).
def poisson_limit(lam: float) -> int:
    return 1 + int(poisson.ppf(0.9999, lam))


# STUB: simplification — docs suggest folding p into ~200 log bins and bisection; we sum over the full array with brentq.
def estimate_people(d: int, p: numpy.ndarray) -> float:
    if d == 0:
        return 0.0
    if d == 1:
        return 1.0
    if d >= len(p):
        raise ValueError(f"d={d} must be less than the number of fingerprints ({len(p)})")
    log_q = numpy.log1p(-numpy.asarray(p, dtype=float))

    def f(n):
        return -numpy.expm1(n * log_q).sum() - d

    hi = 2.0
    while f(hi) < 0:
        hi *= 2
    return brentq(f, 0.0, hi)


def key_limit(n_limit: int, distinct_voters: int, r: int) -> int:
    return max(n_limit, distinct_voters - r)


def repeat_budget(window_seconds: float) -> int:
    return math.floor(window_seconds / 5)


def ip_ceiling(window_seconds: float) -> int:
    return 5000 * math.ceil(window_seconds / 60)

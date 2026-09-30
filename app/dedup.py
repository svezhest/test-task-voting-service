import math

import numpy
from scipy.optimize import brentq
from scipy.stats import poisson


# STUB: simplification — docs suggest a precomputed table over a λ grid; we call scipy directly (exact, slower).
def poisson_limit(lam: float) -> int:
    return 1 + int(poisson.ppf(0.9999, lam))


# STUB: simplification — the same: scipy directly instead of a precomputed table over a λ grid.
def poisson_limits(lam: numpy.ndarray) -> numpy.ndarray:  # poisson_limit for all keys at once
    return 1 + poisson.ppf(0.9999, lam).astype(numpy.int64)


def estimate_people(d: int, p: numpy.ndarray) -> float:  # exact, over the full array (stage 2 uses estimate_people_many)
    if d == 0:
        return 0.0
    if d == 1:
        return 1.0
    if d >= len(p):  # all fingerprints of the poll are on this IP: no root
        return float(d)
    log_q = numpy.log1p(-numpy.asarray(p, dtype=float))

    def f(n):
        return -numpy.expm1(n * log_q).sum() - d

    hi = 2.0
    while f(hi) < 0:
        hi *= 2
    return brentq(f, 0.0, hi)


def estimate_people_many(d: numpy.ndarray, p: numpy.ndarray) -> numpy.ndarray:
    """estimate_people for many d at once: p folded into 200 log bins (mean p of each bin), bisection for each distinct d."""
    log_p = numpy.log(p)
    counts, edges = numpy.histogram(log_p, bins=200)
    sums = numpy.histogram(log_p, bins=edges, weights=p)[0]
    counts, log_q = counts[counts > 0], numpy.log1p(-sums[counts > 0] / counts[counts > 0])
    u, back = numpy.unique(d, return_inverse=True)
    solvable = (u > 1) & (u < len(p))
    target = numpy.where(solvable, u, 0)  # others have no root to find

    def f(n):
        return -(counts * numpy.expm1(n[:, None] * log_q)).sum(axis=1) - target

    lo, hi = numpy.zeros(len(u)), numpy.full(len(u), 2.0)
    while (short := f(hi) < 0).any():
        hi[short] *= 2
    for _ in range(60):
        mid = (lo + hi) / 2
        below = f(mid) < 0
        lo, hi = numpy.where(below, mid, lo), numpy.where(below, hi, mid)
    return numpy.where(solvable, (lo + hi) / 2, u)[back].astype(float)


def key_limit(n_limit: int, distinct_voters: int, r: int) -> int:
    return max(n_limit, distinct_voters - r)


def repeat_budget(window_seconds: float) -> int:
    return math.floor(window_seconds / 5)


def ip_ceiling(window_seconds: float) -> int:
    return 5000 * math.ceil(window_seconds / 60)

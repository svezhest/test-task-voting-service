import math

import numpy as np
import pytest

from app.dedup import estimate_people, ip_ceiling, key_limit, poisson_limit, repeat_budget

# Таблица N из architecture_deduplication.md: строки — людей за IP (n), столбцы — p(f); λ = n·p.
N_TABLE = [
    (3, 0.02, 3), (3, 0.001, 2), (3, 0.0001, 2),
    (50, 0.02, 7), (50, 0.001, 3), (50, 0.0001, 2),
    (1000, 0.02, 40), (1000, 0.001, 7), (1000, 0.0001, 4),
    (3000, 0.02, 92), (3000, 0.001, 12), (3000, 0.0001, 5),
]


@pytest.mark.parametrize("n,p,expected", N_TABLE)
def test_poisson_limit_matches_doc_table(n, p, expected):
    assert poisson_limit(n * p) == expected


def test_poisson_limit_zero_lambda():
    # Пуассон(0) всегда 0, значит Q = 0 и N = 1.
    assert poisson_limit(0.0) == 1


def test_poisson_limit_non_decreasing():
    lams = [0, 0.001, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 20, 60, 100]
    limits = [poisson_limit(x) for x in lams]
    assert limits == sorted(limits)


def uniform(m):
    return np.full(m, 1.0 / m)


NON_UNIFORM = np.array([0.5] + [0.5 / 99] * 99)


@pytest.mark.parametrize("p", [uniform(100), NON_UNIFORM])
def test_estimate_people_zero(p):
    assert estimate_people(0, p) == 0


@pytest.mark.parametrize("p", [uniform(100), NON_UNIFORM])
def test_estimate_people_one(p):
    assert estimate_people(1, p) == pytest.approx(1, rel=1e-3)


@pytest.mark.parametrize("p", [uniform(100), NON_UNIFORM])
def test_estimate_people_increasing_in_d(p):
    values = [estimate_people(d, p) for d in range(len(p))]
    assert all(a < b for a, b in zip(values, values[1:]))


@pytest.mark.parametrize("extra", [0, 1, 10])
def test_estimate_people_d_too_large(extra):
    # contracts.md: d >= len(p) — корня нет, возвращает float(d).
    p = uniform(50)
    d = len(p) + extra
    r = estimate_people(d, p)
    assert isinstance(r, float)
    assert r == d


@pytest.mark.parametrize("d", [2, 10, 100, 500, 900, 990])
def test_estimate_people_uniform_matches_analytic(d):
    # p_f = 1/M: D = M·(1 − (1 − 1/M)^n)  ⇒  n = ln(1 − D/M) / ln(1 − 1/M).
    # Допуск 10%: точность n̂ при гистограмме из ~200 корзин в docs не задана (см. отчёт).
    m = 1000
    expected = math.log(1 - d / m) / math.log(1 - 1 / m)
    assert estimate_people(d, uniform(m)) == pytest.approx(expected, rel=0.1)


def test_estimate_people_single_fingerprint():
    assert estimate_people(1, np.array([1.0])) == pytest.approx(1)


# Таблица «Кто за ключом» из architecture_deduplication.md, окно 60 с → R = 12.
@pytest.mark.parametrize("v", [1, 5, 12])
def test_key_limit_single_cheater_gets_n(v):
    assert key_limit(2, v, 12) == 2


def test_key_limit_family_of_three_iphones():
    assert key_limit(3, 3, 12) == 3


def test_key_limit_300_identical_ipads():
    assert key_limit(2, 300, 12) == 288


def test_key_limit_is_max():
    assert key_limit(290, 300, 12) == 290
    assert key_limit(7, 13, 12) == 7


@pytest.mark.parametrize("seconds,expected", [(60, 12), (64.9, 12), (65, 13), (4.9, 0), (5, 1)])
def test_repeat_budget(seconds, expected):
    assert repeat_budget(seconds) == expected


@pytest.mark.parametrize("seconds,expected", [(60, 5000), (61, 10000), (1, 5000), (120, 10000), (121, 15000)])
def test_ip_ceiling(seconds, expected):
    assert ip_ceiling(seconds) == expected

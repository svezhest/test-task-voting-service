import uuid

import pytest

# Допуск 1e-3 у долей: в примере api.md доли округлены до 3 знаков.


def opt(results, idx):
    for o in results["options"]:
        if o["idx"] == idx:
            return o
    return {"idx": idx, "counted": 0, "total": 0}


def ok(r):
    assert r.status_code == 204, r.text


def test_one_vote_per_voter_id_across_ips(make_poll, vote, wait_final):
    p = make_poll()
    voter = uuid.uuid4()
    fp = {"model": "phone-x"}
    ok(vote(p["id"], [0], voter, fp, ip="198.51.100.1"))  # первый голос засчитывается
    ok(vote(p["id"], [1], voter, fp, ip="198.51.100.2"))
    ok(vote(p["id"], [1], voter, fp, ip="198.51.100.3"))
    ok(vote(p["id"], [1], None, {"model": "other"}, ip="198.51.100.4"))

    res = wait_final(p)
    assert res["received"] == 4
    assert res["total"] == 2
    assert res["counted"] == 2
    assert (opt(res, 0)["total"], opt(res, 0)["counted"]) == (1, 1)
    assert (opt(res, 1)["total"], opt(res, 1)["counted"]) == (1, 1)


def test_distinct_ips_and_fps_all_counted(make_poll, vote, wait_final):
    p = make_poll()
    for i in range(20):
        ok(vote(p["id"], [i % 2], None, {"model": f"dev-{i}"}, ip=f"198.51.100.{i + 1}"))

    res = wait_final(p)
    assert res["received"] == res["total"] == res["counted"] == 20
    assert opt(res, 0)["counted"] == opt(res, 1)["counted"] == 10
    assert res["over_limit_share"] == pytest.approx(0)


def test_multi_counts_each_option(make_poll, vote, wait_final):
    p = make_poll(labels=("A", "B", "C"), type="multi")
    ok(vote(p["id"], [0, 1], None, {"model": "m1"}, ip="198.51.100.1"))
    ok(vote(p["id"], [1], None, {"model": "m2"}, ip="198.51.100.2"))
    ok(vote(p["id"], [2], None, {"model": "m3"}, ip="198.51.100.3"))

    res = wait_final(p)
    assert res["total"] == res["counted"] == 3  # число голосов, а не сумма по вариантам
    assert [opt(res, i)["counted"] for i in range(3)] == [1, 2, 1]
    assert [opt(res, i)["total"] for i in range(3)] == [1, 2, 1]


def test_multi_share(make_poll, vote, wait_final):
    # share = counted варианта / counted в корне; у multi сумма долей может быть > 1.
    p = make_poll(labels=("A", "B"), type="multi")
    ok(vote(p["id"], [0, 1], None, {"model": "m1"}, ip="198.51.100.1"))
    ok(vote(p["id"], [0], None, {"model": "m2"}, ip="198.51.100.2"))

    res = wait_final(p)
    assert opt(res, 0)["share"] == pytest.approx(1.0)
    assert opt(res, 1)["share"] == pytest.approx(0.5, abs=1e-3)


def test_zero_votes_shares_null(make_poll, wait_final):
    p = make_poll(window_s=3)
    res = wait_final(p)
    assert (res["received"], res["total"], res["counted"]) == (0, 0, 0)
    assert res["over_limit_share"] is None
    for o in res["options"]:
        assert o["share"] is None


def test_single_ip_single_fp_all_counted(make_poll, vote, wait_final):
    # Все отпечатки опроса на одном IP: D = len(p) → n̂ = D (contracts.md), голос засчитан.
    p = make_poll()
    ok(vote(p["id"], [0], None, {"model": "only"}, ip="198.51.100.1"))
    res = wait_final(p)
    assert (res["received"], res["total"], res["counted"]) == (1, 1, 1)


def test_results_format(make_poll, vote, wait_final):
    p = make_poll(labels=("Да", "Нет"))
    voter = uuid.uuid4()
    ok(vote(p["id"], [0], voter, {"model": "f1"}, ip="198.51.100.1"))
    ok(vote(p["id"], [0], voter, {"model": "f1"}, ip="198.51.100.2"))  # повтор: в received, не в total
    ok(vote(p["id"], [1], None, {"model": "f2"}, ip="198.51.100.3"))
    ok(vote(p["id"], [0], None, {"model": "f3"}, ip="198.51.100.4"))

    res = wait_final(p)
    assert {"status", "received", "total", "counted", "over_limit_share", "options"} <= res.keys()
    assert res["status"] == "final"
    assert (res["received"], res["total"], res["counted"]) == (4, 3, 3)
    assert res["over_limit_share"] == pytest.approx(1 - res["counted"] / res["total"], abs=1e-3)
    assert sorted(o["idx"] for o in res["options"]) == [0, 1]
    for o in res["options"]:
        assert {"idx", "label", "counted", "total", "share"} <= o.keys()
        assert o["label"] == p["labels"][o["idx"]]
        assert o["share"] == pytest.approx(o["counted"] / res["counted"], abs=1e-3)  # share — от засчитанных
    assert (opt(res, 0)["counted"], opt(res, 1)["counted"]) == (2, 1)


# Окно 60 с, grace_s = 0 → R = floor(60 / 5) = 12, потолок на IP 5000 (не мешает).
# На каждом IP один отпечаток → D = 1 → n̂ = 1 (контракт estimate_people) → λ = p(f) ≤ 1
# → N ≤ poisson_limit(1) = 7 (таблица N: 50 человек × 2%).
WINDOW = 60
R = WINDOW // 5
N_MAX = 7


@pytest.fixture(scope="module")
def limits_result(make_poll, vote, wait_final):
    p = make_poll(window_s=WINDOW)
    for _ in range(R):  # накрутчик в инкогнито: один IP, один fp, R разных voter_id
        ok(vote(p["id"], [0], None, {"model": "cheater"}, ip="192.0.2.10"))
    for _ in range(300):  # 300 одинаковых устройств за NAT
        ok(vote(p["id"], [1], None, {"model": "ipad"}, ip="192.0.2.20"))
    return wait_final(p)


def test_incognito_cheater_capped(limits_result):
    # V = R → L = max(N, V − R) = N ≤ 7.
    o = opt(limits_result, 0)
    assert o["total"] == R
    assert 1 <= o["counted"] <= N_MAX


def test_nat_300_identical_devices(limits_result):
    # L = max(N, 300 − R) = 288, так как N ≤ 7 (таблица «Кто за ключом»: 300 − 12 = 288).
    o = opt(limits_result, 1)
    assert o["total"] == 300
    assert o["counted"] == 300 - R


def test_limits_root_totals(limits_result):
    res = limits_result
    assert res["received"] == res["total"] == R + 300
    assert res["counted"] == opt(res, 0)["counted"] + opt(res, 1)["counted"]
    assert res["over_limit_share"] == pytest.approx(1 - res["counted"] / res["total"], abs=1e-3)

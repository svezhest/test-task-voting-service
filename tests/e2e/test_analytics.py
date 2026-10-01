import datetime as dt
import time
import uuid

import pytest

from conftest import AUTH
BUCKETS = {"1", "2-10", "11-100", "101+"}


def analytics(admin, pid):
    r = admin.get(f"/admin/polls/{pid}/analytics", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def bucket(a, name):
    return next((b["ips"], b["votes"]) for b in a["ip_concentration"] if b["bucket"] == name)


def ok(r):
    assert r.status_code == 204, r.text


@pytest.fixture(scope="module")
def active_poll(make_poll, vote, admin):
    p = make_poll(window_s=30)
    for i in range(3):
        ok(vote(p["id"], [0], None, {"model": f"a{i}"}, ip=f"198.51.100.{i + 1}"))
    # Этап 1 пишет timeline раз в секунду.
    deadline = time.time() + 10
    while time.time() < deadline:
        a = analytics(admin, p["id"])
        if sum(t["received"] for t in a["timeline"] or []) == 3:
            return p, a
        time.sleep(0.5)
    pytest.fail(f"timeline не показал 3 голоса за 10 с: {a}")


def test_active_timeline(active_poll):
    p, a = active_poll
    assert a["status"] == "active"
    assert dt.datetime.fromisoformat(a["window_start"]).timestamp() == p["window_start"]
    assert [t["t"] for t in a["timeline"]] == list(range(len(a["timeline"])))
    assert a["timeline"][-1]["received"] > 0


def test_active_funnel_and_ip_null(active_poll):
    _, a = active_poll
    assert a["funnel"] is None
    assert a["ip_concentration"] is None


def test_active_model(active_poll):
    # api.md: model доступна и во время эфира; параметры — из architecture.md.
    _, a = active_poll
    assert a["model"] == {"median_s": 14, "sigma": 0.5}


def test_active_results_received(admin, active_poll):
    p, _ = active_poll
    r = admin.get(f"/admin/polls/{p['id']}/results", headers=AUTH).json()
    assert r == {"status": "active", "received": 3}


# Окно 20 с, grace_s = 0 → R = 4, потолок на IP 5000 (не срабатывает).
@pytest.fixture(scope="module")
def final(make_poll, vote, wait_final, admin):
    p = make_poll(window_s=20)
    repeat = uuid.uuid4()
    for _ in range(3):  # один voter_id трижды → repeat_voter = 2
        ok(vote(p["id"], [0], repeat, {"model": "rep"}, ip="192.0.2.1"))
    for i in range(5):  # обычные зрители: по одному на IP
        ok(vote(p["id"], [i % 2], None, {"model": f"u{i}"}, ip=f"192.0.2.{i + 2}"))
    for _ in range(20):  # накрутчик: один IP, один fp, 20 voter_id → V − L = min(V − N, R) > 0
        ok(vote(p["id"], [0], None, {"model": "cheater"}, ip="192.0.2.100"))
    for _ in range(300):  # 300 голосов с одного IP → диапазон «101+»
        ok(vote(p["id"], [1], None, {"model": "ipad"}, ip="192.0.2.200"))
    res = wait_final(p)
    return p, res, analytics(admin, p["id"])


def test_final_format(final):
    p, _, a = final
    assert a.keys() == {"status", "window_start", "timeline", "model", "funnel", "ip_concentration"}
    assert a["status"] == "final"
    assert dt.datetime.fromisoformat(a["window_start"]).timestamp() == p["window_start"]
    assert a["model"] == {"median_s": 14, "sigma": 0.5}  # architecture.md
    assert a["funnel"].keys() == {"received", "unique_voters", "counted", "rejected"}
    assert a["funnel"]["rejected"].keys() == {"repeat_voter", "key_limit", "ip_ceiling"}
    assert len(a["ip_concentration"]) == 4
    assert {b["bucket"] for b in a["ip_concentration"]} == BUCKETS
    for b in a["ip_concentration"]:
        assert b.keys() == {"bucket", "ips", "votes"}


def test_final_timeline_matches_received(final):
    _, res, a = final
    assert [t["t"] for t in a["timeline"]] == list(range(len(a["timeline"])))
    assert sum(t["received"] for t in a["timeline"]) == res["received"] == 328


def test_funnel_matches_results(final):
    _, res, a = final
    f = a["funnel"]
    assert f["received"] == res["received"]
    assert f["unique_voters"] == res["total"]
    assert f["counted"] == res["counted"]


def test_funnel_rejected_adds_up(final):
    _, _, a = final
    f = a["funnel"]
    rej = f["rejected"]
    assert f["received"] - f["unique_voters"] == rej["repeat_voter"]
    assert f["unique_voters"] - f["counted"] == rej["key_limit"] + rej["ip_ceiling"]


def test_funnel_repeat_and_key_limit(final):
    _, _, a = final
    rej = a["funnel"]["rejected"]
    assert rej["repeat_voter"] == 2
    assert rej["key_limit"] > 0
    assert rej["ip_ceiling"] == 0


def test_ip_concentration(final):
    _, _, a = final
    assert sum(b["votes"] for b in a["ip_concentration"]) == a["funnel"]["unique_voters"]
    assert bucket(a, "1") == (6, 6)  # 5 обычных + повторщик (после этапа 1 — один голос)
    assert bucket(a, "2-10") == (0, 0)
    assert bucket(a, "11-100") == (1, 20)
    assert bucket(a, "101+") == (1, 300)


def test_draft_analytics(admin, make_poll):
    p = make_poll(active=False)
    a = analytics(admin, p["id"])
    assert a["status"] == "draft"
    assert a["timeline"] == []
    assert a["funnel"] is None
    assert a["ip_concentration"] is None


def test_zero_votes_analytics(admin, make_poll, wait_final):
    p = make_poll(window_s=3)
    wait_final(p)
    a = analytics(admin, p["id"])
    assert a["timeline"] == []
    assert a["funnel"] == {"received": 0, "unique_voters": 0, "counted": 0,
                           "rejected": {"repeat_voter": 0, "key_limit": 0, "ip_ceiling": 0}}
    assert sorted(a["ip_concentration"], key=lambda b: b["bucket"]) == [
        {"bucket": b, "ips": 0, "votes": 0} for b in sorted(BUCKETS)]

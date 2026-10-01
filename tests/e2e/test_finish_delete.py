import datetime as dt
import time

import pytest

from conftest import AUTH


def ts(s):
    return dt.datetime.fromisoformat(s).timestamp()


def ok(r):
    assert r.status_code == 204, r.text


def opt(results, idx):
    return next((o for o in results["options"] if o["idx"] == idx), {"idx": idx, "counted": 0, "total": 0})


def finish(admin, pid):
    return admin.post(f"/admin/polls/{pid}/finish", headers=AUTH)


# --- finish ---------------------------------------------------------------

def test_finish_sets_window_end_now(admin, make_poll):
    # api.md: finish ставит window_end = now, grace_s не меняет; ответ — объект опроса.
    p = make_poll(window_s=60)
    before = time.time()
    r = finish(admin, p["id"])
    after = time.time()
    assert r.status_code < 300, r.text
    poll = r.json()
    assert poll["id"] == p["id"]
    assert poll["grace_s"] == 0
    assert ts(poll["window_start"]) == p["window_start"]
    assert before - 1.5 <= ts(poll["window_end"]) <= after + 1.5  # допуск на часы контейнера и округление
    got = admin.get(f"/admin/polls/{p['id']}", headers=AUTH).json()
    assert ts(got["window_end"]) == ts(poll["window_end"])


def test_finish_late_votes_not_counted(admin, make_poll, vote, wait_final):
    # contracts.md: приём может принять голос в первую секунду после finish (кэш),
    # но голоса с received_at позже window_end + grace_s в итог не попадают.
    p = make_poll(window_s=60)
    ok(vote(p["id"], [0], None, {"model": "f1"}, ip="198.51.100.1"))
    ok(vote(p["id"], [1], None, {"model": "f2"}, ip="198.51.100.2"))
    time.sleep(1.1)  # received_at этих голосов точно раньше нового window_end, даже если его округлят до секунды

    r = finish(admin, p["id"])
    assert r.status_code < 300, r.text
    new_end = ts(r.json()["window_end"])

    sent = time.time()
    late = vote(p["id"], [0], None, {"model": "f3"}, ip="198.51.100.3")
    assert late.status_code in (204, 410), late.text

    time.sleep(3)  # приём перечитывает опросы раз в секунду — теперь уже 410
    assert vote(p["id"], [0], None, {"model": "f4"}, ip="198.51.100.4").status_code == 410

    p["window_end"] = new_end
    res = wait_final(p)
    if late.status_code == 204 and sent <= new_end:
        pytest.skip(f"поздний голос отправлен не позже window_end ({sent} <= {new_end}): проверить нечего")
    assert (res["received"], res["total"], res["counted"]) == (2, 2, 2), res
    assert (opt(res, 0)["total"], opt(res, 1)["total"]) == (1, 1)


def test_finish_draft_409(admin, make_poll):
    p = make_poll(active=False)
    assert finish(admin, p["id"]).status_code == 409


def test_finish_before_window_409(admin, make_poll):
    p = make_poll(start_in=60, window_s=10)
    assert finish(admin, p["id"]).status_code == 409


def test_finish_after_window_end_409(admin, make_poll):
    # finish — только пока window_start ≤ now < window_end.
    p = make_poll(window_s=2)
    time.sleep(max(0.0, p["window_end"] + 0.5 - time.time()))
    assert finish(admin, p["id"]).status_code == 409


def test_finish_twice_409(admin, make_poll):
    p = make_poll(window_s=60)
    assert finish(admin, p["id"]).status_code < 300
    time.sleep(1.1)  # now > нового window_end даже при округлении до секунды
    assert finish(admin, p["id"]).status_code == 409


def test_finish_final_409(admin, make_poll, wait_final):
    p = make_poll(window_s=2)
    wait_final(p)
    assert finish(admin, p["id"]).status_code == 409


# --- DELETE ---------------------------------------------------------------

def test_delete_draft(admin, make_poll):
    p = make_poll(active=False)
    r = admin.delete(f"/admin/polls/{p['id']}", headers=AUTH)
    assert r.status_code == 204, r.text
    assert admin.get(f"/admin/polls/{p['id']}", headers=AUTH).status_code == 404
    assert p["id"] not in [x["id"] for x in admin.get("/admin/polls", headers=AUTH).json()]
    assert admin.delete(f"/admin/polls/{p['id']}", headers=AUTH).status_code == 404


@pytest.mark.parametrize("start_in", [0, 60], ids=["in-window", "before-window"])
def test_delete_active_409(admin, make_poll, start_in):
    p = make_poll(start_in=start_in, window_s=30)
    assert admin.delete(f"/admin/polls/{p['id']}", headers=AUTH).status_code == 409
    got = admin.get(f"/admin/polls/{p['id']}", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["status"] == "active"


def test_delete_final(admin, viewer, make_poll, vote, wait_final):
    p = make_poll(window_s=3)
    ok(vote(p["id"], [0], None, {"model": "d1"}, ip="198.51.100.1"))
    wait_final(p)
    pid = p["id"]
    assert viewer.get(f"/p/{pid}/config.json").status_code == 200

    r = admin.delete(f"/admin/polls/{pid}", headers=AUTH)
    assert r.status_code == 204, r.text

    for path in (f"/admin/polls/{pid}", f"/admin/polls/{pid}/results", f"/admin/polls/{pid}/analytics"):
        assert admin.get(path, headers=AUTH).status_code == 404, path
    assert pid not in [x["id"] for x in admin.get("/admin/polls", headers=AUTH).json()]
    assert viewer.get(f"/p/{pid}/config.json").status_code == 404

    # Приём перечитывает опросы раз в секунду: до этого может ответить 410 (final).
    deadline = time.time() + 5
    while (code := vote(pid, [0]).status_code) != 404 and time.time() < deadline:
        time.sleep(0.5)
    assert code == 404

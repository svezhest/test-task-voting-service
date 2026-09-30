import uuid

import pytest

AUTH = {"Authorization": "Bearer dev-token"}


def ok(r):
    assert r.status_code == 204, r.text


# contracts.md «Стенд»: на зрительском входе /admin/ и /manage/ — 404 (в том числе с верным токеном).
@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/polls"),
    ("POST", "/admin/polls"),
    ("GET", f"/admin/polls/{uuid.uuid4()}"),
    ("GET", "/admin/public-url"),
    ("GET", "/manage/"),
    ("GET", "/manage/index.html"),
])
def test_admin_not_on_viewer_entry(viewer, method, path):
    r = viewer.request(method, path, headers=AUTH, json={} if method == "POST" else None)
    assert r.status_code == 404, r.text


def test_manage_page_on_admin_entry(admin):
    assert admin.get("/manage/").status_code == 200


def test_viewer_entry_serves_page_and_config(viewer, make_poll):
    p = make_poll()
    assert viewer.get(f"/p/{p['id']}").status_code == 200
    r = viewer.get(f"/p/{p['id']}/config.json")
    assert r.status_code == 200
    assert r.json()["id"] == p["id"]


def test_cf_connecting_ip_beats_xff(make_poll, vote, wait_final, admin):
    # contracts.md: IP клиента — CF-Connecting-IP, если есть; X-Forwarded-For — только без него.
    # Два voter_id, один fp, разные XFF, один CF-Connecting-IP → один ip_hmac:
    # в ip_concentration один IP с 2 голосами, а не два IP по одному.
    p = make_poll(window_s=5)
    cf = {"CF-Connecting-IP": "203.0.113.77"}
    ok(vote(p["id"], [0], None, {"model": "cf"}, ip="198.51.100.11", headers=cf))
    ok(vote(p["id"], [1], None, {"model": "cf"}, ip="198.51.100.12", headers=cf))
    res = wait_final(p)
    assert (res["received"], res["total"]) == (2, 2)

    a = admin.get(f"/admin/polls/{p['id']}/analytics", headers=AUTH).json()
    buckets = {b["bucket"]: (b["ips"], b["votes"]) for b in a["ip_concentration"]}
    assert buckets["1"] == (0, 0), buckets
    assert buckets["2-10"] == (1, 2), buckets

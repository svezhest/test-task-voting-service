import uuid

import pytest

from conftest import AUTH


def ok(r):
    assert r.status_code == 204, r.text


# contracts.md «Стенд»: на зрительском входе /admin/ и /manage/ — 404 (в том числе с верным токеном).
@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/polls"),
    ("POST", "/admin/polls"),
    ("GET", f"/admin/polls/{uuid.uuid4()}"),
    ("GET", "/admin/public-url"),
    ("GET", "/admin/timezone"),
    ("POST", f"/admin/polls/{uuid.uuid4()}/cancel"),
    ("POST", "/admin/tunnel"),
    ("DELETE", "/admin/tunnel"),
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


@pytest.mark.parametrize("header", ["X-Forwarded-For", "CF-Connecting-IP"])
def test_forged_ip_ignored_on_viewer_entry(viewer, make_poll, wait_final, admin, header):
    # contracts.md «Стенд»: на 8090 IP — адрес соединения. Два voter_id с одного компьютера и разными
    # поддельными IP в заголовке → один ip_hmac на два голоса.
    p = make_poll()
    for i, ip in enumerate(["203.0.113.77", "203.0.113.78"]):
        body = {"poll_id": p["id"], "options": [i], "voter_id": str(uuid.uuid4()), "fp": {"model": "forged"}}
        ok(viewer.post("/api/vote", json=body, headers={header: ip}))
    res = wait_final(p)
    assert (res["received"], res["total"]) == (2, 2)

    a = admin.get(f"/admin/polls/{p['id']}/analytics", headers=AUTH).json()
    buckets = {b["bucket"]: (b["ips"], b["votes"]) for b in a["ip_concentration"]}
    assert buckets["2-10"] == (1, 2), buckets

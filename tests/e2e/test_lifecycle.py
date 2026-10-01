import uuid

import pytest

from conftest import AUTH


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/polls"),
    ("POST", "/admin/polls"),
    ("GET", f"/admin/polls/{uuid.uuid4()}"),
    ("GET", f"/admin/polls/{uuid.uuid4()}/analytics"),
    ("GET", f"/admin/polls/{uuid.uuid4()}/results"),
    ("POST", f"/admin/polls/{uuid.uuid4()}/activate"),
    ("PATCH", f"/admin/polls/{uuid.uuid4()}"),
    ("POST", f"/admin/polls/{uuid.uuid4()}/finish"),
    ("DELETE", f"/admin/polls/{uuid.uuid4()}"),
    ("GET", "/admin/public-url"),
    ("GET", "/admin/timezone"),
    ("POST", f"/admin/polls/{uuid.uuid4()}/cancel"),
    ("POST", "/admin/tunnel"),
    ("DELETE", "/admin/tunnel"),
])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}], ids=["no-token", "wrong-token"])
def test_admin_requires_token(admin, method, path, headers):
    r = admin.request(method, path, headers=headers, json={})
    assert r.status_code == 401


def test_lifecycle_draft_active_final(admin, viewer, make_poll, activate, vote, wait_final):
    poll = make_poll(active=False)
    pid = poll["id"]

    assert pid in admin.get("/admin/polls", headers=AUTH).text

    # draft: голос не принимается (опрос не активен), PATCH разрешён.
    assert vote(pid, [0]).status_code == 404
    r = admin.patch(f"/admin/polls/{pid}", json={"question": "Изменённый вопрос?"}, headers=AUTH)
    assert r.status_code < 300, r.text

    # active: конфиг опубликован с изменённым вопросом, PATCH запрещён, голоса принимаются.
    activate(pid)

    cfg = viewer.get(f"/p/{pid}/config.json").json()
    assert cfg["id"] == pid
    assert cfg["question"] == "Изменённый вопрос?"
    assert cfg["type"] == "single"
    assert cfg["options"] == [{"idx": 0, "label": "Да"}, {"idx": 1, "label": "Нет"}]
    assert cfg["grace_s"] == 0
    assert {"window_start", "window_end"} <= cfg.keys()

    assert viewer.get(f"/p/{pid}").status_code == 200

    r = admin.patch(f"/admin/polls/{pid}", json={"question": "Ещё раз?"}, headers=AUTH)
    assert r.status_code == 409
    assert admin.post(f"/admin/polls/{pid}/activate", headers=AUTH).status_code == 409

    assert vote(pid, [0]).status_code == 204
    assert vote(pid, [1], fp={"model": "other"}, ip="203.0.113.2").status_code == 204

    # До final итоги — только {status, received}.
    r = admin.get(f"/admin/polls/{pid}/results", headers=AUTH)
    assert r.status_code == 200
    assert r.json().keys() == {"status", "received"}
    assert r.json()["status"] == "active"

    res = wait_final(poll)
    assert res["status"] == "final"
    assert res["received"] == 2
    assert res["counted"] == 2

    r = admin.patch(f"/admin/polls/{pid}", json={"question": "После итога?"}, headers=AUTH)
    assert r.status_code == 409
    assert admin.post(f"/admin/polls/{pid}/activate", headers=AUTH).status_code == 409

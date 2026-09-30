import uuid

import pytest

AUTH = {"Authorization": "Bearer dev-token"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/polls"),
    ("POST", "/admin/polls"),
    ("GET", f"/admin/polls/{uuid.uuid4()}/results"),
    ("POST", f"/admin/polls/{uuid.uuid4()}/activate"),
    ("PATCH", f"/admin/polls/{uuid.uuid4()}"),
])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token"}], ids=["no-token", "wrong-token"])
def test_admin_requires_token(client, method, path, headers):
    r = client.request(method, path, headers=headers, json={})
    assert r.status_code == 401


def test_lifecycle_draft_active_final(client, make_poll, activate, vote, wait_final):
    poll = make_poll(active=False)
    pid = poll["id"]

    assert pid in client.get("/admin/polls", headers=AUTH).text

    # draft: голос не принимается (опрос не активен), PATCH разрешён.
    assert vote(pid, [0]).status_code == 404
    r = client.patch(f"/admin/polls/{pid}", json={"question": "Изменённый вопрос?"}, headers=AUTH)
    assert r.status_code < 300, r.text

    # active: конфиг опубликован с изменённым вопросом, PATCH запрещён, голоса принимаются.
    activate(pid)

    cfg = client.get(f"/p/{pid}/config.json").json()
    assert cfg["id"] == pid
    assert cfg["question"] == "Изменённый вопрос?"
    assert cfg["type"] == "single"
    assert cfg["options"] == [{"idx": 0, "label": "Да"}, {"idx": 1, "label": "Нет"}]
    assert cfg["grace_s"] == 0
    assert {"window_start", "window_end"} <= cfg.keys()

    assert client.get(f"/p/{pid}").status_code == 200

    r = client.patch(f"/admin/polls/{pid}", json={"question": "Ещё раз?"}, headers=AUTH)
    assert 400 <= r.status_code < 500

    assert vote(pid, [0]).status_code == 204
    # Второй отпечаток на другом IP: обходим вопрос про D(IP) = len(p) (см. tests/unit/test_dedup.py).
    assert vote(pid, [1], fp={"model": "other"}, ip="203.0.113.2").status_code == 204

    # В active итоги — только received, без распределения по вариантам.
    r = client.get(f"/admin/polls/{pid}/results", headers=AUTH)
    assert r.status_code == 200
    assert "received" in r.json()
    assert not r.json().get("options")

    res = wait_final(poll)
    assert res["status"] == "final"
    assert res["received"] == 2
    assert res["counted"] == 2

    r = client.patch(f"/admin/polls/{pid}", json={"question": "После итога?"}, headers=AUTH)
    assert 400 <= r.status_code < 500

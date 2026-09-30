import datetime as dt
import time

import pytest

AUTH = {"Authorization": "Bearer dev-token"}


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def body(**kw):
    start = int(time.time()) + 60
    b = {"question": "Вопрос?", "type": "single", "options": ["Кофе", "Чай"],
         "window_start": iso(start), "window_end": iso(start + 60), "grace_s": 60}
    b.update(kw)
    return b


def same_time(a, b):
    return dt.datetime.fromisoformat(a) == dt.datetime.fromisoformat(b)


def test_create_201_and_get(client):
    b = body()
    r = client.post("/admin/polls", json=b, headers=AUTH)
    assert r.status_code == 201, r.text
    poll = r.json()
    assert {"id", "status", "created_at", "question", "type", "options",
            "window_start", "window_end", "grace_s"} <= poll.keys()
    assert poll["status"] == "draft"
    assert poll["options"] == [{"idx": 0, "label": "Кофе"}, {"idx": 1, "label": "Чай"}]
    assert (poll["question"], poll["type"], poll["grace_s"]) == ("Вопрос?", "single", 60)
    assert same_time(poll["window_start"], b["window_start"])
    assert same_time(poll["window_end"], b["window_end"])

    r = client.get(f"/admin/polls/{poll['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == poll


def test_get_poll_after_activate(client, make_poll):
    p = make_poll()
    r = client.get(f"/admin/polls/{p['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_list_newest_first(client):
    a = client.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    time.sleep(1)  # чтобы created_at точно различались
    b = client.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    ids = [p["id"] for p in client.get("/admin/polls", headers=AUTH).json()]
    assert ids.index(b) < ids.index(a)


def test_64_options_ok(client):
    r = client.post("/admin/polls", json=body(options=[f"o{i}" for i in range(64)]), headers=AUTH)
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("kw", [
    {"options": ["Один"]},
    {"options": [f"o{i}" for i in range(65)]},
    {"window_start": iso(2_000_000_000), "window_end": iso(2_000_000_000)},
    {"window_start": iso(2_000_000_060), "window_end": iso(2_000_000_000)},
    {"grace_s": -1},
], ids=["1-option", "65-options", "end-eq-start", "end-before-start", "negative-grace"])
def test_create_422(client, kw):
    assert client.post("/admin/polls", json=body(**kw), headers=AUTH).status_code == 422


def test_activate_past_window_409(client):
    now = int(time.time())
    b = body(window_start=iso(now - 120), window_end=iso(now - 60), grace_s=0)
    r = client.post("/admin/polls", json=b, headers=AUTH)
    assert r.status_code == 201, r.text
    assert client.post(f"/admin/polls/{r.json()['id']}/activate", headers=AUTH).status_code == 409


def test_patch_draft(client):
    pid = client.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    r = client.patch(f"/admin/polls/{pid}", json={"options": ["А", "Б", "В"]}, headers=AUTH)
    assert r.status_code < 300, r.text  # код успешного PATCH в api.md не задан
    got = client.get(f"/admin/polls/{pid}", headers=AUTH).json()
    assert got["options"] == [{"idx": 0, "label": "А"}, {"idx": 1, "label": "Б"}, {"idx": 2, "label": "В"}]
    assert got["question"] == "Вопрос?"


@pytest.mark.skip(reason="вопрос: код ответа на несуществующий опрос в GET/PATCH/activate/results/analytics "
                         "(/admin/polls/{неизвестный id}) — 404? В api.md не сказано")
def test_unknown_poll_404():
    pass


@pytest.mark.skip(reason="вопрос: PATCH с невалидными полями (1 вариант, window_end <= window_start, grace_s < 0) — "
                         "тоже 422? Правило 2–64/окно/допуск в api.md написано для тела POST")
def test_patch_422():
    pass

import datetime as dt
import time
import uuid

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


INVALID = pytest.mark.parametrize("kw", [
    {"options": ["Один"]},
    {"options": [f"o{i}" for i in range(65)]},
    {"window_start": iso(2_000_000_000), "window_end": iso(2_000_000_000)},
    {"window_start": iso(2_000_000_060), "window_end": iso(2_000_000_000)},
    {"grace_s": -1},
    {"question": ""},
    {"options": ["Кофе", ""]},
    {"options": ["Кофе", "   "]},
], ids=["1-option", "65-options", "end-eq-start", "end-before-start", "negative-grace",
        "empty-question", "empty-option", "blank-option"])


def assert_422_detail_list(r):
    # api.md: 422 у POST и PATCH — в одном формате, список detail, как у FastAPI.
    assert r.status_code == 422, r.text
    assert isinstance(r.json().get("detail"), list), r.text
    assert r.json()["detail"], r.text


def same_time(a, b):
    return dt.datetime.fromisoformat(a) == dt.datetime.fromisoformat(b)


def test_create_201_and_get(admin):
    b = body()
    r = admin.post("/admin/polls", json=b, headers=AUTH)
    assert r.status_code == 201, r.text
    poll = r.json()
    assert {"id", "status", "created_at", "question", "type", "options",
            "window_start", "window_end", "grace_s"} <= poll.keys()
    assert poll["status"] == "draft"
    assert poll["options"] == [{"idx": 0, "label": "Кофе"}, {"idx": 1, "label": "Чай"}]
    assert (poll["question"], poll["type"], poll["grace_s"]) == ("Вопрос?", "single", 60)
    assert same_time(poll["window_start"], b["window_start"])
    assert same_time(poll["window_end"], b["window_end"])

    r = admin.get(f"/admin/polls/{poll['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == poll


def test_get_poll_after_activate(admin, make_poll):
    p = make_poll()
    r = admin.get(f"/admin/polls/{p['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "active"


def test_list_newest_first(admin):
    a = admin.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    time.sleep(1)  # чтобы created_at точно различались
    b = admin.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    ids = [p["id"] for p in admin.get("/admin/polls", headers=AUTH).json()]
    assert ids.index(b) < ids.index(a)


def test_64_options_ok(admin):
    r = admin.post("/admin/polls", json=body(options=[f"o{i}" for i in range(64)]), headers=AUTH)
    assert r.status_code == 201, r.text


@INVALID
def test_create_422(admin, kw):
    assert_422_detail_list(admin.post("/admin/polls", json=body(**kw), headers=AUTH))


@pytest.mark.skip(reason="Вопрос: вопрос из одних пробелов — 422? В api.md «пробелы не считаются» сказано про варианты.")
def test_create_blank_question_422(admin):
    assert_422_detail_list(admin.post("/admin/polls", json=body(question="   "), headers=AUTH))


def test_activate_past_window_409(admin):
    # «Окно прошло» = now > window_end + grace_s.
    now = int(time.time())
    b = body(window_start=iso(now - 120), window_end=iso(now - 60), grace_s=30)
    r = admin.post("/admin/polls", json=b, headers=AUTH)
    assert r.status_code == 201, r.text
    assert admin.post(f"/admin/polls/{r.json()['id']}/activate", headers=AUTH).status_code == 409


def test_activate_in_grace_ok(admin):
    # window_end прошёл, но window_end + grace_s ещё нет — окно не прошло.
    now = int(time.time())
    b = body(window_start=iso(now - 120), window_end=iso(now - 10), grace_s=60)
    pid = admin.post("/admin/polls", json=b, headers=AUTH).json()["id"]
    r = admin.post(f"/admin/polls/{pid}/activate", headers=AUTH)
    assert r.status_code < 300, r.text


def test_patch_draft(admin):
    pid = admin.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    r = admin.patch(f"/admin/polls/{pid}", json={"options": ["А", "Б", "В"]}, headers=AUTH)
    assert r.status_code < 300, r.text  # код успешного PATCH в api.md не задан
    got = admin.get(f"/admin/polls/{pid}", headers=AUTH).json()
    assert got["options"] == [{"idx": 0, "label": "А"}, {"idx": 1, "label": "Б"}, {"idx": 2, "label": "В"}]
    assert got["question"] == "Вопрос?"


@pytest.mark.parametrize("method,path", [
    ("GET", "/admin/polls/{id}"),
    ("PATCH", "/admin/polls/{id}"),
    ("POST", "/admin/polls/{id}/activate"),
    ("POST", "/admin/polls/{id}/finish"),
    ("DELETE", "/admin/polls/{id}"),
    ("GET", "/admin/polls/{id}/results"),
    ("GET", "/admin/polls/{id}/analytics"),
])
@pytest.mark.parametrize("poll_id", [None, "abc"], ids=["unknown-uuid", "not-uuid"])
def test_unknown_poll_404(admin, method, path, poll_id):
    # api.md: неизвестный или некорректный id (не UUID) — 404 на любом запросе админки.
    pid = poll_id or str(uuid.uuid4())
    r = admin.request(method, path.format(id=pid), headers=AUTH, json={"question": "Вопрос?"})
    assert r.status_code == 404, r.text


@INVALID
def test_patch_422(admin, kw):
    pid = admin.post("/admin/polls", json=body(), headers=AUTH).json()["id"]
    assert_422_detail_list(admin.patch(f"/admin/polls/{pid}", json=kw, headers=AUTH))


def test_patch_end_before_stored_start_422(admin):
    # PATCH только window_end: итоговое окно end <= start — те же правила, что у POST.
    b = body()
    pid = admin.post("/admin/polls", json=b, headers=AUTH).json()["id"]
    start = dt.datetime.fromisoformat(b["window_start"]).timestamp()
    assert_422_detail_list(admin.patch(f"/admin/polls/{pid}", json={"window_end": iso(start - 10)}, headers=AUTH))
    got = admin.get(f"/admin/polls/{pid}", headers=AUTH).json()
    assert same_time(got["window_end"], b["window_end"])  # опрос не изменился


def test_activate_twice_409(admin, make_poll):
    p = make_poll()  # уже активирован
    assert admin.post(f"/admin/polls/{p['id']}/activate", headers=AUTH).status_code == 409

import datetime as dt
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

# Чтобы `import app` работал при запуске pytest из корня репозитория.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VIEWER = "http://localhost:8090"  # зрительский вход: страница, config.json, /api/
ADMIN = "http://localhost:8091"   # админский вход: всё то же плюс /admin/ и /manage/
AUTH = {"Authorization": "Bearer dev-token"}

def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _connect(base, probe, ok_codes):
    c = httpx.Client(base_url=base, timeout=10)
    try:
        r = c.get(probe, headers=AUTH)
    except httpx.TransportError as e:
        c.close()
        pytest.skip(f"стенд недоступен на {base} ({e!r}); поднимите его: make up")
    if r.status_code not in ok_codes:
        c.close()
        pytest.skip(f"на {base} не стенд или он не готов: GET {probe} → {r.status_code}; make up")
    return c


CREATED = []  # id опросов, созданных тестами за сессию: в конце их удаляет _cleanup


def _track(r):
    if r.request.method == "POST" and r.request.url.path == "/admin/polls" and r.status_code == 201:
        r.read()
        CREATED.append(r.json()["id"])


def _cleanup(c, pid, deadline):
    # DELETE можно только в draft/final; active — finish (как только идёт окно), counting — ждать final.
    while time.time() < deadline:
        r = c.get(f"/admin/polls/{pid}", headers=AUTH)
        if r.status_code == 404:
            return
        p = r.json()
        if p["status"] in ("draft", "final"):
            if c.delete(f"/admin/polls/{pid}", headers=AUTH).status_code in (204, 404):
                return
        elif p["status"] == "active":
            now = time.time()
            start = dt.datetime.fromisoformat(p["window_start"]).timestamp()
            if start > deadline:
                raise RuntimeError(f"окно начнётся через {start - now:.0f} с")
            if start < now < dt.datetime.fromisoformat(p["window_end"]).timestamp():
                c.post(f"/admin/polls/{pid}/finish", headers=AUTH)
        time.sleep(0.5)
    raise TimeoutError(f"не дошёл до удаления, статус {p['status']}")


@pytest.fixture(scope="session")
def admin(request):
    c = _connect(ADMIN, "/admin/polls", {200})
    c.event_hooks["response"] = [_track]
    yield c
    t0 = time.time()
    with ThreadPoolExecutor(32) as ex:
        futs = {pid: ex.submit(_cleanup, c, pid, t0 + 120) for pid in set(CREATED)}
    errors = [f"  {pid}: {f.exception()!r}" for pid, f in futs.items() if f.exception()]
    with request.config.pluginmanager.get_plugin("capturemanager").global_and_fixture_disabled():
        print(f"\nочистка: {len(futs)} опросов за {time.time() - t0:.1f} с, не удалено {len(errors)}",
              *errors, sep="\n")
    c.close()


@pytest.fixture(scope="session")
def viewer(admin):
    # /p/{любой id} отдаёт index.html; админка здесь — 404.
    c = _connect(VIEWER, f"/p/{uuid.uuid4()}", {200})
    yield c
    c.close()


@pytest.fixture(scope="session")
def vote(viewer):
    def send(poll_id, options, voter_id=None, fp=None, ip="203.0.113.1", headers=None):
        body = {
            "poll_id": str(poll_id),
            "options": options,
            "voter_id": str(uuid.uuid4()) if voter_id is None else str(voter_id) if isinstance(voter_id, uuid.UUID) else voter_id,
            "fp": fp if fp is not None else {"model": "test"},
        }
        h = {"X-Forwarded-For": ip} if ip is not None else {}
        h.update(headers or {})
        return viewer.post("/api/vote", json=body, headers=h)

    return send


@pytest.fixture(scope="session")
def activate(admin, viewer):
    def act(poll_id):
        r = admin.post(f"/admin/polls/{poll_id}/activate", headers=AUTH)
        assert r.status_code < 300, r.text
        wait_loaded(viewer, poll_id)

    return act


@pytest.fixture(scope="session")
def make_poll(admin, activate):
    def make(labels=("Да", "Нет"), type="single", window_s=10, start_in=0, active=True):
        start = int(time.time()) + start_in
        body = {
            "question": "Тестовый вопрос?",
            "type": type,
            "options": list(labels),
            "window_start": iso(start),
            "window_end": iso(start + window_s),
            "grace_s": 0,
        }
        r = admin.post("/admin/polls", json=body, headers=AUTH)
        assert r.status_code == 201, r.text
        poll_id = r.json()["id"]
        poll = {"id": poll_id, "labels": list(labels), "window_start": start, "window_end": start + window_s}
        if active:
            activate(poll_id)
        return poll

    return make


def wait_loaded(viewer, poll_id):
    # Приём перечитывает активные опросы раз в секунду, экземпляров несколько.
    # Пробный голос с несуществующим вариантом в Kafka не пишется: 422 (или 410 вне окна).
    deadline = time.time() + 10
    while time.time() < deadline:
        body = {"poll_id": str(poll_id), "options": [63], "voter_id": str(uuid.uuid4()), "fp": {}}
        if viewer.post("/api/vote", json=body).status_code != 404:
            time.sleep(1.5)  # чтобы подхватили все экземпляры приёма
            return
        time.sleep(0.2)
    pytest.fail(f"приём не увидел активный опрос {poll_id} за 10 с")


@pytest.fixture(scope="session")
def wait_final(admin):
    def wait(poll):
        deadline = poll["window_end"] + 60
        while time.time() < deadline:
            r = admin.get(f"/admin/polls/{poll['id']}/results", headers=AUTH)
            assert r.status_code == 200, r.text
            if r.json().get("status") == "final":
                return r.json()
            time.sleep(0.5)
        pytest.fail(f"опрос {poll['id']} не дошёл до final за 60 с после конца окна")

    return wait


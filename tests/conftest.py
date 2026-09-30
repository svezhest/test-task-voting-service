import datetime as dt
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

# Чтобы `import app` работал при запуске pytest из корня репозитория.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://localhost:8090"
AUTH = {"Authorization": "Bearer dev-token"}

# Формат тела POST /admin/polls в docs не описан. Берём поля config.json из contracts.md
# и ISO 8601 для времени. Если стенд их не принимает — тест пропускается с этим вопросом.
CREATE_QUESTION = (
    "вопрос: формат тела POST /admin/polls и ответа на него не описан в docs. "
    "Тест шлёт поля config.json {question, type, options: [{idx, label}], window_start, "
    "window_end (ISO 8601), grace_s} и ждёт в ответе поле id"
)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


@pytest.fixture(scope="session")
def client():
    c = httpx.Client(base_url=BASE, timeout=10)
    try:
        r = c.get("/admin/polls", headers=AUTH)
    except httpx.TransportError as e:
        pytest.skip(f"стенд недоступен на {BASE} ({e!r}); поднимите его: make up")
    if r.status_code != 200:
        pytest.skip(f"на {BASE} не стенд или он не готов: GET /admin/polls → {r.status_code}; make up")
    yield c
    c.close()


@pytest.fixture(scope="session")
def vote(client):
    def send(poll_id, options, voter_id=None, fp=None, ip="203.0.113.1"):
        body = {
            "poll_id": str(poll_id),
            "options": options,
            "voter_id": str(uuid.uuid4()) if voter_id is None else str(voter_id) if isinstance(voter_id, uuid.UUID) else voter_id,
            "fp": fp if fp is not None else {"model": "test"},
        }
        return client.post("/api/vote", json=body, headers={"X-Forwarded-For": ip})

    return send


@pytest.fixture(scope="session")
def activate(client):
    def act(poll_id):
        r = client.post(f"/admin/polls/{poll_id}/activate", headers=AUTH)
        assert r.status_code < 300, r.text
        wait_loaded(client, poll_id)

    return act


@pytest.fixture(scope="session")
def make_poll(client, activate):
    def make(labels=("Да", "Нет"), type="single", window_s=10, start_in=0, active=True):
        start = int(time.time()) + start_in
        body = {
            "question": "Тестовый вопрос?",
            "type": type,
            "options": [{"idx": i, "label": label} for i, label in enumerate(labels)],
            "window_start": iso(start),
            "window_end": iso(start + window_s),
            "grace_s": 0,
        }
        r = client.post("/admin/polls", json=body, headers=AUTH)
        try:
            poll_id = r.json()["id"]
        except Exception:
            poll_id = None
        if r.status_code >= 300 or poll_id is None:
            pytest.skip(f"{CREATE_QUESTION}; стенд ответил {r.status_code} {r.text[:200]}")
        poll = {"id": poll_id, "labels": list(labels), "window_start": start, "window_end": start + window_s}
        if active:
            activate(poll_id)
        return poll

    return make


def wait_loaded(client, poll_id):
    # Приём перечитывает активные опросы раз в секунду, экземпляров несколько.
    # Пробный голос с несуществующим вариантом в Kafka не пишется: 422 (или 410 вне окна).
    deadline = time.time() + 10
    while time.time() < deadline:
        body = {"poll_id": str(poll_id), "options": [63], "voter_id": str(uuid.uuid4()), "fp": {}}
        if client.post("/api/vote", json=body).status_code != 404:
            time.sleep(1.5)  # чтобы подхватили все экземпляры приёма
            return
        time.sleep(0.2)
    pytest.fail(f"приём не увидел активный опрос {poll_id} за 10 с")


@pytest.fixture(scope="session")
def wait_final(client):
    def wait(poll):
        deadline = poll["window_end"] + 60
        while time.time() < deadline:
            r = client.get(f"/admin/polls/{poll['id']}/results", headers=AUTH)
            assert r.status_code == 200, r.text
            if r.json().get("status") == "final":
                return r.json()
            time.sleep(0.5)
        pytest.fail(f"опрос {poll['id']} не дошёл до final за 60 с после конца окна")

    return wait


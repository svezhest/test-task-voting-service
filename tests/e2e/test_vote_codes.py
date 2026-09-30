import time
import uuid

import pytest


@pytest.fixture
def poll(make_poll):
    return make_poll(labels=("A", "B", "C"), window_s=10)


def test_204_valid_vote(poll, vote):
    assert vote(poll["id"], [1]).status_code == 204


def test_204_repeat_is_same(poll, vote):
    voter = uuid.uuid4()
    assert vote(poll["id"], [0], voter).status_code == 204
    assert vote(poll["id"], [1], voter).status_code == 204


def test_400_no_voter_id(client, poll):
    body = {"poll_id": poll["id"], "options": [0], "fp": {}}
    assert client.post("/api/vote", json=body).status_code == 400


@pytest.mark.parametrize("voter_id", ["not-a-uuid", "", 123])
def test_400_bad_voter_id(poll, vote, voter_id):
    assert vote(poll["id"], [0], voter_id=voter_id).status_code == 400


@pytest.mark.skip(reason="вопрос: «неверный формат» в 400 — только формат voter_id или любой неразбираемый запрос "
                         "(битый JSON, нет poll_id/options/fp)? FastAPI по умолчанию отвечает на это 422")
def test_400_not_json(client):
    r = client.post("/api/vote", content=b"{oops", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_404_unknown_poll(client, vote):
    assert vote(uuid.uuid4(), [0]).status_code == 404


def test_404_draft_poll(make_poll, vote):
    draft = make_poll(active=False)
    assert vote(draft["id"], [0]).status_code == 404


def test_410_before_window(make_poll, vote):
    future = make_poll(start_in=60, window_s=10)
    assert vote(future["id"], [0]).status_code == 410


def test_410_after_window(make_poll, vote):
    # Между window_end и close_at (= window_end + 0 + DELIVERY_TIMEOUT_S 3 с) опрос ещё active: 410.
    p = make_poll(window_s=5)
    time.sleep(max(0.0, p["window_end"] + 1 - time.time()))
    assert vote(p["id"], [0]).status_code == 410


@pytest.mark.parametrize("options", [[3], [-1], [63], [0, 1], [0, 3]])
def test_422_single_bad_options(poll, vote, options):
    assert vote(poll["id"], options).status_code == 422


def test_422_multi_out_of_range(make_poll, vote):
    p = make_poll(labels=("A", "B"), type="multi")
    assert vote(p["id"], [0, 1]).status_code == 204
    assert vote(p["id"], [0, 2]).status_code == 422


@pytest.mark.skip(reason="вопрос: пустой список options ([]) — это 422 «неверные варианты» или 400 «неверный формат»?")
def test_empty_options(poll, vote):
    assert vote(poll["id"], []).status_code == 422


@pytest.mark.skip(reason="вопрос: повтор варианта в options ([0, 0]) у single — 422 или один вариант?")
def test_duplicate_option_single(poll, vote):
    assert vote(poll["id"], [0, 0]).status_code == 422

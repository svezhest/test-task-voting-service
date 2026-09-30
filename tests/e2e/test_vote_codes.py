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


def test_400_no_voter_id(viewer, poll):
    body = {"poll_id": poll["id"], "options": [0], "fp": {}}
    assert viewer.post("/api/vote", json=body).status_code == 400


@pytest.mark.parametrize("voter_id", ["not-a-uuid", "", 123])
def test_400_bad_voter_id(poll, vote, voter_id):
    assert vote(poll["id"], [0], voter_id=voter_id).status_code == 400


def test_400_not_json(viewer):
    r = viewer.post("/api/vote", content=b"{oops", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


@pytest.mark.parametrize("field", ["poll_id", "options", "voter_id", "fp"])
def test_400_missing_field(viewer, poll, field):
    body = {"poll_id": poll["id"], "options": [0], "voter_id": str(uuid.uuid4()), "fp": {}}
    del body[field]
    assert viewer.post("/api/vote", json=body).status_code == 400


def test_404_unknown_poll(viewer, vote):
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


@pytest.mark.parametrize("type", ["single", "multi"])
@pytest.mark.parametrize("options", [[], [0, 0]])
def test_422_empty_or_duplicate(make_poll, vote, type, options):
    p = make_poll(labels=("A", "B"), type=type)
    assert vote(p["id"], options).status_code == 422


def test_410_after_final(make_poll, vote, wait_final):
    p = make_poll(window_s=3)
    wait_final(p)
    assert vote(p["id"], [0]).status_code == 410


@pytest.mark.parametrize("field,value", [
    ("fp", "str"), ("fp", 1), ("fp", [1]),
    ("options", 0), ("options", "0"),
    ("poll_id", "not-a-uuid"), ("poll_id", 123),
])
def test_400_bad_types(viewer, poll, field, value):
    body = {"poll_id": poll["id"], "options": [0], "voter_id": str(uuid.uuid4()), "fp": {}}
    body[field] = value
    assert viewer.post("/api/vote", json=body).status_code == 400


@pytest.mark.parametrize("options", [["0"], [True], [0.0], [0, "1"], [None]],
                         ids=["str", "bool", "float", "mixed", "null"])
def test_400_options_not_int_list(viewer, poll, options):
    # api.md: options — только список целых; "0", true, 0.0 — неверный тип → 400.
    body = {"poll_id": poll["id"], "options": options, "voter_id": str(uuid.uuid4()), "fp": {}}
    assert viewer.post("/api/vote", json=body).status_code == 400

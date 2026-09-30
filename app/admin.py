import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from pydantic import BaseModel, Field


def check_token(authorization: str = Header("")):
    if authorization != f"Bearer {os.environ['ADMIN_TOKEN']}":
        raise HTTPException(401)


app = FastAPI(dependencies=[Depends(check_token)])


class Option(BaseModel):
    label: str


# STUB: question — the request body format is not in docs. Field names follow config.json;
# options are accepted as ["Да", "Нет"] or [{"label": "Да"}, ...], idx = position in the list.
# STUB: question — no checks beyond types (e.g. window_end > window_start) because docs define none.
class PollIn(BaseModel):
    question: str
    type: Literal["single", "multi"]
    options: list[str | Option] = Field(min_length=1, max_length=64)
    window_start: datetime
    window_end: datetime
    grace_s: int = Field(ge=0)


class PollPatch(BaseModel):
    question: str | None = None
    type: Literal["single", "multi"] | None = None
    options: list[str | Option] | None = Field(None, min_length=1, max_length=64)
    window_start: datetime | None = None
    window_end: datetime | None = None
    grace_s: int | None = Field(None, ge=0)


POLL = """
select id, question, type, status, window_start, window_end, grace_s, created_at,
       (select json_agg(json_build_object('idx', idx, 'label', label) order by idx)
          from options where poll_id = polls.id) as options
  from polls"""


def db():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def get_poll(conn, poll_id):
    poll = conn.execute(POLL + " where id = %s", (poll_id,)).fetchone()
    if poll is None:
        raise HTTPException(404)
    return poll


def set_options(conn, poll_id, options):
    conn.execute("delete from options where poll_id = %s", (poll_id,))
    conn.cursor().executemany(
        "insert into options (poll_id, idx, label) values (%s, %s, %s)",
        [(poll_id, idx, o if isinstance(o, str) else o.label) for idx, o in enumerate(options)],
    )


# STUB: question — response codes/bodies for create (201 + poll), wrong status (409) and bad token (401) are not in docs.
@app.post("/admin/polls", status_code=201)
def create_poll(poll: PollIn):
    poll_id = uuid.uuid4()
    with db() as conn:
        conn.execute(
            "insert into polls (id, question, type, window_start, window_end, grace_s) values (%s, %s, %s, %s, %s, %s)",
            (poll_id, poll.question, poll.type, poll.window_start, poll.window_end, poll.grace_s),
        )
        set_options(conn, poll_id, poll.options)
        return get_poll(conn, poll_id)


@app.patch("/admin/polls/{poll_id}")
def update_poll(poll_id: uuid.UUID, patch: PollPatch):
    fields = patch.model_dump(exclude_unset=True, exclude={"options"})
    with db() as conn:
        if get_poll(conn, poll_id)["status"] != "draft":
            raise HTTPException(409)
        if fields:
            sets = ", ".join(f"{name} = %s" for name in fields)
            conn.execute(f"update polls set {sets} where id = %s", (*fields.values(), poll_id))
        if patch.options is not None:
            set_options(conn, poll_id, patch.options)
        return get_poll(conn, poll_id)


# STUB: config_version is never bumped — per contracts, ingest re-reads all active polls every second.
@app.post("/admin/polls/{poll_id}/activate")
def activate_poll(poll_id: uuid.UUID):
    with db() as conn:
        poll = get_poll(conn, poll_id)
        if poll["status"] != "draft":
            raise HTTPException(409)
        conn.execute("update polls set status = 'active', salt = %s where id = %s", (os.urandom(32), poll_id))
        config = {k: poll[k] for k in ("id", "question", "type", "options", "window_start", "window_end", "grace_s")}
        path = Path(os.environ["WEB_ROOT"], "p", str(poll_id), "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(jsonable_encoder(config), ensure_ascii=False))
        return poll | {"status": "active"}


# STUB: question — list format is not in docs; a plain JSON array of polls.
@app.get("/admin/polls")
def list_polls():
    with db() as conn:
        return conn.execute(POLL + " order by created_at").fetchall()


@app.get("/admin/polls/{poll_id}/results")
def poll_results(poll_id: uuid.UUID):
    with db() as conn:
        status = get_poll(conn, poll_id)["status"]
        received = conn.execute(
            "select coalesce(sum(votes), 0)::bigint as n from stage_progress where poll_id = %s and stage = 1", (poll_id,)
        ).fetchone()["n"]
        # STUB: question — docs define the non-final answer only for active; draft and counting get the same shape.
        if status != "final":
            return {"status": status, "received": received}
        votes = conn.execute(
            """select coalesce(sum(counted), 0)::bigint as counted, coalesce(sum(total), 0)::bigint as total
                 from results where poll_id = %s and option_idx = -1""",
            (poll_id,),
        ).fetchone()
        options = conn.execute(
            """select o.idx, o.label, coalesce(sum(r.counted), 0)::bigint as counted, coalesce(sum(r.total), 0)::bigint as total
                 from options o left join results r on r.poll_id = o.poll_id and r.option_idx = o.idx
                where o.poll_id = %s group by o.idx, o.label order by o.idx""",
            (poll_id,),
        ).fetchall()
    counted, total = votes["counted"], votes["total"]
    # STUB: question — share and over_limit_share are undefined with zero votes; we return null.
    for o in options:
        o["share"] = o["counted"] / counted if counted else None
    return {
        "status": status,
        "received": received,
        "total": total,
        "counted": counted,
        "over_limit_share": 1 - counted / total if total else None,
        "options": options,
    }

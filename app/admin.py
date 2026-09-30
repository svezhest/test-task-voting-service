import json
import os
import shutil
import urllib.request
import uuid
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from psycopg.rows import dict_row
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator


def check_token(authorization: str = Header("")):
    if authorization != f"Bearer {os.environ['ADMIN_TOKEN']}":
        raise HTTPException(401)


app = FastAPI(dependencies=[Depends(check_token)])


@app.exception_handler(RequestValidationError)
async def invalid_id(request, exc):  # a poll id that is not a UUID is an unknown poll
    if any(e["loc"][:1] == ("path",) for e in exc.errors()):
        return await http_exception_handler(request, HTTPException(404))
    return await request_validation_exception_handler(request, exc)


NonBlank = Annotated[str, Field(pattern=r"\S")]  # at least one non-whitespace character


class PollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field (a typo, "status") is 422, not silently dropped
    question: NonBlank
    type: Literal["single", "multi"]
    options: list[NonBlank] = Field(min_length=2, max_length=64)
    # STUB: question — docs say only "ISO 8601"; a time without a timezone is rejected with 422.
    window_start: AwareDatetime
    window_end: AwareDatetime
    grace_s: int = Field(ge=0, le=86400)  # STUB: question — the upper bound (a day) is not in docs; above int32 it was a 500

    @model_validator(mode="after")
    def window(self):
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


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


def change(conn, poll_id, sql, params):
    """Atomic change: `sql` updates the poll only in the allowed status and returns its id; otherwise 404 or 409."""
    if conn.execute(sql, params).fetchone() is None:
        get_poll(conn, poll_id)  # 404 if unknown
        raise HTTPException(409)
    return get_poll(conn, poll_id)


def totals(conn, poll_id):  # received (stage 1); root counted and total (results rows with option_idx = -1)
    return conn.execute(
        """select (select coalesce(sum(votes), 0) from stage_progress where poll_id = %(id)s and stage = 1)::bigint as received,
                  coalesce(sum(counted), 0)::bigint as counted, coalesce(sum(total), 0)::bigint as total
             from results where poll_id = %(id)s and option_idx = -1""",
        {"id": poll_id},
    ).fetchone()


def set_options(conn, poll_id, options):
    conn.execute("delete from options where poll_id = %s", (poll_id,))
    conn.cursor().executemany(
        "insert into options (poll_id, idx, label) values (%s, %s, %s)",
        [(poll_id, idx, label) for idx, label in enumerate(options)],
    )


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
def update_poll(poll_id: uuid.UUID, patch: dict):
    with db() as conn:
        conn.execute("select from polls where id = %s for update", (poll_id,))  # parallel PATCHes do not lose changes
        poll = get_poll(conn, poll_id)
        if poll["status"] != "draft":
            raise HTTPException(409)
        poll["options"] = [o["label"] for o in poll["options"]]
        try:
            new = PollIn(**({k: poll[k] for k in PollIn.model_fields} | patch))
        except ValidationError as e:
            raise RequestValidationError(e.errors(include_url=False))
        change(conn, poll_id, """update polls set question = %s, type = %s, window_start = %s, window_end = %s, grace_s = %s
                                  where id = %s and status = 'draft' returning id""",
               (new.question, new.type, new.window_start, new.window_end, new.grace_s, poll_id))
        set_options(conn, poll_id, new.options)
        return get_poll(conn, poll_id)


@app.post("/admin/polls/{poll_id}/activate")
def activate_poll(poll_id: uuid.UUID):
    with db() as conn:
        poll = change(conn, poll_id, """update polls set status = 'active', salt = %s
                                         where id = %s and status = 'draft' and now() <= window_end + make_interval(secs => grace_s)
                                     returning id""", (os.urandom(32), poll_id))
        config = {k: poll[k] for k in ("id", "question", "type", "options", "window_start", "window_end", "grace_s")}
        path = Path(os.environ["WEB_ROOT"], "p", str(poll_id), "config.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name("config.json.tmp")  # atomic: nginx never serves a half-written file
        tmp.write_text(json.dumps(jsonable_encoder(config), ensure_ascii=False))
        os.replace(tmp, path)
        return poll


# config.json keeps the old window_end on purpose: the page only renders the question, the window is enforced by ingest.
@app.post("/admin/polls/{poll_id}/finish")
def finish_poll(poll_id: uuid.UUID):
    with db() as conn:
        return change(conn, poll_id, """update polls set window_end = now()
                                         where id = %s and status = 'active' and window_start < now() and now() < window_end
                                     returning id""", (poll_id,))


@app.delete("/admin/polls/{poll_id}", status_code=204)
def delete_poll(poll_id: uuid.UUID):
    with db() as conn:
        get_poll(conn, poll_id)
        for table in ("options", "results", "stage_progress", "fp_counts", "timeline", "stage2_stats"):
            conn.execute(f"delete from {table} where poll_id = %s", (poll_id,))
        if conn.execute("delete from polls where id = %s and status in ('draft', 'final')", (poll_id,)).rowcount == 0:
            raise HTTPException(409)  # rolls back the deletes above
    shutil.rmtree(Path(os.environ["WEB_ROOT"], "p", str(poll_id)), ignore_errors=True)


@app.get("/admin/polls")
def list_polls():
    with db() as conn:
        return conn.execute(POLL + " order by created_at desc").fetchall()


@app.get("/admin/public-url")
def public_url():
    try:
        with urllib.request.urlopen("http://cloudflared:2000/quicktunnel", timeout=1) as r:
            hostname = json.load(r).get("hostname")
    except Exception:  # tunnel is off or not up yet
        hostname = None
    lan = os.environ.get("HOST_LAN_IP")
    return {"tunnel": f"https://{hostname}" if hostname else None, "lan": f"http://{lan}:8090" if lan else None}


@app.get("/admin/polls/{poll_id}")
def read_poll(poll_id: uuid.UUID):
    with db() as conn:
        return get_poll(conn, poll_id)


@app.get("/admin/polls/{poll_id}/results")
def poll_results(poll_id: uuid.UUID):
    with db() as conn:
        status = get_poll(conn, poll_id)["status"]
        t = totals(conn, poll_id)
        if status != "final":
            return {"status": status, "received": t["received"]}
        options = conn.execute(
            """select o.idx, o.label, coalesce(sum(r.counted), 0)::bigint as counted, coalesce(sum(r.total), 0)::bigint as total
                 from options o left join results r on r.poll_id = o.poll_id and r.option_idx = o.idx
                where o.poll_id = %s group by o.idx, o.label order by o.idx""",
            (poll_id,),
        ).fetchall()
    for o in options:
        o["share"] = o["counted"] / t["counted"] if t["counted"] else None
    return {"status": status, **t, "over_limit_share": 1 - t["counted"] / t["total"] if t["total"] else None, "options": options}


BUCKETS = ["1", "2-10", "11-100", "101+"]


@app.get("/admin/polls/{poll_id}/analytics")
def poll_analytics(poll_id: uuid.UUID):
    with db() as conn:
        poll = get_poll(conn, poll_id)
        seconds = {r["second"]: r["votes"] for r in conn.execute(
            "select second, sum(votes)::bigint as votes from timeline where poll_id = %s group by second", (poll_id,)
        )}
        answer = {
            "status": poll["status"],
            "window_start": poll["window_start"],
            "timeline": [{"t": t, "received": seconds.get(t, 0)} for t in range(max(seconds, default=-1) + 1)],
            "model": {"median_s": 14, "sigma": 0.5},  # architecture.md: fixed model, not fitted to the poll
            "funnel": None,
            "ip_concentration": None,
        }
        if poll["status"] != "final":
            return answer
        t = totals(conn, poll_id)
        stats = conn.execute("select key_limit, ip_ceiling, ip_hist from stage2_stats where poll_id = %s", (poll_id,)).fetchall()
    answer["funnel"] = {
        "received": t["received"],
        "unique_voters": t["total"],
        "counted": t["counted"],
        "rejected": {
            "repeat_voter": t["received"] - t["total"],
            "key_limit": sum(s["key_limit"] for s in stats),
            "ip_ceiling": sum(s["ip_ceiling"] for s in stats),
        },
    }
    answer["ip_concentration"] = [
        {"bucket": b, "ips": sum(s["ip_hist"][b]["ips"] for s in stats), "votes": sum(s["ip_hist"][b]["votes"] for s in stats)}
        for b in BUCKETS
    ]
    return answer

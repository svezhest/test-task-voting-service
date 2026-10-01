import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from psycopg.rows import dict_row
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.config import ADMIN_TOKEN, DATABASE_URL, HOST_LAN_IP, HOST_TZ, WEB_ROOT
from app.tunnel import CloudflareTunnel


def check_token(authorization: str = Header("")):
    if authorization != f"Bearer {ADMIN_TOKEN}":
        raise HTTPException(401)


app = FastAPI(dependencies=[Depends(check_token)])
app.state.tunnel = CloudflareTunnel()


@app.exception_handler(RequestValidationError)
async def invalid_poll_id_is_404(request, exc):
    if any(error["loc"][:1] == ("path",) for error in exc.errors()):
        return await http_exception_handler(request, HTTPException(404))
    return await request_validation_exception_handler(request, exc)


NonBlank = Annotated[str, Field(pattern=r"\S")]


class PollIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: NonBlank
    type: Literal["single", "multi"]
    options: list[NonBlank] = Field(min_length=2, max_length=64)
    window_start: AwareDatetime
    window_end: AwareDatetime
    grace_s: int = Field(ge=0, le=86400)

    @model_validator(mode="after")
    def window_end_after_start(self):
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


SELECT_POLLS = """
select id, question, type, status, window_start, window_end, grace_s, created_at,
       (
           select json_agg(json_build_object('idx', idx, 'label', label) order by idx)
             from options
            where poll_id = polls.id
       ) as options
  from polls
"""
POLL_DATA_TABLES = ("options", "results", "stage_progress", "fp_counts", "timeline", "stage2_stats")
IP_BUCKETS = ["1", "2-10", "11-100", "101+"]
FIXED_ARRIVAL_MODEL = {"median_s": 14, "sigma": 0.5}


def connect():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def get_poll_or_404(conn, poll_id):
    poll = conn.execute(SELECT_POLLS + " where id = %s", (poll_id,)).fetchone()
    if poll is None:
        raise HTTPException(404)
    return poll


def update_poll_or_409(conn, poll_id, status_guarded_update, params):
    """`status_guarded_update` changes the poll only in the allowed status and returns its id."""
    updated = conn.execute(status_guarded_update, params).fetchone()
    if updated is None:
        get_poll_or_404(conn, poll_id)
        raise HTTPException(409)
    return get_poll_or_404(conn, poll_id)


def read_vote_totals(conn, poll_id):
    return conn.execute(
        """
        select (
                   select coalesce(sum(votes), 0)
                     from stage_progress
                    where poll_id = %(id)s
                      and stage = 1
               )::bigint as received,
               coalesce(sum(counted), 0)::bigint as counted,
               coalesce(sum(total), 0)::bigint as total
          from results
         where poll_id = %(id)s
           and option_idx = -1
        """,
        {"id": poll_id},
    ).fetchone()


def replace_options(conn, poll_id, labels):
    conn.execute(
        """
        delete from options
         where poll_id = %s
        """,
        (poll_id,),
    )
    conn.cursor().executemany(
        """
        insert into options (poll_id, idx, label)
        values (%s, %s, %s)
        """,
        [(poll_id, idx, label) for idx, label in enumerate(labels)],
    )


@app.post("/admin/polls", status_code=201)
def create_poll(poll: PollIn):
    poll_id = uuid.uuid4()
    with connect() as conn:
        conn.execute(
            """
            insert into polls (id, question, type, window_start, window_end, grace_s)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (poll_id, poll.question, poll.type, poll.window_start, poll.window_end, poll.grace_s),
        )
        replace_options(conn, poll_id, poll.options)
        return get_poll_or_404(conn, poll_id)


@app.patch("/admin/polls/{poll_id}")
def update_poll(poll_id: uuid.UUID, patch: dict):
    with connect() as conn:
        # parallel PATCHes do not lose changes
        conn.execute(
            """
            select
              from polls
             where id = %s
               for update
            """,
            (poll_id,),
        )
        poll = get_poll_or_404(conn, poll_id)
        if poll["status"] != "draft":
            raise HTTPException(409)
        current = {field: poll[field] for field in PollIn.model_fields}
        current["options"] = [option["label"] for option in poll["options"]]
        try:
            updated = PollIn(**(current | patch))
        except ValidationError as error:
            raise RequestValidationError(error.errors(include_url=False))
        update_poll_or_409(
            conn,
            poll_id,
            """
            update polls
               set question = %s,
                   type = %s,
                   window_start = %s,
                   window_end = %s,
                   grace_s = %s
             where id = %s
               and status = 'draft'
            returning id
            """,
            (updated.question, updated.type, updated.window_start, updated.window_end, updated.grace_s, poll_id),
        )
        replace_options(conn, poll_id, updated.options)
        return get_poll_or_404(conn, poll_id)


def publish_page_config(poll_id, poll):
    config = {key: poll[key] for key in ("id", "question", "type", "options", "window_start", "window_end", "grace_s")}
    path = Path(WEB_ROOT, "p", str(poll_id), "config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name("config.json.tmp")  # atomic: nginx never serves a half-written file
    temporary_path.write_text(json.dumps(jsonable_encoder(config), ensure_ascii=False))
    os.replace(temporary_path, path)


@app.post("/admin/polls/{poll_id}/activate")
def activate_poll(poll_id: uuid.UUID):
    with connect() as conn:
        poll = update_poll_or_409(
            conn,
            poll_id,
            """
            update polls
               set status = 'active',
                   salt = %s
             where id = %s
               and status = 'draft'
               and now() <= window_end + make_interval(secs => grace_s)
            returning id
            """,
            (os.urandom(32), poll_id),
        )
        publish_page_config(poll_id, poll)
        return poll


# config.json keeps the old window_end on purpose: the page only renders the question, the window is enforced by ingest.
@app.post("/admin/polls/{poll_id}/finish")
def finish_poll(poll_id: uuid.UUID):
    with connect() as conn:
        return update_poll_or_409(
            conn,
            poll_id,
            """
            update polls
               set window_end = now()
             where id = %s
               and status = 'active'
               and window_start < now()
               and now() < window_end
            returning id
            """,
            (poll_id,),
        )


@app.delete("/admin/polls/{poll_id}", status_code=204)
def delete_poll(poll_id: uuid.UUID):
    with connect() as conn:
        get_poll_or_404(conn, poll_id)
        for table in POLL_DATA_TABLES:
            conn.execute(
                f"""
                delete from {table}
                 where poll_id = %s
                """,
                (poll_id,),
            )
        deleted = conn.execute(
            """
            delete from polls
             where id = %s
               and status in ('draft', 'final')
            """,
            (poll_id,),
        )
        if deleted.rowcount == 0:
            raise HTTPException(409)  # rolls back the deletes above
    shutil.rmtree(Path(WEB_ROOT, "p", str(poll_id)), ignore_errors=True)


@app.get("/admin/polls")
def list_polls():
    with connect() as conn:
        return conn.execute(SELECT_POLLS + " order by created_at desc").fetchall()


@app.get("/admin/public-url")
def public_url(request: Request):
    lan_url = None
    if HOST_LAN_IP:
        lan_url = f"http://{HOST_LAN_IP}:8090"
    return {"tunnel": request.app.state.tunnel.url(), "lan": lan_url}


@app.get("/admin/timezone")
def timezone():
    return {"timezone": HOST_TZ or None}


@app.post("/admin/tunnel")
def open_tunnel(request: Request):
    if not request.app.state.tunnel.open():
        raise HTTPException(503)
    return public_url(request)


@app.delete("/admin/tunnel", status_code=204)
def close_tunnel(request: Request):
    request.app.state.tunnel.close()


@app.get("/admin/polls/{poll_id}")
def read_poll(poll_id: uuid.UUID):
    with connect() as conn:
        return get_poll_or_404(conn, poll_id)


def ratio_or_none(part, whole):
    if not whole:
        return None
    return part / whole


@app.get("/admin/polls/{poll_id}/results")
def poll_results(poll_id: uuid.UUID):
    with connect() as conn:
        status = get_poll_or_404(conn, poll_id)["status"]
        totals = read_vote_totals(conn, poll_id)
        if status != "final":
            return {"status": status, "received": totals["received"]}
        options = conn.execute(
            """
            select options.idx,
                   options.label,
                   coalesce(sum(results.counted), 0)::bigint as counted,
                   coalesce(sum(results.total), 0)::bigint as total
              from options
              left join results
                on results.poll_id = options.poll_id
               and results.option_idx = options.idx
             where options.poll_id = %s
             group by options.idx, options.label
             order by options.idx
            """,
            (poll_id,),
        ).fetchall()
    for option in options:
        option["share"] = ratio_or_none(option["counted"], totals["counted"])
    over_limit_share = None
    if totals["total"]:
        over_limit_share = 1 - totals["counted"] / totals["total"]
    return {"status": status, **totals, "over_limit_share": over_limit_share, "options": options}


def funnel(totals, partition_stats):
    return {
        "received": totals["received"],
        "unique_voters": totals["total"],
        "counted": totals["counted"],
        "rejected": {
            "repeat_voter": totals["received"] - totals["total"],
            "key_limit": sum(stats["key_limit"] for stats in partition_stats),
            "ip_ceiling": sum(stats["ip_ceiling"] for stats in partition_stats),
        },
    }


def ip_concentration(partition_stats):
    return [
        {
            "bucket": bucket,
            "ips": sum(stats["ip_hist"][bucket]["ips"] for stats in partition_stats),
            "votes": sum(stats["ip_hist"][bucket]["votes"] for stats in partition_stats),
        }
        for bucket in IP_BUCKETS
    ]


@app.get("/admin/polls/{poll_id}/analytics")
def poll_analytics(poll_id: uuid.UUID):
    with connect() as conn:
        poll = get_poll_or_404(conn, poll_id)
        rows = conn.execute(
            """
            select second,
                   sum(votes)::bigint as votes
              from timeline
             where poll_id = %s
             group by second
            """,
            (poll_id,),
        )
        votes_by_second = {row["second"]: row["votes"] for row in rows}
        last_second = max(votes_by_second, default=-1)
        answer = {
            "status": poll["status"],
            "window_start": poll["window_start"],
            "timeline": [
                {"t": second, "received": votes_by_second.get(second, 0)} for second in range(last_second + 1)
            ],
            "model": FIXED_ARRIVAL_MODEL,
            "funnel": None,
            "ip_concentration": None,
        }
        if poll["status"] != "final":
            return answer
        totals = read_vote_totals(conn, poll_id)
        partition_stats = conn.execute(
            """
            select key_limit, ip_ceiling, ip_hist
              from stage2_stats
             where poll_id = %s
            """,
            (poll_id,),
        ).fetchall()
    answer["funnel"] = funnel(totals, partition_stats)
    answer["ip_concentration"] = ip_concentration(partition_stats)
    return answer

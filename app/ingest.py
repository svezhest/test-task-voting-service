import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import psycopg
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, StrictInt

from app.record import Vote, encode

TRUST_XFF = os.environ["TRUST_XFF"] == "1"
DELIVERY_TIMEOUT_S = float(os.environ["DELIVERY_TIMEOUT_S"])

polls = {}  # non-draft poll_id -> (type, salt, window_start, window_end + grace_s, option idxs); times in unix seconds; salt only while active
producer: AIOKafkaProducer


# STUB: api.md lists polls.config_version, but nothing needs it: ingest re-reads all non-draft polls every second
# (contracts.md), so the column is not created.
async def load_polls():
    global polls
    try:
        async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"]) as conn:
            cur = await conn.execute(
                """select p.id, p.type, p.salt, extract(epoch from p.window_start)::float8,
                          extract(epoch from p.window_end)::float8 + p.grace_s, array_agg(o.idx)
                     from polls p join options o on o.poll_id = p.id
                    where p.status <> 'draft' group by p.id"""
            )
            polls = {row[0]: row[1:] for row in await cur.fetchall()}
    except psycopg.Error as e:  # Postgres is down: keep working on the last polls
        print("polls refresh failed:", e, flush=True)


async def refresh_polls():
    while True:
        await load_polls()
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    global producer
    producer = AIOKafkaProducer(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"], acks="all", enable_idempotence=True, compression_type="lz4"
    )
    await producer.start()
    task = asyncio.create_task(refresh_polls())
    yield
    task.cancel()
    await producer.stop()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def bad_request(request, exc):
    return Response(status_code=400)


class VoteIn(BaseModel):
    poll_id: uuid.UUID
    options: list[StrictInt]
    voter_id: uuid.UUID
    fp: dict


@app.post("/api/vote", status_code=204)
async def vote(body: VoteIn, request: Request):
    xff = request.headers.get("x-forwarded-for")
    raw = request.headers.get("cf-connecting-ip") or (xff.split(",")[0] if TRUST_XFF and xff else request.client.host)
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:  # STUB: question — an unparsable client IP is not in docs; treated as a bad request
        return Response(status_code=400)
    ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:a.b.c.d -> a.b.c.d
    if body.poll_id not in polls:  # activated less than a second ago?
        await load_polls()
    poll = polls.get(body.poll_id)
    if poll is None:
        return Response(status_code=404)
    poll_type, salt, start, end, idxs = poll
    now = time.time()
    if salt is None or not start <= now <= end:
        return Response(status_code=410)
    chosen = set(body.options)
    if not chosen or len(chosen) < len(body.options) or not chosen <= set(idxs) or (poll_type == "single" and len(chosen) > 1):
        return Response(status_code=422)
    record = encode(Vote(
        poll_id=body.poll_id,
        options=sum(1 << i for i in chosen),
        received_at_ms=int(now * 1000),
        ip_hmac=hmac.digest(salt, ip.packed if ip.version == 4 else ip.packed[:8], "sha256")[:16],
        fp_hash=hashlib.blake2b(json.dumps(body.fp, sort_keys=True, separators=(",", ":")).encode()).digest()[:8],
        voter_id=body.voter_id,
    ))
    try:
        await asyncio.wait_for(producer.send_and_wait("votes_raw", record, key=body.voter_id.bytes), DELIVERY_TIMEOUT_S)
    except Exception:
        return Response(status_code=503)
    return Response(status_code=204)

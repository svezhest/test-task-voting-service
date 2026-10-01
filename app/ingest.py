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
MISS_RELOAD_INTERVAL_S = 0.2
UNKNOWN_POLL_MEMORY_S = 1

# A poll closed more than a day ago is not loaded: 410 is needed only right after the window, later it is 404.
RECENT_POLLS = """
select polls.id,
       polls.type,
       polls.salt,
       extract(epoch from polls.window_start)::float8 as window_start_s,
       extract(epoch from polls.window_end)::float8 + polls.grace_s as accepts_until_s,
       array_agg(options.idx) as option_indexes
  from polls
  join options on options.poll_id = polls.id
 where polls.status <> 'draft'
   and polls.window_end + make_interval(secs => polls.grace_s) > now() - interval '1 day'
 group by polls.id
"""

polls = {}
producer: AIOKafkaProducer
polls_connection = None
polls_lock = asyncio.Lock()
miss_reload = None
miss_reload_starts_at = 0.0
unknown_polls_until = {}  # random poll_ids must not load Postgres


async def load_polls():
    global polls, polls_connection
    async with polls_lock:
        try:
            if polls_connection is None or polls_connection.closed:
                polls_connection = await psycopg.AsyncConnection.connect(
                    os.environ["DATABASE_URL"], autocommit=True, connect_timeout=2
                )
            # a silently dead connection must not hold the lock for minutes
            cursor = await asyncio.wait_for(polls_connection.execute(RECENT_POLLS), 2)
            polls = {row[0]: row[1:] for row in await cursor.fetchall()}
        except (psycopg.Error, TimeoutError) as error:  # Postgres is down: keep working on the last polls
            print("polls refresh failed:", repr(error), flush=True)
            polls_connection = None


async def refresh_polls_every_second():
    global unknown_polls_until
    while True:
        await load_polls()
        unknown_polls_until = {
            poll_id: until for poll_id, until in unknown_polls_until.items() if until > time.monotonic()
        }
        await asyncio.sleep(1)


async def load_polls_at(moment):
    await asyncio.sleep(moment - time.monotonic())
    await load_polls()


async def reload_polls_after_miss(poll_id, arrived_at):
    global miss_reload, miss_reload_starts_at
    latest_reload_may_miss_this_poll = miss_reload_starts_at < arrived_at
    if latest_reload_may_miss_this_poll:
        miss_reload_starts_at = max(miss_reload_starts_at + MISS_RELOAD_INTERVAL_S, arrived_at)
        miss_reload = asyncio.create_task(load_polls_at(miss_reload_starts_at))
    await asyncio.shield(miss_reload)  # a client that hangs up does not cancel the reload for the others
    if poll_id not in polls:
        unknown_polls_until[poll_id] = time.monotonic() + UNKNOWN_POLL_MEMORY_S


@asynccontextmanager
async def lifespan(app):
    global producer
    producer = AIOKafkaProducer(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"],
        acks="all",
        enable_idempotence=True,
        compression_type="lz4",
        # also the batch expiry. Not a hard bound: with idempotence aiokafka retries retriable errors,
        # so a vote answered 503 may still reach Kafka later (architecture.md, ingest).
        request_timeout_ms=int(DELIVERY_TIMEOUT_S * 1000),
    )
    await producer.start()
    polls_refresher = asyncio.create_task(refresh_polls_every_second())
    yield
    polls_refresher.cancel()
    await producer.stop()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_body_is_400(request, exc):
    return Response(status_code=400)


class VoteIn(BaseModel):
    poll_id: uuid.UUID
    options: list[StrictInt]
    voter_id: uuid.UUID
    fp: dict


def client_ip(request):
    address = request.headers.get("cf-connecting-ip")
    if not address:
        forwarded_for = request.headers.get("x-forwarded-for")
        if TRUST_XFF and forwarded_for:
            address = forwarded_for.split(",")[0]
        else:
            address = request.client.host
    ip = ipaddress.ip_address(address.strip())
    ipv4_inside_ipv6 = getattr(ip, "ipv4_mapped", None)
    if ipv4_inside_ipv6 is not None:
        return ipv4_inside_ipv6
    return ip


def hash_ip(salt, ip):
    if ip.version == 4:
        return hmac.digest(salt, ip.packed, "sha256")[:16]
    ipv6_prefix_64 = ip.packed[:8]
    return hmac.digest(salt, ipv6_prefix_64, "sha256")[:16]


def hash_fingerprint(fp):
    canonical_json = json.dumps(fp, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical_json.encode()).digest()[:8]


@app.post("/api/vote", status_code=204)
async def vote(body: VoteIn, request: Request):
    arrived_at = time.monotonic()
    try:
        ip = client_ip(request)
    except ValueError:
        return Response(status_code=400)
    if body.poll_id not in polls and unknown_polls_until.get(body.poll_id, 0) < time.monotonic():
        await reload_polls_after_miss(body.poll_id, arrived_at)
    poll = polls.get(body.poll_id)
    if poll is None:
        return Response(status_code=404)

    poll_type, salt, window_start_s, accepts_until_s, option_indexes = poll
    now = time.time()
    is_active = salt is not None
    in_window = window_start_s <= now <= accepts_until_s
    if not is_active or not in_window:
        return Response(status_code=410)

    chosen = set(body.options)
    is_empty = not chosen
    has_repeats = len(chosen) < len(body.options)
    has_unknown_option = not chosen <= set(option_indexes)
    too_many_for_single = poll_type == "single" and len(chosen) > 1
    if is_empty or has_repeats or has_unknown_option or too_many_for_single:
        return Response(status_code=422)

    record = encode(
        Vote(
            poll_id=body.poll_id,
            options=sum(1 << idx for idx in chosen),
            received_at_ms=int(now * 1000),
            ip_hmac=hash_ip(salt, ip),
            fp_hash=hash_fingerprint(body.fp),
            voter_id=body.voter_id,
        )
    )
    try:
        await asyncio.wait_for(
            producer.send_and_wait("votes_raw", record, key=body.voter_id.bytes),
            DELIVERY_TIMEOUT_S,
        )
    except Exception:
        return Response(status_code=503)
    return Response(status_code=204)

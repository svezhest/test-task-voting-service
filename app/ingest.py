import asyncio
import hashlib
import hmac
import ipaddress
import json
import time
import uuid
from contextlib import asynccontextmanager

import psycopg
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, StrictInt

from app.record import Vote, encode

from app.config import (
    CLOSED_POLL_MEMORY_S,
    DATABASE_URL,
    DELIVERY_TIMEOUT_S,
    KAFKA_BOOTSTRAP,
    MISS_RELOAD_INTERVAL_S,
    POLLS_QUERY_TIMEOUT_S,
    POLLS_REFRESH_INTERVAL_S,
    POSTGRES_CONNECT_TIMEOUT_S,
    TRUST_XFF,
    UNKNOWN_POLL_MEMORY_S,
)

# After CLOSED_POLL_MEMORY_S a closed poll is not loaded: 410 matters only right after the window, later it is 404.
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
   and polls.window_end + make_interval(secs => polls.grace_s) > now() - make_interval(secs => %s)
 group by polls.id
"""


class PollCache:
    """Polls ingest accepts votes for: re-read from Postgres every second and, at most every 200 ms, after a miss."""

    def __init__(self):
        self.polls = {}
        self.connection = None
        self.lock = asyncio.Lock()
        self.miss_reload = None
        self.miss_reload_starts_at = 0.0
        self.unknown_polls_until = {}  # random poll_ids must not load Postgres

    async def load(self):
        async with self.lock:
            try:
                if self.connection is None or self.connection.closed:
                    self.connection = await psycopg.AsyncConnection.connect(
                        DATABASE_URL, autocommit=True, connect_timeout=POSTGRES_CONNECT_TIMEOUT_S
                    )
                # a silently dead connection must not hold the lock for minutes
                cursor = await asyncio.wait_for(
                    self.connection.execute(RECENT_POLLS, (CLOSED_POLL_MEMORY_S,)), POLLS_QUERY_TIMEOUT_S
                )
                self.polls = {row[0]: row[1:] for row in await cursor.fetchall()}
            except (psycopg.Error, TimeoutError) as error:  # Postgres is down: keep working on the last polls
                print("polls refresh failed:", repr(error), flush=True)
                self.connection = None

    async def refresh_forever(self):
        while True:
            await self.load()
            now = time.monotonic()
            self.unknown_polls_until = {
                poll_id: until for poll_id, until in self.unknown_polls_until.items() if until > now
            }
            await asyncio.sleep(POLLS_REFRESH_INTERVAL_S)

    async def get(self, poll_id, arrived_at):
        is_known = poll_id in self.polls
        recently_missed = self.unknown_polls_until.get(poll_id, 0) >= time.monotonic()
        if not is_known and not recently_missed:
            await self.reload_after_miss(poll_id, arrived_at)
        return self.polls.get(poll_id)

    async def reload_after_miss(self, poll_id, arrived_at):
        latest_reload_may_miss_this_poll = self.miss_reload_starts_at < arrived_at
        if latest_reload_may_miss_this_poll:
            self.miss_reload_starts_at = max(self.miss_reload_starts_at + MISS_RELOAD_INTERVAL_S, arrived_at)
            self.miss_reload = asyncio.create_task(self.load_at(self.miss_reload_starts_at))
        await asyncio.shield(self.miss_reload)  # a client that hangs up does not cancel the reload for the others
        if poll_id not in self.polls:
            self.unknown_polls_until[poll_id] = time.monotonic() + UNKNOWN_POLL_MEMORY_S

    async def load_at(self, moment):
        await asyncio.sleep(moment - time.monotonic())
        await self.load()


@asynccontextmanager
async def lifespan(app):
    app.state.polls = PollCache()
    app.state.producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        acks="all",
        enable_idempotence=True,
        compression_type="lz4",
        # also the batch expiry. Not a hard bound: with idempotence aiokafka retries retriable errors,
        # so a vote answered 503 may still reach Kafka later (architecture.md, ingest).
        request_timeout_ms=int(DELIVERY_TIMEOUT_S * 1000),
    )
    await app.state.producer.start()
    polls_refresher = asyncio.create_task(app.state.polls.refresh_forever())
    yield
    polls_refresher.cancel()
    await app.state.producer.stop()


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
    poll = await request.app.state.polls.get(body.poll_id, arrived_at)
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
            request.app.state.producer.send_and_wait("votes_raw", record, key=body.voter_id.bytes),
            DELIVERY_TIMEOUT_S,
        )
    except Exception:
        return Response(status_code=503)
    return Response(status_code=204)

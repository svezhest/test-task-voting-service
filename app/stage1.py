import asyncio
import os
import time
from collections import Counter, defaultdict

import psycopg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition

from app.record import decode

PARTITIONS = [TopicPartition("votes_raw", p) for p in range(8)]
DELIVERY_TIMEOUT_S = float(os.environ["DELIVERY_TIMEOUT_S"])

# poll_id -> window_start, window_end + grace_s (ms), close_at passed
POLLS = """
select id, extract(epoch from window_start)::float8 * 1000, extract(epoch from window_end)::float8 * 1000 + grace_s * 1000,
       now() > window_end + make_interval(secs => grace_s + %s)
  from polls where status in ('active', 'counting')"""
PROGRESS = """
insert into stage_progress (poll_id, stage, partition, votes, end_offset) values (%s, 1, %s, %s, %s)
on conflict (poll_id, stage, partition) do update set votes = excluded.votes, end_offset = excluded.end_offset
 where excluded.end_offset > stage_progress.end_offset"""
TIMELINE = """
insert into timeline (poll_id, partition, second, votes) values (%s, %s, %s, %s)
on conflict (poll_id, partition, second) do update set votes = excluded.votes"""
DONE = "update stage_progress set done_at = now() where poll_id = %s and stage = 1 and partition = %s and done_at is null"


async def report(db, consumer, producer, polls, votes, timeline):
    ends = await consumer.end_offsets(PARTITIONS)  # taken after the close_at check
    for poll_id, (_, _, closed) in polls.items():
        for tp in PARTITIONS:
            position = await consumer.position(tp)
            key = (poll_id, tp.partition)
            # timeline only when progress moved forward: a restarted stage 1 does not roll the curve back
            if (await db.execute(PROGRESS, (*key, votes[key], position))).rowcount:
                async with db.cursor() as cur:
                    await cur.executemany(TIMELINE, [(*key, t, n) for t, n in timeline[key].items()])
            if closed and position >= ends[tp]:
                await producer.flush()
                await db.execute(DONE, key)


async def main():
    bootstrap = os.environ["KAFKA_BOOTSTRAP"]
    db = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)
    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True, compression_type="lz4")
    await consumer.start()
    await producer.start()
    consumer.assign(PARTITIONS)
    start = (await (await db.execute(
        "select extract(epoch from min(window_start)) * 1000 from polls where status in ('active', 'counting')"
    )).fetchone())[0]
    ends = await consumer.end_offsets(PARTITIONS)  # before offsets_for_times: nothing written in between is skipped
    found = await consumer.offsets_for_times({tp: int(start) for tp in PARTITIONS}) if start is not None else {}
    for tp in PARTITIONS:
        consumer.seek(tp, found[tp].offset if found.get(tp) else ends[tp])

    # STUB: per-poll state is never freed after the poll is final.
    votes = defaultdict(int)  # (poll_id, partition) -> all votes
    seen = defaultdict(set)  # (poll_id, partition) -> voter_ids
    timeline = defaultdict(Counter)  # (poll_id, partition) -> {second from window_start: votes}
    last_report = 0.0
    while True:
        batch = await consumer.getmany(timeout_ms=1000)
        # read after the batch, so a vote accepted by ingest's stale cache after finish meets the new window_end
        polls = {row[0]: row[1:] for row in await (await db.execute(POLLS, (DELIVERY_TIMEOUT_S,))).fetchall()}
        for tp, messages in batch.items():
            for m in messages:
                v = decode(m.value)
                if v.poll_id not in polls:  # final or deleted poll
                    continue
                window_start, window_end, _ = polls[v.poll_id]
                if v.received_at_ms > window_end:  # after window_end + grace_s
                    continue
                key = (v.poll_id, tp.partition)
                votes[key] += 1
                timeline[key][int((v.received_at_ms - window_start) // 1000)] += 1
                if v.voter_id not in seen[key]:
                    seen[key].add(v.voter_id)
                    await producer.send("votes_by_ip", m.value, key=v.ip_hmac)
        if time.monotonic() - last_report >= 1:
            last_report = time.monotonic()
            await report(db, consumer, producer, polls, votes, timeline)


if __name__ == "__main__":
    asyncio.run(main())

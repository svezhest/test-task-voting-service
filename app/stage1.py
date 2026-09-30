import asyncio
import os
import time
from collections import defaultdict

import psycopg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition

from app.record import decode

PARTITIONS = [TopicPartition("votes_raw", p) for p in range(8)]
DELIVERY_TIMEOUT_S = float(os.environ["DELIVERY_TIMEOUT_S"])

PROGRESS = """
insert into stage_progress (poll_id, stage, partition, votes, end_offset) values (%s, 1, %s, %s, %s)
on conflict (poll_id, stage, partition) do update set votes = excluded.votes, end_offset = excluded.end_offset
 where excluded.end_offset > stage_progress.end_offset"""
DONE = "update stage_progress set done_at = now() where poll_id = %s and stage = 1 and partition = %s and done_at is null"


async def report(db, consumer, producer, votes):
    polls = await (await db.execute(
        """select id, now() > window_end + make_interval(secs => grace_s + %s)
             from polls where status in ('active', 'counting')""",
        (DELIVERY_TIMEOUT_S,),
    )).fetchall()
    ends = await consumer.end_offsets(PARTITIONS)  # taken after the close_at check
    for poll_id, closed in polls:
        for tp in PARTITIONS:
            position = await consumer.position(tp)
            await db.execute(PROGRESS, (poll_id, tp.partition, votes[poll_id, tp.partition], position))
            if closed and position >= ends[tp]:
                await producer.flush()
                await db.execute(DONE, (poll_id, tp.partition))


async def main():
    bootstrap = os.environ["KAFKA_BOOTSTRAP"]
    db = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)
    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, enable_auto_commit=False)
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True, compression_type="lz4")
    await consumer.start()
    await producer.start()
    consumer.assign(PARTITIONS)
    start = (await (await db.execute(
        "select extract(epoch from min(window_start)) * 1000 from polls where status <> 'final'"
    )).fetchone())[0]
    if start is not None:
        for tp, found in (await consumer.offsets_for_times({tp: int(start) for tp in PARTITIONS})).items():
            if found is not None:
                consumer.seek(tp, found.offset)
    # partitions with nothing to re-read start from the end (auto_offset_reset="latest")

    # STUB: per-poll state is never freed after the poll is final.
    votes = defaultdict(int)  # (poll_id, partition) -> all votes
    seen = defaultdict(set)  # (poll_id, partition) -> voter_ids
    last_report = 0.0
    while True:
        for tp, messages in (await consumer.getmany(timeout_ms=1000)).items():
            for m in messages:
                v = decode(m.value)
                key = (v.poll_id, tp.partition)
                votes[key] += 1
                if v.voter_id not in seen[key]:
                    seen[key].add(v.voter_id)
                    await producer.send("votes_by_ip", m.value, key=v.ip_hmac)
        if time.monotonic() - last_report >= 1:
            last_report = time.monotonic()
            await report(db, consumer, producer, votes)


if __name__ == "__main__":
    asyncio.run(main())

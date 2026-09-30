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
# rows only for a poll that still exists and is not final
LIVE = "where exists (select 1 from polls where id = %(poll)s and status <> 'final')"
PROGRESS = f"""
insert into stage_progress (poll_id, stage, partition, votes, end_offset) select %(poll)s, 1, %(part)s, %(votes)s, %(offset)s {LIVE}
on conflict (poll_id, stage, partition) do update set votes = excluded.votes, end_offset = excluded.end_offset
 where excluded.end_offset > stage_progress.end_offset"""
TIMELINE = f"""
insert into timeline (poll_id, partition, second, votes) select %(poll)s, %(part)s, %(second)s, %(votes)s {LIVE}
on conflict (poll_id, partition, second) do update set votes = excluded.votes"""
DONE = "update stage_progress set done_at = now() where poll_id = %s and stage = 1 and partition = %s and done_at is null"


async def read_polls(db):
    return {row[0]: row[1:] for row in await (await db.execute(POLLS, (DELIVERY_TIMEOUT_S,))).fetchall()}


async def report(db, consumer, polls, votes, timeline, changed):
    ends = await consumer.end_offsets(PARTITIONS)  # taken after the close_at check
    for poll_id, (_, _, closed) in polls.items():
        for tp in PARTITIONS:
            position = await consumer.position(tp)
            key = (poll_id, tp.partition)
            row = {"poll": poll_id, "part": tp.partition}
            # timeline only when progress moved forward: a restarted stage 1 does not roll the curve back
            async with db.transaction():
                if (await db.execute(PROGRESS, row | {"votes": votes[key], "offset": position})).rowcount:
                    async with db.cursor() as cur:  # only the seconds changed since the last write
                        await cur.executemany(TIMELINE, [row | {"second": t, "votes": timeline[key][t]} for t in changed.pop(key, ())])
            if closed and position >= ends[tp]:
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

    votes = defaultdict(int)  # (poll_id, partition) -> all votes
    seen = defaultdict(set)  # (poll_id, partition) -> voter_ids
    timeline = defaultdict(Counter)  # (poll_id, partition) -> {second from window_start: votes}
    changed = defaultdict(set)  # (poll_id, partition) -> seconds of timeline not written yet
    fresh = defaultdict(list)  # (poll_id, partition) -> received_at of votes since the last report
    sent = []  # delivery futures of votes_by_ip since the last report
    last_report = 0.0
    while True:
        batch = [(tp.partition, m.value, decode(m.value)) for tp, ms in (await consumer.getmany(timeout_ms=1000)).items() for m in ms]
        polls = await read_polls(db)  # after the batch: the first vote of a voter_id is picked against fresh polls
        for partition, value, v in batch:
            # window_end may be cached from before finish: votes are counted in the report, against polls read after them
            if v.poll_id not in polls or v.received_at_ms > polls[v.poll_id][1]:  # final, deleted or late
                continue
            key = (v.poll_id, partition)
            fresh[key].append(v.received_at_ms)
            if v.voter_id not in seen[key]:  # a late vote after finish is dropped by stage 2 (it checks received_at)
                seen[key].add(v.voter_id)
                sent.append(await producer.send("votes_by_ip", value, key=v.ip_hmac))
        if time.monotonic() - last_report >= 1:
            last_report = time.monotonic()
            await asyncio.gather(*sent)  # raises on a failed delivery: the restart forwards again
            sent.clear()
            polls = await read_polls(db)  # after the batch, so a vote accepted by ingest's stale cache after finish meets the new window_end
            for key, received in fresh.items():
                if key[0] in polls:
                    window_start, window_end, _ = polls[key[0]]
                    for r in received:
                        if r <= window_end:
                            votes[key] += 1
                            timeline[key][t := int((r - window_start) // 1000)] += 1
                            changed[key].add(t)
            fresh.clear()
            for state in (votes, seen, timeline, changed):  # free polls that are final or deleted
                for key in [key for key in state if key[0] not in polls]:
                    del state[key]
            await report(db, consumer, polls, votes, timeline, changed)


if __name__ == "__main__":
    asyncio.run(main())

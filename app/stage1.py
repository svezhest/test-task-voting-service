import asyncio
import time
from collections import Counter, defaultdict

import psycopg
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition

from app.config import (
    DATABASE_URL,
    DELIVERY_TIMEOUT_S,
    KAFKA_BOOTSTRAP,
    KAFKA_POLL_TIMEOUT_S,
    PARTITION_COUNT,
    STAGE1_REPORT_INTERVAL_S,
    STAGE1_WORKER_INDEX,
    STAGE1_WORKERS,
)
from app.record import VOTER_ID_BYTES, decode

MY_PARTITIONS = [
    TopicPartition("votes_raw", partition)
    for partition in range(PARTITION_COUNT)
    if partition % STAGE1_WORKERS == STAGE1_WORKER_INDEX
]

UNFINISHED_POLLS = """
select id,
       extract(epoch from window_start)::float8 * 1000 as window_start_ms,
       extract(epoch from window_end)::float8 * 1000 + grace_s * 1000 as accepts_until_ms,
       now() > window_end + make_interval(secs => grace_s + %s) as close_at_passed
  from polls
 where status in ('active', 'counting')
"""
EARLIEST_WINDOW_START_MS = """
select extract(epoch from min(window_start)) * 1000
  from polls
 where status in ('active', 'counting')
"""
WHERE_POLL_EXISTS_AND_IS_NOT_FINAL = """
 where exists (
           select 1
             from polls
            where id = %(poll)s
              and status <> 'final'
       )
"""
UPSERT_PROGRESS = f"""
insert into stage_progress (poll_id, stage, partition, votes, end_offset)
select %(poll)s, 1, %(part)s, %(votes)s, %(offset)s
{WHERE_POLL_EXISTS_AND_IS_NOT_FINAL}
    on conflict (poll_id, stage, partition) do update
   set votes = excluded.votes,
       end_offset = excluded.end_offset
 where excluded.end_offset > stage_progress.end_offset
"""
UPSERT_TIMELINE = f"""
insert into timeline (poll_id, partition, second, votes)
select %(poll)s, %(part)s, %(second)s, %(votes)s
{WHERE_POLL_EXISTS_AND_IS_NOT_FINAL}
    on conflict (poll_id, partition, second) do update
   set votes = excluded.votes
"""
MARK_DONE = """
update stage_progress
   set done_at = now()
 where poll_id = %s
   and stage = 1
   and partition = %s
   and done_at is null
"""


async def read_unfinished_polls(connection):
    cursor = await connection.execute(UNFINISHED_POLLS, (DELIVERY_TIMEOUT_S,))
    return {row[0]: row[1:] for row in await cursor.fetchall()}


async def seek_to_earliest_window(connection, consumer):
    cursor = await connection.execute(EARLIEST_WINDOW_START_MS)
    (earliest_window_start_ms,) = await cursor.fetchone()
    # before offsets_for_times: nothing written in between is skipped
    end_offsets = await consumer.end_offsets(MY_PARTITIONS)
    if earliest_window_start_ms is None:
        window_start_offsets = {}
    else:
        window_start_offsets = await consumer.offsets_for_times(
            {topic_partition: int(earliest_window_start_ms) for topic_partition in MY_PARTITIONS}
        )
    for topic_partition in MY_PARTITIONS:
        window_start_offset = window_start_offsets.get(topic_partition)
        if window_start_offset is not None:
            consumer.seek(topic_partition, window_start_offset.offset)
        else:
            consumer.seek(topic_partition, end_offsets[topic_partition])


def count_received(polls, received_at_by_key, received_count, timeline, unwritten_seconds):
    for key, received_at in received_at_by_key.items():
        poll_id, _ = key
        if poll_id not in polls:
            continue
        window_start_ms, accepts_until_ms, _ = polls[poll_id]
        for received_at_ms in received_at:
            if received_at_ms <= accepts_until_ms:
                second = int((received_at_ms - window_start_ms) // 1000)
                received_count[key] += 1
                timeline[key][second] += 1
                unwritten_seconds[key].add(second)


def forget_final_and_deleted_polls(polls, states):
    for state in states:
        keys_to_forget = [key for key in state if key[0] not in polls]
        for key in keys_to_forget:
            del state[key]


async def write_progress(connection, consumer, polls, received_count, timeline, unwritten_seconds):
    end_offsets = await consumer.end_offsets(MY_PARTITIONS)  # taken after the close_at check
    for poll_id, (_, _, close_at_passed) in polls.items():
        for topic_partition in MY_PARTITIONS:
            position = await consumer.position(topic_partition)
            key = (poll_id, topic_partition.partition)
            row = {"poll": poll_id, "part": topic_partition.partition}
            async with connection.transaction():
                progress = await connection.execute(
                    UPSERT_PROGRESS, row | {"votes": received_count[key], "offset": position}
                )
                # a restarted stage 1 does not roll the curve back
                progress_moved_forward = progress.rowcount
                if progress_moved_forward:
                    async with connection.cursor() as cursor:
                        timeline_rows = [
                            row | {"second": second, "votes": timeline[key][second]}
                            for second in unwritten_seconds.pop(key, ())
                        ]
                        await cursor.executemany(UPSERT_TIMELINE, timeline_rows)
            if close_at_passed and position >= end_offsets[topic_partition]:
                await connection.execute(MARK_DONE, key)


async def main():
    connection = await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True)
    consumer = AIOKafkaConsumer(bootstrap_servers=KAFKA_BOOTSTRAP, enable_auto_commit=False)
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP, acks="all", enable_idempotence=True, compression_type="lz4"
    )
    await consumer.start()
    await producer.start()
    consumer.assign(MY_PARTITIONS)
    await seek_to_earliest_window(connection, consumer)

    # all state is keyed by (poll_id, partition)
    received_count = defaultdict(int)
    seen_voter_ids = defaultdict(set)  # raw 16 bytes: less memory than uuid.UUID
    timeline = defaultdict(Counter)
    unwritten_seconds = defaultdict(set)
    received_at_since_report = defaultdict(list)
    deliveries_since_report = []
    last_report_at = 0.0
    while True:
        messages_by_partition = await consumer.getmany(timeout_ms=int(KAFKA_POLL_TIMEOUT_S * 1000))
        batch = [
            (topic_partition.partition, message.value, decode(message.value))
            for topic_partition, messages in messages_by_partition.items()
            for message in messages
        ]
        # after the batch: the first vote of a voter_id is picked against fresh polls
        polls = await read_unfinished_polls(connection)
        for partition, record, vote in batch:
            if vote.poll_id not in polls:
                continue
            _, accepts_until_ms, _ = polls[vote.poll_id]
            if vote.received_at_ms > accepts_until_ms:
                continue
            key = (vote.poll_id, partition)
            received_at_since_report[key].append(vote.received_at_ms)
            voter_id = record[VOTER_ID_BYTES]
            # a late vote after finish is dropped by stage 2 (it checks received_at)
            if voter_id not in seen_voter_ids[key]:
                seen_voter_ids[key].add(voter_id)
                deliveries_since_report.append(await producer.send("votes_by_ip", record, key=vote.ip_hmac))

        if time.monotonic() - last_report_at >= STAGE1_REPORT_INTERVAL_S:
            last_report_at = time.monotonic()
            await asyncio.gather(*deliveries_since_report)  # raises on a failed delivery: the restart forwards again
            deliveries_since_report.clear()
            # ingest's stale cache may accept a vote after finish: count it only against window_end read after it
            polls = await read_unfinished_polls(connection)
            count_received(polls, received_at_since_report, received_count, timeline, unwritten_seconds)
            received_at_since_report.clear()
            forget_final_and_deleted_polls(polls, (received_count, seen_voter_ids, timeline, unwritten_seconds))
            await write_progress(connection, consumer, polls, received_count, timeline, unwritten_seconds)


if __name__ == "__main__":
    asyncio.run(main())

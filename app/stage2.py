import asyncio
import os
import struct
from collections import Counter, defaultdict

import numpy
import psycopg
from aiokafka import AIOKafkaConsumer, TopicPartition
from psycopg.types.json import Jsonb

from app.dedup import estimate_people_many, ip_ceiling, key_limit, poisson_limits, repeat_budget
from app.record import POLL_ID_BYTES, decode

PARTITION_COUNT = 8
WORKERS = int(os.environ["STAGE2_WORKERS"])
WORKER_INDEX = int(os.environ["STAGE2_WORKER_INDEX"])
MY_PARTITIONS = [partition for partition in range(PARTITION_COUNT) if partition % WORKERS == WORKER_INDEX]
DELIVERY_TIMEOUT_S = float(os.environ["DELIVERY_TIMEOUT_S"])

ALL_VOTES_IDX = -1
IP_BUCKETS = ["1", "2-10", "11-100", "101+"]
FP_COUNTS_ENTRY = struct.Struct("<8sQ")

START_COUNTING_CLOSED_POLLS = """
update polls
   set status = 'counting',
       salt = null
 where status = 'active'
   and now() > window_end + make_interval(secs => grace_s + %s)
"""
COUNTING_POLLS_WITH_STAGE1_DONE = """
select id,
       extract(epoch from window_start)::float8 * 1000 as window_start_ms,
       extract(epoch from window_end)::float8 * 1000 + grace_s * 1000 as accepts_until_ms,
       extract(epoch from window_end - window_start)::float8 + grace_s as window_s,
       (
           select count(*)
             from fp_counts
            where poll_id = polls.id
       ) = %(partitions)s as all_fp_counts_written
  from polls
 where status = 'counting'
   and (
           select count(*)
             from stage_progress
            where poll_id = polls.id
              and stage = 1
              and done_at is not null
       ) = %(partitions)s
"""
UPSERT_FP_COUNTS = """
insert into fp_counts
values (%s, %s, %s)
    on conflict (poll_id, partition) do update
   set blob = excluded.blob
"""
FP_COUNTS = """
select blob
  from fp_counts
 where poll_id = %s
 order by partition
"""
UPSERT_RESULT = """
insert into results (poll_id, partition, option_idx, counted, total, last_offset)
values (%s, %s, %s, %s, %s, %s)
    on conflict (poll_id, partition, option_idx) do update
   set counted = excluded.counted,
       total = excluded.total,
       last_offset = excluded.last_offset
 where excluded.last_offset > results.last_offset
"""
UPSERT_STATS = """
insert into stage2_stats (poll_id, partition, key_limit, ip_ceiling, ip_hist, last_offset)
values (%s, %s, %s, %s, %s, %s)
    on conflict (poll_id, partition) do update
   set key_limit = excluded.key_limit,
       ip_ceiling = excluded.ip_ceiling,
       ip_hist = excluded.ip_hist,
       last_offset = excluded.last_offset
 where excluded.last_offset > stage2_stats.last_offset
"""
MARK_FINAL_IF_ALL_PARTITIONS_WRITTEN = """
update polls
   set status = 'final'
 where id = %(poll)s
   and (
           select count(*)
             from results
            where poll_id = %(poll)s
              and option_idx = -1
       ) = %(partitions)s
"""


# STUB: the partition is read once and kept in memory for both passes instead of being re-read from Kafka.
async def read_partition(consumer, partition, poll_id, window_start_ms, accepts_until_ms):
    topic_partition = TopicPartition("votes_by_ip", partition)
    consumer.assign([topic_partition])
    end_offset = (await consumer.end_offsets([topic_partition]))[topic_partition]
    window_start_offset = (await consumer.offsets_for_times({topic_partition: window_start_ms}))[topic_partition]
    votes = []
    seen_voter_ids = set()
    if window_start_offset is None:
        return votes, end_offset
    consumer.seek(topic_partition, window_start_offset.offset)
    while await consumer.position(topic_partition) < end_offset:
        messages_by_partition = await consumer.getmany(topic_partition, timeout_ms=1000)
        for message in messages_by_partition.get(topic_partition, []):
            if message.offset >= end_offset:
                continue
            if message.value[POLL_ID_BYTES] != poll_id.bytes:  # cheap check before decode
                continue
            vote = decode(message.value)
            if vote.received_at_ms <= accepts_until_ms and vote.voter_id not in seen_voter_ids:
                seen_voter_ids.add(vote.voter_id)
                votes.append(vote)
    return votes, end_offset


async def write_fp_counts(connection, poll_id, partition, votes):
    voters_by_fingerprint = Counter(vote.fp_hash for vote in votes)
    blob = b"".join(FP_COUNTS_ENTRY.pack(fp_hash, voters) for fp_hash, voters in voters_by_fingerprint.items())
    await connection.execute(UPSERT_FP_COUNTS, (poll_id, partition, blob))


async def pass1(connection, consumer, poll_id, window_start_ms, accepts_until_ms):
    partitions = {}
    for partition in MY_PARTITIONS:
        partitions[partition] = await read_partition(consumer, partition, poll_id, window_start_ms, accepts_until_ms)
    for partition, (votes, _) in partitions.items():
        await write_fp_counts(connection, poll_id, partition, votes)
    return partitions


async def read_fingerprint_shares(connection, poll_id):
    voters_by_fingerprint = Counter()
    cursor = await connection.execute(FP_COUNTS, (poll_id,))
    for (blob,) in await cursor.fetchall():
        voters_by_fingerprint.update(dict(FP_COUNTS_ENTRY.iter_unpack(blob)))
    all_voters = voters_by_fingerprint.total()
    # in a fixed order, so every worker and every rerun gets the same p
    return {fp_hash: voters_by_fingerprint[fp_hash] / all_voters for fp_hash in sorted(voters_by_fingerprint)}


def key_limits(votes, fingerprint_shares, all_shares, max_repeats):
    fingerprints_by_ip = defaultdict(set)
    voters_by_key = Counter()
    for vote in votes:
        fingerprints_by_ip[vote.ip_hmac].add(vote.fp_hash)
        voters_by_key[vote.ip_hmac, vote.fp_hash] += 1
    distinct_fingerprints = numpy.array([len(fingerprints) for fingerprints in fingerprints_by_ip.values()])
    people_by_ip = dict(zip(fingerprints_by_ip, estimate_people_many(distinct_fingerprints, all_shares)))
    expected_honest_people = numpy.array(
        [people_by_ip[ip_hmac] * fingerprint_shares[fp_hash] for ip_hmac, fp_hash in voters_by_key]
    )
    return {
        key: key_limit(int(n_limit), voters_by_key[key], max_repeats)
        for key, n_limit in zip(voters_by_key, poisson_limits(expected_honest_people))
    }


def count_votes(votes, limits, max_votes_per_ip):
    counted_on_key = Counter()
    counted_on_ip = Counter()
    rejected_by_key = 0
    rejected_by_ip = 0
    counted = Counter({ALL_VOTES_IDX: 0})
    total = Counter({ALL_VOTES_IDX: 0})
    for vote in votes:
        key = (vote.ip_hmac, vote.fp_hash)
        if counted_on_key[key] >= limits[key]:
            is_counted = False
            rejected_by_key += 1
        elif counted_on_ip[vote.ip_hmac] >= max_votes_per_ip:
            is_counted = False
            rejected_by_ip += 1
        else:
            is_counted = True
            counted_on_key[key] += 1
            counted_on_ip[vote.ip_hmac] += 1
        chosen = [idx for idx in range(vote.options.bit_length()) if vote.options >> idx & 1]
        for idx in [ALL_VOTES_IDX] + chosen:
            total[idx] += 1
            if is_counted:
                counted[idx] += 1
    return counted, total, rejected_by_key, rejected_by_ip


def ip_concentration(votes):
    histogram = {bucket: {"ips": 0, "votes": 0} for bucket in IP_BUCKETS}
    votes_by_ip = Counter(vote.ip_hmac for vote in votes)
    for votes_from_ip in votes_by_ip.values():
        if votes_from_ip == 1:
            bucket = "1"
        elif votes_from_ip <= 10:
            bucket = "2-10"
        elif votes_from_ip <= 100:
            bucket = "11-100"
        else:
            bucket = "101+"
        histogram[bucket]["ips"] += 1
        histogram[bucket]["votes"] += votes_from_ip
    return histogram


async def pass2(connection, poll_id, partitions, window_s):
    fingerprint_shares = await read_fingerprint_shares(connection, poll_id)
    all_shares = numpy.array(list(fingerprint_shares.values()))
    max_repeats = repeat_budget(window_s)
    max_votes_per_ip = ip_ceiling(window_s)

    for partition, (votes, end_offset) in partitions.items():
        limits = key_limits(votes, fingerprint_shares, all_shares, max_repeats)
        counted, total, rejected_by_key, rejected_by_ip = count_votes(votes, limits, max_votes_per_ip)
        histogram = ip_concentration(votes)
        async with connection.transaction():  # the final check below never sees a partition half-written
            await connection.execute(
                UPSERT_STATS, (poll_id, partition, rejected_by_key, rejected_by_ip, Jsonb(histogram), end_offset)
            )
            for idx in total:
                await connection.execute(
                    UPSERT_RESULT, (poll_id, partition, idx, counted[idx], total[idx], end_offset)
                )

    await connection.execute(
        MARK_FINAL_IF_ALL_PARTITIONS_WRITTEN, {"poll": poll_id, "partitions": PARTITION_COUNT}
    )


async def main():
    connection = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)
    consumer = AIOKafkaConsumer(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"],
        enable_auto_commit=False,
        fetch_max_wait_ms=20,  # we read up to a known end: do not wait for new data
    )
    await consumer.start()
    waiting_for_barrier = {}
    results_written = set()
    while True:
        await connection.execute(START_COUNTING_CLOSED_POLLS, (DELIVERY_TIMEOUT_S,))
        cursor = await connection.execute(COUNTING_POLLS_WITH_STAGE1_DONE, {"partitions": PARTITION_COUNT})
        ready = await cursor.fetchall()
        ready_ids = {poll_id for poll_id, *_ in ready}
        waiting_for_barrier = {
            poll_id: partitions for poll_id, partitions in waiting_for_barrier.items() if poll_id in ready_ids
        }
        results_written &= ready_ids
        for poll_id, window_start_ms, accepts_until_ms, window_s, all_fp_counts_written in ready:
            if poll_id in results_written:  # another worker writes the last results and sets final
                continue
            if poll_id not in waiting_for_barrier:
                waiting_for_barrier[poll_id] = await pass1(
                    connection, consumer, poll_id, int(window_start_ms), accepts_until_ms
                )
            elif all_fp_counts_written:
                await pass2(connection, poll_id, waiting_for_barrier.pop(poll_id), window_s)
                results_written.add(poll_id)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())

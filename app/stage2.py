import asyncio
import os
import struct
from collections import Counter, defaultdict

import numpy
import psycopg
from aiokafka import AIOKafkaConsumer, TopicPartition
from psycopg.types.json import Jsonb

from app.dedup import estimate_people_many, ip_ceiling, key_limit, poisson_limits, repeat_budget
from app.record import decode

PARTITIONS = 8
MINE = [p for p in range(PARTITIONS) if p % int(os.environ["STAGE2_WORKERS"]) == int(os.environ["STAGE2_WORKER_INDEX"])]
DELIVERY_TIMEOUT_S = float(os.environ["DELIVERY_TIMEOUT_S"])

RESULT = """
insert into results (poll_id, partition, option_idx, counted, total, last_offset) values (%s, %s, %s, %s, %s, %s)
on conflict (poll_id, partition, option_idx) do update
   set counted = excluded.counted, total = excluded.total, last_offset = excluded.last_offset
 where excluded.last_offset > results.last_offset"""
STATS = """
insert into stage2_stats (poll_id, partition, key_limit, ip_ceiling, ip_hist, last_offset) values (%s, %s, %s, %s, %s, %s)
on conflict (poll_id, partition) do update
   set key_limit = excluded.key_limit, ip_ceiling = excluded.ip_ceiling, ip_hist = excluded.ip_hist, last_offset = excluded.last_offset
 where excluded.last_offset > stage2_stats.last_offset"""
BUCKETS = ["1", "2-10", "11-100", "101+"]
BLOCK = struct.Struct("<8sQ")  # fp_counts blob entry: fp_hash, c(f)


# STUB: the partition is read once and kept in memory for both passes instead of being re-read from Kafka.
async def read_partition(consumer, p, poll_id, start_ms, end_ms):
    """Poll votes from window_start to the end of the partition, received by window_end + grace_s,
    first one per voter_id; and the end offset."""
    tp = TopicPartition("votes_by_ip", p)
    consumer.assign([tp])
    end = (await consumer.end_offsets([tp]))[tp]
    found = (await consumer.offsets_for_times({tp: start_ms}))[tp]
    votes, seen = [], set()
    if found is not None:
        consumer.seek(tp, found.offset)
        while await consumer.position(tp) < end:
            for m in (await consumer.getmany(tp, timeout_ms=1000)).get(tp, []):
                if m.offset >= end or m.value[1:17] != poll_id.bytes:  # cheap check before decode
                    continue
                v = decode(m.value)
                if v.received_at_ms <= end_ms and v.voter_id not in seen:
                    seen.add(v.voter_id)
                    votes.append(v)
    return votes, end


async def pass1(db, consumer, poll_id, start_ms, end_ms):  # c(f) of each partition of this worker
    parts = {p: await read_partition(consumer, p, poll_id, start_ms, end_ms) for p in MINE}
    for p, (votes, _) in parts.items():
        await db.execute(
            "insert into fp_counts values (%s, %s, %s) on conflict (poll_id, partition) do update set blob = excluded.blob",
            (poll_id, p, b"".join(BLOCK.pack(*fc) for fc in Counter(v.fp_hash for v in votes).items())),
        )
    return parts


async def pass2(db, poll_id, parts, window_s):
    # merge: in a fixed order, so every worker and every rerun gets the same p
    c = Counter()
    for (blob,) in await (await db.execute("select blob from fp_counts where poll_id = %s order by partition", (poll_id,))).fetchall():
        c.update(dict(BLOCK.iter_unpack(blob)))
    c_total = c.total()
    share = {f: c[f] / c_total for f in sorted(c)}
    p_all = numpy.array(list(share.values()))
    r, ceiling = repeat_budget(window_s), ip_ceiling(window_s)

    for p, (votes, end) in parts.items():
        fps = defaultdict(set)  # D(IP)
        voters = Counter()  # V(key), key = (ip_hmac, fp_hash)
        for v in votes:
            fps[v.ip_hmac].add(v.fp_hash)
            voters[v.ip_hmac, v.fp_hash] += 1
        people = dict(zip(fps, estimate_people_many(numpy.array([len(f) for f in fps.values()]), p_all)))
        lam = numpy.array([people[ip] * share[f] for ip, f in voters])
        limits = {key: key_limit(int(n), voters[key], r) for key, n in zip(voters, poisson_limits(lam))}
        on_key, on_ip = Counter(), Counter()
        rejected_key = rejected_ip = 0
        counted, total = Counter({-1: 0}), Counter({-1: 0})  # option_idx -1 = number of votes
        for v in votes:
            key = (v.ip_hmac, v.fp_hash)
            key_ok = on_key[key] < limits[key]
            ok = key_ok and on_ip[v.ip_hmac] < ceiling
            # api.md: a vote over both limits is counted in key_limit (checked first)
            rejected_key += not key_ok
            rejected_ip += key_ok and not ok
            on_key[key] += ok
            on_ip[v.ip_hmac] += ok
            # votes over the limit are only counted in total, not stored
            for idx in [-1] + [i for i in range(v.options.bit_length()) if v.options >> i & 1]:
                total[idx] += 1
                counted[idx] += ok
        hist = {b: {"ips": 0, "votes": 0} for b in BUCKETS}
        for n in Counter(v.ip_hmac for v in votes).values():
            b = "1" if n == 1 else "2-10" if n <= 10 else "11-100" if n <= 100 else "101+"
            hist[b]["ips"] += 1
            hist[b]["votes"] += n
        async with db.transaction():  # the final check below never sees a partition half-written
            await db.execute(STATS, (poll_id, p, rejected_key, rejected_ip, Jsonb(hist), end))
            for idx in total:
                await db.execute(RESULT, (poll_id, p, idx, counted[idx], total[idx], end))

    await db.execute(
        """update polls set status = 'final'
            where id = %s and (select count(*) from results where poll_id = %s and option_idx = -1) = %s""",
        (poll_id, poll_id, PARTITIONS),
    )


async def main():
    db = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)
    consumer = AIOKafkaConsumer(bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"], enable_auto_commit=False, fetch_max_wait_ms=20)  # we read up to a known end: do not wait for new data
    await consumer.start()
    waiting, done = {}, set()  # poll_id -> partitions read in pass 1; polls whose results this worker has written
    while True:
        await db.execute(
            """update polls set status = 'counting', salt = null
                where status = 'active' and now() > window_end + make_interval(secs => grace_s + %s)""",
            (DELIVERY_TIMEOUT_S,),
        )
        ready = await (await db.execute(
            """select id, extract(epoch from window_start)::float8 * 1000,
                      extract(epoch from window_end)::float8 * 1000 + grace_s * 1000,
                      extract(epoch from window_end - window_start)::float8 + grace_s,
                      (select count(*) from fp_counts where poll_id = polls.id) = %s
                 from polls
                where status = 'counting'
                  and (select count(*) from stage_progress where poll_id = polls.id and stage = 1 and done_at is not null) = %s""",
            (PARTITIONS, PARTITIONS),
        )).fetchall()
        ids = {row[0] for row in ready}  # forget polls that are final (or gone)
        waiting, done = {k: v for k, v in waiting.items() if k in ids}, done & ids
        # the barrier never blocks: pass 1 for every ready poll, pass 2 once fp_counts of all partitions are in
        for poll_id, start_ms, end_ms, window_s, barrier in ready:
            if poll_id in done:  # another worker writes the last results and sets final
                continue
            if poll_id not in waiting:
                waiting[poll_id] = await pass1(db, consumer, poll_id, int(start_ms), end_ms)
            elif barrier:
                await pass2(db, poll_id, waiting.pop(poll_id), window_s)
                done.add(poll_id)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())

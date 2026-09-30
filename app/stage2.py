import asyncio
import math
import os
from collections import Counter, defaultdict

import numpy
import psycopg
from aiokafka import AIOKafkaConsumer, TopicPartition

from app.dedup import estimate_people, ip_ceiling, key_limit, poisson_limit, repeat_budget
from app.record import decode

PARTITIONS = 8
MINE = [p for p in range(PARTITIONS) if p % int(os.environ["STAGE2_WORKERS"]) == int(os.environ["STAGE2_WORKER_INDEX"])]
DELIVERY_TIMEOUT_S = float(os.environ["DELIVERY_TIMEOUT_S"])

RESULT = """
insert into results (poll_id, partition, option_idx, counted, total, last_offset) values (%s, %s, %s, %s, %s, %s)
on conflict (poll_id, partition, option_idx) do update
   set counted = excluded.counted, total = excluded.total, last_offset = excluded.last_offset
 where excluded.last_offset > results.last_offset"""


def pack_counts(counts):  # c(f) block: 8-byte fp_hash + 8-byte count, repeated
    return b"".join(f + n.to_bytes(8, "little") for f, n in counts.items())


def unpack_counts(blob):
    return {blob[i:i + 8]: int.from_bytes(blob[i + 8:i + 16], "little") for i in range(0, len(blob), 16)}


# STUB: the partition is read once and kept in memory for both passes instead of being re-read from Kafka.
async def read_partition(consumer, p, poll_id, start_ms):
    """Poll votes from window_start to the end of the partition, first one per voter_id; and the end offset."""
    tp = TopicPartition("votes_by_ip", p)
    consumer.assign([tp])
    end = (await consumer.end_offsets([tp]))[tp]
    found = (await consumer.offsets_for_times({tp: start_ms}))[tp]
    votes, seen = [], set()
    if found is not None:
        consumer.seek(tp, found.offset)
        while await consumer.position(tp) < end:
            for m in (await consumer.getmany(tp, timeout_ms=1000)).get(tp, []):
                v = decode(m.value)
                if m.offset < end and v.poll_id == poll_id and v.voter_id not in seen:
                    seen.add(v.voter_id)
                    votes.append(v)
    return votes, end


async def count_poll(db, consumer, poll_id, start_ms, window_s):
    parts = {p: await read_partition(consumer, p, poll_id, start_ms) for p in MINE}

    # pass 1: c(f) of each partition
    for p, (votes, _) in parts.items():
        await db.execute(
            "insert into fp_counts values (%s, %s, %s) on conflict (poll_id, partition) do update set blob = excluded.blob",
            (poll_id, p, pack_counts(Counter(v.fp_hash for v in votes))),
        )
    while (await (await db.execute(
        "select count(*) from fp_counts where poll_id = %s and partition >= 0", (poll_id,)
    )).fetchone())[0] < PARTITIONS:
        await asyncio.sleep(1)

    # merge
    c = Counter()
    for (blob,) in await (await db.execute(
        "select blob from fp_counts where poll_id = %s and partition >= 0", (poll_id,)
    )).fetchall():
        c.update(unpack_counts(blob))
    await db.execute("insert into fp_counts values (%s, -1, %s) on conflict do nothing", (poll_id, pack_counts(c)))
    share = {f: n / c.total() for f, n in c.items()}
    p_all = numpy.array(list(share.values()))
    r, ceiling = repeat_budget(window_s), ip_ceiling(window_s)

    # pass 2
    for p, (votes, end) in parts.items():
        fps = defaultdict(set)  # D(IP)
        voters = Counter()  # V(key), key = (ip_hmac, fp_hash)
        for v in votes:
            fps[v.ip_hmac].add(v.fp_hash)
            voters[v.ip_hmac, v.fp_hash] += 1
        people = {}
        for ip, f in fps.items():
            try:
                people[ip] = estimate_people(len(f), p_all)
            except ValueError:
                # STUB: question — D(IP) = number of all fingerprints of the poll (e.g. one IP with 2 of 2
                # fingerprints): the equation has no root and docs don't say what to do. Here n̂ = ∞, no key limit.
                people[ip] = math.inf
        limits = {
            key: n if math.isinf(people[key[0]]) else key_limit(poisson_limit(people[key[0]] * share[key[1]]), n, r)
            for key, n in voters.items()
        }
        on_key, on_ip = Counter(), Counter()
        counted, total = Counter({-1: 0}), Counter({-1: 0})  # option_idx -1 = number of votes
        for v in votes:
            key = (v.ip_hmac, v.fp_hash)
            ok = on_key[key] < limits[key] and on_ip[v.ip_hmac] < ceiling
            on_key[key] += ok
            on_ip[v.ip_hmac] += ok
            # STUB: votes over the limit are not stored anywhere with a mark, only counted in total.
            for idx in [-1] + [i for i in range(64) if v.options >> i & 1]:
                total[idx] += 1
                counted[idx] += ok
        for idx in total:
            await db.execute(RESULT, (poll_id, p, idx, counted[idx], total[idx], end))

    await db.execute(
        """update polls set status = 'final'
            where id = %s and (select count(*) from results where poll_id = %s and option_idx = -1) = %s""",
        (poll_id, poll_id, PARTITIONS),
    )


async def main():
    db = await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True)
    consumer = AIOKafkaConsumer(bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"], enable_auto_commit=False)
    await consumer.start()
    while True:
        await db.execute(
            """update polls set status = 'counting', salt = null
                where status = 'active' and now() > window_end + make_interval(secs => grace_s + %s)""",
            (DELIVERY_TIMEOUT_S,),
        )
        ready = await (await db.execute(
            """select id, extract(epoch from window_start)::float8 * 1000, extract(epoch from window_end - window_start)::float8 + grace_s
                 from polls
                where status = 'counting'
                  and (select count(*) from stage_progress where poll_id = polls.id and stage = 1 and done_at is not null) = %s""",
            (PARTITIONS,),
        )).fetchall()
        for poll_id, start_ms, window_s in ready:
            await count_poll(db, consumer, poll_id, int(start_ms), window_s)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())

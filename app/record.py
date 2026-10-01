import struct
import uuid
from dataclasses import dataclass

VERSION = 1
FORMAT = struct.Struct("<B16sQQ16s8s16s")
POLL_ID_BYTES = slice(1, 17)
VOTER_ID_BYTES = slice(57, 73)


@dataclass(frozen=True)
class Vote:
    poll_id: uuid.UUID
    options: int
    received_at_ms: int
    ip_hmac: bytes
    fp_hash: bytes
    voter_id: uuid.UUID


def encode(vote: Vote) -> bytes:
    return FORMAT.pack(
        VERSION,
        vote.poll_id.bytes,
        vote.options,
        vote.received_at_ms,
        vote.ip_hmac,
        vote.fp_hash,
        vote.voter_id.bytes,
    )


def decode(record: bytes) -> Vote:
    if len(record) != FORMAT.size:
        raise ValueError(f"record must be {FORMAT.size} bytes, got {len(record)}")
    version, poll_id, options, received_at_ms, ip_hmac, fp_hash, voter_id = FORMAT.unpack(record)
    if version != VERSION:
        raise ValueError(f"unknown record version {version}")
    return Vote(uuid.UUID(bytes=poll_id), options, received_at_ms, ip_hmac, fp_hash, uuid.UUID(bytes=voter_id))

import struct
import uuid
from dataclasses import dataclass

VERSION = 1
# version, poll_id, options, received_at, ip_hmac, fp_hash, voter_id — little-endian, 73 bytes
FORMAT = struct.Struct("<B16sQQ16s8s16s")


@dataclass(frozen=True)
class Vote:
    poll_id: uuid.UUID
    options: int
    received_at_ms: int
    ip_hmac: bytes
    fp_hash: bytes
    voter_id: uuid.UUID


def encode(v: Vote) -> bytes:
    return FORMAT.pack(VERSION, v.poll_id.bytes, v.options, v.received_at_ms, v.ip_hmac, v.fp_hash, v.voter_id.bytes)


def decode(b: bytes) -> Vote:
    if len(b) != FORMAT.size:
        raise ValueError(f"record must be {FORMAT.size} bytes, got {len(b)}")
    version, poll_id, options, received_at_ms, ip_hmac, fp_hash, voter_id = FORMAT.unpack(b)
    if version != VERSION:
        raise ValueError(f"unknown record version {version}")
    return Vote(uuid.UUID(bytes=poll_id), options, received_at_ms, ip_hmac, fp_hash, uuid.UUID(bytes=voter_id))

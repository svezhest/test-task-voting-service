import uuid

import pytest

from app.record import Vote, decode, encode


def make(**kw):
    fields = dict(
        poll_id=uuid.uuid4(),
        options=0b101,
        received_at_ms=1_790_000_000_123,
        ip_hmac=bytes(range(16)),
        fp_hash=bytes(range(100, 108)),
        voter_id=uuid.uuid4(),
    )
    fields.update(kw)
    return Vote(**fields)


def test_encode_is_73_bytes():
    assert len(encode(make())) == 73


def test_roundtrip():
    v = make()
    assert decode(encode(v)) == v


# До 64 вариантов: бит i = вариант idx i, включая старший бит 63.
@pytest.mark.parametrize("mask", [1, 1 << 1, 0b1011, 1 << 63, (1 << 64) - 1])
def test_options_bitmask_preserved(mask):
    assert decode(encode(make(options=mask))).options == mask


@pytest.mark.parametrize("n", [0, 72, 74, 146])
def test_decode_wrong_length(n):
    with pytest.raises(ValueError):
        decode(bytes([1]) + bytes(n - 1) if n else b"")


@pytest.mark.parametrize("version", [0, 2, 255])
def test_decode_wrong_version(version):
    b = bytearray(encode(make()))
    b[0] = version
    with pytest.raises(ValueError):
        decode(bytes(b))


def test_layout_of_non_uuid_fields():
    # api.md: version 1 | poll_id 16 | options 8 | received_at 8 | ip_hmac 16 | fp_hash 8 | voter_id 16, little-endian.
    v = make(options=(1 << 63) | 0b110, received_at_ms=1_790_000_000_123)
    b = encode(v)
    assert b[0] == 1
    assert b[17:25] == v.options.to_bytes(8, "little")
    assert b[25:33] == v.received_at_ms.to_bytes(8, "little")
    assert b[33:49] == v.ip_hmac
    assert b[49:57] == v.fp_hash


@pytest.mark.skip(reason="вопрос: порядок байт UUID в записи не задан (uuid.bytes или uuid.bytes_le?); "
                         "«little-endian» в api.md относится к числам или и к UUID?")
def test_layout_of_uuid_fields():
    v = make()
    b = encode(v)
    assert b[1:17] == v.poll_id.bytes
    assert b[57:73] == v.voter_id.bytes

import pytest

from app.core.security import (
    generate_api_key,
    hash_key,
    key_prefix,
    sign_payload,
    sign_token,
    verify_key,
    verify_payload,
    verify_token,
)

SECRET = b"s" * 32
NOW = 1_800_000_000


def test_api_key_roundtrip() -> None:
    key = generate_api_key()
    assert key.startswith("sk_live_") and len(key) > 40
    assert key_prefix(key) == key[:16]
    assert verify_key(key, hash_key(key))
    assert not verify_key(key + "x", hash_key(key))
    assert generate_api_key() != key


GOOD = sign_token("ask_1:per_1", NOW + 60, SECRET)
data, exp, sig = GOOD.split(".")


@pytest.mark.parametrize(
    ("token", "secret", "now", "expected"),
    [
        (GOOD, SECRET, NOW, "ask_1:per_1"),
        (GOOD, SECRET, NOW + 60, None),  # expired exactly at exp
        (GOOD, SECRET, NOW + 3600, None),  # long expired
        (GOOD, b"other" * 8, NOW, None),  # wrong secret
        (f"ask_2:per_1.{exp}.{sig}", SECRET, NOW, None),  # data swapped
        (f"{data}.{int(exp) + 9999}.{sig}", SECRET, NOW, None),  # expiry extended
        (f"{data}.{exp}.{sig[:-1]}A", SECRET, NOW, None),  # signature altered
        (f"{data}.{exp}.ünïcode", SECRET, NOW, None),  # non-ascii signature
        (f"{data}.{exp}", SECRET, NOW, None),  # missing part
        (f"{data}.soon.{sig}", SECRET, NOW, None),  # non-numeric expiry
        ("", SECRET, NOW, None),
    ],
)
def test_verify_token(token: str, secret: bytes, now: int, expected: str | None) -> None:
    assert verify_token(token, secret, now=now) == expected


def test_token_data_cannot_contain_dot() -> None:
    with pytest.raises(ValueError):
        sign_token("a.b", NOW, SECRET)


BODY = b'{"event":"decision.decided"}'
HEADER = sign_payload(BODY, SECRET, NOW)


@pytest.mark.parametrize(
    ("body", "header", "secret", "now", "ok"),
    [
        (BODY, HEADER, SECRET, NOW, True),
        (BODY, HEADER, SECRET, NOW + 300, True),  # inside tolerance
        (BODY, HEADER, SECRET, NOW + 301, False),  # replayed too late
        (BODY, HEADER, SECRET, NOW - 301, False),  # from the future
        (BODY + b" ", HEADER, SECRET, NOW, False),  # body tampered
        (BODY, HEADER, b"other" * 8, NOW, False),  # wrong secret
        (BODY, HEADER.replace(f"t={NOW}", f"t={NOW + 1}"), SECRET, NOW, False),  # t tampered
        (BODY, f"t={NOW}", SECRET, NOW, False),  # no v1
        (BODY, "v1=abc", SECRET, NOW, False),  # no t
        (BODY, "garbage", SECRET, NOW, False),
    ],
)
def test_verify_payload(body: bytes, header: str, secret: bytes, now: int, ok: bool) -> None:
    assert verify_payload(body, header, secret, now=now) is ok


def test_payload_header_shape() -> None:
    assert HEADER.startswith(f"t={NOW},v1=") and len(HEADER.split("v1=")[1]) == 64

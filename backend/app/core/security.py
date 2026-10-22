import base64
import hashlib
import hmac
import secrets
import time

API_KEY_PREFIX = "sk_live_"
KEY_LOOKUP_LEN = 16  # "sk_live_" + 8 chars: stored in clear to find the row; the rest stays secret


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def key_prefix(key: str) -> str:
    return key[:KEY_LOOKUP_LEN]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def verify_key(key: str, key_hash: str) -> bool:
    return hmac.compare_digest(hash_key(key).encode(), key_hash.encode())


def _token_sig(data: str, exp: int, secret: bytes) -> str:
    mac = hmac.new(secret, f"{data}.{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def sign_token(data: str, exp: int, secret: bytes) -> str:
    """URL-safe `data.exp.sig`. `exp` is a unix timestamp; `data` must not contain '.'."""
    if "." in data:
        raise ValueError("token data must not contain '.'")
    return f"{data}.{exp}.{_token_sig(data, exp, secret)}"


def verify_token(token: str, secret: bytes, now: float | None = None) -> str | None:
    """Return the signed data, or None if the token is malformed, tampered with or expired."""
    try:
        data, exp_s, sig = token.split(".")
        exp = int(exp_s)
    except ValueError:
        return None
    expected = _token_sig(data, exp, secret)
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        return None
    return data if exp > (time.time() if now is None else now) else None


def sign_payload(body: bytes, secret: bytes, t: int) -> str:
    """Stripe-style `SaySo-Signature` header value: `t=<unix>,v1=<hex hmac of "t.body">`."""
    v1 = hmac.new(secret, f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def verify_payload(
    body: bytes, header: str, secret: bytes, now: float | None = None, tolerance_s: int = 300
) -> bool:
    parts = {k: v for k, _, v in (p.strip().partition("=") for p in header.split(","))}
    try:
        t = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - t) > tolerance_s:
        return False
    expected = sign_payload(body, secret, t).encode()
    return hmac.compare_digest(f"t={t},v1={parts.get('v1', '')}".encode(), expected)

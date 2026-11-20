"""Cloudflare Access JWTs, as pure decisions: parse a compact JWT, check its RS256 signature
with the standard library alone and judge its claims (see "Authentication" in
development/design/library-ui.md).

Nothing here touches the network, the disk or the clock: the keys and the time are handed in
(`access_keys.py` fetches and caches the keys), and no function raises on bad input. A token
that is not acceptable gets a `Verdict` that says why; the reason never holds the token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ALG = "RS256"
HEADER = "Cf-Access-Jwt-Assertion"  # the header cloudflared forwards; the cookie is never read
LEEWAY = 60  # seconds a `nbf` or `iat` may be in the future (clock drift)
MAX_TOKEN = 8 * 1024  # characters; Access tokens are about 1 KiB
MIN_MODULUS_BITS = 2048  # a smaller key from the key set is not used
UNKNOWN_KID = "the key id is not one of Access's keys"
DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]*")


@dataclass(frozen=True)
class RsaKey:
    """The public half of an RSA key: its modulus and public exponent."""

    n: int
    e: int


@dataclass(frozen=True)
class Verdict:
    """Whether a token is acceptable and, when it is not, why. `email` is the claim of an
    acceptable token, when it has one."""

    ok: bool
    reason: str = ""
    email: str | None = None


def certs_url(team_domain: str) -> str:
    """Where Access publishes its signing keys (always `https`)."""
    return f"https://{team_domain}/cdn-cgi/access/certs"


def issuer(team_domain: str) -> str:
    """The `iss` claim of every token of a team."""
    return f"https://{team_domain}"


def decode_segment(text: str) -> bytes | None:
    """A base64url segment without padding, as a JWT writes it; None for anything else
    (padding, the standard alphabet, spaces, a length no encoder produces)."""
    if not _BASE64URL.fullmatch(text) or len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def parse_jwks(data: object) -> dict[str, RsaKey]:
    """The usable keys of a key set (`{"keys": [...]}`) by key id; anything else in the set is
    skipped (see "Cloudflare Access" in development/design/library-ui.md)."""
    keys: dict[str, RsaKey] = {}
    entries = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return keys
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        kid, raw_n, raw_e = entry.get("kid"), entry.get("n"), entry.get("e")
        if not isinstance(kid, str) or not kid:
            continue
        if not isinstance(raw_n, str) or not isinstance(raw_e, str):
            continue
        n_bytes, e_bytes = decode_segment(raw_n.rstrip("=")), decode_segment(raw_e.rstrip("="))
        if not n_bytes or not e_bytes:
            continue
        n, e = int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")
        if n.bit_length() >= MIN_MODULUS_BITS and e >= 3 and e % 2 == 1:
            keys[kid] = RsaKey(n, e)
    return keys


def rsa_sha256_valid(key: RsaKey, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017, 8.2.2). The expected block is rebuilt and
    compared, never parsed, so padding tricks have nothing to exploit."""
    size = (key.n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= key.n:
        return False
    digest_info = DIGEST_INFO_SHA256 + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest_info) - 3) + b"\x00" + digest_info
    return hmac.compare_digest(pow(value, key.e, key.n).to_bytes(size, "big"), expected)


def _json_object(segment: str) -> dict[str, Any] | None:
    raw = decode_segment(segment)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _time(claims: dict[str, Any], name: str) -> float | None:
    """A claim that is a finite number (a bool is not one); None when it is anything else."""
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:  # an integer too big for a float
        return None


def verify_token(
    token: str,
    keys: Mapping[str, RsaKey],
    *,
    issuer: str,
    audience: str,
    emails: Sequence[str] = (),
    now: float,
) -> Verdict:
    """Whether `token` is a JWT that Access signed with one of `keys`, for this team
    (`issuer`) and this application (`audience`), and is valid at `now` (Unix seconds); with
    `emails`, its `email` claim must be one of them. The checks and their order are in
    development/design/library-ui.md, "Cloudflare Access"."""
    if len(token) > MAX_TOKEN:
        return Verdict(False, "the token is too long")
    parts = token.split(".")
    if len(parts) != 3:
        return Verdict(False, "the token does not have three parts")
    header, claims = _json_object(parts[0]), _json_object(parts[1])
    if header is None or claims is None:
        return Verdict(False, "the token is not well formed")
    if header.get("alg") != ALG:
        return Verdict(False, f"the algorithm is not {ALG}")
    signature = decode_segment(parts[2])
    if not signature:
        return Verdict(False, "the token is not well formed")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return Verdict(False, "the token names no key id")
    key = keys.get(kid)
    if key is None:
        return Verdict(False, UNKNOWN_KID)
    if not rsa_sha256_valid(key, f"{parts[0]}.{parts[1]}".encode("ascii"), signature):
        return Verdict(False, "the signature is not valid")
    return _judge_claims(claims, issuer, audience, emails, now)


def _judge_claims(
    claims: dict[str, Any], issuer: str, audience: str, emails: Sequence[str], now: float
) -> Verdict:
    if claims.get("iss") != issuer:
        return Verdict(False, "the issuer is not this team")
    aud = claims.get("aud")
    accepted = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if audience not in accepted:
        return Verdict(False, "the audience is not this application")
    exp = _time(claims, "exp")
    if exp is None or exp <= now:
        return Verdict(False, "the token has expired or has no expiry")
    for name in ("nbf", "iat"):
        if name in claims:
            stamp = _time(claims, name)
            if stamp is None or stamp > now + LEEWAY:
                return Verdict(False, f"{name} is not valid yet")
    if "type" in claims and claims["type"] != "app":
        return Verdict(False, "the token is not an application token")
    email = claims.get("email")
    email = email if isinstance(email, str) else None
    if emails and (email is None or email.casefold() not in {e.casefold() for e in emails}):
        return Verdict(False, "the e-mail is not allowed")
    return Verdict(True, email=email)

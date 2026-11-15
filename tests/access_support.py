"""Helpers to test Cloudflare Access tokens without the network: a fixed RSA key made for these
tests only (it signs nothing real and Access has never heard of it) and a signer.

The standard library cannot make RSA keys, so the numbers are written down.
"""

import base64
import hashlib
import json
from typing import Any

from warpzone.access_jwt import DIGEST_INFO_SHA256, RsaKey

TEAM = "example.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # gitleaks:allow
KID = "test-key-1"
NOW = 1_800_000_000

# gitleaks:allow (a throwaway test key, not a secret)
TEST_N = int(
    "c83119aaf93ad59617300502f7569b23b938baf8c16977dad24f19ce11854e48"
    "07a0e434ba293bda1af8ba7ec5c89c332e3b40d7d00279cfda3c1ac9e909771d"
    "75ba52e4e4f40042f34efa010fd60b05d90de2180cfe32b9a2660fb137bcbac8"
    "6db4c854d499a8f12ee401bfa827dce6555d80149e1fae92c94fc2a0bfbd2f91"
    "1fa5251fc5a8485a26806d46fc03f8005ad71904a0e1abaf64ea2abc2f915bc7"
    "83d66145f5e5768ed511311f16a70dca8f4d6a3272636dea039f27e13c5950b7"
    "9bf2fa8841679c2940fdccdd4c168e2c7d1a5ffec00957216215611f23d8ffce"
    "d9e64928f44ac7af2ee7978779c80b55cd69d1c42d8b4cca67a52ed93a033cc5",
    16,
)
TEST_D = int(  # gitleaks:allow
    "f8daf089752009fa76009f7a3c75508d6be675a65ce7fab1edd59ec2674d4b2e"
    "872d1e01dd98fc69f83551bfdda5af668005f069568e7d5640778ee587e6d847"
    "aadd5fc662465f3cdab0465fa38bf44fe4ae396ca45a2a3c510086694a5c35e4"
    "895bbddc961c1dc3130324995733968a4d7bd987c7b8a28e9de9a48ba74e4a1c"
    "eb6f4b543c42d4d9cccb14af760b3398798e3d389c9619e669ca5c7c4a774edd"
    "89a29372dc506ce1d0ce91ee55a7f12a210fe4bf501a96102ebbe63161dde963"
    "b7bfdc8880157474dd02e709b0c66b6309bdbfab9cb9bac005f527da2c01fece"
    "ce8dbfd75ea67e4681595383174db808f579e65b849616a115bb4cf00af4d59",
    16,
)
TEST_E = 65537
KEY = RsaKey(TEST_N, TEST_E)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(message: bytes, *, d: int = TEST_D, n: int = TEST_N) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256, the way a signer builds it."""
    size = (n.bit_length() + 7) // 8
    info = DIGEST_INFO_SHA256 + hashlib.sha256(message).digest()
    block = b"\x00\x01" + b"\xff" * (size - len(info) - 3) + b"\x00" + info
    return pow(int.from_bytes(block, "big"), d, n).to_bytes(size, "big")


def claims(**changes: Any) -> dict[str, Any]:
    """A valid Access payload at `NOW`, with some claims changed; a value of None removes one."""
    payload: dict[str, Any] = {
        "aud": [AUD],
        "email": "user@example.com",
        "exp": NOW + 3600,
        "iat": NOW - 10,
        "nbf": NOW - 10,
        "iss": ISSUER,
        "type": "app",
        "sub": "00000000-0000-0000-0000-000000000000",
    }
    payload.update(changes)
    return {k: v for k, v in payload.items() if v is not None}


def make_token(
    payload: dict[str, Any] | None = None,
    header: dict[str, Any] | None = None,
    *,
    signature: bytes | None = None,
) -> str:
    """A compact JWT signed with the test key."""
    head = {"alg": "RS256", "kid": KID, "typ": "JWT"} if header is None else header
    first = b64(json.dumps(head).encode())
    second = b64(json.dumps(claims() if payload is None else payload).encode())
    raw = sign(f"{first}.{second}".encode()) if signature is None else signature
    return f"{first}.{second}.{b64(raw)}"


def jwks(*, kid: str = KID) -> dict[str, Any]:
    """The key set Access publishes, with the test key in it."""
    return {
        "keys": [
            {
                "kid": kid,
                "kty": "RSA",
                "alg": "RS256",
                "use": "sig",
                "e": b64(TEST_E.to_bytes(3, "big")),
                "n": b64(TEST_N.to_bytes((TEST_N.bit_length() + 7) // 8, "big")),
            }
        ],
        "public_cert": {"kid": kid, "cert": "-----BEGIN CERTIFICATE-----"},
    }

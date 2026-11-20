"""Cloudflare Access sign-in for `warpzone serve`: the team's signing keys, fetched and kept,
and the gate that asks `access_jwt` about a request's token (see "Authentication" in
development/design/library-ui.md).

The decisions are in `access_jwt.py`; this module is the part that touches the network.
Whatever goes wrong here, a token is simply not accepted and the caller falls back to the UI
token: a failure never grants access.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from http.client import HTTPMessage
from typing import IO

from warpzone import access_jwt
from warpzone.access_jwt import RsaKey, Verdict

FETCH_TIMEOUT = 5.0  # seconds to wait for the key set
MAX_CERTS_BYTES = 256 * 1024  # a longer answer is not read
REFRESH_SECONDS = 3600.0  # the key set is fetched again after this long
RETRY_SECONDS = 60.0  # at most one fetch per this long, whatever asks for it
MAX_STALE_SECONDS = 24 * 3600.0  # keys older than this, with no fetch since, are not used

Fetch = Callable[[str], object]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The key set is read from one `https` address only; a redirect is a failure."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def fetch_certs(team_domain: str) -> object:
    """The team's key set, parsed from JSON. Raises on any failure."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        access_jwt.certs_url(team_domain),
        headers={"Accept": "application/json", "User-Agent": "warpzone-serve"},
    )
    with opener.open(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read(MAX_CERTS_BYTES + 1)
    if len(raw) > MAX_CERTS_BYTES:
        raise ValueError("the key set is too large")
    return json.loads(raw)


def _reason(exc: BaseException) -> str:
    """What to log about a failed fetch: the kind of failure, never a message that could
    carry more."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"cannot connect ({type(exc.reason).__name__})"
    return type(exc).__name__


class AccessGate:
    """Whether a request's `Cf-Access-Jwt-Assertion` is a valid Access token for this team and
    application. When the key set is fetched, and how a failure is logged, is in
    development/design/library-ui.md, "Cloudflare Access" ("Keys of the team")."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        emails: Sequence[str] = (),
        *,
        fetch: Fetch | None = None,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.team_domain = team_domain
        self._issuer = access_jwt.issuer(team_domain)
        self._audience = audience
        self._emails = tuple(emails)
        self._fetch = fetch or fetch_certs
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._keys: dict[str, RsaKey] = {}
        self._fetched: float | None = None  # when the keys were last fetched
        self._attempted: float | None = None  # when a fetch was last tried
        self._failing = False
        self._refused: set[str] = set()  # the reasons logged since the last token accepted

    def check(self, token: str, now: float) -> Verdict:
        """The verdict on `token` at `now` (Unix seconds). Never raises."""
        try:
            verdict = self._verify(token, self._current_keys(), now)
            if verdict.reason == access_jwt.UNKNOWN_KID and self._refresh_for_unknown_key():
                verdict = self._verify(token, self._current_keys(), now)
            self._note(verdict)
            return verdict
        except Exception as exc:  # a bug here must not become a 500 or an acceptance
            self._log(f"access: internal error: {type(exc).__name__}")
            return Verdict(False, "internal error")

    def _note(self, verdict: Verdict) -> None:
        """Log why a token was refused, once per reason until a token is accepted. The reason
        never holds the token."""
        with self._lock:
            if verdict.ok:
                self._refused.clear()
                return
            if verdict.reason in self._refused:
                return
            self._refused.add(verdict.reason)
        self._log(f"access: a token was refused: {verdict.reason}")

    def _verify(self, token: str, keys: dict[str, RsaKey], now: float) -> Verdict:
        return access_jwt.verify_token(
            token,
            keys,
            issuer=self._issuer,
            audience=self._audience,
            emails=self._emails,
            now=now,
        )

    def _current_keys(self) -> dict[str, RsaKey]:
        with self._lock:
            now = self._clock()
            if self._fetched is None or now - self._fetched >= REFRESH_SECONDS:
                self._fetch_locked(now)
            if self._fetched is None or now - self._fetched > MAX_STALE_SECONDS:
                return {}
            return self._keys

    def _refresh_for_unknown_key(self) -> bool:
        with self._lock:
            return self._fetch_locked(self._clock())

    def _fetch_locked(self, now: float) -> bool:
        """Fetch the keys unless one was tried less than a minute ago; whether new keys came."""
        if self._attempted is not None and now - self._attempted < RETRY_SECONDS:
            return False
        self._attempted = now
        try:
            keys = access_jwt.parse_jwks(self._fetch(self.team_domain))
            if not keys:
                raise ValueError("the key set has no usable key")
        except Exception as exc:
            if not self._failing:
                self._failing = True
                self._log(
                    f"access: cannot fetch the signing keys of {self.team_domain}: "
                    f"{_reason(exc)}; the UI token still works"
                )
            return False
        if self._failing:
            self._failing = False
            self._log(f"access: the signing keys of {self.team_domain} were fetched again")
        self._keys, self._fetched = keys, now
        return True

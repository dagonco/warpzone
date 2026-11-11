"""Minimal Syncthing REST client (urllib), with secret redaction and dry-run."""

from __future__ import annotations

import functools
import hashlib
import hmac
import http.client
import ipaddress
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from warpzone.errors import WarpzoneError

REDACTED = "***"
_SECRET_KEYS = {"apikey", "api_key", "x-api-key", "password", "encryptionpassword", "token"}


class SyncthingError(WarpzoneError):
    def __init__(self, message: str, status: int | None = None, timed_out: bool = False) -> None:
        super().__init__(message)
        self.status = status
        # Syncthing took the request but did not answer in time (not: could not connect).
        self.timed_out = timed_out


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Copy of `value` with secret-looking keys and any known secret string masked."""
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and k.lower() in _SECRET_KEYS else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        return scrub(value, secrets)
    return value


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class CertificateMismatchError(OSError):
    """The TLS peer's certificate is not the pinned one; nothing was sent to it."""

    def __init__(self, presented: str | None) -> None:
        super().__init__("the certificate is not the pinned one")
        self.presented = presented  # SHA-256 of what the peer sent as hex, None if nothing


def cert_sha256_matches(der: bytes | None, pin: bytes) -> bool:
    """Whether the SHA-256 of the DER certificate is `pin` (32 bytes). No certificate: no."""
    return der is not None and hmac.compare_digest(hashlib.sha256(der).digest(), pin)


def format_fingerprint(digest_hex: str) -> str:
    """'abcd..' as 'AB:CD:..', the form openssl prints and browsers show."""
    upper = digest_hex.upper()
    return ":".join(upper[i : i + 2] for i in range(0, len(upper), 2))


def mismatch_message(label: str, base_url: str, presented: str | None, expected: str) -> str:
    """Why a pinned connection was refused. Holds only public values: no API key."""
    wanted = format_fingerprint(expected)
    if presented is None:
        got = f"It presented no certificate. The config expects {wanted}."
    else:
        got = f"It presented SHA-256 {format_fingerprint(presented)}; the config expects {wanted}."
    return (
        f"{label}: the certificate of {base_url} is not the one in hub.api_cert_sha256, so "
        f"nothing was sent to it. {got} "
        "If Syncthing renewed its GUI certificate (it does so about every 790 days), read the "
        "new fingerprint on the hub with: openssl x509 -in <Syncthing config folder>/"
        "https-cert.pem -noout -fingerprint -sha256"
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection that checks the peer's certificate against a pin before use.

    `http.client` buffers the request line and headers and writes to the socket only in
    `send()`, which calls `connect()` first; raising here means no request byte is written.
    """

    def __init__(self, *args: Any, pin: bytes, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pin = pin

    def connect(self) -> None:
        super().connect()  # TCP and the TLS handshake; nothing is written yet
        sock = self.sock
        # The DER form is available even though the context does not verify the chain.
        der = sock.getpeercert(binary_form=True) if isinstance(sock, ssl.SSLSocket) else None
        if not cert_sha256_matches(der, self._pin):
            presented = None if der is None else hashlib.sha256(der).hexdigest()
            sock.close()
            self.sock = None
            raise CertificateMismatchError(presented)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, pin: bytes) -> None:
        super().__init__(context=context)
        self._pin = pin
        self._pinned_context = context

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        connection = functools.partial(_PinnedHTTPSConnection, pin=self._pin)
        return self.do_open(connection, req, context=self._pinned_context)


def _pinned_opener(pin_hex: str) -> urllib.request.OpenerDirector:
    """An opener that speaks only TLS to a server whose certificate hashes to `pin_hex`.

    No redirect handler (a 3xx is an error), no proxy handler (it connects directly) and no
    plain-HTTP handler (it cannot be downgraded).
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # the pin replaces name checking
    # Safe only because _PinnedHTTPSConnection compares the pin on every connection this
    # context makes; it is used nowhere else.
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    opener = urllib.request.OpenerDirector()
    for handler in (
        _PinnedHTTPSHandler(context, bytes.fromhex(pin_hex)),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


@dataclass(frozen=True)
class Call:
    method: str
    path: str
    body: Any = None


class Client:
    """One Syncthing instance. In dry-run mode only GET requests are sent."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        label: str = "syncthing",
        dry_run: bool = False,
        timeout: float = 30.0,
        sink: Callable[[str], None] = print,
        cert_sha256: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.label = label
        self.dry_run = dry_run
        self.timeout = timeout
        self._sink = sink
        self.recorded: list[Call] = []
        parsed = urllib.parse.urlsplit(self.base_url)
        self._context: ssl.SSLContext | None = None
        # With a pin: TLS to the one certificate with that SHA-256, whatever the host.
        self._pin = cert_sha256
        self._opener: urllib.request.OpenerDirector | None = None
        if cert_sha256 is not None:
            if parsed.scheme != "https":
                raise SyncthingError(f"{label}: a pinned certificate needs an https:// URL")
            self._opener = _pinned_opener(cert_sha256)
        elif parsed.scheme == "https" and _is_loopback(parsed.hostname or ""):
            # Syncthing's own certificate is self-signed; trust it only on loopback.
            self._context = ssl.create_default_context()
            self._context.check_hostname = False
            self._context.verify_mode = ssl.CERT_NONE

    def __repr__(self) -> str:
        return f"Client({self.base_url!r}, label={self.label!r})"

    def request(self, method: str, path: str, body: Any = None, **params: str) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        if self.dry_run and method != "GET":
            call = Call(method, path + query, body)
            self.recorded.append(call)
            shown = "" if body is None else " " + json.dumps(redact(body, (self._api_key,)))
            self._sink(f"[dry-run] {self.label}: {method} {call.path}{shown}")
            return None

        data = None if body is None else json.dumps(body).encode()
        headers = {"X-API-Key": self._api_key}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path + query, data=data, headers=headers, method=method
        )
        secrets = (self._api_key,)
        try:
            if self._opener is not None:
                opened = self._opener.open(req, timeout=self.timeout)
            else:
                opened = urllib.request.urlopen(req, timeout=self.timeout, context=self._context)
            with opened as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = scrub(exc.read(300).decode(errors="replace").strip(), secrets)
            exc.close()
            raise SyncthingError(
                f"{self.label}: {method} {path}: HTTP {exc.code} {detail}".rstrip(), exc.code
            ) from None
        except TimeoutError:
            # Raised bare only while waiting for the reply; a connect timeout is a URLError.
            raise SyncthingError(
                f"{self.label}: {method} {path}: no answer from {self.base_url} "
                f"within {self.timeout:.0f}s",
                timed_out=True,
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            cause = getattr(exc, "reason", exc)
            if isinstance(cause, CertificateMismatchError) and self._pin is not None:
                text = mismatch_message(self.label, self.base_url, cause.presented, self._pin)
                raise SyncthingError(scrub(text, secrets)) from None
            reason = scrub(str(getattr(exc, "reason", exc)), secrets)
            raise SyncthingError(f"{self.label}: cannot reach {self.base_url} ({reason})") from None
        except http.client.HTTPException as exc:
            # A reply cut short or not HTTP at all (IncompleteRead, BadStatusLine, ...).
            reason = scrub(f"{type(exc).__name__}: {exc}", secrets)
            raise SyncthingError(
                f"{self.label}: {method} {path}: the reply from {self.base_url} broke ({reason})"
            ) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def get(self, path: str, **params: str) -> Any:
        return self.request("GET", path, None, **params)

    # Typed helpers: one line per REST endpoint warpzone uses.

    def my_id(self) -> str:
        return str(self.get("/rest/system/status")["myID"])

    def devices(self) -> list[dict[str, Any]]:
        return self.get("/rest/config/devices") or []

    def add_device(self, device: dict[str, Any]) -> None:
        self.request("POST", "/rest/config/devices", device)

    def folders(self) -> list[dict[str, Any]]:
        return self.get("/rest/config/folders") or []

    def add_folder(self, folder: dict[str, Any]) -> None:
        self.request("POST", "/rest/config/folders", folder)

    def remove_folder(self, folder_id: str) -> None:
        """Remove a folder from Syncthing's configuration. Its files stay on disk."""
        self.request("DELETE", f"/rest/config/folders/{urllib.parse.quote(folder_id)}")

    def patch_folder(self, folder_id: str, patch: dict[str, Any]) -> None:
        self.request("PATCH", f"/rest/config/folders/{urllib.parse.quote(folder_id)}", patch)

    def get_ignores(self, folder_id: str) -> list[str]:
        return list((self.get("/rest/db/ignores", folder=folder_id) or {}).get("ignore") or [])

    def ignores_state(self, folder_id: str) -> dict[str, Any]:
        """The raw `db/ignores` answer: `ignore`, `expanded` and `error` (load failure)."""
        return self.get("/rest/db/ignores", folder=folder_id) or {}

    def set_ignores(self, folder_id: str, lines: list[str]) -> None:
        self.request("POST", "/rest/db/ignores", {"ignore": lines}, folder=folder_id)

    def db_status(self, folder_id: str) -> dict[str, Any]:
        return self.get("/rest/db/status", folder=folder_id) or {}

    def db_file(self, folder_id: str, name: str) -> dict[str, Any] | None:
        """File info, or None if Syncthing has no such object in its index."""
        try:
            info = self.get("/rest/db/file", folder=folder_id, file=name)
        except SyncthingError as exc:
            if exc.status == 404:
                return None
            raise
        return info if isinstance(info, dict) else None

    def connections(self) -> dict[str, dict[str, Any]]:
        """`GET /rest/system/connections`: the connection of each remote device, by device ID."""
        answer = self.get("/rest/system/connections")
        found = answer.get("connections") if isinstance(answer, dict) else None
        return found if isinstance(found, dict) else {}

    def completion(self, folder_id: str, device_id: str) -> dict[str, Any]:
        """`GET /rest/db/completion`: how much of the folder `device_id` has (`completion`,
        `needBytes`, `needItems`, `remoteState`)."""
        answer = self.get("/rest/db/completion", folder=folder_id, device=device_id)
        return answer if isinstance(answer, dict) else {}

    def browse(self, folder_id: str) -> list[str]:
        """Names at the top level of a folder."""
        entries = self.get("/rest/db/browse", folder=folder_id, levels="0") or []
        return [e["name"] for e in entries]

    def scan(self, folder_id: str, sub: str | None = None) -> bool:
        """Scan the folder, or only `sub` inside it. True once the scan ran.

        Syncthing answers when the scan is done, and a folder that is pulling runs it only
        after the pull, which can take longer than the client's timeout. A full scan that
        does not answer in time stays queued in Syncthing and runs then: it returns False.
        A scan of `sub` raises instead, since its caller reads the result right after.
        """
        params = {"folder": folder_id} if sub is None else {"folder": folder_id, "sub": sub}
        try:
            self.request("POST", "/rest/db/scan", None, **params)
        except SyncthingError as exc:
            if sub is None and exc.timed_out:
                return False
            raise
        return True

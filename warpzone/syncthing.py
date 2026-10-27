"""Minimal Syncthing REST client (urllib), with secret redaction and dry-run."""

from __future__ import annotations

import ipaddress
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

REDACTED = "***"
_SECRET_KEYS = {"apikey", "api_key", "x-api-key", "password", "encryptionpassword", "token"}


class SyncthingError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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
        if parsed.scheme == "https" and _is_loopback(parsed.hostname or ""):
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
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._context) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = scrub(exc.read(300).decode(errors="replace").strip(), secrets)
            exc.close()
            raise SyncthingError(
                f"{self.label}: {method} {path}: HTTP {exc.code} {detail}".rstrip(), exc.code
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = scrub(str(getattr(exc, "reason", exc)), secrets)
            raise SyncthingError(f"{self.label}: cannot reach {self.base_url} ({reason})") from None
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

    def browse(self, folder_id: str) -> list[str]:
        """Names at the top level of a folder."""
        entries = self.get("/rest/db/browse", folder=folder_id, levels="0") or []
        return [e["name"] for e in entries]

    def scan(self, folder_id: str) -> None:
        self.request("POST", "/rest/db/scan", None, folder=folder_id)

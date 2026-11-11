"""Pinning the hub's certificate against real TLS servers on loopback.

The certificate is generated here with the `openssl` binary, so no key is checked in.
"""

import hashlib
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import API_KEY, HUB_ID, FakeSyncthing
from warpzone.config import normalize_cert_sha256
from warpzone.syncthing import Client, SyncthingError, format_fingerprint

_dir: tempfile.TemporaryDirectory[str] | None = None
CERT = KEY = ""
DER_SHA256 = ""  # hex, computed from the certificate, never written as a literal


def setUpModule() -> None:
    global _dir, CERT, KEY, DER_SHA256
    if shutil.which("openssl") is None:
        if os.environ.get("CI"):
            raise RuntimeError("openssl is needed for the certificate pin tests and CI has none")
        raise unittest.SkipTest("the openssl binary is not installed")
    _dir = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(_dir.cleanup)
    CERT, KEY = str(Path(_dir.name) / "cert.pem"), str(Path(_dir.name) / "key.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            KEY,
            "-out",
            CERT,
            "-days",
            "2",
            "-subj",
            "/CN=warpzone-test",
        ],
        check=True,
        capture_output=True,
    )
    der = ssl.PEM_cert_to_DER_cert(Path(CERT).read_text())
    DER_SHA256 = hashlib.sha256(der).hexdigest()


def server_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    return ctx


class Recorder:
    """A raw server that records every byte a client sends and answers `reply`, if any.

    With a context it speaks TLS (handshake included in what it tolerates failing); without
    one it is plain TCP. `handled` has one entry per connection, the bytes received on it,
    appended once the connection is over.
    """

    def __init__(self, tls: ssl.SSLContext | None = None, reply: bytes | None = None) -> None:
        self._tls = tls
        self._reply = reply
        self.handled: list[bytes] = []
        self._stop = threading.Event()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.05)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"{'https' if self._tls else 'http'}://127.0.0.1:{self.port}"

    def __enter__(self) -> "Recorder":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()

    def wait_for(self, connections: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.handled) < connections and time.monotonic() < deadline:
            time.sleep(0.01)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.handled.append(self._read(conn))

    def _read(self, conn: socket.socket) -> bytes:
        data = b""
        try:
            conn.settimeout(2.0)
            if self._tls is not None:
                conn = self._tls.wrap_socket(conn, server_side=True)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if self._reply is not None and b"\r\n\r\n" in data:
                    conn.sendall(self._reply)
                    break
        except (OSError, ssl.SSLError):  # no handshake, a reset, or the 2 s deadline
            pass
        finally:
            conn.close()
        return data


class PinnedClientTests(unittest.TestCase):
    def test_the_right_pin_connects_and_sends_the_key(self) -> None:
        with FakeSyncthing(HUB_ID, tls=server_context()) as fake:
            self.assertTrue(fake.url.startswith("https://127.0.0.1:"))
            client = Client(fake.url, API_KEY, label="hub", cert_sha256=DER_SHA256)
            # The fake answers 403 to a request without the right X-API-Key.
            self.assertEqual(client.my_id(), HUB_ID)
            self.assertEqual(fake.requests, [("GET", "/rest/system/status", None)])

    def test_the_right_pin_works_on_a_host_that_is_not_loopback(self) -> None:
        with (
            FakeSyncthing(HUB_ID, tls=server_context()) as fake,
            mock.patch("warpzone.syncthing._is_loopback", return_value=False),
        ):
            client = Client(fake.url, API_KEY, cert_sha256=DER_SHA256)
            self.assertEqual(client.my_id(), HUB_ID)

    def test_a_wrong_pin_sends_not_one_byte(self) -> None:
        with Recorder(server_context()) as server:
            client = Client(server.url, API_KEY, label="hub", cert_sha256="00" * 32)
            with self.assertRaises(SyncthingError) as ctx:
                client.my_id()
            server.wait_for(1)
        text = str(ctx.exception)
        self.assertIsNone(ctx.exception.status)
        self.assertIn(format_fingerprint(DER_SHA256), text)
        self.assertIn(format_fingerprint("00" * 32), text)
        self.assertIn("nothing was sent", text)
        self.assertNotIn(API_KEY, text)
        # The TLS handshake happened (one connection) and then nothing came: no request
        # line, no header, no X-API-Key.
        self.assertEqual(server.handled, [b""])

    def test_a_wrong_pin_on_a_write_sends_not_one_byte_either(self) -> None:
        with Recorder(server_context()) as server:
            client = Client(server.url, API_KEY, cert_sha256="00" * 32)
            with self.assertRaises(SyncthingError):
                client.request("POST", "/rest/config/folders", {"id": "x"})
            server.wait_for(1)
        self.assertEqual(server.handled, [b""])

    def test_a_server_with_no_tls_is_not_sent_the_key_either(self) -> None:
        with Recorder() as server:  # plain TCP answering nothing
            client = Client(server.url.replace("http:", "https:"), API_KEY, cert_sha256="ab" * 32)
            with self.assertRaises(SyncthingError):
                client.my_id()
            server.wait_for(1)
        self.assertNotIn(API_KEY.encode(), b"".join(server.handled))

    def test_without_a_pin_a_host_that_is_not_loopback_gets_default_verification(self) -> None:
        with Recorder(server_context()) as server:
            with mock.patch("warpzone.syncthing._is_loopback", return_value=False):
                client = Client(server.url, API_KEY, label="hub")
            with self.assertRaises(SyncthingError) as ctx:
                client.my_id()
            server.wait_for(1)
        self.assertIn("cannot reach", str(ctx.exception))
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(ctx.exception))
        self.assertNotIn(API_KEY, str(ctx.exception))
        self.assertEqual(server.handled, [b""])

    def test_without_a_pin_loopback_keeps_trusting_the_certificate(self) -> None:
        with FakeSyncthing(HUB_ID, tls=server_context()) as fake:
            self.assertEqual(Client(fake.url, API_KEY).my_id(), HUB_ID)

    def test_a_redirect_is_not_followed(self) -> None:
        with Recorder() as target:
            reply = (
                b"HTTP/1.1 307 Temporary Redirect\r\n"
                b"Location: http://127.0.0.1:%d/rest/system/status\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n" % target.port
            )
            with Recorder(server_context(), reply=reply) as hub:
                client = Client(hub.url, API_KEY, label="hub", cert_sha256=DER_SHA256)
                with self.assertRaises(SyncthingError) as ctx:
                    client.my_id()
                hub.wait_for(1)
            time.sleep(0.3)  # long enough for the target's accept loop to see a connection
            self.assertEqual(target.handled, [])
        self.assertEqual(ctx.exception.status, 307)
        self.assertIn(API_KEY.encode(), hub.handled[0])  # only the pinned hub got the key
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_a_proxy_in_the_environment_is_not_used(self) -> None:
        with Recorder() as proxy, FakeSyncthing(HUB_ID, tls=server_context()) as fake:
            env = {
                "HTTPS_PROXY": proxy.url,
                "https_proxy": proxy.url,
                "HTTP_PROXY": proxy.url,
                "http_proxy": proxy.url,
                "NO_PROXY": "",
                "no_proxy": "",
            }
            with mock.patch.dict(os.environ, env):
                client = Client(fake.url, API_KEY, cert_sha256=DER_SHA256)
                self.assertEqual(client.my_id(), HUB_ID)
            time.sleep(0.3)
            self.assertEqual(proxy.handled, [])


class FingerprintCommandTests(unittest.TestCase):
    def test_the_openssl_command_in_the_docs_gives_a_value_the_config_accepts(self) -> None:
        out = subprocess.run(
            ["openssl", "x509", "-in", CERT, "-noout", "-fingerprint", "-sha256"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        printed = out.strip().partition("=")[2]
        self.assertEqual(normalize_cert_sha256(printed), DER_SHA256)


if __name__ == "__main__":
    unittest.main()

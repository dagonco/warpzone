"""The key cache and the gate of Cloudflare Access, with a fake fetch and a fake clock."""

import unittest
import urllib.error
from typing import Any

import access_support as sup
from warpzone import access_jwt, access_keys
from warpzone.access_keys import AccessGate


class Fetcher:
    """A stand-in for the network: answers with `answer`, or raises it when it is an exception."""

    def __init__(self, answer: object = None) -> None:
        self.answer: Any = sup.jwks() if answer is None else answer
        self.calls: list[str] = []

    def __call__(self, team: str) -> object:
        self.calls.append(team)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fetch = Fetcher()
        self.time = 1000.0
        self.logs: list[str] = []

    def gate(self, emails: tuple[str, ...] = ()) -> AccessGate:
        return AccessGate(
            sup.TEAM,
            sup.AUD,
            emails,
            fetch=self.fetch,
            clock=lambda: self.time,
            log=self.logs.append,
        )

    def check(self, gate: AccessGate, token: str | None = None) -> bool:
        return gate.check(token or sup.make_token(), sup.NOW).ok


class FetchTests(GateCase):
    def test_the_keys_are_fetched_once_and_kept(self) -> None:
        gate = self.gate()
        self.assertTrue(self.check(gate))
        self.time += 10
        self.assertTrue(self.check(gate))
        self.assertEqual(self.fetch.calls, [sup.TEAM])

    def test_the_keys_are_fetched_again_after_an_hour(self) -> None:
        gate = self.gate()
        self.check(gate)
        self.time += access_keys.REFRESH_SECONDS - 1
        self.check(gate)
        self.assertEqual(len(self.fetch.calls), 1)
        self.time += 2
        self.assertTrue(self.check(gate))
        self.assertEqual(len(self.fetch.calls), 2)

    def test_an_unknown_kid_fetches_again_at_most_once_a_minute(self) -> None:
        gate = self.gate()
        self.check(gate)
        stranger = sup.make_token(header={"alg": "RS256", "kid": "rotated"})
        self.time += 5
        self.assertFalse(self.check(gate, stranger))  # the first fetch was 5 s ago: no refetch
        self.assertEqual(len(self.fetch.calls), 1)
        self.time += access_keys.RETRY_SECONDS
        self.assertFalse(self.check(gate, stranger))
        self.assertEqual(len(self.fetch.calls), 2)
        self.time += 1
        self.assertFalse(self.check(gate, stranger))
        self.assertEqual(len(self.fetch.calls), 2)

    def test_a_rotated_key_is_picked_up_by_the_refetch(self) -> None:
        self.fetch.answer = sup.jwks(kid="old")
        gate = self.gate()
        self.assertFalse(self.check(gate))  # kid "test-key-1" is not there yet
        self.fetch.answer = sup.jwks()
        self.time += access_keys.RETRY_SECONDS
        self.assertTrue(self.check(gate))

    def test_a_failed_fetch_grants_nothing_and_is_logged_once_per_streak(self) -> None:
        def fetch_logs() -> list[str]:
            return [line for line in self.logs if "signing keys" in line]

        self.fetch.answer = urllib.error.URLError(OSError("no route"))
        gate = self.gate()
        for _ in range(4):
            self.assertFalse(self.check(gate))
            self.time += access_keys.RETRY_SECONDS
        self.assertEqual(len(self.fetch.calls), 4)
        self.assertEqual(len(fetch_logs()), 1)
        self.assertIn("cannot fetch the signing keys", fetch_logs()[0])
        self.assertNotIn("no route", fetch_logs()[0])
        self.fetch.answer = sup.jwks()
        self.assertTrue(self.check(gate))
        self.assertIn("fetched again", fetch_logs()[-1])
        self.fetch.answer = ValueError("broken")
        self.time += access_keys.REFRESH_SECONDS
        self.assertTrue(self.check(gate))  # the old keys serve while the fetch fails
        self.assertEqual(len(fetch_logs()), 3)

    def test_an_answer_with_no_usable_key_is_a_failure(self) -> None:
        for answer in ({"keys": []}, "text", None, {"keys": [{"kid": "x", "kty": "RSA"}]}):
            with self.subTest(answer=answer):
                self.fetch = Fetcher(answer if answer is not None else {})
                gate = self.gate()
                self.assertFalse(self.check(gate))

    def test_a_http_error_is_logged_by_its_status_only(self) -> None:
        self.fetch.answer = urllib.error.HTTPError("https://x/y?secret=1", 503, "Bad", None, None)  # type: ignore[arg-type]
        gate = self.gate()
        self.assertFalse(self.check(gate))
        self.assertIn("HTTP 503", self.logs[0])
        self.assertNotIn("secret", self.logs[0])

    def test_old_keys_are_not_used_after_a_day_without_a_fetch(self) -> None:
        gate = self.gate()
        self.assertTrue(self.check(gate))
        self.fetch.answer = OSError("down")
        self.time += access_keys.MAX_STALE_SECONDS
        self.assertTrue(self.check(gate))
        self.time += 1
        self.assertFalse(self.check(gate))

    def test_a_token_is_never_in_the_log(self) -> None:
        self.fetch.answer = OSError("down")
        token = sup.make_token()
        self.gate().check(token, sup.NOW)
        self.assertNotIn(token, "\n".join(self.logs))


class VerdictTests(GateCase):
    def test_the_gate_applies_the_issuer_audience_and_emails_it_was_given(self) -> None:
        self.assertTrue(self.check(self.gate(("user@example.com",))))
        self.assertFalse(self.check(self.gate(("other@example.com",))))
        self.assertFalse(self.check(self.gate(), sup.make_token(sup.claims(aud=["nope"]))))
        self.assertFalse(self.check(self.gate(), sup.make_token(sup.claims(exp=sup.NOW - 5))))

    def test_a_refusal_is_logged_once_per_reason_until_a_token_is_accepted(self) -> None:
        gate = self.gate()
        wrong_aud = sup.make_token(sup.claims(aud=["nope"]))
        expired = sup.make_token(sup.claims(exp=sup.NOW - 5))
        for token in (wrong_aud, wrong_aud, expired, wrong_aud, expired):
            self.assertFalse(self.check(gate, token))
        refusals = [line for line in self.logs if "was refused" in line]
        self.assertEqual(len(refusals), 2)
        self.assertIn("audience", refusals[0])
        self.assertIn("expired", refusals[1])
        self.assertTrue(self.check(gate))  # an accepted token ends the streak
        self.assertFalse(self.check(gate, wrong_aud))
        self.assertEqual(len([line for line in self.logs if "was refused" in line]), 3)
        for token in (wrong_aud, expired):
            self.assertNotIn(token, "\n".join(self.logs))

    def test_it_does_not_raise_on_garbage(self) -> None:
        gate = self.gate()
        for token in ("", "x", "a.b.c", "\x00" * 10):
            self.assertFalse(gate.check(token, sup.NOW).ok)

    def test_an_unexpected_failure_is_not_an_acceptance(self) -> None:
        gate = self.gate()

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("bug")

        gate._verify = boom  # type: ignore[method-assign]
        self.assertFalse(gate.check(sup.make_token(), sup.NOW).ok)
        self.assertIn("internal error", self.logs[-1])


class UrlTests(unittest.TestCase):
    def test_the_fetch_goes_to_the_https_certs_address_of_the_team(self) -> None:
        self.assertEqual(
            access_jwt.certs_url(sup.TEAM),
            "https://example.cloudflareaccess.com/cdn-cgi/access/certs",
        )

    def test_a_redirect_is_not_followed(self) -> None:
        handler = access_keys._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", None, "http://elsewhere/")  # type: ignore[arg-type]
        )


if __name__ == "__main__":
    unittest.main()

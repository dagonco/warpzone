"""Cloudflare Access tokens judged by the pure part, with no network (access_support.py signs
them with a fixed test key)."""

import hashlib
import hmac
import json
import unittest
from typing import Any

import access_support as sup
from warpzone import access_jwt
from warpzone.access_jwt import RsaKey, Verdict, verify_token

KEYS = {sup.KID: sup.KEY}


def verify(token: str, **options: Any) -> Verdict:
    settings: dict[str, Any] = {"issuer": sup.ISSUER, "audience": sup.AUD, "now": sup.NOW}
    return verify_token(token, KEYS, **{**settings, **options})


class ValidTokenTests(unittest.TestCase):
    def test_a_token_signed_with_a_known_key_is_accepted(self) -> None:
        verdict = verify(sup.make_token())
        self.assertEqual(verdict, Verdict(True, "", "user@example.com"))

    def test_the_audience_may_be_a_string_or_a_list_that_holds_the_tag(self) -> None:
        for aud in (sup.AUD, [sup.AUD], ["other", sup.AUD]):
            with self.subTest(aud=aud):
                self.assertTrue(verify(sup.make_token(sup.claims(aud=aud))).ok)

    def test_the_email_claim_is_optional_without_a_list_of_emails(self) -> None:
        verdict = verify(sup.make_token(sup.claims(email=None)))
        self.assertEqual(verdict, Verdict(True, "", None))

    def test_nbf_and_iat_a_little_ahead_are_within_the_leeway(self) -> None:
        ahead = sup.NOW + access_jwt.LEEWAY
        self.assertTrue(verify(sup.make_token(sup.claims(nbf=ahead, iat=ahead))).ok)

    def test_nbf_and_iat_are_optional(self) -> None:
        self.assertTrue(verify(sup.make_token(sup.claims(nbf=None, iat=None))).ok)


class RefusedTokenTests(unittest.TestCase):
    def refused(self, token: str, reason: str, **options: Any) -> None:
        verdict = verify(token, **options)
        self.assertFalse(verdict.ok)
        self.assertIn(reason, verdict.reason)
        self.assertNotIn(token, verdict.reason)

    def test_a_bad_signature(self) -> None:
        wrong = sup.sign(b"something else")
        self.refused(sup.make_token(signature=wrong), "signature")

    def test_a_signature_of_another_length_or_over_the_modulus(self) -> None:
        self.refused(sup.make_token(signature=b"\x01" * 10), "signature")
        too_big = sup.TEST_N.to_bytes(256, "big")
        self.refused(sup.make_token(signature=too_big), "signature")

    def test_a_tampered_payload(self) -> None:
        head, _, sig = sup.make_token().split(".")
        forged = sup.b64(json.dumps(sup.claims(email="intruder@example.com")).encode())
        self.refused(f"{head}.{forged}.{sig}", "signature")

    def test_a_tampered_header(self) -> None:
        _, payload, sig = sup.make_token().split(".")
        head = sup.b64(json.dumps({"alg": "RS256", "kid": sup.KID, "x": 1}).encode())
        self.refused(f"{head}.{payload}.{sig}", "signature")

    def test_the_none_algorithm(self) -> None:
        head = sup.b64(json.dumps({"alg": "none", "kid": sup.KID}).encode())
        body = sup.b64(json.dumps(sup.claims()).encode())
        for token in (f"{head}.{body}.", f"{head}.{body}.{sup.b64(b'x')}"):
            self.refused(token, "algorithm")

    def test_the_hmac_algorithms_and_others(self) -> None:
        for alg in ("HS256", "HS512", "RS512", "ES256", "rs256", "", None, 5):
            with self.subTest(alg=alg):
                token = sup.make_token(header={"alg": alg, "kid": sup.KID})
                self.refused(token, "algorithm")

    def test_hs256_signed_with_the_public_key_as_the_secret_is_refused(self) -> None:
        secret = sup.TEST_N.to_bytes(256, "big")
        head = sup.b64(json.dumps({"alg": "HS256", "kid": sup.KID}).encode())
        body = sup.b64(json.dumps(sup.claims()).encode())
        mac = hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest()
        self.refused(f"{head}.{body}.{sup.b64(mac)}", "algorithm")

    def test_no_kid_and_an_unknown_kid(self) -> None:
        self.refused(sup.make_token(header={"alg": "RS256"}), "no key id")
        self.refused(sup.make_token(header={"alg": "RS256", "kid": 7}), "no key id")
        verdict = verify(sup.make_token(header={"alg": "RS256", "kid": "other"}))
        self.assertEqual(verdict.reason, access_jwt.UNKNOWN_KID)

    def test_an_expired_token_and_one_with_no_expiry(self) -> None:
        self.refused(sup.make_token(sup.claims(exp=sup.NOW)), "expired")
        self.refused(sup.make_token(sup.claims(exp=sup.NOW - 1)), "expired")
        self.refused(sup.make_token(sup.claims(exp=None)), "expired")
        self.refused(sup.make_token(sup.claims(exp="9999999999")), "expired")
        self.refused(sup.make_token(sup.claims(exp=True)), "expired")
        self.refused(sup.make_token(sup.claims(exp=float("inf"))), "expired")

    def test_a_number_too_big_for_a_float_is_refused_not_raised(self) -> None:
        self.refused(sup.make_token(sup.claims(exp=10**400)), "expired")
        self.refused(sup.make_token(sup.claims(nbf=10**400)), "nbf")
        self.refused(sup.make_token(sup.claims(iat=-(10**400))), "iat")

    def test_nbf_or_iat_in_the_future(self) -> None:
        late = sup.NOW + access_jwt.LEEWAY + 1
        self.refused(sup.make_token(sup.claims(nbf=late)), "nbf")
        self.refused(sup.make_token(sup.claims(iat=late)), "iat")
        self.refused(sup.make_token(sup.claims(nbf="soon")), "nbf")

    def test_a_wrong_audience(self) -> None:
        for aud in ("another-application-tag", ["another"], [], None, 5, {"a": sup.AUD}):
            with self.subTest(aud=aud):
                self.refused(sup.make_token(sup.claims(aud=aud)), "audience")

    def test_a_wrong_issuer(self) -> None:
        for iss in ("https://other.cloudflareaccess.com", sup.TEAM, None, 5):
            with self.subTest(iss=iss):
                self.refused(sup.make_token(sup.claims(iss=iss)), "issuer")

    def test_a_token_that_is_not_an_application_token(self) -> None:
        self.refused(sup.make_token(sup.claims(type="org")), "application token")

    def test_an_email_that_is_not_in_the_list(self) -> None:
        allowed = ["Other@Example.com"]
        self.refused(sup.make_token(), "e-mail", emails=allowed)
        self.refused(sup.make_token(sup.claims(email=None)), "e-mail", emails=allowed)
        self.refused(sup.make_token(sup.claims(email=5)), "e-mail", emails=allowed)

    def test_the_email_list_matches_in_any_case(self) -> None:
        token = sup.make_token(sup.claims(email="User@EXAMPLE.com"))
        self.assertTrue(verify(token, emails=["USER@example.com"]).ok)


class MalformedTokenTests(unittest.TestCase):
    def test_nothing_that_is_not_a_token_is_accepted_or_raises(self) -> None:
        good = sup.make_token()
        head, payload, sig = good.split(".")
        padded = sup.b64(b"{}") + "=="
        deep = sup.b64(b"[" * 5000)
        not_utf8 = sup.b64(b"\xff\xfe")
        tokens = [
            "",
            ".",
            "..",
            "a.b",
            "a.b.c.d",
            f"{head}.{payload}",
            f"{good}.extra",
            f"{head}.{payload}.",
            f"{head}!.{payload}.{sig}",
            f"{head}.{payload}!.{sig}",
            f"{head}.{payload}.{sig}!",
            f"{head}.{payload}.{sig}=",
            f"{padded}.{payload}.{sig}",
            f"{sup.b64(b'not json')}.{payload}.{sig}",
            f"{head}.{sup.b64(b'not json')}.{sig}",
            f"{sup.b64(b'[]')}.{payload}.{sig}",
            f"{head}.{sup.b64(b'[1]')}.{sig}",
            f"{head}.{sup.b64(b'null')}.{sig}",
            f"{head}.{deep}.{sig}",
            f"{not_utf8}.{payload}.{sig}",
            "a" * (access_jwt.MAX_TOKEN + 1),
            "é.é.é",
            good.replace(".", " ."),
        ]
        for token in tokens:
            with self.subTest(token=token[:40]):
                self.assertFalse(verify(token).ok)

    def test_a_segment_must_be_unpadded_base64url(self) -> None:
        self.assertEqual(access_jwt.decode_segment("aGk"), b"hi")
        for bad in ("aGk=", "a+b", "a/b", "a b", "a", "aGkgA"):
            with self.subTest(bad=bad):
                self.assertIsNone(access_jwt.decode_segment(bad))


class KeySetTests(unittest.TestCase):
    def test_the_key_set_access_publishes_is_read(self) -> None:
        keys = access_jwt.parse_jwks(sup.jwks())
        self.assertEqual(keys, {sup.KID: sup.KEY})

    def test_keys_that_cannot_be_used_are_skipped(self) -> None:
        good = sup.jwks()["keys"][0]
        weak = {**good, "kid": "weak", "n": sup.b64((2**1023 + 1).to_bytes(128, "big"))}
        entries = [
            weak,
            {**good, "kid": "ec", "kty": "EC"},
            {**good, "kid": ""},
            {**good, "kid": 5},
            {**good, "kid": "no-n", "n": None},
            {**good, "kid": "bad-n", "n": "!!"},
            {**good, "kid": "even-e", "e": sup.b64(b"\x04")},
            {**good, "kid": "one-e", "e": sup.b64(b"\x01")},
            "text",
            None,
            good,
        ]
        self.assertEqual(access_jwt.parse_jwks({"keys": entries}), {sup.KID: sup.KEY})

    def test_anything_that_is_not_a_key_set_has_no_keys(self) -> None:
        for data in (None, [], "x", {}, {"keys": None}, {"keys": {}}, {"keys": []}):
            with self.subTest(data=data):
                self.assertEqual(access_jwt.parse_jwks(data), {})

    def test_a_key_of_another_modulus_does_not_verify_the_token(self) -> None:
        other = {sup.KID: RsaKey(sup.TEST_N + 2, sup.TEST_E)}
        verdict = verify_token(
            sup.make_token(), other, issuer=sup.ISSUER, audience=sup.AUD, now=sup.NOW
        )
        self.assertFalse(verdict.ok)


class UrlTests(unittest.TestCase):
    def test_the_certs_url_and_the_issuer_are_https(self) -> None:
        url = access_jwt.certs_url(sup.TEAM)
        self.assertEqual(url, "https://example.cloudflareaccess.com/cdn-cgi/access/certs")
        self.assertEqual(access_jwt.issuer(sup.TEAM), "https://example.cloudflareaccess.com")


if __name__ == "__main__":
    unittest.main()

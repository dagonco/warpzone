"""The webhook sender `prune` and `inbox` share."""

import unittest

from notify_support import Receiver
from warpzone.notify import NotifyError, notify


class NotifyTests(unittest.TestCase):
    def test_posts_the_payload_as_json(self) -> None:
        with Receiver() as hook:
            notify(hook.url, {"a": 1, "b": ["x"]})
        self.assertEqual(hook.bodies, [{"a": 1, "b": ["x"]}])

    def test_a_refusal_names_the_status_and_not_the_url(self) -> None:
        with Receiver(status=403) as hook, self.assertRaises(NotifyError) as ctx:
            notify(hook.url, {})
        self.assertEqual(str(ctx.exception), "notification rejected: HTTP 403")

    def test_an_unreachable_host_never_echoes_the_url_or_its_path(self) -> None:
        with self.assertRaises(NotifyError) as ctx:
            notify("http://127.0.0.1:1/api/webhook/secret-id", {})
        self.assertNotIn("secret-id", str(ctx.exception))
        self.assertTrue(str(ctx.exception).startswith("notification failed:"))


if __name__ == "__main__":
    unittest.main()

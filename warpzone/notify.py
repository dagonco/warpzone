"""The webhook that `prune` and `inbox` POST a JSON summary to.

The URL may embed a secret (a Home Assistant webhook id), so it is never echoed: neither in an
error message nor anywhere else. A failure is an error the caller turns into a warning; it never
fails the run that sent it. See development/design/prune.md ("Notifications").
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from warpzone.errors import WarpzoneError
from warpzone.syncthing import scrub


class NotifyError(WarpzoneError):
    """The webhook could not be reached or refused the summary. The message never holds the URL."""


def notify(url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    """POST a JSON summary. The URL may embed a secret (webhook id): it is never echoed."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        raise NotifyError(f"notification rejected: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = scrub(str(getattr(exc, "reason", exc)), (url, urllib.parse.urlsplit(url).path))
        raise NotifyError(f"notification failed: {reason}") from None

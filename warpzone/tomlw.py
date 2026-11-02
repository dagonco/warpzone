"""Minimal TOML value and key writer (the standard library only reads TOML)."""

from __future__ import annotations

import math
import re

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _string(text: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError("a lone surrogate cannot be written as TOML")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def scalar(value: str | int | float | bool) -> str:
    """A TOML literal for a string, integer, float or boolean."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    raise TypeError(f"not a TOML scalar: {type(value).__name__}")


def key(name: str) -> str:
    """A bare key when the name allows it, a quoted key otherwise."""
    return name if _BARE_KEY.match(name) else _string(name)

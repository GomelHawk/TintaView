"""Outbound HTTP details that are easy to get wrong once per call site.

Stdlib only. Two things live here because each was wrong in several places at once:

- **A credential must not follow a redirect.** `urllib` re-sends every ordinary request
  header to wherever a 3xx points, another host included, so a bearer token set through
  `headers=` goes along. `token_request` attaches it as an *unredirected* header — the
  stdlib's own mechanism for exactly this — so the first host gets it and nobody else.
- **A body is read up to a limit.** Everything fetched is a small JSON document or a
  release asset of known scale; `read_capped` refuses anything larger rather than
  holding it in memory.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Mapping
from typing import IO

#: Ceiling for an API's JSON answer. Every one read here is a few kilobytes.
MAX_JSON_BYTES = 1024 * 1024


class ResponseTooLarge(OSError):
    """A response body ran past the caller's limit. An `OSError`, so every existing
    "network failed" handler already covers it."""


def token_request(url: str, authorization: str, *, headers: Mapping[str, str] | None = None,
                  data: bytes | None = None, method: str | None = None) -> urllib.request.Request:
    """A request carrying `authorization` to `url`'s host only — never to a redirect's.

    Raises a `ValueError` that does **not** quote the value when it holds something a
    header cannot carry (a newline, a non-ASCII character). `http.client` raises its
    own for those, with the whole credential in the message — and that message went
    into the usage panel, the log and the `doctor` report.
    """
    if not (authorization.isascii() and authorization.isprintable()):
        raise ValueError("the stored credential has characters a header cannot carry")
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    req.add_unredirected_header("Authorization", authorization)
    return req


def read_capped(resp: IO[bytes], limit: int) -> bytes:
    """The whole body, or `ResponseTooLarge` if it is longer than `limit` bytes."""
    body = resp.read(limit + 1)
    if len(body) > limit:
        raise ResponseTooLarge(f"response is larger than {limit} bytes")
    return body


def copy_capped(resp: IO[bytes], out: IO[bytes], limit: int, chunk: int = 1024 * 1024) -> int:
    """Stream `resp` into `out`, raising `ResponseTooLarge` past `limit` bytes."""
    total = 0
    while True:
        block = resp.read(chunk)
        if not block:
            return total
        total += len(block)
        if total > limit:
            raise ResponseTooLarge(f"download is larger than {limit} bytes")
        out.write(block)

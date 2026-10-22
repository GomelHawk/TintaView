"""The machine's public IP address and its country — the usage panel's network line.

Fetched only when the panel opens (`ui.network_info`, off by default), never in the
background: the address changes on a network event (a VPN switched on, another Wi-Fi),
and the panel is the only place it is read, so polling while nobody looks buys nothing
and sends a request to a third party for every interval.

Source: Cloudflare's trace page, plain `key=value` lines with `ip=` and `loc=` (an
ISO 3166 alpha-2 code). No key, no account, no published rate limit. Two measured
details decide how it is reached:

- **`www.cloudflare.com`, not `1.1.1.1`.** The IP-literal host's certificate chains to an
  SSL.com root that a Windows certificate store may simply not hold yet (Windows fetches
  roots on first use, and Python's `ssl` only reads what is already there) — measured:
  `CERTIFICATE_VERIFY_FAILED` from the tray's own Windows interpreter, while
  `www.cloudflare.com` verified and served the same page.
- **IPv4 first.** The connection is opened to the host's IPv4 address when it has one,
  so the reported address is IPv4: an IPv6 address is up to 39 characters, which the
  panel's right-hand column has no room for. An IPv6-only network falls back to the
  default connection and shows its IPv6 address.

Stdlib only, never raises: the panel shows "unavailable" for every failure.
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import socket
import ssl
from dataclasses import dataclass

log = logging.getLogger(__name__)

TRACE_HOST = "www.cloudflare.com"
TRACE_PATH = "/cdn-cgi/trace"
#: Generous on purpose: the line says "Updating…" while it waits, and requests from the
#: Windows side were measured taking 6-12 s on a machine where WSL took well under one.
TIMEOUT_S = 12.0
#: The trace body is a few hundred bytes; anything bigger is not the page we asked for.
MAX_BYTES = 16 * 1024


@dataclass(frozen=True)
class NetInfo:
    ip: str
    #: ISO 3166 alpha-2, upper case — "" when the service didn't place the address
    #: (Cloudflare answers `XX` for unknown and `T1` for Tor).
    country: str


def parse_trace(text: str) -> NetInfo | None:
    """`NetInfo` from a trace body, or None when it carries no valid IP address."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        ip = str(ipaddress.ip_address(fields.get("ip", "")))
    except ValueError:
        return None
    loc = fields.get("loc", "").upper()
    country = loc if len(loc) == 2 and loc.isalpha() and loc != "XX" else ""
    return NetInfo(ip, country)


class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    """Connects to the host's first IPv4 address when it has one — see the module
    docstring — and otherwise exactly as `HTTPSConnection` would."""

    def __init__(self, host: str, timeout: float) -> None:
        self._tv_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self._tv_context)

    def connect(self) -> None:
        try:
            infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            infos = []
        if not infos:
            super().connect()
            return
        sock = socket.create_connection((str(infos[0][4][0]), self.port), self.timeout)
        self.sock = self._tv_context.wrap_socket(sock, server_hostname=self.host)


def fetch(timeout: float = TIMEOUT_S) -> NetInfo | None:
    """The current public address, or None on any failure. Never raises."""
    conn = _IPv4FirstHTTPSConnection(TRACE_HOST, timeout=timeout)
    try:
        conn.request("GET", TRACE_PATH, headers={"User-Agent": "TintaView"})
        resp = conn.getresponse()
        if resp.status != 200:
            log.info("network info unavailable: HTTP %s", resp.status)
            return None
        raw = resp.read(MAX_BYTES)
    except (OSError, http.client.HTTPException) as exc:
        log.info("network info unavailable: %s", exc)
        return None
    finally:
        conn.close()
    info = parse_trace(raw.decode("utf-8", "replace"))
    if info is None:
        log.info("network info unavailable: unrecognised response")
    return info

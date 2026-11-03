"""The machine's public IP address, its country and (when known) its city — the usage
panel's network line.

Fetched only when the panel opens (`ui.network_info`, off by default), never in the
background: the address changes on a network event (a VPN switched on, another Wi-Fi),
and the panel is the only place it is read, so polling while nobody looks buys nothing
and sends a request to a third party for every interval.

Two sources, tried in order:

1. **ipinfo.io** (`/json`, no token) — the only one of the two that names a city. Its
   answer carries `ip`, `country` (ISO 3166 alpha-2) and `city`, plus fields we ignore.
   Without a token it is rate-limited (roughly 50k requests a month), and geo-IP cities
   are approximate — a mobile network or a VPN often reads as the ISP's hub city, or
   none — so the city is shown only when the answer has one.
2. **Cloudflare's trace page**, plain `key=value` lines with `ip=` and `loc=` (alpha-2).
   No key, no account, no published rate limit — but no city: its `colo=` is the
   Cloudflare data centre that served the request, not where the user is, so it is
   never shown as one. Used whenever ipinfo fails, is rate-limited (HTTP 429) or answers
   with something that doesn't parse.

Each source gets its own `TIMEOUT_S`, so a hung ipinfo delays the fallback answer by
that much; the line says "Updating…" meanwhile. Two measured details decide how the
hosts are reached:

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
import json
import logging
import socket
import ssl
from dataclasses import dataclass

log = logging.getLogger(__name__)

IPINFO_HOST = "ipinfo.io"
IPINFO_PATH = "/json"
TRACE_HOST = "www.cloudflare.com"
TRACE_PATH = "/cdn-cgi/trace"
#: Generous on purpose: the line says "Updating…" while it waits, and requests from the
#: Windows side were measured taking 6-12 s on a machine where WSL took well under one.
TIMEOUT_S = 12.0
#: Both bodies are a few hundred bytes; anything bigger is not the page we asked for.
MAX_BYTES = 16 * 1024
#: A city name is display text from a third party; anything longer is not one.
MAX_CITY_CHARS = 64


@dataclass(frozen=True)
class NetInfo:
    ip: str
    #: ISO 3166 alpha-2, upper case — "" when the service didn't place the address
    #: (Cloudflare answers `XX` for unknown and `T1` for Tor).
    country: str
    #: "" when unknown — always so from the Cloudflare fallback, which has no city.
    city: str = ""


def _country_code(value: object) -> str:
    code = value.upper() if isinstance(value, str) else ""
    return code if len(code) == 2 and code.isalpha() and code != "XX" else ""


def parse_ipinfo(text: str) -> NetInfo | None:
    """`NetInfo` from an ipinfo.io `/json` body, or None when it carries no valid IP."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        ip = str(ipaddress.ip_address(str(data.get("ip", ""))))
    except ValueError:
        return None
    city = data.get("city")
    city = " ".join(city.split()) if isinstance(city, str) else ""
    if len(city) > MAX_CITY_CHARS:
        city = ""
    return NetInfo(ip, _country_code(data.get("country")), city)


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
    return NetInfo(ip, _country_code(fields.get("loc", "")))


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


def _get(host: str, path: str, timeout: float) -> str | None:
    """One GET's body, or None on any failure. Never raises."""
    conn = _IPv4FirstHTTPSConnection(host, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "TintaView",
                                           "Accept": "application/json, text/plain"})
        resp = conn.getresponse()
        if resp.status != 200:
            log.info("network info from %s unavailable: HTTP %s", host, resp.status)
            return None
        raw = resp.read(MAX_BYTES)
    except (OSError, http.client.HTTPException) as exc:
        log.info("network info from %s unavailable: %s", host, exc)
        return None
    finally:
        conn.close()
    return raw.decode("utf-8", "replace")


def fetch(timeout: float = TIMEOUT_S) -> NetInfo | None:
    """The current public address — from ipinfo.io, else Cloudflare — or None when
    neither answers. Never raises."""
    for host, path, parse in ((IPINFO_HOST, IPINFO_PATH, parse_ipinfo),
                              (TRACE_HOST, TRACE_PATH, parse_trace)):
        body = _get(host, path, timeout)
        if body is None:
            continue
        info = parse(body)
        if info is not None:
            return info
        log.info("network info from %s unavailable: unrecognised response", host)
    return None

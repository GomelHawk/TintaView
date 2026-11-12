"""`core.netinfo`: parsing ipinfo.io and Cloudflare's trace page, falling back from one
to the other, and failing soft."""

from __future__ import annotations

import http.client
import json
import ssl

from tintaview.core import netinfo
from tintaview.core.netinfo import NetInfo, parse_ipinfo, parse_trace

TRACE = """fl=1291f5
h=1.1.1.1
ip=203.0.113.42
ts=1792690781.000
visit_scheme=https
loc=UA
tls=TLSv1.3
warp=off
"""

# Shape measured from a live `https://ipinfo.io/json` answer; values replaced.
IPINFO = json.dumps({
    "ip": "203.0.113.42", "hostname": "host.example.net", "city": "Toruń",
    "region": "Kujawsko-Pomorskie", "country": "PL", "loc": "53.0138,18.5981",
    "org": "AS64500 Example", "postal": "87-100", "timezone": "Europe/Warsaw",
    "readme": "https://ipinfo.io/missingauth",
}, ensure_ascii=False)


def test_ipinfo_gives_address_country_and_city():
    assert parse_ipinfo(IPINFO) == NetInfo("203.0.113.42", "PL", "Toruń")


def test_ipinfo_without_a_city_or_country():
    assert parse_ipinfo('{"ip": "203.0.113.42", "country": "pl"}') == NetInfo(
        "203.0.113.42", "PL", "")
    assert parse_ipinfo('{"ip": "203.0.113.42"}') == NetInfo("203.0.113.42", "", "")
    assert parse_ipinfo('{"ip": "203.0.113.42", "country": "XX", "city": 7}') == NetInfo(
        "203.0.113.42", "", "")


def test_ipinfo_city_is_one_short_line():
    body = json.dumps({"ip": "203.0.113.42", "country": "PL", "city": " New\n  York "})
    assert parse_ipinfo(body).city == "New York"
    body = json.dumps({"ip": "203.0.113.42", "country": "PL", "city": "x" * 65})
    assert parse_ipinfo(body).city == ""


def test_ipinfo_without_a_valid_address_is_no_answer():
    for body in ('{"country": "PL"}', '{"ip": "nope"}', "[]", "not json",
                 '{"error": {"title": "Rate limit exceeded"}}'):
        assert parse_ipinfo(body) is None


def test_parses_the_address_and_country():
    assert parse_trace(TRACE) == NetInfo("203.0.113.42", "UA")


def test_tolerates_crlf_and_lower_case():
    assert parse_trace("ip=203.0.113.42\r\nloc=pl\r\n") == NetInfo("203.0.113.42", "PL")


def test_unplaced_addresses_have_no_country():
    for loc in ("XX", "T1", "", "UKR"):
        assert parse_trace(f"ip=203.0.113.42\nloc={loc}\n") == NetInfo("203.0.113.42", "")
    assert parse_trace("ip=203.0.113.42\n") == NetInfo("203.0.113.42", "")


def test_no_valid_address_is_no_answer():
    assert parse_trace("loc=UA\n") is None
    assert parse_trace("ip=not-an-ip\nloc=UA\n") is None
    assert parse_trace("<html>captive portal</html>") is None


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self, n: int) -> bytes:
        return self._body[:n]


class _FakeConnection:
    """Answers per host: `replies[host]` is `(status, body)` or an exception to raise."""

    instances: list[_FakeConnection] = []
    replies: dict = {}

    def __init__(self, host: str, timeout: float) -> None:
        self.host, self.timeout, self.requested, self.closed = host, timeout, None, False
        type(self).instances.append(self)

    def request(self, method: str, path: str, headers: dict) -> None:
        reply = self.replies[self.host]
        if isinstance(reply, BaseException):
            raise reply
        self.requested = (method, path)

    def getresponse(self) -> _FakeResponse:
        status, body = self.replies[self.host]
        return _FakeResponse(status, body)

    def close(self) -> None:
        self.closed = True


_IPINFO_OK = (200, IPINFO.encode())
_TRACE_OK = (200, TRACE.encode())


def _fake(monkeypatch, ipinfo=_IPINFO_OK, trace=_TRACE_OK):
    cls = type("Conn", (_FakeConnection,), {
        "instances": [], "replies": {netinfo.IPINFO_HOST: ipinfo, netinfo.TRACE_HOST: trace}})
    monkeypatch.setattr(netinfo, "_IPv4FirstHTTPSConnection", cls)
    return cls


def test_fetch_asks_ipinfo_first_and_stops_there(monkeypatch):
    conn = _fake(monkeypatch)
    assert netinfo.fetch() == NetInfo("203.0.113.42", "PL", "Toruń")
    (made,) = conn.instances
    assert (made.host, made.timeout) == (netinfo.IPINFO_HOST, netinfo.TIMEOUT_S)
    assert made.requested == ("GET", netinfo.IPINFO_PATH)
    assert made.closed


def test_fetch_falls_back_to_cloudflare(monkeypatch):
    """Every way ipinfo can fail lands on the trace page, which has no city."""
    for ipinfo in (TimeoutError("timed out"), ssl.SSLCertVerificationError("no issuer"),
                   (429, b'{"error": "rate limited"}'), (200, b"<html>portal</html>")):
        conn = _fake(monkeypatch, ipinfo=ipinfo)
        assert netinfo.fetch() == NetInfo("203.0.113.42", "UA", "")
        assert [c.host for c in conn.instances] == [netinfo.IPINFO_HOST, netinfo.TRACE_HOST]
        assert conn.instances[1].requested == ("GET", netinfo.TRACE_PATH)
        assert all(c.closed for c in conn.instances)


def test_fetch_never_raises(monkeypatch):
    for error in (ssl.SSLCertVerificationError("unable to get local issuer"),
                  TimeoutError("timed out"), http.client.RemoteDisconnected("closed")):
        _fake(monkeypatch, ipinfo=error, trace=error)
        assert netinfo.fetch() is None


def test_fetch_rejects_error_pages(monkeypatch):
    _fake(monkeypatch, ipinfo=(403, IPINFO.encode()), trace=(403, b"ip=203.0.113.42\nloc=UA\n"))
    assert netinfo.fetch() is None


def test_connects_over_ipv4_when_the_host_has_it(monkeypatch):
    """The address shown is whichever family the connection used, so the connection
    has to be IPv4 for the short form to appear."""
    seen = {}
    monkeypatch.setattr(netinfo.socket, "getaddrinfo", lambda host, port, family, kind: (
        seen.setdefault("family", family), [(family, kind, 6, "", ("192.0.2.7", port))])[1])
    monkeypatch.setattr(netinfo.socket, "create_connection",
                        lambda addr, timeout: seen.setdefault("addr", addr))
    conn = netinfo._IPv4FirstHTTPSConnection("example.test", timeout=1.0)
    monkeypatch.setattr(conn._tv_context, "wrap_socket",
                        lambda sock, server_hostname: seen.setdefault("sni", server_hostname))
    conn.connect()
    assert seen == {"family": netinfo.socket.AF_INET, "addr": ("192.0.2.7", 443),
                    "sni": "example.test"}


def test_the_tls_handshake_offers_http_1_1_like_http_client_does(monkeypatch):
    """`ssl.create_default_context()` offers no ALPN protocol; `http.client`'s own context
    offers `http/1.1`. Measured: ipinfo.io answered the bare handshake with HTTP 406 on an
    Ubuntu VM — country but never a city in the panel — and 200 with `http/1.1` offered."""
    offered: list = []
    real = netinfo.ssl.create_default_context

    def recording_context(*args, **kwargs):
        ctx = real(*args, **kwargs)
        original = ctx.set_alpn_protocols
        ctx.set_alpn_protocols = lambda protocols: (offered.append(list(protocols)),
                                                    original(protocols))[1]
        return ctx

    monkeypatch.setattr(netinfo.ssl, "create_default_context", recording_context)
    netinfo._IPv4FirstHTTPSConnection("ipinfo.io", timeout=1)
    assert offered == [["http/1.1"]]

"""`core.netinfo`: parsing Cloudflare's trace page and failing soft."""

from __future__ import annotations

import http.client
import ssl

from tintaview.core import netinfo
from tintaview.core.netinfo import NetInfo, parse_trace

TRACE = """fl=1291f5
h=1.1.1.1
ip=203.0.113.42
ts=1792690781.000
visit_scheme=https
loc=UA
tls=TLSv1.3
warp=off
"""


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
    instances: list[_FakeConnection] = []
    status, body, error = 200, TRACE.encode(), None

    def __init__(self, host: str, timeout: float) -> None:
        self.host, self.timeout, self.requested, self.closed = host, timeout, None, False
        type(self).instances.append(self)

    def request(self, method: str, path: str, headers: dict) -> None:
        if self.error:
            raise self.error
        self.requested = (method, path)

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.status, self.body)

    def close(self) -> None:
        self.closed = True


def _fake(monkeypatch, **attrs):
    cls = type("Conn", (_FakeConnection,), dict(attrs, instances=[]))
    monkeypatch.setattr(netinfo, "_IPv4FirstHTTPSConnection", cls)
    return cls


def test_fetch_reads_the_trace(monkeypatch):
    conn = _fake(monkeypatch)
    assert netinfo.fetch() == NetInfo("203.0.113.42", "UA")
    (made,) = conn.instances
    assert (made.host, made.timeout) == (netinfo.TRACE_HOST, netinfo.TIMEOUT_S)
    assert made.requested == ("GET", netinfo.TRACE_PATH)
    assert made.closed


def test_fetch_never_raises(monkeypatch):
    _fake(monkeypatch, error=ssl.SSLCertVerificationError("unable to get local issuer"))
    assert netinfo.fetch() is None
    _fake(monkeypatch, error=TimeoutError("timed out"))
    assert netinfo.fetch() is None
    _fake(monkeypatch, error=http.client.RemoteDisconnected("closed"))
    assert netinfo.fetch() is None


def test_fetch_rejects_an_error_page(monkeypatch):
    _fake(monkeypatch, status=403, body=b"ip=203.0.113.42\nloc=UA\n")
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

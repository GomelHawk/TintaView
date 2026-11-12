"""What keeps a web page, another host and another local user out.

The broker has no authentication by design — a hook is a one-line `curl` — so what it
refuses is everything a *browser* sends, and what it writes is owner-only. These tests
drive a real `StatusServer` with real HTTP requests carrying the headers a browser adds.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

from tintaview.core import config as config_mod
from tintaview.core import fsutil, net, usercmd
from tintaview.core import server as server_mod
from tintaview.core import state as state_mod
from tintaview.core.config import Config
from tintaview.core.controller import LightController
from tintaview.core.server import StatusServer, foreign_request_reason, is_loopback_host
from tintaview.core.state import StateStore
from tintaview.engines.null import NullEngine

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="permission bits are POSIX")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path / "home" / ".tintaview"))


@pytest.fixture
def daemon():
    cfg = Config()
    cfg.server.port = 0
    server = StatusServer(cfg, controller=LightController(cfg, engine=NullEngine()))
    assert server.start()
    server.notified = []  # type: ignore[attr-defined]
    server.quits = []  # type: ignore[attr-defined]
    server.on_notify = lambda agent, message, cwd: server.notified.append(message)
    server.on_quit = lambda: server.quits.append(True)
    try:
        yield server
    finally:
        server.stop()


def _request(server: StatusServer, method: str, path: str, headers: dict | None = None,
             body: bytes | None = None, host: str | None = None) -> tuple[int, dict]:
    port = int(server.url.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{port}")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


_NOTIFY = json.dumps({"message": "hello"}).encode()
_JSON = {"Content-Type": "application/json"}


# --------------------------------------------------------------------------- browser requests


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_a_web_page_cannot_stop_the_tray(daemon, headers):
    """`<img src=…/quit>` carries no Origin, only Sec-Fetch-Site; a `fetch` carries both."""
    status, _ = _request(daemon, "GET", "/quit", headers)
    assert status == 403
    assert daemon.quits == []


def test_a_web_page_cannot_send_a_notification(daemon):
    """The one a page would want most: `notify.command` runs with its text."""
    status, _ = _request(daemon, "POST", "/v1/notify?agent=claude",
                         {**_JSON, "Origin": "https://evil.example"}, _NOTIFY)
    assert status == 403
    assert daemon.notified == []


def test_a_web_page_cannot_raise_a_confirm(daemon):
    status, _ = _request(daemon, "GET", "/v1/event/confirm?agent=claude&sid=s&question=run+this",
                         {"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert daemon.state.snapshot()["count"] == 0
    status, _ = _request(daemon, "POST", "/v1/event/confirm?agent=claude",
                         {"Origin": "https://evil.example"}, b'{"session_id": "s"}')
    assert status == 403
    assert daemon.state.snapshot()["count"] == 0


def test_a_rebound_dns_name_cannot_read_the_state(daemon):
    """DNS rebinding makes the page same-origin with us; only its Host gives it away."""
    port = daemon.url.rsplit(":", 1)[1]
    status, body = _request(daemon, "GET", "/state", host=f"attacker.example:{port}")
    assert status == 403 and "agents" not in body


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}",
                                  "127.0.0.1", "192.168.1.20:{port}"])
def test_every_way_a_hook_names_the_daemon_still_works(daemon, host):
    """curl sends no Origin and no Sec-Fetch-Site; its Host is an IP or `localhost`."""
    port = daemon.url.rsplit(":", 1)[1]
    status, _ = _request(daemon, "GET", "/v1/event/working?agent=claude&sid=abc",
                         host=host.format(port=port))
    assert status == 200
    assert daemon.state.snapshot()["count"] == 1


def test_a_request_with_no_host_header_still_works(daemon):
    """HTTP/1.0 clients send none; a browser always does."""
    port = int(daemon.url.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(b"GET /healthz HTTP/1.0\r\n\r\n")
        assert b" 200 " in sock.recv(200)


def test_typing_the_url_into_a_browser_still_works(daemon):
    status, body = _request(daemon, "GET", "/state", {"Sec-Fetch-Site": "none"})
    assert status == 200 and "agents" in body


def test_the_configured_host_name_is_accepted():
    assert foreign_request_reason({"Host": "mybox.lan:8777"}, "mybox.lan") == ""
    assert foreign_request_reason({"Host": "other.lan:8777"}, "mybox.lan") != ""


def test_notify_needs_a_json_content_type(daemon):
    """A cross-site form or text/plain post is the only body a page can send unasked."""
    for headers in ({}, {"Content-Type": "text/plain"},
                    {"Content-Type": "application/x-www-form-urlencoded"}):
        status, _ = _request(daemon, "POST", "/v1/notify?agent=claude", headers, _NOTIFY)
        assert status == 415
    assert daemon.notified == []
    status, body = _request(daemon, "POST", "/v1/notify?agent=claude",
                            {"Content-Type": "application/json; charset=utf-8"}, _NOTIFY)
    assert (status, body["notified"]) == (200, True)
    assert daemon.notified == ["hello"]


def test_the_mcp_server_passes_every_check(daemon):
    """The real client of `/v1/notify`, end to end."""
    from tintaview.core import mcp

    reply = mcp.Server("claude", base_url=daemon.url, cwd="/work")._notify("done")
    assert reply["isError"] is False
    assert daemon.notified == ["done"]


# --------------------------------------------------------------------------- ingress values


def test_a_get_session_id_cannot_forge_a_log_line_or_grow_without_bound(daemon):
    sid = "abc%0A2026-01-01%20ERROR%20forged" + "x" * 5000
    status, _ = _request(daemon, "GET",
                         f"/v1/event/tool-start?agent=claude&sid={sid}&tool=B%0Aash")
    assert status == 200
    (key,) = daemon.state._sessions
    assert "\n" not in key[1] and len(key[1]) <= server_mod.MAX_ID_CHARS
    assert key[1].startswith("abc")
    assert "\n" not in daemon.state._sessions[key].tool


def test_an_ordinary_session_id_is_kept_as_it_is(daemon):
    """Cleaning must not move a real session into another bucket."""
    for path in ("/v1/event/working?agent=claude&sid=3f2a-9.b_c", "/working?sid=3f2a-9.b_c"):
        assert _request(daemon, "GET", path)[0] == 200
    assert daemon.state.snapshot()["count"] == 1
    assert server_mod._ingress_ids({"sid": ["my session:1"], "agent": ["claude"]})[1] == (
        "my session:1")


def test_the_session_store_is_bounded(monkeypatch):
    monkeypatch.setattr(state_mod, "MAX_SESSIONS", 5)
    store = StateStore()
    for n in range(20):
        store.set("claude", f"s{n}", "working")
        time.sleep(0.001)
    snapshot = store.snapshot()
    assert snapshot["count"] == 5
    # The newest survive; an existing session is never evicted by its own update.
    store.set("claude", "s19", "idle")
    assert store.snapshot()["count"] == 5


def test_a_silent_connection_does_not_hold_a_thread_forever(daemon, monkeypatch):
    assert server_mod._Handler.timeout == server_mod.REQUEST_TIMEOUT_S
    monkeypatch.setattr(server_mod._Handler, "timeout", 0.2)
    port = int(daemon.url.rsplit(":", 1)[1])
    slots = daemon._httpd._slots
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.settimeout(3)
        # The server gave up and closed it; a reset is it being dropped just the same.
        with contextlib.suppress(ConnectionError):
            assert sock.recv(10) == b""
    # Its handler — and the thread running it — is finished: the slot is free again.
    # (Not `threading.active_count()`: other tests' threads are still winding down.)
    deadline = time.monotonic() + 3
    while slots._value < server_mod.MAX_CONNECTIONS and time.monotonic() < deadline:
        time.sleep(0.02)
    assert slots._value == server_mod.MAX_CONNECTIONS


# --------------------------------------------------------------------------- non-loopback host


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.5", True),
    ("0.0.0.0", False), ("192.168.1.20", False), ("::", False), ("mybox.lan", False),
])
def test_is_loopback_host(host, expected):
    assert is_loopback_host(host) is expected


def test_doctor_warns_about_a_non_loopback_host(capsys):
    from tintaview.install import doctor

    cfg = Config()
    cfg.server.host = "0.0.0.0"
    config_mod.save(cfg)
    reporter = doctor._Reporter(verbose=False)
    doctor._check_config(reporter)
    out = capsys.readouterr().out
    assert "[WARN] CONFIG" in out and "not a loopback address" in out

    cfg.server.host = "127.0.0.1"
    config_mod.save(cfg)
    doctor._check_config(doctor._Reporter(verbose=False))
    assert "loopback" not in capsys.readouterr().out


# --------------------------------------------------------------------------- files


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@posix_only
def test_config_and_its_directory_are_owner_only(tmp_path):
    """`notify.command` is where the documented Telegram example puts a bot token."""
    old = os.umask(0o022)
    try:
        path = config_mod.save(Config())
    finally:
        os.umask(old)
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    # An existing world-readable config is tightened by the next save, not left alone.
    path.chmod(0o644)
    config_mod.save(Config())
    assert _mode(path) == 0o600


@posix_only
def test_usage_cache_and_trend_are_owner_only(tmp_path):
    from tintaview.stats.cache import UsageCache
    from tintaview.stats.model import UsageResult

    cache = UsageCache(tmp_path / "usage_cache.json")
    cache.update(UsageResult(agent="claude"))
    assert _mode(tmp_path / "usage_cache.json") == 0o600


@posix_only
def test_atomic_write_never_exposes_a_private_file(tmp_path, monkeypatch):
    """The temp file is created with the final mode — never chmod-ed down afterwards."""
    target = tmp_path / "secret.json"
    target.write_text("old")
    target.chmod(0o600)
    seen: list[int] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(_mode(Path(src)))
        real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", spy)
    old = os.umask(0)
    try:
        fsutil.atomic_write(target, "new")
    finally:
        os.umask(old)
    assert seen == [0o600]
    assert target.read_text() == "new" and _mode(target) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["secret.json"]


def test_atomic_write_leaves_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("old")
    monkeypatch.setattr(fsutil.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        fsutil.atomic_write(target, "new")
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_atomic_write_takes_text_and_bytes(tmp_path):
    fsutil.atomic_write(tmp_path / "a", "héllo\n")
    fsutil.atomic_write(tmp_path / "b", b"\x00\x01")
    assert (tmp_path / "a").read_text(encoding="utf-8") == "héllo\n"
    assert (tmp_path / "b").read_bytes() == b"\x00\x01"


@posix_only
def test_the_hook_script_is_replaced_whole_and_stays_executable(tmp_path):
    from tintaview.install import hookscript

    dest = tmp_path / "tv-hook.sh"
    hookscript._write_local_script(dest, "#!/bin/sh\nexit 0\n")
    assert _mode(dest) & 0o111 == 0o111
    hookscript._write_local_script(dest, "#!/bin/sh\nexit 1\n")
    assert dest.read_text() == "#!/bin/sh\nexit 1\n" and _mode(dest) & 0o111 == 0o111
    assert [p.name for p in tmp_path.iterdir()] == ["tv-hook.sh"]


# --------------------------------------------------------------------------- outbound tokens


def test_a_token_is_not_resent_to_a_redirect_target():
    """urllib copies `headers` onto the request a 3xx produces — and nothing else."""
    req = net.token_request("https://api.example/usage", "Bearer secret",
                            headers={"Accept": "application/json"})
    assert req.get_header("Authorization") == "Bearer secret"
    assert "Authorization" not in req.headers
    assert req.headers == {"Accept": "application/json"}


def test_every_provider_sends_its_token_unredirected(monkeypatch):
    """Derived from the providers themselves: whatever request each builds for its
    token-bearing call must keep the token out of the redirectable headers."""
    import urllib.request

    from tintaview.stats.providers import claude, copilot, cursor

    captured: list = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        raise OSError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(claude, "_read_access_token", lambda home: ("tok", False))
    calls = (lambda: claude._fetch_usage(Path("."), 1), lambda: cursor._post_usage("tok", 1),
             lambda: copilot._fetch_user("tok", 1))
    for call in calls:
        with pytest.raises(OSError):
            call()
    assert len(captured) == len(calls)
    for req in captured:
        assert "tok" in req.get_header("Authorization")
        assert not any("tok" in str(v) for v in req.headers.values())


def test_read_capped_refuses_an_oversized_body():
    import io

    assert net.read_capped(io.BytesIO(b"abc"), 3) == b"abc"
    with pytest.raises(net.ResponseTooLarge):
        net.read_capped(io.BytesIO(b"abcd"), 3)


# --------------------------------------------------------------------------- user commands


def test_a_failed_user_command_is_never_written_to_the_log(monkeypatch, caplog):
    secret = "curl https://api.telegram.org/bot123456:SECRET/sendMessage"
    monkeypatch.setattr(usercmd.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no shell")))
    with caplog.at_level("ERROR"):
        usercmd.spawn(secret, {})  # must not raise
    assert caplog.records and "SECRET" not in caplog.text


def test_every_command_gets_the_same_variable_names():
    """One script serves all three commands, so none may leave a variable unset."""
    confirm = usercmd.confirm_variables(["Claude Code"], "q", "m", {"detail": "d"})
    notify = usercmd.notify_variables("Claude Code", "m", "/work")
    limit = usercmd.limit_variables("Claude Code", "5-hour limit", 90, "m")
    assert set(confirm) == set(notify)
    assert set(confirm) <= set(limit)
    assert [v["TINTAVIEW_STATUS"] for v in (confirm, notify, limit)] == [
        "confirm", "notify", "limit"]


# --------------------------------------------------------------------------- shared config


def test_saving_the_config_does_not_change_it():
    """The tray saves on the GUI thread while HTTP threads read `cfg.agents`; a
    serialiser that inserted tables as it went was the one writer they could collide
    with. The file still gets a table for every enabled agent."""
    cfg = Config()
    cfg.enabled_agents = ["claude", "cursor"]
    before = dict(cfg.agents)
    text = config_mod.dumps(cfg)
    assert cfg.agents == before
    assert "[agents.claude]" in text and "[agents.cursor]" in text
    assert config_mod.dumps(cfg) == text


def test_only_code_that_owns_its_config_uses_the_inserting_lookup():
    """`Config.agent()` inserts a table; `agent_config()` only reads. Anything that runs
    against the *live* config on another thread — the broker, the stats providers, the
    workers, the flyout — must use the read-only one. The modules allowed to insert each
    work on a config of their own: the wizard and `doctor` load theirs, the settings
    dialog edits a deep copy. Walked from the package, so a new module is covered."""
    import re

    package = Path(config_mod.__file__).resolve().parent.parent
    allowed = {"install/doctor.py", "ui/settings_dialog.py", "ui/wizard.py"}
    inserting = re.compile(r"\.agent\(")
    users = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if inserting.search(path.read_text(encoding="utf-8"))
    }
    assert users <= allowed, f"Config.agent() used on a shared config in {sorted(users - allowed)}"
    assert "ui/wizard.py" in users  # the walk found a real caller, so it looked

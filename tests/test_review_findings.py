"""Regression tests for the from-zero security review.

One test (or a small group) per finding, named for what an attacker — a web page, a
prompt-injected agent, another local user, a hostile file — could do before the fix.
Each drives the real code: a real `StatusServer`, real files, real subprocesses.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import stat
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

from tintaview.core import config as config_mod
from tintaview.core import fsutil, net, proc, request, usercmd
from tintaview.core import server as server_mod
from tintaview.core.config import Config
from tintaview.core.controller import LightController
from tintaview.core.server import StatusServer, foreign_request_reason
from tintaview.engines.null import NullEngine
from tintaview.install import codex_flag, hooks
from tintaview.install import update as update_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="permission bits are POSIX")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TINTAVIEW_HOME", str(tmp_path / "home" / ".tintaview"))


@pytest.fixture
def daemon():
    cfg = Config()
    cfg.server.port = 0
    cfg.enabled_agents = ["claude", "codex"]
    server = StatusServer(cfg, controller=LightController(cfg, engine=NullEngine()))
    assert server.start()
    server.notified = []  # type: ignore[attr-defined]
    server.quits = []  # type: ignore[attr-defined]
    server.on_notify = lambda agent, message, cwd: server.notified.append((agent, message))
    server.on_quit = lambda: server.quits.append(True)
    try:
        yield server
    finally:
        server.stop()


def _port(server: StatusServer) -> int:
    return int(server.url.rsplit(":", 1)[1])


def _request(server, method, path, headers=None, body=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", _port(server), timeout=3)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host if host is not None else f"127.0.0.1:{_port(server)}")
        for key, value in (headers or {}).items():
            conn.putheader(key, value)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, (json.loads(raw) if raw else {})
        except ValueError:
            return resp.status, {}
    finally:
        conn.close()


_FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
_SAFARI_OLD = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/15.6 Safari/605.1.15")
_POWERSHELL = "Mozilla/5.0 (Windows NT; Windows NT 10.0; en-US) WindowsPowerShell/5.1.26100.1"
_JSON = {"Content-Type": "application/json"}


# --------------------------------------------------------------------------- the browser guard


def test_a_page_cannot_reach_the_broker_through_the_unspecified_address(daemon):
    """`<img src="http://0.0.0.0:8777/quit">`: on Linux and macOS that address connects
    to a loopback listener, and a browser sends no Sec-Fetch-Site to it."""
    port = _port(daemon)
    for host in (f"0.0.0.0:{port}", "0.0.0.0", f"[::]:{port}"):
        status, _ = _request(daemon, "GET", "/quit", {"User-Agent": "curl/8.5.0"}, host=host)
        assert status == 403, host
    status, _ = _request(daemon, "GET", "/v1/event/confirm?agent=claude&sid=w&question=x",
                         {"User-Agent": _FIREFOX, "Referer": "https://evil.example/"},
                         host=f"0.0.0.0:{port}")
    assert status == 403
    assert daemon.quits == [] and daemon.state.snapshot()["count"] == 0


def test_a_bind_on_the_unspecified_address_still_answers_to_it():
    """`server.host = "0.0.0.0"` is the user's own choice; a hook may then say so."""
    assert foreign_request_reason({"Host": "0.0.0.0:8777"}, "0.0.0.0") == ""
    assert foreign_request_reason({"Host": "0.0.0.0:8777"}, "127.0.0.1") != ""


@pytest.mark.parametrize("agent", [_FIREFOX, _SAFARI_OLD])
def test_a_browser_that_sends_no_fetch_metadata_is_refused(daemon, agent):
    """Safari before 16.4 sends no Sec-Fetch-Site at all, even to 127.0.0.1."""
    status, _ = _request(daemon, "GET", "/quit", {"User-Agent": agent})
    assert status == 403
    assert daemon.quits == []
    # The same browser, a real address-bar visit on a current version: still allowed.
    status, _ = _request(daemon, "GET", "/healthz", {"User-Agent": agent, "Sec-Fetch-Site": "none"})
    assert status == 200


@pytest.mark.parametrize("agent", ["curl/8.5.0", "Python-urllib/3.12", _POWERSHELL, ""])
def test_command_line_clients_are_never_mistaken_for_browsers(daemon, agent):
    """PowerShell's Invoke-WebRequest says `Mozilla/5.0` too, and hooks may be written
    in it — the installer's own smoke test stops the daemon that way."""
    headers = {"User-Agent": agent} if agent else {}
    status, _ = _request(daemon, "GET", "/v1/event/working?agent=claude&sid=s", headers)
    assert status == 200
    assert daemon.state.snapshot()["count"] == 1


# --------------------------------------------------------------------------- /state, notify


def test_state_over_http_does_not_say_what_an_agent_is_asking(daemon):
    """A pending command can hold a token, and loopback is open to every local account.
    The tray reads the same thing in-process."""
    body = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": "/home/u/secret-proj",
                       "tool_input": {"command": "curl -H 'Authorization: Bearer sk-live'"}})
    _request(daemon, "POST", "/v1/event/confirm?agent=claude", _JSON, body.encode())
    deadline = time.monotonic() + 2
    while daemon.state.snapshot()["effective"] != "confirm" and time.monotonic() < deadline:
        time.sleep(0.01)

    status, public = _request(daemon, "GET", "/state")
    assert status == 200 and public["effective"] == "confirm"
    assert public["agents"]["claude"]["sessions"] == {"s1": "confirm"}
    text = json.dumps(public)
    assert "sk-live" not in text and "secret-proj" not in text
    assert not set(server_mod.PRIVATE_STATE_KEYS) & set(public["agents"]["claude"])

    private = daemon.state_payload()["agents"]["claude"]
    assert "sk-live" in private["detail"] and private["cwd"] == "/home/u/secret-proj"


def test_a_disabled_agent_cannot_notify_the_user(daemon):
    status, body = _request(daemon, "POST", "/v1/notify?agent=cursor", _JSON,
                            b'{"message": "hi"}')
    assert (status, body["notified"]) == (200, False)
    assert daemon.notified == []


def test_notifications_are_rate_limited(daemon, monkeypatch):
    """Each one is a balloon and a run of the user's notify command — a phone push."""
    statuses = [_request(daemon, "POST", "/v1/notify?agent=claude", _JSON,
                         b'{"message": "hi"}')[0] for _ in range(server_mod.NOTIFY_BURST + 20)]
    assert statuses[:server_mod.NOTIFY_BURST] == [200] * server_mod.NOTIFY_BURST
    assert set(statuses[server_mod.NOTIFY_BURST:]) == {429}
    assert len(daemon.notified) == server_mod.NOTIFY_BURST
    # The allowance comes back with time.
    daemon._notify_stamp -= server_mod.NOTIFY_REFILL_S
    assert _request(daemon, "POST", "/v1/notify?agent=claude", _JSON, b'{"message": "x"}')[0] == 200


def test_deeply_nested_json_cannot_swallow_a_confirm(daemon):
    """`json.loads` raises RecursionError, which is not a ValueError: the confirm was
    acknowledged and then dropped, so the session never turned red."""
    nested = "[" * 20000 + "]" * 20000
    payload = ('{"session_id": "deep-1", "tool_name": "Bash", "tool_input": {"x": '
               + nested + "}}")
    assert request.parse(payload.encode()).sid == "deep-1"
    status, _ = _request(daemon, "POST", "/v1/event/confirm?agent=claude", _JSON, payload.encode())
    assert status == 200
    deadline = time.monotonic() + 2
    while daemon.state.snapshot()["effective"] != "confirm" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert daemon.state.snapshot()["effective"] == "confirm"
    status, _ = _request(daemon, "POST", "/v1/notify?agent=claude", _JSON, nested.encode())
    assert status == 400


def test_a_malformed_request_line_is_a_400_not_a_traceback(daemon, caplog):
    port = _port(daemon)
    with caplog.at_level("ERROR"), socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(b"GET http://[x/quit HTTP/1.0\r\n\r\n")
        assert b" 400 " in s.recv(200)
    assert "Traceback" not in caplog.text and not caplog.records


def test_escape_sequences_in_a_path_never_reach_the_log(daemon, caplog):
    port = _port(daemon)
    with caplog.at_level("WARNING"), socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(b"GET /quit\x1b[2J\x1b[31mFAKE HTTP/1.0\r\nOrigin: https://evil.example\r\n\r\n")
        s.recv(200)
    assert caplog.records and "\x1b" not in caplog.text


def test_a_trickling_client_is_cut_off_at_the_deadline(daemon, monkeypatch):
    """One byte every few seconds beat the per-read timeout indefinitely."""
    monkeypatch.setattr(server_mod, "REQUEST_DEADLINE_S", 0.4)
    monkeypatch.setattr(server_mod._Handler, "timeout", 30)
    with socket.create_connection(("127.0.0.1", _port(daemon)), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(b"G")
        started = time.monotonic()
        assert sock.recv(10) == b""  # closed by the server
        assert time.monotonic() - started < 3


def test_connections_beyond_the_cap_are_closed_not_given_threads(daemon, monkeypatch):
    held = [socket.create_connection(("127.0.0.1", _port(daemon)), timeout=5)
            for _ in range(server_mod.MAX_CONNECTIONS)]
    try:
        for sock in held:
            sock.sendall(b"G")
        time.sleep(0.3)
        before = threading.active_count()
        extra = [socket.create_connection(("127.0.0.1", _port(daemon)), timeout=5)
                 for _ in range(40)]
        try:
            for sock in extra:
                sock.settimeout(3)
                _assert_closed_by_peer(sock)
            assert threading.active_count() <= before + 2
        finally:
            for sock in extra:
                sock.close()
    finally:
        for sock in held:
            sock.close()
    # ...and once they are gone, the broker answers again.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            if _request(daemon, "GET", "/healthz")[0] == 200:
                break
        except OSError:
            time.sleep(0.05)
    assert _request(daemon, "GET", "/healthz")[0] == 200


def _assert_closed_by_peer(sock) -> None:
    # A reset instead of a clean close is the server dropping it just the same.
    with contextlib.suppress(ConnectionError):
        assert sock.recv(10) == b""


# --------------------------------------------------------------------------- shown text


def test_agent_text_in_the_bell_is_never_treated_as_markup(qapp_or_skip):
    """A tooltip and a label both auto-detect rich text: `<img src=…>` in a notify_user
    message or a working directory made Qt go and load the file (a UNC path on Windows
    is an outbound login attempt)."""
    from PySide6 import QtCore, QtWidgets

    from tintaview.ui import flyout as flyout_mod
    from tintaview.ui.notify_log import NotifyEntry

    shown: list[str] = []
    panel = flyout_mod.Flyout()
    panel.notify_log.add(NotifyEntry(agent="claude", message='Done <img src="x.png">',
                                     cwd="/home/u/<img src=&#47;p.png>"))
    original = QtWidgets.QToolTip.showText
    QtWidgets.QToolTip.showText = staticmethod(lambda pos, text, *a: shown.append(text))
    try:
        centre = panel._bell_rect().center().toPoint()
        from PySide6 import QtGui

        help_event = QtGui.QHelpEvent(QtCore.QEvent.ToolTip, centre, panel.mapToGlobal(centre))
        panel.event(help_event)
    finally:
        QtWidgets.QToolTip.showText = original
    assert shown and "<img" not in shown[0] and "&lt;img" in shown[0]

    source = Path(flyout_mod.__file__).read_text(encoding="utf-8")
    label = source[source.index("project = QtWidgets.QLabel(entry.project)"):][:300]
    assert "setTextFormat(Qt.PlainText)" in label


@pytest.fixture
def qapp_or_skip():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])


def test_a_value_ending_in_a_backslash_cannot_reopen_a_quoted_argument():
    """`"…%TINTAVIEW_CWD%"` with `C:\\proj\\`: the program's own parser reads `\\"` as
    a literal quote, and the next argument's text spilled into the command line."""
    assert usercmd.cmd_safe("C:\\proj\\", windows=True) == "C:\\proj\\ "
    assert usercmd.cmd_safe("C:\\proj", windows=True) == "C:\\proj"
    assert usercmd.cmd_safe('say "hi"\\', windows=True) == "say 'hi'\\ "
    assert usercmd.cmd_safe("C:\\proj\\", windows=False) == "C:\\proj\\"


# --------------------------------------------------------------------------- credentials


@pytest.mark.parametrize("token", ["FAKE-SECRET-TOKEN\n", "FAKE-SECRET-☃", "FAKE\rSECRET"])
def test_a_credential_a_header_cannot_carry_never_appears_in_the_error(token):
    """`http.client` quotes the whole header value in its exception, and that text went
    to the usage panel, the log and the doctor report."""
    with pytest.raises(ValueError) as caught:
        net.token_request("https://api.example/usage", f"Bearer {token}")
    assert "FAKE" not in str(caught.value) and "FAKE" not in repr(caught.value)


def test_a_provider_error_never_quotes_the_stored_token(tmp_path, monkeypatch):
    import urllib.request

    from tintaview.stats.providers import claude

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("sent a request with a broken credential"))
    home = tmp_path / ".claude"
    home.mkdir()
    (home / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "FAKE-SECRET-TOKEN\n", "expiresAt": 9999999999999}}))
    from tintaview.core.config import AgentConfig

    result = claude.ClaudeUsageProvider().fetch(AgentConfig(home=str(home)), with_estimate=False)
    assert "FAKE-SECRET" not in (result.error or "")


def test_the_doctor_report_does_not_quote_the_users_codex_config(tmp_path):
    """The flag is appended at the end of config.toml — where `[mcp_servers.*.env]`
    blocks with their tokens usually are — and three context lines came with the diff."""
    config = tmp_path / "config.toml"
    config.write_text('model = "o3"\n\n[mcp_servers.gh.env]\nGITHUB_TOKEN = "ghp_FAKESECRET"\n')
    plan = codex_flag.plan(config, "codex-cli 0.114.0")
    assert plan.changes
    assert "ghp_FAKESECRET" in codex_flag.diff(plan)  # the full diff, shown before applying
    assert "ghp_FAKESECRET" not in codex_flag.diff(plan, context=0)
    source = (REPO_ROOT / "tintaview" / "install" / "doctor.py").read_text(encoding="utf-8")
    assert "codex_flag.diff(plan, context=0)" in source


# --------------------------------------------------------------------------- files


def test_an_unreadable_codex_config_is_never_replaced(tmp_path, monkeypatch):
    """Unreadable read as "empty" produced a `create` plan, and applying it replaced the
    user's whole config with two lines."""
    config = tmp_path / "config.toml"
    config.write_text('model = "o3"\n')
    real = Path.read_text

    def locked(self, *args, **kwargs):
        if self == config:
            raise PermissionError("locked")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    plan = codex_flag.plan(config, "codex-cli 0.114.0")
    assert not plan.changes and "could not be read" in plan.reason
    assert codex_flag.apply(plan) is None
    monkeypatch.undo()
    assert config.read_text() == 'model = "o3"\n'


@posix_only
def test_the_codex_flag_edit_keeps_the_files_permissions(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "o3"\n')
    config.chmod(0o600)
    backup = codex_flag.apply(codex_flag.plan(config, "codex-cli 0.114.0"))
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert backup is not None and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "codex_hooks = true" in config.read_text()


def _plan(path: Path) -> hooks.HookPlan:
    from tintaview.agents import base as agents_base

    adapter = agents_base.get("claude")

    class _At:
        def __getattr__(self, name):
            return getattr(adapter, name)

        def hooks_config_path(self, scope="user", project_dir=None):
            return path

    return hooks.plan_install(_At(), tmp_bin(path), "user", None)


def tmp_bin(path: Path) -> Path:
    return path.parent / "tv-hook.sh"


def test_a_plan_is_not_applied_over_a_file_that_changed_meanwhile(tmp_path):
    """An agent rewrites its own config constantly. Writing the approved plan over a
    file that moved on while the diff was on screen undid whatever changed in between."""
    settings = tmp_path / "settings.json"
    settings.write_text('{"theme": "dark"}\n')
    plan = _plan(settings)
    assert plan.changes
    settings.write_text('{"theme": "dark", "addedMeanwhile": true}\n')
    with pytest.raises(hooks.PlanStale):
        hooks.apply(plan)
    assert json.loads(settings.read_text()) == {"theme": "dark", "addedMeanwhile": True}
    assert hooks.apply(_plan(settings)) is not None  # planned again, it goes through
    assert json.loads(settings.read_text())["addedMeanwhile"] is True


def test_two_backups_in_one_second_do_not_overwrite_each_other(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("original\n")
    first = hooks._backup(settings)
    settings.write_text("edited\n")
    second = hooks._backup(settings)
    assert first != second
    assert first.read_text() == "original\n" and second.read_text() == "edited\n"


def test_a_hook_entry_that_is_not_a_list_is_refused_not_rewritten(tmp_path):
    settings = tmp_path / "settings.json"
    original = '{"hooks": {"PreToolUse": {"matcher": "*", "hooks": []}}}\n'
    settings.write_text(original)
    with pytest.raises(ValueError):
        _plan(settings)
    assert settings.read_text() == original


@posix_only
def test_a_symlinked_config_is_written_through_the_link(tmp_path):
    """Renaming over the link replaced a settings.json symlinked into a dotfiles
    repository with a regular file, and the repository's copy never changed."""
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("old\n")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    fsutil.atomic_write(link, "new\n")
    assert link.is_symlink() and real.read_text() == "new\n"


def test_a_config_with_the_wrong_shape_never_stops_the_tray(tmp_path):
    path = config_mod.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ('engine = "auto"\n', "engine = 1\nui = \"x\"\n", "agents = 1\n",
                 'agents = "x"\n', "colors = 5\n", "server = []\n", "[ui]\nclocks = 3\n",
                 '[agents]\nenabled = [1, "claude", ""]\n'):
        path.write_text(text)
        cfg = config_mod.load()
        assert cfg.enabled_agents == ["claude"], text


def test_whatever_is_saved_can_be_loaded_again():
    """An agent key with a space, or a command with a quote and a control character,
    was written as a file that no longer parsed — and the next load came back as all
    defaults, which the save after that wrote over the user's settings."""
    cfg = Config()
    cfg.enabled_agents = ["claude", "my agent", 'we"ird']
    cfg.notify.command = "echo 'a'\r\x01 b \"q\" \\ x\ty"
    cfg.escalation.command = "plain 'quoted' value"
    back = tomllib.loads(config_mod.dumps(cfg))
    assert back["agents"]["enabled"] == cfg.enabled_agents
    assert {"claude", "my agent", 'we"ird'} <= set(back["agents"])
    assert back["notify"]["command"] == cfg.notify.command
    assert back["escalation"]["command"] == cfg.escalation.command


def test_a_malformed_trend_or_cache_file_is_skipped_not_fatal(tmp_path):
    from tintaview.stats.cache import UsageCache
    from tintaview.stats.trend import TrendStore

    trend = tmp_path / "usage_trend.json"
    trend.write_text(json.dumps({"claude": {"5-hour limit": 5, "week": [[None, 1]],
                                            "ok": [[1.0, 2.0]]}}))
    store = TrendStore(trend)  # raised TypeError before
    assert store is not None

    cache = tmp_path / "usage_cache.json"
    cache.write_text(json.dumps({
        "claude": {"agent": "claude", "rows": [{"label": "5h", "pct": 10, "reset_at": "soon"}]},
        "codex": {"agent": "codex", "rows": [{"label": "5h", "pct": 10, "reset_at": 5}]}}))
    loaded = UsageCache(cache)
    assert loaded.get("claude") is None  # the entry with a non-numeric instant is dropped
    assert loaded.get("codex").rows[0].reset_at == 5.0


# --------------------------------------------------------------------------- untrusted data


def test_a_far_future_reset_instant_cannot_break_painting(monkeypatch):
    from tintaview.stats import format as fmt

    monkeypatch.setenv("TZ", "Europe/Warsaw")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        near_the_end = 253402300799.0  # 9999-12-31T23:59:59Z
        for style in (fmt.RESET_RELATIVE, fmt.RESET_DATE, fmt.RESET_RELATIVE_WEEK, fmt.RESET_DAYS):
            assert isinstance(fmt.reset_row_text(near_the_end, style), str)
        assert fmt.reset_row_text(float("inf")) == ""
    finally:
        monkeypatch.undo()
        if hasattr(time, "tzset"):
            time.tzset()


def test_an_exponent_from_the_api_cannot_stall_the_process():
    from tintaview.stats.providers import claude

    started = time.monotonic()
    assert claude._money({"amount_minor": 1234, "exponent": 40_000_000}) is None
    assert claude._money({"amount_minor": 1234, "exponent": "2"}) is None
    assert claude._money({"amount_minor": 1234, "exponent": 2}) == (12.34, "USD")
    assert time.monotonic() - started < 1


def test_one_codex_timestamp_without_a_zone_does_not_break_the_rest():
    from tintaview.stats.providers import codex

    naive = codex._parse_ts("2026-11-01T10:00:00")
    aware = codex._parse_ts("2026-11-01T10:00:00Z")
    assert naive == aware and (aware - naive).total_seconds() == 0


def test_the_lighting_sdks_are_only_ever_spoken_to_on_this_machine(tmp_path):
    """Whatever answers on the Chroma port names where every later write goes, and so
    does a file under ProgramData for GameSense."""
    from tintaview.engines import chroma, steelseries

    assert chroma._is_local_http("http://localhost:54235/chromasdk")
    assert chroma._is_local_http("http://127.0.0.1:60000/x")
    for uri in ("http://evil.example/x", "file:///etc/passwd", "ftp://127.0.0.1/x",
                "http://127.0.0.1.evil.example/x", None, 5, "http://[x"):
        assert not chroma._is_local_http(uri), uri

    props = tmp_path / "coreProps.json"
    for address, expected in (("127.0.0.1:51234", "127.0.0.1:51234"),
                              ("localhost:8080", "localhost:8080"),
                              ("evil.example:80", None), ("10.0.0.5:80", None),
                              ("127.0.0.1:80/../x", None), (5, None)):
        props.write_text(json.dumps({"address": address}))
        assert steelseries.read_address(str(props)) == expected, address


# --------------------------------------------------------------------------- update, tools


def test_the_downloaded_installer_never_sits_directly_in_the_shared_temp_folder(tmp_path,
                                                                               monkeypatch):
    """install.sh treats "a pyproject.toml one directory above me" as a checkout. One
    level above a file directly inside mkdtemp() is the machine's shared temp folder."""
    monkeypatch.setattr(update_mod.tempfile, "tempdir", str(tmp_path))
    seen: dict = {}

    def fake(url, checksums_url, dest_dir, filename):
        seen["dir"] = dest_dir
        return dest_dir / filename

    monkeypatch.setattr(update_mod, "_download_and_verify", fake)
    prefix = update_mod.RELEASE_DOWNLOAD_PREFIX + "v9.9.9/"
    assets = [{"name": n, "browser_download_url": prefix + n}
              for n in ("install.sh", "SHA256SUMS.txt")]
    script = update_mod._fetch_install_script(assets, "install.sh")
    assert script.parent.parent.parent == tmp_path  # tmp/<private>/<run>/install.sh
    assert script.parent.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(script.parent.parent.stat().st_mode) == 0o700


def test_install_sh_only_accepts_a_real_checkout():
    sh = (REPO_ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
    assert '[ "$(basename -- "$script_dir")" = "packaging" ]' in sh
    assert '[ -f "$script_dir/../tintaview/__init__.py" ]' in sh


@pytest.mark.parametrize("installed,offered,expected", [
    ("1.2.0rc1", "1.2.0", -1), ("1.2.0", "1.2.0rc1", 1), ("1.2.0rc1", "1.2.0rc2", -1),
    ("1.2.0-rc1", "1.2.0", -1), ("1.2.0", "1.2.0", 0), ("0.9.0", "0.10.0", -1),
    ("1.2.0.dev3", "1.2.0", -1),
])
def test_a_prerelease_build_is_offered_the_final_release(installed, offered, expected):
    """An installed build reports the PEP 440 spelling (`1.2.0rc1`, no dash), which
    compared equal to `1.2.0` — so it was never offered the update."""
    assert update_mod.compare_versions(installed, offered) == expected


def test_odd_version_tags_and_checksum_lines_are_handled():
    assert update_mod.compare_versions("1.²", "1.0") in (-1, 0, 1)  # raised ValueError
    sums = "aaaa  evil/dir/install.sh\nbbbb  install.sh\n"
    assert update_mod._parse_checksums(sums, "install.sh") == "bbbb"
    assert update_mod._parse_checksums("aaaa  evil/dir/install.sh\n", "install.sh") is None


def test_windows_system_tools_are_never_taken_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(proc.sys, "platform", "win32")
    system32 = tmp_path / "Windows" / "System32"
    (system32 / "WindowsPowerShell" / "v1.0").mkdir(parents=True)
    (system32 / "wsl.exe").write_text("")
    (system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe").write_text("")
    monkeypatch.setenv("SYSTEMROOT", str(tmp_path / "Windows"))
    hostile = tmp_path / "cloned-repo"
    hostile.mkdir()
    for name in ("powershell.exe", "wsl.exe", "winget.exe"):
        (hostile / name).write_text("")
    monkeypatch.chdir(hostile)
    monkeypatch.setattr(proc.shutil, "which", lambda name: str(hostile / name))

    assert proc.system_tool("wsl.exe") == str(system32 / "wsl.exe")
    assert proc.system_tool("powershell.exe") == str(
        system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    assert proc.system_tool("winget.exe") is None  # only found in the working directory


def test_every_python_launcher_and_installer_call_is_isolated():
    """`python -m x` and `python -c` put the working directory first on sys.path. Derived
    from the files: every interpreter call in the installers carries -I, every launcher
    the app writes or starts carries -P."""
    import re

    sh = (REPO_ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
    ps1 = (REPO_ROOT / "packaging" / "install.ps1").read_text(encoding="utf-8")
    for line in sh.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in re.finditer(r'"\$(?:PY|VENV_PY|candidate|FIRST_PY)" (-\S+)', line):
            assert match.group(1) == "-I", line
    for line in ps1.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in re.finditer(r'& \$(?:VenvPython|python\.Exe|Exe) (-\S+)', line):
            assert match.group(1) == "-I", line
    assert "'-P -m tintaview'" in ps1 and '" -P -m tintaview' in ps1

    from tintaview.install import mcp

    assert mcp.launcher("claude", "python")["args"][0] == "-P"
    package = REPO_ROOT / "tintaview"
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'\[[^\[\]\n]*"-m",\s*"tintaview[^\]\n]*\]', text):
            assert '"-P"' in match.group(0) or '"-I"' in match.group(0), (path.name, match.group(0))


def test_the_installers_only_fall_back_when_pip_says_the_lock_does_not_fit():
    """A dropped connection or an index that does not answer used to take the unpinned
    path too — so whoever could make a download fail chose what was installed."""
    for name in ("install.sh", "install.ps1"):
        text = (REPO_ROOT / "packaging" / name).read_text(encoding="utf-8")
        assert "require a different python version|Hashes are required in --require-hashes mode|all requirements must have their versions pinned" in text
        assert "Could not download" in text
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "--upgrade pip" not in code, f"{name} upgrades pip from the index, unpinned"


def test_the_release_is_built_where_no_test_tooling_ever_ran():
    import yaml

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "build.yml").read_text())
    jobs = workflow["jobs"]
    assert jobs["build"]["needs"] == "test" and jobs["verify"]["needs"] == "build"
    assert set(jobs["publish"]["needs"]) == {"build", "verify"}
    build_script = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    installs = [line.strip() for line in build_script.splitlines()
                if "pip install" in line and not line.lstrip().startswith("#")]
    assert installs == ["python -I -m pip install --require-hashes --only-binary :all: "
                        "-r packaging/build-requirements.txt"]
    assert "pytest" not in build_script and "pip-audit" not in build_script
    assert "--no-isolation" in build_script
    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in jobs[name] for name in ("test", "build", "verify"))

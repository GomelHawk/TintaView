"""The notify tool: the MCP server (`core/mcp.py`), the daemon's `POST /v1/notify`, and
registering the server in each agent's config (`install/mcp.py`)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tintaview.core import config as config_mod
from tintaview.core import mcp
from tintaview.core.config import Config
from tintaview.core.server import MAX_NOTIFY_CHARS, StatusServer
from tintaview.engines.null import NullEngine
from tintaview.install import hooks as hooks_mod
from tintaview.install import mcp as mcp_install

# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def daemon():
    """A real status server on an ephemeral port, its notify callback recorded."""
    from tintaview.core.controller import LightController

    cfg = Config()
    cfg.server.port = 0
    server = StatusServer(cfg, controller=LightController(cfg, engine=NullEngine()))
    assert server.start()
    received: list[tuple[str, str, str]] = []
    server.on_notify = lambda agent, message, cwd: received.append((agent, message, cwd))
    try:
        yield server, received
    finally:
        server.stop()


def _post_notify(server: StatusServer, body: bytes, agent: str = "claude") -> tuple[int, dict]:
    request = urllib.request.Request(f"{server.url}/v1/notify?agent={agent}", data=body,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=2) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _call(server: mcp.Server, message: str | None, msg_id: int = 7) -> dict:
    arguments = {} if message is None else {"message": message}
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                          "params": {"name": mcp.TOOL_NAME, "arguments": arguments}})


# --------------------------------------------------------------------------- endpoint


def test_notify_endpoint_hands_the_message_to_the_tray(daemon):
    server, received = daemon
    status, reply = _post_notify(server, json.dumps(
        {"message": "Migration done\n\n142 tables", "cwd": "/work"}).encode(), agent="codex")
    assert (status, reply) == (200, {"ok": True, "notified": True})
    # One line, like `detail`: `cmd` would cut `%TINTAVIEW_MESSAGE%` at a newline.
    assert received == [("codex", "Migration done ⏎ 142 tables", "/work")]


def test_notify_endpoint_caps_the_message(daemon):
    server, received = daemon
    _post_notify(server, json.dumps({"message": "x" * 5000}).encode())
    assert len(received[0][1]) == MAX_NOTIFY_CHARS


def test_notify_endpoint_answers_false_when_nothing_can_show_it(daemon):
    """Headless registers no callback — the agent must hear that nothing went out."""
    server, received = daemon
    server.on_notify = None
    assert _post_notify(server, b'{"message": "hi"}') == (200, {"ok": True, "notified": False})


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"message": "   "}', b"{}"])
def test_notify_endpoint_refuses_a_bad_body(daemon, body):
    server, received = daemon
    status, reply = _post_notify(server, body)
    assert status == 400 and reply["ok"] is False
    assert received == []


def test_notify_endpoint_never_takes_an_unsafe_agent_key(daemon):
    server, received = daemon
    _post_notify(server, b'{"message": "hi"}', agent="..%2F%3Cx%3E")
    assert received[0][0] == "claude"


def test_notify_changes_no_status(daemon):
    server, _received = daemon
    _post_notify(server, b'{"message": "hi"}')
    assert server.state_payload()["effective"] == "none"


# --------------------------------------------------------------------------- protocol


def test_initialize_echoes_the_clients_protocol_version():
    server = mcp.Server("claude", base_url="http://127.0.0.1:1")
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "x", "version": "1"}}})
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == "2025-11-25"
    assert reply["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert reply["result"]["serverInfo"]["name"] == "tintaview"

    bare = server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize"})
    assert bare["result"]["protocolVersion"] == mcp.DEFAULT_PROTOCOL_VERSION


def test_tools_list_offers_exactly_the_notify_tool():
    reply = mcp.Server("claude").handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    (tool,) = reply["result"]["tools"]
    assert tool["name"] == "notify_user"
    assert tool["inputSchema"]["required"] == ["message"]
    # The description is what makes the model pick it for "notify me via phone".
    assert "notif" in tool["description"] and "phone" in tool["description"]


def test_notifications_get_no_reply_and_unknown_methods_an_error():
    server = mcp.Server("claude")
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})["result"] == {}
    reply = server.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
    assert reply["error"]["code"] == mcp.METHOD_NOT_FOUND


def test_a_call_reaches_the_daemon_and_reports_delivery(daemon):
    server, received = daemon
    reply = _call(mcp.Server("cursor", base_url=server.url, cwd="/proj"), "Tests are green")
    assert reply["result"]["isError"] is False
    assert received == [("cursor", "Tests are green", "/proj")]


def test_a_call_says_so_when_nothing_showed_it(daemon):
    server, _received = daemon
    server.on_notify = None
    result = _call(mcp.Server("claude", base_url=server.url), "hi")["result"]
    assert result["isError"] is True
    assert "without its tray" in result["content"][0]["text"]


def test_a_call_says_so_when_tintaview_is_not_running():
    result = _call(mcp.Server("claude", base_url="http://127.0.0.1:9"), "hi")["result"]
    assert result["isError"] is True
    assert "not running" in result["content"][0]["text"]


def test_a_call_without_a_message_is_refused_before_the_daemon():
    result = _call(mcp.Server("claude", base_url="http://127.0.0.1:9"), None)["result"]
    assert result["isError"] is True and "message" in result["content"][0]["text"]


def test_serve_speaks_one_message_per_line_and_nothing_else():
    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        b"\n"
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        b"garbage\n"
    )
    stdout = io.BytesIO()
    mcp.serve(mcp.Server("claude"), stdin, stdout)
    lines = stdout.getvalue().decode().splitlines()
    assert [json.loads(line).get("id") for line in lines] == [1, None]
    assert json.loads(lines[1])["error"]["code"] == mcp.PARSE_ERROR


def test_the_module_runs_as_the_agents_launch_it(daemon, tmp_path):
    """`python -m tintaview.core.mcp <agent>` — exactly the launcher the registration
    writes — finding the daemon through TintaView's own config file."""
    server, received = daemon
    cfg = Config()
    cfg.server.port = int(server.url.rsplit(":", 1)[1])
    config_mod.save(cfg, tmp_path / "config.toml")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "notify_user", "arguments": {"message": "done"}}},
    ]
    # The package's own checkout on the path: a dev run may not have it installed.
    repo = str(Path(mcp.__file__).resolve().parents[2])
    env = {**os.environ, "TINTAVIEW_HOME": str(tmp_path),
           "PYTHONPATH": os.pathsep.join(filter(None, [repo, os.environ.get("PYTHONPATH")]))}
    launch = mcp_install.launcher("codex", sys.executable)
    proc = subprocess.run(
        [launch["command"], *launch["args"]],
        input="".join(json.dumps(r) + "\n" for r in requests).encode(),
        capture_output=True, timeout=30, env=env, cwd=tmp_path, check=True,
    )
    replies = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert replies[1]["result"]["isError"] is False
    assert received == [("codex", "done", str(tmp_path))]


# --------------------------------------------------------------------------- registration


PY = "/opt/tintaview/venv/bin/python"


def _home(tmp_path: Path, key: str) -> Path:
    home = tmp_path / f".{key}"
    home.mkdir(exist_ok=True)
    return home


def _apply(plans: list[hooks_mod.HookPlan]) -> None:
    for plan in plans:
        hooks_mod.apply(plan)


def test_claude_registration_is_lossless_idempotent_and_reversible(tmp_path):
    home = _home(tmp_path, "claude")
    state = mcp_install.claude_state_path(home)
    assert state == tmp_path / ".claude.json"  # beside ~/.claude, not in it
    # Claude Code's own serialisation: 2-space, non-ASCII kept, no trailing newline.
    original_state = json.dumps({"numStartups": 3, "userName": "Zoë",
                                 "mcpServers": {"gitlab": {"command": "npx"}}}, indent=2,
                                ensure_ascii=False)
    state.write_text(original_state, encoding="utf-8")
    settings = home / "settings.json"
    original_settings = json.dumps({"permissions": {"allow": ["Bash(git *)"]},
                                    "theme": "dark"}, indent=2) + "\n"
    settings.write_text(original_settings, encoding="utf-8")

    plans = mcp_install.plan_install("claude", home, PY)
    assert [p.path for p in plans] == [state, settings]
    _apply(plans)

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["numStartups"] == 3 and data["userName"] == "Zoë"
    assert data["mcpServers"]["gitlab"] == {"command": "npx"}
    assert data["mcpServers"]["tintaview"] == {
        "type": "stdio", "command": PY, "args": ["-m", "tintaview.core.mcp", "claude"],
        "env": {}}
    assert not state.read_text(encoding="utf-8").endswith("\n")
    allow = json.loads(settings.read_text())["permissions"]["allow"]
    assert allow == ["Bash(git *)", "mcp__tintaview__notify_user"]
    assert mcp_install.status("claude", home, PY) == hooks_mod.STATUS_INSTALLED

    assert all(not p.changes for p in mcp_install.plan_install("claude", home, PY))

    _apply(mcp_install.plan_uninstall("claude", home))
    assert state.read_text(encoding="utf-8") == original_state
    assert settings.read_text() == original_settings
    assert mcp_install.status("claude", home, PY) == hooks_mod.STATUS_MISSING


def test_codex_registration_keeps_the_users_toml_and_auto_approves(tmp_path):
    home = _home(tmp_path, "codex")
    config = home / "config.toml"
    original = ('# my settings\nmodel = "gpt-5"  # keep\n\n'
                '[mcp_servers.docs]\ncommand = "docs-mcp"\n')
    config.write_text(original)

    _apply(mcp_install.plan_install("codex", home, PY))
    text = config.read_text()
    assert text.startswith(original)  # comments and the user's own server untouched
    import tomllib

    entry = tomllib.loads(text)["mcp_servers"]["tintaview"]
    assert entry["command"] == PY
    assert entry["args"] == ["-m", "tintaview.core.mcp", "codex"]
    assert entry["tools"]["notify_user"]["approval_mode"] == "approve"
    assert all(not p.changes for p in mcp_install.plan_install("codex", home, PY))

    _apply(mcp_install.plan_uninstall("codex", home))
    assert config.read_text() == original


def test_codex_registration_on_a_fresh_file_writes_no_empty_parent_table(tmp_path):
    home = _home(tmp_path, "codex")
    _apply(mcp_install.plan_install("codex", home, PY))
    assert "[mcp_servers]\n" not in (home / "config.toml").read_text()


def test_cursor_registration(tmp_path):
    home = _home(tmp_path, "cursor")
    path = home / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}, indent=2) + "\n")

    _apply(mcp_install.plan_install("cursor", home, PY))
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["tintaview"] == {"command": PY,
                                    "args": ["-m", "tintaview.core.mcp", "cursor"]}

    _apply(mcp_install.plan_uninstall("cursor", home))
    assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "x"}}}


def test_copilot_registration(tmp_path):
    """Copilot reads `~/.copilot/mcp-config.json`: a `local` server, with `tools` naming
    the one tool the model may see. Only our entry is added, and only it is removed."""
    home = _home(tmp_path, "copilot")
    path = home / "mcp-config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}, indent=2) + "\n")

    plans = mcp_install.plan_install("copilot", home, PY)
    assert [p.path for p in plans] == [path]
    assert any("--allow-tool='tintaview(notify_user)'" in n for n in plans[0].notes)
    _apply(plans)
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["tintaview"] == {"type": "local", "command": PY,
                                    "args": ["-m", "tintaview.core.mcp", "copilot"],
                                    "env": {}, "tools": ["notify_user"]}
    assert mcp_install.status("copilot", home, PY) == hooks_mod.STATUS_INSTALLED
    assert mcp_install.plan_install("copilot", home, PY)[0].action == hooks_mod.ACTION_NOOP

    _apply(mcp_install.plan_uninstall("copilot", home))
    assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "x"}}}


@pytest.mark.parametrize("key", mcp_install.SUPPORTED)
def test_a_registration_for_another_interpreter_is_stale(tmp_path, key):
    home = _home(tmp_path, key)
    _apply(mcp_install.plan_install(key, home, "/old/python"))
    assert mcp_install.status(key, home, PY) == hooks_mod.STATUS_STALE_PATH
    # ...and reinstalling moves it, rather than adding a second entry.
    _apply(mcp_install.plan_install(key, home, PY))
    assert mcp_install.status(key, home, PY) == hooks_mod.STATUS_INSTALLED


def test_claude_with_the_server_but_no_permission_is_partial(tmp_path):
    home = _home(tmp_path, "claude")
    hooks_mod.apply(mcp_install.plan_install("claude", home, PY)[0])
    assert mcp_install.status("claude", home, PY) == hooks_mod.STATUS_PARTIAL


def test_a_file_that_will_not_parse_is_unreadable_and_never_rewritten(tmp_path):
    home = _home(tmp_path, "cursor")
    (home / "mcp.json").write_text("{ not json")
    assert mcp_install.status("cursor", home, PY) == hooks_mod.STATUS_UNREADABLE
    with pytest.raises(ValueError):
        mcp_install.plan_install("cursor", home, PY)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_private_file_stays_private(tmp_path):
    """`~/.claude.json` is 0600; rewriting it (or backing it up) must not widen that."""
    home = _home(tmp_path, "claude")
    state = mcp_install.claude_state_path(home)
    state.write_text("{}")
    state.chmod(0o600)
    _apply(mcp_install.plan_install("claude", home, PY))
    assert state.stat().st_mode & 0o777 == 0o600
    (backup,) = tmp_path.glob(".claude.json" + hooks_mod.BACKUP_SUFFIX + "*")
    assert backup.stat().st_mode & 0o777 == 0o600


def test_the_launcher_is_never_pythonw(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    assert mcp_install.console_python(str(scripts / "pythonw.exe")) == str(scripts / "python.exe")
    assert mcp_install.console_python("/usr/bin/python3") == "/usr/bin/python3"


def test_agent_home_follows_the_configured_home(tmp_path):
    from tintaview.agents import base as agents_base

    cfg = Config()
    adapter = agents_base.get("claude")
    assert mcp_install.agent_home(cfg, adapter) == adapter.default_home()
    cfg.agent("claude").home = str(tmp_path / "elsewhere" / ".claude")
    assert mcp_install.agent_home(cfg, adapter) == tmp_path / "elsewhere" / ".claude"


def test_the_distro_is_given_the_windows_path_with_forward_slashes(monkeypatch):
    """`wsl.exe` passes its arguments through a shell that strips backslashes."""
    from tintaview.install import wsl

    seen: list[list[str]] = []
    monkeypatch.setattr(mcp_install, "console_python",
                        lambda: r"C:\Users\me\AppData\Local\TintaView\venv\Scripts\python.exe")
    monkeypatch.setattr(wsl, "run_in", lambda distro, argv: seen.append(argv) or "/mnt/c/x\n")

    assert mcp_install.wsl_python("Ubuntu") == "/mnt/c/x"
    assert seen == [["wslpath", "-u",
                     "C:/Users/me/AppData/Local/TintaView/venv/Scripts/python.exe"]]


def test_a_hand_formatted_file_is_announced_as_reformatted(tmp_path):
    home = _home(tmp_path, "cursor")
    (home / "mcp.json").write_text('{"mcpServers": {"other": {"args": ["-y", "x"]}}}\n')
    (plan,) = mcp_install.plan_install("cursor", home, PY)
    assert any("reformatted" in note for note in plan.notes)

    hooks_mod.apply(plan)  # ours now: 2-space already, so no note the second time round
    (again,) = mcp_install.plan_install("cursor", home, "/other/python")
    assert not any("reformatted" in note for note in again.notes)

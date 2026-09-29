"""TintaView's MCP server: one tool, ``notify_user``, for an agent asked to notify you.

    python -m tintaview.core.mcp <agent>

The agent launches this over stdio (newline-delimited JSON-RPC 2.0, the MCP stdio
transport) because `install/mcp.py` registered it in the agent's own MCP config. Every
session then lists the tool, and the model calls it when the user says something like
"notify me via phone when this is done" — the agent needs no instruction, path or
command beyond that sentence, and never sees how the message is delivered: the tray
shows it and runs `notify.command`, which lives only in TintaView's config.

Deliberately thin. It forwards each call to the running daemon's `POST /v1/notify` and
reports back whether anything showed it, so an agent never tells the user a
notification went out when it did not. Stdlib only, like the rest of `core/`: in a
WSL-split install it is the *Windows* interpreter, launched from inside the distro
through WSL interop, that runs this — so loopback reaches the Windows-side daemon with
no firewall rule, the same trick `tv-hook` gets from `curl.exe`.

The module path is baked into every agent config the registration writes, exactly like
the `tv-hook` path, so it must never move.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any

from .. import __version__

SERVER_NAME = "tintaview"
TOOL_NAME = "notify_user"

#: Answered when the client names no protocol version. A client that names one gets it
#: echoed back: this server offers one tool and nothing else, which reads the same in
#: every revision of the protocol so far.
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

#: The daemon answers `/v1/notify` as soon as a Qt signal is emitted; anything slower
#: than this is a daemon that is not really there.
DAEMON_TIMEOUT_S = 3.0

#: What the model reads to decide when to call the tool — the whole of the "agent needs
#: no instruction" promise rests on it, so it says when to call, when not to, and what a
#: useful message looks like.
TOOL_DESCRIPTION = (
    "Send the user a notification: a pop-up on their desktop and, if they have set one "
    "up, a push to their phone or another channel. Use it when the user asks to be "
    "notified, pinged, alerted or told about something — typically when a long task "
    "finishes (\"notify me when done\", \"ping me on my phone when the build is green\"). "
    "Call it once, when the thing they asked about happens, with a short self-contained "
    "message that says what happened and how it turned out. Do not call it unless the "
    "user asked to be notified."
)

TOOL = {
    "name": TOOL_NAME,
    "title": "Notify the user",
    "description": TOOL_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "What to tell the user, in a sentence or two — e.g. "
                               "\"Migration finished: 142 tables, no errors.\"",
            },
        },
        "required": ["message"],
    },
    "annotations": {
        "title": "Notify the user",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
}

# JSON-RPC error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def daemon_url() -> str:
    """Base URL of the daemon this machine's TintaView config names.

    Falls back to the stock loopback port on any config problem: a broken config is
    `doctor`'s to report, and this process has nowhere to report it but the agent.
    """
    try:
        from . import config as config_mod

        cfg = config_mod.load()
        return f"http://{cfg.server.host}:{cfg.server.port}"
    except Exception:
        return "http://127.0.0.1:8777"


class Server:
    """The protocol, minus the transport — `handle` takes one decoded message and returns
    the reply (or None for a notification), so tests drive it without pipes."""

    def __init__(self, agent: str, base_url: str | None = None, cwd: str | None = None) -> None:
        self.agent = agent
        self._base_url = base_url
        self._cwd = cwd

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            self._base_url = daemon_url()
        return self._base_url

    def handle(self, message: Any) -> Any:
        if isinstance(message, list):
            replies = [r for r in (self.handle(m) for m in message) if r is not None]
            return replies or None
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return _error(message.get("id") if isinstance(message, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC request")
        if "id" not in message:
            return None  # a notification (`notifications/initialized`, …): no reply
        msg_id = message["id"]
        method = message["method"]
        params = message.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            return _result(msg_id, {
                "protocolVersion": requested if isinstance(requested, str) and requested
                else DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "TintaView",
                               "version": __version__},
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": [TOOL]})
        if method == "tools/call":
            if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
                return _error(msg_id, INVALID_PARAMS, f"unknown tool; the only one is {TOOL_NAME}")
            arguments = params.get("arguments") or {}
            text = arguments.get("message") if isinstance(arguments, dict) else None
            if not isinstance(text, str) or not text.strip():
                return _result(msg_id, _tool_text("`message` must be a non-empty string.",
                                                  error=True))
            return _result(msg_id, self._notify(text))
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _notify(self, text: str) -> dict:
        cwd = self._cwd if self._cwd is not None else _safe_cwd()
        body = json.dumps({"message": text, "cwd": cwd}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/notify?agent={urllib.parse.quote(self.agent)}", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=DAEMON_TIMEOUT_S) as resp:
                reply = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return _tool_text(f"TintaView refused the notification (HTTP {exc.code}).",
                              error=True)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            return _tool_text("TintaView is not running, so the notification could not be "
                              "delivered. Tell the user instead.", error=True)
        if isinstance(reply, dict) and reply.get("notified"):
            return _tool_text("Notification delivered.")
        return _tool_text("TintaView is running without its tray, so there was nothing to "
                          "show the notification. Tell the user instead.", error=True)


def _safe_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def _tool_text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(server: Server, stdin: IO[bytes], stdout: IO[bytes]) -> None:
    """Read one JSON-RPC message per line until EOF, answering each on stdout.

    Nothing else may ever be written to stdout: it is the protocol channel.
    """
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            reply: Any = _error(None, PARSE_ERROR, "invalid JSON")
        else:
            reply = server.handle(message)
        if reply is None:
            continue
        stdout.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    agent = args[0] if args else "claude"
    with contextlib.suppress(KeyboardInterrupt):
        serve(Server(agent), sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main())

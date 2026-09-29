"""Registering TintaView's MCP server (`core/mcp.py`) in each agent's own MCP config.

Registration is what lets a user say "notify me via phone when it's done" and nothing
else: the agent lists the ``notify_user`` tool in every session and picks it by its
description. It edits files the user owns, so it follows the hook merge's rules
(`install/hooks.py`) exactly — plan, show the diff, apply on an explicit yes, back up,
write atomically, stay idempotent, and only ever touch what TintaView wrote:

| Agent  | File                     | What is ours                                          |
| ------ | ------------------------ | ----------------------------------------------------- |
| Claude | ``~/.claude.json``       | ``mcpServers.tintaview``                              |
| Claude | ``~/.claude/settings.json`` | ``permissions.allow`` entry ``mcp__tintaview__notify_user`` |
| Codex  | ``~/.codex/config.toml`` | ``[mcp_servers.tintaview]`` and its ``tools`` table   |
| Cursor | ``~/.cursor/mcp.json``   | ``mcpServers.tintaview``                              |

The allow entry and Codex's ``approval_mode = "approve"`` exist because the call comes
at the end of a long task, when nobody is watching: a tool that stops to ask permission
to send the notification defeats the notification. Cursor's own approval setting lives
in its UI and is not touched.

``~/.claude.json`` is Claude Code's whole state file, not a config the user writes; it
is re-serialised exactly as Claude writes it (2-space JSON, no ASCII escaping, the same
trailing newline), which keeps the diff to the lines TintaView adds. It sits *beside*
``~/.claude``, not in it, which is why every path here is derived from the agent's home.

The launcher written is the interpreter that runs TintaView plus ``-m
tintaview.core.mcp <agent>``. In a WSL-split install it is the *Windows* ``python.exe``
under its ``/mnt/c/…`` path, so the in-distro agent starts it through WSL interop and
it reaches the Windows-side daemon on loopback.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..core import mcp as mcp_server
from .hooks import (
    ACTION_CREATE,
    ACTION_NOOP,
    ACTION_REMOVE,
    ACTION_UPDATE,
    BACKUP_SUFFIX,
    STATUS_INSTALLED,
    STATUS_MISSING,
    STATUS_PARTIAL,
    STATUS_STALE_PATH,
    STATUS_UNREADABLE,
    HookPlan,
)

SERVER_KEY = mcp_server.SERVER_NAME
MODULE = "tintaview.core.mcp"
#: Claude Code's permission rule for exactly this one tool (``mcp__<server>__<tool>``).
CLAUDE_ALLOW_RULE = f"mcp__{SERVER_KEY}__{mcp_server.TOOL_NAME}"

#: Agents that can be registered. Stats-only providers have no agent to call a tool.
SUPPORTED = ("claude", "codex", "cursor")


def console_python(executable: str | None = None) -> str:
    """The interpreter to launch the MCP server with: this one, but never ``pythonw``.

    The tray runs under ``pythonw.exe``, which has no standard streams to speak MCP over;
    its console sibling sits in the same ``Scripts`` directory.
    """
    exe = Path(executable or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.exists() or executable is not None:
            return str(sibling)
    return str(exe)


def launcher(agent_key: str, python: str) -> dict[str, Any]:
    """The ``command`` + ``args`` pair every agent's MCP config takes."""
    return {"command": python, "args": ["-m", MODULE, agent_key]}


def agent_home(cfg: Any, adapter: Any) -> Path:
    """The agent's home as the config records it (a WSL-split UNC path, a hand-edited
    ``home``), else the adapter's default — never a bare ``Path.home()`` guess."""
    from ..core import config as config_mod

    acfg = cfg.agents.get(adapter.key) if hasattr(cfg, "agents") else None
    return config_mod.expand(acfg.home) if acfg is not None and acfg.home \
        else adapter.default_home()


def wsl_python(distro: str) -> str:
    """This Windows interpreter as the distro sees it (``/mnt/c/…/python.exe``)."""
    from . import wsl

    # Forward slashes: `wsl.exe` hands its arguments to a shell that eats backslashes
    # (measured — `wslpath` received `C:UsersdmitrAppData…`), and `wslpath` reads
    # `C:/Users/...` the same as a backslashed path.
    return wsl.run_in(distro, ["wslpath", "-u", console_python().replace("\\", "/")]).strip()


# --------------------------------------------------------------------------- files


def claude_state_path(home: Path) -> Path:
    """``~/.claude.json`` for an agent home of ``~/.claude``."""
    return home.parent / f"{home.name}.json"


def config_paths(agent_key: str, home: Path) -> list[Path]:
    """Every file registration touches for this agent, in the order it plans them."""
    if agent_key == "claude":
        return [claude_state_path(home), home / "settings.json"]
    if agent_key == "codex":
        return [home / "config.toml"]
    if agent_key == "cursor":
        return [home / "mcp.json"]
    return []


def _read(path: Path) -> str:
    """The file's text, "" when it doesn't exist. Every other failure propagates, for
    the same reason as `hooks._read_json`: unreadable must never look like empty."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_json(path: Path, text: str) -> dict:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc}); fix or move it, then retry") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _dump_json(data: dict, like: str) -> str:
    """`data` serialised the way the file already was: Claude Code writes ``~/.claude.json``
    with no trailing newline, the hook merge writes ``settings.json`` with one."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return text + "\n" if (not like or like.endswith("\n")) else text


def _json_plan(agent_key: str, path: Path, change, notes: list[str]) -> HookPlan:
    """Plan an edit of a JSON file: `change(data)` mutates the parsed object in place."""
    before = _read(path)
    data = _load_json(path, before)
    original = json.dumps(data, sort_keys=True)
    change(data)
    if json.dumps(data, sort_keys=True) == original:
        return HookPlan(agent_key, path, ACTION_NOOP, before, before, notes)
    after = _dump_json(data, before)
    if before and _dump_json(json.loads(before), before) != before:
        # Same courtesy as the hook merge: say up front that the rewrite reformats a
        # hand-formatted file, rather than letting the diff surprise someone.
        notes = [*notes, f"This file will be reformatted to standard 2-space JSON. A backup "
                         f"is kept next to it as *{BACKUP_SUFFIX}<timestamp>."]
    if not before:
        action = ACTION_CREATE
    elif not data or len(after) < len(before):
        action = ACTION_REMOVE
    else:
        action = ACTION_UPDATE
    return HookPlan(agent_key, path, action, before, after, notes)


# --------------------------------------------------------------------------- planning


def plan_install(agent_key: str, home: Path, python: str) -> list[HookPlan]:
    """The edits that register the server for one agent — one plan per file, all of
    them for one confirmation. Raises `ValueError` for a file that won't parse."""
    entry = launcher(agent_key, python)
    note = ("Adds TintaView's notify_user tool, so asking the agent to \"notify me when "
            "it's done\" reaches TintaView. New sessions pick it up.")
    if agent_key == "claude":
        def add_server(data: dict) -> None:
            servers = data.get("mcpServers")
            servers = dict(servers) if isinstance(servers, dict) else {}
            servers[SERVER_KEY] = {"type": "stdio", **entry, "env": {}}
            data["mcpServers"] = servers

        def allow(data: dict) -> None:
            perms = data.get("permissions")
            perms = dict(perms) if isinstance(perms, dict) else {}
            rules = list(perms.get("allow") or []) if isinstance(perms.get("allow"), list) else []
            if CLAUDE_ALLOW_RULE not in rules:
                rules.append(CLAUDE_ALLOW_RULE)
            perms["allow"] = rules
            data["permissions"] = perms

        state, settings = config_paths(agent_key, home)
        return [
            _json_plan(agent_key, state, add_server, [note]),
            _json_plan(agent_key, settings, allow, [
                "Lets the tool run without asking — it is called when a task ends, "
                "when nobody may be there to approve it."]),
        ]
    if agent_key == "codex":
        return [_codex_plan(config_paths(agent_key, home)[0], entry, note)]
    if agent_key == "cursor":
        def add_cursor(data: dict) -> None:
            servers = data.get("mcpServers")
            servers = dict(servers) if isinstance(servers, dict) else {}
            servers[SERVER_KEY] = dict(entry)
            data["mcpServers"] = servers

        return [_json_plan(agent_key, config_paths(agent_key, home)[0], add_cursor, [
            note, "Cursor may ask you to approve the tool the first time it runs."])]
    return []


def _codex_plan(path: Path, entry: dict[str, Any], note: str) -> HookPlan:
    import tomlkit

    before = _read(path)
    doc = tomlkit.parse(before) if before.strip() else tomlkit.document()
    if _codex_matches(doc, entry):
        return HookPlan("codex", path, ACTION_NOOP, before, before, [note])
    servers = doc.get("mcp_servers")
    if servers is None:
        # A super-table, so a file with no other server gains `[mcp_servers.tintaview]`
        # and not an empty `[mcp_servers]` header above it.
        servers = tomlkit.table(is_super_table=True)
        doc["mcp_servers"] = servers
    table = tomlkit.table()
    table["command"] = entry["command"]
    table["args"] = entry["args"]
    tool = tomlkit.table()
    tool["approval_mode"] = "approve"
    tools = tomlkit.table(is_super_table=True)
    tools[mcp_server.TOOL_NAME] = tool
    table["tools"] = tools
    servers[SERVER_KEY] = table
    after = tomlkit.dumps(doc)
    return HookPlan("codex", path, ACTION_CREATE if not before.strip() else ACTION_UPDATE,
                    before, after, [note, "Lets the tool run without asking — it is called "
                                          "when a task ends, when nobody may be there."])


def _codex_entry(doc: Any) -> Any:
    servers = doc.get("mcp_servers") if doc is not None else None
    return servers.get(SERVER_KEY) if hasattr(servers, "get") else None


def _codex_matches(doc: Any, entry: dict[str, Any]) -> bool:
    current = _codex_entry(doc)
    if not hasattr(current, "get"):
        return False
    tools = current.get("tools")
    tool = tools.get(mcp_server.TOOL_NAME) if hasattr(tools, "get") else None
    return (current.get("command") == entry["command"]
            and list(current.get("args") or []) == entry["args"]
            and hasattr(tool, "get") and tool.get("approval_mode") == "approve")


def plan_uninstall(agent_key: str, home: Path) -> list[HookPlan]:
    """Remove exactly what `plan_install` adds, and nothing else."""
    def drop_server(data: dict) -> None:
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and SERVER_KEY in servers:
            servers = {k: v for k, v in servers.items() if k != SERVER_KEY}
            if servers:
                data["mcpServers"] = servers
            else:
                del data["mcpServers"]  # we emptied it; leave the file as we found it

    if agent_key == "claude":
        def disallow(data: dict) -> None:
            perms = data.get("permissions")
            if isinstance(perms, dict) and isinstance(perms.get("allow"), list) \
                    and CLAUDE_ALLOW_RULE in perms["allow"]:
                perms = dict(perms)
                perms["allow"] = [r for r in perms["allow"] if r != CLAUDE_ALLOW_RULE]
                data["permissions"] = perms

        state, settings = config_paths(agent_key, home)
        return [_json_plan(agent_key, state, drop_server, []),
                _json_plan(agent_key, settings, disallow, [])]
    if agent_key == "cursor":
        return [_json_plan(agent_key, config_paths(agent_key, home)[0], drop_server, [])]
    if agent_key == "codex":
        import tomlkit

        path = config_paths(agent_key, home)[0]
        before = _read(path)
        doc = tomlkit.parse(before) if before.strip() else None
        if _codex_entry(doc) is None:
            return [HookPlan(agent_key, path, ACTION_NOOP, before, before, [])]
        servers = doc["mcp_servers"]  # type: ignore[index]
        del servers[SERVER_KEY]
        if not len(servers):
            del doc["mcp_servers"]  # type: ignore[attr-defined]
        # tomlkit keeps the blank line that separated our table from the one above it.
        after = tomlkit.dumps(doc).rstrip("\n") + "\n" if before.endswith("\n") \
            else tomlkit.dumps(doc)
        return [HookPlan(agent_key, path, ACTION_REMOVE, before, after, [])]
    return []


def status(agent_key: str, home: Path, python: str) -> str:
    """`installed / missing / partial / stale-path / unreadable`, as `hooks.status` means
    them. ``stale-path`` is a registration for another interpreter (see `same_python`);
    ``partial`` is the server registered without its no-approval setting, so every call
    would stop and ask. Read-only: `doctor` runs this."""
    try:
        registered = registered_launcher(agent_key, home)
        if registered is None:
            return STATUS_MISSING
        if list(registered["args"]) != launcher(agent_key, python)["args"] \
                or not same_python(str(registered["command"] or ""), python):
            return STATUS_STALE_PATH
        if not _auto_approved(agent_key, home):
            return STATUS_PARTIAL
    except (OSError, ValueError):
        return STATUS_UNREADABLE
    return STATUS_INSTALLED


def same_python(registered: str, expected: str) -> bool:
    """Is `registered` the interpreter `expected` names, however it is spelled?

    One venv answers to ``bin/python`` and ``bin/python3`` alike, and which one
    `sys.executable` reports depends on how the process was started — the wizard and a
    tray started by autostart can disagree. Both are symlinks to one base interpreter,
    so "the same file" alone would also match *another* venv's; the same directory
    as well is what pins it to this install.
    """
    if registered == expected:
        return True
    try:
        a, b = Path(registered), Path(expected)
        return a.parent == b.parent and a.exists() and b.exists() and a.samefile(b)
    except (OSError, ValueError):
        return False


def _auto_approved(agent_key: str, home: Path) -> bool:
    if agent_key == "claude":
        settings = config_paths(agent_key, home)[1]
        perms = _load_json(settings, _read(settings)).get("permissions")
        allow = perms.get("allow") if isinstance(perms, dict) else None
        return isinstance(allow, list) and CLAUDE_ALLOW_RULE in allow
    if agent_key == "codex":
        import tomlkit

        text = _read(config_paths(agent_key, home)[0])
        entry = _codex_entry(tomlkit.parse(text)) if text.strip() else None
        tools = entry.get("tools") if hasattr(entry, "get") else None
        tool = tools.get(mcp_server.TOOL_NAME) if hasattr(tools, "get") else None
        return hasattr(tool, "get") and tool.get("approval_mode") == "approve"
    return True  # Cursor: its approval setting is in its own UI, not a file of ours


def registered_launcher(agent_key: str, home: Path) -> dict[str, Any] | None:
    """The ``command``/``args`` this agent's config registers, None when it has none.
    Raises `ValueError` for a JSON file that won't parse."""
    path = config_paths(agent_key, home)[0]
    text = _read(path)
    if agent_key == "codex":
        import tomlkit

        entry = _codex_entry(tomlkit.parse(text)) if text.strip() else None
    else:
        servers = _load_json(path, text).get("mcpServers")
        entry = servers.get(SERVER_KEY) if isinstance(servers, dict) else None
    if not hasattr(entry, "get"):
        return None
    return {"command": entry.get("command"), "args": list(entry.get("args") or [])}

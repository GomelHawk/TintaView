"""A self-contained walk-through of the whole pipeline. Changes nothing you own.

Runs the real status server on a spare port, deploys the real hook script into a
temporary directory, and fires real hook invocations at it — so what you see is the
actual code path an agent drives, not a simulation of it.

    python scripts/demo.py            # status-only (no lights touched)
    python scripts/demo.py --lights   # also drive whatever lighting engine is available

Your config, your agents' settings files and your normal lighting are untouched either
way: the demo uses its own TINTAVIEW_HOME and, with --lights, restores the engine on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lights", action="store_true",
                    help="drive a real lighting engine (default: status-only)")
    ap.add_argument("--pause", type=float, default=1.2, help="seconds between steps")
    args = ap.parse_args()

    home = Path(tempfile.mkdtemp(prefix="tintaview-demo-"))
    os.environ["TINTAVIEW_HOME"] = str(home)

    from tintaview.core.config import Config
    from tintaview.core.server import StatusServer
    from tintaview.install.detect import detect
    from tintaview.install.hookscript import install_hook_script

    cfg = Config()
    cfg.server.port = free_port()
    cfg.enabled_agents = ["claude", "cursor"]
    cfg.agent("cursor").confirm_detection = "stall"
    cfg.agent("cursor").stall_seconds = 2.0
    if not args.lights:
        cfg.engine.mode = "none"

    env = detect()
    hook = install_hook_script(cfg, env)
    # The demo talks to its own server, so the hook must use a curl that shares this
    # process's network namespace — inside WSL the installed default is curl.exe, which
    # would reach the *Windows* side instead.
    (home / "hook.env").write_text(
        f"TINTAVIEW_URL=http://127.0.0.1:{cfg.server.port}\nTINTAVIEW_CURL=curl\n",
        encoding="utf-8",
    )

    server = StatusServer(cfg)
    if not server.start():
        print(f"could not bind port {cfg.server.port}", file=sys.stderr)
        return 1

    engine = server.controller.engine_status()
    print(f"TintaView demo — server on {server.url}")
    print(f"  temp home:  {home}")
    print(f"  hook:       {hook}")
    print(f"  engine:     {engine['name']} (active={engine['active']})")
    if not args.lights:
        print("  (status-only — re-run with --lights to drive real hardware)")
    print()

    def fire(agent: str, event: str, sid: str) -> None:
        payload = {"session_id": sid} if agent != "cursor" else {"conversation_id": sid}
        subprocess.run([str(hook), agent, event], input=json.dumps(payload).encode(),
                       timeout=5, check=False)

    def show(label: str) -> None:
        with urllib.request.urlopen(f"{server.url}/state", timeout=2) as r:
            st = json.loads(r.read())
        per = " · ".join(f"{k}={v['effective']}" for k, v in sorted(st["agents"].items())) or "—"
        blink = "  [BLINKING]" if st["blinking"] else ""
        print(f"  {label:<34} {st['effective']:<8} {per}{blink}")

    steps = [
        ("Claude session starts", lambda: fire("claude", "session-start", "demo-1")),
        ("Claude starts working", lambda: fire("claude", "working", "demo-1")),
        ("Claude asks for permission", lambda: fire("claude", "confirm", "demo-1")),
        ("you answer; it works again", lambda: fire("claude", "working", "demo-1")),
        ("Claude finishes", lambda: fire("claude", "idle", "demo-1")),
        ("Cursor session starts", lambda: fire("cursor", "session-start", "demo-2")),
        ("Cursor runs a tool", lambda: fire("cursor", "tool-start", "demo-2")),
    ]
    try:
        show("initial state")
        for label, action in steps:
            action()
            time.sleep(0.25)
            show(label)
            time.sleep(args.pause)

        print("\n  ...waiting for Cursor's stall heuristic (no tool-end arrives)...")
        time.sleep(cfg.agent("cursor").stall_seconds + 1.0)
        show("Cursor stalled -> needs you")

        fire("cursor", "tool-end", "demo-2")
        time.sleep(0.3)
        show("Cursor's tool finishes")
        for agent, sid in (("claude", "demo-1"), ("cursor", "demo-2")):
            fire(agent, "session-end", sid)
        time.sleep(0.3)
        show("both sessions end (lights released)")
    finally:
        server.stop()

    print(f"\nDone. Nothing outside {home} was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

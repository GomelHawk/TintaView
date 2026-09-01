"""Out-of-process G HUB LED worker + client.

Measured: under ``pythonw.exe`` the legacy LED SDK returns success from every
``LogiLedSetLighting`` while the mouse stays on G HUB's own profile; the same
``GHubEngine`` paints under ``python.exe``. The tray stays on ``pythonw`` (no console);
when it needs G HUB it spawns a short-lived ``python.exe -m tintaview.engines.ghub_worker``
child that owns the DLL. Chroma/OpenRGB stay in-process.

Protocol: one JSON object per line on stdin/stdout.

  -> {"id": 1, "cmd": "open", "cfg": {...}}
  <- {"id": 1, "ok": true}
  -> {"id": 2, "cmd": "set_color", "r": 255, "g": 200, "b": 0}
  <- {"id": 2, "ok": true}
  -> {"id": 3, "cmd": "close"}
  <- {"id": 3, "ok": true}

``TINTAVIEW_GHUB_WORKER=1`` is set in the child so it never spawns another sidecar.
``TINTAVIEW_GHUB_SIDECAR=1`` forces the parent to use a sidecar even under ``python.exe``
(for tests); ``=0`` disables it under ``pythonw``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from ..core.config import GHubConfig

log = logging.getLogger(__name__)

_ENV_WORKER = "TINTAVIEW_GHUB_WORKER"
_ENV_SIDECAR = "TINTAVIEW_GHUB_SIDECAR"
_CREATE_NO_WINDOW = 0x08000000
_RPC_TIMEOUT = 15.0


def should_use_ghub_sidecar(*, dll_override: Any = None) -> bool:
    """True when this process must paint G HUB via a ``python.exe`` child."""
    if dll_override is not None:
        return False
    if os.environ.get(_ENV_WORKER) == "1":
        return False
    if sys.platform != "win32":
        return False
    flag = (os.environ.get(_ENV_SIDECAR) or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return Path(sys.executable).name.lower() == "pythonw.exe"


def _python_exe_for_worker() -> Path | None:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        return exe
    candidate = exe.with_name("python.exe")
    return candidate if candidate.is_file() else None


def _cfg_payload(cfg: GHubConfig) -> dict[str, Any]:
    return {
        "dll_path": cfg.dll_path or "",
        "settings_db": cfg.settings_db or "",
        "device_types": list(cfg.device_types),
        "restore_on_release": bool(cfg.restore_on_release),
    }


def _cfg_from_payload(data: dict[str, Any]) -> GHubConfig:
    return GHubConfig(
        dll_path=str(data.get("dll_path") or ""),
        settings_db=str(data.get("settings_db") or ""),
        device_types=list(data.get("device_types") or ["monochrome", "rgb", "perkey"]),
        restore_on_release=bool(data.get("restore_on_release", True)),
    )


class GHubSidecar:
    """Parent-side RPC client. Owns one ``python.exe`` worker process."""

    def __init__(self, cfg: GHubConfig) -> None:
        self._cfg = cfg
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._stderr_thread: threading.Thread | None = None
        # Set when an RPC leaves the pipe in an unknown state (see `_discard`). The
        # process is killed at the same time, but `poll()` can lag a moment behind the
        # kill, and until it catches up `alive` must already read False.
        self._broken = False

    @property
    def alive(self) -> bool:
        if self._broken:
            return False
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        with self._lock:
            if self.alive:
                return
            # A worker that crashed or was discarded still has to be buried, or every
            # respawn would leak a zombie plus its three pipes.
            self._reap_locked()
            python = _python_exe_for_worker()
            if python is None:
                raise RuntimeError("python.exe sibling of the tray interpreter not found")
            creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
            env = os.environ.copy()
            env[_ENV_WORKER] = "1"
            # Avoid inheriting a parent force-flag that would recurse.
            env.pop(_ENV_SIDECAR, None)
            self._proc = subprocess.Popen(
                [str(python), "-u", "-m", "tintaview.engines.ghub_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
            self._broken = False
            self._stderr_thread = threading.Thread(
                target=self._pump_stderr, args=(self._proc,), daemon=True,
                name="tv-ghub-sidecar-err",
            )
            self._stderr_thread.start()
            log.info("G HUB sidecar started (pid=%s via %s)", self._proc.pid, python)

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    self._request_unlocked(proc, "close", {}, timeout=2.0)
                except Exception as e:
                    log.debug("ghub sidecar close before stop failed: %r", e)
                with contextlib.suppress(OSError):
                    proc.stdin.close()
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        except Exception as e:
            log.debug("ghub sidecar stop failed: %r", e)
        log.info("G HUB sidecar stopped")

    def open(self) -> bool:
        self.start()
        resp = self._request("open", {"cfg": _cfg_payload(self._cfg)})
        return bool(resp.get("ok"))

    def set_color(self, r: int, g: int, b: int) -> bool:
        if not self.alive:
            return False
        resp = self._request("set_color", {"r": int(r), "g": int(g), "b": int(b)})
        return bool(resp.get("ok"))

    def close(self) -> None:
        if not self.alive:
            return
        try:
            self._request("close", {})
        except Exception as e:
            log.debug("ghub sidecar close failed: %r", e)

    def _pump_stderr(self, proc: subprocess.Popen[str]) -> None:
        # The process is passed in rather than read off `self`: a respawn swaps
        # `self._proc` underneath this thread, and it must keep draining the pipe it
        # was started for, not the new one's.
        if proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    log.info("ghub-worker: %s", line)
        except Exception:
            return

    def _request(self, cmd: str, fields: dict[str, Any], timeout: float = _RPC_TIMEOUT) -> dict:
        with self._lock:
            proc = self._proc
            if proc is None or self._broken or proc.poll() is not None:
                raise RuntimeError("G HUB sidecar is not running")
            return self._request_unlocked(proc, cmd, fields, timeout=timeout)

    def _discard(self, proc: subprocess.Popen[str], reason: str) -> None:
        """Abandon a worker whose response stream is no longer trustworthy.

        Every RPC failure below leaves the pipe in an unknown state — a reply may still
        be in flight, and a reader thread may still be parked in ``readline()``. Left
        running, that thread would consume the *next* request's response line, so from
        then on every call would be answered with the wrong id: a silent, permanent
        desync that no amount of retrying recovers from. Killing the worker ends both
        problems at once (the parked reader gets an EOF), and ``alive`` going False is
        what makes ``GHubEngine.active`` report the session closed, so the controller's
        next ``open()`` spawns a clean one.

        Never raises: it runs on the failure path of every RPC, including ``stop()``'s.
        """
        self._broken = True
        log.info("G HUB sidecar discarded (%s)", reason)
        with contextlib.suppress(Exception):
            proc.kill()

    def _reap_locked(self) -> None:
        """Bury the previous worker and its pipes. Caller holds ``self._lock``."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=2.0)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()

    def _request_unlocked(
        self,
        proc: subprocess.Popen[str],
        cmd: str,
        fields: dict[str, Any],
        *,
        timeout: float,
    ) -> dict:
        assert proc.stdin is not None and proc.stdout is not None
        req_id = self._next_id
        self._next_id += 1
        payload = {"id": req_id, "cmd": cmd, **fields}
        proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        proc.stdin.flush()

        # Blocking read with a watchdog thread — stdout.readline has no timeout on Windows.
        holder: dict[str, Any] = {}
        error: list[BaseException] = []

        def _read() -> None:
            try:
                line = proc.stdout.readline()
                holder["line"] = line
            except BaseException as e:  # noqa: BLE001 — reported to caller
                error.append(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._discard(proc, f"timed out on {cmd!r}")
            raise TimeoutError(f"G HUB sidecar timed out on {cmd!r}")
        if error:
            self._discard(proc, f"stdout read failed during {cmd!r}")
            raise error[0]
        line = holder.get("line") or ""
        if not line:
            self._discard(proc, f"closed stdout during {cmd!r}")
            raise RuntimeError(f"G HUB sidecar closed stdout during {cmd!r}")
        try:
            resp = json.loads(line)
        except ValueError:
            self._discard(proc, f"unparseable reply to {cmd!r}")
            raise
        if resp.get("id") != req_id:
            self._discard(proc, f"id mismatch on {cmd!r}")
            raise RuntimeError(f"G HUB sidecar id mismatch: sent {req_id}, got {resp!r}")
        return resp


def worker_main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m tintaview.engines.ghub_worker``."""
    del argv  # no CLI flags yet
    os.environ[_ENV_WORKER] = "1"
    # Worker is a console python.exe spawned with CREATE_NO_WINDOW — still bind logs to stderr.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )
    from .ghub import GHubEngine

    engine: GHubEngine | None = None
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"id": None, "ok": False, "error": f"bad json: {e}"}), flush=True)
            continue
        req_id = req.get("id")
        cmd = req.get("cmd")
        try:
            if cmd == "open":
                cfg = _cfg_from_payload(req.get("cfg") or {})
                if engine is not None and engine.active:
                    engine.close()
                engine = GHubEngine(cfg)
                ok = engine.open()
                print(json.dumps({"id": req_id, "ok": ok}), flush=True)
            elif cmd == "set_color":
                if engine is None or not engine.active:
                    print(json.dumps({"id": req_id, "ok": False, "error": "not open"}), flush=True)
                    continue
                engine.set_color(int(req["r"]), int(req["g"]), int(req["b"]))
                print(
                    json.dumps({"id": req_id, "ok": True, "note": engine.status_note}),
                    flush=True,
                )
            elif cmd == "close":
                if engine is not None:
                    engine.close()
                print(json.dumps({"id": req_id, "ok": True}), flush=True)
            elif cmd == "ping":
                print(json.dumps({"id": req_id, "ok": True}), flush=True)
            else:
                print(
                    json.dumps({"id": req_id, "ok": False, "error": f"unknown cmd {cmd!r}"}),
                    flush=True,
                )
        except Exception as e:
            log.exception("ghub worker cmd %r failed", cmd)
            print(json.dumps({"id": req_id, "ok": False, "error": repr(e)}), flush=True)
    if engine is not None and engine.active:
        with contextlib.suppress(Exception):
            engine.close()
    return 0

"""Unit tests for the G HUB python.exe sidecar gate and RPC helpers."""

from __future__ import annotations

import io
import json
import threading

import pytest

from tintaview.core.config import GHubConfig
from tintaview.engines import ghub_sidecar as sc
from tintaview.engines.ghub import GHubEngine


def test_should_use_sidecar_false_with_dll_override(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    assert sc.should_use_ghub_sidecar(dll_override=object()) is False


def test_should_use_sidecar_false_inside_worker(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    monkeypatch.setenv(sc._ENV_WORKER, "1")
    assert sc.should_use_ghub_sidecar() is False


def test_should_use_sidecar_true_under_pythonw(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    monkeypatch.delenv(sc._ENV_WORKER, raising=False)
    monkeypatch.delenv(sc._ENV_SIDECAR, raising=False)
    assert sc.should_use_ghub_sidecar() is True


def test_should_use_sidecar_false_under_python_exe(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/python.exe")
    monkeypatch.delenv(sc._ENV_WORKER, raising=False)
    monkeypatch.delenv(sc._ENV_SIDECAR, raising=False)
    assert sc.should_use_ghub_sidecar() is False


def test_should_use_sidecar_force_flag(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/python.exe")
    monkeypatch.setenv(sc._ENV_SIDECAR, "1")
    assert sc.should_use_ghub_sidecar() is True
    monkeypatch.setenv(sc._ENV_SIDECAR, "0")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    assert sc.should_use_ghub_sidecar() is False


def test_should_use_sidecar_false_off_windows(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "linux")
    monkeypatch.setattr(sc.sys, "executable", "/usr/bin/pythonw")
    assert sc.should_use_ghub_sidecar() is False


def test_engine_uses_sidecar_flag_under_pythonw(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    monkeypatch.delenv(sc._ENV_WORKER, raising=False)
    monkeypatch.delenv(sc._ENV_SIDECAR, raising=False)
    engine = GHubEngine(GHubConfig())
    assert engine._use_sidecar is True


def test_engine_in_process_with_injected_dll(monkeypatch):
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    engine = GHubEngine(GHubConfig(), dll=object())
    assert engine._use_sidecar is False


def test_cfg_roundtrip():
    cfg = GHubConfig(dll_path=r"C:\LGHUB\sdk.dll", device_types=["rgb"], restore_on_release=False)
    payload = sc._cfg_payload(cfg)
    back = sc._cfg_from_payload(payload)
    assert back.dll_path == cfg.dll_path
    assert back.device_types == ["rgb"]
    assert back.restore_on_release is False


def test_worker_main_ping(monkeypatch, capsys):
    monkeypatch.setattr(sc.sys, "stdin", io.StringIO('{"id": 7, "cmd": "ping"}\n'))
    assert sc.worker_main() == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"id": 7, "ok": True}


# --------------------------------------------------------------------------- RPC failures


class _FakeProc:
    """Enough of `subprocess.Popen` for the RPC helpers to drive.

    `block=True` parks `readline()` the way a worker that has stopped answering does —
    the only way to reproduce a timeout without waiting on a real one.
    """

    def __init__(self, replies=None, block=False):
        self.stdin = io.StringIO()
        self.stdout = self
        self.stderr = None
        self._replies = list(replies or [])
        self._parked = threading.Event() if block else None
        self._returncode = None
        self.killed = False

    def readline(self):  # stdout.readline
        if self._parked is not None:
            self._parked.wait(5.0)
            return ""  # EOF, as a killed worker's pipe gives
        return self._replies.pop(0) if self._replies else ""

    def poll(self):
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9
        if self._parked is not None:
            self._parked.set()

    def wait(self, timeout=None):
        return self._returncode


def _sidecar_with(proc):
    sidecar = sc.GHubSidecar(GHubConfig())
    sidecar._proc = proc
    return sidecar


def test_successful_rpc_keeps_the_worker():
    proc = _FakeProc(replies=['{"id": 1, "ok": true}\n'])
    sidecar = _sidecar_with(proc)
    assert sidecar._request("ping", {}) == {"id": 1, "ok": True}
    assert not proc.killed
    assert sidecar.alive is True


def test_rpc_timeout_discards_the_worker_instead_of_desyncing():
    """A timed-out request must not leave a reader parked on the pipe.

    That thread wakes on the *next* line the worker writes — which is the reply to
    whatever request came after it. From then on every call is answered with the wrong
    id, silently and for the rest of the run. Killing the worker ends it: the parked
    reader gets an EOF and the sidecar reads as dead, so the engine reopens a clean one.
    """
    proc = _FakeProc(block=True)
    sidecar = _sidecar_with(proc)
    with pytest.raises(TimeoutError):
        sidecar._request("set_color", {"r": 1, "g": 2, "b": 3}, timeout=0.05)
    assert proc.killed
    assert sidecar.alive is False
    with pytest.raises(RuntimeError, match="not running"):
        sidecar._request("ping", {})


def test_id_mismatch_discards_the_worker():
    proc = _FakeProc(replies=['{"id": 99, "ok": true}\n'])
    sidecar = _sidecar_with(proc)
    with pytest.raises(RuntimeError, match="id mismatch"):
        sidecar._request("ping", {})
    assert proc.killed
    assert sidecar.alive is False


def test_closed_stdout_discards_the_worker():
    proc = _FakeProc(replies=[""])
    sidecar = _sidecar_with(proc)
    with pytest.raises(RuntimeError, match="closed stdout"):
        sidecar._request("ping", {})
    assert proc.killed
    assert sidecar.alive is False


# --------------------------------------------------------------------------- engine.active


def _pythonw_engine(monkeypatch) -> GHubEngine:
    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(sc.sys, "executable", "C:/venv/Scripts/pythonw.exe")
    monkeypatch.delenv(sc._ENV_WORKER, raising=False)
    monkeypatch.delenv(sc._ENV_SIDECAR, raising=False)
    engine = GHubEngine(GHubConfig())
    assert engine._use_sidecar is True
    return engine


def test_engine_reports_inactive_once_the_sidecar_dies(monkeypatch):
    """`active` gates `LightController._ensure_open_locked`'s reopen.

    It used to return `_saved` alone, which stays True for the life of the session, so a
    worker that died was never noticed: open() was never called again and every paint
    after that failed silently.
    """
    engine = _pythonw_engine(monkeypatch)
    proc = _FakeProc()
    engine._sidecar = _sidecar_with(proc)
    engine._saved = True
    assert engine.active is True

    proc.kill()
    assert engine.active is False


def test_engine_reports_inactive_when_the_sidecar_was_never_built(monkeypatch):
    engine = _pythonw_engine(monkeypatch)
    engine._saved = True
    assert engine._sidecar is None
    assert engine.active is False

"""Unit tests for the G HUB python.exe sidecar gate and RPC helpers."""

from __future__ import annotations

import io
import json

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

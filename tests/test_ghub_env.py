"""Tests for tintaview.engines.ghub_env — no real G HUB required."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from tintaview.core.config import GHubConfig
from tintaview.engines import ghub_env as E


def test_ghub_running_is_none_off_windows(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "linux")
    assert E.ghub_running() is None


def test_ghub_running_true_when_tasklist_names_the_agent(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "win32")

    class _Result:
        stdout = "lghub_agent.exe               1234 Console  1    50,000 K\n"
        stderr = ""

    monkeypatch.setattr(
        E.subprocess, "run",
        lambda *a, **k: _Result(),
    )
    assert E.ghub_running() is True


def test_ghub_running_false_when_tasklist_has_no_match(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "win32")

    class _Result:
        stdout = "INFO: No tasks are running which match the specified criteria.\n"
        stderr = ""

    monkeypatch.setattr(E.subprocess, "run", lambda *a, **k: _Result())
    assert E.ghub_running() is False


def test_ghub_running_none_when_tasklist_fails(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "win32")

    def boom(*a, **k):
        raise OSError("no tasklist")

    monkeypatch.setattr(E.subprocess, "run", boom)
    assert E.ghub_running() is None


def test_blockers_list_running_and_dynamic_lighting():
    env = E.GHubEnvironment(
        dll_path=Path("C:/fake.dll"),
        running=False,
        dynamic_lighting=True,
        foreground_only=None,
        integration="unknown",
    )
    lines = E.blockers(env)
    assert any("not running" in line for line in lines)
    assert any("Dynamic Lighting" in line for line in lines)


def test_blockers_empty_when_everything_unknown():
    env = E.GHubEnvironment(
        dll_path=Path("C:/fake.dll"),
        running=None,
        dynamic_lighting=None,
        foreground_only=None,
        integration="unknown",
    )
    assert E.blockers(env) == []


def test_integration_absent_when_apps_list_has_no_tintaview(tmp_path):
    db = tmp_path / "settings.db"
    payload = {
        "applications": {
            "applications": [
                {"name": "Some Game", "applicationPath": "C:/Games/game.exe"},
            ]
        }
    }
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE DATA (_id INTEGER PRIMARY KEY, _date_created TEXT, FILE BLOB)"
    )
    con.execute(
        "INSERT INTO DATA (_id, _date_created, FILE) VALUES (1, 'now', ?)",
        (json.dumps(payload).encode("utf-8"),),
    )
    con.commit()
    con.close()

    assert E._integration_state(GHubConfig(settings_db=str(db))) == "absent"


def test_integration_unknown_when_tintaview_present_but_toggle_unconfirmed(tmp_path):
    """Toggle field name is unconfirmed — never invent on/off from a present entry."""
    db = tmp_path / "settings.db"
    payload = {
        "applications": {
            "applications": [
                {"name": "TintaView", "applicationPath": "C:/TintaView/python.exe"},
            ]
        }
    }
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE DATA (_id INTEGER PRIMARY KEY, _date_created TEXT, FILE BLOB)"
    )
    con.execute(
        "INSERT INTO DATA (_id, _date_created, FILE) VALUES (1, 'now', ?)",
        (json.dumps(payload).encode("utf-8"),),
    )
    con.commit()
    con.close()

    assert E._integration_state(GHubConfig(settings_db=str(db))) == "unknown"


def test_integration_unknown_when_db_missing(tmp_path):
    missing = tmp_path / "nope.db"
    assert E._integration_state(GHubConfig(settings_db=str(missing))) == "unknown"


@pytest.mark.skipif(sys.platform == "win32", reason="registry stub is Linux-oriented")
def test_dynamic_lighting_none_off_windows(monkeypatch):
    monkeypatch.setattr(E.sys, "platform", "linux")
    assert E._dynamic_lighting_on() is None

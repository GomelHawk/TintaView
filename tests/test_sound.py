"""`ui/sound.py` — whatever goes wrong with a custom chime, the system sound plays."""

from __future__ import annotations

import pytest

from tintaview.ui import sound


@pytest.fixture
def beeps(monkeypatch):
    calls = []
    monkeypatch.setattr(sound, "system_beep", lambda: calls.append("beep"))
    return calls


def test_no_custom_sound_plays_the_system_sound(beeps, monkeypatch):
    monkeypatch.setattr(sound, "play_file", lambda *a: pytest.fail("must not play a file"))
    sound.play_chime(False, "/s/ding.wav", 50)
    sound.play_chime(True, "   ", 50)
    assert beeps == ["beep", "beep"]


def test_a_missing_file_falls_back_to_the_system_sound(beeps, tmp_path):
    sound.play_chime(True, str(tmp_path / "gone.ogg"), 50)
    assert beeps == ["beep"]


def test_an_unavailable_backend_falls_back_to_the_system_sound(beeps, monkeypatch, tmp_path):
    """QtMultimedia needs libpulse on Linux; without it the chime must still sound."""
    file = tmp_path / "ding.wav"
    file.write_bytes(b"RIFF")
    monkeypatch.setattr(sound, "_ensure_player", lambda: None)
    sound.play_chime(True, str(file), 50)
    assert beeps == ["beep"]


def test_a_playable_file_does_not_also_beep(beeps, monkeypatch, tmp_path):
    played = []
    monkeypatch.setattr(sound, "play_file", lambda path, vol: played.append((path, vol)) or True)
    sound.play_chime(True, " /s/ding.mp3 ", 30)
    assert played == [("/s/ding.mp3", 30)]
    assert beeps == []


def test_stop_and_is_playing_are_safe_before_anything_played(monkeypatch):
    monkeypatch.setattr(sound, "_player", None)
    assert sound.is_playing() is False
    sound.stop()  # no player yet: nothing to stop, no error
    sound.on_playing_changed(lambda playing: None)

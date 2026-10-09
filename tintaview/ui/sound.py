"""The confirm chime: the system sound by default, or a user-picked file at a set volume.

`ui.chime_custom` + `ui.chime_sound` + `ui.chime_volume`, edited on the settings
dialog's Sound tab. The file is played through QtMultimedia's `QMediaPlayer`, which
reads WAV, OGG and MP3 alike. QtMultimedia ships inside PySide6, but on Linux it needs
the system's `libpulse` at import time — so it is imported only when a custom sound is
actually played, and every way that can fail (no backend, a missing or unreadable file,
an unsupported format) falls back to the system sound. A chime that silently plays
nothing is exactly what this setting must never turn the confirm alert into.

The volume (0-100) is relative to the system volume — never louder than it — and is
applied on a logarithmic scale, so the slider's midpoint sounds like half as loud
rather than barely quieter than full.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6 import QtWidgets

log = logging.getLogger(__name__)

#: For the Browse… dialog's filter; QMediaPlayer would accept more, these are the
#: three the setting promises.
SOUND_SUFFIXES = ("*.wav", "*.ogg", "*.mp3")

_player: Any = None  # QMediaPlayer, created on first use and kept alive
_output: Any = None  # its QAudioOutput


def system_beep() -> None:
    """The default chime — what TintaView played before custom sounds existed."""
    if sys.platform == "win32":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return
        except Exception:
            pass
    QtWidgets.QApplication.beep()


def play_chime(custom: bool, path: str, volume: int) -> None:
    """Play the configured chime: `path` at `volume` when `custom` is on and a file is
    set, the system sound otherwise (or when the file cannot be played)."""
    if custom and path.strip() and play_file(path.strip(), volume):
        return
    system_beep()


def play_file(path: str, volume: int) -> bool:
    """Start playing `path` at `volume` percent. False — having logged why — when it
    can't even start; a decode error found later falls back to the system sound by
    itself, from the player's error signal."""
    if not Path(path).is_file():
        log.warning("chime sound %r does not exist, using the system sound", path)
        return False
    player = _ensure_player()
    if player is None:
        return False
    from PySide6 import QtCore

    _output.setVolume(_linear_volume(volume))
    player.stop()
    player.setSource(QtCore.QUrl.fromLocalFile(str(Path(path).resolve())))
    player.play()
    log.debug("chime: playing %s at %d%%", path, volume)
    return True


def is_playing() -> bool:
    """Is a custom sound playing right now? (The system sound can't be stopped and is
    over in a second, so it never counts.)"""
    if _player is None:
        return False
    from PySide6 import QtMultimedia

    return bool(_player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState)


def stop() -> None:
    """Stop a custom sound mid-play — a whole song picked as the chime included."""
    if _player is not None:
        _player.stop()


def on_playing_changed(slot: Callable[[Any], None]) -> None:
    """Connect `slot` to the shared player's playback-state signal, for the settings
    dialog's Test/Stop button; the slot asks `is_playing()` for the answer. Pass a
    method of a QObject, never a lambda: Qt then drops the connection when that object
    is destroyed, where a lambda would keep a closed dialog alive for the life of the
    player. Only possible once the player exists — after `play_file` has been called."""
    if _player is not None:
        _player.playbackStateChanged.connect(slot)


def _linear_volume(volume: int) -> float:
    """Slider percent → the linear gain `QAudioOutput` takes, through a log scale."""
    from PySide6 import QtMultimedia

    level = max(0, min(100, int(volume))) / 100.0
    return float(QtMultimedia.QtAudio.convertVolume(
        level,
        QtMultimedia.QtAudio.VolumeScale.LogarithmicVolumeScale,
        QtMultimedia.QtAudio.VolumeScale.LinearVolumeScale,
    ))


def _ensure_player() -> Any:
    global _player, _output
    if _player is not None:
        return _player
    try:
        from PySide6 import QtMultimedia
    except Exception as exc:  # ImportError, or a missing libpulse on Linux
        log.warning("QtMultimedia unavailable (%s), using the system sound", exc)
        return None
    _output = QtMultimedia.QAudioOutput()
    _player = QtMultimedia.QMediaPlayer()
    _player.setAudioOutput(_output)
    _player.errorOccurred.connect(_on_error)
    return _player


def _on_error(error: Any, message: str) -> None:
    log.warning("chime sound could not be played (%s), using the system sound", message)
    system_beep()

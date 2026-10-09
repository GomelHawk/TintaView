"""The last few `notify_user` messages, for the bell in the usage panel's title bar.

Kept in memory only: these are "the agent finished" notes, read within minutes, and the
tray rarely restarts. Ten is enough for two agents finishing in quick succession plus
some history, and few enough that the popup never needs more than a short scroll.

The bell has three states, all derived here: no entries (gray), entries all seen
(white), and something new since the popup was last opened (white with a dot).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from tintaview.i18n import t

MAX_ENTRIES = 10


@dataclass(frozen=True)
class NotifyEntry:
    agent: str  # agent key ("claude"), drawn with its badge and display name
    message: str
    cwd: str = ""
    at: float = field(default_factory=time.time)  # epoch seconds

    @property
    def project(self) -> str:
        """The working directory's last component — which project this is about."""
        text = self.cwd.rstrip("/\\")
        return text.replace("\\", "/").rsplit("/", 1)[-1] if text else ""


class NotifyLog:
    def __init__(self) -> None:
        self._entries: deque[NotifyEntry] = deque(maxlen=MAX_ENTRIES)
        self._unread = False

    @property
    def entries(self) -> list[NotifyEntry]:
        """Newest first."""
        return list(self._entries)

    @property
    def unread(self) -> bool:
        return self._unread

    def add(self, entry: NotifyEntry) -> None:
        self._entries.appendleft(entry)
        self._unread = True

    def mark_seen(self) -> None:
        self._unread = False

    def clear(self) -> None:
        self._entries.clear()
        self._unread = False


def age_text(at: float, now: float | None = None) -> str:
    """ "just now", "12 min ago", "3 hr ago", "2d ago" — coarse, worded on every paint
    rather than stored, for the same reason usage rows carry an instant (AGENTS.md)."""
    age = max(0.0, (time.time() if now is None else now) - at)
    if age < 60:
        return t("notify.log.just_now")
    if age < 3600:
        return t("notify.log.minutes_ago", minutes=int(age // 60))
    if age < 2 * 86400:
        return t("notify.log.hours_ago", hours=int(age // 3600))
    return t("notify.log.days_ago", days=int(age // 86400))

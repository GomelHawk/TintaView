"""The tray's usage flyout — a frameless dark card painted directly (no QSS).

Two things it does for TintaView's multi-agent world:

  - it renders one SECTION per agent (a small header line, then that agent's rows,
    or a one-line reason when the agent errored) instead of a single global usage
    block;
  - `set_results(dict[str, UsageResult])` is keyed by agent rather than taking one
    global usage payload, since there is no longer one "the" payload.

The remaining details are deliberate, not incidental: dismiss-on-focus-out,
remembering `hidden_at` so the click that just closed the flyout doesn't immediately
reopen it, and the rounded track/fill bars with severity colours.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QRectF, Qt

if TYPE_CHECKING:  # pragma: no cover - types only, no runtime import of the stats layer
    from tintaview.stats.model import UsageResult

# --------------------------------------------------------------------------- palette

CARD_BG = QtGui.QColor("#242427")
TEXT = QtGui.QColor("#e7e7ea")
SUBTLE = QtGui.QColor("#9b9ba3")
TRACK = QtGui.QColor("#3b3b42")
FILL = QtGui.QColor("#6c8cff")
WARN = QtGui.QColor("#e0a63a")
CRIT = QtGui.QColor("#e0574f")
BORDER = QtGui.QColor(255, 255, 255, 22)

# --------------------------------------------------------------------------- geometry

PAD = 18
CARD_W = 380
SECTION_GAP = 14  # extra vertical space between one agent's block and the next


def _severity_color(sev: str) -> QtGui.QColor:
    return {"warning": WARN, "critical": CRIT}.get(sev, FILL)


#: Overrides for keys with no `AgentAdapter` (stats-only integrations, see
#: `ui.wizard._STATS_ONLY_AGENTS`) whose correct casing plain `.title()` can't produce.
_DISPLAY_NAME_OVERRIDES = {"jetbrains": "JetBrains AI Assistant", "copilot": "GitHub Copilot CLI"}


def _display_name(key: str) -> str:
    """Best-effort human name for an agent key ("claude" -> "Claude Code").

    Reaches into the agent-adapter registry (`tintaview.agents`, not stats/core, so
    safe to import here) for the canonical `display_name`; falls back to a titleised
    key if that's ever unavailable, so a cosmetic lookup can never crash the flyout.
    """
    try:
        from tintaview.agents.base import get as get_agent

        adapter = get_agent(key)
        if adapter is not None:
            return adapter.display_name
    except Exception:
        pass
    return _DISPLAY_NAME_OVERRIDES.get(key) or key.replace("_", " ").title() or key


class Flyout(QtWidgets.QWidget):
    """Frameless dark card that paints one usage section per agent."""

    def __init__(self) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._results: dict[str, UsageResult] = {}
        self.hidden_at = 0.0
        self.resize(CARD_W, 140)

    def event(self, e: QtCore.QEvent) -> bool:
        # Dismiss when the card loses focus (click elsewhere, alt-tab, …) — without
        # this the flyout stays pinned open until the next explicit toggle.
        if e.type() == QtCore.QEvent.WindowDeactivate:
            self.hide()
        return super().event(e)

    def hideEvent(self, e: QtGui.QHideEvent) -> None:
        # Recorded so the tray can tell "this click just closed the flyout via
        # focus-out" apart from "this click should open it" — see tray.py's
        # CLICK_REOPEN_GUARD_S.
        self.hidden_at = time.monotonic()
        super().hideEvent(e)

    # --- data ------------------------------------------------------------------

    def set_results(self, results: dict[str, UsageResult]) -> None:
        """`results`: dict[str, UsageResult] keyed by agent key, in display order."""
        self._results = dict(results or {})
        self._resize_to_content()
        self.update()

    # --- layout ------------------------------------------------------------------

    def _section_height(self, result: UsageResult) -> float:
        h = 24.0  # agent header line
        if result.ok:
            prev = None
            for row in result.rows:
                if prev == "limit" and row.kind == "credits":
                    h += 8
                prev = row.kind
                # Must mirror paintEvent: info rows draw no bar, so they are shorter.
                h += 22 + 6 if row.kind == "info" else 22 + 6 + 16
        else:
            h += 20.0  # one-line reason
        return h

    def _resize_to_content(self) -> None:
        h = float(PAD)
        if not self._results:
            h += 20 + 8  # "no agents enabled" message
        else:
            for i, result in enumerate(self._results.values()):
                if i:
                    h += SECTION_GAP
                h += self._section_height(result)
        h += PAD
        self.setFixedSize(CARD_W, max(80, int(h)))

    # --- paint ------------------------------------------------------------------

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        p.fillPath(path, CARD_BG)
        p.setPen(QtGui.QPen(BORDER))
        p.drawPath(path)

        x, w, y = float(PAD), float(self.width() - 2 * PAD), float(PAD)
        f = p.font()

        if not self._results:
            f.setPointSize(10)
            p.setFont(f)
            p.setPen(SUBTLE)
            p.drawText(
                QRectF(x, y, w, self.height() - y - PAD),
                Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                "No agents enabled.",
            )
            p.end()
            return

        for i, result in enumerate(self._results.values()):
            if i:
                y += SECTION_GAP

            f.setPointSize(10)
            p.setFont(f)
            p.setPen(SUBTLE)
            # The section header is the agent's display name ("Claude Code"), not
            # the provider's own `header` string ("Your usage limits · Max") — with
            # several agents on screen at once, identifying *which* agent a section
            # belongs to matters more than the tier blurb the old single-agent
            # flyout led with.
            p.drawText(QRectF(x, y, w, 20), Qt.AlignLeft | Qt.AlignVCenter,
                       _display_name(result.agent))
            y += 24

            if not result.ok:
                p.setPen(SUBTLE)
                reason = result.error or "No usage data."
                p.drawText(QRectF(x, y, w, 20), Qt.AlignLeft | Qt.AlignVCenter, reason)
                y += 20
                continue

            prev = None
            for row in result.rows:
                if prev == "limit" and row.kind == "credits":
                    y += 8
                prev = row.kind

                f.setPointSize(11)
                p.setFont(f)
                p.setPen(TEXT)
                p.drawText(QRectF(x, y, w, 18), Qt.AlignLeft | Qt.AlignVCenter, row.label)

                right = row.right
                if row.show_pct:
                    right = f"{right}   {row.pct:.0f}%" if right else f"{row.pct:.0f}%"
                p.setPen(SUBTLE)
                p.drawText(QRectF(x, y, w, 18), Qt.AlignRight | Qt.AlignVCenter, right)
                y += 22

                # Informational rows (Codex token totals, Claude's local estimate) have
                # no percentage to show. Drawing an empty track for them reads as "0% of
                # your limit", which is a different and wrong claim — so skip the bar and
                # close the gap instead.
                if row.kind == "info":
                    y += 6
                    continue

                track = QtGui.QPainterPath()
                track.addRoundedRect(QRectF(x, y, w, 6), 3, 3)
                p.fillPath(track, TRACK)
                fw = max(0.0, min(1.0, row.pct / 100.0)) * w
                if fw > 0:
                    fill = QtGui.QPainterPath()
                    fill.addRoundedRect(QRectF(x, y, fw, 6), 3, 3)
                    p.fillPath(fill, _severity_color(row.severity))
                y += 6 + 16

        p.end()

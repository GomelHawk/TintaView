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

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QRectF, Qt

if TYPE_CHECKING:  # pragma: no cover - types only, no runtime import of the stats layer
    from tintaview.stats.model import UsageResult, UsageRow

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
HEADER_H = 24  # height of the badge+title header line, per section
CHEVRON_W = 16  # right-edge width reserved for the collapse affordance


def _severity_color(sev: str) -> QtGui.QColor:
    return {"warning": WARN, "critical": CRIT}.get(sev, FILL)


def _draw_chevron(p: QtGui.QPainter, header_rect: QRectF, collapsed: bool) -> None:
    """Small ▸ / ▾ mark at a header's right edge — the only visual cue that a
    section can be clicked to collapse/expand (the hover cursor is the other)."""
    cx = header_rect.right() - CHEVRON_W / 2
    cy = header_rect.center().y()
    s = 4.0
    if collapsed:
        pts = [QtCore.QPointF(cx - s * 0.6, cy - s), QtCore.QPointF(cx - s * 0.6, cy + s),
               QtCore.QPointF(cx + s * 0.7, cy)]
    else:
        pts = [QtCore.QPointF(cx - s, cy - s * 0.6), QtCore.QPointF(cx + s, cy - s * 0.6),
               QtCore.QPointF(cx, cy + s * 0.7)]
    p.setPen(Qt.NoPen)
    p.setBrush(SUBTLE)
    p.drawPolygon(QtGui.QPolygonF(pts))


def _row_layout(rows: list[UsageRow]) -> list[tuple[UsageRow, float, float]]:
    """(row, y-offset from the section's rows-start, block height) per row.

    This is the one place row spacing lives — `_rows_height` and `Flyout.paintEvent`
    both read from it, so sizing and drawing can never drift apart the way two
    hand-duplicated formulas eventually would.
    """
    out: list[tuple[UsageRow, float, float]] = []
    y = 0.0
    prev = None
    for row in rows:
        if prev == "limit" and row.kind == "credits":
            y += 8
        prev = row.kind
        # Info rows draw no bar, so they're shorter than a row with a track+fill.
        block_h = 22 + 6 if row.kind == "info" else 22 + 6 + 16
        out.append((row, y, block_h))
        y += block_h
    return out


def _rows_height(rows: list[UsageRow]) -> float:
    layout = _row_layout(rows)
    return layout[-1][1] + layout[-1][2] if layout else 0.0


@dataclass
class _SectionLayout:
    """One agent's on-screen geometry for this paint — shared by sizing, drawing
    and mouse hit-testing so all three agree on where a header/body actually is."""

    key: str
    result: UsageResult
    header_rect: QRectF  # the clickable/hoverable badge+title band
    rows_top: float  # y where this section's body (rows or error line) starts
    collapsible: bool  # False for errored/empty sections — nothing to hide
    collapsed: bool
    height: float  # total section height, header included


#: Overrides for keys with no `AgentAdapter` (stats-only integrations, see
#: `ui.wizard._STATS_ONLY_AGENTS`) whose correct casing plain `.title()` can't produce.
_DISPLAY_NAME_OVERRIDES = {"jetbrains": "JetBrains AI Assistant", "copilot": "GitHub Copilot CLI"}

# --------------------------------------------------------------------------- provider badges

#: One accent colour per provider, used only as a colour swatch — not sampled from any
#: official brand guideline. Deliberately not the real Claude/OpenAI/Cursor/JetBrains/
#: GitHub marks: those are trademarked artwork this project has no license to bundle,
#: so each badge below is drawn from scratch (an original glyph on a flat accent tile),
#: the same "generated, not bundled" approach `icons.py` already uses for TintaView's
#: own tray mark.
_BADGE_COLORS = {
    "claude": QtGui.QColor("#D2795A"),
    "codex": QtGui.QColor("#3FBF9F"),
    "cursor": QtGui.QColor("#4FA6E8"),
    "jetbrains": QtGui.QColor("#9B59F6"),
    "copilot": QtGui.QColor("#6E5BFF"),
}
_BADGE_DEFAULT_COLOR = QtGui.QColor("#7a7a82")
_BADGE_GLYPH = QtGui.QColor("#f2f2f5")

#: Keyed by (agent key, pixel size) — badges are re-requested on every repaint (the
#: flyout has no dirty-region tracking), so this avoids re-running QPainter for a glyph
#: that never changes once drawn.
_badge_cache: dict[tuple[str, int], QtGui.QPixmap] = {}


def _draw_claude_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A simple four-point sparkle — an original shape, not Anthropic's asterisk mark.
    cx, cy = r.center().x(), r.center().y()
    long, short = r.width() * 0.42, r.width() * 0.12
    for angle in (0, 90, 180, 270):
        p.save()
        p.translate(cx, cy)
        p.rotate(angle)
        diamond = QtGui.QPolygonF([
            QtCore.QPointF(0, -long), QtCore.QPointF(short, 0),
            QtCore.QPointF(0, long * 0.35), QtCore.QPointF(-short, 0),
        ])
        p.drawPolygon(diamond)
        p.restore()


def _draw_codex_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A plain hexagon outline — an original shape, not OpenAI's mark.
    cx, cy, rad = r.center().x(), r.center().y(), r.width() * 0.34
    hexagon = QtGui.QPolygonF(
        [QtCore.QPointF(cx + rad * math.cos(math.radians(a)), cy + rad * math.sin(math.radians(a)))
         for a in range(-90, 271, 60)]
    )
    pen = QtGui.QPen(_BADGE_GLYPH, max(1.0, r.width() * 0.09))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPolygon(hexagon)


def _draw_cursor_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A generic mouse-pointer arrow — literally what "cursor" means, not the product's
    # actual wordmark/icon.
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    arrow = QtGui.QPolygonF([
        QtCore.QPointF(x + w * 0.30, y + h * 0.18), QtCore.QPointF(x + w * 0.30, y + h * 0.82),
        QtCore.QPointF(x + w * 0.48, y + h * 0.64), QtCore.QPointF(x + w * 0.60, y + h * 0.86),
        QtCore.QPointF(x + w * 0.70, y + h * 0.80), QtCore.QPointF(x + w * 0.58, y + h * 0.58),
        QtCore.QPointF(x + w * 0.78, y + h * 0.52),
    ])
    p.drawPolygon(arrow)


def _draw_jetbrains_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # A 2x2 grid of dots — a generic "IDE suite" motif, not JetBrains' ring mark.
    cx, cy, gap, rad = r.center().x(), r.center().y(), r.width() * 0.22, r.width() * 0.11
    for dx in (-gap, gap):
        for dy in (-gap, gap):
            p.drawEllipse(QtCore.QPointF(cx + dx, cy + dy), rad, rad)


def _draw_copilot_glyph(p: QtGui.QPainter, r: QRectF) -> None:
    # Two linked circles ("goggles") — an original abstraction, not the Copilot mark.
    cx, cy = r.center().x(), r.center().y()
    rad, dx = r.width() * 0.19, r.width() * 0.22
    pen = QtGui.QPen(_BADGE_GLYPH, max(1.0, r.width() * 0.1))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawLine(QtCore.QPointF(cx - dx * 0.4, cy), QtCore.QPointF(cx + dx * 0.4, cy))
    p.drawEllipse(QtCore.QPointF(cx - dx, cy), rad, rad)
    p.drawEllipse(QtCore.QPointF(cx + dx, cy), rad, rad)


#: One glyph painter per known provider key; anything else gets no glyph (plain tile).
_BADGE_GLYPHS = {
    "claude": _draw_claude_glyph,
    "codex": _draw_codex_glyph,
    "cursor": _draw_cursor_glyph,
    "jetbrains": _draw_jetbrains_glyph,
    "copilot": _draw_copilot_glyph,
}


def _provider_badge(key: str, size: int) -> QtGui.QPixmap:
    """A small rounded-square tile in the provider's accent colour with an original
    (non-trademarked) glyph — sized to match the section header text it sits beside."""
    cached = _badge_cache.get((key, size))
    if cached is not None:
        return cached

    pix = QtGui.QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    rect = QRectF(0, 0, size, size)
    path = QtGui.QPainterPath()
    path.addRoundedRect(rect, size * 0.28, size * 0.28)
    p.fillPath(path, _BADGE_COLORS.get(key, _BADGE_DEFAULT_COLOR))

    glyph = _BADGE_GLYPHS.get(key)
    if glyph is not None:
        p.setPen(Qt.NoPen)
        p.setBrush(_BADGE_GLYPH)
        inset = rect.adjusted(size * 0.16, size * 0.16, -size * 0.16, -size * 0.16)
        glyph(p, inset)
    p.end()

    _badge_cache[(key, size)] = pix
    return pix


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

    def __init__(
        self,
        collapsed: Iterable[str] | None = None,
        on_toggle: Callable[[str, bool], None] | None = None,
    ) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)  # needed to get mouseMoveEvent without a button held
        self._results: dict[str, UsageResult] = {}
        self._collapsed: set[str] = set(collapsed or ())
        self._on_toggle = on_toggle
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

    def _layout(self) -> tuple[list[_SectionLayout], float]:
        """Single layout pass for the current results, shared by sizing, painting
        and mouse hit-testing (see `_SectionLayout`). Cheap enough to redo on every
        paint/mouse-move: a handful of agents, no QPainter calls involved."""
        x, w = float(PAD), float(CARD_W - 2 * PAD)
        y = float(PAD)
        sections: list[_SectionLayout] = []
        for i, result in enumerate(self._results.values()):
            if i:
                y += SECTION_GAP
            header_rect = QRectF(x, y, w, HEADER_H)
            collapsible = result.ok and bool(result.rows)
            collapsed = collapsible and result.agent in self._collapsed
            rows_top = y + HEADER_H
            if not result.ok:
                body_h = 20.0  # one-line reason
            elif collapsed:
                body_h = 0.0
            else:
                body_h = _rows_height(result.rows)
            height = HEADER_H + body_h
            sections.append(_SectionLayout(result.agent, result, header_rect, rows_top,
                                            collapsible, collapsed, height))
            y += height
        return sections, y

    def _resize_to_content(self) -> None:
        if not self._results:
            h = PAD + 20 + 8 + PAD  # "no agents enabled" message
        else:
            _sections, y = self._layout()
            h = y + PAD
        self.setFixedSize(CARD_W, max(80, int(h)))

    def _toggle(self, key: str) -> None:
        if key in self._collapsed:
            self._collapsed.discard(key)
        else:
            self._collapsed.add(key)
        self._resize_to_content()
        self.update()
        if self._on_toggle is not None:
            self._on_toggle(key, key in self._collapsed)

    # --- mouse -------------------------------------------------------------------

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        sections, _ = self._layout()
        pos = event.position()
        hovering = any(s.collapsible and s.header_rect.contains(pos) for s in sections)
        self.setCursor(Qt.PointingHandCursor if hovering else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            sections, _ = self._layout()
            pos = event.position()
            for s in sections:
                if s.collapsible and s.header_rect.contains(pos):
                    self._toggle(s.key)
                    return
        super().mousePressEvent(event)

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

        x, w = float(PAD), float(self.width() - 2 * PAD)
        f = p.font()

        if not self._results:
            f.setPointSize(10)
            p.setFont(f)
            p.setPen(SUBTLE)
            p.drawText(
                QRectF(x, PAD, w, self.height() - PAD - PAD),
                Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                "No agents enabled.",
            )
            p.end()
            return

        sections, _ = self._layout()
        for section in sections:
            result = section.result
            header = section.header_rect

            f.setPointSize(10)
            p.setFont(f)
            p.setPen(SUBTLE)
            # Badge sized to the header text's own line height, per its ask — not a
            # fixed pixel constant — so it tracks if the header font ever changes.
            badge_size = QtGui.QFontMetrics(f).height()
            badge_gap = 6
            p.drawPixmap(
                QRectF(header.x(), header.y() + (20 - badge_size) / 2, badge_size, badge_size),
                _provider_badge(result.agent, badge_size),
                QRectF(0, 0, badge_size, badge_size),
            )
            # The section header is the agent's display name ("Claude Code"), not
            # the provider's own `header` string ("Your usage limits · Max") — with
            # several agents on screen at once, identifying *which* agent a section
            # belongs to matters more than the tier blurb the old single-agent
            # flyout led with.
            name_w = header.width() - badge_size - badge_gap - (CHEVRON_W if section.collapsible else 0)
            p.drawText(QRectF(header.x() + badge_size + badge_gap, header.y(), name_w, 20),
                       Qt.AlignLeft | Qt.AlignVCenter, _display_name(result.agent))

            if section.collapsible:
                _draw_chevron(p, header, section.collapsed)

            if not result.ok:
                p.setPen(SUBTLE)
                reason = result.error or "No usage data."
                p.drawText(QRectF(x, section.rows_top, w, 20), Qt.AlignLeft | Qt.AlignVCenter, reason)
                continue

            if section.collapsed:
                continue

            for row, y_off, _block_h in _row_layout(result.rows):
                ry = section.rows_top + y_off

                f.setPointSize(11)
                p.setFont(f)
                p.setPen(TEXT)
                p.drawText(QRectF(x, ry, w, 18), Qt.AlignLeft | Qt.AlignVCenter, row.label)

                right = row.right
                if row.show_pct:
                    right = f"{right}   {row.pct:.0f}%" if right else f"{row.pct:.0f}%"
                p.setPen(SUBTLE)
                p.drawText(QRectF(x, ry, w, 18), Qt.AlignRight | Qt.AlignVCenter, right)

                # Informational rows (Codex token totals, Claude's local estimate) have
                # no percentage to show. Drawing an empty track for them reads as "0% of
                # your limit", which is a different and wrong claim — so skip the bar.
                if row.kind == "info":
                    continue

                bar_y = ry + 22 + 6
                track = QtGui.QPainterPath()
                track.addRoundedRect(QRectF(x, bar_y, w, 6), 3, 3)
                p.fillPath(track, TRACK)
                fw = max(0.0, min(1.0, row.pct / 100.0)) * w
                if fw > 0:
                    fill = QtGui.QPainterPath()
                    fill.addRoundedRect(QRectF(x, bar_y, fw, 6), 3, 3)
                    p.fillPath(fill, _severity_color(row.severity))

        p.end()

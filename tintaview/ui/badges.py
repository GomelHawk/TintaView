"""The little provider badges drawn beside each agent's name in the usage panel.

One accent colour and one original glyph per provider, **drawn, not bundled** — the real
Claude/OpenAI/Cursor/JetBrains/GitHub marks are trademarked artwork this project has no
licence to ship, and the same "generated, not bundled" rule already covers TintaView's own
tray icon (`icons.py`). None of these is sampled from an official brand guideline.

Its own module because it is exactly the kind of code that only ever grows — one more
glyph per provider added — and none of it has anything to do with the panel's layout,
which is what the rest of `flyout.py` is about.
"""

from __future__ import annotations

import math

from PySide6 import QtCore, QtGui
from PySide6.QtCore import QRectF, Qt

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


def provider_badge(key: str, size: int) -> QtGui.QPixmap:
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

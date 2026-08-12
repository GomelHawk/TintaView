"""Tray icons: the TintaView mark, tinted per status, plus a brand-coloured variant
for the "no session" state.

The mark is a white silhouette PNG (alpha only) that gets recoloured by compositing a
solid fill through its alpha channel — so one asset serves every status colour instead
of needing one PNG per colour.
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt

#: Sizes actually generated on disk (assets/generated/mark_*_<n>.png) — used to pick
#: the closest pre-rendered size instead of upscaling a tiny one or downscaling the
#: full-res original every call.
_GENERATED_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

#: Fallback burst: 8 tapered rays, echoing the ray count implied by the real mark's
#: silhouette.
RAYS = 8

# Icons are cheap to recompute but get requested constantly (the confirm blink timer
# alternates two colours every `blink_ms`) — cache per (rgb, size) so blinking never
# touches disk or re-runs QPainter after the first pass through each colour.
_state_icon_cache: dict[tuple[int, int, int, int], QtGui.QIcon] = {}
_brand_icon_cache: dict[int, QtGui.QIcon] = {}

#: Sizes baked into every state icon. Covers the usual tray requests (16-24),
#: HiDPI multiples of those, and the larger sizes menus and dialogs ask for.
TRAY_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def asset_path(name: str) -> Path:
    """Resolve a bundled asset (under assets/generated/) by filename.

    The assets live *inside* the package (tintaview/assets/generated), not at the repo
    root, specifically so that a plain `pip install` ships them — which is how TintaView
    is installed on every platform. Anything placed at the repo root would be dropped from
    the wheel and the tray would silently fall back to the drawn placeholder.
    """
    # tintaview/ui/icons.py -> tintaview/
    base = Path(__file__).resolve().parents[1]
    # Missing resolves to the same path anyway; QPixmap(...) on it loads as null, which
    # every caller already handles.
    return base / "assets" / "generated" / name


def _closest_size(size: int) -> int:
    return min(_GENERATED_SIZES, key=lambda s: abs(s - size))


def _load_pixmap(base_name: str, size: int) -> QtGui.QPixmap:
    """Best-matching generated PNG for `size`, falling back to the un-suffixed
    original. Returns a null QPixmap (never raises) if nothing is found — Qt loading
    a missing path is already safe, so callers just check `.isNull()`.
    """
    exact = _closest_size(size)
    for name in (f"{base_name}_{exact}.png", f"{base_name}.png"):
        pm = QtGui.QPixmap(str(asset_path(name)))
        if not pm.isNull():
            return pm
    return QtGui.QPixmap()


def state_icon(rgb: tuple[int, int, int], size: int = 128) -> QtGui.QIcon:
    """The mark drawn in `rgb`, cached per (rgb, size)."""
    key = (rgb[0], rgb[1], rgb[2], size)
    cached = _state_icon_cache.get(key)
    if cached is not None:
        return cached

    # Build a MULTI-RESOLUTION icon rather than one pixmap. A tray asks for ~16-24px
    # (more on a HiDPI display), and letting the shell shrink a single large pixmap
    # softens the capsule ends. Drawing each size outright keeps them crisp at whatever
    # size is actually requested.
    icon = QtGui.QIcon()
    for px in TRAY_ICON_SIZES:
        icon.addPixmap(_draw_mark(rgb, px))

    _state_icon_cache[key] = icon
    return icon


#: The mark's geometry, as fractions of the icon's size — **measured from
#: assets/generated/mark_silhouette.png**, not eyeballed. Reproducing these values agrees
#: with the shipped artwork on 96.6% of pixels.
#:
#: The mark is eight long, slim capsules on 45-degree spokes around a wide-open centre,
#: plus a single accent dot sitting *outside* the ring at the lower right. Two mistakes
#: are easy to make here and both destroy it: fattening the capsules (they are 4.4x
#: longer than they are wide — at half that ratio the mark reads as a flower, not a
#: burst), and treating the dot as a ninth spoke replacing a capsule rather than an extra
#: element beyond them.
#:
#: It is drawn rather than scaled from the PNG because a tray asks for 16-24px, where
#: resampling softens the capsule ends into mush, and because thickness then becomes a
#: number instead of a filter. An earlier attempt dilated the PNG's alpha to embolden it;
#: that rounds the capsule ends off and closes the gaps, which is exactly the failure the
#: ratio above guards against.
MARK_CAPSULE_WIDTH = 0.115  # artwork is 0.082 — widened for legibility at tray sizes
MARK_INNER = 0.104  # capsule start: the open centre
MARK_OUTER = 0.461  # capsule end
MARK_DOT_RADIUS = 0.0625
MARK_DOT_DISTANCE = 0.567  # centre-to-dot; beyond MARK_OUTER, so the dot stands apart
MARK_DOT_ANGLE = 135.0  # degrees clockwise from 12 o'clock: the lower right
MARK_SPOKES = 8


#: The logo's colours, as three flat zones running clockwise from 12 o'clock:
#: **blue -> green -> yellow**, with the orange accent dot.
#:
#: Sampled per-capsule hues were tried first (the artwork has eight, blending blue
#: through teal and green into amber) and they fail at the size that matters: at 16-20px
#: the lone green and teal spokes are one or two pixels wide each, so the mark reads as
#: blue-on-top / amber-below — two colours, not the logo. Three flat zones of three, three
#: and two spokes survive the shrink: each is wide enough to register, and the order still
#: follows the artwork's gradient, so it still reads as the TintaView mark rather than a
#: recolour of it.
_BRAND_BLUE = (1, 132, 248)
_BRAND_GREEN = (107, 210, 67)
_BRAND_YELLOW = (248, 192, 7)

#: Index = spoke, clockwise from 12 o'clock. Blue takes the top and left, green the right,
#: yellow the bottom — the accent dot sits beyond the lower-right (green) spoke.
MARK_BRAND_COLORS = (
    _BRAND_BLUE,    # 0    12 o'clock
    _BRAND_GREEN,   # 1    45
    _BRAND_GREEN,   # 2    90   (right)
    _BRAND_GREEN,   # 3    135
    _BRAND_YELLOW,  # 4    180  (bottom)
    _BRAND_YELLOW,  # 5    225
    _BRAND_BLUE,    # 6    270  (left)
    _BRAND_BLUE,    # 7    315
)
MARK_BRAND_DOT = (254, 151, 4)  # orange


def _draw_mark(
    rgb: tuple[int, int, int] | None,
    size: int,
    colors: tuple[tuple[int, int, int], ...] | None = None,
    dot_color: tuple[int, int, int] | None = None,
) -> QtGui.QPixmap:
    """The TintaView mark at `size`.

    Flat when `rgb` is given (the status colours), or per-spoke when `colors` is — which
    is how the multicolour brand mark is drawn *with the same geometry* as every status
    icon. Using the gradient PNG for that instead would make the idle icon visibly
    thinner than the others, since the artwork's capsules are narrower than
    MARK_CAPSULE_WIDTH.
    """
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    try:
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.translate(size / 2.0, size / 2.0)

        width = size * MARK_CAPSULE_WIDTH
        inner, outer = size * MARK_INNER, size * MARK_OUTER
        radius = width / 2.0  # fully rounded ends: a capsule, not a rounded rectangle
        for spoke in range(MARK_SPOKES):
            p.setBrush(QtGui.QColor(*(colors[spoke % len(colors)] if colors else rgb)))
            p.save()
            p.rotate(spoke * (360.0 / MARK_SPOKES))
            p.drawRoundedRect(
                QtCore.QRectF(-width / 2.0, -outer, width, outer - inner), radius, radius
            )
            p.restore()

        p.setBrush(QtGui.QColor(*(dot_color or (colors[0] if colors else rgb))))
        p.save()
        p.rotate(MARK_DOT_ANGLE)
        dot = size * MARK_DOT_RADIUS
        p.drawEllipse(QtCore.QPointF(0.0, -size * MARK_DOT_DISTANCE), dot, dot)
        p.restore()
    finally:
        p.end()
    return pm


def brand_icon(size: int = 128) -> QtGui.QIcon:
    """The multicolour mark, used for the "no session" state.

    Drawn from MARK_BRAND_COLORS rather than loaded from mark_color.png so that it shares
    the status icons' geometry exactly: same capsule width, same open centre, same dot.
    An idle tray then reads as the logo at rest instead of as a differently-weighted icon.
    """
    cached = _brand_icon_cache.get(size)
    if cached is not None:
        return cached

    icon = QtGui.QIcon()
    for px in TRAY_ICON_SIZES:
        icon.addPixmap(_draw_mark(None, px, colors=MARK_BRAND_COLORS, dot_color=MARK_BRAND_DOT))
    _brand_icon_cache[size] = icon
    return icon


def _draw_burst_icon(rgb: tuple[int, int, int], size: int = 128) -> QtGui.QIcon:
    """Procedural fallback used only if a mark asset is missing at runtime: an
    8-ray tapered burst plus a small accent dot at the lower right, echoing the
    TintaView mark's shape.
    """
    pm = QtGui.QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor(*rgb))

    center = size / 2.0
    inner, outer = size * 0.06, size * 0.44
    mid, halfw = (inner + outer) / 2.0, size * 0.085
    p.save()
    p.translate(center, center)
    for i in range(RAYS):
        p.save()
        p.rotate(i * (360.0 / RAYS))
        path = QtGui.QPainterPath()
        path.moveTo(0, -inner)
        path.quadTo(halfw, -mid, 0, -outer)
        path.quadTo(-halfw, -mid, 0, -inner)
        path.closeSubpath()
        p.drawPath(path)
        p.restore()
    p.restore()

    # Small accent dot, lower-right — the mark's signature accent.
    dot_r = size * 0.065
    p.drawEllipse(QtCore.QPointF(size * 0.78, size * 0.78), dot_r, dot_r)
    p.end()
    return QtGui.QIcon(pm)

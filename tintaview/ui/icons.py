"""Tray icons: the TintaView mark, tinted per status, plus a brand-coloured variant
for the "no session" state.

Ported from claude_code_razer_lights/tray_app.py's `make_state_icon` /
`_draw_burst_icon` / `_resource_path`. The mark is a white silhouette PNG (alpha
only) that gets recoloured by compositing a solid fill through its alpha channel —
so one asset serves every status colour instead of needing one PNG per colour.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt

#: Sizes actually generated on disk (assets/generated/mark_*_<n>.png) — used to pick
#: the closest pre-rendered size instead of upscaling a tiny one or downscaling the
#: full-res original every call.
_GENERATED_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

#: Fallback burst: 8 tapered rays, echoing the ray count implied by the real mark's
#: silhouette (the predecessor's fallback used a plain 12-ray burst with no accent).
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

    Works both from a source checkout and from a PyInstaller bundle: frozen builds
    extract to `sys._MEIPASS`, a plain source checkout has `assets/generated` two
    levels up from this file (tintaview/ui/icons.py -> repo root). Mirrors the
    predecessor's `_resource_path`.
    """
    rel = Path("assets") / "generated" / name
    bases: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # PyInstaller stages the package tree under _MEIPASS; check both the
        # package-relative and bundle-root layouts so either spec works.
        bases.append(Path(meipass) / "tintaview")
        bases.append(Path(meipass))
    # tintaview/ui/icons.py -> tintaview/. The assets live *inside* the package so
    # that a plain `pip install` ships them; anything at the repo root would be
    # dropped and the tray would silently fall back to the drawn placeholder.
    bases.append(Path(__file__).resolve().parents[1])
    for base in bases:
        candidate = base / rel
        if candidate.exists():
            return candidate
    return bases[-1] / rel  # missing; QPixmap(...) on this loads as null, handled by callers


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
    """The mark silhouette recoloured to `rgb`, cached per (rgb, size).

    Ported from the predecessor's `make_state_icon`: draw the (white) silhouette,
    then composite a solid fill through its alpha via `CompositionMode_SourceIn` —
    the result keeps the silhouette's antialiased edges and takes on exactly `rgb`
    wherever the silhouette was opaque.
    """
    key = (rgb[0], rgb[1], rgb[2], size)
    cached = _state_icon_cache.get(key)
    if cached is not None:
        return cached

    if _load_pixmap("mark_silhouette", size).isNull():
        icon = _draw_burst_icon(rgb, size)
        _state_icon_cache[key] = icon
        return icon

    # Build a MULTI-RESOLUTION icon rather than one pixmap. A tray asks for ~16-24px
    # (more on a HiDPI display), and letting the shell shrink a single 128px pixmap
    # softens the thin rays. Supplying each size, rendered from the closest
    # purpose-built PNG, keeps them crisp at whatever size is actually requested.
    icon = QtGui.QIcon()
    for px in TRAY_ICON_SIZES:
        icon.addPixmap(_tinted_pixmap(rgb, px))

    _state_icon_cache[key] = icon
    return icon


def _tinted_pixmap(rgb: tuple[int, int, int], size: int) -> QtGui.QPixmap:
    """The silhouette at `size`, recoloured to `rgb` through its own alpha."""
    silhouette = _load_pixmap("mark_silhouette", size)
    scaled = silhouette.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    out = QtGui.QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QtGui.QPainter(out)
    p.drawPixmap(0, 0, scaled)
    p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QtGui.QColor(*rgb))  # recolour the shape, keep its alpha
    p.end()
    return out


def brand_icon(size: int = 128) -> QtGui.QIcon:
    """The original gradient mark (blue/green/yellow/orange), used for the
    "no session" tray state so an idle tray reads as the TintaView mark at rest
    rather than a tinted status colour (docs/PLAN.md §7).
    """
    cached = _brand_icon_cache.get(size)
    if cached is not None:
        return cached

    pm = _load_pixmap("mark_color", size)
    if pm.isNull():
        # No brand colour to fall back to procedurally — use the neutral "none"
        # tone so this never renders as a blank tray icon.
        icon = _draw_burst_icon((214, 118, 85), size)
    else:
        scaled = pm.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        icon = QtGui.QIcon(scaled)

    _brand_icon_cache[size] = icon
    return icon


def _draw_burst_icon(rgb: tuple[int, int, int], size: int = 128) -> QtGui.QIcon:
    """Procedural fallback used only if a mark asset is missing at runtime: an
    8-ray tapered burst plus a small accent dot at the lower right, echoing the
    TintaView mark's shape (adapted from the predecessor's 12-plain-ray fallback).
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

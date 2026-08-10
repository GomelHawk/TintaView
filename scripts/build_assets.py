#!/usr/bin/env python3
"""Generate TintaView's tray/icon/logo assets from the source artwork.

Reads the read-only sources under ``assets/source/`` and (re)writes everything
under ``assets/generated/``:

  mark_silhouette.png / mark_silhouette_<N>.png  - white burst, alpha only, for tray recolouring
  mark_color.png      / mark_color_<N>.png       - burst in its original gradient colours
  tintaview.ico                                   - Windows multi-resolution icon
  tintaview.icns (or a .iconset/ folder if icns writing is unsupported here)
  logo_full.png / logo_transparent.png            - wordmark lockups for docs/wizard

Run as:

    python3 scripts/build_assets.py            # (re)generate everything
    python3 scripts/build_assets.py --check    # verify artifacts exist (CI)

The script is idempotent: given unchanged sources it writes byte-identical
output (Pillow's PNG/ICO encoders are deterministic for a given pixel buffer
and save options).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "assets" / "source"
# Inside the package, not beside it: setuptools package-data can only ship files
# under the package directory, so anything left at the repo root is silently
# missing from a pip install and the tray falls back to a drawn placeholder.
OUT_DIR = REPO_ROOT / "tintaview" / "assets" / "generated"

ICON_SRC = SRC_DIR / "icon.png"
FULL_LOGO_SRC = SRC_DIR / "full_logo.png"
TRANSPARENT_SRC = SRC_DIR / "transparent.png"

# --- luminance keying -------------------------------------------------------
# The mark sources sit on a near-black (~lum 6-11) square background. Sampling
# assets/source/icon.png shows the background clustered under lum ~10 and the
# mark's gradient body starting around lum ~90+, with only a thin antialiased
# band in between. LUM_LOW/LUM_HIGH define that ramp: below LUM_LOW is fully
# transparent, above LUM_HIGH is fully opaque, and pixels in between fade
# smoothly (an eased ramp, not a hard cutoff) so antialiased ray edges stay
# smooth instead of jagged.
#
# Tune these if a regenerated source has a different background level or if
# edges look too hard (narrow the gap) or too soft/fringed (widen it).
LUM_LOW = 12.0
LUM_HIGH = 90.0

# Luminance weights (ITU-R BT.601 "perceived brightness").
LUM_WEIGHTS = (0.299, 0.587, 0.114)

# Threshold (on the 0-255 keyed alpha) used only to find the mark's bounding
# box for trimming - low enough to keep faint antialiased fringes inside the
# crop, high enough to ignore residual background noise.
BBOX_ALPHA_THRESHOLD = 2

# Margin around the trimmed bounding box, as a fraction of the box's longer side,
# before padding back out to a square.
#
# The mark must FILL its canvas. assets/source/icon.png is an *app icon*: it frames
# the mark at ~65% inside a rounded-square tile, and that 17% of built-in padding is
# tile chrome, not part of the logo. A tray icon gets padding from the shell as well,
# so honouring the source's padding stacks the two and the icon lands visibly smaller
# than every neighbour in the tray (Claude, OpenAI, Razer et al. all fill ~90%).
#
# Scaling changes no proportions — ray length, thickness and spacing are intrinsic to
# the artwork — so filling the frame keeps the shape exactly and only makes it bigger.
#
# Derived rather than hardcoded: a mark spanning F of the frame has margins of
# (1 - F) / 2 per side, i.e. a margin fraction of (1 - F) / (2F) of the mark's own
# longer side. Lower TARGET_MARK_SPAN if the rays ever crowd or clip at 16px.
TARGET_MARK_SPAN = 0.94
MARGIN_FRACTION = (1.0 - TARGET_MARK_SPAN) / (2.0 * TARGET_MARK_SPAN)

# Output size sets.
PNG_SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
BASE_SIZE = 512
LOGO_MAX_WIDTH = 1024


def _smoothstep(x: np.ndarray) -> np.ndarray:
    """Ease a 0..1 ramp with an S-curve so the alpha fade isn't linear-harsh."""
    return x * x * (3.0 - 2.0 * x)


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 luma of an (H, W, 3) float array."""
    r, g, b = LUM_WEIGHTS
    return rgb[..., 0] * r + rgb[..., 1] * g + rgb[..., 2] * b


def _keyed_alpha(rgb: np.ndarray) -> np.ndarray:
    """Build a 0-255 uint8 alpha channel from luminance keying."""
    lum = _luminance(rgb.astype(np.float64))
    ramp = np.clip((lum - LUM_LOW) / (LUM_HIGH - LUM_LOW), 0.0, 1.0)
    return (_smoothstep(ramp) * 255.0).round().astype(np.uint8)


def _bbox_from_alpha(alpha: np.ndarray, threshold: int) -> tuple[int, int, int, int]:
    """Return an inclusive (left, top, right, bottom) box of pixels above threshold."""
    mask = alpha > threshold
    ys, xs = np.nonzero(mask)
    if xs.size == 0 or ys.size == 0:
        raise ValueError("no pixels found above the bbox alpha threshold")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _estimate_background(rgb: np.ndarray, lum: np.ndarray, lum_cutoff: float = 8.0) -> np.ndarray:
    """Median colour of the near-black background, used to un-blend edge pixels."""
    mask = lum < lum_cutoff
    if not mask.any():
        mask = lum < np.percentile(lum, 5)
    return np.median(rgb[mask].reshape(-1, 3), axis=0)


def _trim_and_pad(rgb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Crop to the mark's bounding box and re-pad to a centred square with margin."""
    left, top, right, bottom = _bbox_from_alpha(alpha, BBOX_ALPHA_THRESHOLD)
    content_w = right - left + 1
    content_h = bottom - top + 1
    side = max(content_w, content_h)
    margin = round(side * MARGIN_FRACTION)
    square = side + 2 * margin

    canvas_rgb = np.zeros((square, square, 3), dtype=np.uint8)
    canvas_alpha = np.zeros((square, square), dtype=np.uint8)

    off_x = (square - content_w) // 2
    off_y = (square - content_h) // 2
    canvas_rgb[off_y : off_y + content_h, off_x : off_x + content_w] = rgb[
        top : bottom + 1, left : right + 1
    ]
    canvas_alpha[off_y : off_y + content_h, off_x : off_x + content_w] = alpha[
        top : bottom + 1, left : right + 1
    ]
    return canvas_rgb, canvas_alpha


def _unpremultiply_edges(rgb: np.ndarray, alpha: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Undo the black-background blend baked into antialiased edge pixels.

    Source edge pixels are ``bg*(1-a) + fg*a``; solving for fg pulls the dark
    background contamination back out so edge colours stay true to the
    gradient instead of fading toward near-black before alpha even applies.
    """
    a = np.clip(alpha.astype(np.float64) / 255.0, 1e-3, 1.0)[..., None]
    fg = (rgb.astype(np.float64) - bg[None, None, :] * (1.0 - a)) / a
    return np.clip(fg, 0, 255).astype(np.uint8)


def _premultiplied_resize(img: Image.Image, size: int) -> Image.Image:
    """Resize an RGBA image with LANCZOS, working in premultiplied alpha space.

    Plain (non-premultiplied) resizing of RGBA blends colour and alpha
    independently, which drags dark fringes in from fully-transparent
    neighbours at the antialiased edge. Premultiplying before the resize and
    dividing back out after keeps those edges clean at small sizes.

    If every opaque pixel shares the same RGB (e.g. the white silhouette),
    that fringe risk doesn't exist and the premultiply/un-premultiply
    round-trip only adds float rounding noise (255 * (a/255) does not always
    round-trip exactly), so the alpha channel is resized directly instead and
    recombined with the exact constant colour.
    """
    arr = np.asarray(img)
    rgb, alpha = arr[..., :3], arr[..., 3]
    opaque = alpha > 0
    if not opaque.any() or np.all(rgb[opaque] == rgb[opaque][0]):
        constant_rgb = rgb[opaque][0] if opaque.any() else np.array([255, 255, 255], dtype=np.uint8)
        resized_alpha = np.asarray(Image.fromarray(alpha, "L").resize((size, size), Image.LANCZOS))
        resized_rgb = np.broadcast_to(constant_rgb, (size, size, 3))
        out = np.dstack([resized_rgb, resized_alpha]).astype(np.uint8)
        return Image.fromarray(out, "RGBA")

    farr = arr.astype(np.float64)
    frgb, fa = farr[..., :3], farr[..., 3:4]
    premult = np.concatenate([frgb * (fa / 255.0), fa], axis=-1).round().astype(np.uint8)
    resized = np.asarray(
        Image.fromarray(premult, "RGBA").resize((size, size), Image.LANCZOS)
    ).astype(np.float64)
    r_rgb, r_a = resized[..., :3], resized[..., 3:4]
    safe_a = np.clip(r_a / 255.0, 1e-6, 1.0)
    out_rgb = np.clip(r_rgb / safe_a, 0, 255)
    out = np.concatenate([out_rgb, r_a], axis=-1).round().astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _build_marks() -> tuple[Image.Image, Image.Image]:
    """Return (silhouette, colour) base RGBA images at BASE_SIZE, trimmed+padded."""
    src = Image.open(ICON_SRC).convert("RGB")
    rgb = np.asarray(src)
    lum = _luminance(rgb.astype(np.float64))
    alpha = _keyed_alpha(rgb)
    bg = _estimate_background(rgb, lum)

    clean_rgb = _unpremultiply_edges(rgb, alpha, bg)

    padded_rgb, padded_alpha = _trim_and_pad(clean_rgb, alpha)

    white_rgb = np.full_like(padded_rgb, 255)
    silhouette_native = Image.fromarray(
        np.dstack([white_rgb, padded_alpha]), "RGBA"
    )
    color_native = Image.fromarray(np.dstack([padded_rgb, padded_alpha]), "RGBA")

    silhouette = _premultiplied_resize(silhouette_native, BASE_SIZE)
    color = _premultiplied_resize(color_native, BASE_SIZE)
    return silhouette, color, silhouette_native, color_native


def _write_size_set(native: Image.Image, base_512: Image.Image, name: str) -> list[Path]:
    written = []
    base_path = OUT_DIR / f"{name}.png"
    base_512.save(base_path)
    written.append(base_path)
    for size in PNG_SIZES:
        img = base_512 if size == BASE_SIZE else _premultiplied_resize(native, size)
        path = OUT_DIR / f"{name}_{size}.png"
        img.save(path)
        written.append(path)
    return written


def _write_ico(color_native: Image.Image) -> Path:
    path = OUT_DIR / "tintaview.ico"
    sized = [_premultiplied_resize(color_native, s) for s in ICO_SIZES]
    largest = sized[-1]
    largest.save(
        path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=sized[:-1],
    )
    return path


def _write_icns(color_native: Image.Image) -> tuple[Path | None, Path | None]:
    """Try to write a real .icns; fall back to a documented .iconset/ folder."""
    icns_path = OUT_DIR / "tintaview.icns"
    icns_sizes = [16, 32, 64, 128, 256, 512, 1024]
    try:
        sized = [_premultiplied_resize(color_native, s) for s in icns_sizes]
        largest = sized[-1]
        largest.save(icns_path, format="ICNS", append_images=sized[:-1])
        if icns_path.exists() and icns_path.stat().st_size > 0:
            return icns_path, None
    except Exception:
        pass

    if icns_path.exists():
        icns_path.unlink()

    # Fallback: write the .iconset folder Apple's iconutil expects, plus a
    # README documenting how to finish the conversion on a macOS host.
    iconset_dir = OUT_DIR / "tintaview.iconset"
    iconset_dir.mkdir(exist_ok=True)
    iconset_map = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for filename, size in iconset_map.items():
        _premultiplied_resize(color_native, size).save(iconset_dir / filename)

    readme = iconset_dir / "README.txt"
    readme.write_text(
        "Pillow could not write a .icns directly in this environment.\n"
        "This folder is a ready .iconset - finish the conversion on macOS with:\n\n"
        "    iconutil -c icns tintaview.iconset -o tintaview.icns\n\n"
        "then move tintaview.icns into assets/generated/.\n"
    )
    return None, iconset_dir


def _resize_to_max_width(img: Image.Image, max_width: int) -> Image.Image:
    if img.width <= max_width:
        return img.copy()
    new_height = round(img.height * (max_width / img.width))
    return img.resize((max_width, new_height), Image.LANCZOS)


def _write_logos() -> list[Path]:
    written = []

    full_logo = Image.open(FULL_LOGO_SRC).convert("RGB")
    full_out = _resize_to_max_width(full_logo, LOGO_MAX_WIDTH)
    full_path = OUT_DIR / "logo_full.png"
    full_out.save(full_path)
    written.append(full_path)

    transparent_logo = Image.open(TRANSPARENT_SRC).convert("RGBA")
    transparent_out = _resize_to_max_width(transparent_logo, LOGO_MAX_WIDTH)
    transparent_path = OUT_DIR / "logo_transparent.png"
    transparent_out.save(transparent_path)
    written.append(transparent_path)

    return written


def expected_artifacts() -> dict[str, list[Path]]:
    """Manifest of everything build() should produce, for --check."""
    manifest: dict[str, list[Path]] = {
        "mark_color": [OUT_DIR / "mark_color.png"]
        + [OUT_DIR / f"mark_color_{s}.png" for s in PNG_SIZES],
        "mark_silhouette": [OUT_DIR / "mark_silhouette.png"]
        + [OUT_DIR / f"mark_silhouette_{s}.png" for s in PNG_SIZES],
        "ico": [OUT_DIR / "tintaview.ico"],
        "logos": [OUT_DIR / "logo_full.png", OUT_DIR / "logo_transparent.png"],
    }
    return manifest


def build() -> dict[str, object]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    silhouette, color, silhouette_native, color_native = _build_marks()

    silhouette_files = _write_size_set(silhouette_native, silhouette, "mark_silhouette")
    color_files = _write_size_set(color_native, color, "mark_color")
    ico_file = _write_ico(color_native)
    icns_file, iconset_dir = _write_icns(color_native)
    logo_files = _write_logos()

    return {
        "silhouette_files": silhouette_files,
        "color_files": color_files,
        "ico_file": ico_file,
        "icns_file": icns_file,
        "iconset_dir": iconset_dir,
        "logo_files": logo_files,
        "silhouette_512": silhouette,
    }


def check() -> bool:
    ok = True
    manifest = expected_artifacts()
    for group, paths in manifest.items():
        for path in paths:
            if not path.exists() or path.stat().st_size == 0:
                print(f"MISSING or EMPTY [{group}]: {path}", file=sys.stderr)
                ok = False

    icns_path = OUT_DIR / "tintaview.icns"
    iconset_dir = OUT_DIR / "tintaview.iconset"
    if icns_path.exists() and icns_path.stat().st_size > 0:
        pass
    elif iconset_dir.is_dir() and any(iconset_dir.glob("*.png")):
        print(f"NOTE: tintaview.icns not present, using fallback {iconset_dir}", file=sys.stderr)
    else:
        print("MISSING: tintaview.icns (and no fallback tintaview.iconset/)", file=sys.stderr)
        ok = False

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify all expected artifacts exist and are non-empty; do not regenerate",
    )
    args = parser.parse_args()

    if args.check:
        return 0 if check() else 1

    result = build()

    silhouette_512 = result["silhouette_512"]
    alpha = np.asarray(silhouette_512)[..., 3]
    coverage_512 = (alpha > 0).mean() * 100

    small = _premultiplied_resize(
        Image.open(OUT_DIR / "mark_silhouette.png").convert("RGBA"), 16
    )
    alpha_16 = np.asarray(small)[..., 3]
    coverage_16 = (alpha_16 > 0).mean() * 100

    print(f"Generated assets under {OUT_DIR}")
    print(f"  mark_silhouette.png alpha coverage @512: {coverage_512:.1f}%")
    print(f"  mark_silhouette.png alpha coverage @16 : {coverage_16:.1f}%")
    if result["icns_file"]:
        print(f"  tintaview.icns written: {result['icns_file']}")
    else:
        print(f"  tintaview.icns NOT supported here; wrote {result['iconset_dir']} instead")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

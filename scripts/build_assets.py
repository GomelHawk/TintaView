#!/usr/bin/env python3
"""Generate TintaView's two runtime image assets from the source artwork.

Reads the read-only sources under ``assets/source/`` and (re)writes everything
under ``tintaview/assets/generated/``:

  logo_full.png   - the wordmark lockup, loaded by ui/icons.py for the About dialog
  tintaview.ico   - Windows multi-resolution icon, used by packaging/install.ps1

That is the whole list on purpose. This script used to also emit sixteen sized
``mark_silhouette_<N>.png`` / ``mark_color_<N>.png`` files, their un-suffixed
originals, ``logo_transparent.png`` and a ``tintaview.icns`` - about a megabyte
shipped inside every wheel and loaded by nothing. The tray mark is *drawn* (see
``tintaview/ui/icons.py``: a tray asks for 16-24px, where resampling a PNG turns
the capsule ends to mush), the About dialog deliberately uses the opaque logo
rather than the transparent one, and there is no macOS ``.app`` bundle for a
``.icns`` to live in - packaging is a pure-Python wheel on every platform
(AGENTS.md, "Packaging"). If a macOS bundle ever happens, add the ``.icns`` back
*outside* the package directory so it stays out of the wheel.

Run as:

    python3 scripts/build_assets.py            # (re)generate everything
    python3 scripts/build_assets.py --check    # fail if the committed files are stale

``--check`` regenerates into a temporary directory and compares bytes, which it
can do because the script is idempotent: given unchanged sources it writes
byte-identical output (Pillow's PNG/ICO encoders are deterministic for a given
pixel buffer and save options).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
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
# assets/source/transparent.png is kept as source artwork but no longer generated
# from: logo_transparent.png had no runtime reader (see the module docstring).

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
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
LOGO_MAX_WIDTH = 1024

#: Only used to report the mark's alpha coverage after a build — a cheap sanity signal
#: that the luminance keying above still separates mark from background.
COVERAGE_SIZES = (512, 16)


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


def _build_mark() -> Image.Image:
    """The colour mark as an RGBA image at the source's native resolution, trimmed and
    re-padded so it fills its frame (see MARGIN_FRACTION)."""
    src = Image.open(ICON_SRC).convert("RGB")
    rgb = np.asarray(src)
    lum = _luminance(rgb.astype(np.float64))
    alpha = _keyed_alpha(rgb)
    bg = _estimate_background(rgb, lum)

    clean_rgb = _unpremultiply_edges(rgb, alpha, bg)
    padded_rgb, padded_alpha = _trim_and_pad(clean_rgb, alpha)
    return Image.fromarray(np.dstack([padded_rgb, padded_alpha]), "RGBA")


def _write_ico(mark: Image.Image, out_dir: Path) -> Path:
    path = out_dir / "tintaview.ico"
    sized = [_premultiplied_resize(mark, s) for s in ICO_SIZES]
    largest = sized[-1]
    largest.save(
        path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=sized[:-1],
    )
    return path


def _resize_to_max_width(img: Image.Image, max_width: int) -> Image.Image:
    if img.width <= max_width:
        return img.copy()
    new_height = round(img.height * (max_width / img.width))
    return img.resize((max_width, new_height), Image.LANCZOS)


def _write_logo(out_dir: Path) -> Path:
    full_logo = Image.open(FULL_LOGO_SRC).convert("RGB")
    path = out_dir / "logo_full.png"
    _resize_to_max_width(full_logo, LOGO_MAX_WIDTH).save(path)
    return path


def expected_artifacts(out_dir: Path = OUT_DIR) -> dict[str, list[Path]]:
    """Manifest of everything build() produces, for --check."""
    return {
        "ico": [out_dir / "tintaview.ico"],
        "logo": [out_dir / "logo_full.png"],
    }


def build(out_dir: Path = OUT_DIR) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)

    mark = _build_mark()
    ico_file = _write_ico(mark, out_dir)
    logo_file = _write_logo(out_dir)

    return {"ico_file": ico_file, "logo_file": logo_file, "mark": mark}


def check() -> bool:
    """Regenerate into a temp directory and compare bytes with the committed files.

    An existence check was worse than nothing here. Every one of these files is derived
    from artwork under assets/source/, so the failure that actually happens is a re-exported
    source whose output nobody regenerated — at which point all the files still exist, are
    all non-empty, and are all wrong. Byte comparison catches exactly that, and it does not
    false-alarm because the pipeline is reproducible (see the module docstring).
    """
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        fresh_dir = Path(tmp)
        build(fresh_dir)
        for group, paths in expected_artifacts().items():
            for path in paths:
                if not path.exists() or path.stat().st_size == 0:
                    print(f"MISSING or EMPTY [{group}]: {path}", file=sys.stderr)
                    ok = False
                    continue
                if path.read_bytes() != (fresh_dir / path.name).read_bytes():
                    print(
                        f"STALE [{group}]: {path} differs from a fresh build — "
                        "run `python scripts/build_assets.py`",
                        file=sys.stderr,
                    )
                    ok = False

    # Leftovers matter as much as staleness: every file under this directory is shipped
    # inside the wheel, so one the script no longer produces is dead weight in every
    # install until somebody notices.
    expected_names = {p.name for paths in expected_artifacts().values() for p in paths}
    for path in sorted(OUT_DIR.glob("*")):
        if path.name not in expected_names:
            print(
                f"UNEXPECTED: {path} is not produced by this script and is shipped in "
                "the wheel for nothing — delete it",
                file=sys.stderr,
            )
            ok = False

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate into a temp dir and fail if the committed files differ",
    )
    args = parser.parse_args()

    if args.check:
        return 0 if check() else 1

    result = build()

    mark = result["mark"]
    print(f"Generated assets under {OUT_DIR}")
    for size in COVERAGE_SIZES:
        alpha = np.asarray(_premultiplied_resize(mark, size))[..., 3]
        print(f"  mark alpha coverage @{size}: {(alpha > 0).mean() * 100:.1f}%")
    print(f"  {result['logo_file'].name}, {result['ico_file'].name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

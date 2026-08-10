"""Tests for the generated icon/logo assets under tintaview/assets/generated/.

These run against the *committed* generated files - they never invoke
scripts/build_assets.py themselves, so a broken pipeline can't silently pass
by regenerating fresh (correct) output during the test run.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = REPO_ROOT / "tintaview" / "assets" / "generated"

PNG_SIZES = [16, 24, 32, 48, 64, 128, 256, 512]

# A trimmed-and-padded mark should clearly fill its frame, not sit tiny in a
# sea of transparency. This is a loose sanity bound, not a precise spec.
MIN_OPAQUE_FRACTION = 0.10
MAX_OPAQUE_FRACTION = 0.60


def _all_mark_pngs():
    for prefix in ("mark_color", "mark_silhouette"):
        yield GENERATED_DIR / f"{prefix}.png", prefix, 512
        for size in PNG_SIZES:
            yield GENERATED_DIR / f"{prefix}_{size}.png", prefix, size


@pytest.mark.parametrize(("path", "prefix", "size"), list(_all_mark_pngs()))
def test_mark_png_exists_and_is_valid(path: Path, prefix: str, size: int) -> None:
    assert path.exists(), f"missing generated asset: {path}"
    assert path.stat().st_size > 0, f"generated asset is empty: {path}"
    with Image.open(path) as img:
        img.verify()
    with Image.open(path) as img:
        assert img.mode == "RGBA", f"{path} should be RGBA, got {img.mode}"
        assert img.size == (size, size), f"{path} should be {size}x{size}, got {img.size}"


def test_logo_files_exist_and_are_valid() -> None:
    full_path = GENERATED_DIR / "logo_full.png"
    transparent_path = GENERATED_DIR / "logo_transparent.png"

    for path in (full_path, transparent_path):
        assert path.exists(), f"missing generated asset: {path}"
        assert path.stat().st_size > 0

    with Image.open(full_path) as img:
        assert img.width <= 1024
    with Image.open(transparent_path) as img:
        assert img.mode == "RGBA"
        assert img.width <= 1024


def test_ico_exists_with_expected_sizes() -> None:
    path = GENERATED_DIR / "tintaview.ico"
    assert path.exists() and path.stat().st_size > 0
    with Image.open(path) as img:
        assert img.format == "ICO"
        sizes = img.info.get("sizes") or set()
        expected = {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)}
        assert expected.issubset(set(sizes)), f"tintaview.ico missing sizes: {expected - set(sizes)}"


def test_icns_or_documented_iconset_fallback() -> None:
    icns_path = GENERATED_DIR / "tintaview.icns"
    iconset_dir = GENERATED_DIR / "tintaview.iconset"

    if icns_path.exists() and icns_path.stat().st_size > 0:
        with Image.open(icns_path) as img:
            assert img.format == "ICNS"
        return

    # Fallback path: a .iconset/ folder with a README documenting the
    # iconutil command to finish the conversion on macOS.
    assert iconset_dir.is_dir(), "neither tintaview.icns nor a tintaview.iconset/ fallback exists"
    pngs = list(iconset_dir.glob("*.png"))
    assert pngs, "tintaview.iconset/ fallback has no PNGs"
    readme = iconset_dir / "README.txt"
    assert readme.exists()
    assert "iconutil" in readme.read_text()


def test_silhouette_is_white_with_clean_alpha() -> None:
    path = GENERATED_DIR / "mark_silhouette.png"
    with Image.open(path) as img:
        arr = np.asarray(img)

    rgb = arr[..., :3]
    alpha = arr[..., 3]

    # Corners must be fully transparent (the square canvas background).
    h, w = alpha.shape
    corner_coords = [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]
    for y, x in corner_coords:
        assert alpha[y, x] == 0, f"corner ({x},{y}) is not fully transparent"

    # Opaque pixels (the burst body) must be pure white.
    opaque_mask = alpha > 250
    assert opaque_mask.sum() > 0, "no fully-opaque pixels found in silhouette"
    opaque_rgb = rgb[opaque_mask]
    assert np.array_equal(
        opaque_rgb, np.full_like(opaque_rgb, 255)
    ), "opaque silhouette pixels are not pure white (255,255,255)"


def test_mark_fills_a_sensible_fraction_of_the_frame() -> None:
    for name in ("mark_color.png", "mark_silhouette.png"):
        path = GENERATED_DIR / name
        with Image.open(path) as img:
            alpha = np.asarray(img)[..., 3]
        opaque_fraction = (alpha > 128).mean()
        assert MIN_OPAQUE_FRACTION <= opaque_fraction <= MAX_OPAQUE_FRACTION, (
            f"{name} opaque fraction {opaque_fraction:.2%} outside expected "
            f"[{MIN_OPAQUE_FRACTION:.0%}, {MAX_OPAQUE_FRACTION:.0%}] range - "
            "mark may not have been trimmed/padded correctly"
        )


def test_small_silhouette_still_reads_as_the_mark() -> None:
    """At 16px there should still be a meaningful amount of opaque coverage,
    not just a faint smudge (which would mean the rays vanished on downscale).
    """
    path = GENERATED_DIR / "mark_silhouette_16.png"
    with Image.open(path) as img:
        alpha = np.asarray(img)[..., 3]
    majority_opaque_fraction = (alpha > 128).mean()
    assert majority_opaque_fraction >= MIN_OPAQUE_FRACTION, (
        f"mark_silhouette_16.png majority-opaque fraction "
        f"{majority_opaque_fraction:.2%} is too low - burst rays may have "
        "disappeared at small size"
    )


def test_color_mark_has_gradient_not_flat_color() -> None:
    """mark_color.png should keep the original blue->green->yellow->orange
    gradient rather than collapsing to a single colour.
    """
    path = GENERATED_DIR / "mark_color.png"
    with Image.open(path) as img:
        arr = np.asarray(img)
    alpha = arr[..., 3]
    rgb = arr[..., :3]
    opaque = alpha > 200
    colors = rgb[opaque]
    assert colors.shape[0] > 0
    # A meaningful spread in hue implies a gradient rather than a flat fill.
    std_per_channel = colors.astype(float).std(axis=0)
    assert std_per_channel.max() > 20, (
        f"mark_color.png opaque pixels look too uniform (per-channel std "
        f"{std_per_channel}); expected a visible colour gradient"
    )

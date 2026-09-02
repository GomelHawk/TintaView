"""Tests for the generated image assets under tintaview/assets/generated/.

These run against the *committed* generated files - they never invoke
scripts/build_assets.py themselves, so a broken pipeline can't silently pass
by regenerating fresh (correct) output during the test run. The staleness
question ("do the committed files still match the sources?") is a different
one, and `python scripts/build_assets.py --check` is what answers it.

There are exactly two generated files, and both have a named runtime reader:
logo_full.png (ui/icons.py's About dialog) and tintaview.ico
(packaging/install.ps1). The tray mark itself is drawn, not loaded - see
ui/icons.py - so the sized mark PNGs that used to live here were a megabyte
shipped inside every wheel for nothing.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = REPO_ROOT / "tintaview" / "assets" / "generated"

#: Everything the build produces, and everything the wheel should therefore ship.
EXPECTED_FILES = {"logo_full.png", "tintaview.ico"}


def test_only_the_two_used_assets_are_shipped() -> None:
    """Every file here goes into the wheel, so one nothing reads is dead weight in
    every install on every platform until somebody notices."""
    present = {p.name for p in GENERATED_DIR.iterdir()}
    assert present == EXPECTED_FILES, f"unexpected: {sorted(present - EXPECTED_FILES)}"


def test_logo_exists_and_is_valid() -> None:
    path = GENERATED_DIR / "logo_full.png"
    assert path.exists() and path.stat().st_size > 0
    with Image.open(path) as img:
        img.verify()
    with Image.open(path) as img:
        assert img.width <= 1024


def test_ico_exists_with_expected_sizes() -> None:
    path = GENERATED_DIR / "tintaview.ico"
    assert path.exists() and path.stat().st_size > 0
    with Image.open(path) as img:
        assert img.format == "ICO"
        sizes = img.info.get("sizes") or set()
        expected = {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)}
        assert expected.issubset(set(sizes)), f"tintaview.ico missing sizes: {expected - set(sizes)}"


def test_check_mode_regenerates_and_compares_bytes() -> None:
    """`--check` must actually be able to fail.

    It used to only assert that each file existed and was non-empty, which passes
    for the failure that really happens: a re-exported source whose output nobody
    regenerated. Every file is still there and every one of them is stale.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tv_build_assets", REPO_ROOT / "scripts" / "build_assets.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.check() is True  # the committed files are current

    manifest = module.expected_artifacts()
    assert {p.name for paths in manifest.values() for p in paths} == EXPECTED_FILES

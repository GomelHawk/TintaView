"""Contract tests for the install channel.

`packaging/install.ps1`, `.github/workflows/build.yml` and `tintaview.install.update`
have to agree on a handful of literal strings — the wheel's filename, the checksums
filename, the install prefix — and nothing in the normal test run exercises PowerShell
or CI to catch it when they drift. A rename on one side and not the other produces a
release that simply fails to install, which is exactly the kind of break that only shows
up in front of a user. These are cheap string assertions that fail at the point of the
mistake instead.

They also pin the decisions that are easy to undo by accident:

- **No compiled bundle, ever.** Windows Smart App Control refuses to run executables that
  are neither signed nor cloud-reputable, and a freshly built PyInstaller binary is unique
  to each release so it can never become reputable. The app is installed as a wheel into a
  virtual environment and launched through the PSF-signed interpreter (docs/PLAN.md §8.3).
- The install prefix must stay the directory `config.config_dir()` resolves to, and the
  prefix itself must never be deleted recursively — on Windows the user's config.toml,
  hook.env, `bin\\tv-hook.cmd` and `logs\\` are siblings of the venv (see install.ps1's
  header), so a stray `Remove-Item -Recurse` on the prefix would destroy them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tintaview.core import config
from tintaview.install import update as U

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO_ROOT / "packaging" / "install.ps1"
INSTALL_SH = REPO_ROOT / "packaging" / "install.sh"
BUILD_YML = REPO_ROOT / ".github" / "workflows" / "build.yml"


@pytest.fixture(scope="module")
def ps1() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def build_yml() -> str:
    return BUILD_YML.read_text(encoding="utf-8")


def test_both_installers_ship_in_the_repo():
    assert INSTALL_PS1.is_file(), "install.ps1 is the only supported Windows install path"
    assert INSTALL_SH.is_file()


def test_installer_and_ci_agree_on_the_wheel_name(ps1: str, build_yml: str):
    # install.ps1 asks the release for "<name>-<version>-py3-none-any.whl"; CI produces
    # exactly that filename via `python -m build` and copies every .whl to the release.
    assert '"{0}-{1}-py3-none-any.whl"' in ps1
    assert "python -m build" in build_yml
    assert "cp dist/*.whl" in build_yml


def test_everyone_agrees_on_the_checksums_filename(ps1: str, build_yml: str):
    assert "'SHA256SUMS.txt'" in ps1
    assert "SHA256SUMS.txt" in build_yml
    assert "SHA256SUMS.txt" in U._CHECKSUM_ASSET_NAMES, (
        "the self-updater has to recognise the checksums asset CI actually publishes"
    )


def test_ci_publishes_the_scripts_the_updater_downloads(build_yml: str):
    # `_update_windows`/`_update_posix` fetch release assets by these exact names.
    assert "cp packaging/install.ps1 dist/release/" in build_yml
    assert "cp packaging/install.sh dist/release/" in build_yml


def test_install_prefix_matches_the_apps_own_config_dir(ps1: str):
    # config_dir() on Windows is LOCALAPPDATA/APP_NAME, and install.ps1 must land in that
    # same folder -- they are deliberately one directory, not two (see install.ps1's header).
    assert config.APP_NAME == "TintaView"
    assert re.search(r"\$AppName\s*=\s*'TintaView'", ps1)
    assert "$Prefix = Join-Path $localAppData $AppName" in ps1


def test_the_updater_and_the_installer_agree_on_the_venv_location(ps1: str):
    """`_install_prefix()` walks up from `sys.prefix`, so the venv must be `<prefix>/venv`."""
    assert re.search(r"\$VenvDir\s*=\s*Join-Path \$Prefix 'venv'", ps1)
    assert 'venv.name.lower() != "venv"' in (
        (REPO_ROOT / "tintaview" / "install" / "update.py").read_text(encoding="utf-8")
    )


def test_the_installer_never_deletes_the_prefix_recursively(ps1: str):
    """The user's config lives *inside* the install prefix on Windows.

    Deleting `$VenvDir` recursively is fine and expected -- that directory is created and
    owned by the installer. Deleting `$Prefix` is not.
    """
    for line in ps1.splitlines():
        if "Remove-Item" not in line or "-Recurse" not in line:
            continue
        assert not re.search(r"\$Prefix\b", line), (
            f"a recursive delete of the install prefix would destroy the user's "
            f"config.toml, hook.env, bin/ and logs/: {line.strip()}"
        )


def test_windows_launches_through_the_signed_interpreter(ps1: str):
    """Smart App Control allows pythonw.exe (PSF-signed); it blocks unique binaries.

    The shortcut must therefore target pythonw.exe with `-m tintaview`, never the
    pip-generated `tintaview.exe` shim, which is an unsigned executable of its own.
    """
    assert re.search(r"\$VenvPythonW\s*=\s*Join-Path \$VenvScripts 'pythonw\.exe'", ps1)
    assert re.search(r"-Target \$VenvPythonW\s+-Arguments '-m tintaview'", ps1)
    assert (REPO_ROOT / "tintaview" / "__main__.py").is_file(), (
        "`pythonw.exe -m tintaview` is what the Startup shortcut runs; without a "
        "__main__.py it fails at login and nowhere else"
    )


def test_no_compiled_bundle_is_built_or_published(build_yml: str):
    assert not list(REPO_ROOT.joinpath("packaging").rglob("*.spec")), (
        "no PyInstaller bundle: Smart App Control can never trust a per-release unique "
        "binary (docs/PLAN.md §8.3)"
    )
    assert not list(REPO_ROOT.joinpath("packaging").rglob("*.iss"))
    # Comments are stripped first: the header explains *why* these tools are absent, and
    # naming them there is the point. What must not come back is a step that runs one.
    steps = "\n".join(
        line for line in build_yml.lower().splitlines() if not line.strip().startswith("#")
    )
    for forbidden in ("pyinstaller", "iscc", "innosetup", "inno setup", "setup.exe"):
        assert forbidden not in steps, f"build.yml should not run {forbidden!r}"

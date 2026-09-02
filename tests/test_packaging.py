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
  virtual environment and launched through the PSF-signed interpreter (AGENTS.md, "Packaging:
  no compiled bundle, ever").
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


@pytest.fixture(scope="module")
def sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


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
    G HUB lighting uses a python.exe sidecar from inside the tray process.
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
        "binary (AGENTS.md, 'Packaging: no compiled bundle, ever')"
    )
    assert not list(REPO_ROOT.joinpath("packaging").rglob("*.iss"))
    # Comments are stripped first: the header explains *why* these tools are absent, and
    # naming them there is the point. What must not come back is a step that runs one.
    steps = "\n".join(
        line for line in build_yml.lower().splitlines() if not line.strip().startswith("#")
    )
    for forbidden in ("pyinstaller", "iscc", "innosetup", "inno setup", "setup.exe"):
        assert forbidden not in steps, f"build.yml should not run {forbidden!r}"


# --------------------------------------------------------------------------- install.sh


def test_install_sh_downloads_the_same_wheel_ci_publishes(sh: str, build_yml: str):
    """Both installers fetch the wheel, because the wheel is what SHA256SUMS.txt covers.

    GitHub's auto-generated `archive/refs/tags/<tag>.tar.gz` is produced on demand and is
    not reproducible, so it can never appear in the checksums file and could therefore
    never be verified — which is why install.sh used to install it unchecked.
    """
    assert 'WHEEL_NAME="tintaview-$VERSION-py3-none-any.whl"' in sh
    assert "archive/refs/tags" not in sh, (
        "the GitHub auto tarball is not in SHA256SUMS.txt and cannot be verified"
    )
    assert "cp dist/*.whl" in build_yml


def test_install_sh_verifies_the_download_and_fails_closed(sh: str):
    """The Linux/macOS twin of install.ps1's Assert-Checksum."""
    assert "SHA256SUMS.txt" in sh
    assert "sha256sum -c" in sh, "coreutils on Linux"
    assert "shasum -a 256 -c" in sh, "macOS ships no sha256sum"
    # A missing checksums file, a missing entry, a mismatch and a machine with neither
    # hashing tool must *all* abort, and the download must be deleted rather than left
    # around for something else to install.
    for phrase in (
        "Refusing to install an unverified build",           # checksums file unreachable
        "refusing to install an unverified download",        # no entry for our file
        "an unverified build is never installed",            # mismatch / no hashing tool
    ):
        assert phrase in sh, phrase
    assert sh.count('rm -f "$_dir/$_file"') == 3, "every failure path deletes the download"


def test_install_sh_never_falls_back_to_pypi(sh: str):
    """TintaView is deliberately not on PyPI (AGENTS.md non-goals), so `tintaview` there
    is an unclaimed, squattable name — installing from it would run a stranger's code."""
    # Comments stripped first: the surviving comment explains *why* the fallback is gone
    # and naming it there is the point. What must not come back is code that runs it.
    code = "\n".join(line for line in sh.splitlines() if not line.strip().startswith("#"))
    assert 'PKG_SPEC="tintaview"' not in code
    assert "pip install tintaview" not in code


def test_the_source_url_override_still_works_and_warns(sh: str):
    """A developer override may bypass verification, but never quietly."""
    assert "TINTAVIEW_SOURCE_URL" in sh
    assert "WITHOUT any" in sh and "SHA-256 verification" in sh


def test_both_installers_force_reinstall_to_repair_a_damaged_install(sh: str, ps1: str):
    """`pip install --upgrade` compares version numbers and does nothing when they match.

    Without the second pass, re-running the installer cannot repair a broken install and
    a re-tagged release silently keeps the old code while reporting success (AGENTS.md,
    "Packaging").
    """
    assert "--force-reinstall --no-deps" in ps1
    assert "--force-reinstall --no-deps" in sh


def test_install_sh_exits_zero_from_a_local_checkout(sh: str):
    """An EXIT trap whose last command is false sets the script's exit status.

    `[ -n "$WORKDIR" ] && rm -rf "$WORKDIR"` is false when nothing was downloaded (the
    local-checkout path), so a fully successful install reported failure — and
    `tintaview update` returns install.sh's exit code straight to the caller.
    """
    assert '[ -n "$WORKDIR" ] && rm -rf' not in sh
    assert 'if [ -n "$WORKDIR" ]; then' in sh

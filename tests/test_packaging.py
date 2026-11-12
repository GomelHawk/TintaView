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
    assert "python -I -m build --no-isolation" in build_yml
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
    assert re.search(r"-Target \$VenvPythonW\s+-Arguments '-P -m tintaview'", ps1)
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


def test_install_sh_requires_ensurepip_not_just_venv(sh: str):
    """Debian/Ubuntu ship `ensurepip` in python3.X-venv; `-m venv --help` passes without it.

    Checking only `venv` let `python3 -m venv` fail halfway on a stock Ubuntu and leave a
    venv with no pip. The CI job `smoke-install-distro-python` runs the real thing.
    """
    assert "import venv, ensurepip" in sh
    assert "apt install python$FIRST_VER-venv" in sh


def test_both_installers_rebuild_a_venv_without_pip(sh: str, ps1: str):
    """A venv is only reused when its pip works — a pip-less one is rebuilt, not inherited.

    Reusing on "python3 exists" alone made every re-run of install.sh fail with
    "No module named pip", even after the missing package was installed.
    """
    assert '"$VENV_PY" -I -m pip --version' in sh
    assert '-I -m venv --clear "$VENV_DIR"' in sh
    assert "& $VenvPython -I -m pip --version" in ps1


def test_install_sh_does_everything_install_ps1_does(sh: str, ps1: str):
    """Step-for-step parity (AGENTS.md, "Every supported OS gets everything").

    Ubuntu users got none of these: no menu entry, no wizard, no running tray, and a
    "Next: run 'tintaview setup'" that answered "command not found" because
    ~/.local/bin was not on PATH yet.
    """
    # The applications entry: Start Menu <-> XDG .desktop <-> ~/Applications/*.app
    assert "Get-StartMenuShortcutPath" in ps1
    assert "$APPS_DIR/tintaview.desktop" in sh
    assert '$HOME/Applications/$APP_NAME.app' in sh
    # ...removed again on uninstall.
    assert 'rm -f "$APP_DESKTOP_PATH"' in sh
    # The wizard at the end, skippable the same way.
    assert "TINTAVIEW_NO_WIZARD" in ps1 and "TINTAVIEW_NO_WIZARD" in sh
    assert '"$LAUNCHER" setup </dev/tty' in sh
    # The tray started at the end.
    assert 'nohup "$LAUNCHER"' in sh
    # Never a bare command the user's PATH may not have yet.
    assert "Next: run 'tintaview setup'" not in sh
    assert 'RUN_HINT="$LAUNCHER"' in sh


def test_the_updater_never_runs_the_wizard():
    """`-Silent` on Windows; TINTAVIEW_NO_WIZARD for install.sh."""
    update_py = (REPO_ROOT / "tintaview" / "install" / "update.py").read_text(encoding="utf-8")
    assert '"TINTAVIEW_NO_WIZARD": "1"' in update_py


def test_the_app_menu_icon_is_the_square_mark_not_the_wordmark(sh: str):
    """logo_full.png is a 1024x484 wordmark; GNOME's app grid squeezed it onto a black
    square. Both menu entries use the brand mark the tray draws, rendered at install."""
    assert "logo_full.png" not in sh
    assert "write_brand_png" in sh
    # A named theme icon: GNOME kept its cached picture when only Icon='s *path* changed.
    assert 'ICON_PATH="tintaview"' in sh
    assert "icons/hicolor" in sh and "256x256/apps/tintaview.png" in sh
    assert "CFBundleIconFile" in sh


def test_a_reinstall_restarts_the_tray_with_the_new_code(sh: str, ps1: str):
    """install.ps1 stops the running app first; install.sh said "already running" and left
    the old code on screen until the next login, so every fix looked like it did nothing.
    From SSH or a tty (no DISPLAY) it starts the tray through the session's systemd unit."""
    assert "Stop-RunningApp" in ps1
    assert "restart.running_pid(cfg)" in sh and "restart._stop(pid, cfg)" in sh
    assert "is already running" not in sh
    assert "systemctl --user restart tintaview.service" in sh


def test_missing_x11_libraries_are_offered_not_just_warned(sh: str):
    """A warning scrolled past unread on the maintainer's Ubuntu VM, so GNOME's left click
    never reached the app. Interactive installs are asked; nothing installs unasked."""
    assert 'Install them now with: $PKG_CMD$MISSING_PKGS? [Y/n]' in sh
    assert "$PKG_CMD $MISSING_PKGS </dev/tty" in sh
    # The package names of every distribution the installer knows, not Ubuntu's alone.
    for manager in ("sudo apt-get install -y", "sudo dnf install -y",
                    "sudo pacman -S --needed --noconfirm"):
        assert manager in sh


def test_install_sh_stops_the_tray_before_the_wizard_like_install_ps1(sh: str, ps1: str):
    """install.ps1 calls Stop-RunningApp before its wizard. Stopping after it let the
    wizard restart the old tray first, so it was started twice (seen in a real log)."""
    assert sh.index("restart.running_pid(cfg)") < sh.index('"$LAUNCHER" setup </dev/tty')
    assert ps1.index("Stop-RunningApp\n", ps1.index("# ------------------------------------------------------------- venv")) \
        < ps1.index("Launching the setup wizard")


def test_install_sh_starts_the_mac_tray_in_the_desktop_session(sh: str):
    """The LaunchAgent autostart writes, kickstarted in gui/<uid> -- the macOS twin of
    `systemctl --user restart`, so an install over SSH still shows the tray."""
    assert 'launchctl kickstart -k "gui/$(id -u)/com.tintaview.app"' in sh


def test_uninstall_never_deletes_the_prefix_itself(sh: str):
    """`--prefix` is whatever was typed: `rm -rf "$PREFIX"` took `$HOME` with it for
    `--uninstall --prefix "$HOME"`. Only the installer's own venv — proven by its
    pyvenv.cfg — is removed recursively; the directory goes only if that emptied it."""
    code = "\n".join(line for line in sh.splitlines() if not line.lstrip().startswith("#"))
    assert 'rm -rf "$PREFIX"' not in code
    assert 'if [ -f "$VENV_DIR/pyvenv.cfg" ]; then' in code
    assert 'rmdir "$PREFIX"' in code


# --- the dependency lock: built by CI, verified and enforced by both installers ---


def _load_build_lock():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_lock", REPO_ROOT / "scripts" / "build_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_ships_the_lock_files_the_installers_download(ps1: str, sh: str, build_yml: str):
    """One list of lock names (`scripts/build_lock.py`), and each installer asks for the
    ones it can need: Windows always installs the desktop set, `install.sh` either."""
    locks = _load_build_lock().LOCKS
    assert set(locks) == {"requirements-lock.txt", "requirements-headless-lock.txt"}
    assert "'requirements-lock.txt'" in ps1
    for name in locks:
        assert f'"{name}"' in sh
    # The committed files, copied before the checksums are computed so SHA256SUMS.txt
    # covers them — and never regenerated at release time: that moved the pins with every
    # release, and every user's update re-downloaded Qt (about 260 MB) for it.
    copy = "cp packaging/requirements-lock.txt packaging/requirements-headless-lock.txt dist/release/"
    assert build_yml.index(copy) < build_yml.index("sha256sum -- * > SHA256SUMS.txt")
    assert "build_lock.py" not in build_yml and "pip-tools" not in build_yml


def test_both_installers_install_from_the_lock_the_same_way(ps1: str, sh: str):
    for script in (ps1, sh):
        code = "\n".join(line for line in script.splitlines()
                         if not line.lstrip().startswith("#"))
        # Exact files only: pinned versions, checked hashes, no source builds.
        assert "--require-hashes" in code and "--only-binary" in code
        # A checksum mismatch stops the install; it is never retried unpinned.
        assert "DO NOT MATCH THE HASHES" in code
        # The reason is read from pip's own log, not from captured output.
        assert "--log" in code
    # A downloaded lock is verified against the release's checksums like the wheel.
    assert 'verify_checksum "$WORKDIR" "$LOCK_NAME" "$SUMS_NAME"' in sh
    assert "Assert-Checksum -FilePath $lockLocal -ChecksumsPath $sumsLocal -FileName $lockName" in ps1


def test_the_smoke_jobs_prove_the_locked_path_ran_on_every_os():
    """Versions alone cannot tell a locked install from the unpinned fallback on the day
    the lock is made, so each smoke job runs `build_lock.py verify`."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "build_lock.py verify packaging/requirements-headless-lock.txt" in ci
    assert "build_lock.py verify packaging/requirements-lock.txt" in ci
    assert "build_lock.py verify dist\\requirements-lock.txt" in ci
    # ...against the committed locks, which CI also checks still fit pyproject.toml.
    assert "Copy-Item packaging\\requirements-lock.txt dist\\" in ci
    assert "python scripts/build_lock.py check" in ci
    assert "build_lock.py build" not in ci


def test_the_committed_locks_pin_and_hash_every_dependency(sh: str):
    """What a release ships and a checkout install uses. Every requirement line is an
    exact pin followed by at least one hash, or `--require-hashes` refuses the file."""
    module = _load_build_lock()
    assert module.LOCK_DIR == REPO_ROOT / "packaging"
    assert '"$REPO_ROOT/packaging/$LOCK_NAME"' in sh
    for name, extras in module.LOCKS.items():
        text = (module.LOCK_DIR / name).read_text(encoding="utf-8")
        assert "\r" not in text, f"{name} must stay LF"
        assert "/home/" not in text and "runner" not in text, f"{name} names a build machine"
        pins = module.pinned(module.LOCK_DIR / name)
        names = {pin_name for pin_name, _version in pins}
        assert {"tomlkit", "openrgb-python"} <= names
        assert ({"pyside6-essentials", "pyside6-addons"} <= names) == ("ui" in extras)
        # Never the `PySide6` meta-package: it drags in WebEngine and Pdf, which nothing
        # imports — more than half of the download.
        assert not names & {"pyside6", "pyside6-webengine", "pyside6-pdf"}
        requirement_lines = [line for line in text.splitlines()
                             if line and not line.startswith((" ", "#"))]
        assert len(requirement_lines) == len(pins), f"{name} has an unpinned requirement"
        assert text.count("--hash=sha256:") >= len(pins)


def test_lock_pins_are_read_from_a_pip_compile_file(tmp_path):
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text(
        "#\n# comment\n#\n"
        "openrgb-python==0.3.8 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n"
        "    # via tintaview (pyproject.toml)\n"
        "tomlkit==0.15.2 \\\n    --hash=sha256:cc\n", encoding="utf-8")
    assert _load_build_lock().pinned(lock) == [("openrgb-python", "0.3.8"),
                                               ("tomlkit", "0.15.2")]


def test_the_install_command_fetches_the_released_installer():
    """Not the `main` branch: the script a user pipes into a shell is the one published
    with — and checksummed beside — the wheel it installs."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "raw.githubusercontent.com" not in readme
    assert "releases/latest/download/install.sh | sh" in readme
    assert "releases/latest/download/install.ps1 | iex" in readme


def test_the_ui_extra_names_only_the_qt_parts_the_tray_imports():
    """Derived from the package: every `PySide6.<module>` imported anywhere must come
    from Essentials or Addons, the two distributions the [ui] extra installs."""
    import re
    import tomllib

    extras = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["optional-dependencies"]["ui"]
    assert [spec.split(">=")[0] for spec in extras] == ["PySide6-Essentials", "PySide6-Addons"]

    essentials = {"QtCore", "QtGui", "QtWidgets"}
    addons = {"QtMultimedia"}
    used = set()
    for path in (REPO_ROOT / "tintaview").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"from PySide6 import ([A-Za-z, ]+)", text):
            used |= {name.strip() for name in match.group(1).split(",")}
        used |= set(re.findall(r"PySide6\.(Qt[A-Za-z]+)", text))
    assert used, "found no PySide6 imports — the scan is broken"
    assert used <= essentials | addons, f"a Qt module outside the [ui] extra: {used - essentials - addons}"


def test_both_installers_say_it_takes_a_while_and_to_keep_the_window_open(ps1: str, sh: str):
    """A silent terminal for minutes under "this takes a minute" read as a hung install.
    Plain words, no package names or sizes: the reader is not a developer."""
    for script in (ps1, sh):
        code = "\n".join(line for line in script.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "This can take several minutes." in code
        assert "keep this window open" in code and "it is working" in code
        message = [line for line in code.splitlines() if "keep this window open" in line][0]
        assert "Qt" not in message and "pip" not in message and "MB" not in message
        assert "this takes a minute" not in code and "may take a minute" not in code


# --- pinned must not mean stale: freshness and vulnerability checks ---


def _lock_dir(tmp_path, module, tomlkit="0.15.2"):
    for name in module.LOCKS:
        (tmp_path / name).write_text(
            f"tomlkit=={tomlkit} \\\n    --hash=sha256:aa\n"
            "openrgb-python==0.3.8 \\\n    --hash=sha256:bb\n", encoding="utf-8")
    return tmp_path


def test_outdated_fails_when_a_pin_is_behind_its_newest_release(tmp_path, capsys):
    module = _load_build_lock()
    newest = {"tomlkit": "0.15.2", "openrgb-python": "0.3.8"}
    assert module.outdated(_lock_dir(tmp_path, module), newest=newest.__getitem__) == 0

    newest["tomlkit"] = "0.16.0"
    assert module.outdated(tmp_path, newest=newest.__getitem__) == 1
    captured = capsys.readouterr()
    assert "tomlkit==0.15.2   <- newest is 0.16.0" in captured.out
    assert "build_lock.py upgrade" in captured.err


def test_outdated_does_not_pass_when_pypi_cannot_be_asked(tmp_path):
    """An unanswered check must never read as "everything is up to date"."""
    module = _load_build_lock()

    def unreachable(name):
        raise OSError("no network")

    assert module.outdated(_lock_dir(tmp_path, module), newest=unreachable) == 1


def test_vulnerable_or_stale_pins_cannot_go_unnoticed(build_yml: str):
    """Every lock is audited on each push and before each release, and a weekly run
    checks both the audit and the newest releases. Derived from the lock list, so a
    third lock file cannot be left out of any of them."""
    locks = _load_build_lock().LOCKS
    workflows = REPO_ROOT / ".github" / "workflows"
    ci = (workflows / "ci.yml").read_text(encoding="utf-8")
    deps = (workflows / "deps.yml").read_text(encoding="utf-8")
    for name in locks:
        audit = f"pip-audit --require-hashes --disable-pip -r packaging/{name}"
        for workflow in (ci, build_yml, deps):
            assert audit in workflow, f"{name} is not audited everywhere"
    # The release audits before it stages the files it ships.
    assert build_yml.index("pip-audit") < build_yml.index("cp packaging/requirements-lock.txt")
    assert "schedule:" in deps and "cron:" in deps
    assert "python scripts/build_lock.py outdated" in deps
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "python scripts/build_lock.py outdated" in agents
    assert "A known vulnerability is never shipped" in agents


def test_tests_import_nothing_the_project_does_not_declare():
    """A test that imports a package the `dev` extra does not install passes on a
    machine that happens to have it and fails on every clean one — which is how one
    `import yaml` turned the whole CI matrix red. Walks every test and script file."""
    import ast
    import sys
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = [*project["dependencies"],
                *(spec for extra in project["optional-dependencies"].values() for spec in extra)]
    names = {spec.split(">=")[0].split("==")[0].strip().lower() for spec in declared}
    # distribution name -> the module it is imported as
    import_names = {"pyside6-essentials": "PySide6", "pyside6-addons": "PySide6",
                    "pillow": "PIL", "openrgb-python": "openrgb"}
    allowed = {import_names.get(name, name) for name in names} | {"tintaview"}

    offenders = []
    for folder in ("tests", "scripts"):
        for path in sorted((REPO_ROOT / folder).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    top = module.split(".")[0]
                    if top not in sys.stdlib_module_names and top not in allowed:
                        offenders.append(f"{path.name}: {module}")
    assert not offenders, f"imports of undeclared packages: {offenders}"

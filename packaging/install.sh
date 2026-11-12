#!/bin/sh
# TintaView installer for Linux and macOS.
#
# Usage:
#   install.sh [--prefix DIR] [--no-autostart] [--headless] [--uninstall]
#
#   --prefix DIR      Install location (default: ~/.local/share/tintaview).
#   --no-autostart    Skip wiring up a login autostart entry.
#   --headless        Skip the PySide6 "ui" extra and register a headless (no tray)
#                      autostart entry instead — for servers / WSL-only boxes with no
#                      desktop session to show a tray icon in.
#   --uninstall       Reverse everything below: remove the autostart entry and the
#                      install prefix. Config and hook installs in ~/.tintaview are left
#                      alone on purpose (see the notice printed at the end).
#
# This script IS the update mechanism on Linux/macOS: re-running it (piped or local, same
# --prefix) reinstalls TintaView into the existing virtual environment and rewrites the
# autostart entry in place — nothing is ever duplicated. Config and hook installs are
# never touched by a re-run; only `tintaview setup`/`tintaview hooks` change those.
#
# Every Python call below carries `-I` (isolated mode): without it the interpreter puts
# the directory this script is run from on its import path, and a `venv.py` or `pip.py`
# lying there — /tmp, a shared folder, a cloned repository — would run instead.
#
# Written in POSIX sh (no bashisms, no `local`) plus a portable `curl`/`wget` fallback so
# it also works piped straight from a release, with no local checkout on disk:
#
#   curl -fsSL https://github.com/GomelHawk/TintaView/releases/latest/download/install.sh | sh
#
# When piped this way, $0 is just "sh" and there is no script file to resolve a path
# from — every code path below accounts for that instead of assuming a checkout exists.

set -eu

APP_NAME="TintaView"
GITHUB_REPO="GomelHawk/TintaView"
DEFAULT_PREFIX="$HOME/.local/share/tintaview"
BIN_DIR="$HOME/.local/bin"

# Same resolution `tintaview.core.config.config_dir()` uses on non-Windows: TINTAVIEW_HOME
# wins for portable installs, tests, and the WSL split; otherwise ~/.tintaview. This is
# where the stable hook path, hook.env and config.toml live — never inside $PREFIX, so an
# uninstall/reinstall of the app itself never disturbs them.
TINTAVIEW_HOME_DIR="${TINTAVIEW_HOME:-$HOME/.tintaview}"

info() { printf '%s\n' "==> $*"; }
warn() { printf '%s\n' "WARNING: $*" >&2; }
die() { printf '%s\n' "ERROR: $*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: install.sh [--prefix DIR] [--no-autostart] [--no-wizard] [--headless] [--uninstall]

  --prefix DIR      Install location (default: $DEFAULT_PREFIX)
  --no-autostart    Do not register a login autostart entry
  --no-wizard       Do not launch the setup wizard afterwards (env: TINTAVIEW_NO_WIZARD)
  --headless        Skip the PySide6 UI extra; register a headless autostart entry
  --uninstall       Remove the autostart entry and the install under --prefix
                    (config/hooks untouched)
EOF
}

PREFIX="$DEFAULT_PREFIX"
AUTOSTART=1
WIZARD=1
[ -z "${TINTAVIEW_NO_WIZARD:-}" ] || WIZARD=0
HEADLESS=0
UNINSTALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            [ $# -ge 2 ] || die "--prefix requires a value"
            PREFIX="$2"
            shift 2
            ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"
            shift
            ;;
        --no-autostart)
            AUTOSTART=0
            shift
            ;;
        --no-wizard)
            WIZARD=0
            shift
            ;;
        --headless)
            HEADLESS=1
            shift
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown option: $1"
            ;;
    esac
done

VENV_DIR="$PREFIX/venv"
VENV_PY="$VENV_DIR/bin/python3"
LAUNCHER="$BIN_DIR/tintaview"
HOOK_BIN_DIR="$TINTAVIEW_HOME_DIR/bin"
HOOK_ENV_PATH="$TINTAVIEW_HOME_DIR/hook.env"
SYSTEMD_UNIT_DIR="$HOME/.config/systemd/user"
SYSTEMD_UNIT_PATH="$SYSTEMD_UNIT_DIR/tintaview.service"
# The applications-menu entry (Linux) -- the twin of install.ps1's Start Menu shortcut.
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
APP_DESKTOP_PATH="$APPS_DIR/tintaview.desktop"
# The menu icon as a *named* theme icon (Icon=tintaview), the XDG way. A file path in Icon=
# was tried first: GNOME Shell kept drawing the icon it had cached for the entry even after
# the path changed (measured on Ubuntu: the new 256px PNG on disk, the old wordmark shown).
ICON_THEME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
ICON_THEME_PNG="$ICON_THEME_DIR/256x256/apps/tintaview.png"
# ...and its macOS twin: a minimal app bundle in the per-user Applications folder, so
# TintaView is in Launchpad/Spotlight. Created here, never signed, so no quarantine flag.
MAC_APP_PATH="$HOME/Applications/$APP_NAME.app"

# --------------------------------------------------------------------------- uninstall

if [ "$UNINSTALL" -eq 1 ]; then
    info "Disabling autostart"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now tintaview.service >/dev/null 2>&1 || true
    fi
    if [ -x "$VENV_PY" ]; then
        "$VENV_PY" -I -c "from tintaview.install import autostart; autostart.disable()" \
            >/dev/null 2>&1 || true
    fi
    rm -f "$SYSTEMD_UNIT_PATH"
    rm -f "$HOME/.config/autostart/tintaview.desktop"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi

    info "Removing launcher $LAUNCHER"
    rm -f "$LAUNCHER"
    rm -f "$APP_DESKTOP_PATH" "$ICON_THEME_PNG"
    if [ -f "$MAC_APP_PATH/Contents/MacOS/$APP_NAME" ]; then
        rm -rf "$HOME/Applications/TintaView.app"
    fi

    # Only what this script put there, never "$PREFIX" itself: --prefix is whatever was
    # typed, and a recursive delete of it took "$HOME" with it for `--prefix "$HOME"`.
    # The virtual environment is this script's own (its pyvenv.cfg is the proof that it
    # is one); the icon is the only other file written here. The directory goes only if
    # that left it empty.
    if [ -f "$VENV_DIR/pyvenv.cfg" ]; then
        info "Removing the virtual environment at $VENV_DIR"
        rm -rf "$VENV_DIR"
    else
        warn "No virtual environment found at $VENV_DIR — nothing to remove there."
    fi
    rm -f "$PREFIX/tintaview.png"
    rmdir "$PREFIX" 2>/dev/null || true

    cat <<EOF

$APP_NAME has been uninstalled from $PREFIX.

Your configuration and agent hook installs under $TINTAVIEW_HOME_DIR were left in place
on purpose. If you also want to remove the hook entries this installed into your agents'
own config files (~/.claude/settings.json etc.), run this first, before or instead of
--uninstall, using an existing install:

  tintaview hooks uninstall --agent all

EOF
    exit 0
fi

# --------------------------------------------------------------------------- python3

# A usable interpreter needs `venv` AND `ensurepip`. Debian/Ubuntu ship ensurepip in a
# separate python3.X-venv package, and `-m venv --help` passes without it -- so checking
# only that let `python3 -m venv` fail halfway and leave a venv with no pip, which every
# later run then reused ("No module named pip"). Prefer the newest Python that is
# complete; remember the newest one found at all, to name its package if none is.
PY=""
FIRST_PY=""
for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        [ -n "$FIRST_PY" ] || FIRST_PY="$candidate"
        if "$candidate" -I -c 'import venv, ensurepip' >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    fi
done

if [ -z "$PY" ] && [ -n "$FIRST_PY" ]; then
    FIRST_VER=$("$FIRST_PY" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    die "$FIRST_PY ($FIRST_VER) cannot create a virtual environment: its 'venv'/'ensurepip'
modules are missing. On Debian/Ubuntu they are a separate package:
  sudo apt install python$FIRST_VER-venv
Then re-run this script."
fi

if [ -z "$PY" ]; then
    die "python3 was not found on PATH. Install it first, then re-run this script:
  Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip
  Fedora/RHEL:    sudo dnf install python3
  Arch:           sudo pacman -S python
  macOS:          brew install python3   (or: xcode-select --install)"
fi

PY_VERSION=$("$PY" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])')
info "Using $PY ($PY_VERSION)"

# --------------------------------------------------------------------------- source

# If this script is running from inside a TintaView checkout (not piped from curl, where
# $0 is just "sh" with no directory component), install straight from there. Otherwise
# download the latest tagged release's WHEEL and install that — never reach outside a venv
# to `pip install`, which is exactly the operation Debian/Ubuntu 24.04+ refuse with
# "externally-managed-environment" (PEP 668). The venv's own pip has no such restriction,
# so this script never needs `--break-system-packages` anywhere.
#
# The wheel, and not GitHub's auto-generated source tarball, because the wheel is what
# `SHA256SUMS.txt` covers (build.yml checksums the wheel, the sdist and both install
# scripts). The auto tarball is generated on demand and is not reproducible, so it can
# never appear in that file and could therefore never be verified. Same artifact as
# install.ps1 downloads, same checksum file, same fail-closed rule.
REPO_ROOT=""
case "$0" in
    */*)
        script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || script_dir=""
        # A checkout is this script sitting in its own `packaging/` folder beside the
        # package it installs — all three, not just "a pyproject.toml one level up".
        # `tintaview update` runs a downloaded copy of this file out of a temp folder,
        # and one level up from there is the machine's shared temp directory: a
        # `pyproject.toml` left in it by any local user made this install *their*
        # package instead of the verified release.
        if [ -n "$script_dir" ] && [ "$(basename -- "$script_dir")" = "packaging" ] \
            && [ -f "$script_dir/../pyproject.toml" ] \
            && [ -f "$script_dir/../tintaview/__init__.py" ]; then
            candidate_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
            if grep -q '^name = "tintaview"' "$candidate_root/pyproject.toml" 2>/dev/null; then
                REPO_ROOT="$candidate_root"
            fi
        fi
        ;;
esac

WORKDIR=""
cleanup() {
    # `if`, not `[ … ] && rm`: an EXIT trap whose last command is false sets the script's
    # exit status, so with no WORKDIR (installing from a local checkout) a completely
    # successful run reported failure — and `tintaview update` reads that exit code.
    if [ -n "$WORKDIR" ]; then
        rm -rf "$WORKDIR"
    fi
}
trap cleanup EXIT INT TERM

fetch() {
    # fetch URL OUTFILE — via curl if present, else wget. Neither prints to stdout.
    url="$1"
    out="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$out"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$out" "$url"
    else
        return 1
    fi
}

fetch_stdout() {
    url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O - "$url"
    else
        return 1
    fi
}

verify_checksum() {
    # verify_checksum DIR FILE SUMSFILE — fail closed, exactly like install.ps1's
    # Assert-Checksum. A missing entry or a mismatched digest deletes the download and
    # aborts: an installer that skipped the check whenever it was inconvenient would be
    # no check at all, and this is the one place a supply-chain slip is catastrophic.
    _dir="$1"
    _file="$2"
    _sums="$3"

    # sha256sum writes "<hex>  name" (text) or "<hex> *name" (binary); accept both, and
    # pull out only the one line we care about so an unrelated entry can never satisfy
    # the check below.
    _line=$(awk -v want="$_file" '
        /^[[:space:]]*#/ { next }
        NF >= 2 { name = $NF; sub(/^\*/, "", name); if (name == want) { print; exit } }
    ' "$_dir/$_sums")
    if [ -z "$_line" ]; then
        rm -f "$_dir/$_file"
        die "No checksum for $_file in the release's SHA256SUMS.txt — refusing to install an unverified download."
    fi
    printf '%s\n' "$_line" >"$_dir/expected.sha256"

    # sha256sum on Linux (coreutils), shasum on macOS (which ships no sha256sum). Both
    # resolve the filename in the checksums line relative to the working directory,
    # hence the subshell cd.
    if command -v sha256sum >/dev/null 2>&1; then
        _verified=$( (cd "$_dir" && sha256sum -c expected.sha256 >/dev/null 2>&1) && echo yes || echo no )
    elif command -v shasum >/dev/null 2>&1; then
        _verified=$( (cd "$_dir" && shasum -a 256 -c expected.sha256 >/dev/null 2>&1) && echo yes || echo no )
    else
        rm -f "$_dir/$_file"
        die "Neither sha256sum nor shasum was found, so $_file cannot be verified.
Install one (coreutils on Linux, shasum ships with Perl on macOS) and re-run — an
unverified build is never installed."
    fi

    if [ "$_verified" != yes ]; then
        rm -f "$_dir/$_file"
        die "SHA-256 mismatch for $_file: it does not match the release's SHA256SUMS.txt.
The download has been deleted — an unverified build is never installed."
    fi
    info "SHA-256 verified for $_file"
}

RELEASES_URL="https://github.com/$GITHUB_REPO/releases"

# The release's dependency lock: every package TintaView needs, pinned to the version the
# release was built and tested with, each with the SHA-256 of its files (build.yml writes
# it; it is listed in SHA256SUMS.txt like the wheel). With it pip installs exactly those
# files, so a dependency release published *after* this TintaView release — the usual way
# a compromised package arrives — is never downloaded. The pins are committed
# (packaging/) and move only on purpose, so an update replaces TintaView's own wheel and
# leaves an already-installed Qt alone. Empty means "no lock": an older release without
# one installs as it always did.
if [ "$HEADLESS" -eq 1 ]; then
    LOCK_NAME="requirements-headless-lock.txt"
else
    LOCK_NAME="requirements-lock.txt"
fi
LOCK_FILE=""

if [ -n "$REPO_ROOT" ]; then
    info "Installing from local checkout at $REPO_ROOT"
    PKG_SPEC="$REPO_ROOT"
    # The committed lock a release ships, so a checkout installs the same pins.
    if [ -f "$REPO_ROOT/packaging/$LOCK_NAME" ]; then
        LOCK_FILE="$REPO_ROOT/packaging/$LOCK_NAME"
    fi
elif [ -n "${TINTAVIEW_SOURCE_URL:-}" ]; then
    # Explicit developer/CI override: install exactly what the caller pointed at, with no
    # checksum to verify it against. Loud on purpose — nobody should reach this by
    # accident, and it is the only path in this script that installs unverified code.
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        die "Neither curl nor wget is available to download $TINTAVIEW_SOURCE_URL."
    fi
    warn "TINTAVIEW_SOURCE_URL is set — installing from $TINTAVIEW_SOURCE_URL WITHOUT any
SHA-256 verification. This override exists for development; unset it to install a
checksum-verified release."
    WORKDIR=$(mktemp -d)
    fetch "$TINTAVIEW_SOURCE_URL" "$WORKDIR/src.tar.gz" \
        || die "Could not download $TINTAVIEW_SOURCE_URL."
    tar -xzf "$WORKDIR/src.tar.gz" -C "$WORKDIR"
    SRC_DIR=$(find "$WORKDIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)
    [ -n "$SRC_DIR" ] || die "Downloaded archive did not contain a source directory."
    PKG_SPEC="$SRC_DIR"
else
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        die "Neither curl nor wget is available to download the TintaView release.
  Debian/Ubuntu:  sudo apt install curl
  Fedora/RHEL:    sudo dnf install curl
  macOS:          curl is preinstalled — check your PATH"
    fi

    info "Looking up the latest TintaView release"
    release_json=$(fetch_stdout "https://api.github.com/repos/$GITHUB_REPO/releases/latest" 2>/dev/null || true)
    TAG=$(printf '%s\n' "$release_json" \
        | grep -m1 '"tag_name"' \
        | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
    if [ -z "$TAG" ]; then
        # Never fall back to `pip install tintaview`: TintaView is deliberately not
        # published to PyPI (AGENTS.md, non-goals), so that name is unclaimed and
        # squattable — installing from it would run a stranger's code.
        die "Could not resolve a release tag from GitHub, so there is nothing to install.
Check $RELEASES_URL and your network, then re-run this script. If you already have a
checkout, run it from there instead:  sh packaging/install.sh"
    fi

    VERSION="${TAG#v}"
    # PEP 427: the distribution part of a wheel filename uses underscores. "tintaview"
    # has none, but normalising here means a future rename cannot silently break the URL
    # — same reasoning as install.ps1's $wheelName.
    WHEEL_NAME="tintaview-$VERSION-py3-none-any.whl"
    SUMS_NAME="SHA256SUMS.txt"
    BASE_URL="$RELEASES_URL/download/$TAG"

    WORKDIR=$(mktemp -d)
    info "Downloading $WHEEL_NAME"
    fetch "$BASE_URL/$WHEEL_NAME" "$WORKDIR/$WHEEL_NAME" \
        || die "Could not download $BASE_URL/$WHEEL_NAME.
The release may still be building — check $RELEASES_URL and try again shortly."
    fetch "$BASE_URL/$SUMS_NAME" "$WORKDIR/$SUMS_NAME" \
        || die "Could not download $BASE_URL/$SUMS_NAME, so $WHEEL_NAME cannot be verified.
Refusing to install an unverified build. Check $RELEASES_URL and try again shortly."

    verify_checksum "$WORKDIR" "$WHEEL_NAME" "$SUMS_NAME"
    PKG_SPEC="$WORKDIR/$WHEEL_NAME"

    # Whether this release has a lock is read from SHA256SUMS.txt, which is already
    # downloaded: listed there, the lock is required — a download that fails is the end
    # of the install, not a quiet switch to unpinned dependencies. Only a release from
    # before the lock existed (not listed) goes without.
    if grep -Eq "[[:space:]][*]?$LOCK_NAME\$" "$WORKDIR/$SUMS_NAME"; then
        fetch "$BASE_URL/$LOCK_NAME" "$WORKDIR/$LOCK_NAME" \
            || die "Could not download $BASE_URL/$LOCK_NAME.
Check your internet connection and run this again."
        verify_checksum "$WORKDIR" "$LOCK_NAME" "$SUMS_NAME"
        LOCK_FILE="$WORKDIR/$LOCK_NAME"
    fi
fi

# --------------------------------------------------------------------------- venv

# A venv is reused only if its pip works. One left by an interrupted run, or by an older
# install.sh that created it without ensurepip, has a python3 and no pip; `--clear` rebuilds
# it in place (the venv directory is the installer's own, never user data).
if [ -x "$VENV_PY" ] && "$VENV_PY" -I -m pip --version >/dev/null 2>&1; then
    info "Reusing existing virtual environment at $VENV_DIR"
else
    if [ -d "$VENV_DIR" ]; then
        warn "The virtual environment at $VENV_DIR is incomplete (no pip); recreating it."
    fi
    # `--clear` below empties the directory. It may only ever empty a virtual
    # environment: `--prefix` is whatever was typed, and a `venv` folder already there
    # with something else in it is not this script's to wipe.
    if [ -d "$VENV_DIR" ] && [ ! -f "$VENV_DIR/pyvenv.cfg" ] \
        && [ -n "$(ls -A "$VENV_DIR" 2>/dev/null)" ]; then
        die "$VENV_DIR exists and is not a Python virtual environment.
Move it away, or install somewhere else with --prefix."
    fi
    info "Creating a virtual environment at $VENV_DIR"
    mkdir -p "$PREFIX"
    "$PY" -I -m venv --clear "$VENV_DIR" \
        || die "Could not create a virtual environment at $VENV_DIR with $PY."
fi

# pip is used as the venv has it. It used to be upgraded from the index first, unpinned
# and unverified, on every install and update — so whatever "pip" was newest that day
# ran before the checksummed lock below was ever consulted.

# [openrgb] goes in either way: it is the second lighting engine, it is small and pure
# Python, and leaving it out makes the engine permanently unavailable however the user
# configures it -- reported as "the SDK server isn't answering", which sends people to
# restart software that was never the problem. [ui] (Qt, over 100 MB) is the one worth
# skipping on a headless box, since there is no tray to draw there.
if [ "$HEADLESS" -eq 1 ]; then
    EXTRA_SPEC="${PKG_SPEC}[openrgb]"
else
    EXTRA_SPEC="${PKG_SPEC}[ui,openrgb]"
fi

# The long step. In plain words, for someone who is not a developer: something is being
# installed, it takes a while, and the window must stay open. "This may take a minute"
# above a silent terminal for five minutes read as a hung install, and people closed it.
# pip's own progress is left visible for this step so that something keeps moving.
info "Installing $APP_NAME's components. This can take several minutes."
info "Please keep this window open — it is working, even when nothing seems to move."
LOCKED=0
if [ -n "$LOCK_FILE" ]; then
    # pip's own log, not captured output: it is where the reason for a failure is read
    # from, the same way on every platform (install.ps1 does exactly this).
    PIP_LOG=$(mktemp)
    if "$VENV_PY" -I -m pip install --require-hashes --only-binary :all: \
        --log "$PIP_LOG" -r "$LOCK_FILE"; then
        LOCKED=1
    elif grep -q "DO NOT MATCH THE HASHES" "$PIP_LOG" 2>/dev/null; then
        # A file that is not the one the release was built with. Never installed, and
        # never worked around by falling back to "whatever the index serves".
        rm -f "$PIP_LOG"
        die "A downloaded dependency does not match the checksum in this release's $LOCK_NAME.
Nothing was installed from it. This can be a corrupted download or a tampered package —
try again on another network, and report it at $RELEASES_URL if it repeats."
    elif grep -Eq "require a different python version|Hashes are required in --require-hashes mode|all requirements must have their versions pinned" "$PIP_LOG" 2>/dev/null; then
        # The lock does not fit this machine: pip itself says the pinned versions do
        # not support this Python, or that this system needs a package the lock does
        # not list. Only then: install as before the lock existed, and say so.
        warn "Could not install the exact dependency versions this release was built with
(they do not support Python $PY_VERSION on this system); installing the newest compatible ones instead."
    else
        # Everything else — no connection, an index that did not answer, a pinned file
        # that is gone — stops here. Carrying on unpinned would let whoever can make a
        # download fail choose what gets installed.
        rm -f "$PIP_LOG"
        die "Could not download $APP_NAME's components (see the messages above). Nothing was
changed in your existing install. Check your internet connection and run this again."
    fi
    rm -f "$PIP_LOG"
fi
if [ "$LOCKED" -eq 0 ]; then
    "$VENV_PY" -I -m pip install --upgrade "$EXTRA_SPEC"
fi
info "Installing $APP_NAME itself"

# ...and then plant TintaView's own code unconditionally, the same repair pass install.ps1
# runs. `--upgrade` compares version numbers and does nothing when they match, so without
# this a re-run cannot repair a damaged install, and any release that reuses a version
# string (a re-tag, or a dev build) silently leaves the old code in place while reporting
# success. `--no-deps` keeps it to the one artifact, so the dependency resolution above is
# not repeated.
"$VENV_PY" -I -m pip install --quiet --force-reinstall --no-deps "$PKG_SPEC"

# --------------------------------------------------------------------------- launcher

mkdir -p "$BIN_DIR"
cat >"$LAUNCHER" <<EOF
#!/bin/sh
# Generated by TintaView's install.sh — re-run install.sh to update, don't hand-edit.
exec "$VENV_DIR/bin/tintaview" "\$@"
EOF
chmod +x "$LAUNCHER"
info "Installed launcher: $LAUNCHER"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not on your PATH. Add this to your shell profile (~/.bashrc, ~/.zshrc, ...):
    export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

# --------------------------------------------------------------------------- hook

info "Installing the hook script to $HOOK_BIN_DIR"
HOOK_SRC=$("$VENV_PY" -I -c \
    "import pathlib, tintaview; print(pathlib.Path(tintaview.__file__).resolve().parent / 'hooks' / 'tv-hook.sh')")
if [ ! -f "$HOOK_SRC" ]; then
    die "tv-hook.sh was not found inside the installed package at $HOOK_SRC — the install is broken."
fi
mkdir -p "$HOOK_BIN_DIR"
cp "$HOOK_SRC" "$HOOK_BIN_DIR/tv-hook.sh"
chmod +x "$HOOK_BIN_DIR/tv-hook.sh"

# hook.env is written once and never touched again: it is what every agent's hook config
# ends up pointing at indirectly (via the stable tv-hook.sh path), so a later `tintaview
# setup` run that changes the port is what should update it — not a reinstall.
if [ ! -f "$HOOK_ENV_PATH" ]; then
    mkdir -p "$TINTAVIEW_HOME_DIR"
    cat >"$HOOK_ENV_PATH" <<EOF
TINTAVIEW_URL=http://127.0.0.1:8777
TINTAVIEW_CURL=curl
EOF
    info "Wrote $HOOK_ENV_PATH"
else
    info "$HOOK_ENV_PATH already exists — left untouched"
fi

# --------------------------------------------------------------------------- autostart

if [ "$AUTOSTART" -eq 0 ]; then
    info "Skipping autostart (--no-autostart)"
elif [ "$HEADLESS" -eq 1 ]; then
    # tintaview.install.autostart targets the tray (After/WantedBy=graphical-session.target),
    # which never arrives on a box with no desktop session — so a headless install writes
    # its own unit here instead, gated on the ordinary login target and running
    # `tintaview --headless` (broker only, no Qt import at all).
    info "Registering a headless autostart service (systemd --user)"
    if command -v systemctl >/dev/null 2>&1; then
        mkdir -p "$SYSTEMD_UNIT_DIR"
        cat >"$SYSTEMD_UNIT_PATH" <<EOF
[Unit]
Description=TintaView status broker (headless)

[Service]
ExecStart=$LAUNCHER --headless
Restart=on-failure

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now tintaview.service \
            || warn "systemd unit written but 'enable --now' failed; run 'systemctl --user status tintaview.service' to see why."
    else
        warn "No systemd --user found; skipping headless autostart. Start it yourself with: $LAUNCHER --headless"
    fi
else
    info "Registering autostart"
    if "$VENV_PY" -I -c "from tintaview.install import autostart; import sys; sys.exit(0 if autostart.enable() else 1)"; then
        info "Autostart registered"
    else
        warn "Autostart could not be fully configured automatically; run 'tintaview doctor' after setup to check it."
    fi
fi

# --------------------------------------------------------------------------- app menu

# The square brand mark -- the tray's own icon -- rendered by the installed app, never the
# wide wordmark asset, which the app grid squeezes onto a black square. It lives
# in $PREFIX, so --uninstall removes it with everything else.
APP_ICON_PNG="$PREFIX/tintaview.png"
render_app_icon() {
    QT_QPA_PLATFORM=offscreen "$VENV_PY" -I -c "
import sys
from pathlib import Path
from PySide6.QtGui import QGuiApplication
app = QGuiApplication([])
from tintaview.ui.icons import write_brand_png
sys.exit(0 if write_brand_png(Path(sys.argv[1]), 256) else 1)
" "$APP_ICON_PNG" >/dev/null 2>&1
}

# A headless box has no menu to add to.
if [ "$(uname -s)" = "Darwin" ] && [ "$HEADLESS" -eq 0 ]; then
    mkdir -p "$MAC_APP_PATH/Contents/MacOS" "$MAC_APP_PATH/Contents/Resources"
    if render_app_icon && command -v sips >/dev/null 2>&1; then
        sips -s format icns "$APP_ICON_PNG" --out "$MAC_APP_PATH/Contents/Resources/AppIcon.icns" \
            >/dev/null 2>&1 || warn "Could not convert the app icon; Launchpad will show a generic one."
    fi
    cat >"$MAC_APP_PATH/Contents/MacOS/$APP_NAME" <<EOF
#!/bin/sh
exec "$LAUNCHER" "\$@"
EOF
    chmod +x "$MAC_APP_PATH/Contents/MacOS/$APP_NAME"
    # LSUIElement: a menu-bar app, no Dock icon -- the same as the tray it starts.
    cat >"$MAC_APP_PATH/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>io.github.gomelhawk.tintaview</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF
    info "Added $APP_NAME to Applications ($MAC_APP_PATH)"
fi

if [ "$(uname -s)" = "Linux" ] && [ "$HEADLESS" -eq 0 ]; then
    if render_app_icon; then
        mkdir -p "$(dirname "$ICON_THEME_PNG")"
        cp "$APP_ICON_PNG" "$ICON_THEME_PNG"
        if command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -f -t "$ICON_THEME_DIR" >/dev/null 2>&1 || true
        fi
        ICON_PATH="tintaview"
    else
        warn "Could not render the app icon; the applications menu will show a generic one."
        ICON_PATH="application-x-executable"
    fi
    mkdir -p "$APPS_DIR"
    cat >"$APP_DESKTOP_PATH" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=AI coding agent status and usage in the tray
Exec=$LAUNCHER
Icon=$ICON_PATH
Terminal=false
Categories=Utility;Development;
EOF
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    fi
    info "Added $APP_NAME to the applications menu ($APP_DESKTOP_PATH)"
fi

# Qt's X11 platform plugin needs these libraries (measured: `ldd` on PySide6's libqxcb.so
# and libQt6XcbQpa.so.6), and a stock Ubuntu desktop does not ship all of them. Without
# them the tray cannot start on an X11 session, and on GNOME it cannot use the X11 tray
# icon that lets a left click open the usage panel (ui/tray.py, `_use_legacy_tray`) -- it
# then falls back to the D-Bus icon, whose left click GNOME turns into the menu.
# ldconfig is in /sbin, which is not on every user's PATH.
LDCONFIG=$(command -v ldconfig 2>/dev/null || echo /sbin/ldconfig)
if [ "$(uname -s)" = "Linux" ] && [ "$HEADLESS" -eq 0 ] && [ -x "$LDCONFIG" ]; then
    # Each library and the package that ships it, per package manager:
    #   soname : Debian/Ubuntu (apt) : Fedora (dnf) : Arch (pacman)
    if command -v apt-get >/dev/null 2>&1; then
        PKG_COL=2; PKG_CMD="sudo apt-get install -y"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_COL=3; PKG_CMD="sudo dnf install -y"
    elif command -v pacman >/dev/null 2>&1; then
        PKG_COL=4; PKG_CMD="sudo pacman -S --needed --noconfirm"
    else
        PKG_COL=""; PKG_CMD=""
    fi
    LIBS_KNOWN=$("$LDCONFIG" -p 2>/dev/null || true)
    MISSING_LIBS=""
    MISSING_PKGS=""
    for row in \
            libxcb-cursor.so.0:libxcb-cursor0:xcb-util-cursor:xcb-util-cursor \
            libxcb-icccm.so.4:libxcb-icccm4:xcb-util-wm:xcb-util-wm \
            libxcb-image.so.0:libxcb-image0:xcb-util-image:xcb-util-image \
            libxcb-keysyms.so.1:libxcb-keysyms1:xcb-util-keysyms:xcb-util-keysyms \
            libxcb-render-util.so.0:libxcb-render-util0:xcb-util-renderutil:xcb-util-renderutil \
            libxcb-xkb.so.1:libxcb-xkb1:libxcb:libxcb \
            libxkbcommon-x11.so.0:libxkbcommon-x11-0:libxkbcommon-x11:libxkbcommon-x11; do
        soname=$(printf '%s' "$row" | cut -d: -f1)
        case "$LIBS_KNOWN" in
            *"$soname "*) ;;
            *)
                MISSING_LIBS="$MISSING_LIBS $soname"
                if [ -n "$PKG_COL" ]; then
                    MISSING_PKGS="$MISSING_PKGS $(printf '%s' "$row" | cut -d: -f"$PKG_COL")"
                fi
                ;;
        esac
    done
    if [ -n "$MISSING_LIBS" ]; then
        warn "Qt's X11 libraries are missing:$MISSING_LIBS
  On GNOME a left click on the tray icon cannot open the usage panel without them
  (until then: double-click the icon, or open $APP_NAME from the applications menu),
  and on an X11 session the tray cannot start at all."
        # Asked, never assumed: it is the one step that needs sudo. A non-interactive run
        # (CI, `tintaview update`) only gets the command to run.
        LIBS_ANSWER=n
        if [ -n "$PKG_CMD" ] && ( : </dev/tty ) 2>/dev/null; then
            printf '%s' "Install them now with: $PKG_CMD$MISSING_PKGS? [Y/n] " >/dev/tty
            read -r LIBS_ANSWER </dev/tty || LIBS_ANSWER=n
            case "$LIBS_ANSWER" in [nN]*) LIBS_ANSWER=n ;; *) LIBS_ANSWER=y ;; esac
        fi
        if [ "$LIBS_ANSWER" = y ]; then
            # shellcheck disable=SC2086 -- one word per package
            $PKG_CMD $MISSING_PKGS </dev/tty \
                || warn "Installing them failed; run: $PKG_CMD$MISSING_PKGS"
        elif [ -n "$PKG_CMD" ]; then
            warn "Install them later with:  $PKG_CMD$MISSING_PKGS
  then restart $APP_NAME (Quit in its menu, then start it from the applications menu)."
        else
            warn "Install the packages that provide them with your distribution's package
  manager, then restart $APP_NAME."
        fi
    fi
fi

# --------------------------------------------------------------------------- stop

# install.ps1's Stop-RunningApp, at the same point: before the wizard. A reinstall that left
# the running tray alone kept the *old* code on screen until the next login, so every fix
# looked like it had changed nothing; stopped *after* the wizard, the wizard's own restart
# brought the old tray back first and it was started twice. Graceful /quit, so the lights
# are handed back.
if [ "$HEADLESS" -eq 0 ]; then
    "$VENV_PY" -I -c "
import sys
from tintaview.core.config import load
from tintaview.install import restart
cfg = load()
pid = restart.running_pid(cfg)
sys.exit(0 if pid is None or restart._stop(pid, cfg) else 1)
" >/dev/null 2>&1 \
        || warn "Could not stop the running $APP_NAME; quit it from its menu and start it again."
fi

# --------------------------------------------------------------------------- wizard

# The same step install.ps1 runs. `curl ... | sh` makes stdin the script itself, so the
# wizard reads the terminal through /dev/tty; with none (CI, `tintaview update` from the
# tray) it is skipped and the closing message says how to run it. The full launcher path,
# never a bare `tintaview`: ~/.local/bin is only on PATH if it existed at login, which on a
# first install it did not -- "tintaview: command not found" right after installing.
if [ "$WIZARD" -eq 0 ]; then
    info "Skipping the setup wizard (--no-wizard)"
elif ( : </dev/tty ) 2>/dev/null; then
    info "Launching the setup wizard"
    "$LAUNCHER" setup </dev/tty \
        || warn "The setup wizard exited with an error. Re-run it any time with: $LAUNCHER setup"
else
    info "No terminal to run the setup wizard in; run it yourself with: $LAUNCHER setup"
fi

# --------------------------------------------------------------------------- start

# Autostart only fires at the next login, so without this the install ends with no tray
# icon at all -- which reads exactly like a broken install (install.ps1 does the same).
# Started in the *desktop session*, which an SSH or tty shell is not: there the login's own
# service is what can reach it -- the systemd --user unit on Linux, the LaunchAgent on
# macOS (`launchctl kickstart` in the gui/<uid> domain), the same units autostart writes.
MAC_AGENT_PLIST="$HOME/Library/LaunchAgents/com.tintaview.app.plist"
if [ "$HEADLESS" -eq 0 ]; then
    if [ "$(uname -s)" = "Linux" ] && [ -f "$SYSTEMD_UNIT_PATH" ] \
            && systemctl --user is-active --quiet graphical-session.target 2>/dev/null; then
        info "Starting $APP_NAME in your desktop session"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user restart tintaview.service \
            || warn "Could not start it; start $APP_NAME from the applications menu."
    elif [ "$(uname -s)" = "Darwin" ] && [ -f "$MAC_AGENT_PLIST" ] \
            && launchctl kickstart -k "gui/$(id -u)/com.tintaview.app" >/dev/null 2>&1; then
        info "Started $APP_NAME in your desktop session"
    elif [ "$(uname -s)" = "Darwin" ] || [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        info "Starting $APP_NAME"
        nohup "$LAUNCHER" >/dev/null 2>&1 &
    else
        info "No desktop session here; start $APP_NAME from the applications menu (or log in)."
    fi
fi

# --------------------------------------------------------------------------- done

case ":$PATH:" in
    *":$BIN_DIR:"*) RUN_HINT="tintaview" ;;
    *) RUN_HINT="$LAUNCHER" ;;
esac

cat <<EOF

$APP_NAME is installed at $PREFIX.
Launcher:  $LAUNCHER
Hook:      $HOOK_BIN_DIR/tv-hook.sh
Config:    $TINTAVIEW_HOME_DIR/config.toml (written by 'tintaview setup')

Change agents, lighting engine or hooks any time with:  $RUN_HINT setup
EOF

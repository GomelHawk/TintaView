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
# Written in POSIX sh (no bashisms, no `local`) plus a portable `curl`/`wget` fallback so
# it also works piped straight from a release, with no local checkout on disk:
#
#   curl -fsSL https://raw.githubusercontent.com/GomelHawk/TintaView/main/packaging/install.sh | sh
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
Usage: install.sh [--prefix DIR] [--no-autostart] [--headless] [--uninstall]

  --prefix DIR      Install location (default: $DEFAULT_PREFIX)
  --no-autostart    Do not register a login autostart entry
  --headless        Skip the PySide6 UI extra; register a headless autostart entry
  --uninstall       Remove the autostart entry and --prefix (config/hooks untouched)
EOF
}

PREFIX="$DEFAULT_PREFIX"
AUTOSTART=1
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

# --------------------------------------------------------------------------- uninstall

if [ "$UNINSTALL" -eq 1 ]; then
    info "Disabling autostart"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now tintaview.service >/dev/null 2>&1 || true
    fi
    if [ -x "$VENV_PY" ]; then
        "$VENV_PY" -c "from tintaview.install import autostart; autostart.disable()" \
            >/dev/null 2>&1 || true
    fi
    rm -f "$SYSTEMD_UNIT_PATH"
    rm -f "$HOME/.config/autostart/tintaview.desktop"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi

    info "Removing launcher $LAUNCHER"
    rm -f "$LAUNCHER"

    info "Removing $PREFIX"
    rm -rf "$PREFIX"

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

PY=""
for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    die "python3 was not found on PATH. Install it first, then re-run this script:
  Debian/Ubuntu:  sudo apt install python3 python3-venv python3-pip
  Fedora/RHEL:    sudo dnf install python3
  Arch:           sudo pacman -S python
  macOS:          brew install python3   (or: xcode-select --install)"
fi

if ! "$PY" -m venv --help >/dev/null 2>&1; then
    die "$PY has no 'venv' module. On Debian/Ubuntu this is a separate package:
  sudo apt install python3-venv
Then re-run this script."
fi

PY_VERSION=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
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
        if [ -n "$script_dir" ] && [ -f "$script_dir/../pyproject.toml" ]; then
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

if [ -n "$REPO_ROOT" ]; then
    info "Installing from local checkout at $REPO_ROOT"
    PKG_SPEC="$REPO_ROOT"
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
fi

# --------------------------------------------------------------------------- venv

if [ -x "$VENV_PY" ]; then
    info "Reusing existing virtual environment at $VENV_DIR"
else
    info "Creating a virtual environment at $VENV_DIR"
    mkdir -p "$PREFIX"
    "$PY" -m venv "$VENV_DIR"
fi

info "Upgrading pip inside the virtual environment"
"$VENV_PY" -m pip install --quiet --upgrade pip

# [openrgb] goes in either way: it is the second lighting engine, it is small and pure
# Python, and leaving it out makes the engine permanently unavailable however the user
# configures it -- reported as "the SDK server isn't answering", which sends people to
# restart software that was never the problem. [ui] (PySide6, ~100MB) is the one worth
# skipping on a headless box, since there is no tray to draw there.
if [ "$HEADLESS" -eq 1 ]; then
    EXTRA_SPEC="${PKG_SPEC}[openrgb]"
else
    EXTRA_SPEC="${PKG_SPEC}[ui,openrgb]"
fi

info "Installing $APP_NAME ($EXTRA_SPEC) — this may take a minute the first time"
"$VENV_PY" -m pip install --quiet --upgrade "$EXTRA_SPEC"

# ...and then plant TintaView's own code unconditionally, the same repair pass install.ps1
# runs. `--upgrade` compares version numbers and does nothing when they match, so without
# this a re-run cannot repair a damaged install, and any release that reuses a version
# string (a re-tag, or a dev build) silently leaves the old code in place while reporting
# success. `--no-deps` keeps it to the one artifact, so the dependency resolution above is
# not repeated.
"$VENV_PY" -m pip install --quiet --force-reinstall --no-deps "$PKG_SPEC"

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
HOOK_SRC=$("$VENV_PY" -c \
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
    if "$VENV_PY" -c "from tintaview.install import autostart; import sys; sys.exit(0 if autostart.enable() else 1)"; then
        info "Autostart registered"
    else
        warn "Autostart could not be fully configured automatically; run 'tintaview doctor' after setup to check it."
    fi
fi

# --------------------------------------------------------------------------- done

cat <<EOF

$APP_NAME is installed at $PREFIX.
Launcher:  $LAUNCHER
Hook:      $HOOK_BIN_DIR/tv-hook.sh
Config:    $TINTAVIEW_HOME_DIR/config.toml (written by 'tintaview setup')

Next: run 'tintaview setup' to choose your agents, lighting engine and install the hooks.
EOF

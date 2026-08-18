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
# download the source for the latest tagged release and install from that instead — never
# reach outside a venv to `pip install`, which is exactly the operation Debian/Ubuntu
# 24.04+ refuse with "externally-managed-environment" (PEP 668). The venv's own pip has no
# such restriction, so this script never needs `--break-system-packages` anywhere.
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
    [ -n "$WORKDIR" ] && rm -rf "$WORKDIR"
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

if [ -n "$REPO_ROOT" ]; then
    info "Installing from local checkout at $REPO_ROOT"
    PKG_SPEC="$REPO_ROOT"
else
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        die "Neither curl nor wget is available to download the TintaView release."
    fi

    TARBALL_URL="${TINTAVIEW_SOURCE_URL:-}"
    if [ -z "$TARBALL_URL" ]; then
        info "Looking up the latest TintaView release"
        release_json=$(fetch_stdout "https://api.github.com/repos/$GITHUB_REPO/releases/latest" 2>/dev/null || true)
        tag=$(printf '%s\n' "$release_json" \
            | grep -m1 '"tag_name"' \
            | sed -E 's/.*"tag_name":[[:space:]]*"([^"]+)".*/\1/')
        if [ -n "$tag" ]; then
            TARBALL_URL="https://github.com/$GITHUB_REPO/archive/refs/tags/$tag.tar.gz"
        fi
    fi

    if [ -n "$TARBALL_URL" ]; then
        WORKDIR=$(mktemp -d)
        info "Downloading source from $TARBALL_URL"
        if fetch "$TARBALL_URL" "$WORKDIR/src.tar.gz"; then
            tar -xzf "$WORKDIR/src.tar.gz" -C "$WORKDIR"
            SRC_DIR=$(find "$WORKDIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)
            if [ -z "$SRC_DIR" ]; then
                die "Downloaded archive did not contain a source directory."
            fi
            PKG_SPEC="$SRC_DIR"
        else
            warn "Could not download $TARBALL_URL; falling back to 'pip install tintaview'."
            PKG_SPEC="tintaview"
        fi
    else
        warn "Could not resolve a release tag from GitHub; falling back to 'pip install tintaview'."
        PKG_SPEC="tintaview"
    fi
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

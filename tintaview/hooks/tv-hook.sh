#!/bin/sh
# TintaView hook shim — invoked by the agent on every tool call, so it must be ~5ms
# and must never fail the agent's turn. No Python, no jq: sh + curl + sed only.
#
# Usage: tv-hook.sh <agent> <event>
#   agent: claude | codex | cursor   (also the ?agent= query value)
#   event: a tintaview.core.events constant, e.g. tool-start
#
# Session id field varies by agent: Cursor's hook stdin carries "conversation_id",
# every other agent carries "session_id". Falls back to "default" when absent so a
# malformed or empty payload still produces a single stable session bucket rather
# than erroring out.

AGENT="$1"
EVENT="$2"

# Find hook.env: TINTAVIEW_HOME wins (portable installs), else the fixed per-user
# location the installer writes to. Sourcing is optional — sane defaults below cover
# a from-scratch checkout with the daemon on its default port.
if [ -n "$TINTAVIEW_HOME" ] && [ -f "$TINTAVIEW_HOME/hook.env" ]; then
    . "$TINTAVIEW_HOME/hook.env"
elif [ -f "$HOME/.tintaview/hook.env" ]; then
    . "$HOME/.tintaview/hook.env"
fi

# TINTAVIEW_CURL defaults to plain curl; the WSL installer sets it to curl.exe so the
# request runs in the Windows network namespace and reaches a daemon on the Windows
# side with no firewall rule needed — preserve that override, don't hardcode curl.
: "${TINTAVIEW_URL:=http://127.0.0.1:8777}"
: "${TINTAVIEW_CURL:=curl}"

# Pick the session-id field name for this agent. This is deliberately not a JSON parser:
# it matches "<field>"<ws>:<ws>"<value>" and takes the first hit, which is all the
# agents' flat hook payloads need.
if [ "$AGENT" = "cursor" ]; then
    FIELD="conversation_id"
else
    FIELD="session_id"
fi

# ONE sed, reading stdin directly, doing the extraction *and* the safe-character check in
# the same expression and quitting at the first hit. The previous shape — `head -c` into
# a shell variable, then `printf | sed | head -n 1`, then `printf | sed` again — forked
# about seven processes on every single tool call, on a script whose whole budget is
# ~5 ms. It also needed `head -c`, which is not in every POSIX `head`.
#
# The value is matched against the safe character set rather than filtered through it, so
# a payload carrying anything else fails to match and falls back to "default" — a hook
# must never be able to break the request line, and quietly rewriting a session id into a
# *different* valid one would silently merge two sessions into one bucket.
#
# Guarded on a tty because a manual `tv-hook.sh claude working` at an interactive terminal
# must return instantly rather than block on a stdin read that will never come.
SID=""
if [ ! -t 0 ]; then
    SID=$(sed -n "/\"$FIELD\"[[:space:]]*:/{s/.*\"$FIELD\"[[:space:]]*:[[:space:]]*\"\\([A-Za-z0-9._-]*\\)\".*/\\1/p;q;}")
fi
[ -n "$SID" ] || SID="default"

# Fire and forget: short timeout, discard output, and always exit 0 — whatever
# happens here (daemon down, curl missing, network namespace weirdness) must never
# surface as a hook failure to the agent.
"$TINTAVIEW_CURL" -s -m 1 "$TINTAVIEW_URL/v1/event/$EVENT?agent=$AGENT&sid=$SID" >/dev/null 2>&1

exit 0

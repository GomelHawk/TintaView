@echo off
REM TintaView hook shim for Windows-native agents (invoked on every tool call, must be
REM fast and must never fail the agent's turn).
REM
REM Usage: tv-hook.cmd <agent> <event>
REM
REM LIMITATION: batch has no cheap JSON parser, and spinning up PowerShell just to read
REM one field would blow the ~5ms budget these hooks run under. So this does a crude
REM substring scrape of "session_id":"..." (or "conversation_id":"..." for cursor) out
REM of the first line of stdin, using only variable substring substitution (no external
REM process per call), and falls back to sid=default whenever the payload doesn't match
REM that exact compact-JSON shape (spaces around ":", missing field, empty stdin, etc).
REM Practical effect: on a Windows-native install, multiple concurrent sessions of the
REM same agent can collapse
REM into one "default" session bucket — status is still correct, just not always split
REM per-session. WSL installs are unaffected: they use tv-hook.sh, which has real
REM sed-based extraction and does not share this limitation.

setlocal enabledelayedexpansion

set "AGENT=%~1"
set "EVENT=%~2"

REM Locate hook.env: TINTAVIEW_HOME wins (portable installs), else the fixed per-user
REM location the installer writes to under %LOCALAPPDATA%\TintaView.
set "ENVFILE="
if defined TINTAVIEW_HOME if exist "%TINTAVIEW_HOME%\hook.env" set "ENVFILE=%TINTAVIEW_HOME%\hook.env"
if not defined ENVFILE if exist "%LOCALAPPDATA%\TintaView\hook.env" set "ENVFILE=%LOCALAPPDATA%\TintaView\hook.env"
if defined ENVFILE (
    for /f "usebackq tokens=1,* delims==" %%A in ("!ENVFILE!") do (
        if /i "%%A"=="TINTAVIEW_URL" set "TINTAVIEW_URL=%%B"
        if /i "%%A"=="TINTAVIEW_CURL" set "TINTAVIEW_CURL=%%B"
    )
)

if not defined TINTAVIEW_URL set "TINTAVIEW_URL=http://127.0.0.1:8777"
if not defined TINTAVIEW_CURL set "TINTAVIEW_CURL=curl.exe"

REM Which field name to look for: cursor sends conversation_id, everyone else session_id.
set "FIELD=session_id"
if /i "%AGENT%"=="cursor" set "FIELD=conversation_id"

REM Grab one line of piped stdin, if any. `set /p` returns immediately (LINE stays
REM undefined) when stdin is already at EOF, which is what an empty pipe looks like;
REM it only blocks waiting for a line on a real interactive console with nothing
REM redirected in, same as any other `set /p` in a batch script.
REM Every variable this script tests with `if defined` must be cleared first: a hook
REM inherits the agent's whole environment, so an unset TOKEN/AFTER/CHECK would silently
REM pick up whatever the user happens to export under that name — and TOKEN in particular
REM is very often a real API secret, which would then be sent as ?sid=.
set "LINE="
set "TOKEN="
set "AFTER="
set "CHECK="
set /p LINE=

set "SID=default"
if defined LINE (
    REM Cut everything up to and including the literal `"<field>":"`, leaving the
    REM value at the front, e.g. LINE=..."session_id":"abc-123","tool":"Bash"}
    REM                           -> AFTER=abc-123","tool":"Bash"}
    REM If the field isn't present at all, this substitution is a no-op (AFTER stays
    REM equal to LINE), which the next line detects and skips.
    set "AFTER=!LINE:*"%FIELD%":"=!"
    if not "!AFTER!"=="!LINE!" (
        REM Now cut everything from the value's closing quote onward. `for /f` with a
        REM doublequote delimiter needs its options unquoted-and-caret-escaped — the
        REM standard way to hand FOR /F a literal `"` as a delimiter character.
        for /f tokens^=1^ delims^=^" %%V in ("!AFTER!") do set "TOKEN=%%V"
    )
)

REM Never build a URL out of anything containing characters that could break the
REM command line or the query string — reject and fall back to default instead of
REM trying to escape them. (TOKEN can't contain a literal quote — the `for /f` above
REM already cut it at the first one — so only the shell/URL metacharacters need
REM stripping here. Each substitution is its own `set` so none of them has to mix
REM multiple special characters into one quoted string, which is what breaks cmd's
REM own quote-balance tracking.)
if defined TOKEN (
    set "CHECK=!TOKEN!"
    set "CHECK=!CHECK:&=!"
    set "CHECK=!CHECK:%%=!"
    set "CHECK=!CHECK: =!"
    set "CHECK=!CHECK:<=!"
    set "CHECK=!CHECK:>=!"
    set "CHECK=!CHECK:|=!"
    set "CHECK=!CHECK:^=!"
    if "!CHECK!"=="!TOKEN!" if not "!TOKEN!"=="" set "SID=!TOKEN!"
)

REM Fire and forget: short timeout, discard output, always succeed regardless of what
REM happened above — a hook must never fail the agent's turn.
"%TINTAVIEW_CURL%" -s -m 1 "%TINTAVIEW_URL%/v1/event/%EVENT%?agent=%AGENT%&sid=%SID%" >nul 2>&1

exit /b 0

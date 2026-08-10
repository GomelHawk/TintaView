# Migrating from `claude_code_razer_lights`

TintaView is the successor to
[`claude_code_razer_lights`](https://github.com/GomelHawk/claude-code-razer-lights).
This is about *your* migration — pointing at your existing install and getting it onto
TintaView without a hard cutover.

## What's new

- **Three agents, not one.** Claude Code, Codex CLI and Cursor, each with its own hook
  mapping and usage provider (see the README's [status table](../README.md#what-works-where)).
- **One process, not two.** The old project ran `razer_light_server.py`/`.exe` (a
  Scheduled Task) and `tray_app.py`/`.exe` (a Startup shortcut) separately. TintaView
  runs the status broker and the tray in the same process — one autostart entry, one
  log file, one thing to update.
- **Two lighting engines.** Chroma is still the default and behaves the same way; there
  is now also an OpenRGB backend that covers Linux and non-Razer hardware, with its own
  snapshot/restore and Direct-mode handling (OpenRGB has no built-in session concept the
  way the Chroma SDK does).
- **A real installer.** `Setup.exe` on Windows (unsigned — see the README), `install.sh`
  on Linux/macOS, and a guided `tintaview setup` wizard that installs hooks with a
  before/after diff and your explicit confirmation, instead of hand-merging a JSON
  snippet.
- **`tintaview doctor`** — a single command that checks environment, config, the daemon,
  the engine, the hook script, every agent's hook install state and its usage stats, in
  one pass. There was no equivalent before.

## Your old `hook.sh` keeps working — this is incremental, not a cutover

The new daemon serves the old server's exact endpoints
(`/session-start`, `/session-end`, `/working`, `/idle`, `/confirm`, all with a bare
`?sid=`) as back-compat aliases, defaulting the agent to `claude` since the old hook
never sent one. **You do not have to touch your existing Claude Code hooks to start
using TintaView** — point `hook.sh` (or `hook.env`'s `TINTAVIEW_URL`/equivalent) at a
running TintaView daemon on the same port (`8777` by default) and it keeps working
exactly as before.

This means you can install TintaView, run its wizard, and try Codex/Cursor support or
OpenRGB without disturbing your working Claude Code + Chroma setup at all — migrate one
piece at a time, verify each with `tintaview doctor`, and only remove the old install
once you're happy.

## Moving off the old two-process setup

The old setup had two independent autostart mechanisms:

- a **Scheduled Task** (`RazerLights`) running `RazerLightServer.exe`/
  `razer_light_server.py`, and
- a **Startup-folder shortcut** running `ClaudeRazerTray.exe`/`tray_app.py`.

TintaView replaces both with a single autostart entry for the single `tintaview`
process — a Startup shortcut on Windows, a systemd `--user` unit + XDG autostart entry
on Linux, or a launchd agent on macOS, all set up by the wizard's "Start automatically"
step. To retire the old setup once TintaView is running well:

1. **Windows**: remove the old Scheduled Task and Startup shortcut —
   `Unregister-ScheduledTask -TaskName RazerLights` (Administrator PowerShell) and
   delete `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ClaudeRazerTray.lnk`.
   Stop and delete the old `RazerLightServer.exe`/`ClaudeRazerTray.exe` process(es) if
   still running.
2. **Linux/macOS**: `systemctl --user disable --now razer-lights.service
   razer-lights-tray.service` (if you followed the old README's systemd setup), or
   `launchctl unload` the two old `com.claude.razer-*.plist` files on macOS.
3. Confirm TintaView's own autostart is enabled instead: `tintaview doctor` reports
   `autostart at login is enabled` under `ENVIRONMENT` if it worked; if not, re-run
   `tintaview setup` and keep the autostart step checked.

## Environment variable changes

The old project read a handful of env vars directly. TintaView's equivalents are all
**config, not environment variables** — there is no `RAZER_*`-to-`TINTAVIEW_*` renamed
env var to export instead; set these in `config.toml` (or via the wizard) instead:

| Old | Old purpose | New equivalent |
| --- | --- | --- |
| `RAZER_LIGHTS=0` | Skip the Chroma SDK; status + usage only | `engine.mode = "none"` in `config.toml`, or pick "Status-only" in the wizard's engine step |
| `CLAUDE_HOME` | Override the Claude data directory (used for the WSL split) | `agents.claude.home` in `config.toml` — the wizard sets this to the right UNC path automatically on a WSL-split install |
| `RAZER_STATE_URL` | Where the tray reads `/state` from | Not needed — the tray reads state directly in-process from the daemon it started; a separate URL only matters for a headless daemon + something else polling it, which still just uses `server.host`/`server.port` |
| `USAGE_TIMEOUT` | Timeout (seconds) for the usage endpoint call | No config equivalent currently — the Claude provider uses a fixed internal timeout |

Nothing reads the old `RAZER_*`/`CLAUDE_HOME` variable names today — if your old
autostart entries set them, they're simply ignored by TintaView, not translated. Move
the equivalent setting into `config.toml` (directly, or by re-running `tintaview setup`)
instead of relying on the old environment variables carrying over.

## Removing the old hooks, installing the new ones

The old README had you hand-merge `claude-settings.example.json` into
`~/.claude/settings.json`; TintaView installs hooks with `tintaview hooks install` (or
the wizard's hooks step) instead, and only ever touches entries whose command contains
the `tv-hook` sentinel. That has one consequence worth knowing: **`tintaview hooks
uninstall` will not remove your old hand-merged hook.sh entries** — they don't carry the
sentinel, so TintaView correctly treats them as yours, not its own.

To fully switch a given agent over:

1. `tintaview hooks install --agent claude` — review the diff, confirm it. Your old
   `hook.sh` entries are left in place alongside the new `tv-hook` ones (both fire; see
   above for why that's harmless during migration).
2. Confirm the new install works: `tintaview doctor` should show `claude: installed`.
3. Once you're confident, remove the old hook.sh-based entries from
   `~/.claude/settings.json` by hand (or restore the file from before you first merged
   the old README's snippet, then re-run step 1). `tintaview hooks install` will not do
   this for you.
4. Stop the old `razer_light_server.py`/`.exe` — it and TintaView can't both bind
   `127.0.0.1:8777` at once anyway, so whichever started second will simply refuse to
   bind and exit quietly.

## `tray_config.json` is gone — it's `config.toml` now

The old tray read `tray_config.json` (next to the script) for `claude_home` and
`state_url`, with env vars taking priority over it. TintaView has a single config file
for everything — the daemon, the tray, the wizard and `doctor` all read the same
`~/.tintaview/config.toml` (`%LOCALAPPDATA%\TintaView\config.toml` on Windows). There is
no per-tool config file split anymore, and no separate "tray config" to keep in sync
with the server. Run `tintaview setup` once and it writes the whole thing, WSL UNC paths
included.

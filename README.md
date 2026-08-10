# TintaView

![CI](https://github.com/GomelHawk/TintaView/actions/workflows/ci.yml/badge.svg)

![TintaView](tintaview/assets/generated/logo_full.png)

Your keyboard, mouse and headset lighting — plus a tray icon — mirror what your coding
agent is doing, in the TintaView mark's own colours: **blue** when no agent is running,
**green** when one is open but idle, **yellow** while it's working, and **red, blinking**
when it needs you to act. Click the tray icon for a usage panel (5-hour /
weekly limits, credits, or token totals, depending on the agent). Works with
**Claude Code**, **Codex CLI** and **Cursor**; drives **Razer Chroma** or **OpenRGB**
devices; runs on **Windows, WSL, Linux and macOS**.

TintaView is the successor to
[`claude_code_razer_lights`](https://github.com/GomelHawk/claude-code-razer-lights) —
one process instead of two, three agents instead of one, and a real installer.

## What works where

**Agents** — every agent reports session start/end and "working" the same way; only the
"needs your approval" signal differs:

| Agent | "Needs your approval" | Notes |
| --- | --- | --- |
| **Claude Code** | Real event (`Notification` / `permission_prompt`) | Works out of the box on every released build. |
| **Codex CLI** | Real event (`PermissionRequest`) | Hooks are version-gated — see [Troubleshooting](docs/TROUBLESHOOTING.md#codex-hooks-not-firing). Windows-native Codex (not under WSL) falls back to the `notify` program, which only reports idle. |
| **Cursor** | **Heuristic, not a real event.** Cursor has no "waiting for approval" hook, so TintaView guesses: if a tool starts and nothing else happens for `stall_seconds` (default 8s), it's treated as a stall and turns the light red. This can occasionally be wrong in either direction — see [Troubleshooting](docs/TROUBLESHOOTING.md#cursor-never-goes-red). |

**Lighting engines** — Chroma is the default when it's reachable:

| Engine | Windows | Windows + WSL | Linux | macOS |
| --- | --- | --- | --- | --- |
| **Chroma** (Razer) | Default | Default (daemon runs on the Windows side) | Not available (Windows-only SDK) | Not available (Synapse was discontinued on macOS) |
| **OpenRGB** (Razer, Logitech, Corsair, ASUS, …) | Supported | Supported | Supported — best device coverage | Very limited device support |
| **Status-only** | Always available | Always available | Always available | Always available |

In practice: **macOS gets status and usage stats only, no physical lighting.**
Chroma is Windows-only, and OpenRGB's macOS device support is too thin to rely on.

## Install

### Windows

1. Download `TintaView-Setup-x.y.z.exe` from the
   [Releases page](https://github.com/GomelHawk/TintaView/releases).
2. Run it. **TintaView is not code-signed**, so Windows SmartScreen will say
   "Windows protected your PC". Click **More info**, then **Run anyway**. This is
   expected — signing costs money we haven't spent yet, not a sign anything's wrong.
3. The installer needs no admin rights (it installs per-user, under
   `%LOCALAPPDATA%\TintaView`) and finishes by launching the setup wizard.

If you're on Windows with WSL, the installer detects it and offers a **WSL split**
install: the tray and lighting stay on Windows, and the wizard installs the hooks
inside the Linux distro where your agents actually run, over `wsl.exe`. You never need
to open a WSL terminal yourself.

### Linux / macOS

```sh
curl -fsSL https://raw.githubusercontent.com/GomelHawk/TintaView/main/packaging/install.sh | sh
```

Or, from a local checkout:

```sh
sh packaging/install.sh [--prefix DIR] [--no-autostart] [--headless] [--uninstall]
```

- `--prefix DIR` — install location (default `~/.local/share/tintaview`).
- `--no-autostart` — skip wiring up a login autostart entry.
- `--headless` — skip the PySide6 tray dependency and register a headless (no-tray)
  autostart entry instead, for servers or WSL-only boxes with no desktop session.
- `--uninstall` — remove the autostart entry and the install prefix. Your config and
  installed hooks under `~/.tintaview` are left alone on purpose.

This script is also how you **update**: re-running it (with the same `--prefix`)
reinstalls into the existing virtual environment in place — nothing is duplicated, and
your config and hooks are never touched.

### After installing, either way

```sh
tintaview setup
```

This is the wizard that actually configures anything — the installer only puts files on
disk. Nothing lights up and no hooks are installed until you've run it.

## What the wizard asks

`tintaview setup` runs the same seven-step flow whether it's launched by `Setup.exe`,
by `install.sh`, or by hand:

1. **Platform** — auto-detected (Windows / WSL / Linux / macOS), with a prompt to
   override it if detection guessed wrong.
2. **Agents** — probes `~/.claude`, `~/.codex`, `~/.cursor` and `PATH`, pre-ticks
   whatever it finds, and asks which you want TintaView to watch (at least one).
3. **Lighting engine** — probes Chroma and OpenRGB and shows each as detected / not
   running / unsupported here; warns that OpenRGB and Razer Synapse / Logitech G HUB
   fight over the same hardware, and that Linux OpenRGB usually needs udev rules and
   the `i2c-dev` kernel module.
4. **Install location** — where the program files go (separate from your settings,
   which always live under `~/.tintaview` or `%LOCALAPPDATA%\TintaView`).
5. **Autostart** — a Startup-folder shortcut (Windows), a systemd `--user` unit plus an
   XDG autostart entry (Linux), or a launchd agent (macOS). No admin, no Scheduled Task.
6. **Hooks** — for each agent, shows the exact before/after diff of the config file
   it's about to write and asks for confirmation before touching anything.
7. **Verify** — saves the config, installs the hook script, then optionally waits
   (about a minute) for a real event from your agent so you know it actually worked.

Answer every question with its default and skip the prompts entirely with
`tintaview setup -y`.

## Usage stats

Click the tray icon to open the usage panel. What you see depends on the agent:

- **Claude Code** — the same official 5-hour / weekly / usage-credit percentages Claude
  Code's own `/usage` command shows, read from `https://api.anthropic.com/api/oauth/usage`
  with your existing OAuth token. If that endpoint is unreachable, TintaView falls back
  to an estimate reconstructed from your local transcripts, clearly labelled "estimate".
- **Codex CLI** — official rate-limit percentages when your local session logs carry
  them (ChatGPT-plan sessions); plain token totals over the last 5 hours / 7 days
  otherwise (API-key sessions don't get percentages from Codex at all). Entirely local
  — no network call.
- **Cursor** — **unofficial**. There is no published personal-usage API, so TintaView
  reads the access token Cursor already keeps in its local `state.vscdb` and calls the
  same internal endpoint the Cursor app itself uses. This can break on any Cursor
  release with no warning; when it does, the panel just says usage is unavailable —
  lighting is unaffected either way.

Usage is polled every 5 minutes (rate limits, not urgency) and the last good result is
cached, so the panel is never blank and a rate-limit response never overwrites good data
with a worse estimate.

## Configuration

One file: `~/.tintaview/config.toml` (Windows: `%LOCALAPPDATA%\TintaView\config.toml`),
written by `tintaview setup` and safe to hand-edit afterwards.

| Key | Default | Meaning |
| --- | --- | --- |
| `server.host` | `127.0.0.1` | Where the status broker listens. |
| `server.port` | `8777` | Port for the status broker (hooks and the tray both talk to this). |
| `server.watchdog_timeout` | `600` | Seconds of hook silence before the lights are force-released (crash safety). |
| `engine.mode` | `auto` | `auto` \| `chroma` \| `openrgb` \| `none`. `auto` probes `engine.order` and uses the first that responds. |
| `engine.order` | `["chroma", "openrgb"]` | Probe order for `auto` mode. |
| `engine.chroma.devices` | `["mouse", "headset"]` | Chroma device endpoints to drive. |
| `engine.openrgb.host` / `.port` | `127.0.0.1` / `6742` | Where the OpenRGB SDK server is listening. |
| `engine.openrgb.device_types` | `["mouse", "keyboard", "headset"]` | Which OpenRGB devices to drive. Peripherals only by default — motherboard, RAM, GPU and case lighting is ambient decoration, and driving it makes the whole room flash on every tool call. Set to `[]` for every detected device, or add any `openrgb.utils.DeviceType` name (e.g. `"mousemat"`). |
| `engine.openrgb.restore_on_release` | `true` | Snapshot each device's mode/colors on open, restore them on close. |
| `engine.openrgb.direct_mode_only` | `true` | Only drive devices that expose a Direct mode, to avoid flash wear from blinking. |
| `colors.idle` | `#56D155` | Green — a session is open and the agent is waiting on you. |
| `colors.working` | `#F0B30C` | Yellow — the agent is busy. |
| `colors.confirm` | `#F42D3C` | Red, blinking — the agent needs you to act. |
| `colors.none` | `#0080F7` | Blue — no agent session at all. |
| `colors.blink_ms` | `400` | Blink interval for the `confirm` state, in milliseconds. |
| `stats.poll_seconds` | `300` | How often usage providers are polled. |
| `stats.enabled` | `true` | Turn the usage panel off entirely. |
| `ui.chime_on_confirm` | `false` | Play a sound when a session first needs your approval. |
| `update.check` | `true` | Whether the tray checks GitHub Releases for a newer version. |
| `update.channel` | `stable` | Update channel (currently only `stable` is published). |
| `agents.enabled` | `["claude"]` | Which agents TintaView watches; the wizard sets this for you. |
| `agents.<key>.home` | *(adapter default)* | Agent data directory — empty means `~/.claude` / `~/.codex` / `~/.cursor`; a UNC path in a WSL-split install. |
| `agents.<key>.confirm_detection` | `event` | `event` (a real hook fires) or `stall` (heuristic — Cursor's default). |
| `agents.<key>.stall_seconds` | `8.0` | Only used when `confirm_detection = "stall"`. |
| `agents.cursor.state_db` | *(auto-detected)* | Path to Cursor's `state.vscdb`; empty auto-detects the platform default. |

## Commands

| Command | Description |
| --- | --- |
| `tintaview` | Run the tray UI with the status broker in-process (the normal case). |
| `tintaview run [--headless]` | Same as above; `--headless` runs the broker only, with no GUI. |
| `tintaview setup [--platform P] [-y]` | Run the install/reconfigure wizard. `--platform` overrides platform detection; `-y` accepts every default. |
| `tintaview doctor [-v]` | Diagnose an install — see [Troubleshooting](docs/TROUBLESHOOTING.md). `-v` also offers a live 30-second hook test. |
| `tintaview hooks {install,status,uninstall} [--agent A] [--scope user\|project] [--hook-bin PATH] [--all-agents] [-y]` | Manage one agent's (or all agents') hook configuration, with a diff-and-confirm flow. |
| `tintaview update [--check-only]` | Check for, and install, a newer version. |
| `tintaview --version` | Print the installed version. |

## How it works

```
Claude Code / Codex CLI / Cursor
        │  hook fires on session start/end, prompt submit, tool use, permission prompt
        ▼
tv-hook.sh / tv-hook.cmd         (~5 ms: sh/cmd + curl, no Python, always exits 0)
        │  GET /v1/event/<event>?agent=<agent>&sid=<session>
        ▼
TintaView status broker (127.0.0.1:8777, in-process with the tray)
        │  tracks state per (agent, session); confirm > working > idle > none
        ▼
Lighting engine (Chroma / OpenRGB / none)
        │
        ▼
Mouse / keyboard / headset LEDs

The tray reads the broker's state directly in-process (no HTTP hop) to paint its icon
and tooltip, and polls the same GET /state endpoint a doctor or a remote tool would use.
```

## Updating

- **Windows** — the tray's **Check for updates** menu item checks GitHub Releases (the
  result goes to the log, not a popup, today); to actually install a newer version, run
  `tintaview update` from a terminal, or download and run the new `Setup.exe` yourself.
- **Linux / macOS** — re-run `packaging/install.sh` (or `tintaview update`, which does
  the same thing after verifying the script's SHA-256 against the release's checksums).

Updating never touches `config.toml` or any agent's hook configuration — hooks always
point at the same stable `tv-hook` path, so an update can never leave an agent's hooks
broken.

## Uninstall

- **Windows** — use "Uninstall TintaView" from the Start menu / Add or Remove Programs.
  It removes the program files and the Startup shortcut; your config, usage cache and
  logs under `%LOCALAPPDATA%\TintaView` are left in place.
- **Linux / macOS** — `sh packaging/install.sh --uninstall`. Same behaviour: the
  autostart entry and install prefix are removed, `~/.tintaview` is left alone.
- **Either way**, to also remove the hook entries TintaView installed into your agents'
  own config files, run this first (needs a working install, so do it *before*
  uninstalling, or reinstall to run it):

  ```sh
  tintaview hooks uninstall --agent all
  ```

  Leaving them in place is harmless — an agent calling a hook with nothing listening on
  the other end fails silently and always exits 0 — but they'll keep showing up in
  `tintaview hooks status` output on any other machine that shares the config.

## Credits & licence

MIT — see [LICENSE](LICENSE).

TintaView is not affiliated with or endorsed by Anthropic, OpenAI, Cursor (Anysphere),
or Razer Inc. "Razer", "Chroma" and "Synapse" are trademarks of Razer Inc. "Claude" and
"Claude Code" are trademarks of Anthropic. "Codex" is a trademark of OpenAI. "Cursor" is
a trademark of Anysphere.

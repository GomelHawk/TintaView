# Troubleshooting

The default first move for almost everything below is:

```sh
tintaview doctor -v
```

It checks environment, config, the daemon, the lighting engine, the hook script, each
agent's installed hooks and each agent's usage stats, in that order — an early failure
(e.g. a broken config) is usually the real cause of everything printed after it. `-v`
additionally offers to wait up to 30 seconds for a real hook event from a live agent
session. Every `WARN`/`FAIL` line names the exact next command or file to fix, not just
"something is wrong" — if you ever see one that doesn't, that's a bug in `doctor` itself.

## Lights never change

1. `tintaview doctor` — check the `ENGINE` line. If it says `auto mode found no usable
   lighting engine — running status-only`, TintaView is up and tracking state, but
   nothing is configured to actually drive hardware.
   - **Chroma**: Razer Synapse must be *running* — its Chroma Connect SDK is what's
     probed. If Synapse is closed, SDK calls silently succeed but nothing lights up.
     Also confirm the device is set to app/Chroma-controlled lighting in Chroma Studio;
     a fixed onboard effect overrides the SDK.
   - **OpenRGB**: open OpenRGB, enable **Settings → SDK Server → Server**, and leave
     OpenRGB running. `doctor` reports the exact host:port it tried
     (`engine.openrgb.host`/`.port`, default `127.0.0.1:6742`).
2. If `doctor` says the engine is available but lights still don't move, check the
   `DAEMON` line for `effective=` — if it's stuck on `none`, no hook event has reached
   the daemon at all; skip to [Hooks not firing](#hooks-not-firing).
3. Only some devices respond? Confirm each one is listed in `engine.chroma.devices`
   (Chroma) or not filtered out by `engine.openrgb.device_types` /
   `engine.openrgb.direct_mode_only` (OpenRGB — a device with no Direct mode is skipped
   on purpose, to avoid wearing out its flash with the confirm blink).
4. Motherboard, RAM, GPU or case lighting not responding? **That's deliberate.**
   `engine.openrgb.device_types` defaults to `["mouse", "keyboard", "headset"]`, because
   those are the ones in your eyeline while you work. Set it to `[]` to drive every
   device OpenRGB detects, or add specific types by name.

## Lights stuck on after the agent exits

TintaView releases the lights when the agent's `SessionEnd` hook fires. If the agent
crashed, was killed, or its config lost the hook entry, that never happens — but a
**watchdog** thread force-releases the lights after `server.watchdog_timeout` seconds
(default 600 = 10 minutes) of complete hook silence. So:

- If you're willing to wait, it clears itself — no session-end hook is required.
- To recover immediately, run `tintaview doctor` (it doesn't force a release itself, but
  confirms the daemon is alive) and then send a manual `session-end` for the stuck
  session:

  ```sh
  curl "http://127.0.0.1:8777/v1/event/session-end?agent=claude&sid=<sid>"
  ```

  (`sid` isn't shown anywhere convenient today — if you don't know it, it's faster to
  just wait for the watchdog or restart TintaView, which clears all in-memory state.)
- If this happens often, lower `server.watchdog_timeout` in `config.toml` — it trades a
  faster recovery for a slightly higher chance of releasing a session that's just slow,
  not dead.

## Hooks not firing

`tintaview doctor` → the `AGENT HOOKS` section reports one of four states per agent:

| State | Meaning | Fix |
| --- | --- | --- |
| `missing` | No TintaView hook entries in the agent's config at all | `tintaview hooks install --agent <key>` |
| `partial` | Some events wired, others missing | `tintaview hooks install --agent <key>` (idempotent — it fills in the gaps) |
| `stale-path` | Entries exist but point at a `tv-hook` binary that no longer exists — usually a moved or reinstalled TintaView | `tintaview hooks install --agent <key>` to repoint them |
| `installed` | Up to date | — |

`stale-path` is the sneaky one: everything *looks* installed, the agent's config has
TintaView-looking entries, but they call a path that's gone — this fails completely
silently at hook time (the hook shim always exits 0, even on a failed `curl`), so the
only symptom is "the lights just stopped working" days or weeks after a reinstall moved
things. `doctor` catches it explicitly for this reason.

Other things worth checking by hand:

- **The agent rewrote its own config.** Some agents regenerate their settings file on
  upgrade, silently dropping entries they don't recognize. `tintaview hooks status`
  (or `doctor`) catches this the same way as any other drift — just re-run
  `tintaview hooks install`.
- **You hand-edited the file and it's no longer valid JSON.** `hooks install` refuses to
  guess at a broken file rather than risk destroying it — fix the JSON syntax first.
- Every `hooks install`/`uninstall` only ever touches entries whose command contains the
  `tv-hook` sentinel — anything else you've hand-written in the same file is left
  completely alone, including hook entries you added by hand for some other tool.

## Codex hooks not firing

Codex's lifecycle hooks are newer and version-gated, and the flag name changed between
releases:

- Codex older than **0.114** has no lifecycle hooks at all. `doctor`'s `AGENT HOOKS`
  section reports this as `Codex feature flag: ... predates lifecycle hooks` — the only
  fix is upgrading Codex, or living with the `notify`-based idle-only fallback (see
  below).
- Codex **0.114–0.129** needs `[features] codex_hooks = true` in `~/.codex/config.toml`.
  **Only `tintaview setup` writes this flag** — it's a step in the wizard's hooks page,
  showing the diff first and asking for confirmation, same as any other hook change.
  `tintaview hooks install --agent codex` does **not** touch `config.toml` at all; it
  only writes `~/.codex/hooks.json`. If `doctor` reports the flag isn't set, it prints
  the exact diff it would need but doesn't apply it — re-run `tintaview setup` (or apply
  the printed diff to `~/.codex/config.toml` by hand) to fix it.
- Codex **0.130+** has hooks on by default; no flag is needed (this is a `noop` in
  `doctor`'s output, not a failure).
- Version detection runs `codex --version` and is best-effort: if it can't parse a
  version at all, TintaView writes the legacy `codex_hooks` flag on the theory that a
  newer Codex simply ignores an unknown feature key, which costs a stray config line at
  worst — the alternative (writing nothing) risks hooks silently never firing.

**Windows-native Codex** (Codex running directly on Windows, not inside WSL) did not
support hooks at all in early builds. There, TintaView falls back to Codex's `notify`
program, which only fires on `agent-turn-complete` — meaning you'll see the light go
**idle**, but never accurate `working` or `confirm` transitions. This is a real,
documented limitation, not a bug to chase — if you need full fidelity, run Codex inside
WSL instead.

## Cursor never goes red

This is expected in one specific way: **Cursor has no native "waiting for your
approval" hook event at all.** Unlike Claude Code (`Notification`/`permission_prompt`)
and Codex (`PermissionRequest`), Cursor's hook payloads never tell you it's sitting on a
permission prompt.

TintaView infers it instead: `tool-start` arms a timer for that session; if
`stall_seconds` (default 8, `agents.cursor.stall_seconds` in `config.toml`) pass with no
`tool-end` and no other event for it, the session is promoted to `confirm`. This is
deliberately conservative — a long-running test suite or build is also "tool-start, then
silence for a while", and it must not turn the lights red for that.

If it's firing too late or too rarely for your workflow, lower `stall_seconds`. If it's
firing on long-running builds that aren't actually waiting on you, raise it. There is no
setting that makes this perfectly accurate — it's a heuristic over an agent that gives no
real signal, so some false positives/negatives are inherent, not a configuration bug.

## OpenRGB fights Synapse / G HUB

OpenRGB and Razer Synapse (or Logitech G HUB) both try to drive the same hardware
directly. Running two SDK-level controllers against one device at the same time makes
them fight over it — flickering, one program's colour winning intermittently, or a
device getting stuck. The wizard warns about this explicitly when you pick OpenRGB as
the engine. **Only run one at a time**: close Synapse/G HUB if you want OpenRGB to drive
those devices, or set `engine.mode = "chroma"` (or run Synapse) if you'd rather let
Synapse own them.

## OpenRGB on Linux needs udev rules / i2c-dev

OpenRGB usually can't see USB/I2C RGB hardware on Linux out of the box — you need its
udev rules installed and the `i2c-dev` kernel module loaded. This is entirely between
you and OpenRGB, not something TintaView installs on your behalf (the wizard just warns
about it when you choose OpenRGB on Linux). See
[openrgb.org](https://openrgb.org/) for your distro's install steps.

## GNOME needs an AppIndicator extension for the tray

GNOME dropped legacy `XEmbed` tray icons; Qt's tray (what TintaView uses) needs an
AppIndicator-compatible extension to show up at all. Ubuntu usually ships one already —
just make sure it's enabled:

```sh
# Ubuntu (often preinstalled):
gnome-extensions enable ubuntu-appindicators@ubuntu.com
# Upstream GNOME:
sudo apt install gnome-shell-extension-appindicator && \
  gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com
```

KDE, XFCE, Cinnamon, etc. show tray icons natively — no extension needed. If you'd
rather not deal with this at all, run headless (`tintaview run --headless`, or install
with `install.sh --headless`) — status and lighting still work, you just lose the tray
icon and usage panel.

## Usage shows "not signed in" (per agent)

- **Claude Code**: means the OAuth token in `~/.claude/.credentials.json` is invalid —
  run `claude` (or restart Claude Code) to sign in again.
- **Codex CLI**: Codex has no "signed in" concept for usage — this message instead means
  no recent session files were found under `~/.codex/sessions/`. Run a Codex session
  first.
- **Cursor**: means TintaView couldn't find (or read) an access token in Cursor's local
  `state.vscdb` — sign in to Cursor normally and it should resolve on the next poll.
  This path is **unofficial** (see the README) — if signing in doesn't fix it, the
  underlying Cursor endpoint or token storage may have changed since this was written;
  lighting is unaffected either way.

## The WSL split (daemon on Windows, hooks in the distro)

If your coding agents run inside a WSL distro but you want physical lighting, the
lighting engine (Chroma in particular) only exists on the Windows side — so the daemon
and tray run on Windows while the hooks run inside the distro. Symptoms and fixes:

- **Nothing happens at all**: confirm you ran the *Windows* installer/wizard (not one
  inside the distro) and picked "WSL split" — or, if you're inside the distro, that
  you've also run the Windows-side installer once, since a WSL-only wizard run installs
  hooks with no lighting behind them yet.
- **How it reaches the daemon with no firewall rule**: the hook script inside WSL uses
  `curl.exe` (the *Windows* curl, not WSL's own), because a process launched that way
  runs in the Windows network namespace — so `127.0.0.1:8777` reaches the Windows-side
  daemon directly, with no mirrored networking and no firewall exception needed. This is
  written into `hook.env` (`TINTAVIEW_CURL=curl.exe`) by the installer automatically;
  don't "fix" it back to plain `curl` by hand.
- **Agent home paths**: the Windows-side config stores each agent's home directory as a
  UNC path (e.g. `\\wsl.localhost\Ubuntu\home\<user>\.claude`) so the Windows tray can
  read transcripts/credentials living inside the distro. If usage stats or hook status
  look wrong after renaming a distro or moving your home directory, re-run
  `tintaview setup` on Windows to refresh these paths.
- **Live verification isn't automatic across the boundary** — the wizard says so
  explicitly on a WSL-split install. After setup, start a session in your agent inside
  the distro and watch the tray icon on Windows, or run `tintaview doctor -v` there.

## Port 8777 already in use

`tintaview doctor` reports this as a `DAEMON` failure: `{base}/healthz answered, but not
the way TintaView does`. That means something else — not TintaView — is bound to
`server.port` (default `8777`). Two ways to resolve it:

1. Free the port (stop whatever else is using it), or
2. Change `server.port` in `config.toml` to something free, then re-run `tintaview
   setup` and restart TintaView. Hooks call the daemon URL from `hook.env`
   (`TINTAVIEW_URL`), not from anything baked into the per-hook command itself, and
   `tintaview setup` is what rewrites `hook.env` — so a port change needs a re-run of
   setup, not `tintaview hooks install` (which only touches the agents' own config
   files, not `hook.env`).

If instead `doctor` says nothing is answering at all (`TintaView is not running`), that's
a different problem: start it with `tintaview run` (tray) or `tintaview run --headless`
(broker only).

# AGENTS.md — working on TintaView

Technical notes for anyone (human or agent) changing this repo. [README.md](README.md) is the
user-facing documentation and [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) the user-facing
diagnostics; neither is a substitute for the constraints below. Everything here is a decision
that has already been made and paid for — some of it measured on real hardware — so treat it as
a constraint, not a suggestion, and update this file if a decision genuinely changes.

## Dev commands

```sh
python -m pip install -e ".[dev,ui]"
ruff check .                          # line-length 100, E/F/W/I/UP/B/C4/SIM
pytest -q                             # DeprecationWarning is an error
python scripts/build_assets.py --check # generated assets must be committed and current
QT_QPA_PLATFORM=offscreen pytest -q   # required for the Qt tests without a display server
```

CI (`ci.yml`) runs all four on ubuntu / windows / macOS × Python 3.12, 3.13 and 3.14. The UI tests
construct real `QWidget`s and never `.show()` them, so `offscreen` is enough — never add a
test that needs a real display.

At the end of implementing any update or feature, give a short commit-style description of the
change (what changed, in one or two sentences) — even if the change isn't actually committed.

## Layout

```
tintaview/
  core/      config.py  state.py  server.py  events.py  stalldetect.py  controller.py  log.py
  engines/   base.py  chroma.py  openrgb.py  null.py  factory.py
  agents/    base.py  claude.py  codex.py  cursor.py     # hook manifest + paths per agent
  stats/     providers/{claude,codex,cursor,jetbrains,copilot}.py  cache.py  model.py  service.py
  ui/        tray.py  flyout.py  wizard.py  icons.py
  install/   detect.py  hooks.py  hookscript.py  codex_flag.py  autostart.py  wsl.py
             components.py  doctor.py  update.py  restart.py
  hooks/     tv-hook.sh  tv-hook.cmd                     # shipped as package data
  assets/generated/                                      # built by scripts/build_assets.py
packaging/   install.ps1  install.sh
tests/       one module per subsystem, fixtures under tests/fixtures/
```

The core (broker + hooks + engines) is **stdlib-only apart from `tomlkit`** — it has to run on
a bare WSL distro with no compiler toolchain, and it is what a `--headless` install gets.
PySide6 and `openrgb-python` are extras (`[ui]`, `[openrgb]`) and every import of them must be
optional at runtime.

## Locked decisions

| Decision | Choice and why |
| --- | --- |
| Process model | **One process**: tray UI + status broker in-process (`tintaview run [--headless]`). A background service plus a separate tray process means two autostart entries, two logs and two update paths — the biggest source of install pain. |
| Packaging | **A pure-Python wheel installed into a private venv, on every platform.** No compiled bundle, no `.exe` installer. This is a hard requirement of Windows Smart App Control, not a preference — see [Packaging](#packaging-no-compiled-bundle-ever). |
| Autostart | **One per-user entry per platform, never a Scheduled Task**: HKCU `…\CurrentVersion\Run` value on Windows (*not* a Startup-folder shortcut — Windows 11 blocks those), a systemd `--user` unit plus an XDG autostart entry on Linux, a launchd agent on macOS. No admin rights, ever. |
| Hook install | **Auto-merge with a per-agent before/after diff and explicit confirmation.** A copy-paste snippet stays available as a fallback for locked-down environments. |
| Engines | **Both Chroma and OpenRGB, Chroma default when reachable.** Auto-detect order `chroma → openrgb → status-only`. |
| Tray icon | **A single whole-icon state.** No per-agent ray splitting or zone splitting: a user watches one agent at a time, and a gradient icon among solid ones reads as a different icon rather than a fourth state. |
| Cursor stats | Local session token from `state.vscdb` → Cursor's own Connect RPC. No login UI of our own. |
| Stack | Python 3.12+, PySide6 (Qt) tray, stdlib HTTP server. |

Non-goals: lighting effects beyond solid/blink, per-agent colours or device zoning, multi-machine
or remote status, team dashboards, code signing / notarization, and distribution via PyPI,
winget, the Windows Store or Homebrew.

## Core contracts

These are **on-disk / on-the-wire contracts**. They appear in URLs baked into every already
installed agent hook config, so they can be extended but never renamed or repurposed.

**Event vocabulary** (`core/events.py`): `session-start, session-end, working, idle, confirm,
tool-start, tool-end`.

**HTTP API** on `127.0.0.1:8777`:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/event/{event}?agent=&sid=&tool=` | Hook ingress |
| `GET` | `/{session-start,session-end,working,idle,confirm}` | Agent-less aliases defaulting to `agent=claude`, so a hand-written one-line `curl` hook keeps working |
| `GET` | `/state` | Read-only status for the tray and `doctor` |
| `GET` | `/healthz` | Liveness for `doctor` |

Two invariants in the ingress path:

- **Acknowledge before any lighting I/O.** A slow Chroma or OpenRGB call must never be able to
  stall an agent.
- **`/state` must never touch `last_ping`.** Polling it would otherwise keep the watchdog alive
  forever and defeat the crash-safety release.

**State model.** Sessions are keyed by `(agent, sid)`, not `sid` alone. Colour priority is global
and unchanged: `confirm > working > idle > none`. `/state` also reports a per-agent breakdown so
the tooltip can say `Claude: working · Codex: idle`.

**Config.** One file for daemon, tray, wizard and `doctor`: `~/.tintaview/config.toml`, or
`%LOCALAPPDATA%\TintaView\config.toml` on Windows. Every key is documented in the README's
Configuration table — keep that table in sync when adding one.

## Lighting engines

`LightingEngine` (`engines/base.py`): `probe() → bool`, `open() → bool`, `set_color(r, g, b)`,
`close()`, `heartbeat()`, `active` property. `BaseEngine` carries the shared failure cooldown.

- **Chroma** is the default. POST to open, PUT `/heartbeat` every 4 s, PUT per device with **BGR**
  packing, DELETE to close. Release is automatic — Synapse takes back over on DELETE.
- **OpenRGB** is **RGB, not BGR**, and has no session concept, so it must do by hand three things
  Chroma gets for free:
  1. **Snapshot & restore** — record each target device's active mode and per-LED colours on
     `open()`, restore them on `close()`. Without this the rig stays green after the agent exits.
  2. **Direct mode only** — skip devices with no Direct mode. Blinking a non-Direct device writes
     to flash.
  3. **Peripherals only by default** (mouse, keyboard, headset). Motherboard/RAM/GPU/case lighting
     is ambient decoration; driving it makes the whole room flash amber on every tool call.
     `engine.openrgb.device_types = []` opts back in to everything.
- **Null** is status-only, drives nothing, and is always available. `auto` mode probes
  `engine.order` and falls back to it.

OpenRGB and Synapse / G HUB fight over the same devices; the wizard says so in plain language and
`doctor` detects both running. Pin to SDK v5 behaviour, feature-detect, fail soft to status-only.

## Agents and the hook layer

| | Config TintaView writes | Session start/end | Working | "Waiting for you" |
| --- | --- | --- | --- | --- |
| Claude Code | `~/.claude/settings.json` | `SessionStart` / `SessionEnd` | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | `Notification` + matcher `permission_prompt` |
| Codex CLI | `~/.codex/hooks.json` (never their `config.toml`, except the feature flag) | `SessionStart` / `SessionEnd` | same | `PermissionRequest` (first-class) |
| Cursor | `~/.cursor/hooks.json` (`{"version": 1, …}`) | `sessionStart` / `sessionEnd` | `beforeSubmitPrompt`, `preToolUse`, `postToolUse` | none → stall heuristic |

JetBrains AI Assistant and GitHub Copilot CLI are deliberately absent from this table — see
[Statistics](#statistics) for why each is stats-only with no `agents/` adapter.

Per-agent gotchas that must keep being handled:

- **Cursor sends `conversation_id`, not `session_id`** in hook stdin (`session_id_field` on the
  adapter).
- **Codex hooks are version-gated and the flag name moved.** Early builds (~v0.114+) need
  `[features] codex_hooks = true`; newer docs have hooks on by default with `hooks = false` to
  disable. Detect via `codex --version`, write the right flag, and show that TOML edit in the diff.
- **Codex hooks were unavailable on Windows in early builds.** Windows-native Codex falls back to
  the `notify` program, which only fires `agent-turn-complete` → **idle only**. Degraded but
  honest; the wizard says so.

**The hook binary** lives at `~/.tintaview/bin/tv-hook` — **a stable path that never changes across
updates**, so upgrading TintaView never rewrites any agent's config. It fires on every tool call,
so it must stay ~5 ms and be incapable of stalling an agent: POSIX `sh` + `curl` with the session
id pulled out by `sed`, **no Python startup**, `curl -s -m 1`, output discarded, always `exit 0`.
`TINTAVIEW_URL` / `TINTAVIEW_CURL` come from `~/.tintaview/hook.env`, written at install time.
`tv-hook.cmd` is the Windows-native twin.

**Hook merge** (`install/hooks.py`) rewrites the user's real config files, so its rules are strict:

1. Read JSON, or TOML via `tomlkit` so comments and formatting survive.
2. Merge only TintaView-owned entries, identified **solely** by the `tv-hook` sentinel
   (`HOOK_SENTINEL`) appearing in the command. Everything else in the file is untouched.
3. Show a unified before/after diff and require an explicit OK per agent.
4. Back up to `<file>.tintaview-backup-<timestamp>` (keep the last 5), then write **atomically**
   (temp + rename).
5. Be **idempotent**: re-running produces no diff and no backup.
6. `uninstall` removes exactly the sentinel-marked entries and nothing else.
7. `status` reports `installed / missing / partial / stale-path` per agent.

**Drift detection.** Agents rewrite their config on upgrade, so the tray re-checks hook presence
periodically and surfaces "Hooks missing for Codex — Fix" rather than silently going dark.

### Cursor stall heuristic

Cursor has no `Notification`/`PermissionRequest` equivalent: when it stops to ask for approval, no
hook fires at all. The only observable symptom is that `tool-start` fired and nothing followed.
`tool-start` records `(agent, sid, ts, tool)`; if `stall_seconds` elapse with no `tool-end` and no
other event for that session, treat it as **confirm**.

Armed **only** where `confirm_detection = "stall"`. Claude and Codex have a real confirm event and
must never be armed, or a slow tool call would eventually paint them red for no reason. Default 8 s,
tunable, deliberately conservative — a long `pytest` run must not turn the lights red.

## Statistics

One `UsageProvider` per agent, all normalised to the same row model
(`{label, pct, right, show_pct, severity, kind}`) so the flyout renders every agent identically.
Poll on the shared 5-minute cadence (these are rate limits, not urgency), cache the last good
result to `~/.tintaview/usage_cache.json` so the flyout is never blank, and never let a
rate-limited response replace good data with an estimate.

- **Claude** — `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token from
  `~/.claude/.credentials.json`; falls back to an estimate reconstructed from
  `~/.claude/projects/**/*.jsonl`, labelled as an estimate.
- **Codex** — entirely local: `token_count` records in `~/.codex/sessions/**/rollout-*.jsonl`.
  `info.total_token_usage` is always present; `rate_limits.primary/secondary` only on
  ChatGPT-plan sessions (`null` on API-key sessions). Show official percentages when present,
  token totals otherwise.
- **Cursor** — **unofficial and credential-sensitive.** No personal usage API exists and Cursor
  transcripts carry no token counts, so the provider reads `cursorAuth/accessToken` from the
  `ItemTable` of Cursor's `state.vscdb` and calls
  `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`. Rules:
  - The DB is large (~300 MB) and held open by Cursor → open it `file:…?mode=ro`, handle WAL/lock
    errors, and **never copy it**.
  - Read the token fresh on each poll; never log, cache or persist it; mask it in `doctor` output;
    pin requests to `api2.cursor.sh`.
  - On 401/403 re-read from disk (Cursor refreshes it while signed in).
  - Degrade to "not signed in" / "usage unavailable" — this can break on any Cursor release and
    must never block the tray or affect lighting.
- **JetBrains AI Assistant** — stats-only; no adapter in `agents/` at all, since the IDE plugin
  has no scriptable event API to hook. Reads `quotaInfo`/`nextRefill` JSON embedded in
  `<IDE data dir>/options/AIAssistantQuotaManager2.xml`, the same cache the IDE's own status-bar
  widget reads. Quota is account-wide, but each installed IDE only syncs its own copy on its own
  schedule, so every `<Product><Version>` directory under the JetBrains config root is scanned and
  the most recently modified file wins; `agents.jetbrains.quota_path` overrides this. The tariff
  quota's own percentage (the recurring monthly allowance), not the combined total, drives
  severity — the top-up balance is typically much larger and would make a combined percentage
  look artificially healthy. The raw-units-to-"credits" display scale (`CREDIT_SCALE` in the
  provider) is reverse-engineered from a live widget reading, not documented by JetBrains — see
  the provider's module docstring before changing it.
- **GitHub Copilot CLI** — stats-only; no adapter in `agents/` either, even though Copilot CLI
  *does* have a real hook system (a `preToolUse`/`postToolUse`/`sessionStart`/`sessionEnd`/
  `permissionRequest`/`notification`/... vocabulary, confirmed against the CLI's own bundled
  `api.schema.json`). It is dispatched over an internal "SDK callback transport" for programs
  embedding `@github/copilot-sdk`, not a documented external shell-command hook — unverified
  whether a plain `tv-hook`-style script can register for it, so it was not attempted. Deliberately
  no live quota percentage either: GitHub's real "X% used, resets in Nd" figure comes from an
  internal `copilot_internal/user` endpoint that needs a token out of the OS credential store
  (Windows Credential Manager here, target `<uuid>.github-copilot-app`) via a two-step OAuth
  exchange (`copilot_internal/v2/token` first) — reverse-engineerable in principle, but not
  attempted without a captured real response to verify field names against, the same bar Cursor's
  and JetBrains's providers were held to. Instead reads `<home>/session-store.db`'s
  `assistant_usage_events` table (confirmed against a live database) and sums input/output tokens
  per model over the last 7 days — informational totals only (`show_pct=False`), same shape as
  Codex's API-key fallback.

In a **WSL split** install the Claude/Codex JSONL files sit behind a UNC path: scan only files
modified in the last 7 days and cache by mtime, or the poll is slow.

## Packaging: no compiled bundle, ever

One artifact for every platform — a pure-Python wheel installed into a private venv under the
install prefix. Windows runs two *independent* defences against unsigned software, and they need
different answers:

1. **Mark-of-the-Web.** Browsers tag downloads with a `Zone.Identifier` stream; that tag is what
   makes Edge/Chrome block an unsigned installer on reputation and SmartScreen show "Windows
   protected your PC". Nothing PowerShell downloads goes through the Attachment Execution Service,
   so no tag is written and neither check fires. The trap: telling users to download a `.zip` and
   extract it by hand does **not** help — Explorer's extractor propagates the zone into every file.
2. **Smart App Control.** On by default on clean Windows 11 installs, and MOTW is irrelevant to it:
   it refuses to run any executable that is neither signed nor cloud-reputable, however it arrived.
   A PyInstaller bundle loses permanently — every build is byte-unique, so it has no reputation,
   and every release rebuilds it, so it can never accumulate any. **Measured on an enforcing
   machine:** a PyInstaller `TintaView.exe` was blocked (CodeIntegrity event 3118), while
   `python.exe`/`pythonw.exe` (PSF-signed, signature preserved through venv creation), the pip
   console shim and all of PySide6's DLLs ran without complaint — as did the tray itself, serving
   `/state`.

Installing a wheel into a venv satisfies both with no certificate: the only executables involved
are the signed interpreter and widely-mirrored PyPI wheels. This is also why the app is launched as
`python -m tintaview` rather than through a `console_scripts` shim (a small unsigned `.exe` of its
own). **Do not reintroduce a frozen bundle or an `.exe` installer without a code-signing
certificate to go with it** — `tests/test_packaging.py` guards this.

Two Windows-specific traps `install.ps1` encodes:

- On Windows `config_dir()` **is** the install prefix (`%LOCALAPPDATA%\TintaView`) — `config.toml`,
  `hook.env`, `bin\tv-hook.cmd` and `logs\` are siblings of the venv. So the prefix is never
  deleted recursively; only `<prefix>\venv`, which the installer owns outright, ever is.
- `pip install --upgrade` compares version numbers and does nothing when they match, so the wheel
  is additionally force-reinstalled with `--no-deps`. Without that, re-running the installer can't
  repair a damaged install, and a re-tagged release silently keeps the old code while reporting
  success.

`install.ps1` verifies the wheel's SHA-256 against the release's `SHA256SUMS.txt` before installing
and is idempotent — it *is* the update mechanism.

### WSL split install

Daemon and tray on **Windows**, hooks **inside the distro**, and the user never opens a WSL
terminal. The Windows-side wizard enumerates distros via `wsl.exe -l -q`, installs `tv-hook` +
`hook.env` inside the chosen one, patches the in-WSL agent configs over
`wsl.exe -d <distro> -- …`, and writes UNC paths (`\\wsl.localhost\<distro>\home\<user>\.claude`)
into the Windows-side config. It relies on the **`curl.exe` trick**: called from WSL, `curl.exe`
runs in the *Windows* network namespace, so `127.0.0.1:8777` reaches the daemon with no firewall
rule and no mirrored-networking requirement.

### Updating

Config and every agent's hook configuration are **never touched by an update** — hooks point at the
stable `tv-hook` path, not at anything version-specific. So `tintaview update` only ever downloads
the release's own install script, **verifies its SHA-256** against the release's checksums asset,
and re-runs it: `install.ps1 -Silent` on Windows (detached, since it stops the interpreter running
out of the venv it is about to replace), `sh install.sh` on Linux/macOS. `-Prefix` is derived from
`sys.prefix` so a non-default install upgrades itself instead of spawning a second copy.

### CI and release

`build.yml` runs on tag `v*`: `python -m build` on ubuntu-latest (the wheel is pure Python, so no
Windows runner and no matrix), a smoke test that the wheel installs and `python -m tintaview` works,
then it attaches the wheel, the sdist, `install.ps1`, `install.sh` and `SHA256SUMS.txt` to the
GitHub Release. Building on Linux also keeps `install.sh` from being CRLF-mangled on the way into
the release — see `.gitattributes`.

## Testing conventions

- **Unit:** state priority across agents; engine factory per config/platform; **hook merge is
  idempotent and lossless** (golden-file tests against a realistic pre-existing `settings.json`);
  stats parsers against the captured fixtures in `tests/fixtures/`; stall-detector timing.
- **Integration:** fake Chroma REST and fake OpenRGB SDK servers, asserting snapshot/restore
  round-trips.
- Tests must pass on Windows too — watch for path separators, file permissions (`chmod` is a no-op
  there) and locked files.
- **Manual matrix before a release:** Windows+WSL (Chroma), Windows+WSL (OpenRGB), native Ubuntu
  (OpenRGB), macOS (status-only) × each agent — walk
  `session-start → working → confirm → idle → session-end` and confirm the lights return to normal.

## Known fragile surfaces

| Surface | Expectation |
| --- | --- |
| Codex hook feature flag and Windows availability | Will keep drifting; detect the version, fall back to `notify` (idle-only), state the limitation in the wizard |
| Cursor usage RPC | Unofficial; will break without warning. Degrade, never block |
| JetBrains `CREDIT_SCALE` | Reverse-engineered from one live widget reading, not documented. Only affects display formatting, never severity |
| Copilot CLI live quota | Not implemented — would need OS-credential-store access plus an undocumented two-step OAuth exchange with unverified field names. Local token totals only |
| Cursor confirm heuristic | Tune `stall_seconds` against real sessions; ship conservative; allow disabling |
| OpenRGB SDK version drift | Pin to v5 behaviour, feature-detect, fail soft |
| Unsigned build (accepted) | Ship via the install scripts so no MOTW tag ever reaches anything. If a Defender *heuristic* ever bites, submit to Microsoft's false-positive portal and consider free OSS signing (SignPath) or Azure Trusted Signing |
| PySide6 size (~60 MB) | Accepted for the usage-card quality; revisit `pystray` only if it becomes a real problem |
| macOS lighting | There isn't any realistically — positioned as status + stats only, stated up front |

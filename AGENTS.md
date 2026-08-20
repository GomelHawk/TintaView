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
  engines/   base.py  chroma.py  ghub.py  ghub_env.py  openrgb.py  null.py  factory.py
  agents/    base.py  claude.py  codex.py  cursor.py     # hook manifest + paths per agent
  stats/     providers/{claude,codex,cursor,jetbrains,copilot}.py
             cache.py  model.py  service.py  format.py   # format.py = shared row wording
  i18n/      __init__.py  locales/{en,es,it,de,pl,ru,be,uk}.json
  ui/        tray.py  flyout.py  wizard.py  icons.py  settings_dialog.py
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
| Packaging | **A pure-Python wheel installed into a private venv, on every platform.** No compiled bundle, no `.exe` installer. This is a hard requirement of Windows Smart App Control, not a preference — see [Packaging](#packaging-no-compiled-bundle-ever). On Windows the tray launches as **`pythonw.exe -m tintaview`**. G HUB's LED SDK silently no-ops under pythonw (measured); `GHubEngine` then paints via a short-lived **`python.exe` sidecar** (`engines/ghub_sidecar.py`). Chroma/OpenRGB stay in-process. |
| Autostart | **One per-user entry per platform, never a Scheduled Task**: HKCU `…\CurrentVersion\Run` value on Windows (*not* a Startup-folder shortcut — Windows 11 blocks those), a systemd `--user` unit plus an XDG autostart entry on Linux, a launchd agent on macOS. No admin rights, ever. |
| Hook install | **Auto-merge with a per-agent before/after diff and explicit confirmation.** A copy-paste snippet stays available as a fallback for locked-down environments. |
| Engines | **Chroma, G HUB and OpenRGB, Chroma default when reachable.** Auto-detect order `chroma → ghub → openrgb → status-only`. |
| Tray icon | **A single whole-icon state.** No per-agent ray splitting or zone splitting: a user watches one agent at a time, and a gradient icon among solid ones reads as a different icon rather than a fourth state. |
| Cursor stats | Local session token from `state.vscdb` → Cursor's own Connect RPC. No login UI of our own. |
| Translations | **JSON catalogues read by a stdlib `t()`, not `QTranslator`, and only for the tray + usage panel.** See [Interface language](#interface-language-i18n). |
| Stack | Python 3.12+, PySide6 (Qt) tray, stdlib HTTP server. |

Non-goals: lighting effects beyond solid/blink, per-agent colours or device zoning, multi-machine
or remote status, team dashboards, code signing / notarization, and distribution via PyPI,
winget, the Windows Store or Homebrew.

### Two config UIs — touch both

TintaView has **two** places a user changes settings, and they overlap on purpose:

- `ui/wizard.py` — the console wizard (`tintaview setup`). Covers everything: agent
  hook install/diff, autostart, WSL split, platform detection, interface language, and
  every engine's device-level knobs (OpenRGB host/port, G HUB `dll_path`, per-engine
  device-type lists).
- `ui/settings_dialog.py` — the in-process Qt popup opened from the tray's
  "Settings…" menu item (`TrayApp._open_settings` in `ui/tray.py`). Covers only the
  knobs worth reaching often without a terminal: enabled agents/providers (+ their
  order — a drag-reorderable list), interface language, chime, usage-poll interval,
  update-check, lighting engine mode, and the three status colours (with a
  reset-to-defaults button). It hands
  off to the console wizard via its "Open Full Setup Wizard (Terminal)…" button, and by
  raising `launch_wizard` when a newly ticked agent has no hooks installed.

**Whenever a `Config` field either UI exposes gets added, renamed, or its choices
change, update both.** What can drift is now deliberately narrowed to the fields
themselves — everything the two UIs *label* comes from one table each:

| Shared | Lives in | Read by |
| --- | --- | --- |
| engine keys, order, per-platform gating | `engines.factory.ENGINE_MODES` / `ENGINE_DISPLAY` / `engine_supported()` | both UIs |
| engine labels *as shown in the popup* | `i18n` key `engine.mode.<mode>`, falling back to `ENGINE_DISPLAY` | settings dialog |
| agent + stats-only provider keys and display names | `agents.base.STATS_ONLY_AGENTS` / `display_name()` | both UIs, tray tooltip, usage flyout |
| supported interface languages | `i18n.LANGUAGES` | both UIs |

Add a stats-only provider by adding a row to `agents.base.STATS_ONLY_AGENTS`, a detect
callable to `ui.wizard._STATS_ONLY_DETECT` and a provider to
`stats.service.DEFAULT_PROVIDERS` — no display name is written twice.

Two things the popup **cannot** do, because they're side effects the wizard attaches to
a field rather than the field itself, and each one silently produced a config that looks
right and never works:

- **Hooks.** Ticking an agent sets `enabled_agents` but installs nothing. The dialog
  therefore checks `install.hooks.status()` for newly ticked agents and offers the
  wizard; the diff-and-confirm install flow stays in the terminal.
- **Per-agent defaults.** The wizard seeds `confirm_detection` from the adapter (Cursor
  needs `stall`, not the `event` default) and fills a WSL-split UNC `home`. The dialog
  seeds `confirm_detection` (`_seed_new_agent_defaults`); the UNC `home` still needs the
  wizard, which is part of why the hook prompt points there.

Related invariant, in `core/config.py`: `dumps()` writes an `[agents.X]` table for every
*configured* agent, not just the enabled ones. Unticking an agent in the popup is one
click with no diff, and that table holds values the user can't re-derive (a WSL-split
UNC `home`, a hand-picked `state_db`/`quota_path`).

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
- **G HUB** (`engines/ghub.py`) loads `sdk_legacy_led_x64.dll` with `ctypes` — no new
  dependency, no bundled binary, Windows-only. Current G HUB ships it under
  `LGHUB\sdks\`; older installs keep it in the `LGHUB` root. Things about the legacy LED
  Illumination SDK that must keep being handled:
  1. **Colour is 0-100 percent, not 0-255** — every `set_color` call converts.
  2. **`LogiLedShutdown` on every `close()` — measured.** `RestoreLighting` (and
     per-zone restore) leave the mouse on our last colour; only `Shutdown` returns
     G HUB's profile. `LightController` opens/closes per session, so `close()` must
     Shutdown and the next `open()` re-`InitWithName` (with settle + retries) on the
     same SDK thread. Do not "simplify" back to restore-only until atexit — that
     stuck devices until the tray quit.
  3. **The SDK initialises per calling thread**, so every call — from hook handler
     threads, the blink thread, the heartbeat thread — is funnelled through one
     dedicated worker thread owned by the engine, never called ad hoc from whichever
     thread happens to be handling a request.
  4. **`LogiLedInitWithName("TintaView")`**, not bare `LogiLedInit`. Without a name G HUB
     registers the process as `python.exe` and typically leaves lighting disabled for it,
     so init succeeds and every later `SetLighting` is a silent no-op.
  5. **Mice are zoned, and G HUB is one colour behind.** `SetLighting` covers keyboards;
     mouse zones 0–1 are painted too. G HUB shows colour N-1 until a *later lighting
     call on a later turn of the SDK thread* — `SetTargetDevice` in the same burst and a
     sleep after `set_color` returns do not commit it. `set_color` therefore paints, then
     on a second pump job pumps Win32 messages and paints a 1% nudge so `pct` becomes N-1.
     Colour jobs share a coalesce key on the pump; the commit is posted (not waited on)
     so a blink storm drops stale colours instead of queuing them.
  6. **`probe()` must not call `LogiLedInit`.** Reachability is "DLL on disk and G HUB
     not known-stopped" (`engines/ghub_env.py`); init happens only in `open()`. Probing
     used to register TintaView in Integrations even when `auto` then picked Chroma.
     Environmental facts (process list, Dynamic Lighting registry, read-only
     `settings.db`) live in `ghub_env.py` so `doctor`/wizard can print measured blockers
     without loading the SDK. The Integrations toggle field name is unconfirmed — report
     `"unknown"`/`"absent"`, never guess `"on"`/`"off"`.
  7. **Under `pythonw.exe` the SDK is a silent no-op** (returns success, mouse stays on
     G HUB's profile — measured on G102 Lightsync). The tray **always** runs as
     `pythonw`; only this engine paints through a `python.exe` sidecar
     (`engines/ghub_sidecar.py` / `ghub_worker`). Never switch the whole tray/autostart
     to `python.exe` for this. In-process LED calls are used only inside the worker,
     under console `python.exe` tools (`doctor --paint`, smoke scripts), or with an
     injected DLL in tests.
  Unlike OpenRGB, G HUB does not need to be closed — it is designed to be driven while
  it runs — and the capability bitmask (`engine.ghub.device_types`) still has no
  mouse-vs-keyboard instance targeting, so a solid colour lands on every matching
  device at once.
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

## Interface language (i18n)

`tintaview/i18n/` — one JSON catalogue per language plus a `t(key, **kwargs)` lookup. English
(`ui.language = "en"`) is the default and `en.json` is the source of truth for the key set.
Languages: en, es, it, de, pl, ru, be, uk (`i18n.LANGUAGES`, endonyms included).

The decisions behind it, in the order they get questioned:

- **Not `QTranslator`/`.qm`.** Two of the three things that produce translated text can't
  import PySide6: the usage providers (`stats/providers/*`, which build row labels on a worker
  thread and run in a `--headless` install) and `core.config`. A Qt-only mechanism would need a
  second mechanism beside it, plus compiled `.qm` files committed as generated assets and a
  build step. JSON + `dict` lookups needs neither.
- **Scope is the tray and the usage panel, and stops there.** The console wizard, `doctor` and
  the CLI stay English: they are read once from a terminal, half their output is a diff of the
  user's own config files, and a partly-translated diff-and-confirm flow is worse than an
  English one. The wizard does still *set* `ui.language` (its first step) — that is the only
  route a first-time install has to it.
- **Nothing an agent's API returns is ever translated** — plan and model names, release notes,
  HTTP error text. Interpolate them into a translated sentence, don't rewrite them.
- **`t()` never raises.** Missing key → English → the key itself; a translation whose
  placeholders don't match the call is skipped in favour of English. It is called from
  `paintEvent` and from stats threads, where an exception is a crash or a blank panel.
- **Named placeholders only** (`{count}`, never `{}`), so a translator can reorder them, and
  a caller may pass *more* than a given language uses: `usage.reset.at_time` gets `hour12`,
  `hour24` and `ampm`, and each catalogue picks the clock its readers expect.
- **Plurals are a table of CLDR forms**, selected by `count=`. Russian, Ukrainian, Belarusian
  and Polish need three (`one/few/many`) — "5 сессий" is not "5 сессия".
- Shared row wording (weekday and month abbreviations, "Resets in …") lives in
  `stats/format.py`, not in each provider. `strftime("%a")`/`%b`/`%p` are the C locale, not
  the user's choice, so they are never used for user-visible text.

Three layout rules exist **only** because a translated string is 30-40% longer than its
English original, and each of them was a visible defect first:

- **Settings dialog** (`ui/settings_dialog.py`): checkboxes and explanatory hints go in
  *spanning* form rows, never in the field column beside a label — a `QCheckBox` clips its
  own text instead of wrapping (German lost a word off the end), and the field column is
  only as wide as the longest label leaves it. Hints are built by `_hint()`, which sets an
  explicit `minimumHeight` from `heightForWidth`: a word-wrapped `QLabel` reports a
  one-line `sizeHint`, so its second line lands on top of the widget below it. `_MIN_WIDTH`
  is sized for the longest language, not for English.
- **Usage flyout** (`ui/flyout.py`): a row's label is drawn into the width the right-hand
  text leaves, elided, with the right half a point smaller. Drawing both into the same
  full-width rect (left- and right-aligned) works only while they are short enough not to
  meet; translated labels and reset times overprinted each other.
- **`stats.format.reset_at_time`** passes the hour twice (12- and 24-hour) and lets each
  catalogue pick. Non-English catalogues use the short noun form ("Сброс через …", "Reset
  in …") rather than a full sentence, because that column is what squeezes the label.

`tests/test_i18n.py` is what keeps this honest: key parity and placeholder parity against
`en.json`, every plural form its language's rule can select, every literal `t("…")` in the
package resolving to a real key, no unused keys, and `pyproject.toml` still declaring
`"tintaview.i18n" = ["locales/*.json"]` as package data (without which the wheel ships no
catalogues and every language silently falls back to English).

Adding a language: a row in `i18n.LANGUAGES`, a `locales/<code>.json` copied from `en.json`
and translated, and a plural rule in `_PLURAL_RULES` if it isn't one/other. Adding a *string*:
a key in `en.json` **and every other catalogue** — the parity tests fail otherwise, which is
the point.

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
own). On Windows the login/tray entry is **`pythonw.exe -m tintaview`**. G HUB's legacy LED SDK
was measured to return success from `SetLighting` under `pythonw` while leaving the mouse on G HUB's
own profile; the tray therefore keeps `pythonw` and, only for the `ghub` engine, spawns a
`python.exe -m tintaview.engines.ghub_worker` sidecar that owns the DLL. **Do not reintroduce a
frozen bundle or an `.exe` installer without a code-signing certificate to go with it** —
`tests/test_packaging.py` guards this.

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
- **Integration:** fake Chroma REST and fake OpenRGB SDK servers, plus a fake DLL object
 injected into `GHubEngine`'s constructor, asserting snapshot/restore round-trips.
- Tests must pass on Windows too — watch for path separators, file permissions (`chmod` is a no-op
  there) and locked files.
- **Language is process-global.** Most of the suite asserts English text, so any test that calls
  `i18n.set_language` must put it back (`tests/test_i18n.py` does it in an autouse fixture; the
  tray/wizard tests use `try/finally`). A leaked language fails a test in another module
  entirely.
- The full-wizard tests in `tests/test_wizard.py` feed `input()` a *positional* list of answers,
  so adding or removing a wizard step means updating every one of those lists.
- **Manual matrix before a release:** Windows+WSL (Chroma), Windows+WSL (G HUB), Windows+WSL
 (OpenRGB), native Ubuntu (OpenRGB), macOS (status-only) × each agent — walk
 `session-start → working → confirm → idle → session-end` and confirm the lights return to
 normal (for G HUB specifically, that G HUB itself takes its own profile back on `close()`).

## Known fragile surfaces

| Surface | Expectation |
| --- | --- |
| Codex hook feature flag and Windows availability | Will keep drifting; detect the version, fall back to `notify` (idle-only), state the limitation in the wizard |
| Cursor usage RPC | Unofficial; will break without warning. Degrade, never block |
| JetBrains `CREDIT_SCALE` | Reverse-engineered from one live widget reading, not documented. Only affects display formatting, never severity |
| Copilot CLI live quota | Not implemented — would need OS-credential-store access plus an undocumented two-step OAuth exchange with unverified field names. Local token totals only |
| Cursor confirm heuristic | Tune `stall_seconds` against real sessions; ship conservative; allow disabling |
| G HUB legacy LED SDK + start order | Undocumented/unofficial by Logitech's own admission; `RestoreLighting` does not hand mice back — `close()` must `LogiLedShutdown` and the next `open()` re-inits. If G HUB restarts under us the session is orphaned — surface a status_note and restart TintaView. **`pythonw.exe` silent no-op:** SetLighting returns true but does not paint — tray stays on `pythonw`, paint goes through a `python.exe` sidecar only for this engine |
| OpenRGB SDK version drift | Pin to v5 behaviour, feature-detect, fail soft |
| Unsigned build (accepted) | Ship via the install scripts so no MOTW tag ever reaches anything. If a Defender *heuristic* ever bites, submit to Microsoft's false-positive portal and consider free OSS signing (SignPath) or Azure Trusted Signing |
| PySide6 size (~60 MB) | Accepted for the usage-card quality; revisit `pystray` only if it becomes a real problem |
| macOS lighting | There isn't any realistically — positioned as status + stats only, stated up front |
| Non-English catalogues | Written in-house, not by native speakers, and not reviewed by one. `en.json` is the source of truth and the parity tests keep the *structure* right; wording is fixed per report, not by re-translating everything. Expect a placeholder mistake to surface as English (by design, see `t()`) rather than as a crash |

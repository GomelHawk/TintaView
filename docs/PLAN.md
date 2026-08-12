# TintaView — implementation plan

> Universal agent-status lighting + usage tray for **Claude Code**, **Codex CLI** and **Cursor**,
> driving **Razer Chroma** or **OpenRGB** devices on **Windows / WSL / Linux / macOS**.
>
> Status: plan approved, not yet implemented.

---

## 0. Locked decisions

| Decision | Choice |
| --- | --- |
| Audience | Me + colleagues / small team. **No code signing** — the packaging is chosen so that no signature is needed at all (see Packaging). |
| Packaging | **A wheel installed into a private venv, on every platform.** Windows: `install.ps1`, run as `irm … \| iex`; Linux/macOS: `install.sh`. **No compiled bundle and no `.exe` installer** — see §8.3, this is not a preference but a hard requirement of Smart App Control. |
| Autostart | **One per-user entry per platform, never a Scheduled Task**: HKCU `Run` value (Windows — *not* a Startup-folder shortcut, which Windows 11 blocks), systemd `--user` unit + XDG autostart (Linux), launchd agent (macOS). |
| Hook install | **Auto-merge, with a per-agent before/after diff and explicit confirmation.** Manual copy-paste snippet stays as a fallback. |
| Engines | **Both Chroma and OpenRGB. Chroma is the default** when reachable; auto-detect order `chroma → openrgb → status-only`. |
| Tray icon | **Single whole-icon state.** No per-agent ray splitting / zone splitting — a user watches one agent at a time. |
| Cursor stats | **Local session token** from `state.vscdb` → Cursor Connect RPC (see §6.3). |
| Language / stack | Python 3.11+, PySide6 (Qt) tray, stdlib HTTP server. |
| Work split | Mechanical implementation (scaffolding, boilerplate, packaging config, straightforward parsers/tests) goes to **Sonnet subagents**; design-sensitive logic stays in the main thread — see §10. |

## 1. Scope

**In scope (v1):** three agents; two lighting engines + status-only; one-process daemon+tray; usage/stats panel per agent; guided installer with agent/engine/path selection; automated hook installation with diff; WSL split install; self-update; an install script per platform.

**Non-goals (v1):** lighting effects beyond solid/blink; per-agent colors or device zoning; multi-machine / remote status; mobile; team dashboards; code signing / notarization; PyPI, Windows Store, winget or Homebrew distribution.

---

## 2. Architecture

### 2.1 Process model — one process, not two

A split design (a background service plus a separate tray process, with an autostart entry each) is the biggest source of install pain. TintaView is **one process**:

```
tintaview            → tray UI + status broker in-process (default)
tintaview --headless → status broker only (no GUI: servers, WSL-only boxes)
tintaview setup      → the install/reconfigure wizard
tintaview doctor     → diagnostics
tintaview hooks …    → install / status / uninstall
tintaview update     → self-update
```

One autostart entry, one log file, one thing to update. The HTTP broker keeps running on `127.0.0.1:8777` so the hook contract is unchanged and the `curl.exe` WSL trick still works.

### 2.2 State model

Key sessions by `(agent, sid)` instead of `sid`:

```python
sessions: dict[tuple[str, str], SessionState]   # ("claude", "abc123") -> working
```

Colour priority is unchanged and global: `confirm > working > idle > none`.
`/state` additionally reports a per-agent breakdown so the tray tooltip can say
`Claude: working · Codex: idle`.

### 2.3 HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/event/{event}?agent=&sid=&tool=` | Hook ingress. `event` ∈ `session-start, session-end, working, idle, confirm, tool-start, tool-end` |
| `GET` | `/state` | Read-only status for the tray (**never** touches `last_ping` — that would defeat the watchdog) |
| `GET` | `/healthz` | Liveness for `doctor` |

Agent-less aliases `/{session-start,session-end,working,idle,confirm}` default to `agent=claude`, so a hand-written hook script stays a one-line `curl` without needing the multi-agent query API.

`/state` response:

```json
{
  "effective": "working",
  "agents": {"claude": {"effective": "working", "sessions": {"abc": "working"}, "count": 1},
             "codex":  {"effective": "none", "sessions": {}, "count": 0}},
  "blinking": false,
  "engine": {"name": "chroma", "active": true},
  "version": "1.0.0"
}
```

Hook ingress must **acknowledge before doing any lighting I/O** so a slow Chroma/OpenRGB call can never stall an agent.

### 2.4 Config

Single file, read by daemon, tray, wizard and `doctor`:
`~/.tintaview/config.toml` — on Windows `%LOCALAPPDATA%\TintaView\config.toml`.

```toml
version = 1

[server]
host = "127.0.0.1"
port = 8777
watchdog_timeout = 600        # force-release after this much hook silence

[engine]
mode  = "auto"                # auto | chroma | openrgb | none
order = ["chroma", "openrgb"] # auto-detect order — Chroma first by decision

[engine.chroma]
devices = ["mouse", "headset"]

[engine.openrgb]
host = "127.0.0.1"
port = 6742
device_types = ["mouse", "keyboard", "headset"]   # peripherals only; [] = every device
restore_on_release = true     # snapshot mode+colors on open, restore on close
direct_mode_only = true       # skip devices without a Direct mode (no flash wear)

[colors]
idle = "#56D155"; working = "#F0B30C"; confirm = "#F42D3C"; none = "#0080F7"; blink_ms = 400

[agents]
enabled = ["claude", "codex", "cursor"]

[agents.claude]
home = "~/.claude"            # WSL split: "\\\\wsl.localhost\\Ubuntu\\home\\dmitry\\.claude"
confirm_detection = "event"   # Notification/permission_prompt

[agents.codex]
home = "~/.codex"
confirm_detection = "event"   # PermissionRequest

[agents.cursor]
state_db = ""                 # auto-detected; see §6.3
confirm_detection = "stall"   # heuristic — see §5.3
stall_seconds = 8

[stats]
poll_seconds = 300            # usage APIs rate-limit; windows are hours
[ui]
chime_on_confirm = false
[update]
check = true
channel = "stable"
```

### 2.5 Repo layout

```
tintaview/
  core/      config.py  state.py  server.py  stalldetect.py  watchdog.py
  engines/   base.py  chroma.py  openrgb.py  null.py  factory.py
  agents/    base.py  claude.py  codex.py  cursor.py      # hook manifest + stats provider per agent
  stats/     providers/{claude,codex,cursor}.py  cache.py  model.py
  ui/        tray.py  flyout.py  wizard.py  icons.py
  install/   detect.py  hooks.py  diff.py  autostart_{win,linux,mac}.py  wsl.py  doctor.py  update.py
  hooks/     tv-hook.sh  tv-hook.cmd
assets/      icon.png  full_logo.png  transparent.png  generated/{ico,icns,png sizes}
packaging/   install.ps1  install.sh  tintaview.desktop
docs/        PLAN.md  README.md  TROUBLESHOOTING.md
tests/
```

---

## 3. Lighting engines

Interface:

```python
class LightingEngine(Protocol):
    name: str
    def probe(self) -> bool: ...      # is this engine usable right now? (wizard + auto mode)
    def open(self) -> bool: ...       # connect, snapshot current lighting, take control
    def set_color(self, r, g, b) -> None: ...
    def close(self) -> None: ...      # restore previous lighting, release
    def heartbeat(self) -> None: ...  # no-op where not needed
    @property
    def active(self) -> bool: ...
```

**`ChromaBackend` (default).** POST to open, PUT `/heartbeat` every 4 s, PUT per device with **BGR** packing, DELETE to close. Keep the `init_cooldown` back-off. Release is automatic — Synapse takes back over on DELETE.

**`OpenRGBBackend`.** `openrgb-python` against the SDK server on `127.0.0.1:6742`. **RGB, not BGR.** Three things Chroma does for free that must be implemented here:

1. **Snapshot & restore.** OpenRGB has no session concept. On `open()`, record each target device's active mode and per-LED colors; on `close()`, restore them. Without this the user's rig stays green after the agent exits.
2. **Direct mode only.** Filter to devices exposing a Direct mode before setting colors — blinking a non-Direct device writes to flash.
3. **Reconnect/back-off** mirroring the Chroma `init_cooldown` pattern (lift it into the base class).
4. **Peripherals only by default** — mouse, keyboard and headset. Motherboard, RAM, GPU and
   case lighting is ambient decoration; driving it would make the whole room flash amber on
   every tool call. `engine.openrgb.device_types = []` opts back in to every device.

**`NullBackend`.** Status-only: tracks state for tray + stats, drives nothing. Always available.

**Factory / auto mode:** `probe()` each engine in `engine.order`, first success wins, fall back to Null. The wizard shows each engine as *detected / not running / not supported on this platform*.

### Platform matrix

| | Windows | Windows + WSL | Linux | macOS |
| --- | --- | --- | --- | --- |
| Chroma (Razer) | ✅ default | ✅ (daemon on Windows) | ❌ | ❌ (Synapse discontinued) |
| OpenRGB (Razer, Logitech, Corsair, ASUS, MB/RAM) | ✅ | ✅ | ✅ best support | ⚠️ very limited devices |
| Status-only | ✅ | ✅ | ✅ | ✅ |

Wizard must warn in plain language that **OpenRGB and Synapse / G HUB fight over the same devices**, and on Linux offer to install OpenRGB's udev rules + `i2c-dev` (with the sudo prompt explained up front).

---

## 4. Agents — verified capabilities

| | Config file TintaView writes | Session start/end | Working signal | Explicit "waiting for you" |
| --- | --- | --- | --- | --- |
| **Claude Code** | `~/.claude/settings.json` | `SessionStart` / `SessionEnd` | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | `Notification` + matcher `permission_prompt` |
| **Codex CLI** | `~/.codex/hooks.json` (never their `config.toml`, except the feature flag) | `SessionStart` / `SessionEnd` | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` | **`PermissionRequest`** (first-class) |
| **Cursor** | `~/.cursor/hooks.json` (`{"version": 1, …}`) | `sessionStart` / `sessionEnd` | `beforeSubmitPrompt`, `preToolUse`, `postToolUse` | ❌ none → stall heuristic (§5.3) |

Per-agent gotchas the installer must handle:

- **Cursor sends `conversation_id`, not `session_id`** in hook stdin.
- **Codex hooks are version-gated and the flag name moved.** Early builds (~v0.114+) need `[features] codex_hooks = true`; newer docs show hooks on by default with `hooks = false` to disable. Detect via `codex --version` and write the correct flag, showing that TOML edit in the diff too.
- **Codex hooks were unavailable on Windows in early builds.** For Windows-native Codex, fall back to the `notify` program, which only fires `agent-turn-complete` → *idle only*. Degraded but honest; the wizard says so.
- **Claude's `Notification` matcher** is what already works today; keep it.

---

## 5. Hook layer

### 5.1 The hook binary

`~/.tintaview/bin/tv-hook <agent> <event>` — **a stable path that never changes across updates**, so upgrading TintaView never rewrites any agent's config.

Hooks fire on every tool call, so the hook must be ~5 ms and unable to stall an agent:

- POSIX `sh` + `curl`, session id extracted with `sed` (`session_id` / `conversation_id`) — **no Python startup**.
- `curl -s -m 1`, output discarded, always `exit 0`.
- Reads `~/.tintaview/hook.env` for `TINTAVIEW_URL` and `TINTAVIEW_CURL` (written at install time).
- `tv-hook.cmd` twin for Windows-native agents.

### 5.2 Event mapping

| TintaView event | Claude | Codex | Cursor |
| --- | --- | --- | --- |
| `session-start` | `SessionStart` | `SessionStart` | `sessionStart` |
| `working` | `UserPromptSubmit`, `PostToolUse` | `UserPromptSubmit`, `PostToolUse` | `beforeSubmitPrompt`, `postToolUse` |
| `tool-start` | `PreToolUse` | `PreToolUse` | `preToolUse`, `beforeShellExecution` |
| `tool-end` | `PostToolUse` | `PostToolUse` | `postToolUse`, `afterShellExecution` |
| `confirm` | `Notification`/`permission_prompt` | `PermissionRequest` | *(stall heuristic)* |
| `idle` | `Stop` | `Stop` | `stop` |
| `session-end` | `SessionEnd` | `SessionEnd` | `sessionEnd` |

### 5.3 Stall detector (Cursor's missing "confirm")

`tool-start` records `(agent, sid, ts, tool)`. If `stall_seconds` elapse with no `tool-end` and no other event for that session, assume the agent is sitting on an approval prompt → **confirm**.

Enabled **only** where `confirm_detection = "stall"` (Cursor), default 8 s, tunable. Deliberately conservative: a long `pytest` run must not turn the lights red. During implementation, refine using the `sandbox` / auto-run fields Cursor puts in `beforeShellExecution` stdin, and tune the threshold against real sessions before enabling by default.

### 5.4 Automated hook installation

`tintaview hooks install|status|uninstall [--agent claude|codex|cursor|all] [--scope user|project]`

Mechanics:

1. **Read** the agent's config (JSON, or TOML via `tomlkit` so comments/formatting survive).
2. **Merge** only TintaView-owned entries — identified by the command containing the `tv-hook` sentinel. Everything else in the file is untouched. Your existing Claude hooks and hand edits survive.
3. **Show the diff** — unified before/after per file — and require an explicit OK per agent *(chosen behaviour)*.
4. **Back up** to `<file>.tintaview-backup-<ISO8601>` (keep last 5), then write **atomically** (temp + rename).
5. **Idempotent**: re-running produces no diff and no backup.
6. `uninstall` removes exactly the sentinel-marked entries and nothing else.
7. `status` reports per agent: `installed / missing / partial / stale-path`.

**Drift detection.** Agents rewrite their config on upgrade. The tray re-checks hook presence every few minutes and surfaces *"Hooks missing for Codex — Fix"* instead of silently going dark.

**Fallback path.** For locked-down environments, a wizard page shows the exact snippet with real paths and a **Copy** button, per agent, per scope.

**Later (post-v1):** Claude Code and Codex both support plugin-bundled hooks, so a `tintaview` plugin could make install a single `/plugin install`. Cursor has no plugin equivalent.

---

## 6. Statistics

Pluggable `UsageProvider` per agent, all normalized to the existing row model
(`{label, pct, right, show_pct, severity, kind}`) so the tray flyout renders any agent identically.

### 6.1 Claude Code — already built
`GET https://api.anthropic.com/api/oauth/usage` with the OAuth token from `~/.claude/.credentials.json`
→ official 5-hour / weekly / credits. Fallback: reconstruct from `~/.claude/projects/**/*.jsonl`.
Port `usage.py` essentially as-is.

### 6.2 Codex — local, no network
`~/.codex/sessions/**/rollout-*.jsonl` carries `event_msg` records of type `token_count`:

- `info.total_token_usage` → token totals (always present).
- `rate_limits.primary/secondary` → `used_percent`, `resets_at`, `plan_type` — populated on ChatGPT-plan sessions, `null` on API-key sessions (verified on this machine).

Show official percentages when present, token totals otherwise.

### 6.3 Cursor — local session token → Connect RPC

No personal usage API exists; Cursor transcripts contain **no token counts** (verified). The practical keyless path:

1. Read `cursorAuth/accessToken` from the `ItemTable` of Cursor's `state.vscdb`
   (Linux `~/.config/Cursor/User/globalStorage/`, macOS `~/Library/Application Support/Cursor/User/globalStorage/`,
   Windows `%APPDATA%\Cursor\User\globalStorage\`). Verified present on this machine.
2. `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage`
   with `Authorization: Bearer <token>`, `Connect-Protocol-Version: 1`, body `{}`
   → current-period usage (plan usage in cents, remaining, %).
3. On 401/403, re-read the token from disk (Cursor refreshes it while signed in). **No login UI of our own.**

Rules, because this is unofficial and the token is a credential:

- The DB is large (**323 MB here**) and held open by Cursor → open `sqlite3` with `file:…?mode=ro`, handle WAL/lock errors, **never copy it**.
- Read the token fresh on each poll; never log, cache or persist it; mask it in `doctor` output; pin requests to `api2.cursor.sh`.
- Poll on the shared 5-minute cadence, never per-second.
- Degrade gracefully to *"Cursor not signed in"* / *"usage endpoint unavailable"* — this can break at any Cursor release.

### 6.4 Caching & performance
Last good result cached to `~/.tintaview/usage_cache.json` so the flyout is never blank and a rate-limit never replaces good data with an estimate (existing behaviour). In the **WSL split**, Claude/Codex JSONL live behind a UNC path — scan only files modified in the last 7 days and cache by mtime, or the poll will be slow.

---

## 7. Tray UI

Reuse the existing Qt flyout painting. Changes:

- Icon: the TintaView burst silhouette, recoloured per state in the mark's own gradient hues — **blue no session / green idle / yellow working / red blinking confirm**. Single whole-icon state, per decision. (An earlier draft showed the multicolour mark for "no session"; a gradient icon among solid ones reads as a different icon rather than a fourth state.)
- Tooltip: `Claude: working (1 session) · Codex: idle`.
- Flyout: a section per enabled agent; agents with no data show a one-line reason.
- Menu: `Refresh usage · Sound on confirm · Settings… (reopens the wizard) · Check for updates · Quit`.
- Asset pipeline: generate `.ico` (Windows), `.icns` (macOS) and the PNG size set from `assets/icon.png` at build time.
- GNOME still needs an AppIndicator extension — `doctor` detects and explains.

---

## 8. Install & update

### 8.1 One wizard, three entry points

The wizard is Python and is invoked identically by `install.ps1`, `install.sh`, and `tintaview setup --reconfigure` — one code path to keep correct.

1. **Platform** — auto-detect `windows` / `windows+wsl` / `linux` / `macos`, with `--platform` override when detection is wrong.
2. **Agents** — probe `~/.claude`, `~/.codex`, `~/.cursor` and `PATH`; checkbox list pre-ticked by what's found; **at least one required**.
3. **Engine** — probe Chroma and OpenRGB, show each as detected / not running / unsupported; status-only always offered. Default = Chroma when reachable.
4. **Install path** — defaults `%LOCALAPPDATA%\TintaView`, `~/.local/share/tintaview`, `/Applications`.
5. **Autostart** — HKCU `Run` value / systemd `--user` unit / launchd agent. No admin, no Scheduled Task.
6. **Hooks** — per agent, per scope, with the diff + confirm from §5.4.
7. **Verify** — run `doctor`, then a live check: *"start a session in your agent now"* and show events arriving in real time. This is what turns a technically-correct install into one a non-technical user trusts.

### 8.2 WSL split (Windows installer drives both sides)

Daemon + tray on **Windows**; hooks in **WSL**. The installer:

- enumerates distros via `wsl.exe -l -q` and lets the user pick;
- installs `tv-hook` + `hook.env` **inside** the distro and patches the in-WSL agent configs over `wsl.exe -d <distro> -- …`;
- writes UNC paths into the Windows-side config (`\\wsl.localhost\<distro>\home\<user>\.claude`, `…/.codex`);
- uses the **`curl.exe` trick**: called from WSL, `curl.exe` runs in the *Windows* network namespace, so `127.0.0.1:8777` reaches the daemon with **no firewall rule and no mirrored-networking requirement**.

The user never opens a WSL terminal.

### 8.3 Packaging

One artifact for every platform: a pure-Python **wheel**, installed into a private virtual
environment under the install prefix. No compiled bundle exists.

- **Windows:** `packaging/install.ps1` (`irm … | iex`) — finds a Python ≥3.11, creates
  `<prefix>\venv`, pip-installs the wheel with the `[ui]` extra, writes a Start Menu
  shortcut and the autostart entry, then launches the wizard.
- **Linux:** `install.sh` — private venv under the install path, `.desktop` entry, systemd `--user` unit, hooks.
- **macOS:** `install.sh` — same, launchd agent. Status-only in practice (no lighting on macOS).

**Why no `.exe`, and why not even a frozen bundle.** Windows runs two independent
defences against unsigned software, and they need different answers:

1. **Mark-of-the-Web.** Browsers tag downloads with a `Zone.Identifier` stream; that tag
   is what makes Edge/Chrome block an unsigned installer on reputation, and what makes
   SmartScreen show "Windows protected your PC" on launch. Nothing PowerShell downloads
   goes through the Attachment Execution Service, so no tag is written and neither check
   fires. The trap to avoid: telling users to download a `.zip` and extract it by hand
   does *not* help — Explorer's extractor propagates the zone into every file it writes.
2. **Smart App Control.** On by default on clean Windows 11 installs, and MOTW is
   irrelevant to it — it refuses to run any executable that is neither signed nor
   cloud-reputable, however it arrived. A PyInstaller bundle loses here permanently:
   every build is byte-unique, so it has no reputation, and every release rebuilds it, so
   it can never accumulate any. **Measured on an enforcing machine:** the PyInstaller
   `TintaView.exe` was blocked (CodeIntegrity event 3118), while `python.exe`/`pythonw.exe`
   (PSF-signed, signature preserved through venv creation), the pip console shim, and all
   of PySide6's DLLs ran without complaint — as did the tray itself, serving `/state`.

Installing a wheel into a venv satisfies both with no certificate: the only executables
involved are the signed interpreter and widely-mirrored PyPI wheels. **Don't reintroduce a
frozen bundle or an `.exe` installer without a code-signing certificate to go with it** —
`tests/test_packaging.py` guards this.

`install.ps1` verifies the wheel's SHA-256 against the release's `SHA256SUMS.txt` before
installing and is idempotent (it *is* the update mechanism). Two Windows-specific traps it
encodes:

- On Windows `config_dir()` **is** the install prefix (`%LOCALAPPDATA%\TintaView`) —
  config.toml, hook.env, `bin\tv-hook.cmd` and `logs\` are siblings of the venv. So the
  prefix is never deleted recursively; only `<prefix>\venv`, which the installer owns
  outright, ever is.
- `pip install --upgrade` compares version numbers and does nothing when they match, so
  the wheel is additionally force-reinstalled with `--no-deps`. Without that, re-running
  the installer cannot repair a damaged install and a re-tagged release silently keeps the
  old code while reporting success.

### 8.4 Updating

Tray checks the GitHub Releases API weekly (`update.check`) → *"Update available"*:
Both platforms download the release's own install script, verify its SHA-256 against the release's checksums asset, and re-run it — `install.ps1 -Silent` on Windows (detached, since it stops the interpreter running out of the venv it is about to replace), `sh install.sh` on Linux/macOS. `tintaview update` does the same from the CLI, passing `-Prefix` derived from `sys.prefix` so a non-default install upgrades itself instead of spawning a second copy.
**Config and hooks are never touched by an update** — hooks point at the stable `tv-hook` path.

### 8.5 CI

`ci.yml`: ruff + pytest on ubuntu / windows / macos.
`build.yml`: on tag `v*` → `python -m build` on ubuntu-latest (the wheel is pure Python, so no Windows runner and no matrix), smoke-test that the wheel installs and that `python -m tintaview` works, then attach the wheel, the sdist, `install.ps1`, `install.sh` and `SHA256SUMS.txt` to the GitHub Release. Building on Linux also keeps `install.sh` from being CRLF-mangled on the way into the release (see `.gitattributes`).

---

## 9. Testing

- **Unit:** state priority across agents; engine factory per config/platform; **hook merge is idempotent and lossless** (golden-file tests with a realistic pre-existing `settings.json`); stats parsers against captured fixtures; stall detector timing.
- **Integration:** fake Chroma REST and fake OpenRGB SDK servers; assert snapshot/restore round-trips.
- **Manual matrix:** Windows+WSL (Chroma), Windows+WSL (OpenRGB), native Ubuntu (OpenRGB), macOS (status-only) × each agent — walk `session-start → working → confirm → idle → session-end` and confirm the lights return to normal.

---

## 10. Milestones

| # | Milestone | Deliverable |
| --- | --- | --- |
| M0 | Bootstrap | Repo skeleton, `pyproject.toml`, ruff/pytest, asset pipeline (ico/icns/png), CI lint |
| M1 | Core | Config loader, `(agent, sid)` state model, HTTP server + endpoints + watchdog + stall detector, Null engine, `doctor` |
| M2 | Engines | Chroma port (default), OpenRGB + snapshot/restore + Direct-mode filter, auto-detect factory |
| M3 | Agents & hooks | `tv-hook`, per-agent manifests, `hooks install/status/uninstall` with diff+confirm, backups, drift check |
| M4 | Tray & stats | Qt tray + flyout, three stats providers, cache, settings menu |
| M5 | Install & update | Wizard + WSL page, `install.ps1`, `install.sh`, autostart, self-update, `build.yml` |
| M6 | Docs & polish | README, TROUBLESHOOTING, manual test matrix pass |

M1–M3 carry the real logic; M5 carries the calendar time.

**Delegation.** Mechanical work is handed to Sonnet subagents: M0 in full, the asset pipeline,
`install.ps1` / `install.sh` / systemd / launchd boilerplate, the Claude and Codex stats parsers
(fixture-driven), and test scaffolding. Kept in the main thread because a subtle mistake is
expensive: OpenRGB **snapshot/restore**, the **hook merge/diff** (it rewrites the user's real
`~/.claude` / `~/.codex` / `~/.cursor` files), the **stall detector**, the Cursor **token handling**,
and the WSL split installer.

## 11. Risks & open items

| Risk | Mitigation |
| --- | --- |
| Codex hook feature flag / Windows availability drift | Detect `codex --version`; fall back to `notify` (idle-only) on Windows-native; state the limitation in the wizard |
| Cursor RPC is unofficial and can break | Degrade to "usage unavailable", never block the tray; token handled as a credential (§6.3) |
| Cursor "confirm" is a heuristic | Tune `stall_seconds` against real sessions; ship conservative; allow disabling |
| OpenRGB vs Synapse/G HUB device conflict | Chroma is the default; wizard warns explicitly; `doctor` detects both running |
| OpenRGB SDK version drift (v5 released / v6 pipeline) | Pin to SDK v5 behaviour, feature-detect, fail soft to status-only |
| Unsigned build (accepted) | Ship via `install.ps1`, which never lets a Mark-of-the-Web tag reach the binary, so SmartScreen never fires (§8.3). Remaining exposure is a Defender *heuristic* hit on the PyInstaller bootloader — mitigated by `onedir` + no UPX; if it ever bites, submit to Microsoft's false-positive portal and consider free OSS code signing (SignPath) or Azure Trusted Signing |
| PySide6 bundle size (~60 MB) | Accepted for the usage card quality; revisit `pystray` only if it becomes a problem |
| macOS has no realistic lighting | Positioned as status + stats only, stated up front |

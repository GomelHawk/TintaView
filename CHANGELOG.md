# Changelog

What changed for people using TintaView, newest first. Each version's section is also the
text of its [GitHub Release](https://github.com/GomelHawk/TintaView/releases). Releases
before 0.5.0 are described only there.

## 0.9.2 — 2026-10-09

- **Missed a "notify me when it's done" message? It's under the new bell** in the usage
  panel's title bar. The bell keeps the last 10 messages your agents sent, newest first: hover
  it for the latest one, click it to read them all in full and copy them. An orange dot means
  something arrived since you last looked; **Clear** empties the list. The messages are kept
  until TintaView restarts.
- **Testing a long notification sound can now be stopped**: while it plays, the **Test** button
  in **Settings… → Sound** turns into **Stop**. Closing Settings stops it too.
- The settings button in the usage panel has a new, lighter gear icon to match the bell.

## 0.9.1 — 2026-10-09

- **"Notify me when it's done" now works in GitHub Copilot CLI too.** Run the setup wizard
  once more (tray → **Settings…** → **Open Full Setup Wizard (Terminal)…**) or
  `tintaview hooks install --agent copilot`: it adds TintaView's `notify_user` tool to
  `~/.copilot/mcp-config.json`, shown first and only after you confirm. Copilot may ask
  before the tool runs; approve it for the session, or start Copilot with
  `--allow-tool='tintaview(notify_user)'` to skip the question.

## 0.9.0 — 2026-10-09

- **GitHub Copilot CLI now lights up**, like Claude Code, Codex and Cursor. Its lights turn
  red when Copilot asks for permission or asks you a question. To switch it on, run the setup
  wizard once (tray → **Settings…** → **Open Full Setup Wizard (Terminal)…**) or
  `tintaview hooks install --agent copilot`. TintaView's hooks go in a file of their own,
  `~/.copilot/hooks/tintaview.json`, and nothing of yours is edited. It needs a recent
  Copilot CLI (tested on 1.0.94 and later). In a WSL setup it's the Copilot you run inside
  WSL that lights up. If Copilot was already ticked for its usage card, the tray tells you at
  startup that its hooks are missing.
- **Keep awake**: a new tray menu item that stops the computer from going to sleep — also
  after you lock it — so a long agent run finishes while you're away. The screen can still
  turn off to save power. While it's on, the tray icon shows a small green shield. It is
  remembered across restarts. With a laptop on its charger, closing the lid may not put it
  to sleep while this is on.
- **Your own notification sound.** The new **Sound** tab in **Settings…** has the
  "Sound when an agent needs confirmation" option (moved from General), plus a custom WAV,
  OGG or MP3 file with its own volume and a **Test** button. If the file can't be played,
  the usual system sound plays instead.
- **Fixes after an update are picked up by themselves**: TintaView now refreshes its own hook
  script at startup when it's out of date, instead of waiting for you to re-run the setup.
- **Codex**: to stop the "clamping SessionEnd hook timeout to 3s" warning at every Codex
  start, run `tintaview hooks install --agent codex` once.
- Pressing **OK** in Settings no longer undoes a Keep awake toggle or a collapsed usage
  section you changed while the window was open.
- `tintaview doctor` also checks Keep awake and your custom sound, and whether the hook
  script is up to date.

## 0.8.1 — 2026-09-29

- **The explanations in Settings are readable on a dark theme.** On Windows' dark mode they
  were drawn almost in the background colour and looked like empty space.
- **The Settings window is about a quarter shorter**, with shorter hints on the Alerts tab.
  The details of each command's variables are in the README.

## 0.8.0 — 2026-09-29

- **"Notify me when it's done."** Start a long task in Claude Code, Codex or Cursor and add
  *"notify me via phone when it's done"*. The agent calls TintaView's new `notify_user` tool
  when it finishes, with its own one-line summary, and the tray shows it as a notification.
  To get it on your phone, set a command under **Settings… → Alerts → When an agent notifies
  me**. The Telegram example from the README works as is, with `TINTAVIEW_STATUS=notify`
  and the agent's text in `TINTAVIEW_MESSAGE`. Your token stays in TintaView's settings; the
  agent only sends the text.
- **After updating, run the setup wizard once to switch it on**: tray → **Settings…** →
  **Open Full Setup Wizard (Terminal)…**. Its new **Notify tool** step shows what it adds to
  each agent's config and asks first. That's `~/.claude.json` and `~/.claude/settings.json`
  for Claude Code, `~/.codex/config.toml` for Codex, and `~/.cursor/mcp.json` for Cursor.
  It also lets the tool run without asking for approval, since it is called when you may
  not be there. Cursor may still ask the first time. Sessions already open don't see the
  tool; start a new one.
- `tintaview doctor` has a new **NOTIFY TOOL** section showing, per agent, whether the tool
  is set up.
- Uninstalling? Run `tintaview hooks uninstall --agent all` first. It now also removes the
  notify tool, which agents otherwise report as broken once TintaView is gone.

## 0.7.2 — 2026-09-29

- **A usage limit can now run your own command**, just like an unanswered question. Set it
  under **Settings… → Alerts → Warn me before a usage limit runs out**. It runs once each time
  a window crosses your threshold, with `TINTAVIEW_STATUS=limit`, `TINTAVIEW_LIMIT` (e.g.
  "5-hour limit"), `TINTAVIEW_PCT` and a ready-made `TINTAVIEW_MESSAGE`. It's a separate
  setting from the question command, so nothing new fires until you fill it in.
- Fixed: a Claude Code question could go unnoticed, with no reminder and no command, while a
  background subagent in the same session was still running tools.
- **After updating, run the setup wizard once** to get the subagent fix: tray → **Settings…** →
  **Open Full Setup Wizard (Terminal)…**, and go through it to the end. Nothing prompts you
  for this.

## 0.7.1 — 2026-09-25

- The tray popup is short again: it only says that an agent is waiting for your answer. The
  full question still goes to your reminder command (`TINTAVIEW_DETAIL`, `TINTAVIEW_MESSAGE`).
- Fixed: sending a chat message while Claude's question was open made the reminder say only
  "Claude needs your permission" instead of the question.

Nothing to reconfigure, and no hook reinstall needed.

## 0.7.0 — 2026-09-25

- **Reminders now quote exactly what the agent is asking** — the command itself, or every
  question with its options — instead of "Claude needs your permission to use Bash" or just a
  tool name. Covers Claude Code and Codex; Cursor is unchanged.
- **Your reminder command gets the whole request too:** `TINTAVIEW_DETAIL` (the full command or
  questions, on one line), `TINTAVIEW_TOOL` (which tool) and `TINTAVIEW_CWD` (which project).
  These can contain secrets from the command line — check what your command forwards.
- **Claude Code's multiple-choice questions now show as "waiting for you"**, with a
  notification, and permission prompts are flagged about 6 seconds sooner.
- **Windows:** double quotes in these variables become single quotes, so `cmd` can't run parts
  of an agent's command.
- **Cursor usage stays visible after its sign-in expires**, until the billing cycle resets,
  marked e.g. "Signed out — usage from 6d ago".
- **Every usage-panel section can collapse to its header**, including one that shows only an
  error.
- **After updating, run the setup wizard once** — tray → **Settings…** → **Open Full Setup
  Wizard (Terminal)…** — without it, reminders quote only the old short sentence. The tray
  reminds you if Claude Code is set up; with only Codex, nothing does.

## 0.6.1 — 2026-09-16

- Reminders now say what the agent is asking. "Claude Code — still waiting for your answer (1 min): Claude needs your permission to use Bash". Codex shows the tool it wants to run; Cursor has no such hook, so its reminders read as before.
- Your reminder command gets it too — TINTAVIEW_QUESTION and TINTAVIEW_MESSAGE (the finished sentence) join TINTAVIEW_AGENTS, so a Telegram or phone-push one-liner can just send $TINTAVIEW_MESSAGE.
- Fixed: a reminder interval under a minute always claimed "1 min"; it now reads "15 s", "30 s".

One thing to do after updating: run tintaview hooks install --agent all so the hook script on disk is the new one — without it, reminders work but quote nothing. Your agents' own config files don't change.

## 0.6.0 — 2026-09-16

- SteelSeries GameSense engine — and with it, lighting on macOS for the first time.
- Unanswered confirmations keep nagging — chime + notification every minute until you deal with it, plus an optional command of your own (phone push, webhook). On by default, Settings… → Alerts.
- Usage warnings — notification at 90% of a limit, and a "empties in ~40 min" line while you're burning through one.
- Notifications say "TintaView", not "Python" (Windows), and "Check for updates" no longer double-notifies.
- tintaview doctor --json — the whole report as one file to attach to a bug report.
- Source tarball slimmed 4.7 MB → 0.6 MB; the wheel is unchanged.

Nothing to reconfigure.

## 0.5.0 — 2026-09-08

World clocks in the usage panel. Up to four, off by default — switch them on in Settings → Clocks.

- Pick each clock by country, and by city where a country has more than one time zone.
- Label each one Poland/Warsaw or just Poland — a City tick box per clock.
- 24-hour (20:42) or 12-hour (8:42 PM), for all of them.
- Daylight saving is handled for you — nothing to change twice a year.

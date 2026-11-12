# Changelog

What changed for people using TintaView, newest first. Each version's section is also the
text of its [GitHub Release](https://github.com/GomelHawk/TintaView/releases). Releases
before 0.5.0 are described only there.

## 1.2.0 — 2026-11-12

- **Security fixes — please update.** A full review found ways for a web page, a downloaded
  project folder, or another user of the same computer to interfere with TintaView. All are
  closed. The two most serious: the "notify me" tool could run code from the folder an agent
  was working in, and on Linux an update could be made to install someone else's package.
- **After updating, run the setup wizard once** (**Settings… → Open Full Setup Wizard**, or
  `tintaview setup`) and accept the change it shows for the notify tool. Until you do, your
  agents keep using the old, unprotected way of starting it.
- **Updates are quick again.** The update to 1.1.0 downloaded the whole interface toolkit
  again. From now on an update replaces only TintaView itself, unless the toolkit is being
  updated too — and the release notes will say when it is. A fresh install is also about
  half the size.
- **The installer and the update message say what is happening**: that components are being
  installed, that it can take several minutes, and that the window should stay open.
- **Notifications from agents are limited** to a few in quick succession, and an agent you
  switched off in Settings can no longer send them.
- **On Windows, a value ending in `\` in a reminder or notify command** now has a space added
  after it, so it cannot break the quotes around it.
- If you read TintaView's status from your own scripts: `http://127.0.0.1:8777/state` no longer
  includes what an agent is asking (the command, the question, the folder). Statuses and
  session names are unchanged.

Updating from 1.1.0 is small. Updating from an older version downloads the interface toolkit
once more (over 100 MB), so give it a few minutes.

## 1.1.0 — 2026-11-12

- **Linux and macOS now install the way Windows does.** The installer works on a stock Ubuntu
  (if a Python package is missing it names it, and repairs a failed earlier attempt), adds
  TintaView to your applications menu (Linux) or `~/Applications` (macOS) with its icon, runs
  the setup wizard, and starts TintaView. Running it again updates and restarts TintaView,
  also when you install over SSH. New `--no-wizard` option to skip the wizard.
- **A left click on the tray icon opens the usage panel on Ubuntu and macOS too**, right next
  to the icon; a right click opens the menu, as on Windows. On Ubuntu this needs a few X11
  libraries — the installer offers to install them (`apt`, `dnf` or `pacman`). Without them a
  double click on the icon opens the panel.
- **Settings… → Open Full Setup Wizard** now opens a terminal window on Linux and macOS, and
  no longer closes TintaView.
- **The network line shows the city on more systems.** On some machines it showed only the
  country.
- **A waiting question is no longer lost** when an agent sends several events in quick
  succession, so the lights reliably turn red when an agent needs you.
- **A web page can no longer control TintaView.** A page open in your browser could stop
  TintaView or send a notification through it (which runs your notify command). TintaView now
  refuses every request that comes from a browser. Nothing to change — hooks and the
  `notify_user` tool work as before.
- **Your settings are readable only by you** on Linux and macOS: `config.toml`, the logs and
  the usage cache. A notify or reminder command often holds a token.
- **`install.sh --uninstall` removes only TintaView's own files** under `--prefix`, never the
  whole folder.
- **Installing and updating now use the exact versions of TintaView's dependencies that the
  release was tested with**, each checked against a checksum, starting with this release. If
  they don't support your Python yet, the installer says so and installs the newest ones as
  before.

On Ubuntu, to get the left click: run the install command once more in a terminal and accept
the X11 libraries it offers.

## 1.0.0 — 2026-11-03

- **The usage panel's network line now shows your city too**, when it can be worked out: for
  example "PL Toruń, Poland" beside your IP address. The lookup now goes to ipinfo.io first; if
  ipinfo doesn't answer, TintaView asks Cloudflare as before and shows the country only. The
  city is estimated from your IP address, so on a mobile network or a VPN it may be a nearby
  city or missing. Nothing to change: the line is still switched on with **Settings… →
  General → Show public IP address and country in the usage panel**.

## 0.12.1 — 2026-11-01

- **Cursor usage works again on Windows.** Recent Cursor versions encrypt the login they save
  on your computer, so the Cursor card said "Signed out" and kept showing old figures, even
  with Cursor open and signed in. TintaView now unlocks that saved login the same way Cursor
  does, and the card shows your current usage again. You don't need to sign in again or change
  anything.
- The same works on Linux (with the GNOME keyring or KWallet) and on macOS. On a Mac, the first
  time it happens, macOS may ask whether TintaView may use "Cursor Safe Storage" — choose
  **Always Allow**. If the login can't be unlocked, the card says so instead of wrongly saying
  you're signed out.

## 0.12.0 — 2026-10-28

- **A sound when an agent sends you a message.** When an agent sends a "notify me when it's
  done" message, TintaView can now play a sound: the system sound, or a WAV, OGG or MP3 file
  of your own at its own volume, separate from the confirmation sound. It's off by default.
  Turn it on with **Sound on notification** in the tray menu, or in **Settings… → Sound**.
- **The tray icon shows when a message is waiting.** A small speech bubble appears on the
  icon when an agent's message arrives, so you can see one came in after the pop-up is gone.
  It goes away when you open the list under the bell in the usage panel.

## 0.11.0 — 2026-10-26

- **Junie CLI: a question or an approval now turns the light red.** When Junie asks you a
  question, or stops for your approval of an action, the tray and your lights go red, and a
  "notify" message carries the question with its options. It clears a few seconds after you
  answer. What an approval dialog is asking is sometimes sparse, because Junie doesn't always
  say. The "waiting for you in …" message now names your project folder, not `~/.junie`.
- **The JetBrains AI Assistant card now shows the real balance again, with Junie CLI's
  spend counted in it.** PhpStorm 2026.3 stopped updating the file TintaView read, so the
  card kept showing last month's figures (for example 0.13 credits used and a renewal that had
  already passed). It now reads the balance PhpStorm keeps current, so it matches the IDE: for
  example "0.64 / 10.00 used · 14 Nov" and "44.99 credits available". Junie CLI uses the same
  account, so what it has spent since the IDE last updated (Windows and WSL together, only
  sessions billed to your JetBrains account) is added to the same figure, which shows a `~`
  while it includes that estimate. If the IDE's figures are from before the current period,
  a note says to open an IDE's AI Assistant to refresh them.
- **The tray now notices hook entries that are out of date**, not only missing ones, so a
  later TintaView version that changes what it writes gets offered to you at startup. For
  Junie this applies to anyone who set it up on 0.10.0: you'll be offered the update, and
  **Settings… → Open Full Setup Wizard** (or `tintaview setup`) shows the change and asks
  before it writes anything.

## 0.10.0 — 2026-10-26

- **Junie CLI is now supported.** The tray and your lights follow a Junie CLI session
  (working, idle, started, ended), and "notify me when it's done" works there too. Junie
  can't be seen waiting for your approval or a question: it would approve the action on its
  own if TintaView listened for that, so the light stays on "working" while Junie waits for
  you. A Junie running inside a JetBrains IDE isn't seen at all yet, because the IDE sends
  no hooks. Tick **Junie CLI** in **Settings… → General** and run `tintaview setup` to turn
  it on.
- **Cursor on Windows with a project in WSL now reports in.** Its hooks used to be written
  only inside WSL, where Cursor on Windows never looks, so it never lit anything up. Setup
  now writes them into your Windows profile as well, and registers the notify tool there.
  Junie CLI gets the same treatment when it's installed on both Windows and WSL.
  `tintaview doctor` reports each side separately.
- **If you already use Cursor on Windows with WSL**, re-run `tintaview setup` once to add
  the Windows-side hooks and the notify tool; the tray doesn't do it for you.

## 0.9.5 — 2026-10-25

- **Cursor's on-demand spending in the usage panel.** A new **On-Demand** row under
  "Other Models" shows what you've spent beyond your plan this billing cycle. With a
  monthly limit set, it reads like Cursor's own usage page, for example "$1.45 / $50.00",
  with a bar that turns amber and then red as you near the limit. With an unlimited limit,
  it shows only the amount spent, for example "$1.45 spent". The row isn't shown for team
  spending limits yet.

## 0.9.4 — 2026-10-22

- **Your public IP address and country in the usage panel**, on a line under the clocks:
  for example, "[UA] Ukraine" with the address beside it. It is off by default: turn it on
  in **Settings… → General → Show public IP address and country in the usage panel**. It
  refreshes each time you open the panel and shows "Updating…" until the answer arrives.
  The address comes from Cloudflare, and nothing is fetched while the panel is closed.

## 0.9.3 — 2026-10-12

- **The cost estimate in the usage panel now knows Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5.**
  Before, sessions on these models were counted at a generic rate, so the "~$" figure read
  too high: about 1.25× for Opus 5.5, 2.5× for Sonnet 5.5 and far more for Haiku 5.5. Long
  Haiku 5.5 prompts (over 100K tokens) are still estimated at its lower short-prompt rate.
- The gear and bell icons in the usage panel's title bar are slightly smaller and lighter, so
  they match the close button.

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

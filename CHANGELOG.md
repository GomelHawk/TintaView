# Changelog

What changed for people using TintaView, newest first. Each version's section is also the
text of its [GitHub Release](https://github.com/GomelHawk/TintaView/releases). Releases
before 0.5.0 are described only there.

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
- **After updating, run `tintaview hooks install --agent all` once** (or `tintaview setup`) —
  without it, reminders quote only the old short sentence. The tray reminds you if Claude Code
  is set up; with only Codex, nothing does.

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

; TintaView — Inno Setup script.
;
; Build with (from a Windows machine, after `pyinstaller packaging\windows\tintaview.spec`
; has produced dist\TintaView\ from the repo root):
;
;     iscc packaging\windows\tintaview.iss /DMyAppVersion=1.2.3
;
; `/DMyAppVersion` is how CI (.github/workflows/build.yml) substitutes the version from the
; git tag; omit it for a local dev build and it defaults to "0.0.0-dev" below.
;
; Locked decisions this script encodes (see docs/PLAN.md §0, §8.3):
;   - No code signing. This WILL trip Windows SmartScreen ("Windows protected your PC")
;     on first run for every user — that is accepted, not a bug. Tell users: click
;     "More info", then "Run anyway". Do not try to work around SmartScreen here; that
;     needs a paid code-signing certificate, which is explicitly out of scope for now.
;   - Per-user install, no admin rights: DefaultDirName lives under {localappdata} and
;     PrivilegesRequired=lowest, so a non-technical user double-clicking this needs no
;     elevation prompt at all.
;   - One autostart entry (a Startup-folder shortcut), not a Scheduled Task — see the
;     [Icons] section. This mirrors exactly what `tintaview.install.autostart`'s Windows
;     backend does at runtime for `tintaview setup --reconfigure`, so whichever one last
;     wrote the shortcut wins; there is never a second, competing entry.
;   - Silent install (/SILENT, /VERYSILENT) must work end to end with no prompts, because
;     `tintaview update` on Windows re-runs this Setup.exe with /SILENT to self-update.

#define MyAppName "TintaView"
#define MyAppPublisher "TintaView"
#define MyAppURL "https://github.com/GomelHawk/TintaView"
#define MyAppExeName "TintaView.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
; Fixed forever — Inno (and any update logic that inspects the uninstall registry key)
; identifies "this app" by AppId, so it must never change across releases.
AppId={{0CD8F697-1D4E-4AB9-AC38-7C93554A6446}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}

; Per-user, no-admin install — the whole point is a colleague double-clicking this with
; no IT involvement. {localappdata} needs no elevation and is writable by any user.
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Explicitly left enabled: the wizard's "choose install folder" page is a requirement,
; not just Inno's default.
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; Let a silent re-install (the self-updater) close and overwrite a currently-running
; TintaView.exe instead of failing with a file-in-use error. It is not relaunched here —
; `tintaview update` relaunches the new build itself once Setup.exe returns.
CloseApplications=yes
RestartApplications=no

ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\..\tintaview\assets\generated\tintaview.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

OutputDir=..\..\dist\installer
OutputBaseFilename=TintaView-Setup-{#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Checked by default: a status tray that isn't running defeats the point of installing
; it. Untick during an interactive install, or pass /TASKS="" (or a filtered list)
; to a silent one, to opt out.
Name: "autostart"; Description: "Start {#MyAppName} when I sign in"

[Files]
; Everything PyInstaller's onedir build produced — the exe plus its _internal/ payload
; (assets/generated/*, tintaview/hooks/*, the PySide6 runtime, etc; see tintaview.spec).
Source: "..\..\dist\TintaView\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
; The one and only autostart entry (per docs/PLAN.md §0: no Scheduled Task). {userstartup}
; is the per-user Startup folder — no admin needed, and it is exactly where
; tintaview.install.autostart's own Windows backend points, so the two mechanisms never
; disagree about where the shortcut lives.
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
; Lands the user straight in the configuration wizard (agents, engine, hooks) right after
; install — this is what turns "files got copied" into "TintaView is actually set up".
; `skipifsilent` is what makes the self-updater's /SILENT re-install NOT reopen the wizard
; on every version bump.
Filename: "{app}\{#MyAppExeName}"; Parameters: "setup"; \
    Description: "Run the {#MyAppName} setup wizard now"; \
    Flags: postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Inno only ever deletes the files/shortcuts it tracked from [Files]/[Icons] above, and
    only removes {app} itself if it ends up empty — config.toml, hook.env, bin\tv-hook.cmd
    and logs\ all live under {app} too (see tintaview.core.config.config_dir(), which
    resolves to this same %LOCALAPPDATA%\TintaView folder) but were written by the app at
    runtime, not by this installer, so none of that is ever a target of the uninstall.
    This message just makes that explicit, and reminds the user their agents still have
    hooks pointed at a TintaView that's no longer running — which is harmless (tv-hook.cmd
    fire-and-forgets a 1-second HTTP call and always exits 0) but worth cleaning up. }
  if CurUninstallStep = usPostUninstall then
  begin
    MsgBox(
      '{#MyAppName} has been removed.' + #13#10 + #13#10 +
      'Your configuration and usage cache were left in place at:' + #13#10 +
      ExpandConstant('{localappdata}\{#MyAppName}') + #13#10 + #13#10 +
      'Claude Code, Codex and/or Cursor may still have {#MyAppName}''s hooks configured. ' +
      'Those calls fail silently and harmlessly once nothing is listening, but to remove ' +
      'them cleanly, reinstall {#MyAppName} and run:' + #13#10 + #13#10 +
      '    {#MyAppExeName} hooks uninstall --agent all',
      mbInformation, MB_OK);
  end;
end;

# TintaView installer for Windows.
#
# Usage (the supported way -- no download prompt, no SmartScreen, no Smart App Control):
#
#     irm https://raw.githubusercontent.com/GomelHawk/TintaView/main/packaging/install.ps1 | iex
#
# To pass options through that one-liner, invoke it as a script block instead of piping
# into `iex` (which cannot forward arguments):
#
#     & ([scriptblock]::Create((irm https://raw.githubusercontent.com/GomelHawk/TintaView/main/packaging/install.ps1))) -NoAutostart
#
# ...or set the TINTAVIEW_* environment variables documented under `param` below, which
# every option also reads, precisely so the plain piped form stays usable.
#
# This is the Windows twin of packaging/install.sh and works the same way: it creates a
# private virtual environment under the install prefix and pip-installs TintaView into
# it. Re-running it (piped or local, same -Prefix) upgrades in place -- it IS the update
# mechanism, and `tintaview update` just downloads and re-runs it with -Silent.
#
# ---------------------------------------------------------------------------------------
# WHY A VENV AND NOT A COMPILED .exe (docs/PLAN.md SS8.3). Two separate Windows defences
# block unsigned software, and they need different answers:
#
#   1. Mark-of-the-Web. Browsers tag downloads with a Zone.Identifier stream; Edge/Chrome
#      block unsigned installers on reputation on the way in, and SmartScreen shows
#      "Windows protected your PC" on launch. Nothing PowerShell downloads goes through
#      the Attachment Execution Service, so no tag is written and neither check fires.
#      (Extracting a .zip in Explorer does NOT dodge this -- its extractor propagates the
#      zone into every file it writes.)
#
#   2. Smart App Control. On by default on clean Windows 11 installs, and MOTW is
#      irrelevant to it: SAC refuses to run any executable that is neither signed nor
#      cloud-reputable, no matter how it arrived. A PyInstaller bundle is fatally exposed
#      here, because every build produces a byte-unique binary that no reputation service
#      has ever seen -- and every future release rebuilds it, so it can never accumulate
#      reputation either. Measured on an enforcing machine: the PyInstaller TintaView.exe
#      was blocked (CodeIntegrity event 3118), while python.exe/pythonw.exe (PSF-signed,
#      signature preserved by venv), the pip console shim, and all of PySide6's DLLs ran
#      without complaint.
#
# Installing into a venv means the only executables involved are the signed interpreter
# and widely-mirrored PyPI wheels, so both defences are satisfied with no code-signing
# certificate. Do not "simplify" this back into a frozen bundle.
# ---------------------------------------------------------------------------------------
#
# THE ONE LAYOUT CONSTRAINT THIS FILE MUST NEVER BREAK: on Windows,
# `tintaview.core.config.config_dir()` resolves to %LOCALAPPDATA%\TintaView -- the *same*
# directory this installs into. config.toml, hook.env, bin\tv-hook.cmd and logs\ are
# therefore siblings of the venv. So the install prefix is never deleted wholesale, not on
# update and not on uninstall; only the `venv` subdirectory, which this script owns
# outright, is ever removed.

[CmdletBinding()]
param(
    # Install location. Defaults to %LOCALAPPDATA%\TintaView, which is also where
    # tintaview.core.config.config_dir() looks -- see the header. Env: TINTAVIEW_PREFIX
    [string] $Prefix,

    # Version to install, e.g. "1.2.3". Defaults to the latest GitHub release.
    # Env: TINTAVIEW_VERSION
    [string] $Version,

    # Install from a locally built wheel instead of downloading (offline / CI smoke test).
    # Skips the release lookup and the SHA-256 check, since there is nothing to verify
    # against. Env: TINTAVIEW_WHEEL
    [string] $WheelPath,

    # Use this python.exe instead of searching. Must be 3.11 or newer.
    # Env: TINTAVIEW_PYTHON
    [string] $Python,

    # Do not create the "start when I sign in" Startup shortcut on a fresh install.
    # An upgrade never changes an existing autostart choice either way. Env: TINTAVIEW_NO_AUTOSTART
    [switch] $NoAutostart,

    # Do not launch the setup wizard when the install finishes. Env: TINTAVIEW_NO_WIZARD
    [switch] $NoWizard,

    # Non-interactive: no wizard, no prompts, minimal output. Used by `tintaview update`.
    # Env: TINTAVIEW_SILENT
    [switch] $Silent,

    # Remove the virtual environment, shortcuts and autostart entry. Config, hooks and
    # logs under the prefix are deliberately left in place -- see the notice printed out.
    [switch] $Uninstall,

    [switch] $Help
)

$ErrorActionPreference = 'Stop'
# Invoke-WebRequest's progress bar is not cosmetic here: in Windows PowerShell 5.1 it
# repaints per chunk and can make a download several times slower than it needs to be.
# Restored in the finally block at the very bottom.
$script:PreviousProgressPreference = $ProgressPreference
$ProgressPreference = 'SilentlyContinue'

$AppName      = 'TintaView'
$GitHubRepo   = 'GomelHawk/TintaView'
$ManifestName = '.tintaview-install.json'
$UserAgent    = 'TintaView-installer'
$MinPython    = [Version]'3.11'

# --------------------------------------------------------------------------- output

function Write-Info { param([string] $Message) if (-not $Silent) { Write-Host "==> $Message" } }
function Write-Note { param([string] $Message) if (-not $Silent) { Write-Host "    $Message" } }
function Write-Warn { param([string] $Message) Write-Warning $Message }
function Die       { param([string] $Message) throw $Message }

function Show-Usage {
    Write-Host @"
Usage: install.ps1 [-Prefix DIR] [-Version X.Y.Z] [-WheelPath FILE] [-Python EXE]
                   [-NoAutostart] [-NoWizard] [-Silent] [-Uninstall]

  -Prefix DIR      Install location (default: %LOCALAPPDATA%\$AppName)
  -Version X.Y.Z   Install a specific release instead of the latest
  -WheelPath FILE  Install from a local wheel (no download, no checksum)
  -Python EXE      Use this interpreter (default: newest found, must be >= $MinPython)
  -NoAutostart     Do not register a login autostart entry (fresh installs only)
  -NoWizard        Do not launch the setup wizard afterwards
  -Silent          Non-interactive: implies -NoWizard, minimal output
  -Uninstall       Remove the venv, shortcuts and autostart (config is kept)

Environment equivalents, for use with the piped ``irm ... | iex`` form:
  TINTAVIEW_PREFIX, TINTAVIEW_VERSION, TINTAVIEW_WHEEL, TINTAVIEW_PYTHON,
  TINTAVIEW_NO_AUTOSTART, TINTAVIEW_NO_WIZARD, TINTAVIEW_SILENT
"@
}

# --------------------------------------------------------------------------- options

# Switches bound explicitly on the command line always win; the environment is only
# consulted for options the caller did not pass, which is what makes the piped
# `irm | iex` form (where no arguments can be supplied at all) configurable.
function Test-EnvFlag {
    param([string] $Name)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $false }
    return @('1', 'true', 'yes', 'on') -contains $value.Trim().ToLowerInvariant()
}

function Resolve-Switch {
    param([bool] $Bound, [string] $EnvName)
    if ($Bound) { return $true }
    return (Test-EnvFlag $EnvName)
}

if ($Help -or (Test-EnvFlag 'TINTAVIEW_HELP')) { Show-Usage; return }

if (-not $Prefix)    { $Prefix    = $env:TINTAVIEW_PREFIX }
if (-not $Version)   { $Version   = $env:TINTAVIEW_VERSION }
if (-not $WheelPath) { $WheelPath = $env:TINTAVIEW_WHEEL }
if (-not $Python)    { $Python    = $env:TINTAVIEW_PYTHON }

$NoAutostart = Resolve-Switch $NoAutostart.IsPresent 'TINTAVIEW_NO_AUTOSTART'
$NoWizard    = Resolve-Switch $NoWizard.IsPresent    'TINTAVIEW_NO_WIZARD'
$Silent      = Resolve-Switch $Silent.IsPresent      'TINTAVIEW_SILENT'
$Uninstall   = Resolve-Switch $Uninstall.IsPresent   'TINTAVIEW_UNINSTALL'
if ($Silent) { $NoWizard = $true }

if (-not $Prefix) {
    $localAppData = $env:LOCALAPPDATA
    if (-not $localAppData) { $localAppData = Join-Path $env:USERPROFILE 'AppData\Local' }
    $Prefix = Join-Path $localAppData $AppName
}

$VenvDir      = Join-Path $Prefix 'venv'
$VenvScripts  = Join-Path $VenvDir 'Scripts'
$VenvPython   = Join-Path $VenvScripts 'python.exe'
$VenvPythonW  = Join-Path $VenvScripts 'pythonw.exe'
$LauncherPath = Join-Path $VenvScripts 'tintaview.exe'
$ManifestPath = Join-Path $Prefix $ManifestName

# --------------------------------------------------------------------------- preflight

function Assert-Supported {
    # $IsWindows only exists on PowerShell 6+; on 5.1 its absence *is* the answer, since
    # Windows PowerShell has never run anywhere else.
    if ((Get-Variable -Name IsWindows -ErrorAction SilentlyContinue) -and -not $IsWindows) {
        Die "$AppName's PowerShell installer only runs on Windows. On Linux/macOS use packaging/install.sh."
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Die "PowerShell 5.1 or newer is required (found $($PSVersionTable.PSVersion)). Windows 10 and 11 ship 5.1."
    }
}

# --------------------------------------------------------------------------- python

function Get-PythonVersion {
    param([string] $Exe)
    # Ask the interpreter itself rather than parsing `--version` banners, which have
    # changed format before and go to stderr on some old builds.
    try {
        $raw = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try { return [Version]($raw.Trim()) } catch { return $null }
}

function Find-Python {
    if ($Python) {
        if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
            Die "-Python was set to '$Python', which is not an executable this shell can find."
        }
        $version = Get-PythonVersion $Python
        if (-not $version) { Die "'$Python' did not report a usable Python version." }
        if ($version -lt $MinPython) {
            Die "'$Python' is Python $version; $AppName needs $MinPython or newer."
        }
        return @{ Exe = (Get-Command $Python).Source; Version = $version }
    }

    # The `py` launcher first and newest-first: it knows about every registered install,
    # including ones deliberately kept off PATH, which `Get-Command python` cannot see.
    # Note the Microsoft Store's python.exe stub also answers on PATH and does nothing
    # useful, so a PATH hit is only accepted after it reports a real version above.
    $candidates = New-Object System.Collections.Generic.List[string]
    if (Get-Command 'py' -ErrorAction SilentlyContinue) {
        try {
            foreach ($line in (& py -0p 2>$null)) {
                # Lines look like: " -V:3.12 *        C:\Python312\python.exe"
                if ($line -match '([A-Za-z]:\\[^\s].*python\.exe)\s*$') {
                    $candidates.Add($matches[1].Trim())
                }
            }
        } catch { }
    }
    foreach ($name in @('python3', 'python')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { $candidates.Add($cmd.Source) }
    }

    $best = $null
    foreach ($exe in $candidates) {
        if (-not (Test-Path -LiteralPath $exe)) { continue }
        $version = Get-PythonVersion $exe
        if (-not $version -or $version -lt $MinPython) { continue }
        if (-not $best -or $version -gt $best.Version) {
            $best = @{ Exe = $exe; Version = $version }
        }
    }

    if (-not $best) {
        Die @"
No Python $MinPython or newer was found, and $AppName needs one.

Install it, then re-run this script:

    winget install --id Python.Python.3.12 --exact --source winget

(or download it from https://www.python.org/downloads/windows/ -- either installer is
signed by the Python Software Foundation, so Smart App Control and SmartScreen are happy
with it). Open a new terminal afterwards so PATH picks it up.
"@
    }
    return $best
}

# --------------------------------------------------------------------------- manifest

function Read-Manifest {
    if (-not (Test-Path -LiteralPath $ManifestPath)) { return $null }
    try {
        return Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warn "Ignoring an unreadable $ManifestName ($($_.Exception.Message))."
        return $null
    }
}

# --------------------------------------------------------------------------- shortcuts

function Get-StartMenuShortcutPath {
    $appData = $env:APPDATA
    if (-not $appData) { $appData = Join-Path $env:USERPROFILE 'AppData\Roaming' }
    return Join-Path $appData "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
}

# Autostart is a per-user Run key value, NOT a Startup-folder shortcut. Windows 11 refuses
# any .lnk written into the Startup folder -- through WScript.Shell or copied in from
# elsewhere, on a machine with Controlled Folder Access off and no ASR rules configured --
# because shortcut-as-autorun is a persistence technique it blocks outright. The key name
# and value name below must stay identical to _WINDOWS_RUN_KEY/_WINDOWS_RUN_VALUE in
# tintaview/install/autostart.py, so this script and the app's own autostart.enable()
# always address the same entry and can never leave two competing ones behind.
$RunKey       = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunValueName = $AppName

function Test-AutostartEnabled {
    $item = Get-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
    return $null -ne $item
}

function Enable-Autostart {
    # One command *string*, quoted the way Windows parses it: %LOCALAPPDATA% contains the
    # user's name, which is very often a path with a space in it.
    $value = '"{0}" -m tintaview' -f $VenvPythonW
    Set-ItemProperty -Path $RunKey -Name $RunValueName -Value $value
}

function Disable-Autostart {
    Remove-ItemProperty -Path $RunKey -Name $RunValueName -ErrorAction SilentlyContinue
    # Clean up the Startup shortcut older installs used, so nothing launches twice.
    $appData = $env:APPDATA
    if (-not $appData) { $appData = Join-Path $env:USERPROFILE 'AppData\Roaming' }
    $legacy = Join-Path $appData "Microsoft\Windows\Start Menu\Programs\Startup\$AppName.lnk"
    if (Test-Path -LiteralPath $legacy) { Remove-Item -LiteralPath $legacy -Force -ErrorAction SilentlyContinue }
}

function New-Shortcut {
    param(
        [string] $Path, [string] $Target, [string] $Arguments,
        [string] $WorkingDirectory, [string] $IconLocation
    )
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $shell = New-Object -ComObject WScript.Shell
    try {
        $shortcut = $shell.CreateShortcut($Path)
        $shortcut.TargetPath       = $Target
        $shortcut.Arguments        = $Arguments
        $shortcut.WorkingDirectory = $WorkingDirectory
        $shortcut.Description      = "$AppName - agent status lighting and usage tray"
        # Without this the shortcut inherits the icon of whatever it launches -- and it
        # launches pythonw.exe, so the Start Menu would show TintaView as a Python file.
        if ($IconLocation) { $shortcut.IconLocation = $IconLocation }
        $shortcut.Save()
    } finally {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

function Get-AppIconPath {
    # tintaview.ico is package data inside the wheel, so its location depends on the
    # interpreter's site-packages layout -- ask the package rather than guessing. Imports
    # only `tintaview` itself, never tintaview.ui, so this cannot pull in Qt.
    #
    # -I (isolated) on every query below is load-bearing, not hygiene: `python -c` puts the
    # *current directory* at the front of sys.path, so running the installer from a
    # TintaView checkout imports the source tree instead of the venv. That silently pins
    # the Start Menu icon to the checkout -- which on a WSL clone is a \\wsl.localhost
    # UNC path that breaks the moment the distro is not running.
    $path = (& $VenvPython -I -c "import pathlib, tintaview; print(pathlib.Path(tintaview.__file__).resolve().parent / 'assets' / 'generated' / 'tintaview.ico')" 2>$null)
    if ($LASTEXITCODE -ne 0 -or -not $path) { return '' }
    $path = $path.Trim()
    if (Test-Path -LiteralPath $path) { return $path }
    return ''
}

function New-AppShortcut {
    param([string] $Path)
    # pythonw.exe, not the tintaview.exe console shim: the windowed interpreter shows no
    # console window, and it keeps the PSF Authenticode signature that Smart App Control
    # wants to see. This mirrors autostart._executable_command() exactly.
    New-Shortcut -Path $Path -Target $VenvPythonW -Arguments '-m tintaview' `
        -WorkingDirectory $Prefix -IconLocation (Get-AppIconPath)
}

# --------------------------------------------------------------------------- running instance

function Stop-RunningApp {
    # Only interpreters running out of *this* prefix's venv are stopped. A developer's
    # checkout run, or another install elsewhere, is none of this script's business.
    $names = @('pythonw', 'python', 'tintaview')
    $running = @(Get-Process -Name $names -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and ($_.Path -like (Join-Path $VenvDir '*')) } catch { $false }
    })
    if ($running.Count -eq 0) { return }

    Write-Info "Stopping the running $AppName ($($running.Count) process(es)) so its files can be replaced"
    foreach ($proc in $running) { try { $proc.CloseMainWindow() | Out-Null } catch { } }
    foreach ($proc in $running) {
        try { $proc.WaitForExit(3000) | Out-Null } catch { }
        if (-not $proc.HasExited) {
            try { $proc.Kill(); $proc.WaitForExit(5000) | Out-Null } catch { }
        }
    }
}

# --------------------------------------------------------------------------- uninstall

function Invoke-Uninstall {
    if (-not (Test-Path -LiteralPath $Prefix)) {
        Die "Nothing to uninstall: $Prefix does not exist."
    }

    Stop-RunningApp

    Write-Info 'Removing the Start Menu shortcut and the autostart entry'
    $startMenu = Get-StartMenuShortcutPath
    if (Test-Path -LiteralPath $startMenu) { Remove-Item -LiteralPath $startMenu -Force -ErrorAction SilentlyContinue }
    Disable-Autostart

    if (Test-Path -LiteralPath $VenvDir) {
        # Safe to delete recursively, unlike $Prefix: the venv directory is created and
        # owned entirely by this script, and nothing of the user's is ever written inside
        # it (config.toml, hook.env, bin\ and logs\ are all siblings of it, not children).
        Write-Info "Removing the virtual environment at $VenvDir"
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Warn "No virtual environment found at $VenvDir -- nothing to remove there."
    }
    Remove-Item -LiteralPath $ManifestPath -Force -ErrorAction SilentlyContinue

    Write-Host @"

$AppName has been uninstalled from $Prefix.

Your configuration, usage cache and logs were left in place on purpose -- on Windows they
live in that same folder (config.toml, hook.env, bin\, logs\). Delete it by hand if you
want them gone too.

Claude Code, Codex and/or Cursor may still have $AppName's hooks configured. Those calls
fail silently and harmlessly once nothing is listening, but to remove them cleanly, run
this BEFORE uninstalling next time, from a working install:

    tintaview hooks uninstall --agent all

"@
}

# --------------------------------------------------------------------------- download

function Initialize-Tls {
    # Windows PowerShell 5.1 still negotiates SSL3/TLS1.0 by default on some machines, and
    # github.com has required TLS 1.2 for years -- without this the download fails with a
    # bewildering "underlying connection was closed" error. Additive, so a host already
    # configured for TLS 1.3 is not downgraded.
    try {
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        Write-Warn "Could not force TLS 1.2 ($($_.Exception.Message)); the download may fail on older Windows builds."
    }
}

function Get-LatestVersion {
    Write-Info "Looking up the latest $AppName release"
    $url = "https://api.github.com/repos/$GitHubRepo/releases/latest"
    try {
        $release = Invoke-RestMethod -Uri $url -Headers @{ 'User-Agent' = $UserAgent; 'Accept' = 'application/vnd.github+json' } -UseBasicParsing
    } catch {
        Die "Could not reach the GitHub Releases API ($($_.Exception.Message)). Check your connection, or pass -Version to install a specific release."
    }
    $tag = [string] $release.tag_name
    if (-not $tag) { Die "The latest GitHub release has no version tag -- nothing to install." }
    return $tag.TrimStart('v', 'V')
}

function Save-File {
    param([string] $Uri, [string] $OutFile)
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $OutFile -Headers @{ 'User-Agent' = $UserAgent } -UseBasicParsing
    } catch {
        Die "Download failed for $Uri -- $($_.Exception.Message)"
    }
}

function Assert-Checksum {
    param([string] $FilePath, [string] $ChecksumsPath, [string] $FileName)
    $expected = $null
    foreach ($line in (Get-Content -LiteralPath $ChecksumsPath)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        # sha256sum's two output forms: "<hex>  name" (text) and "<hex> *name" (binary).
        $parts = $trimmed -split '\s+'
        if ($parts.Count -lt 2) { continue }
        if ($parts[-1].TrimStart('*') -eq $FileName) { $expected = $parts[0]; break }
    }
    if (-not $expected) {
        Remove-Item -LiteralPath $FilePath -Force -ErrorAction SilentlyContinue
        Die "No checksum for $FileName in the release's SHA256SUMS.txt -- refusing to install an unverified download."
    }

    $actual = (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash
    if ($actual -ne $expected.ToUpperInvariant()) {
        Remove-Item -LiteralPath $FilePath -Force -ErrorAction SilentlyContinue
        Die "SHA-256 mismatch for ${FileName}: expected $expected, got $actual. The download has been deleted -- an unverified build is never installed."
    }
    Write-Info "SHA-256 verified for $FileName"
}

# --------------------------------------------------------------------------- install

function Invoke-Install {
    Assert-Supported

    $python = Find-Python
    Write-Info "Using Python $($python.Version) ($($python.Exe))"

    $workDir = Join-Path ([IO.Path]::GetTempPath()) ("tintaview-install-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null

    try {
        # ------------------------------------------------------------- acquire the wheel
        if ($WheelPath) {
            if (-not (Test-Path -LiteralPath $WheelPath)) { Die "-WheelPath does not exist: $WheelPath" }
            $wheel = (Resolve-Path -LiteralPath $WheelPath).Path
            Write-Info "Installing from the local wheel at $wheel (no checksum to verify against)"
            if (-not $Version) { $Version = 'local' }
        } else {
            Initialize-Tls
            if (-not $Version) { $Version = Get-LatestVersion }
            $Version = $Version.TrimStart('v', 'V')

            # PEP 427: the distribution part of a wheel filename uses underscores, so a
            # project named with a dash would not round-trip. "tintaview" has neither, but
            # normalising here means a future rename cannot silently break the URL.
            $wheelName = ("{0}-{1}-py3-none-any.whl" -f $AppName.ToLowerInvariant().Replace('-', '_'), $Version)
            $sumsName  = 'SHA256SUMS.txt'
            $baseUrl   = "https://github.com/$GitHubRepo/releases/download/v$Version"
            $wheel     = Join-Path $workDir $wheelName
            $sumsLocal = Join-Path $workDir $sumsName

            Write-Info "Downloading $wheelName"
            Save-File -Uri "$baseUrl/$wheelName" -OutFile $wheel
            Save-File -Uri "$baseUrl/$sumsName"  -OutFile $sumsLocal
            Assert-Checksum -FilePath $wheel -ChecksumsPath $sumsLocal -FileName $wheelName
        }

        # ------------------------------------------------------------- venv
        $previous  = Read-Manifest
        $isUpgrade = ($null -ne $previous) -and (Test-Path -LiteralPath $VenvPython)

        Stop-RunningApp

        if (-not (Test-Path -LiteralPath $Prefix)) {
            New-Item -ItemType Directory -Path $Prefix -Force | Out-Null
        }

        if (Test-Path -LiteralPath $VenvPython) {
            Write-Info "Reusing the existing virtual environment at $VenvDir"
        } else {
            # A half-built venv (an interrupted previous run) has no usable python.exe and
            # cannot be repaired by `venv` itself, so start it over rather than inherit it.
            if (Test-Path -LiteralPath $VenvDir) {
                Write-Warn "The virtual environment at $VenvDir is incomplete; recreating it."
                Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            Write-Info "Creating a virtual environment at $VenvDir"
            & $python.Exe -m venv $VenvDir
            if ($LASTEXITCODE -ne 0) { Die "Could not create a virtual environment at $VenvDir (python -m venv exited $LASTEXITCODE)." }
        }
        if (-not (Test-Path -LiteralPath $VenvPythonW)) {
            Die "The virtual environment has no pythonw.exe, which autostart and the shortcuts need. Is '$($python.Exe)' a full Python install rather than a stub?"
        }

        # ------------------------------------------------------------- install
        Write-Info "Installing $AppName $Version and its dependencies -- this takes a minute the first time"
        & $VenvPython -m pip install --quiet --upgrade pip
        if ($LASTEXITCODE -ne 0) { Write-Warn "Could not upgrade pip inside the venv; continuing with the bundled version." }

        # Both extras: [ui] is the tray, [openrgb] is the second lighting engine. The
        # openrgb extra used to be left out, which made the engine permanently
        # unavailable no matter what the user configured -- and the failure surfaced as
        # "the SDK server isn't answering", sending people to restart software that was
        # never the problem. openrgb-python is small and pure Python, so there is no
        # reason to make it opt-in.
        & $VenvPython -m pip install --quiet --upgrade "${wheel}[ui,openrgb]"
        if ($LASTEXITCODE -ne 0) { Die "pip failed to install $AppName (exit $LASTEXITCODE). Re-run without -Silent to see the full output." }

        # ...and then plant the wheel's own code unconditionally. `--upgrade` compares
        # version numbers and does nothing when they match, so without this a re-run does
        # not repair a damaged install, and any release that reuses a version string (a
        # re-tag, or a dev build) silently leaves the previous code in place while
        # reporting success. `--no-deps` keeps it to the one small local wheel, so the
        # dependency resolution above is not repeated.
        & $VenvPython -m pip install --quiet --force-reinstall --no-deps $wheel
        if ($LASTEXITCODE -ne 0) { Die "pip failed to install $AppName (exit $LASTEXITCODE). Re-run without -Silent to see the full output." }

        $installedVersion = (& $VenvPython -I -c "import tintaview; print(tintaview.__version__)" 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $installedVersion) {
            Die "$AppName was installed but cannot be imported from the new virtual environment."
        }
        $installedVersion = $installedVersion.Trim()

        @{
            app       = $AppName
            version   = $installedVersion
            installer = 'install.ps1'
            python    = $python.Exe
            venv      = $VenvDir
        } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8

        # ------------------------------------------------------------- shortcuts
        Write-Info 'Creating the Start Menu shortcut'
        New-AppShortcut -Path (Get-StartMenuShortcutPath)

        if ($isUpgrade) {
            # An upgrade must never silently turn autostart back on for someone who turned
            # it off, nor off for someone who has it on: only refresh what is already there.
            if (Test-AutostartEnabled) {
                Enable-Autostart
                Write-Note 'Autostart entry refreshed'
            } else {
                Write-Note 'Autostart is off and was left off (turn it on with: tintaview setup --reconfigure)'
            }
        } elseif ($NoAutostart) {
            Write-Note 'Skipping autostart (-NoAutostart)'
        } else {
            Write-Info "Registering autostart (starts $AppName when you sign in)"
            Enable-Autostart
        }

        # ------------------------------------------------------------- wizard + launch
        if ($NoWizard) {
            if (-not $Silent) { Write-Note 'Skipping the setup wizard (-NoWizard)' }
        } else {
            Write-Info 'Launching the setup wizard'
            # The wizard is a text-mode flow reading stdin, so it needs a console of its
            # own -- console python.exe here, not the windowed pythonw.exe the tray uses.
            # Waited on, because the tray started afterwards reads the config it writes.
            $wizard = Start-Process -FilePath $VenvPython -ArgumentList '-m', 'tintaview', 'setup' `
                -WorkingDirectory $Prefix -Wait -PassThru
            if ($wizard.ExitCode -ne 0) {
                Write-Warn "The setup wizard exited with code $($wizard.ExitCode). Re-run it any time with: $LauncherPath setup"
            }
        }

        # Autostart only takes effect at the *next* sign-in, so without this the install
        # finishes with no tray icon and nothing to indicate anything is wrong -- which
        # reads exactly like a broken install. Start it now instead.
        Write-Info "Starting $AppName"
        Start-Process -FilePath $VenvPythonW -ArgumentList '-m', 'tintaview' -WorkingDirectory $Prefix | Out-Null
        Start-Sleep -Seconds 3
        $alive = @(Get-Process -Name 'pythonw' -ErrorAction SilentlyContinue | Where-Object {
            try { $_.Path -and ($_.Path -like (Join-Path $VenvDir '*')) } catch { $false }
        })
        if ($alive.Count -eq 0) {
            # The overwhelmingly likely cause is that something else already holds the
            # configured port: TintaView treats a taken port as "another instance owns
            # it" and exits. `doctor` distinguishes the two cases and names the culprit.
            Write-Warn "$AppName started but exited immediately. This usually means another program is using its port. Run this to find out: $LauncherPath doctor"
        } else {
            Write-Note "Running (PID $($alive[0].Id)). The tray icon may be under the '^' arrow in the taskbar until you drag it out."
        }

        if (-not $Silent) {
            # Ask the installed app where its config lives rather than assuming $Prefix.
            # The two coincide at the default prefix and *only* there: config_dir() is
            # %LOCALAPPDATA%\TintaView (or $TINTAVIEW_HOME) regardless of where the app
            # was installed, so a custom -Prefix would otherwise be told the wrong path.
            $configDir = (& $VenvPython -I -c "from tintaview.core.config import config_dir; print(config_dir())" 2>$null)
            if ($LASTEXITCODE -ne 0 -or -not $configDir) { $configDir = $Prefix } else { $configDir = $configDir.Trim() }
            Write-Host @"

$AppName $installedVersion is installed at $Prefix.
  Launcher:  $LauncherPath
  Config:    $(Join-Path $configDir 'config.toml')  (written by 'tintaview setup')
  Logs:      $(Join-Path $configDir 'logs')

Next: finish the setup wizard to choose your agents, lighting engine and hooks.
Run it any time with:  $LauncherPath setup
Update later with '$LauncherPath update', or by re-running this script.
"@
        }
    } finally {
        Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --------------------------------------------------------------------------- entry point

try {
    if ($Uninstall) { Invoke-Uninstall } else { Invoke-Install }
} catch {
    # Reduce the failure to one readable line for the piped `irm | iex` case, then let it
    # terminate anyway so that `powershell -File install.ps1` still exits non-zero -- which
    # is what tintaview.install.update reads to decide whether the update succeeded.
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    throw
} finally {
    $ProgressPreference = $script:PreviousProgressPreference
}

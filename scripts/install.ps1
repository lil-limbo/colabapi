<#
.SYNOPSIS
    Installs colabapi on Windows (PowerShell or CMD).

.DESCRIPTION
    One-liner (pinned to a released tag, so what runs is what was reviewed):

      irm https://raw.githubusercontent.com/lil-limbo/colabapi/v0.2.3/scripts/install.ps1 | iex

    (The same script on `main` also works, but the tag is the recommended URL.)

    Installs colabapi with pipx (isolated venv, the recommended way to install a
    Python CLI), makes sure the install directory is actually on PATH, and
    registers colabapi with Windows so it shows up in Settings -> Installed apps
    and runs from the Start menu / Win+R.

    If no Python 3.12+ is found, offers to install Python via winget (with your
    consent) instead of dead-ending, then continues by itself.

    Nothing here needs administrator rights: everything is a per-user install.

.NOTES
    Google's official Colab CLI does not support Windows. colabapi ships a
    compatibility layer (see colabapi/_winshim.py) that supplies the two POSIX
    modules it is missing, so it runs here unmodified.
#>

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "!!! $msg" -ForegroundColor Red }

# --- helpers ------------------------------------------------------------------

function Test-StoreStub($path) {
    # A clean Windows box ships a fake python.exe in WindowsApps (an "App
    # Execution Alias") that just opens the Microsoft Store. It must never be
    # treated as a Python. The version probe below also rejects it (it prints
    # nothing), but recognise it explicitly rather than by accident.
    return ($path -and $path -like '*\Microsoft\WindowsApps\*')
}

function Find-Python {
    # Prefer the `py` launcher: it is PATH-independent, always real (never the
    # Store stub), and knows every registered install.
    foreach ($candidate in @('py', 'python', 'python3')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if (Test-StoreStub $cmd.Source) {
            Write-Warn "$candidate resolves to the Microsoft Store stub -- skipping"
            continue
        }
        $ver = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
        $parts = $ver.Trim().Split('.')
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
            Write-Ok "Found Python $ver ($candidate)"
            return $candidate
        }
        Write-Warn "$candidate is Python $ver -- too old (need 3.12+)"
    }
    return $null
}

function Update-PathFromRegistry {
    # A winget install updates the *registry* PATH; this process's $env:PATH is
    # a stale copy taken at startup. Rebuild it so the fresh python.exe (and
    # the py launcher) are findable without closing the window.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:PATH = "$machine;$user;$env:PATH"
}

function Show-ManualPythonHelp {
    Write-Host ""
    Write-Host "Install Python 3.12+ yourself, then re-run this script:" -ForegroundColor White
    Write-Host "    winget install Python.Python.3.13" -ForegroundColor White
    Write-Host "  or download from https://www.python.org/downloads/windows/" -ForegroundColor White
    Write-Host ""
    Write-Host "If you install from python.org, tick 'Add python.exe to PATH'." -ForegroundColor White
}

# --- 1. Find a usable Python ------------------------------------------------
# colabapi needs 3.12+, because Google's google-colab-cli requires it.
Write-Step "Looking for Python 3.12+"

$python = Find-Python

if (-not $python) {
    Write-Warn "Python 3.12+ is required and was not found."

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    # Under `irm | iex` in an interactive console Read-Host still works; only a
    # genuinely non-interactive stdin (CI, a redirected script) must not prompt,
    # because Read-Host would hang there.
    $canAsk = -not [Console]::IsInputRedirected

    if ($winget -and $canAsk) {
        $answer = Read-Host "Install Python 3.13 automatically with winget? [Y/n]"
        if ($answer -eq '' -or $answer -match '^[Yy]') {
            Write-Step "Installing Python 3.13 with winget"
            & winget install --id Python.Python.3.13 -e --source winget --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) {
                Write-Err "winget could not install Python (exit code $LASTEXITCODE)."
                Show-ManualPythonHelp
                exit 1
            }
            # The new install is on the registry PATH but not this process's.
            Update-PathFromRegistry
            $python = Find-Python
            if (-not $python) {
                Write-Err "Python was installed but could not be found on PATH yet."
                Write-Host "Close this window, open a new one, and re-run this script." -ForegroundColor White
                exit 1
            }
        } else {
            Show-ManualPythonHelp
            exit 1
        }
    } else {
        if (-not $winget) { Write-Warn "winget is not available on this machine, so Python cannot be installed automatically." }
        Show-ManualPythonHelp
        exit 1
    }
}

# --- 2. Make sure pipx is available -----------------------------------------
# pipx puts each CLI in its own venv, so colabapi cannot break (or be broken by)
# anything else installed with pip.
Write-Step "Checking for pipx"

# The executable and its leading arguments are kept apart on purpose. The
# previous version stored both in one array and re-sliced it at the call site
# with `$pipx[1..($pipx.Count-1)]` -- but on a 1-element array that slice is
# `[1..0]`, and PowerShell evaluates 1..0 as the DESCENDING range @(1, 0), not
# an empty one. The result was `pipx pipx install ...`, which pipx rejects, so
# the install failed on every machine that already had pipx. Two variables and
# a plain concatenation have no such edge case.
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Warn "pipx not found -- installing it"
    & $python -m pip install --user --quiet --upgrade pipx
    if ($LASTEXITCODE -ne 0) { Write-Err "Could not install pipx."; exit 1 }
    # ensurepath adds pipx's bin dir to the *user* PATH, but only for future
    # shells, so we also add it to this process below.
    & $python -m pipx ensurepath 2>$null | Out-Null
    $pipxCmd = $python
    $pipxArgs = @('-m', 'pipx')
} else {
    Write-Ok "pipx is installed"
    $pipxCmd = 'pipx'
    $pipxArgs = @()
}

# --- 3. Install colabapi ----------------------------------------------------
Write-Step "Installing colabapi"

& $pipxCmd @($pipxArgs + @('install', '--force', 'colabapi'))
if ($LASTEXITCODE -ne 0) { Write-Err "Install failed."; exit 1 }
Write-Ok "Installed"

# --- 4. Put it on PATH for THIS shell too -----------------------------------
# pipx ensurepath only affects new shells. Without this, the user installs
# successfully and then gets "colabapi is not recognized" in the very same
# window, which reads as a broken install.
$pipxBin = Join-Path $env:USERPROFILE '.local\bin'
if (Test-Path $pipxBin) {
    if ($env:PATH -notlike "*$pipxBin*") {
        $env:PATH = "$pipxBin;$env:PATH"
        Write-Ok "Added $pipxBin to PATH for this session"
    }
}

# --- 5. Register with Windows -----------------------------------------------
# Makes colabapi a real installed app: visible in Settings -> Installed apps
# with its own icon, launchable from the Start menu (opens the colabapi
# window), uninstallable the normal way.
Write-Step "Registering with Windows"
# NOTE: a native exe that fails does NOT throw in PowerShell, so try/catch
# would never fire here; the exit code is the only truth.
& colabapi register
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Could not register (colabapi still works). Run 'colabapi register' later."
} else {
    Write-Ok "Registered"
}

# --- 6. Verify --------------------------------------------------------------
Write-Step "Verifying"
if (Get-Command colabapi -ErrorAction SilentlyContinue) {
    & colabapi --version
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "colabapi is installed but did not start cleanly. Run 'colabapi doctor' for details."
    }
    Write-Host ""
    Write-Host "colabapi is ready." -ForegroundColor Green
    Write-Host ""
    Write-Host "  colabapi login     " -NoNewline -ForegroundColor White
    Write-Host "# sign in with Google (opens your browser)" -ForegroundColor DarkGray
    Write-Host "  colabapi run       " -NoNewline -ForegroundColor White
    Write-Host "# pick a runtime (CPU / T4 GPU / ...)" -ForegroundColor DarkGray
    Write-Host "  colabapi shell     " -NoNewline -ForegroundColor White
    Write-Host "# terminal into the Colab VM" -ForegroundColor DarkGray
    Write-Host "  colabapi ui        " -NoNewline -ForegroundColor White
    Write-Host "# or use the graphical window (also in the Start menu)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "If 'colabapi' is not found in a NEW window, close and reopen it" -ForegroundColor DarkGray
    Write-Host "so PATH refreshes." -ForegroundColor DarkGray
} else {
    Write-Warn "Installed, but 'colabapi' is not on PATH in this shell."
    Write-Host "Open a new PowerShell window, or run: $pipxBin\colabapi.exe doctor"
}

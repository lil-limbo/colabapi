<#
.SYNOPSIS
    Installs colabapi on Windows (PowerShell or CMD).

.DESCRIPTION
    One-liner:

      irm https://raw.githubusercontent.com/lil-limbo/colabapi/main/scripts/install.ps1 | iex

    Installs colabapi with pipx (isolated venv, the recommended way to install a
    Python CLI), makes sure the install directory is actually on PATH, and
    registers colabapi with Windows so it shows up in Settings -> Installed apps
    and runs from the Start menu / Win+R.

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

# --- 1. Find a usable Python ------------------------------------------------
# colabapi needs 3.12+, because Google's google-colab-cli requires it.
Write-Step "Looking for Python 3.12+"

$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
        $ver = & $candidate -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { continue }
    if (-not $ver) { continue }
    $parts = $ver.Trim().Split('.')
    if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 12) {
        $python = $candidate
        Write-Ok "Found Python $ver ($candidate)"
        break
    }
    Write-Warn "$candidate is Python $ver -- too old (need 3.12+)"
}

if (-not $python) {
    Write-Err "No Python 3.12+ found."
    Write-Host ""
    Write-Host "Install it, then re-run this script:" -ForegroundColor White
    Write-Host "    winget install Python.Python.3.13" -ForegroundColor White
    Write-Host "  or download from https://www.python.org/downloads/windows/" -ForegroundColor White
    Write-Host ""
    Write-Host "If you install from python.org, tick 'Add python.exe to PATH'." -ForegroundColor White
    exit 1
}

# --- 2. Make sure pipx is available -----------------------------------------
# pipx puts each CLI in its own venv, so colabapi cannot break (or be broken by)
# anything else installed with pip.
Write-Step "Checking for pipx"

if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Warn "pipx not found -- installing it"
    & $python -m pip install --user --quiet --upgrade pipx
    if ($LASTEXITCODE -ne 0) { Write-Err "Could not install pipx."; exit 1 }
    # ensurepath adds pipx's bin dir to the *user* PATH, but only for future
    # shells, so we also add it to this process below.
    & $python -m pipx ensurepath 2>$null | Out-Null
    $pipx = @($python, '-m', 'pipx')
} else {
    Write-Ok "pipx is installed"
    $pipx = @('pipx')
}

# --- 3. Install colabapi ----------------------------------------------------
Write-Step "Installing colabapi"

& $pipx[0] @($pipx[1..($pipx.Count-1)] + @('install', '--force', 'colabapi'))
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
# Makes colabapi a real installed app: visible in Settings -> Installed apps,
# launchable from Win+R and the Start menu, uninstallable the normal way.
Write-Step "Registering with Windows"
try {
    & colabapi register
    Write-Ok "Registered"
} catch {
    Write-Warn "Could not register (colabapi still works). Run 'colabapi register' later."
}

# --- 6. Verify --------------------------------------------------------------
Write-Step "Verifying"
if (Get-Command colabapi -ErrorAction SilentlyContinue) {
    & colabapi --version
    Write-Host ""
    Write-Host "colabapi is ready." -ForegroundColor Green
    Write-Host ""
    Write-Host "  colabapi login     " -NoNewline -ForegroundColor White
    Write-Host "# sign in with Google (opens your browser)" -ForegroundColor DarkGray
    Write-Host "  colabapi run       " -NoNewline -ForegroundColor White
    Write-Host "# pick a runtime (CPU / T4 GPU / ...)" -ForegroundColor DarkGray
    Write-Host "  colabapi shell     " -NoNewline -ForegroundColor White
    Write-Host "# terminal into the Colab VM" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "If 'colabapi' is not found in a NEW window, close and reopen it" -ForegroundColor DarkGray
    Write-Host "so PATH refreshes." -ForegroundColor DarkGray
} else {
    Write-Warn "Installed, but 'colabapi' is not on PATH in this shell."
    Write-Host "Open a new PowerShell window, or run: $pipxBin\colabapi.exe doctor"
}

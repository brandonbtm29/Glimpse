<#
.SYNOPSIS
  Install or update Glimpse on Windows. Run by double-clicking "Install Glimpse.cmd".

.DESCRIPTION
  No administrator rights needed. Safe to run again at any time (that's how you update):
    1. Finds Python 3.11+ (offers to install it with winget if missing)
    2. Stops a running Glimpse from this folder, then installs/updates it in .venv
    3. Creates config.yaml + .env on first run (your settings are never overwritten)
    4. Starts Glimpse automatically when you sign in, adds "Glimpse settings" to the Start menu
    5. Starts Glimpse and opens the settings panel in your browser
#>
param([switch]$NoOpen)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

function Say($msg) { Write-Host "  $msg" }
function Step($n, $msg) { Write-Host ""; Write-Host "[$n/5] $msg" -ForegroundColor Cyan }

Write-Host ""
Write-Host "  Glimpse installer" -ForegroundColor White
Write-Host "  Folder: $Root" -ForegroundColor DarkGray

# --- 1. Python -------------------------------------------------------------------------------
Step 1 "Checking for Python 3.11 or newer"

function Test-Python($cmd, [string[]]$pre) {
    try {
        $exe = & $cmd @pre -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
        if ($exe -and (Test-Path $exe) -and ($exe -notmatch "WindowsApps")) { return $exe }
    } catch { }
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $found = Test-Python "py" @("-3")
        if ($found) { return $found }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $found = Test-Python "python" @()
        if ($found) { return $found }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "Python isn't installed (or is older than 3.11)."
        $answer = Read-Host "  Install Python 3.12 for your account now? [Y/n]"
        if ($answer -eq "" -or $answer -match "^[Yy]") {
            winget install --exact --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
            $py = Find-Python
        }
    }
    if (-not $py) {
        Say "Please install Python 3.11 or newer from python.org (tick 'Add python.exe to PATH'), then run this installer again."
        Start-Process "https://www.python.org/downloads/windows/"
        exit 1
    }
}
Say "Using $py"

# --- 2. Install / update -----------------------------------------------------------------------
Step 2 "Installing Glimpse"

$venv = Join-Path $Root ".venv"
# Stop any Glimpse running from this folder so its files can be updated.
$running = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
    Say "Stopping the running copy so it can be updated..."
    $running | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $py -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Couldn't create the Python environment (.venv)." }
}
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPy -m pip install -e $Root --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "Installing Glimpse's Python packages failed (see the messages above)." }
$glimpse = Join-Path $venv "Scripts\glimpse.exe"
Say "Installed."

# --- 3. Settings -----------------------------------------------------------------------------
Step 3 "Settings"
$config = Join-Path $Root "config.yaml"
if (Test-Path $config) {
    Say "Keeping your existing config.yaml and .env."
} else {
    & $glimpse -c $config init --setup-later | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Couldn't create config.yaml." }
    Say "Created config.yaml and .env. You'll choose a password in the browser."
}
$envFile = Join-Path $Root ".env"
if (Test-Path $envFile) {
    # Only your Windows account can read the file that holds your calendar links.
    icacls $envFile /inheritance:r /grant:r "$($env:USERNAME):(F)" | Out-Null
}

# --- 4. Start automatically ------------------------------------------------------------------------
Step 4 "Starting automatically when you sign in"
& $glimpse -c $config autostart on
Say "Start menu: Glimpse > Glimpse settings"

# --- 5. Start and open ------------------------------------------------------------------------
Step 5 "Starting Glimpse"
if ($NoOpen) {
    Start-Process -FilePath (Join-Path $venv "Scripts\glimpse-service.exe") -ArgumentList @("-c", "`"$config`"") -WorkingDirectory $Root -WindowStyle Hidden
    Say "Started in the background."
} else {
    & $glimpse -c $config open
}

Write-Host ""
Write-Host "  Done. Glimpse is running in the background." -ForegroundColor Green
Say "Settings: Start menu > Glimpse > Glimpse settings   (or http://127.0.0.1:8001)"
Say "Logs:     $Root\logs\glimpse.log"
Say "To update later: get the new version into this folder and run Install Glimpse again."

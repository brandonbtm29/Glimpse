<#
.SYNOPSIS
  One-time setup of Glimpse on Windows.

.DESCRIPTION
  Creates a virtual environment in .venv, installs Glimpse, and (on first run)
  creates config.yaml + .env, asking you for a settings-panel password.
  Safe to run again: it never overwrites your config.

  Run from the repository folder:
      powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
#>
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $Root

function Find-Python {
    foreach ($cmd in @("py -3", "python")) {
        try {
            $v = & ([scriptblock]::Create("$cmd -c `"import sys; print(sys.version_info >= (3, 11))`"")) 2>$null
            if ($v -eq "True") { return $cmd }
        } catch { }
    }
    throw "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')."
}

$py = Find-Python
Write-Host "Using $py"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment (.venv)..."
    & ([scriptblock]::Create("$py -m venv .venv"))
}
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
$glimpse = Join-Path $Root ".venv\Scripts\glimpse.exe"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e . --quiet
Write-Host "Installed Glimpse into .venv"

if (-not (Test-Path "config.yaml")) {
    & $glimpse init
    if ($LASTEXITCODE -ne 0) { throw "glimpse init failed" }
} else {
    Write-Host "config.yaml already exists; keeping it."
}

# Lock .env down to the current user only.
if (Test-Path ".env") {
    icacls ".env" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. scripts\windows\tailscale-setup.ps1   (public links + settings panel on your tailnet)"
Write-Host "  2. scripts\windows\install-task.ps1      (start Glimpse with Windows; run as administrator)"
Write-Host "  3. Open the settings panel and add your calendars."

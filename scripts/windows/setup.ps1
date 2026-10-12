<#
.SYNOPSIS
  One-time setup of Glimpse on Windows.

.DESCRIPTION
  Creates a virtual environment in .venv, installs Glimpse, copies the example
  config files (without overwriting yours), and fills in a fresh secret key.

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
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e . --quiet
Write-Host "Installed Glimpse into .venv"

if (-not (Test-Path "config.yaml")) {
    Copy-Item "config.example.yaml" "config.yaml"
    Write-Host "Created config.yaml (edit it to match your calendars)"
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    $secret = & (Join-Path $Root ".venv\Scripts\glimpse.exe") new-secret
    (Get-Content ".env") -replace '^GLIMPSE_SECRET_KEY=.*$', "GLIMPSE_SECRET_KEY=$secret" | Set-Content ".env" -Encoding utf8
    Write-Host "Created .env with a fresh secret key (add your feed links next)"
}

# Lock .env down to the current user only.
icacls ".env" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Paste your calendar feed links into .env"
Write-Host "  2. .venv\Scripts\glimpse new-slug       (once per audience, paste into .env)"
Write-Host "  3. .venv\Scripts\glimpse hash-password  (for each password-protected audience)"
Write-Host "  4. .venv\Scripts\glimpse check"
Write-Host "  5. .venv\Scripts\glimpse preview --audience coworkers"
Write-Host "  6. scripts\windows\install-task.ps1     (start Glimpse automatically)"

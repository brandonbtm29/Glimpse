<#
.SYNOPSIS
  Stop Kalends and stop it starting automatically. Your files and settings stay in this folder.

  powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1
#>
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venv = Join-Path $Root ".venv"
$kalends = Join-Path $venv "Scripts\kalends.exe"
if (-not (Test-Path $kalends)) { $kalends = Join-Path $venv "Scripts\glimpse.exe" }  # installed before the rename

if (Test-Path $kalends) { & $kalends -c (Join-Path $Root "config.yaml") autostart off }
# Start menu folders, under the current name and the old one (Glimpse)
foreach ($name in "Kalends", "Glimpse") {
    Remove-Item -Recurse -Force (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$name") -ErrorAction SilentlyContinue
}

# Start-before-login uses a Task Scheduler task; remove it if present (needs admin if it was created as admin).
foreach ($task in "Kalends", "Glimpse") {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    }
}

Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Kalends is stopped and won't start automatically."
Write-Host "Tailscale settings are unchanged; to stop public links run:  tailscale funnel --https=443 off"
Write-Host "To remove it completely, delete this folder: $Root"

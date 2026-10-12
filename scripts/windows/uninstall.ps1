<#
.SYNOPSIS
  Stop Glimpse and stop it starting automatically. Your files and settings stay in this folder.

  powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1
#>
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$venv = Join-Path $Root ".venv"
$glimpse = Join-Path $venv "Scripts\glimpse.exe"

if (Test-Path $glimpse) { & $glimpse -c (Join-Path $Root "config.yaml") autostart off }
Remove-Item -Recurse -Force (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Glimpse") -ErrorAction SilentlyContinue

# Older versions used a Task Scheduler task; remove it if present (needs admin if it was created as admin).
if (Get-ScheduledTask -TaskName "Glimpse" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "Glimpse" -Confirm:$false -ErrorAction SilentlyContinue
}

Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Glimpse is stopped and won't start automatically."
Write-Host "Tailscale settings are unchanged; to stop public links run:  tailscale funnel --https=443 off"
Write-Host "To remove it completely, delete this folder: $Root"

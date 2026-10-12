<#
.SYNOPSIS
  Start Glimpse automatically whenever Windows starts, and restart it if it stops.

.DESCRIPTION
  Registers a Task Scheduler task named "Glimpse" that runs `glimpse serve`
  at startup as YOUR user (no admin rights needed to run it, no password stored),
  even when nobody is logged in. Output goes to logs\glimpse.log.

  Run from an elevated PowerShell (Run as administrator), in the repo folder:
      powershell -ExecutionPolicy Bypass -File scripts\windows\install-task.ps1
#>
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Exe = Join-Path $Root ".venv\Scripts\glimpse.exe"
if (-not (Test-Path $Exe)) { throw "Run scripts\windows\setup.ps1 first ($Exe not found)." }
if (-not (Test-Path (Join-Path $Root "config.yaml"))) { throw "config.yaml not found in $Root." }

# Fail early if the config is broken, rather than at the next reboot.
& $Exe -c (Join-Path $Root "config.yaml") check
if ($LASTEXITCODE -eq 2) { throw "Fix config.yaml first (see the error above)." }

New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
$log = Join-Path $Root "logs\glimpse.log"
$cmd = "`"$Exe`" -c `"$Root\config.yaml`" serve >> `"$log`" 2>&1"

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd" -WorkingDirectory $Root
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Glimpse" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "Glimpse"
Start-Sleep -Seconds 4

try {
    $health = Invoke-RestMethod "http://127.0.0.1:8000/healthz" -TimeoutSec 5
    Write-Host "Glimpse is running: $($health.feeds_ok)/$($health.feeds_total) feeds OK"
} catch {
    Write-Warning "Task registered, but Glimpse did not answer yet. Check $log"
}
Write-Host "It will now start automatically with Windows. Remove it with scripts\windows\uninstall-task.ps1"

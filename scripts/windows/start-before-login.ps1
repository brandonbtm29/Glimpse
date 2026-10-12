<#
.SYNOPSIS
  Optional, advanced: run Glimpse at boot even before anyone signs in to Windows.

.DESCRIPTION
  The normal installer starts Glimpse when you sign in, which needs no admin rights.
  If this PC sometimes restarts (e.g. for updates) and sits at the sign-in screen,
  this registers a Task Scheduler task that starts Glimpse at boot instead.
  Needs an administrator PowerShell. Run "Install Glimpse" first.

      powershell -ExecutionPolicy Bypass -File scripts\windows\start-before-login.ps1
#>
$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs an administrator PowerShell: right-click PowerShell > Run as administrator, then run it again." -ForegroundColor Yellow
    exit 1
}
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$svc = Join-Path $Root ".venv\Scripts\glimpse-service.exe"
$config = Join-Path $Root "config.yaml"
if (-not (Test-Path $svc)) { throw "Run 'Install Glimpse' first ($svc not found)." }

$action    = New-ScheduledTaskAction -Execute $svc -Argument "-c `"$config`"" -WorkingDirectory $Root
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "Glimpse" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Done: Glimpse will also start at boot, before sign-in." -ForegroundColor Green
Write-Host "(When you sign in, the normal auto-start sees it's already running and does nothing.)"
Write-Host "Undo with: Unregister-ScheduledTask -TaskName Glimpse"

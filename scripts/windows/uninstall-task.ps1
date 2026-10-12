<#
.SYNOPSIS
  Stop Glimpse and remove its startup task.
#>
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName "Glimpse" -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName "Glimpse" -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "Glimpse" -Confirm:$false
    Write-Host "Removed the Glimpse startup task."
} else {
    Write-Host "No Glimpse task found."
}
# cmd.exe /c leaves the Python process running briefly; stop any stragglers.
Get-CimInstance Win32_Process -Filter "Name = 'glimpse.exe'" | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

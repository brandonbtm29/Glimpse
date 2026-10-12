<#
.SYNOPSIS
  Put Glimpse on Tailscale: public share links via Funnel, settings panel tailnet-only.

.DESCRIPTION
  - Funnel (public internet)  : https://<this-pc>.<tailnet>.ts.net        -> 127.0.0.1:8000  (share links)
  - Serve  (your devices only): https://<this-pc>.<tailnet>.ts.net:8443   -> 127.0.0.1:8001  (settings panel)
  Also writes the public address into config.yaml so share links are correct.
  Both settings survive reboots (they're stored by Tailscale, --bg).

  Run from the repository folder:
      powershell -ExecutionPolicy Bypass -File scripts\windows\tailscale-setup.ps1
#>
param(
    [int]$PublicPort = 8000,
    [int]$AdminPort = 8001,
    [int]$AdminHttpsPort = 8443
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$glimpse = Join-Path $Root ".venv\Scripts\glimpse.exe"

$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }
if (-not (Test-Path $ts)) { throw "Tailscale CLI not found. Install Tailscale from https://tailscale.com/download and sign in." }

$status = & $ts status --json | ConvertFrom-Json
if ($status.BackendState -ne "Running") { throw "Tailscale isn't connected. Open Tailscale and sign in, then run this again." }
$dns = $status.Self.DNSName.TrimEnd(".")
if (-not $dns) { throw "Couldn't read this PC's Tailscale name. Is MagicDNS turned on in the Tailscale admin console?" }

Write-Host "This PC on Tailscale: $dns"
Write-Host "Turning on Funnel for the public share links (approve in the browser if asked)..."
& $ts funnel --bg $PublicPort
if ($LASTEXITCODE -ne 0) { throw "tailscale funnel failed. Funnel may need enabling in the Tailscale admin console." }

Write-Host "Serving the settings panel to your tailnet only..."
& $ts serve --bg --https=$AdminHttpsPort "http://127.0.0.1:$AdminPort"
if ($LASTEXITCODE -ne 0) { throw "tailscale serve failed." }

if (Test-Path $glimpse) {
    & $glimpse -c (Join-Path $Root "config.yaml") set-base-url "https://$dns"
}

Write-Host ""
Write-Host "Done."
Write-Host "  Share links (public):          https://$dns/a/<link>"
Write-Host "  Settings panel (your devices): https://${dns}:$AdminHttpsPort"
Write-Host "  On this PC you can also use:   http://127.0.0.1:$AdminPort"
Write-Host ""
Write-Host "Check with: tailscale funnel status"

# One-time machine setup for the bots station - the only step that needs admin.
# Run it from the deployed station folder (<runner>\_work\Bots_Station) after the first
# Update Bots Version run, in an elevated PowerShell:
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup-station.ps1
#
# Firewall rules match the exe by full path, and the build is always swapped into the same
# steam.build folder, so the rule outlives every build update.

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this from an elevated (Administrator) PowerShell."
    exit 1
}

# Must match the rule name run-bots.ps1 checks for.
$fwRule  = "Underdogs Bot Test"
$exePath = Join-Path $PSScriptRoot "steam.build\Underdogs.exe"

netsh advfirewall firewall delete rule name="$fwRule" | Out-Null
netsh advfirewall firewall add rule name="$fwRule" dir=in  action=allow program="$exePath" enable=yes
netsh advfirewall firewall add rule name="$fwRule" dir=out action=allow program="$exePath" enable=yes

Write-Host "Firewall rule '$fwRule' allows $exePath"

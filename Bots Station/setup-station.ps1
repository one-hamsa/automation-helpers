# One-time machine setup for the bots station - the only step that needs admin.
# Creates the firewall rule for the bots' exe.
#
# HOW TO RUN (once per machine, after the first Update Bots Version run has created
# <runner folder>\_work\Bots_Station):
#   1. Start menu -> type "PowerShell" -> right-click "Windows PowerShell" -> "Run as administrator".
#   2. Run this, with <runner folder> replaced by the runner's install folder:
#        powershell -NoProfile -ExecutionPolicy Bypass -File "<runner folder>\_work\Bots_Station\setup-station.ps1"
#   3. The last line printed should be: Firewall rule 'Underdogs Bot Test' allows ...\Underdogs.exe
# Re-run it only if the runner folder moves.
#
# The other station scripts need no admin. To run one by hand, use a normal PowerShell
# (Windows blocks running .ps1 files directly by default; -ExecutionPolicy Bypass allows it
# for this one call):
#   powershell -NoProfile -ExecutionPolicy Bypass -File "<runner folder>\_work\Bots_Station\enable-bots.ps1"
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

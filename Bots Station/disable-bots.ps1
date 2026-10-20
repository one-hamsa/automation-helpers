# Disables the UnderdogsBotLoop scheduled task and closes every bot this station launched.
# enable-bots.ps1 turns it back on. Needs no admin.

$TaskName = "UnderdogsBotLoop"

$logDir = Join-Path $PSScriptRoot 'Logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'disable-bots.log') | Out-Null

#everything is in a try-catch, and will be logged into a log file incase of errors
try
{
Write-Host "=========================================================================================================================================================================================================="
Write-Host "============================================================================================== SCRIPT START =============================================================================================="
Write-Host "=========================================================================================================================================================================================================="

Write-Host ""
$now = Get-Date -Format "dd/MM/yyyy HH:mm:ss"
Write-Host "Script ran on: $now"
Write-Host ""

$buildDir     = Join-Path $PSScriptRoot "steam.build"
$localLow     = Join-Path $env:USERPROFILE "AppData\LocalLow\One Hamsa\UNDERDOGS"
$configPath   = Join-Path $localLow "BuildDebugConfigs.txt"
$botsDataFile = Join-Path $localLow "Bots_Local_Data.txt"

# Disable first so the trigger can't relaunch bots while they are being closed.
# Missing task = fresh machine, nothing to disable.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "Disabled '$TaskName'"
} else {
    Write-Host "'$TaskName' is not registered - nothing to disable."
}

# Clear the data-path debug config (delete it - an empty file still counts as a
# forced source to DebugConfigLoader, so removing it is the clean "clear").
if (Test-Path -LiteralPath $configPath) {
    Remove-Item -LiteralPath $configPath -Force
    Write-Host "Removed $configPath"
}

Write-Host "Closing all running bots"
Write-Host ""

# Kill only the bots THIS station launched (Underdogs.exe from our build folder).
Get-CimInstance Win32_Process -Filter "Name = 'Underdogs.exe'" |
    Where-Object { $_.ExecutablePath -like "$buildDir\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Reset the slot table: every claimed slot ("/ no") becomes available ("/ yes").
if (Test-Path -LiteralPath $botsDataFile) {
    Write-Host "Resetting bots availability in '$botsDataFile'..."
    (Get-Content -LiteralPath $botsDataFile) -replace '/\s*no\s*$', '/ yes' |
        Set-Content -LiteralPath $botsDataFile
} else {
    Write-Host "WARNING: '$botsDataFile' not found - skipping reset."
}

Write-Host "Bots closed. (Run enable-bots.ps1 to resume.)"

Write-Host "=========================================================================================================================================================================================================="
Write-Host "========================================================================================== SCRIPT END ===================================================================================================="
Write-Host "=========================================================================================================================================================================================================="
}
finally
{
Stop-Transcript | Out-Null
}

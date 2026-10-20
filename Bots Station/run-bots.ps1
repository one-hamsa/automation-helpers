
# Run by the UnderdogsBotLoop scheduled task (registered by register-tasks.ps1). Needs no admin:
# the firewall rule for the station's exe is created once by setup-station.ps1.

$logDir = Join-Path $PSScriptRoot 'Logs'
New-Item -ItemType Directory -Path (Join-Path $logDir 'Bots Logs') -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'run-bots.log') | Out-Null

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

$BuildDir      = Join-Path $PSScriptRoot "steam.build"

$ExeName       = "Underdogs.exe"

$InstanceCount = 5

$DebugConfigs = @(
    "Advanced/System/Force Bot",
    "Multiplayer/Levels/Block",
    "Advanced/Multiplayer/Bots Private Key"
) -join "`n"


$exePath      = Join-Path $BuildDir $ExeName
$localLow     = Join-Path $env:USERPROFILE "AppData\LocalLow\One Hamsa\UNDERDOGS"
$configPath   = Join-Path $localLow "BuildDebugConfigs.txt"
$botsDataFile = Join-Path $localLow "Bots_Local_Data.txt"

# The build came from a downloaded zip -> strip Mark-of-the-Web so it can launch.
Get-ChildItem -LiteralPath $BuildDir -Recurse -File -ErrorAction SilentlyContinue | Unblock-File

# Must match the rule name in setup-station.ps1.
$fwRule = "Underdogs Bot Test"
netsh advfirewall firewall show rule name="$fwRule" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Firewall rule '$fwRule' not found - run setup-station.ps1 once as admin on this machine."
}



Write-Host "Closing all running bots"
Write-Host ""

# Kill only the bots THIS station launched (Underdogs.exe from our build folder).
Get-CimInstance Win32_Process -Filter "Name = 'Underdogs.exe'" |
    Where-Object { $_.ExecutablePath -like "$BuildDir\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }


Write-Host "Resetting the bot slot table"
Write-Host ""


# Reset the slot table: every claimed slot ("/ no") becomes available ("/ yes").
if (Test-Path -LiteralPath $botsDataFile) {
    Write-Host "Resetting bots availability in '$botsDataFile'..."
    (Get-Content -LiteralPath $botsDataFile) -replace '/\s*no\s*$', '/ yes' |
        Set-Content -LiteralPath $botsDataFile
} else {
    Write-Host "WARNING: '$botsDataFile' not found - skipping reset, it will be created when the first bot starts."
}

Write-Host ""
Write-Host "Resetting the debug config file in the datapath"
Write-Host ""

# Always reset the debug-config file from scratch, even if it already has content.
if (-not (Test-Path $localLow)) { New-Item -ItemType Directory -Path $localLow -Force | Out-Null }
if (Test-Path -LiteralPath $configPath) { Remove-Item -LiteralPath $configPath -Force }
Set-Content -Path $configPath -Value $DebugConfigs -Encoding UTF8 -NoNewline

Write-Host ""
Write-Host " === LAUNCHING BOTS === "
Write-Host ""


if (Test-Path $exePath) {
    for ($i = 1; $i -le $InstanceCount; $i++) {
        Start-Sleep -Seconds 3

        $botLogPath = Join-Path $logDir "Bots Logs\bot_$i.log"
        $argList = "-batchmode -nographics -logFile `"$botLogPath`""

        # Capture the specific process instance
        $proc = Start-Process -FilePath $exePath `
            -ArgumentList $argList `
            -WorkingDirectory $BuildDir `
            -NoNewWindow `
            -PassThru

        Write-Host "Launched instance $i/$InstanceCount (PID: $($proc.Id))"
    }
} else {
    Write-Warning "No build at $exePath - nothing launched."
}
Write-Host "=========================================================================================================================================================================================================="
Write-Host "========================================================================================== SCRIPT END ===================================================================================================="
Write-Host "=========================================================================================================================================================================================================="

}
finally
{
Stop-Transcript | Out-Null
}

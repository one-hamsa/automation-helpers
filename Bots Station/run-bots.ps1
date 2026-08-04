
# Force the script to run as Administrator
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    Exit
}

$DontRun = $false


$logPath = Join-Path $PSScriptRoot 'Logs\run-bots.log'
Start-Transcript -Path $logPath | Out-Null

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
    "Game Modes\Quick Joint",
    "Advanced/Multiplayer/Bots Private Key"
) -join "`n"


$exePath      = Join-Path $BuildDir $ExeName
$localLow     = Join-Path $env:USERPROFILE "AppData\LocalLow\One Hamsa\UNDERDOGS"
$configPath   = Join-Path $localLow "BuildDebugConfigs.txt"
$botsDataFile = Join-Path $localLow "Bots_Local_Data.txt"

# The build came from a downloaded zip -> strip Mark-of-the-Web so it can launch.
Get-ChildItem -LiteralPath $BuildDir -Recurse -File -ErrorAction SilentlyContinue | Unblock-File

# The game needs network permission, so we elevate this script to admin and give the .exe the permissions
$fwRule  = "Underdogs Bot Test"

Write-Host "Providing the game with the needed network permissions"
Write-Host ""

netsh advfirewall firewall delete rule name="$fwRule" 2>`$null
netsh advfirewall firewall add rule name="$fwRule" dir=in  action=allow program="$exePath" enable=yes
netsh advfirewall firewall add rule name="$fwRule" dir=out action=allow program="$exePath" enable=yes



Write-Host "Closing all running bots"
Write-Host ""

# Kill only the bots THIS station launched (Underdogs.exe from our build folder).
Get-CimInstance Win32_Process -Filter "Name = 'Underdogs.exe'" |
    Where-Object { $_.ExecutablePath -like "$BuildDir\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }


if ($DontRun) 
{
Write-Host "THE BOTS DISPATCH IS CURRENTLY DISABLED, if you want to enable it - in this script(Desktop\Bots Workspace\run-bots.ps1) just flip the '$DontRun' flag at the top to '$false'"
return
}


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


# Launch fresh bots via cmd's `start` (Start-Process trips SmartScreen on a
# downloaded exe). 2s between launches to avoid file-lock conflicts.
if (Test-Path $exePath) {
    for ($i = 1; $i -le $InstanceCount; $i++) {
        Start-Sleep -Seconds 3
        Start-Process -FilePath $exePath -ArgumentList '-batchmode','-nographics', '-noaudio' -WorkingDirectory $BuildDir -NoNewWindow
        Write-Host "Launched instance $i/$InstanceCount"
    }
}
Write-Host "=========================================================================================================================================================================================================="
Write-Host "========================================================================================== SCRIPT END ===================================================================================================="
Write-Host "=========================================================================================================================================================================================================="

}
finally
{
Stop-Transcript | Out-Null
}
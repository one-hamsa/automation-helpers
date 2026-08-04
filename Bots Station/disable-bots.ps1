# Force the script to run as Administrator
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    Exit
}

$logPath = Join-Path $PSScriptRoot 'Logs\disable-bots.log'
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

$runBots      = Join-Path $PSScriptRoot "run-bots.ps1"
$buildDir     = Join-Path $PSScriptRoot "steam.build"
$localLow     = Join-Path $env:USERPROFILE "AppData\LocalLow\One Hamsa\UNDERDOGS"
$configPath   = Join-Path $localLow "BuildDebugConfigs.txt"
$botsDataFile = Join-Path $localLow "Bots_Local_Data.txt"

# Set $DontRun = $true in run-bots.ps1 so the scheduled task stops relaunching.
if (Test-Path -LiteralPath $runBots) {
    $content = Get-Content -LiteralPath $runBots -Raw
    $updated = $content -replace '(\$DontRun\s*=\s*)\$(?:true|false)', '$1$$true'
    if ($updated -ne $content) {
        Set-Content -LiteralPath $runBots -Value $updated -NoNewline
        Write-Host "Set `$DontRun = `$true in run-bots.ps1"
    } else {
        Write-Warning "No `$DontRun = `$true/`$false line found in run-bots.ps1 - flag unchanged."
    }
} else {
    Write-Warning "run-bots.ps1 not found at $runBots"
}

# Clear the data-path debug config (delete it — an empty file still counts as a
# forced source to DebugConfigLoader, so removing it is the clean "clear").
if (Test-Path -LiteralPath $configPath) {
    Remove-Item -LiteralPath $configPath -Force
    Write-Host "Removed $configPath"
}

Write-Host "Closing all running bots"
Write-Host ""

# Kill only the bots THIS station launched (Underdogs.exe from our build folder).
Get-CimInstance Win32_Process -Filter "Name = 'Underdogs.exe'" |
    Where-Object { $_.ExecutablePath -like "$BuildDir\*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Reset the slot table: every claimed slot ("/ no") becomes available ("/ yes").
if (Test-Path -LiteralPath $botsDataFile) {
    Write-Host "Resetting bots availability in '$botsDataFile'..."
    (Get-Content -LiteralPath $botsDataFile) -replace '/\s*no\s*$', '/ yes' |
        Set-Content -LiteralPath $botsDataFile
} else {
    Write-Host "WARNING: '$botsDataFile' not found - skipping reset."
}

Write-Host "Bots closed. (Set `$DontRun back to `$false in run-bots.ps1 to resume.)"

Write-Host "=========================================================================================================================================================================================================="
Write-Host "========================================================================================== SCRIPT END ===================================================================================================="
Write-Host "=========================================================================================================================================================================================================="
}
finally
{
Stop-Transcript | Out-Null
}
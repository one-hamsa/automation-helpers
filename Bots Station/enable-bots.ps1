# Force the script to run as Administrator
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    Exit
}


$logPath = Join-Path $PSScriptRoot 'Logs\enable-bots.log'
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

# Set $DontRun = $false in run-bots.ps1 so that we could run bots now
if (Test-Path -LiteralPath $runBots) {
    $content = Get-Content -LiteralPath $runBots -Raw
    $updated = $content -replace '(\$DontRun\s*=\s*)\$(?:true|false)', '$1$$false'
    if ($updated -ne $content) {
        Set-Content -LiteralPath $runBots -Value $updated -NoNewline
        Write-Host "Set `$DontRun = `$true in run-bots.ps1"
    } else {
        Write-Warning "No `$DontRun = `$true/`$false line found in run-bots.ps1 - flag unchanged."
    }
} else {
    Write-Warning "run-bots.ps1 not found at $runBots"
}



Write-Host "Bots enabled, now starting the bots - running 'run-bots.ps1'"
Write-Host ""

schtasks /Run /TN "UnderdogsBotLoop"

Write-Host "=========================================================================================================================================================================================================="
Write-Host "========================================================================================== SCRIPT END ===================================================================================================="
Write-Host "=========================================================================================================================================================================================================="
}
finally
{
Stop-Transcript | Out-Null
}
# Enables the UnderdogsBotLoop scheduled task and runs one cycle now.
# Run daily at 10:00 by UnderdogsEnableBots; disable-bots.ps1 turns it off. The tasks are
# registered by register-tasks.ps1. Needs no admin.

$TaskName = "UnderdogsBotLoop"

$logDir = Join-Path $PSScriptRoot 'Logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'enable-bots.log') | Out-Null

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

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "'$TaskName' is not registered - run register-tasks.ps1 first."
}

Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
Write-Host "Enabled '$TaskName'"

Write-Host "Bots enabled, now starting the bots - running 'run-bots.ps1'"
Write-Host ""

Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop

Write-Host "=========================================================================================================================================================================================================="
Write-Host "========================================================================================== SCRIPT END ===================================================================================================="
Write-Host "=========================================================================================================================================================================================================="
}
finally
{
Stop-Transcript | Out-Null
}

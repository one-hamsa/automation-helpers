# Registers (or refreshes) the UnderdogsBotLoop scheduled task and runs one cycle now.
# The task is the only thing that keeps the bots alive past the workflow job: the runner
# kills every process a job started when the job ends. Needs no admin.

$TaskName    = "UnderdogsBotLoop"
$LoopMinutes = 15

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

$runBots = Join-Path $PSScriptRoot "run-bots.ps1"
if (-not (Test-Path -LiteralPath $runBots)) {
    throw "run-bots.ps1 not found at $runBots"
}

# The task runs as whoever runs this script (the runner's account), so the bots share its
# LocalLow data path. S4U runs it whether or not that user is logged on, without storing a
# password; the built-in service accounts have no password and use ServiceAccount instead.
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$logonType = if ($identity.User.Value -in 'S-1-5-18', 'S-1-5-19', 'S-1-5-20') { 'ServiceAccount' } else { 'S4U' }

$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
                -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runBots`"" `
                -WorkingDirectory $PSScriptRoot
# First repeat one interval from now - the Start-ScheduledTask below covers the first cycle.
$trigger   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($LoopMinutes) `
                -RepetitionInterval (New-TimeSpan -Minutes $LoopMinutes)
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType $logonType -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

# -Force replaces any existing definition, so paths and settings always match this folder.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
Write-Host "Registered '$TaskName' as $($identity.Name) ($logonType), every $LoopMinutes minutes -> $runBots"

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

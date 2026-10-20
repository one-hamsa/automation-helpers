# Registers (or refreshes) the station's scheduled tasks. Update Bots Version runs it on every
# deploy, so a new machine needs no manual task setup and a moved folder gets fresh paths.
# Needs no admin.
#   UnderdogsEnableBots   daily 10:00 -> enable-bots.ps1
#   UnderdogsDisableBots  daily 22:00 -> disable-bots.ps1
#   UnderdogsBotLoop      every 15 minutes -> run-bots.ps1. Registered disabled;
#                         enable-bots.ps1 / disable-bots.ps1 switch it on and off.
# Scheduled tasks are what keep the bots alive past the workflow job: the runner kills every
# process a job started when the job ends.

$EnableAt    = '10:00'
$DisableAt   = '22:00'
$LoopMinutes = 15

$ErrorActionPreference = 'Stop'

# The tasks run as whoever runs this script (the runner's account), so the bots share its
# LocalLow data path. S4U runs them whether or not that user is logged on, without storing a
# password; the built-in service accounts have no password and use ServiceAccount instead.
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$logonType = if ($identity.User.Value -in 'S-1-5-18', 'S-1-5-19', 'S-1-5-20') { 'ServiceAccount' } else { 'S4U' }
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType $logonType -RunLevel Limited

function Register-StationTask([string]$name, [string]$script, $trigger, [switch]$disabled)
{
    $path = Join-Path $PSScriptRoot $script
    if (-not (Test-Path -LiteralPath $path)) { throw "$script not found at $path" }

    $action   = New-ScheduledTaskAction -Execute 'powershell.exe' `
                    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$path`"" `
                    -WorkingDirectory $PSScriptRoot
    # StartWhenAvailable: a run missed while the machine was off happens once it is back.
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
                    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                    -Disable:$disabled

    # -Force replaces any existing definition.
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Registered '$name' -> $path$(if ($disabled) { ' (disabled)' })"
}

Register-StationTask 'UnderdogsEnableBots'  'enable-bots.ps1'  (New-ScheduledTaskTrigger -Daily -At $EnableAt)
Register-StationTask 'UnderdogsDisableBots' 'disable-bots.ps1' (New-ScheduledTaskTrigger -Daily -At $DisableAt)
Register-StationTask 'UnderdogsBotLoop'     'run-bots.ps1' `
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $LoopMinutes)) `
    -disabled

Write-Host "Tasks run as $($identity.Name) ($logonType)."

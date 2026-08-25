@echo off
setlocal EnableDelayedExpansion

set "BUILD_DIR=%~1"
set "EXE_NAME=Underdogs.exe"
set "PROCESS_NAME=Underdogs"
set "INSTANCE_COUNT=%~2"
set "SYNC_DIR=%TEMP%\underdogs_bot_sync"

:: Unity's persistentDataPath on Windows. Every instance shares it - the control file the
:: game polls, the session logs it writes and the bot slot file all live here.
set "PC_DATA_DIR=%USERPROFILE%\AppData\LocalLow\One Hamsa\UNDERDOGS"
set "PC_AUTOMATION_CONTROL=%PC_DATA_DIR%\automationControl.txt"
set "PC_LOGS_SRC=%PC_DATA_DIR%\Logs"
set "BOTS_DATA_FILE=%PC_DATA_DIR%\Bots_Local_Data.txt"
:: Stamped just before the instances start, so the session logs written by this run can be
:: told apart from those still sitting in the Logs folder from earlier ones.
set "PC_LAUNCH_MARKER=%SYNC_DIR%\PC_LAUNCH_MARKER"

if %INSTANCE_COUNT% LSS 0 (
    set "INSTANCE_COUNT=0"
)

if %INSTANCE_COUNT% GTR 5 (
    set "INSTANCE_COUNT=5"
)

echo ========================================================
echo        STARTING PC BOTS TEST (!INSTANCE_COUNT! instances)
echo ========================================================
echo   Build dir: !BUILD_DIR!
echo   Looking for: !BUILD_DIR!\!EXE_NAME!

if not exist "!BUILD_DIR!\!EXE_NAME!" (
    echo ERROR: !BUILD_DIR!\!EXE_NAME! not found!
    echo Listing contents of !BUILD_DIR!:
    dir "!BUILD_DIR!" 2>nul || echo   Directory does not exist!
    pause
    exit /b 1
)

:: Add firewall rule via elevated PowerShell so the exe doesn't trigger "allow network access" popup
echo Adding firewall rule for %EXE_NAME% (may trigger UAC prompt)...
set "FW_SCRIPT=%TEMP%\underdogs_fw.ps1"

:: 1. Define the exact variables using double quotes
echo $fwRule = "Underdogs Bot Test" > "%FW_SCRIPT%"
echo $exePath = "%BUILD_DIR%\%EXE_NAME%" >> "%FW_SCRIPT%"

:: 2. Write the netsh commands exactly as they appear in Snippet 1
echo netsh advfirewall firewall delete rule name="$fwRule" 2^>`$null >> "%FW_SCRIPT%"
echo netsh advfirewall firewall add rule name="$fwRule" dir=in  action=allow program="$exePath" enable=yes >> "%FW_SCRIPT%"
echo netsh advfirewall firewall add rule name="$fwRule" dir=out action=allow program="$exePath" enable=yes >> "%FW_SCRIPT%"

:: 3. Run it elevated and clean up
powershell -Command "Start-Process powershell -ArgumentList '-ExecutionPolicy Bypass -File \"%FW_SCRIPT%\"' -Verb RunAs -Wait"
del "%FW_SCRIPT%" >nul 2>&1

:: Wait for Quest game to start before launching PC instances
echo Waiting for Quest game to start...
:WAIT_FOR_START
if not exist "%SYNC_DIR%\GAME_STARTED" (
    ping 127.0.0.1 -n 2 >nul
    goto WAIT_FOR_START
)
echo Quest game started! Launching PC instances...

:: A stale word here would be acted on seconds after launch, and the session log cutoff
:: below is measured from this marker.
del /q "%PC_AUTOMATION_CONTROL%" >nul 2>&1
echo launching > "%PC_LAUNCH_MARKER%"

:: Launch instances minimized in parallel
for /L %%i in (1,1,%INSTANCE_COUNT%) do (
    echo Launching instance %%i...
    start "" "%BUILD_DIR%\%EXE_NAME%" -batchmode -nographics -noaudio
    :: delay between launches to avoid file-lock conflicts
    ping 127.0.0.1 -n 6 >nul
)

echo All %INSTANCE_COUNT% instances launched. Waiting for Quest game to stop...

:: Wait for Quest game to stop
:WAIT_FOR_STOP
if not exist "%SYNC_DIR%\GAME_STOPPED" (
    ping 127.0.0.1 -n 2 >nul
    goto WAIT_FOR_STOP
)

echo Quest game stopped. Waiting 10 extra seconds and closing...
ping 127.0.0.1 -n 11 >nul

:: ************************************************   CLOSING THE BOTS   ************************************************
:: One instance is kept alive to be quit gracefully, because only a graceful quit produces
:: a usable session log: ReportUtility flushes the queued entries and zips them into a
:: .udlog, where a force-kill leaves an empty archive behind.
:: The others have to die first. All instances share one persistentDataPath, and
:: AutomationControl deletes the control file the moment it reads it - with several alive,
:: exactly one would get the word and which one is a race. Killing the rest makes the
:: survivor the only reader. It is the first launched, so it joined the room first and has
:: the longest session of the five.
echo Closing all but the first instance...
powershell -NoProfile -Command "$p = @(Get-Process -Name '%PROCESS_NAME%' -ErrorAction SilentlyContinue | Sort-Object StartTime); if ($p.Count -gt 1) { $p[1..($p.Count - 1)] | Stop-Process -Force }; Write-Host \"  instances still running: $(@(Get-Process -Name '%PROCESS_NAME%' -ErrorAction SilentlyContinue).Count)\""
ping 127.0.0.1 -n 6 >nul

echo Asking the last instance to quit...
>"%PC_AUTOMATION_CONTROL%" echo quit
powershell -NoProfile -Command "$deadline = (Get-Date).AddSeconds(30); while ((Get-Date) -lt $deadline -and @(Get-Process -Name '%PROCESS_NAME%' -ErrorAction SilentlyContinue).Count -gt 0) { Start-Sleep -Seconds 2 }; if (@(Get-Process -Name '%PROCESS_NAME%' -ErrorAction SilentlyContinue).Count -gt 0) { Write-Host '  WARNING: still running after 30s - force-stopping, the session log will be truncated.' } else { Write-Host '  last instance exited on its own.' }"

:: force-kill whatever is left
taskkill /F /IM %EXE_NAME% 2>nul

:: Hand the Steam client's session log to the test folder, where the functionality checks
:: and the upload read it. Only logs written since the launch marker: the Logs folder keeps
:: older sessions, and one of those would report on the wrong run.
if not defined BOT_TEST_DIR (
    echo No BOT_TEST_DIR set - skipping the session log copy.
    goto after_pc_logs
)
:: Selected on the session start time in the file name, not on the file's timestamp: a
:: force-killed session leaves its folder behind and a later launch zips that leftover into
:: a .udlog with a fresh timestamp, which would otherwise look like this run's log while
:: holding the previous run's session. The name is written from the session's own start
:: time (LogManager), so it cannot drift. One minute of slack for clock skew.
echo Copying the Steam session log to the test folder...
powershell -NoProfile -Command "$ref = (Get-Item -LiteralPath $env:PC_LAUNCH_MARKER).LastWriteTimeUtc.AddMinutes(-1); $dest = Join-Path $env:BOT_TEST_DIR 'PC Logs'; New-Item -ItemType Directory -Force -Path $dest | Out-Null; $f = @(Get-ChildItem -LiteralPath $env:PC_LOGS_SRC -Filter *.udlog -File -ErrorAction SilentlyContinue | Where-Object { $_.BaseName -match '^(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})-' -and [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd-HH-mm-ss', $null) -ge $ref }); if ($f.Count -eq 0) { Write-Host '  WARNING: no session log from this run - the Steam checks will report unknown.' } else { $f | Copy-Item -Destination $dest; Write-Host \"  copied $($f.Count) .udlog file(s) to $dest\" }"
:after_pc_logs

:: Reset Bots_Local_Data.txt so all bots slots are available again for the next run
if exist "%BOTS_DATA_FILE%" (
    echo Resetting bots availability in "%BOTS_DATA_FILE%"...
    powershell -NoProfile -Command "(Get-Content -LiteralPath '%BOTS_DATA_FILE%') -replace '/\s*no\s*$', '/ yes' | Set-Content -LiteralPath '%BOTS_DATA_FILE%'"
) else (
    echo WARNING: "%BOTS_DATA_FILE%" not found - skipping reset.
)

:: Signal that the PC test is fully done
echo done > "%SYNC_DIR%\PC_DONE"

echo ========================================================
echo              PC BOTS TEST COMPLETE
echo ========================================================

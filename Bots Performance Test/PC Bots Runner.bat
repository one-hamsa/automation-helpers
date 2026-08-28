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
set "BOTS_DATA_FILE=%PC_DATA_DIR%\Bots_Local_Data.txt"

:: Unity writes its player log to one fixed path per user, so without -logFile all the
:: instances truncate and overwrite each other's and the surviving file belongs to whichever
:: one launched last. Each instance gets its own file here instead; "Run Both Tests.bat"
:: collects them into the test folder once both runners are done.
if not defined PC_BOT_LOGS_DIR set "PC_BOT_LOGS_DIR=%TEMP%\underdogs_bot_logs"

:: NirCmd mutes a single process's audio session. Needed because -noaudio does not silence
:: the instances, and five unmuted bots make the rig audible to whoever is next to it.
set "NIRCMD=%~dp0SoundDisableHelper\nircmd.exe"

:: Seconds between instance launches. Widened from 5 to spread the startup load, which is
:: the heaviest moment on the rig - otherwise every instance loads its scenes at once.
:: "ping -n N" waits N-1 seconds.
set "LAUNCH_INTERVAL=10"
set /a LAUNCH_PING_COUNT=LAUNCH_INTERVAL+1

if %INSTANCE_COUNT% LSS 0 (
    set "INSTANCE_COUNT=0"
)

if %INSTANCE_COUNT% GTR 5 (
    set "INSTANCE_COUNT=5"
)

echo ========================================================
echo        STARTING PC BOTS TEST (!INSTANCE_COUNT! instances)
echo ========================================================
echo   Launch interval: !LAUNCH_INTERVAL!s
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

:: The PC instances go up first and the headset follows - "Run Both Tests.bat" holds the
:: Quest runner back until PC_BOTS_LAUNCHED appears below, then waits out its own delay.
echo Launching PC instances...

:: A stale word here would be acted on seconds after launch.
del /q "%PC_AUTOMATION_CONTROL%" >nul 2>&1

:: Start from an empty log directory so a crashed instance from the previous run cannot be
:: mistaken for this one's.
if exist "!PC_BOT_LOGS_DIR!" rd /s /q "!PC_BOT_LOGS_DIR!"
mkdir "!PC_BOT_LOGS_DIR!"

if not exist "!NIRCMD!" echo WARNING: !NIRCMD! not found - the instances will play audio.

:: Launch instances minimized in parallel
for /L %%i in (1,1,%INSTANCE_COUNT%) do (
    echo Launching instance %%i...
    set "BOT_INDEX=%%i"
    :: PowerShell rather than "start", because muting needs the PID of the process just
    :: launched. Paths go through the environment - several of them contain spaces, and
    :: quoting them through cmd into -Command is what breaks first.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$log = Join-Path $env:PC_BOT_LOGS_DIR ('bot_' + $env:BOT_INDEX + '.log'); $q = [char]34; $p = Start-Process -FilePath (Join-Path $env:BUILD_DIR $env:EXE_NAME) -ArgumentList ('-batchmode -nographics -noaudio -logFile ' + $q + $log + $q) -WorkingDirectory $env:BUILD_DIR -PassThru; Start-Sleep -Milliseconds 500; if (Test-Path $env:NIRCMD) { & $env:NIRCMD muteappvolume ('/' + $p.Id) 1 }; Write-Host ('   PID ' + $p.Id + ' -> ' + $log)"
    :: delay between launches to avoid file-lock conflicts
    ping 127.0.0.1 -n !LAUNCH_PING_COUNT! >nul
)

:: Releases "Run Both Tests.bat", which then waits out its delay before starting the headset.
echo launched > "%SYNC_DIR%\PC_BOTS_LAUNCHED"

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
:: All instances share one persistentDataPath, and LogManager archives every session
:: directory that is not its own on startup - so a PC session (.udlog) log is either an
:: empty archive or deleted out from under a live instance. The per-instance player logs
:: written via -logFile are unaffected and are collected after this. The run itself is
:: still judged from the Quest log, where a full room and logged kills already prove the
:: PC bots played. No graceful quit to arrange, so kill them all.
echo Closing all instances...
taskkill /F /IM %EXE_NAME% 2>nul
powershell -NoProfile -Command "Write-Host \"  instances still running: $(@(Get-Process -Name '%PROCESS_NAME%' -ErrorAction SilentlyContinue).Count)\""

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

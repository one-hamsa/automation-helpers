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

:: A stale word here would be acted on seconds after launch.
del /q "%PC_AUTOMATION_CONTROL%" >nul 2>&1

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
:: All instances share one persistentDataPath, and LogManager archives every session
:: directory that is not its own on startup - so a PC session log is either an empty
:: archive or deleted out from under a live instance. Nothing reads them: the run is
:: judged from the Quest log, where a full room and logged kills already prove the PC
:: bots played. No graceful quit to arrange, so kill them all.
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

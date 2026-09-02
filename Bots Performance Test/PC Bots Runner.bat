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

:: The wait loop below ticks about once a second; MONITOR_INTERVAL is how many of those
:: ticks pass between liveness checks. Replacement is not capped - the free bot slots are
:: the budget, and the run stops replacing instances once they are used up.
set "MONITOR_INTERVAL=5"

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

:: Free slots at the start of the run are the budget for the whole run: a crashed instance
:: holds its slot until the reset at the end, so the count only ever drops. LAUNCH_COUNT is
:: every launch we issue, initial ones included, and spends against that budget.
call :READ_BOT_SLOTS
set "SLOTS_BASELINE_FREE=!SLOTS_FREE!"
set "LAUNCH_COUNT=0"
echo   Free bot slots: !SLOTS_FREE! of !SLOTS_TOTAL!

:: Launch instances minimized in parallel
for /L %%i in (1,1,%INSTANCE_COUNT%) do (
    echo Launching instance %%i...
    set "RESTARTS_%%i=0"
    call :LAUNCH_INSTANCE %%i
    :: delay between launches to avoid file-lock conflicts. Skipped after the last one -
    :: the headset is held back until the signal below, so a trailing wait here delays it.
    if %%i LSS !INSTANCE_COUNT! ping 127.0.0.1 -n !LAUNCH_PING_COUNT! >nul
)

:: Releases "Run Both Tests.bat", which starts the headset as soon as it sees this.
echo launched > "%SYNC_DIR%\PC_BOTS_LAUNCHED"

echo All %INSTANCE_COUNT% instances launched. Waiting for Quest game to stop...

:: Wait for Quest game to stop, replacing any instance that died in the meantime.
:: MONITOR_INSTANCES is cleared once there is nothing left to replace a dead instance with.
set "MONITOR_TICK=0"
set "MONITOR_INSTANCES=1"
:WAIT_FOR_STOP
if not exist "%SYNC_DIR%\GAME_STOPPED" (
    ping 127.0.0.1 -n 2 >nul
    set /a MONITOR_TICK+=1
    if defined MONITOR_INSTANCES if !MONITOR_TICK! GEQ !MONITOR_INTERVAL! (
        set "MONITOR_TICK=0"
        call :CHECK_INSTANCES
    )
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

exit /b 0

:: ************************************************   SUBROUTINES   ************************************************

:: :LAUNCH_INSTANCE <index>
:: Starts one instance and records its process id in PID_<index>, which is what
:: :CHECK_INSTANCES watches. Used for the initial launch and for every replacement.
:LAUNCH_INSTANCE
set "BOT_INDEX=%~1"
set /a LAUNCH_COUNT+=1
:: Logs are numbered by launch order rather than by instance: the initial launches take
:: 1..INSTANCE_COUNT and every replacement takes the next number after those. A replacement
:: therefore never writes over the log of the instance it replaced - that log is the only
:: record of the crash. "Run Both Tests.bat" globs bot_*.log and collects both.
if !LAUNCH_COUNT! GTR !INSTANCE_COUNT! (
    set "BOT_LOG_NAME=bot_!LAUNCH_COUNT!_retry.log"
) else (
    set "BOT_LOG_NAME=bot_!LAUNCH_COUNT!.log"
)
:: PowerShell hands the PID back through a file - a value written to stdout would be mixed
:: in with whatever else the command prints.
set "BOT_PID_FILE=!PC_BOT_LOGS_DIR!\bot_%~1.pid"
del /q "!BOT_PID_FILE!" >nul 2>&1
set "PID_%~1="
:: PowerShell rather than "start", because muting needs the PID of the process just
:: launched. Paths go through the environment - several of them contain spaces, and
:: quoting them through cmd into -Command is what breaks first.
:: Launch flags and mute timing are kept identical to the Bots Station's run-bots.ps1, which
:: is the version known to run silently. The one thing that script has and this chain does
:: not is elevation - it re-launches itself as Administrator, so its nircmd runs elevated -
:: hence the RunAs here, the same way the firewall rule above is elevated.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$log = Join-Path $env:PC_BOT_LOGS_DIR $env:BOT_LOG_NAME; $q = [char]34; $p = Start-Process -FilePath (Join-Path $env:BUILD_DIR $env:EXE_NAME) -ArgumentList ('-batchmode -nographics -logFile ' + $q + $log + $q) -WorkingDirectory $env:BUILD_DIR -NoNewWindow -PassThru; Set-Content -LiteralPath $env:BOT_PID_FILE -Value $p.Id; Start-Sleep -Milliseconds 500; if (Test-Path $env:NIRCMD) { Start-Process -FilePath $env:NIRCMD -ArgumentList @('muteappvolume', ('/' + $p.Id), '1') -Verb RunAs -Wait }; Write-Host ('   PID ' + $p.Id + ' -> ' + $log)"
if exist "!BOT_PID_FILE!" set /p PID_%~1=<"!BOT_PID_FILE!"
if not defined PID_%~1 echo   WARNING: no PID captured for instance %~1 - it will not be monitored.
goto :eof

:: :CHECK_INSTANCES
:: One pass over the launched PIDs, replacing anything that is no longer running. About
:: half the runs lose an instance to an IL2CPP crash partway through, which otherwise
:: leaves the room short a bot for the rest of the test.
:CHECK_INSTANCES
for /L %%i in (1,1,%INSTANCE_COUNT%) do (
    if defined MONITOR_INSTANCES (
        set "CHECK_PID=!PID_%%i!"
        if defined CHECK_PID (
            rem Image name as well as PID - Windows reuses PIDs, and a match on the number
            rem alone would read some unrelated process as a live bot. tasklist prints an
            rem "INFO:" line rather than a row when nothing matches, so the image name
            rem landing in the first column is what says the instance is up. Parsed here
            rem instead of piped into find, which resolves to the wrong binary when the
            rem runner is started from a shell that puts a unix toolchain ahead of
            rem System32 on PATH.
            set "INSTANCE_ALIVE="
            for /f "tokens=1" %%a in ('tasklist /FI "PID eq !CHECK_PID!" /FI "IMAGENAME eq %EXE_NAME%" /NH 2^>nul') do (
                if /I "%%a"=="%EXE_NAME%" set "INSTANCE_ALIVE=1"
            )
            if not defined INSTANCE_ALIVE (
                rem Per replacement rather than once per pass - two instances can be found
                rem dead in the same pass with only one slot left between them.
                call :CHECK_FREE_BOT_SLOT
                if defined MONITOR_INSTANCES (
                    set /a RESTARTS_%%i+=1
                    echo   instance %%i ^(PID !CHECK_PID!^) is gone - relaunching ^(restart #!RESTARTS_%%i!^)...
                    call :LAUNCH_INSTANCE %%i
                ) else (
                    echo   instance %%i ^(PID !CHECK_PID!^) is gone - left down.
                )
            )
        )
    )
)
goto :eof

:: :CHECK_FREE_BOT_SLOT
:: Clears MONITOR_INSTANCES when nothing is left to replace a dead instance with.
:CHECK_FREE_BOT_SLOT
if not exist "%BOTS_DATA_FILE%" goto :eof
call :READ_BOT_SLOTS
if !SLOTS_TOTAL! EQU 0 goto :eof
:: An instance takes tens of seconds to boot and mark its slot taken, so a slot already
:: spoken for by a launch we just issued still reads as free here. Netting the launches
:: off against the slots actually claimed is what keeps a second dead instance in the
:: same pass from being promised the one remaining slot as well.
set /a SLOTS_CLAIMED=SLOTS_BASELINE_FREE-SLOTS_FREE
set /a SLOTS_PENDING=LAUNCH_COUNT-SLOTS_CLAIMED
if !SLOTS_PENDING! LSS 0 set "SLOTS_PENDING=0"
set /a SLOTS_AVAILABLE=SLOTS_FREE-SLOTS_PENDING
if !SLOTS_AVAILABLE! GTR 0 goto :eof
set "MONITOR_INSTANCES="
echo   no bot slots left in "%BOTS_DATA_FILE%" - no longer replacing dead instances.
goto :eof

:: :READ_BOT_SLOTS
:: Counts the bot slots into SLOTS_TOTAL and the available ones into SLOTS_FREE. Lines read
:: "<id> / <name> / yes|no". The instances write this file while we read it, so a read that
:: comes back with no rows at all reports zero and is treated by the caller as a bad read.
:READ_BOT_SLOTS
set /a SLOTS_TOTAL=0, SLOTS_FREE=0
if not exist "%BOTS_DATA_FILE%" goto :eof
for /f "usebackq tokens=3 delims=/" %%s in ("%BOTS_DATA_FILE%") do (
    set "SLOT=%%s"
    set "SLOT=!SLOT: =!"
    set /a SLOTS_TOTAL+=1
    if /I "!SLOT!"=="yes" set /a SLOTS_FREE+=1
)
goto :eof

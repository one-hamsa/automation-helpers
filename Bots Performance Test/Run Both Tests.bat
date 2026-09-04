@echo off
setlocal EnableDelayedExpansion

:: Usage: "Run Both Tests.bat" <PC_BUILD_DIR> [DRIVE_FOLDER_NAME] [STARTED_BY] [NUMBER_OF_PC_BOTS] [COMMIT_SHA] [COMMIT_REF]
set "PC_BUILD_DIR=%~1"
set "DRIVE_FOLDER_NAME=%~2"
set "STARTED_BY=%~3"
set "NUMBER_OF_PC_BOTS=%~4"
set "COMMIT_SHA=%~5"
set "COMMIT_REF=%~6"
set "SYNC_DIR=%TEMP%\underdogs_bot_sync"

if not defined PC_BUILD_DIR (
    echo ERROR: Please provide the PC build directory as the first argument.
    echo Usage: "Run Both Tests.bat" ^<PC_BUILD_DIR^> [DRIVE_FOLDER_NAME] [STARTED_BY]
    pause
    exit /b 1
)

:: Resolve relative build path to absolute so the start-ed subprocess can find it
pushd "!PC_BUILD_DIR!" 2>nul && (
    set "PC_BUILD_DIR=!CD!"
    popd
)

:: Where results and runner logs live. Owned here rather than in the Quest runner because
:: the upload at the end of this script needs the test folder, and the PC runner needs it
:: to drop the Steam client's session log into.
set "ROOT_DIR=E:\Automation\UNDERDOGS Bots Automation\Tests Data"
set "LOG_DIR=E:\Automation\UNDERDOGS Bots Automation\Log Files"

if not defined DRIVE_FOLDER_NAME goto default_folder_name
if "!DRIVE_FOLDER_NAME!"=="" goto default_folder_name
if "!DRIVE_FOLDER_NAME!"==" " goto default_folder_name
goto folder_name_ready

:default_folder_name
for /f "tokens=1-6 delims= " %%a in ('powershell -NoProfile -Command "Get-Date -format 'dd MM yy HH mm ss'"') do (
    set "TIMESTAMP=%%a-%%b-%%c_%%d-%%e-%%f"
)
set "DRIVE_FOLDER_NAME=BOTS TEST - Name(-) - Started At(!TIMESTAMP!)"

:folder_name_ready
set "BOT_TEST_DIR=!ROOT_DIR!\!DRIVE_FOLDER_NAME!"
if not exist "!ROOT_DIR!" mkdir "!ROOT_DIR!"
if not exist "!BOT_TEST_DIR!" mkdir "!BOT_TEST_DIR!"
if not exist "!LOG_DIR!" mkdir "!LOG_DIR!"

:: One console log for the whole run. The two runners write their own files while they run
:: in parallel - Windows will not let both append to one file - and are folded into this at
:: the end, so "Log Files" ends up holding this log and the device logcat, nothing else.
set "MASTER_LOG=!LOG_DIR!\Run Both Tests.log"
set "QUEST_LOG=%SYNC_DIR%\quest_output.log"
set "PC_LOG=%SYNC_DIR%\pc_output.log"
set "UPLOAD_LOG=%SYNC_DIR%\upload_output.log"

:: Clean up and create the sync directory. Before the first :say, because the runner logs
:: and the master log both depend on it existing.
:: The ADB daemon first: the one the Quest runner forks inherits that runner's stdout -
:: quest_output.log, in here - and holds the handle for its whole lifetime, so a daemon
:: left over from a previous run makes both the delete below and the redirect that starts
:: the Quest runner fail. That failure is silent, and the run then hangs on :WAIT_BOTH
:: waiting for a QUEST_DONE that never comes. Killing it here makes this run independent
:: of how the last one ended, including a manual run that was never closed down.
adb kill-server >nul 2>&1
if exist "%SYNC_DIR%" rd /s /q "%SYNC_DIR%"
mkdir "%SYNC_DIR%"

:: Last run's logs, including the two per-runner files this script used to leave behind.
del /q "!LOG_DIR!\*.log" >nul 2>&1
type nul > "!MASTER_LOG!"

call :say ========================================================
call :say        LAUNCHING BOTH QUEST AND PC BOT TESTS
call :say ========================================================
call :say   PC build dir: !PC_BUILD_DIR!
call :say   Folder name:  !DRIVE_FOLDER_NAME!
call :say   Test dir:     !BOT_TEST_DIR!
call :say   Started by:   !STARTED_BY!
call :say ========================================================

:: Pass folder name and started-by via environment variables instead of
:: command-line arguments, because folder names with parentheses break
:: cmd.exe argument parsing in the start/cmd /c chain.
set "BOT_FOLDER_NAME=!DRIVE_FOLDER_NAME!"
set "BOT_STARTED_BY=!STARTED_BY!"
set "BOT_NUM_PC_BOTS=!NUMBER_OF_PC_BOTS!"
set "BOT_COMMIT_SHA=!COMMIT_SHA!"
set "BOT_COMMIT_REF=!COMMIT_REF!"
:: UploadFiles.py zips the runner logs out of this.
set "LOG_FILES_DIR=!LOG_DIR!"
:: Where the PC runner points each instance's -logFile. Staged outside the test folder and
:: collected below, because the Quest runner's "adb pull" has to be what creates
:: "Report Logs" - pulling into a directory that already exists nests the session one level
:: deeper than the report expects.
set "PC_BOT_LOGS_DIR=%SYNC_DIR%\pc_bot_logs"

:: The PC instances go first, the headset follows as soon as they are all up. No settling
:: delay on top of that - the Quest runner already waits 60s after launching the game before
:: it records anything, which the bots spend joining.
:: Both runners still run in parallel once started; the ordering is enforced here rather
:: than by the runners signalling each other.
:: /B = no new window (avoids QuickEdit freezing when the window is clicked).
:: Output goes to log files so we can review them after the run.
:: "< nul" on this one too: the PC runner "pause"s if the build exe is missing, and with the
:: headset now waiting behind it that would hang the whole run instead of just its own half.
start /B cmd /c ""%~dp0PC Bots Runner.bat"  "!PC_BUILD_DIR!" !NUMBER_OF_PC_BOTS! < nul > "!PC_LOG!" 2>&1"

:: Bounded, because this runner is a shared resource - the concurrency group holds every
:: other automation run behind this one, so waiting out the job's 30-minute timeout costs
:: far more than starting the headset into a room the PC bots never reached.
call :say PC bots launching. Waiting for them to all come up...
set "PC_LAUNCH_WAIT=0"
:WAIT_PC_LAUNCHED
if not exist "%SYNC_DIR%\PC_BOTS_LAUNCHED" (
    if !PC_LAUNCH_WAIT! GEQ 300 (
        call :say WARNING: PC bots did not report launched after !PC_LAUNCH_WAIT!s - starting the headset anyway.
        goto PC_LAUNCH_DONE
    )
    ping 127.0.0.1 -n 4 >nul
    set /a PC_LAUNCH_WAIT+=3
    goto WAIT_PC_LAUNCHED
)
:PC_LAUNCH_DONE

call :say All PC bots launched. Starting the headset.

:: "< nul" prevents Quest bat's "pause" from hanging in CI.
start /B cmd /c ""%~dp0Quest Bots Runner.bat" < nul > "!QUEST_LOG!" 2>&1"

call :say Both tests launched. Waiting for both to complete...

:: Poll until both signal completion
:WAIT_BOTH
if exist "%SYNC_DIR%\QUEST_DONE" if exist "%SYNC_DIR%\PC_DONE" goto ALL_DONE
ping 127.0.0.1 -n 3 >nul
goto WAIT_BOTH

:ALL_DONE
:: Small grace period for processes to finish writing
ping 127.0.0.1 -n 3 >nul

:: Fold both runners into the master log, and print them so CI sees them.
call :append_section "QUEST LOG" "!QUEST_LOG!"
call :append_section "PC LOG" "!PC_LOG!"

:: Collect the PC instances' player logs into the test folder so they ship with the results
:: the same way the headset's pulled logs do. The launch number in the file name is what
:: identifies the instance, so the logs sit directly in the folder rather than each in one
:: of its own. After both runners are done: the instances are killed by then, so the files
:: are closed, and the Quest runner's pull has already created "Report Logs".
call :blank
call :say ======================== PC BOT LOGS ========================
set "PC_LOGS_DEST=!BOT_TEST_DIR!\Report Logs\PC Bots Logs"
set "PC_LOGS_FOUND=0"
:: Driven by the files on disk rather than NUMBER_OF_PC_BOTS, so a clamped count or an
:: instance that never launched needs no special case here.
mkdir "!PC_LOGS_DEST!" 2>nul
for %%f in ("!PC_BOT_LOGS_DIR!\bot_*.log") do (
    set "BOT_ID=%%~nf"
    set "BOT_ID=!BOT_ID:bot_=!"
    copy /y "%%f" "!PC_LOGS_DEST!\Bot_!BOT_ID!.log" >nul
    call :say   Bot_!BOT_ID!.log: %%~zf bytes
    set /a PC_LOGS_FOUND+=1
)
if "!PC_LOGS_FOUND!"=="0" call :say   No PC bot logs in "!PC_BOT_LOGS_DIR!" - nothing to collect.

:: ***********************************   REPORT AND UPLOAD   ***********************************
:: Runs here rather than inside the Quest runner, so that everything the report reads is
:: already in the test folder: the Quest's pulled logs, the Steam client's session log and
:: the screenshots. Both runners have finished by definition - this is past the wait above.
call :blank
call :say ========================================================
call :say          GENERATING REPORT AND UPLOADING
call :say ========================================================

python "%~dp0UploadFiles.py" "!BOT_TEST_DIR!" "!BOT_FOLDER_NAME!" --started-by "!BOT_STARTED_BY!" --num-pc-bots "!BOT_NUM_PC_BOTS!" --commit-sha "!BOT_COMMIT_SHA!" --commit-ref "!BOT_COMMIT_REF!" --github-token "%AUTOMATION_REPOS_PAT%" > "!UPLOAD_LOG!" 2>&1
set "UPLOAD_EXIT=!ERRORLEVEL!"
call :append_section "UPLOAD LOG" "!UPLOAD_LOG!"
:: Reported, not propagated: the workflow judges the run on the test itself, and a failed
:: upload must still leave the headset and the PC bots to be closed down cleanly.
if not "!UPLOAD_EXIT!"=="0" call :say WARNING: UploadFiles.py exited with !UPLOAD_EXIT!

:: Clean up sync directory. The ADB daemon first - it holds quest_output.log open, so the
:: delete fails while it is alive. Nothing past this point uses adb.
adb kill-server >nul 2>&1
rd /s /q "%SYNC_DIR%" >nul 2>&1

call :say ========================================================
call :say           BOTH TESTS COMPLETE
call :say    Files saved locally in: !BOT_TEST_DIR!
call :say    Files saved in google drive in: !DRIVE_FOLDER_NAME!
call :say ========================================================
exit /b 0

:: Echo a blank separator line to both.
:blank
echo.
>> "!MASTER_LOG!" echo.
exit /b 0

:: Echo to the console and into the run's one log file.
:say
echo %*
>> "!MASTER_LOG!" echo %*
exit /b 0

:: Fold one runner's output into the master log under a banner, and print it for CI.
:append_section
call :blank
call :say ======================== %~1 ========================
if not exist "%~2" (
    call :say   [no log file]
    exit /b 0
)
type "%~2"
type "%~2" >> "!MASTER_LOG!"
exit /b 0

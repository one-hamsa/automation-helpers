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

echo ========================================================
echo        LAUNCHING BOTH QUEST AND PC BOT TESTS
echo ========================================================
echo   PC build dir: !PC_BUILD_DIR!
echo   Folder name:  !DRIVE_FOLDER_NAME!
echo   Started by:   !STARTED_BY!
echo ========================================================

:: Clean up and create the sync directory
if exist "%SYNC_DIR%" rd /s /q "%SYNC_DIR%"
mkdir "%SYNC_DIR%"

:: Log directory for persistent log files
set "LOG_DIR=C:\Automation\UNDERDOGS Bots Automation\Log Files"
if not exist "!LOG_DIR!" mkdir "!LOG_DIR!"

:: Pass folder name and started-by via environment variables instead of
:: command-line arguments, because folder names with parentheses break
:: cmd.exe argument parsing in the start/cmd /c chain.
set "BOT_FOLDER_NAME=!DRIVE_FOLDER_NAME!"
set "BOT_STARTED_BY=!STARTED_BY!"
set "BOT_NUM_PC_BOTS=!NUMBER_OF_PC_BOTS!"
set "BOT_COMMIT_SHA=!COMMIT_SHA!"
set "BOT_COMMIT_REF=!COMMIT_REF!"

:: Launch both runners in parallel.
:: /B = no new window (avoids QuickEdit freezing when the window is clicked).
:: Output goes to log files so we can review them after the run.
:: "< nul" prevents Quest bat's "pause" from hanging in CI.
start /B cmd /c ""%~dp0Quest Bots Runner.bat" < nul > "!LOG_DIR!\quest_output.log" 2>&1"
start /B cmd /c ""%~dp0PC Bots Runner.bat"  "!PC_BUILD_DIR!" !NUMBER_OF_PC_BOTS! > "!LOG_DIR!\pc_output.log" 2>&1"

echo Both tests launched. Waiting for both to complete...

:: Poll until both signal completion
:WAIT_BOTH
if exist "%SYNC_DIR%\QUEST_DONE" if exist "%SYNC_DIR%\PC_DONE" goto ALL_DONE
ping 127.0.0.1 -n 3 >nul
goto WAIT_BOTH

:ALL_DONE
:: Small grace period for processes to finish writing
ping 127.0.0.1 -n 3 >nul

:: Print the logs from both runners so CI can see them
echo.
echo ======================== QUEST LOG ========================
if exist "!LOG_DIR!\quest_output.log" (type "!LOG_DIR!\quest_output.log") else (echo   [no log file])
echo.
echo ======================== PC LOG ============================
if exist "!LOG_DIR!\pc_output.log" (type "!LOG_DIR!\pc_output.log") else (echo   [no log file])
echo.

:: Clean up sync directory
rd /s /q "%SYNC_DIR%" >nul 2>&1

echo ========================================================
echo           BOTH TESTS COMPLETE
echo ========================================================

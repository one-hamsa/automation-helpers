@echo off
setlocal EnableDelayedExpansion

:: making the adb use the one the profiler project uses so they won't fight over the adb server
set "PATH=C:\Program Files\Unity\Hub\Editor\2022.3.31f1\Editor\Data\PlaybackEngines\AndroidPlayer\SDK\platform-tools;%PATH%"

:: --- CONFIGURATION START ---
set "ROOT_DIR=E:\Automation\UNDERDOGS Bots Automation\Tests Data"
set "LOG_FILES_DIR=E:\Automation\UNDERDOGS Bots Automation\Log Files"
set "REMOTE_PATH=/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics"
set "SYNC_DIR=%TEMP%\underdogs_bot_sync"
:: --- CONFIGURATION END ---

:: Read folder name and started-by from environment variables (set by Run Both Tests.bat).
:: Environment variables avoid cmd.exe argument-parsing issues with parentheses in names.
:: Fall back to command-line arguments for standalone use.
if defined BOT_FOLDER_NAME (
    set "DRIVE_FOLDER_NAME=!BOT_FOLDER_NAME!"
) else (
    set "DRIVE_FOLDER_NAME=%~1"
)
if defined BOT_STARTED_BY (
    set "STARTED_BY=!BOT_STARTED_BY!"
) else (
    set "STARTED_BY=%~2"
)
if not defined STARTED_BY set "STARTED_BY=unknown"
if "!STARTED_BY!"=="" set "STARTED_BY=unknown"

:: Number of PC bots requested (the XR bot is added on top by the uploader).
:: Passed in via env var by Run Both Tests.bat; empty if standalone.
if defined BOT_NUM_PC_BOTS (
    set "NUM_PC_BOTS=!BOT_NUM_PC_BOTS!"
) else (
    set "NUM_PC_BOTS="
)

:: Commit/branch the build was made from (set by Run Both Tests.bat; empty if standalone).
if defined BOT_COMMIT_SHA (set "COMMIT_SHA=!BOT_COMMIT_SHA!") else (set "COMMIT_SHA=")
if defined BOT_COMMIT_REF (set "COMMIT_REF=!BOT_COMMIT_REF!") else (set "COMMIT_REF=")

echo Folder name is: "!DRIVE_FOLDER_NAME!"
echo Test started by: "!STARTED_BY!"
echo Number of PC bots requested: "!NUM_PC_BOTS!"
echo Build commit: "!COMMIT_SHA!" ref: "!COMMIT_REF!"

:: Get the date and time
for /f "tokens=1-6 delims= " %%a in ('powershell -Command "Get-Date -format 'dd MM yy HH mm ss'"') do (
    set "Day=%%a"
    set "Month=%%b"
    set "Year=%%c"
    set "Hour=%%d"
    set "Minute=%%e"
    set "Second=%%f"
)

:: Define the folder name including seconds
set "TIMESTAMP=%Day%-%Month%-%Year%_%Hour%-%Minute%-%Second%"

if not defined DRIVE_FOLDER_NAME set "DRIVE_FOLDER_NAME=BOTS TEST - Name(-) - Started at(!TIMESTAMP!)"
if "!DRIVE_FOLDER_NAME!"=="" set "DRIVE_FOLDER_NAME=BOTS TEST - Name(-) - Started at(!TIMESTAMP!)"
if "!DRIVE_FOLDER_NAME!"==" " set "DRIVE_FOLDER_NAME=BOTS TEST - Name(-) - Started at(!TIMESTAMP!)"

:: The directory
set "CURRENT_TEST_DIR=%ROOT_DIR%\%DRIVE_FOLDER_NAME%"

echo ========================================================
echo        STARTING UNDERDOGS TEST
echo ========================================================

if not exist "%CURRENT_TEST_DIR%" (
    mkdir "%CURRENT_TEST_DIR%"
)
:: ************************************************    SETTING UP EVERYTHING FOR THE TEST   ************************************************
echo ...
echo Setting up the headset for the test
echo ...

:: Restart the ADB daemon clean first, so no stale port-forward from a previous run
:: can make the profiler connect to a dead endpoint (TCP connects but the handshake fails).
adb kill-server >nul 2>&1
ping 127.0.0.1 -n 3 >nul

:: Ensure ADB daemon is running and device is connected before doing anything
adb start-server >nul 2>&1
echo Waiting for ADB device...
adb wait-for-device

:: Verify device is actually reachable
adb shell echo ok >nul 2>&1
if errorlevel 1 (
    echo ERROR: ADB device not responding. Aborting test.
    pause
    exit /b 1
)
echo ADB device connected.

:: Grow the logcat ring and clear it, so the whole run fits and nothing from a previous
:: run is in it. This is where the game's native profiler diagnostics land (tag
:: "il2cpplab"), plus Unity and system messages - dumped to Log Files at the end.
adb logcat -G 16M >nul 2>&1
adb logcat -c >nul 2>&1

::wake up the headset, disable the proximity censor and disable the guardian
adb wait-for-device
adb shell input keyevent KEYCODE_WAKEUP
adb shell am broadcast -a com.oculus.vrpowermanager.prox_close
adb shell setprop debug.oculus.guardian_pause 1

:: disable notifications and enable 'do not disturb' mode to make sure the navigation popup won't appear
adb shell settings put global heads_up_notifications_enabled 0
adb shell settings put global zen_mode 1

::wait 10 seconds to let the headset fully load
ping 127.0.0.1 -n 11 >nul

:: set brightness to normal range in case it's too high/low
adb wait-for-device
adb shell settings put system screen_brightness_mode 0
adb shell settings put system screen_brightness 120

::mute audio (why not)
adb shell input keyevent 164

echo ...
echo Setup completed
echo ...

:: ************************************************   1. WAKING UP & RESTART OVR   ************************************************
echo ...
echo [1/10] Enabling OVR metrics profiler and restarting OVR metrics tool...
echo ...

:: Force-stop OVR Metrics Tool to start clean
adb wait-for-device
adb shell am force-stop com.oculus.ovrmonitormetricsservice
ping 127.0.0.1 -n 3 >nul

:: Disable GPU profiling by turning off the service.
adb shell setprop debug.vr.gpuprofilingservice 0 
ping 127.0.0.1 -n 3 >nul



adb wait-for-device
adb shell am start omms://app
ping 127.0.0.1 -n 5 >nul


:: ************************************************ 2. LOCKING HARDWARE PERFORMANCE   ************************************************
echo ...
echo [2/10] making sure the quest is on a free performance state...
echo ...

adb wait-for-device
:: release the cpu/gpu locks
adb shell setprop debug.oculus.cpuLevel 4
adb shell setprop debug.oculus.gpuLevel 5

:: Turn off dynamic foveation and lock the foveation level
adb shell setprop debug.oculus.foveation.dynamic 0
adb shell setprop debug.oculus.foveation.level 0

:: ************************************************   3. LAUNCHING GAME   ************************************************
echo ...
echo [3/10] Launching Underdogs...
echo ...

adb wait-for-device
adb shell am start -n com.onehamsa.underdogs/com.unity3d.player.UnityPlayerActivity

:: Signal that the game has started
echo started > "%SYNC_DIR%\GAME_STARTED"

::sometimes a few seconds after opening a menu pops up, so we focus on the app again

ping 127.0.0.1 -n 6 >nul

adb wait-for-device
adb shell monkey -p com.onehamsa.underdogs -c android.intent.category.LAUNCHER 1

:: ************************************************   4. WAITING FOR THE GAME TO LOAD   ************************************************
echo ...
echo [4/10] Waiting a minute for the game to fully load...
echo ...

ping 127.0.0.1 -n 61 >nul

:: ************************************************   5. RECORDING PERFORMANCE   ************************************************
echo ...
echo [5/10] Running game...
echo ...

:: take a screenshot, Wait 30 sec, take a screenshot again, wait 30 sec, and take a screenshot at the end.

echo    Taking screenshot 1 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_1.png

ping 127.0.0.1 -n 31 >nul

echo preparing the CPU performance capture
adb wait-for-device
adb shell input keyevent KEYCODE_WAKEUP

:: Old unity profiler deprecated setup
:: "C:\Program Files\Unity\Hub\Editor\2022.3.31f1\Editor\Unity.exe" -batchmode -projectPath "E:\Automation\Profiler-Project" -executeMethod AutoProfiler.Record -logFile "E:\Automation\UNDERDOGS Bots Automation\Log Files\unity_profiler.log"


set "IL2CPPLAB_ROOT=/sdcard/Android/data/com.onehamsa.underdogs/files/il2cpplab"

echo starting the 30 second il2cpplab CPU capture
adb wait-for-device

adb shell "mkdir -p %IL2CPPLAB_ROOT% && chmod 2777 %IL2CPPLAB_ROOT%"
adb shell "echo cap 200 > %IL2CPPLAB_ROOT%/control.txt"
adb shell "echo start >> %IL2CPPLAB_ROOT%/control.txt"
adb shell "chmod 666 %IL2CPPLAB_ROOT%/control.txt"

ping 127.0.0.1 -n 31 >nul

echo stopping the il2cpplab capture
adb wait-for-device
adb shell "echo stop > %IL2CPPLAB_ROOT%/control.txt"
adb shell "chmod 666 %IL2CPPLAB_ROOT%/control.txt"
:: let the writer flush and close the session files before the game is force-stopped
ping 127.0.0.1 -n 4 >nul

:: Ask the game for an in-game report. The flush is the point, not the upload: ZLogger's
:: writer sleeps per entry (~10 entries/s in builds), so the capture window's log entries
:: are still queued when the game is force-stopped below and die with it. The report drains
:: that backlog onto disk, so the "Logs" pull further down gets a complete session and the
:: profiler can place entries on real frames. Capped at 5s inside the game, hence this wait.
echo    asking the game to report (flushes the log backlog to disk)...
adb shell "echo report > %IL2CPPLAB_ROOT%/control.txt"
adb shell "chmod 666 %IL2CPPLAB_ROOT%/control.txt"
ping 127.0.0.1 -n 16 >nul
:: shows up in the log when the capture root is wrong (internal storage) or the build isn't a tracking build
echo il2cpplab sessions on the device:
adb shell "ls -l %IL2CPPLAB_ROOT%"

echo    Taking screenshot 2 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_2.png

ping 127.0.0.1 -n 21 >nul

echo    Taking screenshot 3 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_3.png

ping 127.0.0.1 -n 6 >nul

:: ************************************************   6. STOPPING GAME   ************************************************
echo ...
echo [6/10] Stopping Game...
echo ...

adb wait-for-device
adb shell am force-stop com.onehamsa.underdogs

:: Signal that the game has stopped
echo stopped > "%SYNC_DIR%\GAME_STOPPED"

ping 127.0.0.1 -n 6 >nul

:: ************************************************   7. CLOSING OVR METRICS   ************************************************
echo ...
echo [7/10] Closing OVR metrics tool ...
echo ...

adb wait-for-device
adb shell am force-stop com.oculus.ovrmonitormetricsservice

ping 127.0.0.1 -n 6 >nul

:: ************************************************   9. DOWNLOADING THE CSV REPORT AND SCREENSHOT   ************************************************
echo ...
echo [9/10] Finding CSV report and downloading screenshot...
echo ...

adb wait-for-device
for /f "delims=" %%F in ('adb shell "ls -t %REMOTE_PATH% | head -n 1"') do set "LATEST_FILE=%%F"

if "%LATEST_FILE%"=="" (
    echo    ERROR: No CSV file found!
) else (
    echo    Found: %LATEST_FILE%
adb pull "%REMOTE_PATH%/%LATEST_FILE%" "%CURRENT_TEST_DIR%\CSV_REPORT.csv")

:: Download the screenshots from the headset, then delete them
echo    Downloading screenshots...
adb wait-for-device
adb pull /sdcard/AUTOMATION_SCREENSHOT_1.png "%CURRENT_TEST_DIR%\SCREENSHOT_1.png"
adb shell rm /sdcard/AUTOMATION_SCREENSHOT_1.png
adb pull /sdcard/AUTOMATION_SCREENSHOT_2.png "%CURRENT_TEST_DIR%\SCREENSHOT_2.png"
adb shell rm /sdcard/AUTOMATION_SCREENSHOT_2.png
adb pull /sdcard/AUTOMATION_SCREENSHOT_3.png "%CURRENT_TEST_DIR%\SCREENSHOT_3.png"
adb shell rm /sdcard/AUTOMATION_SCREENSHOT_3.png

ping 127.0.0.1 -n 4 >nul

:: Pull the il2cpplab capture session, then delete it so the next run starts clean (the
:: recorder never removes old sessions itself - the disk cap only bounds the live one).
:: "C++ Profiler" holds the capture files and "C++ Profiler\Symbol Parser" the sites.db +
:: symbols needed to read them. Both are inputs to the parse step below, which replaces
:: them with a single il2cpplab.db.zip - that zip is what gets kept and uploaded.
set "IL2CPPLAB_LOCAL_DIR=%CURRENT_TEST_DIR%\C++ Profiler"
echo    Pulling il2cpplab capture from headset...
adb wait-for-device
adb pull "%IL2CPPLAB_ROOT%" "%IL2CPPLAB_LOCAL_DIR%"
if errorlevel 1 (
    echo    WARNING: no il2cpplab capture pulled - check that this is a perf_tracking build.
) else (
    adb shell "rm -rf %IL2CPPLAB_ROOT%"
)

:: adb pull keeps the recorder's per-session subfolder; lift its files up so the capture
:: sits directly in "C++ Profiler" - one run records one session, and il2cpplab parses a
:: flat capture folder.
for /d %%S in ("%IL2CPPLAB_LOCAL_DIR%\*") do (
    echo    Flattening capture session %%~nxS...
    robocopy "%%~fS" "%IL2CPPLAB_LOCAL_DIR%" /E /MOVE /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 echo    WARNING: flattening the capture session failed.
)

:: sites.db + libil2cpp.so from the Build_<code>_Profiler_Symbols artifact of the exact
:: build under test. il2cpplab refuses a capture whose build id doesn't match sites.db,
:: so the capture cannot be parsed without them.
:: IL2CPPLAB_SYMBOLS_DIR is set by the workflow, which downloads the artifact.
if defined IL2CPPLAB_SYMBOLS_DIR (
    if exist "%IL2CPPLAB_SYMBOLS_DIR%" (
        echo    Copying il2cpplab symbols from %IL2CPPLAB_SYMBOLS_DIR%...
        robocopy "%IL2CPPLAB_SYMBOLS_DIR%" "%IL2CPPLAB_LOCAL_DIR%\Symbol Parser" /E /NFL /NDL /NJH /NJS /NP >nul
        :: robocopy exit codes 0-7 are success (copied / nothing to do); 8+ is a real failure
        if errorlevel 8 echo    WARNING: copying the il2cpplab symbols failed.
    ) else (
        echo    WARNING: IL2CPPLAB_SYMBOLS_DIR is set but does not exist: %IL2CPPLAB_SYMBOLS_DIR%
    )
) else (
    echo    No IL2CPPLAB_SYMBOLS_DIR set - the capture cannot be parsed.
)

:: Pull the game logs folder from the headset into "Report Logs"
echo    Pulling game logs from headset...
adb wait-for-device
adb pull /sdcard/Android/data/com.onehamsa.underdogs/files/Logs "%CURRENT_TEST_DIR%\Report Logs"
if errorlevel 1 (
    echo Trying alternative path...
    adb pull /data/user/0/com.onehamsa.underdogs/files/Logs "%CURRENT_TEST_DIR%\Report Logs"
)

:: Dump the device log next to quest_output.log. Read at the end rather than streamed:
:: the game is already force-stopped, so the ring holds the whole run and there is no
:: background process to orphan. Overwritten each run, like the other Log Files entries.
echo    Dumping logcat to "%LOG_FILES_DIR%\logcat.log"...
if not exist "%LOG_FILES_DIR%" mkdir "%LOG_FILES_DIR%"
adb wait-for-device
adb logcat -b all -d -v threadtime > "%LOG_FILES_DIR%\logcat.log" 2>&1
if errorlevel 1 (
    echo    WARNING: logcat dump failed.
) else (
    for %%L in ("%LOG_FILES_DIR%\logcat.log") do echo    logcat.log: %%~zL bytes
)

:: parse and zip the profiler recording. Runs after the game logs are pulled: the parse
:: finds the run's log session in "Report Logs" beside the capture and attaches it into the
:: db, which is where the viewer's Logs tab reads it from - the raw log folder is uploaded
:: separately and is not what the profiler UI reads.
if defined IL2CPPLAB_TOOL_DIR (
    python "%~dp0..\Analysis\parse_il2cpplab.py" "%IL2CPPLAB_LOCAL_DIR%" "%IL2CPPLAB_TOOL_DIR%"
    if errorlevel 1 echo    WARNING: il2cpplab parse failed - uploading the raw capture instead.
) else (
    echo    No IL2CPPLAB_TOOL_DIR set - uploading the raw capture instead of a parsed db.
)


:: ************************************************   10. GENERATE GRAPH AND UPLOAD FILES   ************************************************
echo ...
echo [10/10] Generating App CPU Time graph...
echo ...

python "%~dp0UploadFiles.py" "%CURRENT_TEST_DIR%" "%DRIVE_FOLDER_NAME%" --started-by "%STARTED_BY%" --num-pc-bots "%NUM_PC_BOTS%" --commit-sha "%COMMIT_SHA%" --commit-ref "%COMMIT_REF%" --github-token "%AUTOMATION_REPOS_PAT%"

:: ************************************************    RESETTING EVERYTHING BACK AGAIN   ************************************************

echo ...
echo putting the headset in sleep mode and enabling proximity censor again
echo ...

:: reset performance locks to default
adb shell setprop debug.oculus.cpuLevel -1
adb shell setprop debug.oculus.gpuLevel -1

adb shell setprop debug.oculus.foveation.dynamic 1
adb shell setprop debug.oculus.foveation.level -1

::enable the guardian again, enable the proximity censor and put the headset in sleep mode:
adb wait-for-device
adb shell setprop debug.oculus.guardian_pause 0
adb shell am broadcast -a com.oculus.vrpowermanager.automation_disable
adb shell input keyevent KEYCODE_SLEEP


:: Signal that the Quest test is fully done
echo done > "%SYNC_DIR%\QUEST_DONE"

echo ========================================================
echo                  TEST COMPLETE
echo    Files saved locally in: %CURRENT_TEST_DIR%
echo    Files saved in google drive in: %DRIVE_FOLDER_NAME%
echo ========================================================
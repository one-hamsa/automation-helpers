@echo off
setlocal EnableDelayedExpansion

:: Force the project's Unity adb (2022.3.31f1) to the front of PATH so we don't pick up a different
:: Unity install's adb (e.g. 6000.x) and end up fighting over the adb server. ANDROID_SDK_ROOT/HOME
:: point renderdoccmd at the SAME SDK (belt-and-suspenders alongside renderdoc.conf's SDKDirPath).
set "ANDROID_SDK_ROOT=C:\Program Files\Unity\Hub\Editor\2022.3.31f1\Editor\Data\PlaybackEngines\AndroidPlayer\SDK"
set "ANDROID_HOME=%ANDROID_SDK_ROOT%"
set "PATH=%ANDROID_SDK_ROOT%\platform-tools;%PATH%"

:: --- CONFIGURATION START ---
set "ROOT_DIR=E:\Automation\UNDERDOGS Scene Test Automation\Tests Data"
set "REMOTE_PATH=/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics"
:: RenderDoc Meta fork (bundled with Quest Developer Hub). %APPDATA% avoids hardcoding the username.
set "RENDERDOC_CMD=%APPDATA%\odh\packages\tools\renderdoc-oculus\renderdoccmd.exe"
:: --- CONFIGURATION END ---

:: we assign the variable to the parameter passed
set "DRIVE_FOLDER_NAME=%~1"
set "STARTED_BY=%~2"
if not defined STARTED_BY set "STARTED_BY=unknown"
if "%STARTED_BY%"=="" set "STARTED_BY=unknown"
echo The parameter we received is: "%DRIVE_FOLDER_NAME%"
echo Test started by: "%STARTED_BY%"

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

if not defined DRIVE_FOLDER_NAME set "DRIVE_FOLDER_NAME=GPU TEST - Name(-) - On Scene(-) - Started at(%TIMESTAMP%)"
if "%DRIVE_FOLDER_NAME%"=="" set "DRIVE_FOLDER_NAME=GPU TEST - Name(-) - On Scene(-) - Started at(%TIMESTAMP%)"
if "%DRIVE_FOLDER_NAME%"==" " set "DRIVE_FOLDER_NAME=GPU TEST - Name(-) - On Scene(-) - Started at(%TIMESTAMP%)"

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

:: Ensure ADB daemon is running and device is connected before doing anything
adb start-server >nul 2>&1
echo Waiting for ADB device...
adb wait-for-device

:: Verify device is actually reachable
adb shell echo ok >nul 2>&1
if errorlevel 1 (
    echo ERROR: ADB device not responding. Aborting test.
    exit /b 1
)
echo ADB device connected.

::wake up the headset, disable the proximity censor and disable the guardian
adb wait-for-device
adb shell input keyevent KEYCODE_WAKEUP
adb shell am broadcast -a com.oculus.vrpowermanager.prox_close
adb shell setprop debug.oculus.guardian_pause 1


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
echo [1/9] Enabling GPU profiler and restarting OVR metrics tool...
echo ...

:: Force-stop OVR Metrics Tool to start clean
adb wait-for-device
adb shell am force-stop com.oculus.ovrmonitormetricsservice
ping 127.0.0.1 -n 3 >nul

:: Enable the GPU profiling service property FIRST, before starting OMMS
adb wait-for-device
adb shell setprop debug.vr.gpuprofilingservice 1

:: Enable detailed GPU profiling mode (must happen before the app starts)
adb shell ovrgpuprofiler -e
ping 127.0.0.1 -n 2 >nul

:: Now start OVR Metrics Tool AFTER profiling is fully configured
adb wait-for-device
adb shell am start omms://app
ping 127.0.0.1 -n 5 >nul

:: ************************************************   2. LAUNCHING GAME (metrics phase - NO RenderDoc)   ************************************************
echo ...
echo [2/9] Launching Underdogs normally for the metrics run...
echo ...

:: PHASE 1 is a normal, RenderDoc-free run. The Oculus GPU profiler (enabled in step 1) and RenderDoc's
:: capture layer both hook the GPU driver and cannot coexist: RenderDoc disables ovrgpuprofiler on inject,
:: which starves the OVR Metrics Tool of App GPU Time data. So metrics come from this clean launch; the
:: RenderDoc capture happens in a SEPARATE launch afterwards (phase 2, step 7).
adb wait-for-device
adb shell am start -n com.onehamsa.underdogs/com.unity3d.player.UnityPlayerActivity

::sometimes a few seconds after opening a menu pops up, so we focus on the app again
ping 127.0.0.1 -n 6 >nul
adb wait-for-device
adb shell monkey -p com.onehamsa.underdogs -c android.intent.category.LAUNCHER 1

:: ************************************************   3. WAITING FOR THE GAME TO LOAD   ************************************************
echo ...
echo [3/9] Waiting a minute for the game to fully load...
echo ...

ping 127.0.0.1 -n 61 >nul

:: ************************************************ 4. LOCKING HARDWARE PERFORMANCE   ************************************************
echo ...
echo [4/9] Forcing OS-level performance locks...
echo ...

adb wait-for-device
:: Lock CPU and GPU to level 3 (Sustained High) to prevent frequency bouncing
adb shell setprop debug.oculus.cpuLevel 3
adb shell setprop debug.oculus.gpuLevel 3

:: Turn off dynamic foveation and lock the foveation level
adb shell setprop debug.oculus.foveation.dynamic 0
adb shell setprop debug.oculus.foveation.level 3

:: Give the OS a few seconds to apply the locks
ping 127.0.0.1 -n 4 >nul

:: ************************************************   5. RECORDING PERFORMANCE   ************************************************
echo ...
echo [5/9] Running game and taking screenshots...
echo ...

:: take a screenshot, Wait 1 minute, take a screenshot again, wait a minute, and take a screenshot at the end.

echo    Taking screenshot 1 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_1.png

ping 127.0.0.1 -n 31 >nul

echo    Taking screenshot 2 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_2.png

ping 127.0.0.1 -n 31 >nul

echo    Taking screenshot 3 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_3.png

:: ************************************************   6. END METRICS RUN: STOP OVR + GAME, PULL DATA   ************************************************
echo ...
echo [6/9] Stopping OVR metrics and game, then pulling CSV / screenshots / logs...
echo ...

:: Phase 1 is done: stop the metrics tool (ends CSV recording) and the game.
adb wait-for-device
adb shell am force-stop com.oculus.ovrmonitormetricsservice
ping 127.0.0.1 -n 3 >nul
adb shell am force-stop com.onehamsa.underdogs
ping 127.0.0.1 -n 6 >nul

:: --- CSV report ---
adb wait-for-device
for /f "delims=" %%F in ('adb shell "ls -t %REMOTE_PATH% | head -n 1"') do set "LATEST_FILE=%%F"
if "%LATEST_FILE%"=="" (
    echo    ERROR: No CSV file found!
) else (
    echo    Found: %LATEST_FILE%
    adb pull "%REMOTE_PATH%/%LATEST_FILE%" "%CURRENT_TEST_DIR%\CSV_REPORT.csv"
)

:: --- Screenshots (pull, then delete from headset) ---
echo    Downloading screenshots...
adb wait-for-device
adb pull /sdcard/AUTOMATION_SCREENSHOT_1.png "%CURRENT_TEST_DIR%\SCREENSHOT_1.png"
adb shell rm /sdcard/AUTOMATION_SCREENSHOT_1.png
adb pull /sdcard/AUTOMATION_SCREENSHOT_2.png "%CURRENT_TEST_DIR%\SCREENSHOT_2.png"
adb shell rm /sdcard/AUTOMATION_SCREENSHOT_2.png
adb pull /sdcard/AUTOMATION_SCREENSHOT_3.png "%CURRENT_TEST_DIR%\SCREENSHOT_3.png"
adb shell rm /sdcard/AUTOMATION_SCREENSHOT_3.png

ping 127.0.0.1 -n 4 >nul

:: --- Game logs ---
:: Pull the whole Logs folder into a NON-existent "Report Logs" so adb renames it to the destination
:: (contents land directly in "Report Logs\<session>\Global.json.log"). Pre-creating the dir or appending
:: "/." makes newer/RenderDoc-forked adb pull nothing. The /sdcard path is the accessible one; the
:: /data/user fallback needs root (usually denied).
echo    Pulling game logs from headset...
adb wait-for-device
adb pull /sdcard/Android/data/com.onehamsa.underdogs/files/Logs "%CURRENT_TEST_DIR%\Report Logs"
if errorlevel 1 (
    echo Trying alternative path...
    adb pull /data/user/0/com.onehamsa.underdogs/files/Logs "%CURRENT_TEST_DIR%\Report Logs"
)

ping 127.0.0.1 -n 4 >nul

:: ************************************************   7. RENDERDOC CAPTURE (phase 2 - separate launch)   ************************************************
echo ...
echo [7/9] RenderDoc phase: relaunching under RenderDoc, waiting a minute, then capturing one frame...
echo ...

:: Recreate the conditions a manual RenderDoc run has: RenderDoc must be the ONLY thing hooking the GPU.
:: Turn the Oculus profiler back off (the metrics phase enabled it) so nothing competes with the capture layer.
adb wait-for-device
adb shell ovrgpuprofiler -d
adb shell setprop debug.vr.gpuprofilingservice 0

:: Grab the device serial for the RenderDoc launch/capture commands.
for /f "delims=" %%s in ('adb get-serialno') do set "SERIAL=%%s"
echo Device serial: %SERIAL%

:: RenderDoc can only capture an app it LAUNCHED itself (no attach-to-running on Quest). Requires a
:: DEVELOPMENT build (android:debuggable=true). adb-launch force-stops, relaunches with the capture
:: layer injected, and returns an "ident" used by the capture below.
set "RD_LAUNCH_LOG=%CURRENT_TEST_DIR%\renderdoc_launch.json"
"%RENDERDOC_CMD%" adb-launch --device %SERIAL% --package com.onehamsa.underdogs --skip-controller-check > "%RD_LAUNCH_LOG%" 2>&1
type "%RD_LAUNCH_LOG%"

:: Extract the ident (the "ident": <number> field) from the JSON, robust to log noise and field order.
:: Written to a paren-free temp file so the test folder's parentheses can't break for /f parsing.
set "RD_IDENT="
powershell -NoProfile -Command "$m=[regex]::Match((Get-Content -Raw '%RD_LAUNCH_LOG%'),([char]34+'ident'+[char]34+'\s*:\s*(\d+)')); if($m.Success){$m.Groups[1].Value}" > "%TEMP%\rd_ident.txt"
set /p RD_IDENT=<"%TEMP%\rd_ident.txt"
echo Parsed RenderDoc ident: "%RD_IDENT%"

:: Keep the headset "worn"/awake for the WHOLE wait + capture. renderdoccmd's own prox_close lasts only
:: ~30s; once it expires during the long wait the app loses VR focus and PauseMenu opens (a spectator can't
:: auto-unpause, so the pause then sticks into the capture). A long-duration prox_close prevents that.
adb shell input keyevent KEYCODE_WAKEUP
adb shell am broadcast -a com.oculus.vrpowermanager.prox_close --ei duration 300000

:: Wait one minute for the scene to render, then capture one frame. With the debug scene-loader no longer
:: loading Joint a second time (skipLoad for MP), the spectator brings the scene up quickly, so a short wait
:: is enough now (earlier long waits were compensating for the double-load stall, not RenderDoc slowness).
ping 127.0.0.1 -n 61 >nul

if not defined RD_IDENT (
    echo    WARNING: No RenderDoc ident parsed at launch - skipping capture. Check renderdoc_launch.json ^(likely not a development/debuggable build^).
) else (
    echo    Capturing frame with ident %RD_IDENT%...
    "%RENDERDOC_CMD%" adb-capture --device %SERIAL% --ident %RD_IDENT% --frames 1 --output-dir "%CURRENT_TEST_DIR%"
)

:: Stop the game now that the capture is done.
adb wait-for-device
adb shell am force-stop com.onehamsa.underdogs
ping 127.0.0.1 -n 6 >nul

:: Pull the RenderDoc-phase game logs too. The step-6 pull happened BEFORE this relaunch, so it only has the
:: metrics phase; without this we can't see why the RenderDoc capture landed on "Connecting" vs in-scene.
:: Separate folder so it doesn't collide with the metrics-phase "Report Logs".
adb wait-for-device
adb pull /sdcard/Android/data/com.onehamsa.underdogs/files/Logs "%CURRENT_TEST_DIR%\Report Logs RenderDoc"
if errorlevel 1 (
    echo Trying alternative path...
    adb pull /data/user/0/com.onehamsa.underdogs/files/Logs "%CURRENT_TEST_DIR%\Report Logs RenderDoc"
)

:: ************************************************   8. GENERATE GRAPH AND UPLOAD FILES   ************************************************
echo ...
echo [8/9] Generating App GPU Time graph and uploading files...
echo ...

python "%~dp0UploadFiles.py" "%CURRENT_TEST_DIR%" "%DRIVE_FOLDER_NAME%" --started-by "%STARTED_BY%" --github-token "%UPLOAD_TO_AUTOMATION_REPOS_PAT%"

:: ************************************************    RESETTING EVERYTHING BACK AGAIN   ************************************************

echo ...
echo [9/9] Putting the headset in sleep mode and enabling proximity censor again
echo ...

:: reset performance locks to default
adb shell setprop debug.oculus.cpuLevel -1
adb shell setprop debug.oculus.gpuLevel -1

adb shell setprop debug.oculus.foveation.dynamic 1
adb shell setprop debug.oculus.foveation.level -1

adb shell setprop debug.vr.gpuprofilingservice 0

::enable the guardian again, enable the proximity censor and put the headset in sleep mode:
adb wait-for-device
adb shell setprop debug.oculus.guardian_pause 0
adb shell am broadcast -a com.oculus.vrpowermanager.automation_disable
adb shell input keyevent KEYCODE_SLEEP


echo ========================================================
echo                  TEST COMPLETE
echo    Files saved locally in: %CURRENT_TEST_DIR%
echo    Files saved in google drive in: %DRIVE_FOLDER_NAME%
echo ========================================================
pause

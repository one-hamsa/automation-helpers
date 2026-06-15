@echo off
setlocal EnableDelayedExpansion

:: --- CONFIGURATION START ---
set "ROOT_DIR=C:\Automation\UNDERDOGS Scene Test Automation\Tests Data"
set "REMOTE_PATH=/sdcard/Android/data/com.oculus.ovrmonitormetricsservice/files/CapturedMetrics"
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
echo [1/10] Enabling GPU profiler and restarting OVR metrics tool...
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

:: ************************************************   2. LAUNCHING GAME   ************************************************
echo ...
echo [2/10] Launching Underdogs...
echo ...

adb wait-for-device
adb shell am start -n com.onehamsa.underdogs/com.unity3d.player.UnityPlayerActivity

::sometimes a few seconds after opening a menu pops up, so we focus on the app again

ping 127.0.0.1 -n 6 >nul

adb wait-for-device
adb shell monkey -p com.onehamsa.underdogs -c android.intent.category.LAUNCHER 1

:: ************************************************   3. WAITING FOR THE GAME TO LOAD   ************************************************
echo ...
echo [3/10] Waiting a minute for the game to fully load...
echo ...

ping 127.0.0.1 -n 61 >nul

:: ************************************************ 4. LOCKING HARDWARE PERFORMANCE   ************************************************
echo ...
echo [4/10] Forcing OS-level performance locks...
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
echo [5/10] Running game...
echo ...

:: take a screenshot, Wait 1 minute, take a screenshot again, wait a minute, and take a screenshot at the end.

echo    Taking screenshot 1 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_1.png

ping 127.0.0.1 -n 61 >nul

echo    Taking screenshot 2 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_2.png

ping 127.0.0.1 -n 61 >nul

echo    Taking screenshot 3 from headset...
adb wait-for-device
adb shell screencap -p /sdcard/AUTOMATION_SCREENSHOT_3.png

:: ************************************************   6. STOPPING GAME   ************************************************
echo ...
echo [6/10] Stopping Game...
echo ...

adb wait-for-device
adb shell am force-stop com.onehamsa.underdogs

ping 127.0.0.1 -n 6 >nul

:: ************************************************   7. CLOSING OVR METRICS   ************************************************
echo ...
echo [7/10] Closing OVR metrics tool ...
echo ...

adb wait-for-device
adb shell am force-stop com.oculus.ovrmonitormetricsservice

ping 127.0.0.1 -n 21 >nul

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


:: ************************************************   10. GENERATE GRAPH AND UPLOAD FILES   ************************************************
echo ...
echo [10/10] Generating App GPU Time graph...
echo ...

python "%~dp0UploadFiles.py" "%CURRENT_TEST_DIR%" "%DRIVE_FOLDER_NAME%" --started-by "%STARTED_BY%"

:: ************************************************    RESETTING EVERYTHING BACK AGAIN   ************************************************

echo ...
echo putting the headset in sleep mode and enabling proximity censor again
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

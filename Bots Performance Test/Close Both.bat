@echo off
setlocal EnableDelayedExpansion

:: Idempotent safety-net closer for the Bots Performance Test.
:: Safe to run multiple times, and safe even if the test never started.
:: Called both at the normal end of a run and (crucially) by the workflow's
:: always() "close" job, so the headset/PC are closed even when the test is
:: killed by the 15-minute timeout or crashes mid-run.
::
:: Exit codes (read by the workflow to drive the Discord alert):
::   0 = fully closed   (PC instances killed AND headset put to sleep)
::   2 = PC handled but HEADSET UNREACHABLE over adb (could not confirm sleep)

set "EXE_NAME=Underdogs.exe"
set "BOTS_DATA_FILE=%USERPROFILE%\AppData\LocalLow\One Hamsa\UNDERDOGS\Bots_Local_Data.txt"
set "SYNC_DIR=%TEMP%\underdogs_bot_sync"

echo ========================================================
echo        CLOSE BOTH (safety-net cleanup)
echo ========================================================

:: --- 1. PC side: always works locally, no adb needed ---
echo Killing any running PC bot instances (%EXE_NAME%)...
taskkill /F /IM %EXE_NAME% 2>nul
if errorlevel 1 (
    echo   No running %EXE_NAME% instances found.
) else (
    echo   Killed running %EXE_NAME% instance^(s^).
)

:: Reset bots availability so all slots are free for the next run
if exist "%BOTS_DATA_FILE%" (
    echo Resetting bots availability in "%BOTS_DATA_FILE%"...
    powershell -NoProfile -Command "(Get-Content -LiteralPath '%BOTS_DATA_FILE%') -replace '/\s*no\s*$', '/ yes' | Set-Content -LiteralPath '%BOTS_DATA_FILE%'"
) else (
    echo   "%BOTS_DATA_FILE%" not found - skipping reset.
)

:: --- 2. Quest side: only if the headset is reachable over adb ---
:: NOTE: deliberately NO "adb wait-for-device" here - that blocks forever when
:: the headset is gone/unauthorized, which is exactly the failure we must survive.
echo Checking headset reachability over adb...
adb start-server >nul 2>&1
adb shell echo ok >nul 2>&1
if errorlevel 1 (
    echo   WARNING: Headset is UNREACHABLE over adb - cannot confirm sleep.
    set "HEADSET_OK=0"
) else (
    echo   Headset reachable. Resetting performance locks and sleeping...
    adb shell setprop debug.oculus.cpuLevel -1
    adb shell setprop debug.oculus.gpuLevel -1
    adb shell setprop debug.oculus.foveation.dynamic 1
    adb shell setprop debug.oculus.foveation.level -1
    adb shell setprop debug.vr.gpuprofilingservice 0
    adb shell setprop debug.oculus.enableVideoCapture 0
    adb shell setprop debug.oculus.guardian_pause 0
    adb shell am broadcast -a com.oculus.vrpowermanager.automation_disable
    adb shell input keyevent KEYCODE_SLEEP
    set "HEADSET_OK=1"
)

:: --- 3. Clear the sync dir so a half-finished run can't confuse the next one ---
:: The ADB daemon goes first - it holds quest_output.log in there open, so the delete
:: fails while it is alive and the next run cannot start its Quest half at all. Placed
:: below the headset commands above, which need a live daemon.
adb kill-server >nul 2>&1
if exist "%SYNC_DIR%" rd /s /q "%SYNC_DIR%" >nul 2>&1

if "!HEADSET_OK!"=="0" (
    echo ========================================================
    echo   CLOSE INCOMPLETE: headset may still be ON
    echo ========================================================
    exit /b 2
)

echo ========================================================
echo   CLOSE COMPLETE: PC killed, headset asleep
echo ========================================================
exit /b 0

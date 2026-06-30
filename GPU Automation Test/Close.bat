@echo off
setlocal EnableDelayedExpansion

:: Idempotent safety-net closer for the GPU Automation Test.
:: Safe to run multiple times, and safe even if the test never started.
:: Called both at the normal end of a run and (crucially) by the workflow's
:: always() "close" job, so the headset is closed even when the test is killed
:: by the timeout or crashes mid-run.
::
:: Exit codes (read by the workflow to drive the Discord alert):
::   0 = headset put to sleep
::   2 = HEADSET UNREACHABLE over adb (could not confirm sleep)

echo ========================================================
echo        CLOSE (GPU test safety-net cleanup)
echo ========================================================

:: NOTE: deliberately NO "adb wait-for-device" here - that blocks forever when
:: the headset is gone/unauthorized, which is exactly the failure we must survive.
echo Checking headset reachability over adb...
adb start-server >nul 2>&1
adb shell echo ok >nul 2>&1
if errorlevel 1 (
    echo   WARNING: Headset is UNREACHABLE over adb - cannot confirm sleep.
    echo ========================================================
    echo   CLOSE INCOMPLETE: headset may still be ON
    echo ========================================================
    exit /b 2
)

echo   Headset reachable. Resetting performance locks and sleeping...
adb shell setprop debug.oculus.cpuLevel -1
adb shell setprop debug.oculus.gpuLevel -1
adb shell setprop debug.oculus.foveation.dynamic 1
adb shell setprop debug.oculus.foveation.level -1
adb shell setprop debug.vr.gpuprofilingservice 0
adb shell setprop debug.oculus.guardian_pause 0
adb shell am broadcast -a com.oculus.vrpowermanager.automation_disable
adb shell input keyevent KEYCODE_SLEEP

echo ========================================================
echo   CLOSE COMPLETE: headset asleep
echo ========================================================
exit /b 0

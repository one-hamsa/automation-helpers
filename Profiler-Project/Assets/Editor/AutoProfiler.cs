using System;
using System.IO;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.Hardware;
using UnityEditorInternal;

public static class AutoProfiler
{
    private static string ProfilerTestResultPath = "E:/Automation/Profiler_Test_Result";
    private static string ProfilerTestResultSavePath = "E:/Automation/Profiler_Test_Result/ProfilerRecording.raw";

    // the app we profile on the headset - used to address its player-connection socket by package name
    private const string GamePackageName = "com.onehamsa.underdogs";

    private static float MaxRecordingDuration;
    private static double _discoveryStartTime;
    private static double _recordStartTime;

    private static double DeviceConnectedTime;
    private static double _lastConnectAttemptTime;
    private static double LoopHelper;
    private static string ConnectedQuestName;
    private static bool SavedAlready;

    private static string CurrentConnectedDevice;

    private static bool recorded;

    // THE CMD COMMAND TO RUN THIS SCRIPT IN HEADLESS MODE IS:

    //"C:\Program Files\Unity\Hub\Editor\2022.3.31f1\Editor\Unity.exe" -batchmode -projectPath "D:\Profiler-Project" -executeMethod AutoProfiler.Record -logFile "D:\UNDERDOGS Bots Automation\Log Files\unity_profiler.log"


    // FIRST ARGUMENT IS WHERE THE UNITY.EXE IS LOCATED(MAKE SURE THIS IS THE SAME VERSION AS THE GAME WE RUN)
    // SECOND ARGUMENT IS SO THAT THE PROJECT WOULD RUN HEADLESS(IF YOU WANT TO SEE IT OPEN AND RUN REMOVE THIS FLAG)
    // THIRD ARGUMENT IS PROJECT PATH
    // FORTH ARGUMENT IS (CLASS_NAME.METHOD_NAME)
    // FIFTH ARGUMENT IS WHERE TO WRITE THE LOGS TO

    public static void Record()
    {
        EditorApplication.quitting += SaveRecordingData;

        ProfilerDriver.ClearAllFrames();

        // we setup the profiler:
        //ProfilerDriver.memoryRecordMode = ProfilerMemoryRecordMode.GCAlloc;
        //if in the future i want to get more memory trace calls add:
        //ProfilerDriver.memoryRecordMode =
        //    ProfilerMemoryRecordMode.UnsafeUtilityMalloc |
        //    ProfilerMemoryRecordMode.JobHandleComplete |
        //    ProfilerMemoryRecordMode.NativeAlloc;

        ProfilerDriver.profileGPU = false;
        ProfilerDriver.deepProfiling = false;


        //reset all vars:

        ConnectedQuestName = "";
        CurrentConnectedDevice = "";
        DeviceConnectedTime = 0;
        LoopHelper = EditorApplication.timeSinceStartup;
        MaxRecordingDuration = 20; // we record max of 20 seconds(about to capture about 800 frames even at low frame rate)
        SavedAlready = false;
        recorded = false;

        Console.WriteLine("[AutoProfiler] Starting the profiler...");

        _discoveryStartTime = EditorApplication.timeSinceStartup;
        EditorApplication.update += WaitForDevice;
    }

    private static void WaitForDevice()
    {

        if (EditorApplication.timeSinceStartup - LoopHelper > 0.5)
        {
            LoopHelper = EditorApplication.timeSinceStartup;

            bool IsConnected = TryToConnectToQuest();

            if (IsConnected) // if we connected we continue to recording
            {
                EditorApplication.update -= WaitForDevice;

                DeviceConnectedTime = EditorApplication.timeSinceStartup;
                LoopHelper = EditorApplication.timeSinceStartup;
                _lastConnectAttemptTime = EditorApplication.timeSinceStartup; // WaitForDevice already issued one DirectURLConnect


                EditorApplication.update += WaitForConnectionHandshake;

                return;
            }
        }


        if (EditorApplication.timeSinceStartup - _discoveryStartTime >= 30)
        {
            EditorApplication.update -= WaitForDevice;
            Console.WriteLine($"[AutoProfiler] Timed out, took us more then 30 seconds to find the quest device");

            LogConnectionDiagnostics();
            EditorApplication.Exit(1);
        }
    }

    private static void WaitForConnectionHandshake()
    {

        if (EditorApplication.timeSinceStartup - LoopHelper > 0.5) // we check the connection status every 0.5 seconds
        {
            LoopHelper = EditorApplication.timeSinceStartup;
            CurrentConnectedDevice = ProfilerDriver.GetConnectionIdentifier(ProfilerDriver.connectedProfiler);


            Console.WriteLine($"[AutoProfiler] Waiting for the profiler handshake to complete ... Currently connected to: {CurrentConnectedDevice}");

            // when connecting by IP the identifier is "Autoconnected Player" (device:// gives
            // "AndroidPlayer(...)"), so treat anything except the local "Editor" as connected
            if (!string.IsNullOrEmpty(CurrentConnectedDevice) && CurrentConnectedDevice != "Editor")
            {
                // The connect call works asynchronously: a retry issued just before the handshake
                // completed can still be in flight, and when it lands it RESETS the connection we
                // just got — which would kill the recording ~1 second in. So only start recording
                // once the connection has survived a moment past the last connect attempt.
                if (EditorApplication.timeSinceStartup - _lastConnectAttemptTime < 1.5)
                {
                    Console.WriteLine($"[AutoProfiler] Connected, but a connect retry might still be in flight - confirming the connection is stable...");
                    return;
                }

                EditorApplication.update -= WaitForConnectionHandshake; // Stop polling
                ConnectedQuestName = CurrentConnectedDevice; // RecordTimer checks we stay connected to this exact player
                Console.WriteLine($"[AutoProfiler] still connected to the quest and ready to start the test");
                ExecuteActualRecording();
                return;
            }

            //we try to connect every 3 secpnds until the connection works or we timeout
            if (EditorApplication.timeSinceStartup - _lastConnectAttemptTime >= 3)
            {
                _lastConnectAttemptTime = EditorApplication.timeSinceStartup;
                TryToConnectToQuest();
            }

            // Timeout check (30 seconds)
            if (EditorApplication.timeSinceStartup - DeviceConnectedTime > 30)
            {
                EditorApplication.update -= WaitForConnectionHandshake;
                Console.WriteLine("[AutoProfiler] Took the device over 30 seconds to connect, aborting the test.");
                LogConnectionDiagnostics();
                EditorApplication.Exit(1);
            }
        }



    }

    private static void ExecuteActualRecording()
    {
        recorded = true;

        ProfilerDriver.enabled = true;

        _recordStartTime = EditorApplication.timeSinceStartup;
        DeviceConnectedTime = EditorApplication.timeSinceStartup;

        Console.WriteLine($"[AutoProfiler] Started recording for {MaxRecordingDuration} seconds or until the quest closes...");
        EditorApplication.update += RecordTimer;
    }

    private static void RecordTimer()
    {
        if (EditorApplication.timeSinceStartup - DeviceConnectedTime > 1f) // we check every second that the device we are connected to is the one we connected to at the start(the quest)
        {
            DeviceConnectedTime = EditorApplication.timeSinceStartup;
            CurrentConnectedDevice = ProfilerDriver.GetConnectionIdentifier(ProfilerDriver.connectedProfiler);

            if (ConnectedQuestName == CurrentConnectedDevice)
            {
                Console.WriteLine($"[AutoProfiler] Recording and still connected to the Quest! " +
                    $"recorded for {EditorApplication.timeSinceStartup - _recordStartTime} seconds and we have {MaxRecordingDuration - (EditorApplication.timeSinceStartup - _recordStartTime)} seconds to go");
            }
            else
            {
                Console.WriteLine($"[AutoProfiler] We lost connection to the quest mid test after {EditorApplication.timeSinceStartup - _recordStartTime} seconds");

                EditorApplication.update -= RecordTimer;
                SaveRecordingData();
                return;
            }


        }

        if (EditorApplication.timeSinceStartup - _recordStartTime >= MaxRecordingDuration)
        {
            EditorApplication.update -= RecordTimer;
            Console.WriteLine($"[AutoProfiler] Max recording duration reached after {EditorApplication.timeSinceStartup - _recordStartTime} seconds — saving...");
            SaveRecordingData();
        }
    }



    private static void SaveRecordingData()
    {
        //if we didn't even get to record or we already saved then we skip
        if (!recorded || SavedAlready)
            return;

        SavedAlready = true;

        // Unsubscribe so Exit(0) doesn't trigger a second save via quitting
        EditorApplication.quitting -= SaveRecordingData;

        Console.WriteLine("[AutoProfiler] Saving profile...");


        if (Directory.Exists(ProfilerTestResultPath))
        {
            System.IO.Directory.Delete(ProfilerTestResultPath, true);
        }

        System.IO.Directory.CreateDirectory(ProfilerTestResultPath);

        ProfilerDriver.SaveProfile(ProfilerTestResultSavePath);
        ProfilerDriver.enabled = false;

        Console.WriteLine("[AutoProfiler] Done.");
        EditorApplication.Exit(0);
    }

    // helpers:

    // When the connection fails, log WHY: the editor picks which app to profile by taking the
    // LAST "ACTIVITY <package>/..." entry from "adb shell dumpsys activity top" and forwarding
    // tcp:34998 to localabstract:Unity-<that package>. If the last resumed activity is not the
    // game (a system panel/dialog stole focus), the forward points at a socket that doesn't
    // exist and every handshake fails. These two dumps make that visible in the log.
    private static void LogConnectionDiagnostics()
    {
        Console.WriteLine("[AutoProfiler] ---- Connection diagnostics ----");

        Console.WriteLine("[AutoProfiler] adb forward --list (what is forwarded to the device):");
        Console.WriteLine(RunAdb("forward --list"));

        string topDump = RunAdb("shell dumpsys activity top");
        // same regex Unity's Android extension uses to pick the package it forwards to
        MatchCollection activities = Regex.Matches(topDump, "ACTIVITY ([a-zA-Z.0-9_-]+)/.+");
        if (activities.Count == 0)
        {
            Console.WriteLine("[AutoProfiler] dumpsys activity top returned no ACTIVITY entries");
        }
        else
        {
            foreach (Match activity in activities)
                Console.WriteLine($"[AutoProfiler] ACTIVITY: {activity.Groups[1].Value}");

            Console.WriteLine($"[AutoProfiler] Last resumed activity (the package Unity forwards to): " +
                $"{activities[activities.Count - 1].Groups[1].Value}");
        }

        Console.WriteLine("[AutoProfiler] --------------------------------");
    }

    private static string RunAdb(string arguments)
    {
        // use the adb that ships with this editor, the same one the profiler connection uses
        string adbPath = Path.Combine(
            Path.GetDirectoryName(EditorApplication.applicationPath),
            "Data", "PlaybackEngines", "AndroidPlayer", "SDK", "platform-tools", "adb.exe");

        if (!File.Exists(adbPath))
            adbPath = "adb"; // fall back to PATH (the bat puts the right adb first anyway)

        try
        {
            var startInfo = new System.Diagnostics.ProcessStartInfo
            {
                FileName = adbPath,
                Arguments = arguments,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };

            using (var process = System.Diagnostics.Process.Start(startInfo))
            {
                string output = process.StandardOutput.ReadToEnd();
                string error = process.StandardError.ReadToEnd();

                if (!process.WaitForExit(10000))
                {
                    process.Kill();
                    return "(adb timed out after 10 seconds)";
                }

                return (output + error).Trim();
            }
        }
        catch (Exception e)
        {
            return $"(failed to run adb {arguments}: {e.Message})";
        }
    }

    private static bool TryToConnectToQuest()
    {
        var devices = DevDeviceList.GetDevices();

        foreach (var device in devices)
        {
            bool supportsPlayerConnection = (device.features & DevDeviceFeatures.PlayerConnection) != 0;

            Console.WriteLine($"[AutoProfiler] Device_Name=\"{device.name}\" id=\"{device.id}\" type=\"{device.type}\" connected={device.isConnected} playerConn={supportsPlayerConnection}");

            //if the device is connected + supports the profiler connection + it's a quest device then it's what we are looking for
            if (device.isConnected && supportsPlayerConnection && ((device.name.Contains("Oculus")) || device.name.Contains("Quest")))
            {
                ConnectedQuestName = device.name;

                // Don't use DirectURLConnect("device://..."): it decides which app to forward to by
                // asking the device for the LAST RESUMED activity (dumpsys activity top), so any
                // system panel/dialog that pops over the game makes it forward to the wrong package
                // and the handshake fails. Instead we create the forward ourselves, addressed by
                // package NAME, and connect by IP - focus can't break this.
                // NOTE: local port must be 55000 - DirectIPConnect scans 55000-55063 (and 35000/4600),
                // it does NOT try the 34999 port from the manual-profiling docs (verified on 2022.3.31).
                string forwardResult = RunAdb($"-s {device.id} forward tcp:55000 localabstract:Unity-{GamePackageName}");
                Console.WriteLine($"[AutoProfiler] adb forward tcp:55000 -> localabstract:Unity-{GamePackageName} (result: \"{forwardResult}\")");

                Console.WriteLine("[AutoProfiler] Connecting via DirectIPConnect(\"127.0.0.1\")...");
                ProfilerDriver.DirectIPConnect("127.0.0.1");

                // Give a moment for the connection to establish, then start recording
                return true;
            }
        }

        return false;
    }
}
@echo off
REM ============================================================
REM  Unity Profiler Frame Parser (portable copy)
REM  Edit these 3 variables, save, and double-click to run.
REM  The Python script is expected to sit next to this .bat.
REM ============================================================

REM Path to the Unity profiler recording (.raw) to parse
set RECORDING_FILE=C:\Automation\UNDERDOGS Bots Automation\Tests Data\BOTS TEST - Name(3_Players) - Started At(23-04-2026_22-30-00)\ProfilerRecording.raw

REM First Unity frame to parse (1-indexed, matches Unity Profiler UI)
set FIRST_FRAME=750

REM Last Unity frame to parse (inclusive)
set LAST_FRAME=755

REM ============================================================
REM  Do not edit below this line
REM  PYTHON_SCRIPT resolves to profiler_parser.py next to this .bat.
REM ============================================================

set PYTHON_SCRIPT=%~dp0profiler_parser.py

py -3 "%PYTHON_SCRIPT%" "%RECORDING_FILE%"
echo.
pause

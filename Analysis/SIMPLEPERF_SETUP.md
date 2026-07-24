# simpleperf integration for the bots performance test

Adds a native **sampling** profiler capture to each bots run. This is the tool for
the "flat / count-bound" profile the Unity instrumented profiler produces: it
attributes aggregate CPU time to real functions with zero instrumentation, including
engine-internal and IL2CPP'd C# code that carries no Unity marker.

Meta's guidance (verified): simpleperf is the right tool for *steady-state* hotspots
("longest and most frequently called functions"); it is the WRONG tool for occasional
spikes — for the p90/p95 tail use Perfetto (separate runbook) or the instrumented
`tail_report.py`. Runs on a retail Quest in developer mode, **no root**.

Status: **untested on the rig** — every command below is from Meta/Unity docs but the
capture step and the report helper need one validation pass on the automation headset
before this goes into the nightly. Nothing here is wired into `UploadFiles.py` yet.

---

## 1. Vendor the simpleperf host scripts (one time)

simpleperf ships with the Android NDK. Copy its `simpleperf` python package into the
repo so the runner doesn't depend on an NDK install:

    <NDK>/simpleperf/  ->  automation-helpers/Analysis/simpleperf/

Needed files: `app_profiler.py`, `binary_cache_builder.py`, `report_sample.py`,
`report.py`, `simpleperf_report_lib.py`, and the `bin/` prebuilts. Pure Python + prebuilt
binaries; no install step.

## 2. Build-side change (main UNDERDOGS repo) — for function-level symbols

Tier 0 (DSO-level: `libunity` vs `libil2cpp` vs kernel) needs **no** build change and
works on the existing Development build.

Tier 1 (function names for game C#) needs debug symbols. In the build workflow
(`build.yaml` / `nightly.debugable.yaml`), set:

    EditorUserBuildSettings.androidCreateSymbols = AndroidCreateSymbols.Debugging

and upload the produced `symbols.zip` as a build artifact. IL2CPP + Development Build
is already on for these runs, so this is the only addition.

## 3. Capture step (Quest Bots Runner.bat)

Insert AFTER `AutoProfiler.Record` returns and BEFORE screenshot 3 — the script already
idles ~30s there, and sequencing it after the Unity capture keeps sampling overhead out
of the `.raw`. The game is already focused/awake at that point.

    :: ---- simpleperf native sampling capture (native CPU hotspots) ----
    echo capturing simpleperf native profile
    python "%~dp0..\Analysis\simpleperf\app_profiler.py" ^
        -p com.onehamsa.underdogs ^
        -r "-e cpu-cycles -f 1000 -g --duration 20" ^
        --disable_adb_root ^
        -o "%CURRENT_TEST_DIR%\perf.data"

Notes:
* `--disable_adb_root` is the documented non-root path; the app must be debuggable
  (Development build — already true).
* `-g` records call graphs (needs the IL2CPP symbols from step 2 for readable C# names;
  without them you still get DSO-level attribution).
* 20s matches the Unity capture window.

## 4. Report + reduce to CSV (UploadFiles.py, once validated)

After capture, build the binary cache from `symbols.zip` (if present) and emit a
reduced CSV next to `perf.data`. Add to `run_parsers()` after the tail report:

    binary_cache_builder.py -i perf.data -lib <symbols.zip extracted dir>
    simpleperf_report.py perf.data --symfs <binary_cache dir>   -> SIMPLEPERF_REPORT.csv

`SIMPLEPERF_REPORT.csv` is `*.csv`, so the existing upload glob already ships it.

### simpleperf_report.py — WRITTEN (`Analysis/simpleperf_report.py`), needs rig validation

Thin wrapper over `simpleperf_report_lib.py`:
* Opens `perf.data` via `ReportLib`, sums sample periods per (dso, symbol) and per DSO.
* Filters to the Unity main thread (`UnityMain`) by default; `--thread ""` for all threads.
* Emits `SIMPLEPERF_REPORT.csv`: a thread rollup (sanity), a per-DSO section (Tier 0,
  always works), and top-N functions (Tier 1, needs the binary cache).
* Degrades safely: if `simpleperf_report_lib` isn't vendored, or perf.data is empty, it
  prints a skip line and exits 0 (never fails the run).

**Not yet wired into `UploadFiles.py`** and **not yet run against a real perf.data** — it's
written against the documented ReportLib API but must be validated once on the rig
(confirm the `UnityMain` comm name and that periods aggregate as expected) before it goes
into the nightly. The capture step (§3) and this report share that single validation pass.

## 5. Interpreting output

* **DSO split first.** `libil2cpp.so` % vs `libunity.so` % vs kernel/vendor sizes the
  engine-vs-game-code boundary before drilling into functions. This is the number the
  instrumented profiler can't give.
* Sampling %s are of *active CPU time*, not wall-clock — a function blocked in a wait
  shows ~0 here (correct: it's not burning CPU). Cross-reference with the instrumented
  capture for wait attribution.

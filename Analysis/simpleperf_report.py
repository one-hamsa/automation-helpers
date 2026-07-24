"""
Reduce a simpleperf perf.data capture to a reviewable CSV.

Native SAMPLING profiler output — the complement to the instrumented Unity capture.
Where profiler_parser/tail_report attribute *marked* time, simpleperf attributes
*all* active CPU time to real functions (engine internals, IL2CPP'd C#, libc, kernel)
with zero instrumentation. This is the tool for the "flat / count-bound" profile
where no single Unity marker stands out.

Two tiers of output, both from the same perf.data:
  * Tier 0 (always works, no symbols needed): per-DSO rollup — libil2cpp.so vs
    libunity.so vs kernel/vendor. Sizes the engine-vs-game-code boundary, which the
    instrumented profiler cannot give.
  * Tier 1 (needs symbols: IL2CPP + Debug Symbols = Full, binary_cache built): top-N
    functions by sample weight, per DSO.

Reads via simpleperf's own simpleperf_report_lib (vendored under Analysis/simpleperf/).

IMPORTANT: percentages are of ACTIVE CPU time (summed sample periods), NOT wall-clock.
A function blocked in a wait samples ~0 here — correct, it's not burning CPU, but it
means simpleperf cannot see frame-pacing stalls. Cross-reference tail_report.py for
wait/idle attribution.

STATUS: written against the documented ReportLib API but NOT yet run against a real
perf.data on the rig. Validate once on the automation headset before wiring into
UploadFiles.py. See SIMPLEPERF_SETUP.md.

Usage:
    py -3 simpleperf_report.py <perf.data> [--thread UnityMain] [--symfs <binary_cache_dir>] [--top 50]

Writes SIMPLEPERF_REPORT.csv next to perf.data (picked up by the uploader's *.csv glob).
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

# Vendored simpleperf host scripts live alongside this file (see SIMPLEPERF_SETUP.md step 1).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "simpleperf"))

try:
    from simpleperf_report_lib import ReportLib
except ImportError:
    print("[SIMPLEPERF] simpleperf_report_lib not found under Analysis/simpleperf/ — "
          "vendor the NDK simpleperf scripts (SIMPLEPERF_SETUP.md step 1). Skipping.")
    sys.exit(0)

# Unity's main thread comm on Android. The whole point of this report is main-thread CPU,
# so we default to it; pass --thread "" to aggregate every thread instead.
DEFAULT_THREAD = "UnityMain"


def build_report(perf_data, thread_filter, symfs, top_n):
    lib = ReportLib()
    lib.SetRecordFile(perf_data)
    if symfs:
        lib.SetSymfs(symfs)
    # Show the instruction pointer when a symbol can't be resolved, so unresolved
    # native frames are still distinguishable rather than all collapsing to "unknown".
    lib.ShowIpForUnknownSymbol()

    by_dso = defaultdict(float)              # dso -> summed period
    by_symbol = defaultdict(float)           # (dso, symbol) -> summed period
    by_thread = defaultdict(float)           # thread_comm -> summed period (diagnostic)
    total = 0.0
    matched = 0.0
    event_name = None

    while True:
        sample = lib.GetNextSample()
        if sample is None:
            break
        if event_name is None:
            event_name = lib.GetEventOfCurrentSample().name

        period = float(sample.period)
        total += period
        by_thread[sample.thread_comm] += period

        # Focus on the requested thread (default: Unity main thread).
        if thread_filter and sample.thread_comm != thread_filter:
            continue
        matched += period

        symbol = lib.GetSymbolOfCurrentSample()
        dso = os.path.basename(symbol.dso_name) if symbol.dso_name else "[unknown]"
        sym = symbol.symbol_name or "[unknown]"
        by_dso[dso] += period
        by_symbol[(dso, sym)] += period

    return {
        "event": event_name or "unknown",
        "total": total,
        "matched": matched,
        "thread_filter": thread_filter,
        "by_thread": by_thread,
        "by_dso": by_dso,
        "by_symbol": by_symbol,
        "top_n": top_n,
    }


def write_report(perf_data, r):
    out = Path(perf_data).with_name("SIMPLEPERF_REPORT.csv")
    denom = r["matched"] or 1.0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["section", "key", "pct", "detail"])

        w.writerow(["summary", "event", "", r["event"]])
        w.writerow(["summary", "thread_filter", "", r["thread_filter"] or "(all threads)"])
        w.writerow(["summary", "matched_pct_of_total", f"{100.0 * r['matched'] / (r['total'] or 1.0):.1f}",
                    "share of all samples on the filtered thread"])

        # Thread rollup (diagnostic — confirms we filtered the right thread).
        for comm, period in sorted(r["by_thread"].items(), key=lambda x: -x[1])[:10]:
            w.writerow(["thread", comm, f"{100.0 * period / (r['total'] or 1.0):.1f}", ""])

        # Tier 0 — per DSO (always meaningful).
        for dso, period in sorted(r["by_dso"].items(), key=lambda x: -x[1]):
            w.writerow(["dso", dso, f"{100.0 * period / denom:.1f}", ""])

        # Tier 1 — top functions (meaningful only with symbols).
        for (dso, sym), period in sorted(r["by_symbol"].items(), key=lambda x: -x[1])[:r["top_n"]]:
            w.writerow(["symbol", sym, f"{100.0 * period / denom:.1f}", dso])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("perf_data")
    ap.add_argument("--thread", default=DEFAULT_THREAD,
                    help='thread comm to focus on (default "UnityMain"; pass "" for all threads)')
    ap.add_argument("--symfs", default=None, help="binary_cache dir from binary_cache_builder.py (Tier 1)")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args()

    if not os.path.isfile(args.perf_data):
        print(f"[SIMPLEPERF] not found: {args.perf_data}")
        sys.exit(1)

    r = build_report(args.perf_data, args.thread, args.symfs, args.top)
    if r["total"] == 0:
        print("[SIMPLEPERF] no samples in perf.data — capture failed or app wasn't running. Skipping.")
        return

    denom = r["matched"] or 1.0
    print(f"[SIMPLEPERF] event={r['event']}  thread={r['thread_filter'] or '(all)'}  "
          f"matched {100.0 * r['matched'] / (r['total'] or 1.0):.0f}% of samples")
    print("[SIMPLEPERF] per-DSO (share of filtered-thread CPU):")
    for dso, period in sorted(r["by_dso"].items(), key=lambda x: -x[1])[:8]:
        print(f"         {100.0 * period / denom:5.1f}%  {dso}")
    print("[SIMPLEPERF] top functions:")
    for (dso, sym), period in sorted(r["by_symbol"].items(), key=lambda x: -x[1])[:12]:
        print(f"         {100.0 * period / denom:5.1f}%  {sym[:60]}  ({dso})")

    out = write_report(args.perf_data, r)
    print(f"[SIMPLEPERF] wrote {out}")


if __name__ == "__main__":
    main()

"""
Tail-vs-median frame attribution for a Unity Profiler .raw recording.

Complements profiler_parser.py (aggregated hotspot dump) with the question that
actually matters for VR frame pacing: *which systems drive the slow frames*, not
which are expensive on average. On Quest the median frame is usually fine; the
judder comes from the p90/p95 tail (combat bursts, death/respawn churn, network
receive spikes). Median-only analysis is blind to all of it.

Method (Meta-recommended: per-marker max-vs-median divergence, adapted to a
per-frame work model):
  * "work" per frame = frame CPU time minus wait/idle markers (XRUpdate compositor
    block, Semaphore/Gfx waits). Waits are a SYMPTOM of overruns, not a cost — and
    XRUpdate self-time doubles the very stalls we hunt, so it is excluded.
  * Compares the mean per-marker self-time on the tail band (slowest 10% of frames
    by work) against the calm median band (35-65th percentile). A marker with a
    large tail-minus-median delta is a spike driver even if its median cost is ~0.
  * Single-frame catastrophic outliers (any one marker > OUTLIER_MS in a frame:
    JIT/OS/loader stalls that recur ~never) are dropped from the bands and reported
    SEPARATELY — otherwise one 200ms frame skews the tail mean and fabricates a
    phantom "hotspot" (learned the hard way: a one-off 218ms frame made a 0.03ms
    marker look like a +2.8ms tail driver).

Emits TAIL_REPORT.csv next to the .raw (picked up by the uploader's CSV glob) and
prints a summary to stdout.

Usage:
    py -3 tail_report.py <recording.raw>
"""

import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from profiler_parser import (
    read_file_header,
    load_id_to_name,
    load_chunks,
    parse_thread_samples,
)

# 45Hz ASW budget on Quest (the shipping target). Work at/under this = a good frame.
BUDGET_MS = 22.2

# Frames where a single marker exceeds this are treated as one-off stalls
# (JIT / OS scheduling / asset load), reported separately, excluded from the bands.
OUTLIER_MS = 60.0

# Markers that are idle/wait time, not work. Excluded from the work metric.
# XRUpdate is the compositor frame-pacing block (see the VR pacing note): counting
# it would double-book the stalls, and a real optimization would look like a regression.
WAIT_KEYS = ("Wait", "Semaphore", "EarlyUpdate.XRUpdate", "PresentFrame")

# Coarse buckets for the steady-state breakdown. First match wins.
BUCKETS = {
    "physx": ["PhysX.", "PxScene", "Physics.Processing", "Physics.Simulate",
              "BatchedCollisionDispatcher", "Physics.OnSceneContact"],
    "rendering": ["SRPBatcher", "SRPBRender", "ScriptableRenderContext", "RenderCameraStack",
                  "ExecuteRenderQueueJob", "RenderPipeline", "Culling", "Shadows",
                  "UpdateRendererBoundingVolumes", "RenderLoop"],
    "rig": ["MechRig", "MechPilot", "VRAvatar", "MechArm", "GorillaArm", "ArmAudio",
            "HandArmAttachment"],
    "joints": ["UDJoint", "UDRigidbody"],
    "normcore": ["Normal.Realtime"],
    "particles": ["ParticleSystem"],
    "spawn": ["ActivateAwakeRecursively", "InstantiateMultiple", "AsyncInstantiateOperation",
              "Transform.SetParent", "ThrottledSetup", "CanvasUpdate", "Instantiate.Produce"],
}

TOP_N_DRIVERS = 20


def _pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(p * len(sorted_vals)))
    return sorted_vals[i]


def analyze(path):
    string_table_size = read_file_header(path)
    id_to_name = load_id_to_name(path, string_table_size)
    chunks = load_chunks(path, string_table_size)

    frames = []       # (work_ms, {marker: self_ms}, unity_frame)
    outliers = []     # (unity_frame, frame_ms, worst_marker, worst_ms)

    with open(path, "rb") as f:
        for c in chunks:
            f.seek(c["offset"])
            data = f.read(c["size"])
            frame_ms = c["cpu_us"] / 1000.0
            samples, _ = parse_thread_samples(data, b"Main Thread\x00", id_to_name)
            if not samples:
                continue

            waits = 0.0
            per = defaultdict(float)
            worst_marker, worst_ms = "", 0.0
            for s in samples:
                self_ms = s["self_ns"] / 1e6
                if self_ms > worst_ms:
                    worst_ms, worst_marker = self_ms, s["name"]
                if any(k in s["name"] for k in WAIT_KEYS):
                    waits += self_ms
                    continue
                per[s["name"]] += self_ms

            if worst_ms > OUTLIER_MS:
                outliers.append((c["unity_frame"], frame_ms, worst_marker, worst_ms))
                continue

            frames.append((frame_ms - waits, per, c["unity_frame"]))

    frames.sort(key=lambda x: x[0])
    n = len(frames)
    if n == 0:
        return None

    work_sorted = sorted(x[0] for x in frames)
    total_sorted = sorted(x[0] for x in frames)  # work-based; frame totals include waits, reported below
    compliance = 100.0 * sum(1 for w in work_sorted if w <= BUDGET_MS) / n

    median_band = frames[int(n * 0.35):int(n * 0.65)]
    tail_band = frames[int(n * 0.90):]

    def band_avg(band):
        acc = defaultdict(float)
        for w, per, _fr in band:
            for k, v in per.items():
                acc[k] += v
        return {k: v / len(band) for k, v in acc.items()}, sum(w for w, _p, _f in band) / len(band)

    med, med_work = band_avg(median_band)
    tail, tail_work = band_avg(tail_band)

    # bucket breakdown on the tail band (where the cost concentrates)
    def bucketize(band):
        acc = defaultdict(float)
        for _w, per, _fr in band:
            for name, self_ms in per.items():
                for bk, keys in BUCKETS.items():
                    if any(k in name for k in keys):
                        acc[bk] += self_ms
                        break
        return {k: v / len(band) for k, v in acc.items()}

    drivers = sorted(
        ((tail.get(k, 0) - med.get(k, 0), med.get(k, 0), tail.get(k, 0), k)
         for k in set(med) | set(tail)),
        reverse=True,
    )

    return {
        "n_frames": n,
        "n_outliers": len(outliers),
        "outliers": outliers,
        "work_p50": _pct(work_sorted, 0.50),
        "work_p90": _pct(work_sorted, 0.90),
        "work_p95": _pct(work_sorted, 0.95),
        "compliance": compliance,
        "median_work": med_work,
        "tail_work": tail_work,
        "tail_buckets": bucketize(tail_band),
        "median_buckets": bucketize(median_band),
        "drivers": drivers,
    }


def write_report(path, r):
    out = Path(path).with_name("TAIL_REPORT.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["section", "key", "value", "detail"])

        w.writerow(["summary", "frames_analyzed", r["n_frames"], ""])
        w.writerow(["summary", "budget_ms", BUDGET_MS, "45Hz ASW"])
        w.writerow(["summary", "work_p50_ms", f"{r['work_p50']:.2f}", ""])
        w.writerow(["summary", "work_p90_ms", f"{r['work_p90']:.2f}", ""])
        w.writerow(["summary", "work_p95_ms", f"{r['work_p95']:.2f}", ""])
        w.writerow(["summary", "compliance_pct", f"{r['compliance']:.0f}",
                    "% of frames with work <= budget"])
        w.writerow(["summary", "median_band_work_ms", f"{r['median_work']:.2f}", "calm 35-65 pctile"])
        w.writerow(["summary", "tail_band_work_ms", f"{r['tail_work']:.2f}", "slowest 10%"])
        w.writerow(["summary", "tail_gap_ms", f"{r['tail_work'] - r['median_work']:.2f}",
                    "what the spikes add"])

        for bk, v in sorted(r["tail_buckets"].items(), key=lambda x: -x[1]):
            w.writerow(["tail_bucket", bk, f"{v:.3f}", f"median {r['median_buckets'].get(bk, 0):.3f}"])

        for delta, mv, tv, name in r["drivers"][:TOP_N_DRIVERS]:
            if delta < 0.02:
                break
            w.writerow(["tail_driver", name, f"{delta:+.3f}", f"median {mv:.3f} -> tail {tv:.3f}"])

        for fr, frame_ms, marker, ms in sorted(r["outliers"], key=lambda x: -x[3]):
            w.writerow(["outlier_frame", f"frame_{fr}", f"{frame_ms:.1f}ms",
                        f"{marker} = {ms:.1f}ms (one-off stall, excluded from bands)"])
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: tail_report.py <recording.raw>")
        sys.exit(1)
    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"ERROR: not found: {path}")
        sys.exit(1)

    r = analyze(path)
    if r is None:
        print("[TAIL] no main-thread frames parsed, skipping.")
        return

    print(f"[TAIL] {r['n_frames']} frames | work p50={r['work_p50']:.2f} "
          f"p90={r['work_p90']:.2f} p95={r['work_p95']:.2f} | 45Hz compliance={r['compliance']:.0f}%")
    print(f"[TAIL] median-band work {r['median_work']:.2f}ms -> tail-band work {r['tail_work']:.2f}ms "
          f"(gap {r['tail_work'] - r['median_work']:.2f}ms)")
    if r["outliers"]:
        print(f"[TAIL] {r['n_outliers']} one-off outlier frame(s) excluded (reported in CSV):")
        for fr, frame_ms, marker, ms in sorted(r["outliers"], key=lambda x: -x[3])[:5]:
            print(f"         frame {fr}: {marker} = {ms:.1f}ms (frame {frame_ms:.1f}ms)")
    print("[TAIL] top tail drivers (marker: tail-minus-median self-ms):")
    for delta, mv, tv, name in r["drivers"][:12]:
        if delta < 0.02:
            break
        print(f"         {delta:+.3f}  {mv:.3f}->{tv:.3f}  {name[:70]}")

    out = write_report(path, r)
    print(f"[TAIL] wrote {out}")


if __name__ == "__main__":
    main()

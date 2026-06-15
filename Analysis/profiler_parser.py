"""
Unity Profiler .raw parser.

Usage:
    py -3 profiler_parser.py <recording.raw>                             # 10 evenly-spaced frames, AGGREGATED
    py -3 profiler_parser.py <recording.raw> <ui_frame>                  # single frame
    py -3 profiler_parser.py <recording.raw> <ui_first> <ui_last>        # inclusive range
    py -3 profiler_parser.py <recording.raw> <abs_first> <abs_last> --abs

Frame numbers default to **Unity Profiler UI semantics**: 1-indexed within
the recording (frame 1 = first chunk in the .raw, frame N = N-th chunk). This
matches what the user sees in Unity's Profiler window after loading the .raw.

Internally each chunk header stores `internal_frame_id` which is Unity's
absolute `Time.frameCount` (continuous since process start, not reset when
recording begins). The parser converts UI frame N → absolute via
`min_frame + N - 1` where `min_frame` is the smallest Time.frameCount in the
recording. Pass `--abs` to skip the translation and treat the args as
absolute Time.frameCount directly.

Why this matters: the log's `Frame` field (Global.json.log) is also
Time.frameCount, so the parser's absolute frame numbers line up 1:1 with log
findings — that's how /analyze-perf correlates exceptions to profiler frames.

The parser writes:
    range_<abs_first>-<abs_last>_hierarchy.csv  - one row per (frame, call_path)        (range mode)
    aggregated_<first>-<last>_n<count>_hierarchy.csv - one row per (thread, call_path)  (aggregated mode)

Filenames use absolute frame numbers so they line up with log `Frame` values.

Output files are written next to the .raw recording. A small cache is kept
per recording (keyed by absolute path + file size) under
.profiler_analysis/cache/ so subsequent runs against the same recording are
fast.

Format notes (verified against Unity display for frame 750 of the source recording):

FILE LAYOUT
  [0, string_table_end):   String table (global marker ID registry)
  [string_table_end, EOF): Frame chunks, one per captured frame

FILE HEADER (first few bytes)
  @ 0  uint32  magic = 0x20220328  (also the prefix of every frame chunk)
  @ 4  uint32  string_table_size   (size of string table in bytes)

FRAME CHUNK HEADER
  @ 0  uint32  magic (same as file header)
  @ 4  uint32  chunk_size (chunks are VARIABLE size)
  @28  uint32  internal frame_id (0-indexed; Unity UI shows frame_id+1)
  @44  uint32  CPU time in microseconds

THREAD SECTION
  - thread metadata + null-terminated thread name + 4-byte-align
  - uint32 sample_count
  - sample_count * 20-byte records
  - auxiliary block (8-byte entries, flow events)

SAMPLE RECORD (fixed 20 bytes)
  @ 0  uint32   marker_id       (lookup in string table)
  @ 4  float32  duration_ns
  @ 8  uint64   start_ts_ns
  @16  uint32   child_count     (next N records are this record's children)

STRING TABLE
  Variable-length entries. Each marker_id is stored as uint32 in the 4 bytes
  immediately preceding a null-terminated ASCII name. Top bit (0x80000000)
  is a category/type flag.
"""

import struct
import os
import sys
import json
import csv
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# ---- Layout / output directory ---------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / 'cache'
CACHE_DIR.mkdir(exist_ok=True)

MAGIC = b'\x28\x03\x22\x20'
RECORD_SIZE = 20


# ---- String table ----------------------------------------------------------

def build_id_to_name(file_path, string_table_size):
    with open(file_path, 'rb') as f:
        table = f.read(string_table_size)

    id_to_name = {}
    p = 200
    while p < len(table):
        if not (0x20 <= table[p] < 0x7f):
            p += 1
            continue
        start = p
        while p < len(table) and 0x20 <= table[p] < 0x7f:
            p += 1
        if p < len(table) and table[p] == 0 and (p - start) >= 4 and start >= 8:
            name = table[start:p].decode('ascii')
            if any(c.isalpha() for c in name):
                marker_id = struct.unpack_from('<I', table, start - 4)[0]
                existing = id_to_name.get(marker_id)
                if existing is None or len(name) > len(existing):
                    id_to_name[marker_id] = name
        p += 1
    return id_to_name


# ---- Chunk scan ------------------------------------------------------------

def find_all_chunks(file_path, string_table_size):
    """Scan for every frame chunk and read its header."""
    size = os.path.getsize(file_path)
    offsets = []
    with open(file_path, 'rb') as f:
        SCAN = 16 * 1024 * 1024
        pos = max(0, string_table_size - 1000)
        prev_tail = b''
        while pos < size:
            f.seek(pos)
            buf = f.read(SCAN)
            if not buf:
                break
            search = prev_tail + buf
            off = 0
            while True:
                idx = search.find(MAGIC, off)
                if idx == -1:
                    break
                actual = pos - len(prev_tail) + idx
                if actual >= string_table_size:
                    offsets.append(actual)
                off = idx + 1
            prev_tail = search[-3:]
            pos += SCAN

        result = []
        for coff in offsets:
            f.seek(coff)
            hdr = f.read(48)
            size_field = struct.unpack_from('<I', hdr, 4)[0]
            frame_id = struct.unpack_from('<I', hdr, 28)[0]
            cpu_us = struct.unpack_from('<I', hdr, 44)[0]
            result.append({
                'unity_frame': frame_id + 1,
                'internal_frame_id': frame_id,
                'offset': coff,
                'cpu_us': cpu_us,
                'size': size_field,
            })
    return result


# ---- Cache -----------------------------------------------------------------

def _cache_key(recording_path):
    """Return a filename-safe key for this recording. We keep file size in the
    cache data itself for validation; the key is just for readability."""
    p = Path(recording_path).resolve()
    safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in p.name)
    return safe


def _cache_path(recording_path, kind):
    """kind in {'strings', 'chunks'}."""
    key = _cache_key(recording_path)
    return CACHE_DIR / f'{key}.{kind}.json'


def _read_cache(recording_path, kind, expected_size):
    path = _cache_path(recording_path, kind)
    if not path.exists():
        return None
    try:
        with open(path, encoding='utf-8') as f:
            blob = json.load(f)
        if blob.get('recording_path') != str(Path(recording_path).resolve()):
            return None
        if blob.get('recording_size') != expected_size:
            return None
        return blob.get('data')
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _write_cache(recording_path, kind, data, file_size):
    path = _cache_path(recording_path, kind)
    blob = {
        'recording_path': str(Path(recording_path).resolve()),
        'recording_size': file_size,
        'data': data,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(blob, f, ensure_ascii=False)


def read_file_header(recording_path):
    """Read the first 8 bytes to confirm magic and get the string_table_size."""
    with open(recording_path, 'rb') as f:
        header = f.read(8)
    if len(header) < 8 or header[:4] != MAGIC:
        raise ValueError(
            f"File does not start with profiler magic bytes (28 03 22 20): "
            f"{recording_path}"
        )
    string_table_size = struct.unpack_from('<I', header, 4)[0]
    return string_table_size


def load_id_to_name(recording_path, string_table_size):
    file_size = os.path.getsize(recording_path)
    cached = _read_cache(recording_path, 'strings', file_size)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    print("Building string table (first run for this recording, ~5s)...")
    id_to_name = build_id_to_name(recording_path, string_table_size)
    _write_cache(recording_path, 'strings',
                 {str(k): v for k, v in id_to_name.items()}, file_size)
    return id_to_name


def load_chunks(recording_path, string_table_size):
    file_size = os.path.getsize(recording_path)
    cached = _read_cache(recording_path, 'chunks', file_size)
    if cached is not None:
        return cached
    print("Scanning chunks (first run for this recording, ~15s)...")
    chunks = find_all_chunks(recording_path, string_table_size)
    _write_cache(recording_path, 'chunks', chunks, file_size)
    return chunks


# ---- Sample parser ---------------------------------------------------------

def parse_thread_samples(chunk_data, thread_name_bytes, id_to_name):
    """Parse a thread's sample tree. Returns list of dicts:
      {id, parent_id, depth, marker_id, name, duration_ns, child_count, start_ts}
    where id is the sample's index in the returned list, and parent_id is the
    index of its immediate parent (or -1 for root samples).
    self_ns is computed by subtracting direct children durations from duration_ns.
    """
    name_pos = chunk_data.find(thread_name_bytes)
    if name_pos == -1:
        return None, None

    p = name_pos + len(thread_name_bytes)
    p = (p + 3) & ~3
    sample_count = struct.unpack_from('<I', chunk_data, p)[0]
    records_start = p + 4

    if sample_count > 1_000_000:
        return None, None

    samples = []
    pos = records_start

    def read_record(at):
        return struct.unpack_from('<IfQI', chunk_data, at)

    def append_sample(pos_val, depth, parent_id, mid, dur, kids, ts):
        samples.append({
            'id': len(samples),
            'parent_id': parent_id,
            'depth': depth,
            'pos': pos_val,
            'marker_id': mid,
            'name': id_to_name.get(mid, f'<unknown_0x{mid:x}>'),
            'duration_ns': dur,
            'child_count': kids,
            'start_ts': ts,
        })

    def parse_children(at, n, depth, parent_id):
        for _ in range(n):
            if at + RECORD_SIZE > len(chunk_data) or len(samples) >= sample_count:
                return at, False
            mid, dur, ts, kids = read_record(at)
            name = id_to_name.get(mid)
            if name is None:
                return at, False
            my_id = len(samples)
            append_sample(at, depth, parent_id, mid, dur, kids, ts)
            at += RECORD_SIZE
            if kids > 0 and kids < 100_000:
                at, ok = parse_children(at, kids, depth + 1, my_id)
                if not ok:
                    return at, False
        return at, True

    while pos + RECORD_SIZE <= len(chunk_data) and len(samples) < sample_count:
        mid, dur, ts, kids = read_record(pos)
        name = id_to_name.get(mid)
        if name is None:
            break
        my_id = len(samples)
        append_sample(pos, 0, -1, mid, dur, kids, ts)
        pos += RECORD_SIZE
        if kids > 0 and kids < 100_000:
            pos, ok = parse_children(pos, kids, 1, my_id)
            if not ok:
                break

    # Compute self_ns = duration_ns - sum(direct children duration_ns)
    children_ns_sum = [0.0] * len(samples)
    for s in samples:
        if s['parent_id'] >= 0:
            children_ns_sum[s['parent_id']] += s['duration_ns']
    for i, s in enumerate(samples):
        s['self_ns'] = max(0.0, s['duration_ns'] - children_ns_sum[i])

    return samples, sample_count


def shorten(name):
    return (name.replace('Underdogs.', 'UD.')
                .replace('[Invoke]', '')
                .replace('UnityEngine.', 'UE.')
                .strip())


def build_call_paths(samples):
    """Return list of 'A -> B -> C' strings, one per sample (root = own name)."""
    paths = [''] * len(samples)
    for i, s in enumerate(samples):
        name = shorten(s['name'])
        if s['parent_id'] < 0:
            paths[i] = name
        else:
            paths[i] = f"{paths[s['parent_id']]} -> {name}"
    return paths


# ---- Range clamping --------------------------------------------------------

def clamp_frame_range(first_frame, last_frame, min_frame, max_frame):
    """Clamp the requested [first, last] range to the valid bounds of this
    recording. Returns (first, last, notes) where notes describes corrections.

    Rules (in order):
      1. if first < min_frame:  first = min_frame  (floor: recordings often skip frame 1)
      2. if last  > max_frame:  last  = max_frame  (ceiling: can't exceed recording)
      3. if first > last:       last  = first      (fix ordering)
    """
    notes = []

    if first_frame < min_frame:
        notes.append(f"first_frame was {first_frame}, clamped up to {min_frame} "
                     f"(first frame available in recording)")
        first_frame = min_frame

    if last_frame > max_frame:
        notes.append(f"last_frame was {last_frame}, clamped down to {max_frame} "
                     f"(last frame available in recording)")
        last_frame = max_frame

    if first_frame > last_frame:
        notes.append(f"first_frame ({first_frame}) > last_frame ({last_frame}), "
                     f"set last_frame = first_frame = {first_frame}")
        last_frame = first_frame

    return first_frame, last_frame, notes


# ---- Main ------------------------------------------------------------------

def parse_range(recording_path, first_frame, last_frame):
    recording_path = str(Path(recording_path).resolve())

    if not os.path.exists(recording_path):
        print(f"ERROR: recording file not found: {recording_path}")
        sys.exit(1)

    try:
        string_table_size = read_file_header(recording_path)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    id_to_name = load_id_to_name(recording_path, string_table_size)
    chunks = load_chunks(recording_path, string_table_size)
    by_frame = {c['unity_frame']: c for c in chunks}
    min_frame = min(by_frame)
    max_frame = max(by_frame)
    print(f"Recording: {recording_path}")
    print(f"  File size: {os.path.getsize(recording_path):,} bytes")
    print(f"  Frames in recording: {len(chunks):,} (Unity frames {min_frame}..{max_frame})")

    first_frame, last_frame, notes = clamp_frame_range(
        first_frame, last_frame, min_frame, max_frame)
    if notes:
        print("Frame range was adjusted:")
        for n in notes:
            print(f"  - {n}")
    frame_count = last_frame - first_frame + 1
    print(f"Parsing frames {first_frame}..{last_frame} ({frame_count} frames)...")

    out_dir = Path(recording_path).parent
    THREADS = [b'Main Thread\x00', b'Render Thread\x00']

    if frame_count == 1:
        csv_path = out_dir / f'range_{first_frame}-{last_frame}_hierarchy.csv'
        csv_f = open(csv_path, 'w', newline='', encoding='utf-8-sig')
        writer = csv.writer(csv_f)
        writer.writerow(['frame', 'call_path', 'calls', 'time_ms', 'self_ms'])

        with open(recording_path, 'rb') as rec_f:
            info = by_frame[first_frame]
            rec_f.seek(info['offset'])
            chunk_data = rec_f.read(info['size'])
            for thread_bytes in THREADS:
                samples, _ = parse_thread_samples(chunk_data, thread_bytes, id_to_name)
                if not samples:
                    continue
                paths = build_call_paths(samples)
                order = []
                agg = {}
                for s in samples:
                    path = paths[s['id']]
                    if path not in agg:
                        order.append(path)
                        agg[path] = [0, 0.0, 0.0, shorten(s['name']), s['depth']]
                    agg[path][0] += 1
                    agg[path][1] += s['duration_ns']
                    agg[path][2] += s['self_ns']
                for path in order:
                    calls, total_ns, self_ns, _, _ = agg[path]
                    writer.writerow([
                        first_frame, path, calls,
                        f"{total_ns / 1_000_000:.3f}",
                        f"{self_ns / 1_000_000:.3f}",
                    ])

        csv_f.close()
        print(f"\nWritten:")
        print(f"  {csv_path}")
        print(f"\nCSV: one row per call_path.")
        print(f"  Columns: frame | call_path | calls | time_ms | self_ms")
        print(f"  - Sort by self_ms desc -> real hotspots (code doing the work)")
        print(f"  - Sort by time_ms desc -> what's expensive overall (incl. children)")
        print(f"  - Filter call_path 'contains' X -> subtree drill-down")

    else:
        csv_path = out_dir / f'aggregated_{first_frame}-{last_frame}_n{frame_count}_hierarchy.csv'
        agg = {}
        order_per_thread = {}

        with open(recording_path, 'rb') as rec_f:
            for i, unity_frame in enumerate(range(first_frame, last_frame + 1)):
                info = by_frame[unity_frame]
                rec_f.seek(info['offset'])
                chunk_data = rec_f.read(info['size'])

                for thread_bytes in THREADS:
                    thread_name = thread_bytes[:-1].decode()
                    samples, _ = parse_thread_samples(chunk_data, thread_bytes, id_to_name)
                    if not samples:
                        continue
                    paths = build_call_paths(samples)
                    if thread_name not in order_per_thread:
                        order_per_thread[thread_name] = []
                    ordered = order_per_thread[thread_name]
                    seen_paths = set(ordered)

                    for s in samples:
                        path = paths[s['id']]
                        key = (thread_name, path)
                        rec = agg.get(key)
                        if rec is None:
                            rec = {
                                'calls': 0,
                                'total_ns': 0.0,
                                'self_ns': 0.0,
                                'depth': s['depth'],
                                'last_name': shorten(s['name']),
                                'frames_seen': set(),
                            }
                            agg[key] = rec
                            if path not in seen_paths:
                                ordered.append(path)
                                seen_paths.add(path)
                        rec['calls'] += 1
                        rec['total_ns'] += s['duration_ns']
                        rec['self_ns'] += s['self_ns']
                        rec['frames_seen'].add(unity_frame)

                if (i + 1) % 50 == 0 or i + 1 == frame_count:
                    print(f"  {i+1}/{frame_count} frames parsed")

        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(['thread', 'call_path', 'frames_seen', 'total_calls',
                        'avg_ms_per_frame', 'avg_self_ms_per_frame'])
            for thread, paths_list in order_per_thread.items():
                for path in paths_list:
                    rec = agg[(thread, path)]
                    total_ms = rec['total_ns'] / 1_000_000
                    self_ms = rec['self_ns'] / 1_000_000
                    w.writerow([
                        thread, path,
                        len(rec['frames_seen']),
                        rec['calls'],
                        f"{total_ms / frame_count:.3f}",
                        f"{self_ms / frame_count:.3f}",
                    ])

        print(f"\nWritten:")
        print(f"  {csv_path}")
        print(f"\nCSV rows: (thread, call_path) aggregated across {frame_count} frames.")
        print(f"  - avg_self_ms_per_frame desc -> typical hotspot per frame")
        print(f"  - frames_seen low + high avg -> spiky/intermittent cost")


def _pick_evenly_spaced_frames(available_frames, n):
    """Pick n frames evenly spaced from `available_frames` (a sorted list).
    Snaps each ideal target to the nearest existing frame; de-duplicates while
    preserving order. May return fewer than n frames if the recording is short.
    """
    if not available_frames:
        return []
    if len(available_frames) <= n:
        return list(available_frames)
    if n == 1:
        return [available_frames[len(available_frames) // 2]]

    lo, hi = available_frames[0], available_frames[-1]
    step = (hi - lo) / (n - 1)
    targets = [lo + i * step for i in range(n)]

    picks = []
    seen = set()
    for t in targets:
        idx = min(range(len(available_frames)),
                  key=lambda i: abs(available_frames[i] - t))
        f = available_frames[idx]
        if f not in seen:
            picks.append(f)
            seen.add(f)
    return sorted(picks)


def parse_aggregated(recording_path, num_frames=10):
    """Parse `num_frames` evenly-spaced frames and emit ONE aggregated report
    (per-thread, per-call-path totals + per-frame averages). Output files land
    next to the .raw, same convention as `parse_range`."""
    recording_path = str(Path(recording_path).resolve())

    if not os.path.exists(recording_path):
        print(f"ERROR: recording file not found: {recording_path}")
        sys.exit(1)

    try:
        string_table_size = read_file_header(recording_path)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    id_to_name = load_id_to_name(recording_path, string_table_size)
    chunks = load_chunks(recording_path, string_table_size)
    by_frame = {c['unity_frame']: c for c in chunks}
    available = sorted(by_frame.keys())
    if not available:
        print("ERROR: no frames found in recording.")
        sys.exit(1)

    min_frame, max_frame = available[0], available[-1]
    print(f"Recording: {recording_path}")
    print(f"  File size: {os.path.getsize(recording_path):,} bytes")
    print(f"  Frames in recording: {len(chunks):,} (Unity frames {min_frame}..{max_frame})")

    sampled = _pick_evenly_spaced_frames(available, num_frames)
    n = len(sampled)
    print(f"Aggregated parse over {n} evenly-spaced frame(s): {sampled}")

    out_dir = Path(recording_path).parent
    span = f"{sampled[0]}-{sampled[-1]}"
    csv_path = out_dir / f'aggregated_{span}_n{n}_hierarchy.csv'

    THREADS = [b'Main Thread\x00', b'Render Thread\x00']

    # agg[(thread, call_path)] = {
    #   'calls', 'total_ns', 'self_ns', 'depth', 'last_name', 'frames_seen' (set)
    # }
    agg = {}
    order_per_thread = {}  # preserves first-seen order within each thread

    with open(recording_path, 'rb') as rec_f:
        for unity_frame in sampled:
            info = by_frame[unity_frame]
            rec_f.seek(info['offset'])
            chunk_data = rec_f.read(info['size'])

            for thread_bytes in THREADS:
                thread_name = thread_bytes[:-1].decode()
                samples, _ = parse_thread_samples(chunk_data, thread_bytes, id_to_name)
                if not samples:
                    continue
                paths = build_call_paths(samples)
                if thread_name not in order_per_thread:
                    order_per_thread[thread_name] = []
                ordered = order_per_thread[thread_name]
                seen_paths = set(ordered)

                for s in samples:
                    path = paths[s['id']]
                    key = (thread_name, path)
                    rec = agg.get(key)
                    if rec is None:
                        rec = {
                            'calls': 0,
                            'total_ns': 0.0,
                            'self_ns': 0.0,
                            'depth': s['depth'],
                            'last_name': shorten(s['name']),
                            'frames_seen': set(),
                        }
                        agg[key] = rec
                        if path not in seen_paths:
                            ordered.append(path)
                            seen_paths.add(path)
                    rec['calls'] += 1
                    rec['total_ns'] += s['duration_ns']
                    rec['self_ns'] += s['self_ns']
                    rec['frames_seen'].add(unity_frame)

    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['thread', 'call_path', 'frames_seen', 'total_calls',
                    'avg_ms_per_frame', 'avg_self_ms_per_frame'])
        for thread, paths in order_per_thread.items():
            for path in paths:
                rec = agg[(thread, path)]
                total_ms = rec['total_ns'] / 1_000_000
                self_ms = rec['self_ns'] / 1_000_000
                w.writerow([
                    thread,
                    path,
                    len(rec['frames_seen']),
                    rec['calls'],
                    f"{total_ms / n:.3f}",
                    f"{self_ms / n:.3f}",
                ])

    print(f"\nWritten:")
    print(f"  {csv_path}")
    print(f"\nCSV rows: (thread, call_path) aggregated across {n} sampled frames.")
    print(f"  - avg_self_ms_per_frame desc -> typical hotspot per frame")
    print(f"  - frames_seen low + high avg -> spiky/intermittent cost")


def main():
    argc = len(sys.argv)
    if argc == 2:
        # No frame args — sample 10 evenly-spaced frames, aggregated output.
        parse_aggregated(sys.argv[1], num_frames=10)
        return

    if argc != 4:
        print("Usage:")
        print("  py -3 profiler_parser.py <recording.raw>")
        print("    -> 10 evenly-spaced frames, aggregated report")
        print("  py -3 profiler_parser.py <recording.raw> <first_frame> <last_frame>")
        print("    -> per-frame report for the inclusive range (1-indexed, Unity Profiler UI)")
        sys.exit(2)

    recording_path = sys.argv[1]
    try:
        first_frame = int(sys.argv[2])
        last_frame = int(sys.argv[3])
    except ValueError:
        print(f"ERROR: frame arguments must be integers "
              f"(got '{sys.argv[2]}', '{sys.argv[3]}')")
        sys.exit(2)

    parse_range(recording_path, first_frame, last_frame)


if __name__ == '__main__':
    main()

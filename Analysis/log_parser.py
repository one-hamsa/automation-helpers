"""
UNDERDOGS bots-test Global.json.log parser.

Reads a JSON-Lines structured log produced by ReportUtility, decodes the
base64-encoded Message field, groups errors and exceptions, and emits:

  - A compact human-readable summary on stdout (the agent's primary input).
  - A CSV artifact <session>_log_findings.csv next to the source log
    (or under --out-dir).

Usage:
    py -3 log_parser.py <test-folder>
    py -3 log_parser.py <test-folder>/Report Logs/<session>/Global.json.log
    py -3 log_parser.py <test-folder> --out-dir <dir>
    py -3 log_parser.py <test-folder> --warnings-over 100

When given a test folder, globs Report Logs/*/Global.json.log and uses the
most-recent match. Exits non-zero if no Global.json.log is found.
"""

import argparse
import base64
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')


# ---- Noise ignore list -----------------------------------------------------
# Regexes (case-insensitive) on the decoded message stem. These are warnings
# we know are bot/VR-environment noise, not real findings. Extend as we learn.

NOISE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'^FMOD ChannelI .* Stolen',
        r'virtual state',
        r'audibility',
        r'Local Dimming feature is not supported',
        r'FoveatedRenderingLevel\.HighTop is not supported',
        r'Saving while fade is not black',
        r'provisional.*editor.?only',
    ]
]


# ---- Message normalization for grouping ------------------------------------
# Replace volatile tokens (numbers, GUIDs, instance names) so that messages
# that differ only by an integer or hex id collapse into a single group.

GUID_RE = re.compile(r'\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b')
HEX_RE = re.compile(r'\b0x[0-9a-fA-F]+\b')
NUM_RE = re.compile(r'\b\d+(?:\.\d+)?\b')
CLONE_RE = re.compile(r'\(Clone\)(?: \(\d+\))?')


def message_stem(text, max_len=80):
    """Volatile-token-stripped, length-capped key for deduping similar messages."""
    s = GUID_RE.sub('*', text)
    s = HEX_RE.sub('*', s)
    s = CLONE_RE.sub('(Clone)', s)
    s = NUM_RE.sub('*', s)
    s = ' '.join(s.split())
    return s[:max_len]


def is_noise(text):
    return any(p.search(text) for p in NOISE_PATTERNS)


# ---- Exception parsing -----------------------------------------------------

EXC_TYPE_RE = re.compile(r'^([A-Za-z_][\w.]*Exception(?:\b|:))')
AT_FRAME_RE = re.compile(r'^\s*at\s+([\w.<>+`]+(?:\.\w+)+)\s*\(')


def parse_exception(exc):
    """Return (exception_type, top_frame_symbol, first_5_lines_truncated).

    `exc` may be a dict ({Name, Message, StackTrace, InnerException?}) — the
    shape ReportUtility writes — or a plain string (legacy / fallback).
    """
    if not exc:
        return None, None, []

    if isinstance(exc, dict):
        exc_type = exc.get('Name') or '<unknown>'
        msg = exc.get('Message') or ''
        stack = exc.get('StackTrace') or ''
        head_line = f"{exc_type}: {msg}".strip().rstrip(':')
        stack_lines = [ln.rstrip() for ln in stack.splitlines() if ln.strip()]
        lines = [head_line] + stack_lines if head_line else stack_lines
    else:
        lines = [ln.rstrip() for ln in str(exc).splitlines() if ln.strip()]
        exc_type = '<unknown>'
        if lines:
            m = EXC_TYPE_RE.match(lines[0])
            if m:
                exc_type = m.group(1).rstrip(':')
            else:
                head = lines[0].split(':', 1)[0].strip()
                if head:
                    exc_type = head

    top_frame = '<unknown>'
    for ln in lines:
        m = AT_FRAME_RE.match(ln)
        if m:
            top_frame = m.group(1)
            break

    first_5 = [ln[:200] for ln in lines[:5]]
    return exc_type, top_frame, first_5


# ---- Locate input file -----------------------------------------------------

def resolve_input(arg):
    p = Path(arg)
    if p.is_file():
        return p
    if p.is_dir():
        candidates = sorted(
            p.glob('Report Logs/*/Global.json.log'),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            sys.stderr.write(f"No Global.json.log found under: {p / 'Report Logs'}\n")
            sys.exit(2)
        return candidates[0]
    sys.stderr.write(f"Path not found: {p}\n")
    sys.exit(2)


# ---- Per-line decode -------------------------------------------------------

def decode_message(b64):
    if not b64:
        return ''
    try:
        return base64.b64decode(b64).decode('utf-8', errors='replace')
    except Exception:
        return b64


# ---- Aggregator ------------------------------------------------------------

class Group:
    __slots__ = (
        'kind', 'key', 'count', 'first_frame', 'last_frame', 'frames',
        'nt_min', 'nt_max', 'categories', 'levels',
        'exc_type', 'top_frame', 'message_stem', 'rep_message',
        'rep_exc_first_5',
    )

    def __init__(self, kind, key):
        self.kind = kind
        self.key = key
        self.count = 0
        self.first_frame = None
        self.last_frame = None
        self.frames = []
        self.nt_min = None
        self.nt_max = None
        self.categories = set()
        self.levels = set()
        self.exc_type = ''
        self.top_frame = ''
        self.message_stem = ''
        self.rep_message = ''
        self.rep_exc_first_5 = []

    def update(self, frame, nt, category, level, message, exc_type, top_frame, exc_first_5, stem):
        self.count += 1
        if frame is not None:
            if self.first_frame is None or frame < self.first_frame:
                self.first_frame = frame
            if self.last_frame is None or frame > self.last_frame:
                self.last_frame = frame
            if frame not in self.frames and len(self.frames) < 5:
                self.frames.append(frame)
        if nt is not None:
            if self.nt_min is None or nt < self.nt_min:
                self.nt_min = nt
            if self.nt_max is None or nt > self.nt_max:
                self.nt_max = nt
        if category:
            self.categories.add(category)
        if level:
            self.levels.add(level)
        if not self.rep_message:
            self.rep_message = message[:300]
        if not self.message_stem:
            self.message_stem = stem
        if exc_type and not self.exc_type:
            self.exc_type = exc_type
        if top_frame and not self.top_frame:
            self.top_frame = top_frame
        if exc_first_5 and not self.rep_exc_first_5:
            self.rep_exc_first_5 = exc_first_5


def process(file_path, warnings_over):
    exception_groups = {}
    error_groups = {}
    warning_counts = defaultdict(int)
    warning_groups = {}

    total_lines = 0
    total_errors = 0
    total_exceptions = 0
    total_warnings_raw = 0
    frame_min = None
    frame_max = None
    nt_min_overall = None
    nt_max_overall = None

    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            total_lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            level = rec.get('LogLevel') or ''
            exc = rec.get('Exception')
            frame = rec.get('Frame')
            nt = rec.get('NetworkTime')
            category = rec.get('CategoryName') or ''

            if frame is not None:
                if frame_min is None or frame < frame_min:
                    frame_min = frame
                if frame_max is None or frame > frame_max:
                    frame_max = frame
            if nt is not None:
                if nt_min_overall is None or nt < nt_min_overall:
                    nt_min_overall = nt
                if nt_max_overall is None or nt > nt_max_overall:
                    nt_max_overall = nt

            is_error = (level == 'Error')
            is_warning = (level == 'Warning')
            has_exc = bool(exc)

            if is_error:
                total_errors += 1
            if has_exc:
                total_exceptions += 1
            if is_warning:
                total_warnings_raw += 1

            if not (is_error or has_exc or is_warning):
                continue

            message = decode_message(rec.get('Message') or '')
            stem = message_stem(message)

            if has_exc:
                exc_type, top_frame, exc_first_5 = parse_exception(exc)
                key = (exc_type, top_frame)
                g = exception_groups.get(key)
                if g is None:
                    g = Group('exception', key)
                    exception_groups[key] = g
                g.update(frame, nt, category, level, message, exc_type, top_frame, exc_first_5, stem)
                continue

            if is_error:
                key = (category, stem)
                g = error_groups.get(key)
                if g is None:
                    g = Group('error', key)
                    error_groups[key] = g
                g.update(frame, nt, category, level, message, '', '', [], stem)
                continue

            if is_warning:
                if is_noise(message):
                    continue
                key = (category, stem)
                warning_counts[key] += 1
                g = warning_groups.get(key)
                if g is None:
                    g = Group('warning', key)
                    warning_groups[key] = g
                g.update(frame, nt, category, level, message, '', '', [], stem)

    if warnings_over <= 0:
        warnings_emitted = {}
    else:
        warnings_emitted = {k: v for k, v in warning_groups.items() if warning_counts[k] >= warnings_over}

    stats = {
        'total_lines': total_lines,
        'total_errors': total_errors,
        'total_exceptions': total_exceptions,
        'total_warnings_raw': total_warnings_raw,
        'frame_min': frame_min,
        'frame_max': frame_max,
        'nt_min': nt_min_overall,
        'nt_max': nt_max_overall,
    }
    return exception_groups, error_groups, warnings_emitted, warning_counts, stats


# ---- Output ----------------------------------------------------------------

def fmt_frames(g):
    if not g.frames:
        return '-'
    sample = ', '.join(str(x) for x in sorted(g.frames))
    if g.first_frame == g.last_frame:
        return sample
    return f"{sample}  (range {g.first_frame}-{g.last_frame})"


def print_summary(file_path, exc_groups, err_groups, warn_emit, warn_counts, stats, warnings_over):
    print(f"Global.json.log: {file_path}")
    print(
        f"Lines: {stats['total_lines']}  |  "
        f"Errors: {stats['total_errors']}  |  "
        f"Exceptions: {stats['total_exceptions']}  |  "
        f"Warnings: {stats['total_warnings_raw']} (filtered)"
    )
    fr_lo = stats['frame_min'] if stats['frame_min'] is not None else '?'
    fr_hi = stats['frame_max'] if stats['frame_max'] is not None else '?'
    nt_lo = stats['nt_min'] if stats['nt_min'] is not None else '?'
    nt_hi = stats['nt_max'] if stats['nt_max'] is not None else '?'
    print(f"Frame range: {fr_lo} \u2014 {fr_hi}   NetworkTime range: {nt_lo} \u2014 {nt_hi} ms")
    print()

    sorted_excs = sorted(exc_groups.values(), key=lambda g: -g.count)
    print(f"[EXCEPTIONS]  {len(sorted_excs)} distinct")
    if not sorted_excs:
        print("  (none)")
    for g in sorted_excs:
        cats = ', '.join(sorted(g.categories)) or '-'
        print(f"  \u00d7{g.count}  {g.exc_type} @ {g.top_frame}")
        print(f"       category={cats}   frames: {fmt_frames(g)}")
        if g.rep_exc_first_5:
            print(f"       trace: {g.rep_exc_first_5[0]}")
            for ln in g.rep_exc_first_5[1:]:
                print(f"              {ln}")
    print()

    sorted_errs = sorted(err_groups.values(), key=lambda g: -g.count)
    print(f"[ERRORS, non-exception]  {len(sorted_errs)} distinct")
    if not sorted_errs:
        print("  (none)")
    for g in sorted_errs:
        cat = next(iter(g.categories), '-')
        ff = g.first_frame if g.first_frame is not None else '-'
        lf = g.last_frame if g.last_frame is not None else '-'
        print(f"  \u00d7{g.count}  [{cat}]  {g.rep_message[:140]}")
        print(f"        first frame={ff}, last frame={lf}")
    print()

    if warnings_over <= 0:
        print("[WARNINGS]  not requested (use --warnings-over N to surface high-volume warnings)")
    else:
        emitted = sorted(warn_emit.values(), key=lambda g: -warn_counts[g.key])
        print(f"[WARNINGS]  {len(emitted)} distinct (>= {warnings_over} occurrences, noise filtered)")
        for g in emitted:
            cat = next(iter(g.categories), '-')
            print(f"  \u00d7{warn_counts[g.key]}  [{cat}]  {g.rep_message[:140]}")


def write_csv(out_path, exc_groups, err_groups, warn_emit, warn_counts):
    cols = [
        'kind', 'count', 'exception_type', 'top_frame_symbol', 'category', 'level',
        'first_frame', 'last_frame', 'frames_sample',
        'network_time_min', 'network_time_max',
        'message_stem', 'representative_message',
        'representative_exception_first_5_lines',
    ]
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)

        def row(g, count_override=None):
            return [
                g.kind,
                count_override if count_override is not None else g.count,
                g.exc_type,
                g.top_frame,
                ';'.join(sorted(g.categories)),
                ';'.join(sorted(g.levels)),
                g.first_frame if g.first_frame is not None else '',
                g.last_frame if g.last_frame is not None else '',
                ';'.join(str(x) for x in sorted(g.frames)),
                g.nt_min if g.nt_min is not None else '',
                g.nt_max if g.nt_max is not None else '',
                g.message_stem,
                g.rep_message,
                ' || '.join(g.rep_exc_first_5),
            ]

        for g in sorted(exc_groups.values(), key=lambda g: -g.count):
            w.writerow(row(g))
        for g in sorted(err_groups.values(), key=lambda g: -g.count):
            w.writerow(row(g))
        for g in sorted(warn_emit.values(), key=lambda g: -warn_counts[g.key]):
            w.writerow(row(g, count_override=warn_counts[g.key]))


# ---- CLI -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Parse UNDERDOGS Global.json.log for errors/exceptions.')
    ap.add_argument('input', help='Test folder OR direct path to Global.json.log')
    ap.add_argument('--out-dir', default=None,
                    help='Where to write <session>_log_findings.csv (default: alongside the .json.log)')
    ap.add_argument('--warnings-over', type=int, default=0,
                    help='Surface warning groups whose noise-filtered count >= N (default: warnings hidden)')
    args = ap.parse_args()

    log_path = resolve_input(args.input)
    exc_groups, err_groups, warn_emit, warn_counts, stats = process(log_path, args.warnings_over)

    print_summary(log_path, exc_groups, err_groups, warn_emit, warn_counts, stats, args.warnings_over)

    session_dir = log_path.parent
    session_name = session_dir.name
    out_dir = Path(args.out_dir) if args.out_dir else session_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{session_name}_log_findings.csv"
    write_csv(csv_path, exc_groups, err_groups, warn_emit, warn_counts)
    print(f"\nCSV written: {csv_path}")


if __name__ == '__main__':
    main()

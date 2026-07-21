#!/usr/bin/env python3
"""Decode the FrameStampOverlay stamp from a device video capture.

Produces a CSV mapping every video frame to the app frame (Time.frameCount) whose
render it shows — join against the "fc" field in a profiler-dump JSON to go from
video moment to profiler frame (and back).

Usage:
    python video_framestamp_decode.py capture.mp4 [--roi X,Y,W,H] [--out map.csv]
    python video_framestamp_decode.py capture.mp4 --roi ... --lookup 123456

Without --roi an interactive picker opens on the first frame: drag a rectangle
tightly around the stamp quad (all three bands), press ENTER.

Recording the video: any capture of the headset view works — e.g. host-side
    scrcpy --record capture.mp4
or the Quest's native recorder. The stamp makes the sync recorder-agnostic.

Stamp layout (must match Assets/Resources/Debug/Shaders/FrameStamp.shader):
    26 columns x 8 cell-rows. Top quarter: [white ref][24 data bits MSB first][black ref].
    Next quarter: [white ref][8 checksum bits MSB first][black fill]. Bottom half: digits.
    checksum = (b0 + b1 + b2) % 256 over the little-endian bytes of the 24-bit value.
"""

import argparse
import csv
import sys

import cv2
import numpy as np

N_COLS = 26
N_DATA_BITS = 24
BITS_ROW_Y = 0.125   # band centers, relative to ROI height, video y-down
CHK_ROW_Y = 0.375
MIN_CONTRAST = 40    # min white-ref minus black-ref (gray levels) to trust a frame


def sample_cell(gray_roi, x_rel, y_rel):
    """Mean of a 3x3 patch at a relative position inside the ROI."""
    h, w = gray_roi.shape
    x = min(max(int(x_rel * w), 1), w - 2)
    y = min(max(int(y_rel * h), 1), h - 2)
    return float(gray_roi[y - 1:y + 2, x - 1:x + 2].mean())


def decode_row(gray_roi, y_rel, n_bits, first_bit_col=1):
    """Returns (value, white_ref, black_ref) for a marker-framed bit row."""
    white = sample_cell(gray_roi, 0.5 / N_COLS, y_rel)
    black = sample_cell(gray_roi, (N_COLS - 0.5) / N_COLS, y_rel)
    threshold = (white + black) / 2.0
    value = 0
    for i in range(n_bits):
        c = first_bit_col + i
        bit = 1 if sample_cell(gray_roi, (c + 0.5) / N_COLS, y_rel) > threshold else 0
        value = (value << 1) | bit
    return value, white, black


def decode_frame(gray_roi):
    """Returns the decoded app frame number, or None if invalid/absent."""
    value, white, black = decode_row(gray_roi, BITS_ROW_Y, N_DATA_BITS)
    if white - black < MIN_CONTRAST:
        return None  # stamp not visible (menu, fade, occlusion) or bad ROI
    checksum, _, _ = decode_row(gray_roi, CHK_ROW_Y, 8)
    b0, b1, b2 = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    if (b0 + b1 + b2) % 256 != checksum:
        return None
    return value


def pick_roi(video_path):
    cap = cv2.VideoCapture(video_path)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        sys.exit(f"error: cannot read first frame of {video_path}")
    print("Drag a rectangle tightly around the stamp quad, then press ENTER.")
    x, y, w, h = cv2.selectROI("select frame stamp", frame, showCrosshair=False)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        sys.exit("error: empty ROI selected")
    return int(x), int(y), int(w), int(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="video file (mp4 etc.)")
    ap.add_argument("--roi", help="stamp bounds in video pixels: X,Y,W,H (omit for interactive picker)")
    ap.add_argument("--out", help="output CSV path (default: <video>.framemap.csv)")
    ap.add_argument("--lookup", type=int, metavar="APP_FRAME",
                    help="just print the video timestamp(s) showing this app frame")
    args = ap.parse_args()

    if args.roi:
        x, y, w, h = (int(v) for v in args.roi.split(","))
    else:
        x, y, w, h = pick_roi(args.video)
        print(f"ROI: --roi {x},{y},{w},{h}  (reuse to skip the picker)")

    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    rows = []          # (video_frame, time_s, app_frame or "")
    valid = 0
    prev_fc = None
    non_monotonic = 0
    vf = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
        fc = decode_frame(gray)
        t = vf / fps
        if fc is not None:
            valid += 1
            if prev_fc is not None and fc < prev_fc:
                non_monotonic += 1
                fc = None  # corrupt read that passed the checksum by luck
            else:
                prev_fc = fc
        rows.append((vf, round(t, 4), fc if fc is not None else ""))
        vf += 1
    cap.release()

    if args.lookup is not None:
        hits = [r for r in rows if r[2] != "" and r[2] >= args.lookup]
        if not hits:
            sys.exit(f"app frame {args.lookup} not found (decoded range ends before it)")
        first = hits[0]
        print(f"app frame {args.lookup} -> video frame {first[0]} @ {first[1]:.3f}s"
              f" (decoded {first[2]}, first frame at/after request)")
        return

    out_path = args.out or args.video + ".framemap.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["video_frame", "time_s", "app_frame"])
        writer.writerows(rows)

    decoded = [r[2] for r in rows if r[2] != ""]
    print(f"{len(rows)} video frames @ {fps:.2f}fps, {valid} decoded ({100 * valid / max(len(rows), 1):.1f}%),"
          f" {non_monotonic} rejected as non-monotonic")
    if decoded:
        span = decoded[-1] - decoded[0]
        print(f"app frames {decoded[0]}..{decoded[-1]} ({span} frames over {rows[-1][1]:.1f}s"
              f" -> avg {span / max(rows[-1][1], 1e-6):.1f} app fps)")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

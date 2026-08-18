"""Parse a pulled il2cpplab capture into il2cpplab.db, zip it with the capture's video,
and delete the raw capture + symbols.

Runs on the automation runner right after the capture is pulled, so a test run uploads
one readable artifact instead of the capture files plus the ~2 GB symbol pair needed to
read them. The db is self-contained: parsing resolves every site and stack frame to a
name, so nothing downstream needs sites.db or libil2cpp.so again.

video.mp4 rides in the same zip because it cannot be folded into the db: the viewer seeks
it, and seeking an inter-frame-compressed stream means the decoder jumping to the keyframe
before the target frame and decoding forward, which needs a real file. Zipping it neither
shrinks nor degrades it - the headset's hardware encoder already produced a compressed
stream, and this only packages the run as one artifact.

    parse_il2cpplab.py <capture_dir> <tool_dir> [--out ZIP]

<capture_dir> is the flattened "C++ Profiler" folder (capture-*.alcz, modules.txt, and a
"Symbol Parser" subfolder holding sites.db + the symbol artifact). <tool_dir> is the
checked-out .claude/tools/il2cpplab from the build under test.

The run's game log has to be on disk before this runs: il2cpplab's parse finds it beside
the capture (the run's "Report Logs" folder) and attaches it into the db, which is where
the viewer's Logs tab reads it from.

Deletes nothing unless the parse and the zip both succeed - a failure here must leave the
run's only copy of the capture on disk, so the raw files can still be uploaded and parsed
by hand. Exit code 0 means the zip is ready; non-zero means the caller should fall back to
uploading the capture directory as-is.
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

SYMBOL_DIR_NAME = "Symbol Parser"
# videolab's per-frame video, in the capture dir beside the capture files. Kept in the zip
# next to the db, which is where the viewer needs it: it resolves a capture's video as
# <captures dir>/<id>/video.mp4 and range-serves it to the browser's decoder.
VIDEO_FILE_NAME = "video.mp4"
# The platform symbol artifact il2cpplab resolves return addresses against, by the name
# the build sidecar gives it. Android first: these runs are Quest captures.
SYMBOL_ARTIFACTS = ("libil2cpp.so", "GameAssembly.pdb")


def find_inputs(capture_dir):
    """sites.db (required) and the symbol artifact (optional - without it the parse still
    produces a db, with native frames left as module+offset).

    Searched recursively: build.yaml uploads the whole "<player>.il2cpplab" sidecar
    directory as the symbols artifact, so both files arrive one level down."""
    symbols_dir = capture_dir / SYMBOL_DIR_NAME
    sites = next(iter(sorted(symbols_dir.rglob("sites.db"))), None)
    if sites is None:
        found = sorted(p.name for p in symbols_dir.rglob("*") if p.is_file())[:10]
        print(f"[IL2CPPLAB] no sites.db under '{symbols_dir}' - cannot parse. "
              f"Found instead: {', '.join(found) if found else '(nothing)'}")
        return None, None
    artifact = next((p for n in SYMBOL_ARTIFACTS
                     for p in sorted(symbols_dir.rglob(n)) if p.is_file()), None)
    if artifact is None:
        print(f"[IL2CPPLAB] WARNING: no symbol artifact under '{symbols_dir}' "
              f"({' / '.join(SYMBOL_ARTIFACTS)}) - native frames will stay unresolved.")
    return sites, artifact


def print_degradations(db_path):
    """The parse records what it could not do (missing symbols, capture gaps, a base
    mismatch) in the session row rather than failing. Echo it so a degraded run is
    visible in the CI log instead of only inside the uploaded db."""
    try:
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT config_json FROM session ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        notes = json.loads(row[0]).get("degradations", []) if row and row[0] else []
    except (sqlite3.Error, ValueError, TypeError) as e:
        print(f"[IL2CPPLAB] could not read degradations from the db: {e}")
        return
    if notes:
        print(f"[IL2CPPLAB] parse reported {len(notes)} degradation(s):")
        for n in notes:
            print(f"    - {n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture_dir", type=Path)
    ap.add_argument("tool_dir", type=Path, help=".claude/tools/il2cpplab of the build under test")
    ap.add_argument("--out", type=Path, help="output zip (default: <capture_dir>/../il2cpplab.db.zip)")
    args = ap.parse_args()

    capture_dir = args.capture_dir
    entry = args.tool_dir / "il2cpplab.py"
    if not entry.is_file():
        print(f"[IL2CPPLAB] il2cpplab.py not found at '{entry}' - skipping parse.")
        return 1
    if not any(capture_dir.glob("capture-*")):
        print(f"[IL2CPPLAB] no capture files in '{capture_dir}' - skipping parse.")
        return 1

    sites, artifact = find_inputs(capture_dir)
    if sites is None:
        return 1

    out_zip = args.out or capture_dir.parent / "il2cpplab.db.zip"
    db_path = capture_dir.parent / "il2cpplab.db"
    cmd = [sys.executable, str(entry), "parse", str(capture_dir),
           "--sites", str(sites), "--out", str(db_path)]
    if artifact is not None:
        cmd += ["--symbols", str(artifact)]

    print(f"[IL2CPPLAB] Parsing capture -> {db_path.name} (several minutes)...")
    result = subprocess.run(cmd)
    if result.returncode != 0 or not db_path.is_file():
        print(f"[IL2CPPLAB] parse failed (exit {result.returncode}) - "
              "keeping the raw capture so it can be parsed by hand.")
        db_path.unlink(missing_ok=True)
        return 1
    print_degradations(db_path)

    db_mb = db_path.stat().st_size / (1024 * 1024)
    video = capture_dir / VIDEO_FILE_NAME
    print(f"[IL2CPPLAB] Compressing {db_mb:.0f} MB db -> {out_zip.name}...")
    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(db_path, db_path.name)
            if video.is_file():
                video_mb = video.stat().st_size / (1024 * 1024)
                print(f"[IL2CPPLAB] Adding {VIDEO_FILE_NAME} ({video_mb:.0f} MB, stored "
                      "as-is - an encoded stream does not deflate)...")
                z.write(video, video.name, compress_type=zipfile.ZIP_STORED)
            else:
                print(f"[IL2CPPLAB] no {VIDEO_FILE_NAME} in the capture - "
                      "not a videoTracking build.")
    except OSError as e:
        print(f"[IL2CPPLAB] compression failed ({e}) - keeping the raw capture.")
        out_zip.unlink(missing_ok=True)
        db_path.unlink(missing_ok=True)
        return 1
    zip_mb = out_zip.stat().st_size / (1024 * 1024)
    print(f"[IL2CPPLAB] {out_zip.name} ready ({zip_mb:.0f} MB).")

    # Only now is the capture redundant: everything it carried is resolved into the db or
    # copied into the zip (the video is the one file the db cannot absorb). il2cpplab's parse
    # puts its own copy of the video beside the db it writes - that copy goes too, or it
    # outlives the run as an unreferenced few hundred MB on the runner.
    db_path.unlink(missing_ok=True)
    (db_path.parent / VIDEO_FILE_NAME).unlink(missing_ok=True)
    shutil.rmtree(capture_dir, ignore_errors=True)
    if capture_dir.exists():
        print(f"[IL2CPPLAB] WARNING: could not fully delete '{capture_dir}'.")
    else:
        print(f"[IL2CPPLAB] Deleted the raw capture and symbols from '{capture_dir.name}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

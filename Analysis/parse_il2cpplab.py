"""Parse a pulled il2cpplab capture into a single il2cpplab.db, zip it, and delete the
raw capture + symbols.

Runs on the automation runner right after the capture is pulled, so a test run uploads
one readable artifact instead of the capture files plus the ~2 GB symbol pair needed to
read them. The db is self-contained: parsing resolves every site and stack frame to a
name, so nothing downstream needs sites.db or libil2cpp.so again.

    parse_il2cpplab.py <capture_dir> <tool_dir> [--out ZIP]

<capture_dir> is the flattened "C++ Profiler" folder (capture-*.alcz, modules.txt, and a
"Symbol Parser" subfolder holding sites.db + the symbol artifact). <tool_dir> is the
checked-out .claude/tools/il2cpplab from the build under test.

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
# The platform symbol artifact il2cpplab resolves return addresses against, by the name
# the build sidecar gives it. Android first: these runs are Quest captures.
SYMBOL_ARTIFACTS = ("libil2cpp.so", "GameAssembly.pdb")


def find_inputs(capture_dir):
    """sites.db (required) and the symbol artifact (optional - without it the parse still
    produces a db, with native frames left as module+offset)."""
    symbols_dir = capture_dir / SYMBOL_DIR_NAME
    sites = symbols_dir / "sites.db"
    if not sites.is_file():
        print(f"[IL2CPPLAB] no sites.db under '{symbols_dir}' - cannot parse.")
        return None, None
    artifact = next((symbols_dir / n for n in SYMBOL_ARTIFACTS
                     if (symbols_dir / n).is_file()), None)
    if artifact is None:
        print(f"[IL2CPPLAB] WARNING: no symbol artifact in '{symbols_dir}' "
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
    print(f"[IL2CPPLAB] Compressing {db_mb:.0f} MB db -> {out_zip.name}...")
    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(db_path, db_path.name)
    except OSError as e:
        print(f"[IL2CPPLAB] compression failed ({e}) - keeping the raw capture.")
        out_zip.unlink(missing_ok=True)
        db_path.unlink(missing_ok=True)
        return 1
    zip_mb = out_zip.stat().st_size / (1024 * 1024)
    print(f"[IL2CPPLAB] {out_zip.name} ready ({zip_mb:.0f} MB, {zip_mb / db_mb:.0%} of the db).")

    # Only now is the capture redundant: everything it carried is resolved into the db,
    # and the db is safely inside the zip.
    db_path.unlink(missing_ok=True)
    shutil.rmtree(capture_dir, ignore_errors=True)
    if capture_dir.exists():
        print(f"[IL2CPPLAB] WARNING: could not fully delete '{capture_dir}'.")
    else:
        print(f"[IL2CPPLAB] Deleted the raw capture and symbols from '{capture_dir.name}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

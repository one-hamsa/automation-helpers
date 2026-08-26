r"""
Post-test processing for UNDERDOGS automation.
Generates an App fps Time graph from the CSV report and uploads
all test files (CSV, mp4, graph PNG) to Google Drive AND GitHub Pages.

Uses OAuth2 with a saved token. First run requires a browser login
on the runner machine; after that the token auto-refreshes and
all subsequent runs (including CI) are fully automated.

Usage:
    python DriveUpload.py <test_dir> <folderName> [--graph-only] [--upload-only]
"""

import sys
import os
import csv
import glob
import json
import base64
import io
import math
import statistics
import re
import zipfile
import argparse
import subprocess
import time
from pathlib import Path
import requests

# Repo-relative path to the log parser.
# UploadFiles.py lives in <repo>/ci/Bots Performance Test/, parsers in <repo>/ci/Analysis/.
PARSERS_DIR = Path(__file__).resolve().parent.parent / "Analysis"
LOG_PARSER = PARSERS_DIR / "log_parser.py"
FUNCTIONALITY_CHECKS = PARSERS_DIR / "functionality_checks.py"

# Old unity profiler deprecated setup
# PROFILER_PARSER = PARSERS_DIR / "profiler_parser.py"


def run_parsers(test_dir, profiler_raw_path):
    """Parse the bots-test log in-place.

    Writes <session>_log_findings.csv next to Global.json.log
    (i.e. test_dir/Report Logs/<session>/).

    CPU profiling is il2cpplab, parsed by Analysis/parse_il2cpplab.py during the test
    run; profiler_raw_path is the old editor-profiler recording and is no longer produced.
    """
    py = sys.executable or "python"

    if not LOG_PARSER.is_file():
        print(f"[PARSE] log_parser.py not found at {LOG_PARSER}, skipping log parse.")
    else:
        print(f"[PARSE] Running log_parser on {test_dir}")
        try:
            subprocess.run(
                [py, str(LOG_PARSER), test_dir],
                check=False,
            )
        except Exception as e:
            print(f"[PARSE] log_parser crashed: {e}")

    # Old unity profiler deprecated setup
    # if not profiler_raw_path or not os.path.isfile(profiler_raw_path):
    #     print("[PARSE] No UNITY profiler recording to parse, skipping unity profiler parse.")
    # elif not PROFILER_PARSER.is_file():
    #     print(f"[PARSE] profiler_parser.py not found at {PROFILER_PARSER}, skipping profiler parse.")
    # else:
    #     print(f"[PARSE] Running profiler_parser on {profiler_raw_path}")
    #     try:
    #         subprocess.run(
    #             [py, str(PROFILER_PARSER), profiler_raw_path],
    #             check=False,
    #         )
    #     except Exception as e:
    #         print(f"[PARSE] profiler_parser crashed: {e}")


def run_functionality_checks(test_dir, expected_players=None):
    """Decide whether the run was a real game - both clients in the room, the room the size
    it should have been, players dying - and return the verdicts, or None.

    Writes functionality.json into the test folder. Runs once both clients' logs are in
    place, so it sees what everything else will ship. A crash here costs the checks,
    never the upload.
    """
    if not FUNCTIONALITY_CHECKS.is_file():
        print(f"[CHECKS] functionality_checks.py not found at {FUNCTIONALITY_CHECKS}, skipping.")
        return None

    py = sys.executable or "python"
    cmd = [py, str(FUNCTIONALITY_CHECKS), test_dir]
    if expected_players is not None:
        cmd += ["--expected-players", str(expected_players)]
    print(f"[CHECKS] Running functionality checks on {test_dir}")
    try:
        subprocess.run(cmd, check=False)
    except Exception as e:
        print(f"[CHECKS] functionality_checks crashed: {e}")
        return None

    checks_path = os.path.join(test_dir, CHECKS_JSON)
    if not os.path.isfile(checks_path):
        print(f"[CHECKS] No {CHECKS_JSON} produced, skipping the checklist.")
        return None
    try:
        with open(checks_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"[CHECKS] Could not read {CHECKS_JSON}: {e}")
        return None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Google Drive shared folder ID.
DRIVE_PARENT_FOLDER_ID = "1Ckhix2o8tbz3VA6i25UQ1jf7JKx5bkQD"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# The parsed il2cpplab capture: one self-contained, already-symbolized db, zipped by
# Analysis/parse_il2cpplab.py. This is the normal artifact for a tracking build.
PROFILER_DB_ZIP = "il2cpplab.db.zip"

# Subfolders of the test folder holding the raw il2cpplab capture and the sites.db +
# symbol artifact needed to parse it, written by "Quest Bots Runner.bat". Present only
# when the parse step could not run or failed; the Drive folders carry the same names.
CPP_PROFILER_DIR = "C++ Profiler"
SYMBOL_PARSER_DIR = "Symbol Parser"

# The runner's own logs for the run, zipped out of LOG_FILES_DIR. Drive only - logcat
# alone is several MB, which is permanent weight in the GitHub Pages repo.
RUNNER_LOGS_ZIP = "Runner Logs.zip"

# The run's functionality verdicts, written by Analysis/functionality_checks.py.
CHECKS_JSON = "functionality.json"

# OAuth credentials come from the environment - the workflow step maps them in from
# repo secrets, and the .bat inherits them. There is no on-disk fallback and no
# interactive login, so a rebuilt runner needs no browser session to upload again.
# token_uri is not a secret, just the fixed Google endpoint.
DRIVE_TOKEN_URI = "https://oauth2.googleapis.com/token"
REQUIRED_DRIVE_VARS = ("GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN")

# GitHub Pages configuration
# PAT is supplied only via the --github-token CLI arg (the .bat passes it,
# sourced from the UPLOAD_TO_AUTOMATION_REPOS_PAT workflow secret); set in main().
GITHUB_TOKEN = ""
GITHUB_REPO_OWNER = "TheTripleL123"
GITHUB_REPO_NAME = "Bots-Automation-Tests"
GITHUB_BRANCH = "main"

GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/contents"
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


def metric_stats(values, lower_is_better):
    """Average, median, and the difference between them - App T average minus median,
    FPS median minus average. A higher difference means more spikes."""
    average = sum(values) / len(values)
    median = statistics.median(values)
    return {
        "average": average,
        "median": median,
        "diff": (average - median) if lower_is_better else (median - average),
    }


# ---------------------------------------------------------------------------
# Graph generation
# ---------------------------------------------------------------------------
def read_metric_series(test_dir, column):
    """Read one OVR metrics column from the run's CSV.

    Returns (times_sec, values) for samples after the 60s game-load window,
    or (None, None) if the CSV or the column has no usable data.
    """
    csv_pattern = os.path.join(test_dir, "CSV_REPORT*.csv")
    csv_files = glob.glob(csv_pattern)
    if not csv_files:
        print(f"  ERROR: No CSV file found matching {csv_pattern}")
        return None, None

    csv_path = csv_files[0]
    print(f"  Reading CSV: {csv_path} (column: {column})")

    times_sec = []
    values = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t = float(row["Time Stamp"]) / 1000.0
                v = float(row[column])
                times_sec.append(t)
                values.append(v)
            except (ValueError, KeyError, TypeError):
                continue

    if not times_sec:
        print(f"  ERROR: No valid '{column}' rows found in CSV.")
        return None, None

    print(f"  CSV has {len(times_sec)} rows, time range: {times_sec[0]:.1f}s - {times_sec[-1]:.1f}s")

    filtered = [(t, v) for t, v in zip(times_sec, values) if t >= 60.0]
    if not filtered:
        print(f"  ERROR: No data points after 60 seconds (max timestamp: {times_sec[-1]:.1f}s).")
        print("  Hint: OVR metrics capture was too short — check device connection and game load time.")
        return None, None

    times_sec, values = zip(*filtered)
    return list(times_sec), list(values)


def compute_app_time_stats(test_dir):
    """App T (app GPU time, microseconds) average, median and their difference.
    None if unavailable."""
    _, values = read_metric_series(test_dir, "app_gpu_time_microseconds")
    if not values:
        return None
    stats = metric_stats(values, lower_is_better=True)
    print(f"  App T - average: {stats['average']:.0f} us, median: {stats['median']:.0f} us, "
          f"difference: {stats['diff']:.0f} us (over {len(values)} samples after 60s)")
    return stats


def generate_graph(test_dir, folderName):
    """Read the CSV and produce a fps Utilization line chart.
    Returns (graph_path, fps_stats) or (None, None) on failure.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times_sec, fps_values = read_metric_series(test_dir, "average_frame_rate")
    if not times_sec:
        return None, None

    print(f"  Plotting {len(times_sec)} data points (after 60s)...")

    stats = metric_stats(fps_values, lower_is_better=False)
    min_val = min(fps_values)
    max_val = max(fps_values)
    print(f"  FPS - average: {stats['average']:.1f}, median: {stats['median']:.1f}, "
          f"difference: {stats['diff']:.1f} (over {len(fps_values)} samples after 60s)")

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(times_sec, fps_values, linewidth=1.2, color="#2563eb", label="FPS")
    # The headline number gets the emphatic line - it is what the run is judged on.
    ax.axhline(y=stats["average"], color="#16a34a", linestyle="--", linewidth=1.2, label=f"Average: {stats['average']:.1f}")
    ax.axhline(y=stats["median"], color="#7c3aed", linestyle="--", linewidth=1.2, label=f"Median: {stats['median']:.1f}")
    ax.axhline(y=min_val, color="#0891b2", linestyle=":",  linewidth=1.2, label=f"Min: {min_val:.0f}")
    ax.axhline(y=max_val, color="#dc2626", linestyle=":",  linewidth=1.2, label=f"Max: {max_val:.0f}")

    ax.set_xlabel("Time (seconds)")
    ax.set_ylabel("FPS")
    ax.set_title(f"{folderName}")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(test_dir, "FPS_GRAPH.png")
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    print(f"  Graph saved: {out_path}")
    return out_path, stats


# ---------------------------------------------------------------------------
# Google Drive upload (OAuth2 with saved token)
# ---------------------------------------------------------------------------
def get_drive_service():
    """Drive client, or None if it cannot be built - callers skip Drive and carry on
    with the GitHub Pages upload rather than losing the whole run."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    values = {name: os.environ.get(name, "").strip() for name in REQUIRED_DRIVE_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        # Names only - a value must never reach the log. All three missing means the
        # secrets never reached this process; one missing means that name is misspelled.
        print(f"  ERROR: Google Drive is not configured: {', '.join(missing)} unset.")
        return None

    # No access token - the refresh below mints one. The stored access token was never
    # worth keeping: it lives an hour, and google-auth refreshes on demand anyway.
    creds = Credentials(
        None,
        refresh_token=values["GOOGLE_DRIVE_REFRESH_TOKEN"],
        client_id=values["GOOGLE_DRIVE_CLIENT_ID"],
        client_secret=values["GOOGLE_DRIVE_CLIENT_SECRET"],
        token_uri=DRIVE_TOKEN_URI,
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except Exception as e:
        # Usually an expired refresh token. Google drops them 7 days after issuance
        # while the OAuth consent screen is in "Testing" status - say so, or the
        # symptom is a bare 400 with no hint at what to fix.
        print(f"  ERROR: Google Drive sign-in failed ({e}). The refresh token may need re-issuing.")
        return None

    return build("drive", "v3", credentials=creds)


def create_drive_folder(service, name, parent_id):
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder.get("id")


def upload_file_drive(service, file_path, folder_id, max_retries=3, drive_name=None):
    import time
    from googleapiclient.http import MediaFileUpload

    file_name = drive_name or os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    mime_map = {".csv": "text/csv", ".png": "image/png"}
    mime_type = mime_map.get(os.path.splitext(file_name)[1].lower(), "application/octet-stream")

    metadata = {"name": file_name, "parents": [folder_id]}

    # Resumable for anything over 5MB. Large files (e.g. the multi-GB profiler
    # .raw) are streamed in 50MB chunks so a single huge request can't time out
    # and we can report progress as it uploads.
    resumable = file_size > 5 * 1024 * 1024
    chunksize = 50 * 1024 * 1024 if resumable else -1

    for attempt in range(1, max_retries + 1):
        media = MediaFileUpload(file_path, mimetype=mime_type, resumable=resumable, chunksize=chunksize)
        request = service.files().create(body=metadata, media_body=media, fields="id,webViewLink")
        try:
            if resumable:
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if status:
                        print(f"      ...{int(status.progress() * 100)}%")
                return response
            return request.execute()
        except Exception as e:
            error_str = str(e)
            is_retryable = any(code in error_str for code in ("500", "502", "503", "429"))
            if is_retryable and attempt < max_retries:
                wait = 2 ** attempt
                print(f"    Transient error (attempt {attempt}/{max_retries}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def upload_to_drive(test_dir, folderName):
    """Upload all test files to Google Drive. Returns the Drive folder link or None."""
    service = get_drive_service()
    if not service:
        return None

    print(f"  Creating Drive folder: {folderName}")
    folder_id = create_drive_folder(service, folderName, DRIVE_PARENT_FOLDER_ID)
    drive_folder_link = f"https://drive.google.com/drive/folders/{folder_id}"

    # Before the "no files to upload" bail below: a run that produced no results is
    # exactly the run whose runner logs are worth having.
    runner_logs = os.path.join(test_dir, RUNNER_LOGS_ZIP)
    if os.path.isfile(runner_logs):
        size_mb = os.path.getsize(runner_logs) / (1024 * 1024)
        print(f"  Uploading: {RUNNER_LOGS_ZIP} ({size_mb:.1f} MB)...")
        upload_file_drive(service, runner_logs, folder_id)

    extensions = ("*.csv", "*.png")
    files_to_upload = []
    for ext in extensions:
        files_to_upload.extend(glob.glob(os.path.join(test_dir, ext)))

    if not files_to_upload:
        print("  WARNING: No files found to upload.")
        return None

    for file_path in files_to_upload:
        file_name = os.path.basename(file_path)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        print(f"  Uploading: {file_name} ({file_size_mb:.1f} MB)...")
        result = upload_file_drive(service, file_path, folder_id)
        link = result.get("webViewLink", "")
        print(f"    Done. {link}")

    # Upload the "Report Logs" folder (if present) as a subfolder on Drive,
    # walking subdirectories recursively so nested log files are included.
    report_logs_dir = os.path.join(test_dir, "Report Logs")
    if os.path.isdir(report_logs_dir):
        print(f"  Uploading 'Report Logs' folder to Drive...")
        logs_folder_id = create_drive_folder(service, "Report Logs", folder_id)
        # adb pull creates a timestamped subfolder inside Report Logs,
        # so collect all files recursively and upload them flat into
        # the "Report Logs" Drive folder.
        log_file_count = 0
        for dirpath, _, filenames in os.walk(report_logs_dir):
            for fname in filenames:
                file_path = os.path.join(dirpath, fname)
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                print(f"    Uploading log: {fname} ({file_size_mb:.1f} MB)...")
                upload_file_drive(service, file_path, logs_folder_id)
                log_file_count += 1
        if log_file_count == 0:
            print("  WARNING: 'Report Logs' folder exists but no files found inside!")
        else:
            print(f"  Uploaded {log_file_count} log file(s) to Drive.")

    checks_path = os.path.join(test_dir, CHECKS_JSON)
    if os.path.isfile(checks_path):
        print(f"  Uploading: {CHECKS_JSON}...")
        upload_file_drive(service, checks_path, folder_id)

    upload_cpp_profiler_to_drive(service, test_dir, folder_id)

    print(f"  All files uploaded to Drive folder: {folderName}")
    return drive_folder_link


def _upload_dir_flat(service, local_dir, drive_folder_id, indent="    ", skip_dirs=()):
    """Upload every file under local_dir into one Drive folder, ignoring the local
    subfolder layout. Returns the number of files uploaded. Names that repeat across
    subfolders are prefixed with their folder so nothing is silently overwritten."""
    used_names = set()
    count = 0
    for dirpath, dirnames, filenames in os.walk(local_dir):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in sorted(filenames):
            file_path = os.path.join(dirpath, fname)
            name = fname
            if name in used_names:
                name = f"{os.path.basename(dirpath)}_{fname}"
            used_names.add(name)
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            print(f"{indent}Uploading: {name} ({size_mb:.1f} MB)...")
            upload_file_drive(service, file_path, drive_folder_id, drive_name=name)
            count += 1
    return count


def upload_cpp_profiler_to_drive(service, test_dir, run_folder_id):
    """Upload the run's il2cpplab CPU capture. Normally that is the parsed, zipped db -
    one file, already symbolized, readable on its own. Only when the parse step could not
    run or failed does the raw capture survive on disk, and then it is uploaded as a
    'C++ Profiler' folder with the build's sites.db + symbol artifact under
    'Symbol Parser', so it can still be parsed by hand.

    Drive-only (like the old profiler .raw) - far too big for GitHub.
    """
    db_zip = os.path.join(test_dir, PROFILER_DB_ZIP)
    if os.path.isfile(db_zip):
        size_mb = os.path.getsize(db_zip) / (1024 * 1024)
        print(f"  Uploading: {PROFILER_DB_ZIP} ({size_mb:.1f} MB)...")
        result = upload_file_drive(service, db_zip, run_folder_id)
        print(f"    Done. {result.get('webViewLink', '')}")
        return

    capture_dir = os.path.join(test_dir, CPP_PROFILER_DIR)
    symbols_dir = os.path.join(capture_dir, SYMBOL_PARSER_DIR)
    if not os.path.isdir(capture_dir):
        return
    print("  WARNING: no parsed il2cpplab db - uploading the raw capture instead.")

    print(f"  Uploading '{CPP_PROFILER_DIR}' folder to Drive...")
    profiler_folder_id = create_drive_folder(service, CPP_PROFILER_DIR, run_folder_id)

    # The symbols live in a subfolder and get their own Drive folder below, so they must
    # not also be swept up as capture files.
    capture_count = _upload_dir_flat(service, capture_dir, profiler_folder_id,
                                     skip_dirs=(SYMBOL_PARSER_DIR,))
    if capture_count == 0:
        print("  WARNING: no il2cpplab capture files to upload - "
              "check that this was a tracking build and the capture actually started.")
    else:
        print(f"  Uploaded {capture_count} capture file(s) to Drive.")

    if not os.path.isdir(symbols_dir):
        print("  WARNING: no il2cpplab symbols in the test folder - the capture cannot be "
              "parsed without them (Build_<code>_Profiler_Symbols artifact).")
        return

    print(f"  Uploading '{SYMBOL_PARSER_DIR}' folder to Drive...")
    symbols_folder_id = create_drive_folder(service, SYMBOL_PARSER_DIR, profiler_folder_id)
    symbol_count = _upload_dir_flat(service, symbols_dir, symbols_folder_id)
    if symbol_count == 0:
        print("  WARNING: 'Symbol Parser' folder is empty!")
    else:
        print(f"  Uploaded {symbol_count} symbol file(s) to Drive.")


# ---------------------------------------------------------------------------
# GitHub Pages upload
# ---------------------------------------------------------------------------
# The dashboard only ever reads the current tree, but git keeps every blob it has
# ever seen — so whatever we upload today is repo weight forever. Two rules follow:
#   * shrink before uploading: screenshots go up as WebP (~20x smaller than the
#     Quest PNG, alpha intact) and Report Logs as one zip (~16x). Google Drive
#     still gets the full-size originals.
#   * one commit per run, via the Git Data API. The Contents API can only touch
#     one path per call, which cost a commit per file (~150 per run).
GITHUB_REPO_URL = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
SCREENSHOT_WEBP_QUALITY = 85
SCREENSHOT_PNG = re.compile(r"^SCREENSHOT_\d+\.png$", re.IGNORECASE)


def _webp_bytes(png_path):
    """Re-encode a screenshot as WebP for the dashboard thumbnails."""
    from PIL import Image
    buf = io.BytesIO()
    Image.open(png_path).save(buf, "WEBP", quality=SCREENSHOT_WEBP_QUALITY, method=4)
    return buf.getvalue()


def _write_webp(png_path):
    """Write the WebP next to the screenshot it came from. On disk rather than only in
    memory because the nightly report attaches these to Discord: three Quest PNGs are
    ~10 MB and would not fit, the same three as WebP are well under a megabyte.
    Returns the path, or None.
    """
    webp_path = f"{os.path.splitext(png_path)[0]}.webp"
    try:
        with open(webp_path, "wb") as f:
            f.write(_webp_bytes(png_path))
    except Exception as e:
        print(f"[SCREENSHOT] WARNING: Could not write {os.path.basename(webp_path)}: {e}")
        return None
    return webp_path


def _zip_bytes(root_dir):
    """Zip a directory tree, preserving paths relative to root_dir."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                z.write(full, os.path.relpath(full, root_dir).replace("\\", "/"))
    return buf.getvalue()


def _write_runner_logs_zip(test_dir):
    """Zip the runner's own logs for this run - the combined .bat console log and the
    device logcat - into the test folder, so they leave the rig with the results instead
    of being overwritten by the next run. Returns the zip path, or None.

    LOG_FILES_DIR is set by "Run Both Tests.bat" and inherited from there. Both runners
    have finished by the time this script starts, so their output is complete; the only
    thing the zip cannot contain is this script's own output, which is still being written
    into the same log.
    """
    logs_dir = os.environ.get("LOG_FILES_DIR")
    if not logs_dir or not os.path.isdir(logs_dir):
        print(f"[RUNNER LOGS] Nothing to zip ({logs_dir or 'LOG_FILES_DIR unset'}), skipping.")
        return None

    zip_path = os.path.join(test_dir, RUNNER_LOGS_ZIP)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for dirpath, _, filenames in os.walk(logs_dir):
                for fname in sorted(filenames):
                    full = os.path.join(dirpath, fname)
                    arcname = os.path.relpath(full, logs_dir).replace("\\", "/")
                    try:
                        z.write(full, arcname)
                    except OSError as e:
                        print(f"[RUNNER LOGS] WARNING: skipped {arcname}: {e}")
    except OSError as e:
        print(f"[RUNNER LOGS] WARNING: could not write {RUNNER_LOGS_ZIP}: {e}")
        return None

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print(f"[RUNNER LOGS] {RUNNER_LOGS_ZIP} ({size_mb:.1f} MB) from {logs_dir}")
    return zip_path


def _github_create_blob(data):
    """Upload one file's bytes as a git blob; returns its SHA."""
    r = requests.post(f"{GITHUB_REPO_URL}/git/blobs", headers=GITHUB_HEADERS, json={
        "content": base64.b64encode(data).decode(),
        "encoding": "base64",
    })
    r.raise_for_status()
    return r.json()["sha"]


def _github_commit(files, message, max_retries=3):
    """Commit {repo_path: bytes} as a single commit on GITHUB_BRANCH.

    Blobs are uploaded once; only the tree/commit/ref steps are retried, which is
    what a concurrent push (another test run, the retention job) invalidates.
    """
    tree = []
    for repo_path, data in sorted(files.items()):
        print(f"  Blob: {repo_path} ({len(data) / (1024 * 1024):.2f} MB)")
        tree.append({
            "path": repo_path,
            "mode": "100644",
            "type": "blob",
            "sha": _github_create_blob(data),
        })

    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(f"{GITHUB_REPO_URL}/git/ref/heads/{GITHUB_BRANCH}", headers=GITHUB_HEADERS)
            r.raise_for_status()
            parent_sha = r.json()["object"]["sha"]

            r = requests.get(f"{GITHUB_REPO_URL}/git/commits/{parent_sha}", headers=GITHUB_HEADERS)
            r.raise_for_status()
            base_tree_sha = r.json()["tree"]["sha"]

            r = requests.post(f"{GITHUB_REPO_URL}/git/trees", headers=GITHUB_HEADERS,
                              json={"base_tree": base_tree_sha, "tree": tree})
            r.raise_for_status()
            new_tree_sha = r.json()["sha"]

            r = requests.post(f"{GITHUB_REPO_URL}/git/commits", headers=GITHUB_HEADERS,
                              json={"message": message, "tree": new_tree_sha, "parents": [parent_sha]})
            r.raise_for_status()
            commit_sha = r.json()["sha"]

            r = requests.patch(f"{GITHUB_REPO_URL}/git/refs/heads/{GITHUB_BRANCH}",
                               headers=GITHUB_HEADERS, json={"sha": commit_sha})
            r.raise_for_status()
            print(f"  GitHub: committed {len(tree)} file(s) as {commit_sha[:10]}")
            return commit_sha
        except requests.HTTPError as e:
            if attempt == max_retries:
                raise
            print(f"  GitHub: commit attempt {attempt} failed ({e}), retrying...")
            time.sleep(5)


def _parse_folder_name(folder_name):
    """Parse test name and timestamp from the folder name.
    Expected format: BOTS TEST - Name(TestName) - Started at( timestamp )
    """
    test_name = "-"
    timestamp = "-"

    m = re.search(r'Name\(([^)]*)\)', folder_name)
    if m:
        test_name = m.group(1).strip()

    m = re.search(r'Started At\(\s*([^)]*)\s*\)', folder_name)
    if m:
        timestamp = m.group(1).strip()

    return test_name, timestamp


def _find_report_log(test_dir, filename):
    """Locate a log file under the run's 'Report Logs' folder (adb pull creates
    a timestamped subfolder). Returns the most-recent match, or None."""
    pattern = os.path.join(test_dir, "Report Logs", "*", filename)
    matches = glob.glob(pattern)
    if not matches:
        matches = glob.glob(os.path.join(test_dir, "Report Logs", "**", filename), recursive=True)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _find_udlog(test_dir):
    """The session log archive pulled off the device, or None. The game zips a session's
    logs into one .udlog, so this is where the individual logs live now."""
    matches = glob.glob(os.path.join(test_dir, "Report Logs", "**", "*.udlog"), recursive=True)
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _read_report_log(test_dir, filename):
    """Read one of the session's logs as text, whether it was pulled loose or zipped into
    a .udlog. Returns (label, text), or (None, None) if neither holds it."""
    log_path = _find_report_log(test_dir, filename)
    if log_path:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return os.path.basename(log_path), f.read()

    udlog = _find_udlog(test_dir)
    if not udlog:
        return None, None
    try:
        with zipfile.ZipFile(udlog) as z:
            if filename not in z.namelist():
                return None, None
            return f"{os.path.basename(udlog)}/{filename}", z.read(filename).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError) as e:
        print(f"  WARNING: could not read {filename} from {os.path.basename(udlog)}: {e}")
        return None, None


def _count_bots_joined(test_dir):
    """Count players that connected during the run (the XR bot + the PC bots).
    Counts 'Player connected:' lines in the session's Global.log.
    Returns an int, or None if no Global.log is found.
    """
    label, text = _read_report_log(test_dir, "Global.log")
    if text is None:
        print("[BOTS] No Global.log found, skipping bot count.")
        return None
    count = sum(1 for line in text.splitlines() if "Player connected:" in line)
    print(f"[BOTS] {count} player(s) connected (from {label})")
    return count


def _read_log_stats(test_dir):
    """Sum error and exception counts from the *_log_findings.csv that
    log_parser.py writes alongside Global.json.log. Returns (errors, exceptions),
    or (None, None) if no findings CSV is present.
    """
    csv_path = _find_report_log(test_dir, "*_log_findings.csv")
    if not csv_path:
        print("[LOGSTATS] No *_log_findings.csv found, skipping error/exception counts.")
        return None, None
    errors = exceptions = 0
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    n = int(row.get("count") or 0)
                except ValueError:
                    n = 0
                if row.get("kind") == "error":
                    errors += n
                elif row.get("kind") == "exception":
                    exceptions += n
    except Exception as e:
        print(f"[LOGSTATS] Failed to read {csv_path}: {e}")
        return None, None
    print(f"[LOGSTATS] errors={errors}, exceptions={exceptions} (from {os.path.basename(csv_path)})")
    return errors, exceptions


def _write_ci_outputs(fps_stats, app_time_stats):
    """Publish the run's headline metrics as GitHub Actions step outputs so the
    workflow can threshold-check them without re-reading the test folder.
    No-op outside CI (GITHUB_OUTPUT unset), and never fails the run.
    """
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    try:
        with open(out_path, "a", encoding="utf-8") as f:
            if fps_stats is not None:
                f.write(f"fps_average={fps_stats['average']:.1f}\n")
                f.write(f"fps_median={fps_stats['median']:.1f}\n")
                f.write(f"fps_diff={fps_stats['diff']:.1f}\n")
            if app_time_stats is not None:
                f.write(f"app_time_average={app_time_stats['average']:.0f}\n")
                f.write(f"app_time_median={app_time_stats['median']:.0f}\n")
                f.write(f"app_time_diff={app_time_stats['diff']:.0f}\n")
        print(f"[CI] Wrote the FPS / App T average, median and difference to {out_path}")
    except Exception as e:
        print(f"[CI] WARNING: Could not write step outputs: {e}")


def _write_ci_checks(checks):
    """Publish the functionality verdicts as a step output, so the nightly report renders
    the same checklist the dashboard shows. Compact JSON on one line - a step output
    cannot carry a raw newline. No-op outside CI, and never fails the run.
    """
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path or not checks:
        return
    try:
        one_line = json.dumps(checks, separators=(",", ":"))
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(f"checks_json={one_line}\n")
        print(f"[CI] Wrote checks_json ({len(checks)} checks) to {out_path}")
    except Exception as e:
        print(f"[CI] WARNING: Could not write the checks output: {e}")


def _build_metadata(folder_name, fps_stats, app_time_stats, drive_link=None, has_thumbnail=False, started_by="unknown", extra=None):
    test_name, timestamp = _parse_folder_name(folder_name)
    def _fmt(stats, key, places):
        return f"{stats[key]:.{places}f}" if stats is not None else "N/A"
    entry = {
        "fps_average": _fmt(fps_stats, "average", 1),
        "fps_median": _fmt(fps_stats, "median", 1),
        "fps_diff": _fmt(fps_stats, "diff", 1),
        # App T (app GPU time) in microseconds, same metric the GPU automation test reports.
        "app_time_average": _fmt(app_time_stats, "average", 0),
        "app_time_median": _fmt(app_time_stats, "median", 0),
        "app_time_diff": _fmt(app_time_stats, "diff", 0),
        "test_name": test_name,
        "timestamp": timestamp,
        "has_thumbnail": has_thumbnail,
        "started_by": started_by,
        "isArchived": False,
    }
    if drive_link:
        entry["drive_link"] = drive_link
    if extra:
        entry.update(extra)
    # Derived, never set by hand: retention reads this to decide whether the run's
    # numbers are worth keeping once the artifacts are pruned. A run that produced no
    # metrics is not a real data point either, however green its checks were.
    checks = entry.get("checks") or []
    entry["test_successful"] = bool(checks) and \
        all(c.get("state") == "pass" for c in checks) and \
        entry.get("fps_average") != "N/A"
    return entry


def _save_local_metadata(test_dir, folder_name, fps_stats, app_time_stats, drive_link=None, has_thumbnail=False, started_by="unknown", extra=None):
    """Write metadata.json into the test folder on disk so it's readable offline."""
    entry = _build_metadata(folder_name, fps_stats, app_time_stats, drive_link, has_thumbnail, started_by, extra)
    local_path = os.path.join(test_dir, "metadata.json")
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2)
    print(f"  Saved local metadata: {local_path}")


def upload_to_github(test_dir, folderName, fps_stats, app_time_stats, drive_link=None, has_thumbnail=False, started_by="unknown", extra=None):
    """Publish one run to GitHub Pages as a single commit."""
    print(f"[GITHUB] Uploading run: {folderName}")
    prefix = f"AllTestRuns/{folderName}"
    files = {}

    # CSV and PNG only — mp4 is too large for git, it is served from Drive.
    for path in sorted(glob.glob(os.path.join(test_dir, "*.csv")) + glob.glob(os.path.join(test_dir, "*.png"))):
        name = os.path.basename(path)
        if SCREENSHOT_PNG.match(name):
            webp_name = f"{os.path.splitext(name)[0]}.webp"
            try:
                # Written to disk during the screenshot pass; re-encode only if that failed.
                webp_path = os.path.join(test_dir, webp_name)
                if os.path.isfile(webp_path):
                    with open(webp_path, "rb") as f:
                        files[f"{prefix}/{webp_name}"] = f.read()
                else:
                    files[f"{prefix}/{webp_name}"] = _webp_bytes(path)
                continue
            except Exception as e:
                print(f"  WARNING: WebP conversion failed for {name}, uploading the PNG: {e}")
        with open(path, "rb") as f:
            files[f"{prefix}/{name}"] = f.read()

    logs_dir = os.path.join(test_dir, "Report Logs")
    if os.path.isdir(logs_dir):
        files[f"{prefix}/Report Logs.zip"] = _zip_bytes(logs_dir)

    if not files:
        print("  ERROR: No files found to upload.")
        return False

    # Metadata always goes up, even when a metric is missing (e.g. record-metrics
    # wasn't enabled, so fps_stats is None) — whatever we do have puts the run on
    # the dashboard, and the missing metric stays at its default ("N/A").
    try:
        # Save locally first so the metadata lives alongside the run data on disk
        # even if the GitHub upload fails.
        _save_local_metadata(test_dir, folderName, fps_stats, app_time_stats, drive_link, has_thumbnail, started_by, extra)
    except Exception as e:
        print(f"  WARNING: Failed to save local metadata.json: {e}")

    entry = _build_metadata(folderName, fps_stats, app_time_stats, drive_link, has_thumbnail, started_by, extra)
    files[f"{prefix}/metadata.json"] = json.dumps(entry, indent=2).encode("utf-8")

    try:
        _github_commit(files, f"Add {folderName}")
    except Exception as e:
        print(f"[GITHUB] FAILED: {e}")
        return False

    fps_str = f"{fps_stats['average']:.1f}" if fps_stats is not None else "N/A"
    app_time_str = f"{app_time_stats['average']:.0f}" if app_time_stats is not None else "N/A"
    print(f"  FPS - worst 10%: {fps_str}, App T - worst 10%: {app_time_str} us")
    print(f"[GITHUB] Done! View at: https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Post-test graph & upload")
    parser.add_argument("test_dir", help="Path to the test output directory")
    parser.add_argument("folderName", help="Test folderName string")
    parser.add_argument("--graph-only", action="store_true", help="Only generate graph")
    parser.add_argument("--upload-only", action="store_true", help="Only upload")
    parser.add_argument("--started-by", default="unknown", help="GitHub username who started the test")
    parser.add_argument("--num-pc-bots", default="",
                        help="Number of PC bots requested (the XR bot is added on top for the 'requested' count)")
    parser.add_argument("--commit-sha", default="", help="Git commit SHA the build was made from")
    parser.add_argument("--commit-ref", default="", help="Git branch/ref the build was made from")
    parser.add_argument("--github-token", default="",
                        help="PAT for the GitHub Pages repo (passed in from the workflow secret)")
    args = parser.parse_args()

    # The PAT comes only from --github-token (the .bat forwards the workflow secret).
    global GITHUB_TOKEN, GITHUB_HEADERS
    GITHUB_TOKEN = args.github_token
    GITHUB_HEADERS = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    test_dir = args.test_dir
    folderName = args.folderName

    if not os.path.isdir(test_dir):
        print(f"ERROR: Directory not found: {test_dir}")
        sys.exit(1)

    do_graph = not args.upload_only
    do_upload = not args.graph_only

    fps_stats = None
    app_time_stats = None
    mp4_drive_link = None
    has_thumbnail = False

    if do_graph:
        print("[GRAPH] Generating App fps Time graph...")
        graph_path, fps_stats = generate_graph(test_dir, folderName)
        if graph_path:
            print("[GRAPH] Success.")
        else:
            print("[GRAPH] Failed.")

        print("[METRICS] Computing the App T average, median and difference...")
        app_time_stats = compute_app_time_stats(test_dir)
        _write_ci_outputs(fps_stats, app_time_stats)

        # Quest screencap captures both eyes side-by-side and each eye is
        # tilted, so crop to left eye, rotate to straighten, then trim
        # the black borders created by the rotation.
        from PIL import Image
        import math
        for sc_name in ["SCREENSHOT_1.png", "SCREENSHOT_2.png", "SCREENSHOT_3.png"]:
            sc_path = os.path.join(test_dir, sc_name)
            if not os.path.exists(sc_path) or os.path.getsize(sc_path) == 0:
                print(f"[SCREENSHOT] {sc_name} not found, skipping.")
                continue
            try:
                img = Image.open(sc_path)
                w, h = img.size
                cropped = img.crop((0, 0, w // 2, h))
                cw, ch = cropped.size

                angle = -20
                straightened = cropped.rotate(angle, expand=True, resample=Image.BICUBIC)

                # Calculate largest axis-aligned rect inside the rotated image
                angle_rad = math.radians(abs(angle))
                cos_a = abs(math.cos(angle_rad))
                sin_a = abs(math.sin(angle_rad))

                side_long = max(cw, ch)
                side_short = min(cw, ch)
                width_is_longer = cw >= ch

                if side_short <= 2.0 * sin_a * cos_a * side_long:
                    x = 0.5 * side_short
                    new_w = x / sin_a if width_is_longer else x / cos_a
                    new_h = x / cos_a if width_is_longer else x / sin_a
                else:
                    cos_2a = cos_a * cos_a - sin_a * sin_a
                    new_w = (cw * cos_a - ch * sin_a) / cos_2a
                    new_h = (ch * cos_a - cw * sin_a) / cos_2a

                sw, sh = straightened.size
                left = (sw - new_w) / 2
                top = (sh - new_h) / 2
                final = straightened.crop((int(left), int(top), int(left + new_w), int(top + new_h)))
                final.save(sc_path)
                fw, fh = final.size
                print(f"[SCREENSHOT] {sc_name}: cropped, rotated -20°, trimmed ({w}x{h} -> {fw}x{fh})")
                has_thumbnail = True
                _write_webp(sc_path)
            except Exception as e:
                print(f"[SCREENSHOT] WARNING: Could not process {sc_name}: {e}")
        if not has_thumbnail:
            print("[SCREENSHOT] No screenshots found (SCREENSHOT_1/2/3.png missing).")

    profiler_raw_path = None

    # Old unity profiler deprecated setup
    # import shutil
    # profiler_src_path = r"E:\Automation\Profiler_Test_Result\ProfilerRecording.raw"
    # if os.path.isfile(profiler_src_path):
    #     raw_size = os.path.getsize(profiler_src_path)
    #     if raw_size >= 1 * 1024 * 1024:
    #         profiler_dest_path = os.path.join(test_dir, "ProfilerRecording.raw")
    #         try:
    #             shutil.move(profiler_src_path, profiler_dest_path)
    #             profiler_raw_path = profiler_dest_path
    #             print(f"[PROFILER] Moved profiler recording into test folder: {profiler_dest_path} ({raw_size / (1024*1024):.1f} MB)")
    #         except Exception as e:
    #             print(f"[PROFILER] WARNING: Could not move profiler recording: {e}")
    #             profiler_raw_path = profiler_src_path
    #     else:
    #         print(f"[PROFILER] Profiler recording is too small ({raw_size / (1024*1024):.2f} MB) - recording likely failed.")
    #         print(f"  Hint: Check C:\\Automation\\UNDERDOGS Bots Automation\\Log Files\\unity_profiler.log for errors.")
    # else:
    #     print(f"[PROFILER] No profiler recording found at {profiler_src_path}, skipping.")

    # Parse the run's logs in-place so the resulting CSV ends up in the same folder
    # as the source artifacts (and gets uploaded with them).
    run_parsers(test_dir, profiler_raw_path)

    # The room should hold the PC bots plus the XR bot on the headset.
    try:
        num_pc_bots = int(args.num_pc_bots)
    except (ValueError, TypeError):
        num_pc_bots = None
    expected_players = num_pc_bots + 1 if num_pc_bots is not None and num_pc_bots >= 0 else None

    checks_result = run_functionality_checks(test_dir, expected_players)
    checks = (checks_result or {}).get("checks")
    _write_ci_checks(checks)

    # Extra dashboard metadata gathered from the run's logs.
    #  - checks:         the functionality verdicts, rendered as-is by the dashboard and
    #                    by the nightly Discord report.
    #  - bots_requested: PC bots requested + 1 for the XR/Quest bot.
    #  - bots_joined:    players that actually connected (XR + PC bots).
    #  - errors/exceptions: totals from the log parser's findings CSV.
    #  - github_run_id:  the ONLY link back to the workflow run that produced this
    #                    folder. 15OS's Builds console reads it to find a run's
    #                    profiler capture on Drive; nothing else ties the two together,
    #                    since the folder name carries just a test name and a runner-
    #                    local timestamp and GitHub exposes neither for a run.
    #                    Supplied by the Actions runner; absent when run by hand.
    extra = {}
    github_run_id = os.environ.get("GITHUB_RUN_ID")
    if github_run_id:
        extra["github_run_id"] = github_run_id
    if checks:
        extra["checks"] = checks

    # The checks already built the room roster, counting each player once however many
    # times they reconnected; the line count is the fallback for a run without them.
    quest_client = (checks_result or {}).get("clients", {}).get("quest")
    bots_joined = len(quest_client["players"]) if quest_client else _count_bots_joined(test_dir)
    if bots_joined is not None:
        extra["bots_joined"] = bots_joined
    if expected_players is not None:
        extra["bots_requested"] = expected_players
    errors, exceptions = _read_log_stats(test_dir)
    if errors is not None:
        extra["errors"] = errors
        extra["exceptions"] = exceptions
    if args.commit_sha:
        extra["commit_sha"] = args.commit_sha
    if args.commit_ref:
        extra["commit_ref"] = args.commit_ref

    if do_upload:
        _write_runner_logs_zip(test_dir)

        print("[UPLOAD] Uploading files to Google Drive...")
        try:
            mp4_drive_link = upload_to_drive(test_dir, folderName)
            if mp4_drive_link:
                print("[UPLOAD] Drive upload success.")
            else:
                print("[UPLOAD] Drive upload failed or no mp4 link.")
        except Exception as e:
            print(f"[UPLOAD] Drive upload crashed: {e}")
            mp4_drive_link = None

        print("[UPLOAD] Uploading files to GitHub Pages...")
        try:
            success = upload_to_github(test_dir, folderName, fps_stats, app_time_stats, mp4_drive_link, has_thumbnail, args.started_by, extra)
            if success:
                print("[UPLOAD] GitHub upload success.")
                # Old unity profiler deprecated setup
                # if profiler_raw_path and os.path.isfile(profiler_raw_path):
                #     print(f"[PROFILER] Kept local profiler recording in test folder: {profiler_raw_path}")
            else:
                print("[UPLOAD] GitHub upload failed.")
        except Exception as e:
            print(f"[UPLOAD] GitHub upload crashed: {e}")


if __name__ == "__main__":
    main()

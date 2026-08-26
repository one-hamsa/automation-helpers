r"""
Post-test processing for UNDERDOGS automation.
Generates an App GPU Time graph from the CSV report and uploads
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

# Repo-relative path to the log parser (shared with the Bots test).
# UploadFiles.py lives in <repo>/GPU Automation Test/, the parser in <repo>/Analysis/.
LOG_PARSER = Path(__file__).resolve().parent.parent / "Analysis" / "log_parser.py"

# Game-log folder the .bat pulls off the headset, once, after both app launches: it holds the
# metrics session as a .udlog (it reported before quitting, and the RenderDoc relaunch zipped
# it) plus the RenderDoc session's own log dir. Uploaded to Drive and GitHub Pages.
REPORT_LOGS_DIRS = ("Report Logs",)

# The runner's own logs for this level, zipped out of GPU_TEST_LOG_DIR. Drive only -
# logcat alone is several MB, which is permanent weight in the GitHub Pages repo.
RUNNER_LOGS_ZIP = "Runner Logs.zip"


def run_log_parser(test_dir):
    """Parse the game logs pulled into <test_dir>/Report Logs (writes <session>_log_findings.csv
    next to each Global.json.log). Best-effort — never fails the upload."""
    if not LOG_PARSER.is_file():
        print(f"[PARSE] log_parser.py not found at {LOG_PARSER}, skipping log parse.")
        return
    print(f"[PARSE] Running log_parser on {test_dir}")
    try:
        subprocess.run([sys.executable or "python", str(LOG_PARSER), test_dir], check=False)
    except Exception as e:
        print(f"[PARSE] log_parser crashed: {e}")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Google Drive shared folder ID.
DRIVE_PARENT_FOLDER_ID = "1Ckhix2o8tbz3VA6i25UQ1jf7JKx5bkQD"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

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
GITHUB_REPO_NAME = "Scene-Test-Automation"
GITHUB_BRANCH = "main"

GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/contents"
GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}


# ---------------------------------------------------------------------------
# Graph generation
# ---------------------------------------------------------------------------
def metric_stats(values, lower_is_better):
    """Average, median, and the difference between them - average minus median.
    A higher difference means more spikes."""
    average = sum(values) / len(values)
    median = statistics.median(values)
    return {
        "average": average,
        "median": median,
        "diff": (average - median) if lower_is_better else (median - average),
    }


def generate_graph(test_dir, folderName):
    """Read the CSV and produce an App GPU Time line chart.
    Returns (graph_path, gpu_stats) or (None, None) on failure.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    csv_pattern = os.path.join(test_dir, "CSV_REPORT*.csv")
    csv_files = glob.glob(csv_pattern)
    if not csv_files:
        print(f"  ERROR: No CSV file found matching {csv_pattern}")
        return None, None

    csv_path = csv_files[0]
    print(f"  Reading CSV: {csv_path}")

    times_sec = []
    gpu_times_ms = []

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                t = float(row["Time Stamp"]) / 1000.0
                gpu = float(row["app_gpu_time_microseconds"])
                times_sec.append(t)
                gpu_times_ms.append(gpu)
            except (ValueError, KeyError):
                continue

    if not times_sec:
        print("  ERROR: No valid data rows found in CSV.")
        return None, None

    print(f"  CSV has {len(times_sec)} rows, time range: {times_sec[0]:.1f}s - {times_sec[-1]:.1f}s")

    filtered = [(t, g) for t, g in zip(times_sec, gpu_times_ms) if t >= 60.0]
    if not filtered:
        print(f"  ERROR: No data points after 60 seconds (max timestamp: {times_sec[-1]:.1f}s).")
        print("  Hint: OVR metrics capture was too short — check device connection and game load time.")
        return None, None
    times_sec, gpu_times_ms = zip(*filtered)
    times_sec = list(times_sec)
    gpu_times_ms = list(gpu_times_ms)

    print(f"  Plotting {len(times_sec)} data points (after 60s)...")

    stats = metric_stats(gpu_times_ms, lower_is_better=True)
    min_val = min(gpu_times_ms)
    max_val = max(gpu_times_ms)
    print(f"  App T - average: {stats['average']:.0f} us, median: {stats['median']:.0f} us, "
          f"difference: {stats['diff']:.0f} us (over {len(gpu_times_ms)} samples after 60s)")

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(times_sec, gpu_times_ms, linewidth=1.2, color="#2563eb", label="App GPU Time")
    # The headline number gets the emphatic line - it is what the run is judged on.
    ax.axhline(y=stats["average"], color="#16a34a", linestyle="--", linewidth=1.2, label=f"Average: {stats['average']:.0f}")
    ax.axhline(y=stats["median"], color="#7c3aed", linestyle="--", linewidth=1.2, label=f"Median: {stats['median']:.0f}")
    ax.axhline(y=min_val, color="#0891b2", linestyle=":",  linewidth=1.2, label=f"Min: {min_val:.0f}")
    ax.axhline(y=max_val, color="#dc2626", linestyle=":",  linewidth=1.2, label=f"Max: {max_val:.0f}")

    ax.set_xlabel("Time (seconds)")
    ax.set_ylabel("App GPU Time")
    ax.set_title(f"{folderName}")
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(test_dir, "APP_TIME_GRAPH.png")
    fig.savefig(out_path, dpi=150)
    plt.close(fig)

    print(f"  Graph saved: {out_path}")
    return out_path, stats


# ---------------------------------------------------------------------------
# Google Drive upload (OAuth2 refresh token from the environment)
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


def upload_file_drive(service, file_path, folder_id, max_retries=3):
    import time
    from googleapiclient.http import MediaFileUpload

    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    mime_map = {".csv": "text/csv", ".png": "image/png"}
    mime_type = mime_map.get(os.path.splitext(file_name)[1].lower(), "application/octet-stream")

    metadata = {"name": file_name, "parents": [folder_id]}

    # Resumable for anything over 5MB. Large files (e.g. the RenderDoc .rdc capture) are streamed in
    # 50MB chunks so a single huge request can't time out, and we report progress as it uploads.
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

    # .rdc is the RenderDoc capture — large, Drive-only (never goes to GitHub, like the profiler .raw).
    extensions = ("*.csv", "*.png", "*.rdc")
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

    # Upload the pulled game logs as subfolders on Drive, walking subdirectories
    # recursively so nested log files are included. Two folders because the run has
    # two launches: the metrics phase and the RenderDoc phase (see the .bat).
    for logs_dir_name in REPORT_LOGS_DIRS:
        report_logs_dir = os.path.join(test_dir, logs_dir_name)
        if not os.path.isdir(report_logs_dir):
            continue
        print(f"  Uploading '{logs_dir_name}' folder to Drive...")
        logs_folder_id = create_drive_folder(service, logs_dir_name, folder_id)
        # adb pull creates a timestamped subfolder inside the logs folder,
        # so collect all files recursively and upload them flat into
        # the Drive folder.
        log_file_count = 0
        for dirpath, _, filenames in os.walk(report_logs_dir):
            for fname in filenames:
                file_path = os.path.join(dirpath, fname)
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                print(f"    Uploading log: {fname} ({file_size_mb:.1f} MB)...")
                upload_file_drive(service, file_path, logs_folder_id)
                log_file_count += 1
        if log_file_count == 0:
            print(f"  WARNING: '{logs_dir_name}' folder exists but no files found inside!")
        else:
            print(f"  Uploaded {log_file_count} log file(s) from '{logs_dir_name}' to Drive.")

    print(f"  All files uploaded to Drive folder: {folderName}")
    return drive_folder_link


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
    """Zip the runner's own logs for this level - the .bat console and the device logcat -
    into the test folder, so they leave the rig with the results instead of being
    overwritten by the next run of the same level. Returns the zip path, or None.

    GPU_TEST_LOG_DIR is set by GPU_Performance_Test.yaml, which points it at that level's
    log folder. Both files are still being written at this point (this script runs inside
    the .bat, and logcat streams until the .bat exits), so each ends a little short of the
    full run.
    """
    logs_dir = os.environ.get("GPU_TEST_LOG_DIR")
    if not logs_dir or not os.path.isdir(logs_dir):
        print(f"[RUNNER LOGS] Nothing to zip ({logs_dir or 'GPU_TEST_LOG_DIR unset'}), skipping.")
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


def rename_renderdoc_capture(test_dir, folder_name):
    """Rename the run's RenderDoc capture to RenderDoc_<scene>.rdc.

    RenderDoc names a capture after the app, the wall clock and the frame it caught
    (com.onehamsa.underdogs_2026.08.22_05.29_frame1381.rdc), so nothing downstream can
    predict it. A fixed name is what lets 15OS's RenderDoc button hand you a file whose
    name says which scene it came from, and it reads the same way in Drive.

    Best-effort: a run with no capture (the RenderDoc phase never got that far) just
    logs and carries on — the rest of the upload is still worth doing.
    """
    captures = sorted(glob.glob(os.path.join(test_dir, "*.rdc")))
    if not captures:
        print("[RENDERDOC] No .rdc capture in the test folder - nothing to rename.")
        return

    _, scene_name, _ = _parse_folder_name(folder_name)
    scene = (scene_name or "").strip()
    # Strip only what a filename cannot hold; spaces stay, so "Cage Twins" reads as it does
    # everywhere else in this system.
    safe = re.sub(r'[<>:"/\\|?*]', "", scene).strip()
    if not safe or safe == "-":
        safe = "Unknown"

    src = captures[0]
    if len(captures) > 1:
        print(f"[RENDERDOC] WARNING: {len(captures)} .rdc files present, renaming the first: {os.path.basename(src)}")
    target = os.path.join(test_dir, f"RenderDoc_{safe}.rdc")
    if os.path.abspath(src) == os.path.abspath(target):
        print(f"[RENDERDOC] Capture already named {os.path.basename(target)}.")
        return

    try:
        # A re-run of the same scene into the same folder would otherwise fail the rename.
        if os.path.exists(target):
            os.remove(target)
        os.rename(src, target)
        print(f"[RENDERDOC] Renamed {os.path.basename(src)} -> {os.path.basename(target)}")
    except OSError as e:
        print(f"[RENDERDOC] WARNING: Could not rename {os.path.basename(src)}: {e}")


def _parse_folder_name(folder_name):
    """Parse test name, scene, and timestamp from the folder name.
    Expected format: GPU TEST - Name(TestName) - On Scene(SceneName) - Started at( timestamp )
    """
    test_name = "-"
    scene_name = "-"
    timestamp = "-"

    m = re.search(r'Name\(([^)]*)\)', folder_name)
    if m:
        test_name = m.group(1).strip()

    m = re.search(r'On [Ss]cene\(([^)]*)\)', folder_name)
    if m:
        scene_name = m.group(1).strip()

    m = re.search(r'Started at\(\s*([^)]*)\s*\)', folder_name)
    if m:
        timestamp = m.group(1).strip()

    return test_name, scene_name, timestamp


def _write_ci_outputs(gpu_stats, folder_name):
    """Append one tab-separated row per level - label, average, median, difference - to
    GPU_METRICS_FILE. A file rather than a step output because a sweep runs this script
    once per level, and a step output would keep only the last level's number. Empty
    values = no metric produced. No-op outside CI, and never fails the run.
    """
    if gpu_stats is None:
        values = ["", "", ""]
    else:
        values = [f"{gpu_stats[k]:.0f}" for k in ("average", "median", "diff")]

    metrics_path = os.environ.get("GPU_METRICS_FILE")
    if metrics_path:
        # Label the row by scene, which is the level name on a sweep - the folder name
        # itself carries a runner-local timestamp and reads badly in a notification.
        test_name, scene_name, _ = _parse_folder_name(folder_name)
        label = scene_name or test_name or "-"
        try:
            with open(metrics_path, "a", encoding="utf-8") as f:
                f.write(label + "\t" + "\t".join(values) + "\n")
            print(f"[CI] Appended '{label}' metrics to {metrics_path}")
        except Exception as e:
            print(f"[CI] WARNING: Could not append run metrics: {e}")


def _build_metadata(folder_name, gpu_stats, drive_link=None, has_thumbnail=False, started_by="unknown", extra=None):
    test_name, scene_name, timestamp = _parse_folder_name(folder_name)

    def _fmt(key):
        return f"{gpu_stats[key]:.0f}" if gpu_stats is not None else "N/A"

    entry = {
        "gpu_average": _fmt("average"),
        "gpu_median": _fmt("median"),
        "gpu_diff": _fmt("diff"),
        "test_name": test_name,
        "scene_name": scene_name,
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
    # numbers are worth keeping once the artifacts are pruned. The GPU sweep has no
    # functionality checks, so producing a metric at all is the bar - a level that
    # never rendered records nothing.
    entry["test_successful"] = entry.get("gpu_average") not in (None, "N/A")
    return entry


def _save_local_metadata(test_dir, folder_name, gpu_stats, drive_link=None, has_thumbnail=False, started_by="unknown", extra=None):
    """Write metadata.json into the test folder on disk so it's readable offline."""
    entry = _build_metadata(folder_name, gpu_stats, drive_link, has_thumbnail, started_by, extra)
    local_path = os.path.join(test_dir, "metadata.json")
    with open(local_path, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2)
    print(f"  Saved local metadata: {local_path}")


def upload_to_github(test_dir, folderName, gpu_stats, drive_link=None, has_thumbnail=False, started_by="unknown", extra=None):
    """Publish one run to GitHub Pages as a single commit."""
    print(f"[GITHUB] Uploading run: {folderName}")
    prefix = f"AllTestRuns/{folderName}"
    files = {}

    # CSV and PNG only — mp4 and .rdc are too large for git, they are served from Drive.
    for path in sorted(glob.glob(os.path.join(test_dir, "*.csv")) + glob.glob(os.path.join(test_dir, "*.png"))):
        name = os.path.basename(path)
        if SCREENSHOT_PNG.match(name):
            try:
                files[f"{prefix}/{os.path.splitext(name)[0]}.webp"] = _webp_bytes(path)
                continue
            except Exception as e:
                print(f"  WARNING: WebP conversion failed for {name}, uploading the PNG: {e}")
        with open(path, "rb") as f:
            files[f"{prefix}/{name}"] = f.read()

    # One zip per log folder, so the dashboard offers a single download per phase.
    for logs_dir_name in REPORT_LOGS_DIRS:
        logs_dir = os.path.join(test_dir, logs_dir_name)
        if os.path.isdir(logs_dir):
            files[f"{prefix}/{logs_dir_name}.zip"] = _zip_bytes(logs_dir)

    if not files:
        print("  ERROR: No files found to upload.")
        return False

    # Metadata always goes up, even when a metric is missing (e.g. record-metrics
    # wasn't enabled, so gpu_stats is None) — whatever we do have puts the run on
    # the dashboard, and the missing metric stays at its default ("N/A").
    try:
        # Save locally first so the metadata lives alongside the run data on disk
        # even if the GitHub upload fails.
        _save_local_metadata(test_dir, folderName, gpu_stats, drive_link, has_thumbnail, started_by, extra)
    except Exception as e:
        print(f"  WARNING: Failed to save local metadata.json: {e}")

    entry = _build_metadata(folderName, gpu_stats, drive_link, has_thumbnail, started_by, extra)
    files[f"{prefix}/metadata.json"] = json.dumps(entry, indent=2).encode("utf-8")

    try:
        _github_commit(files, f"Add {folderName}")
    except Exception as e:
        print(f"[GITHUB] FAILED: {e}")
        return False

    gpu_str = f"{gpu_stats['average']:.0f}" if gpu_stats is not None else "N/A"
    print(f"  avg GPU: {gpu_str} us")
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

    # Parse the pulled game logs first so the findings CSV is present for the upload step.
    run_log_parser(test_dir)

    do_graph = not args.upload_only
    do_upload = not args.graph_only

    gpu_stats = None
    mp4_drive_link = None
    has_thumbnail = False

    if do_graph:
        print("[GRAPH] Generating App GPU Time graph...")
        graph_path, gpu_stats = generate_graph(test_dir, folderName)
        if graph_path:
            print("[GRAPH] Success.")
        else:
            print("[GRAPH] Failed.")
        _write_ci_outputs(gpu_stats, folderName)

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
            except Exception as e:
                print(f"[SCREENSHOT] WARNING: Could not process {sc_name}: {e}")
        if not has_thumbnail:
            print("[SCREENSHOT] No screenshots found (SCREENSHOT_1/2/3.png missing).")

    # Give the capture a predictable name before anything uploads it, so both Drive
    # and this folder on disk carry the same one.
    rename_renderdoc_capture(test_dir, folderName)

    # Extra dashboard metadata:
    #  - commit / branch the build under test was made from
    #  - github_run_id: the ONLY link back to the workflow run that produced this
    #    folder. 15OS's Builds console resolves a run's RenderDoc capture through it
    #    (same as the bots test's profiler db), and a sweep's folders all share it.
    # Empty when the test was run by hand rather than from the workflow.
    extra = {}
    github_run_id = os.environ.get("GITHUB_RUN_ID")
    if github_run_id:
        extra["github_run_id"] = github_run_id
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
            success = upload_to_github(test_dir, folderName, gpu_stats, mp4_drive_link, has_thumbnail, args.started_by, extra)
            if success:
                print("[UPLOAD] GitHub upload success.")
            else:
                print("[UPLOAD] GitHub upload failed.")
        except Exception as e:
            print(f"[UPLOAD] GitHub upload crashed: {e}")


if __name__ == "__main__":
    main()
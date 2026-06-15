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
import re
import argparse
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Google Drive shared folder ID.
DRIVE_PARENT_FOLDER_ID = "1Ckhix2o8tbz3VA6i25UQ1jf7JKx5bkQD"

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# OAuth credentials — stored on the runner machine, NOT in the repo.
RUNNER_AUTH_DIR = r"C:\Automation\UNDERDOGS Scene Test Automation\Runner"
CREDENTIALS_FILE = os.path.join(RUNNER_AUTH_DIR, "credentials.json")
TOKEN_FILE = os.path.join(RUNNER_AUTH_DIR, "token.json")

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
def generate_graph(test_dir, folderName):
    """Read the CSV and produce an App GPU Time line chart.
    Returns (graph_path, avg_gpu) or (None, None) on failure.
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

    avg_val = sum(gpu_times_ms) / len(gpu_times_ms)
    min_val = min(gpu_times_ms)
    max_val = max(gpu_times_ms)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(times_sec, gpu_times_ms, linewidth=1.2, color="#2563eb", label="App GPU Time")
    ax.axhline(y=avg_val, color="#16a34a", linestyle="--", linewidth=1.2, label=f"Avg: {avg_val:.0f}")
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
    return out_path, avg_val


# ---------------------------------------------------------------------------
# Google Drive upload (OAuth2 with saved token)
# ---------------------------------------------------------------------------
def get_drive_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                print(f"  ERROR: {CREDENTIALS_FILE} not found.")
                return None
            print("  No token found — opening browser for Google login...")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        print(f"  Token saved to: {TOKEN_FILE}")

    return build("drive", "v3", credentials=creds)


def create_drive_folder(service, name, parent_id):
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder.get("id")


def upload_file_drive(service, file_path, folder_id):
    from googleapiclient.http import MediaFileUpload

    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)

    mime_map = {".csv": "text/csv", ".png": "image/png"}
    mime_type = mime_map.get(os.path.splitext(file_name)[1].lower(), "application/octet-stream")

    metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=(file_size > 5 * 1024 * 1024))

    uploaded = service.files().create(body=metadata, media_body=media, fields="id,webViewLink").execute()
    return uploaded


def upload_to_drive(test_dir, folderName):
    """Upload all test files to Google Drive. Returns the Drive folder link or None."""
    service = get_drive_service()
    if not service:
        return None

    print(f"  Creating Drive folder: {folderName}")
    folder_id = create_drive_folder(service, folderName, DRIVE_PARENT_FOLDER_ID)
    drive_folder_link = f"https://drive.google.com/drive/folders/{folder_id}"

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

    print(f"  All files uploaded to Drive folder: {folderName}")
    return drive_folder_link


# ---------------------------------------------------------------------------
# GitHub Pages upload
# ---------------------------------------------------------------------------
def _github_get_file_sha(repo_path):
    """Get the SHA of an existing file (needed for updates)."""
    r = requests.get(f"{GITHUB_API_BASE}/{repo_path}", headers=GITHUB_HEADERS)
    if r.status_code == 200:
        return r.json()["sha"]
    return None


def _github_upload_file(local_path, repo_path):
    """Upload or update a single file in the GitHub repo.
    Uses the Git Blobs API for files over 25MB (Contents API limit).
    """
    file_size = os.path.getsize(local_path)

    # For files under 25MB, use the simple Contents API
    if file_size < 25 * 1024 * 1024:
        with open(local_path, "rb") as f:
            content = base64.b64encode(f.read()).decode()

        sha = _github_get_file_sha(repo_path)
        payload = {
            "message": f"Add {repo_path}",
            "content": content,
            "branch": GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(f"{GITHUB_API_BASE}/{repo_path}", headers=GITHUB_HEADERS, json=payload)
        r.raise_for_status()
        print(f"  GitHub: Uploaded {repo_path}")
        return

    # For large files, use the Git Data API (blobs + trees + commits)
    repo_url = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"

    # Step 1: Create a blob with the file content
    print(f"  GitHub: Large file ({file_size / (1024*1024):.1f} MB), using Git Blobs API...")
    with open(local_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    r = requests.post(f"{repo_url}/git/blobs", headers=GITHUB_HEADERS, json={
        "content": content,
        "encoding": "base64"
    })
    r.raise_for_status()
    blob_sha = r.json()["sha"]

    # Step 2: Get the current commit SHA and tree SHA for the branch
    r = requests.get(f"{repo_url}/git/ref/heads/{GITHUB_BRANCH}", headers=GITHUB_HEADERS)
    r.raise_for_status()
    current_commit_sha = r.json()["object"]["sha"]

    r = requests.get(f"{repo_url}/git/commits/{current_commit_sha}", headers=GITHUB_HEADERS)
    r.raise_for_status()
    base_tree_sha = r.json()["tree"]["sha"]

    # Step 3: Create a new tree with the file added
    r = requests.post(f"{repo_url}/git/trees", headers=GITHUB_HEADERS, json={
        "base_tree": base_tree_sha,
        "tree": [{
            "path": repo_path,
            "mode": "100644",
            "type": "blob",
            "sha": blob_sha
        }]
    })
    r.raise_for_status()
    new_tree_sha = r.json()["sha"]

    # Step 4: Create a new commit
    r = requests.post(f"{repo_url}/git/commits", headers=GITHUB_HEADERS, json={
        "message": f"Add {repo_path}",
        "tree": new_tree_sha,
        "parents": [current_commit_sha]
    })
    r.raise_for_status()
    new_commit_sha = r.json()["sha"]

    # Step 5: Update the branch reference
    r = requests.patch(f"{repo_url}/git/refs/heads/{GITHUB_BRANCH}", headers=GITHUB_HEADERS, json={
        "sha": new_commit_sha
    })
    r.raise_for_status()
    print(f"  GitHub: Uploaded {repo_path}")


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


def _github_update_summary(folder_name, avg_gpu, drive_link=None, has_thumbnail=False, started_by="unknown"):
    """Upload a metadata.json file into the test's folder."""
    test_name, scene_name, timestamp = _parse_folder_name(folder_name)

    entry = {
        "avg_gpu": f"{avg_gpu:.0f}",
        "test_name": test_name,
        "scene_name": scene_name,
        "timestamp": timestamp,
        "has_thumbnail": has_thumbnail,
        "started_by": started_by,
        "isArchived": False,
    }
    if drive_link:
        entry["drive_link"] = drive_link

    content = json.dumps(entry, indent=2)
    repo_path = f"AllTestRuns/{folder_name}/metadata.json"

    sha = _github_get_file_sha(repo_path)
    payload = {
        "message": f"Add metadata for {folder_name}",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(f"{GITHUB_API_BASE}/{repo_path}", headers=GITHUB_HEADERS, json=payload)
    r.raise_for_status()
    print(f"  GitHub: Metadata uploaded — {folder_name} avg GPU: {avg_gpu:.0f}")


def upload_to_github(test_dir, folderName, avg_gpu, drive_link=None, has_thumbnail=False, started_by="unknown"):
    """Upload CSV and PNG to GitHub Pages and update the GPU summary."""
    print(f"[GITHUB] Uploading run: {folderName}")

    # Only upload CSV and PNG — mp4 is too large, served from Google Drive
    extensions = ("*.csv", "*.png")
    files_to_upload = []
    for ext in extensions:
        files_to_upload.extend(glob.glob(os.path.join(test_dir, ext)))

    if not files_to_upload:
        print("  ERROR: No files found to upload.")
        return False

    failed = []
    for file_path in files_to_upload:
        filename = os.path.basename(file_path)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        print(f"  Uploading: {filename} ({file_size_mb:.1f} MB)...")
        try:
            _github_upload_file(file_path, f"AllTestRuns/{folderName}/{filename}")
        except Exception as e:
            print(f"  WARNING: Failed to upload {filename}: {e}")
            failed.append(filename)

    try:
        if avg_gpu is not None:
            _github_update_summary(folderName, avg_gpu, drive_link, has_thumbnail, started_by)
    except Exception as e:
        print(f"  WARNING: Failed to update summary: {e}")
        failed.append("summary.json")

    if failed:
        print(f"[GITHUB] Completed with errors. Failed: {', '.join(failed)}")
    else:
        print(f"[GITHUB] Done! View at: https://{GITHUB_REPO_OWNER}.github.io/{GITHUB_REPO_NAME}/")
    return len(failed) == 0


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

    avg_gpu = None
    mp4_drive_link = None
    has_thumbnail = False

    if do_graph:
        print("[GRAPH] Generating App GPU Time graph...")
        graph_path, avg_gpu = generate_graph(test_dir, folderName)
        if graph_path:
            print("[GRAPH] Success.")
        else:
            print("[GRAPH] Failed.")

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

    if do_upload:
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
            success = upload_to_github(test_dir, folderName, avg_gpu, mp4_drive_link, has_thumbnail, args.started_by)
            if success:
                print("[UPLOAD] GitHub upload success.")
            else:
                print("[UPLOAD] GitHub upload failed.")
        except Exception as e:
            print(f"[UPLOAD] GitHub upload crashed: {e}")


if __name__ == "__main__":
    main()
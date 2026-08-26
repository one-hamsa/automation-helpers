r"""
Functionality checks for an UNDERDOGS bots test run.

The performance numbers say how fast the run was, not whether it was a real game.
This reads the Quest's session log under "Report Logs" and answers three things: did
the headset reach a room, did the room fill up, and did players die. Nothing else is
needed - a full room proves the PC bots launched and connected, and kills prove they
played, so the PC side is not read at all.

Writes functionality.json into the test folder. Its "checks" list is render-ready
(id / label / state / detail), so the Discord report and the dashboard show the same
verdicts without either of them re-deriving anything.

Usage:
    py -3 functionality_checks.py <test-folder>
    py -3 functionality_checks.py <test-folder> --expected-players 6
    py -3 functionality_checks.py <test-folder> --out <path>
"""

import argparse
import glob
import json
import os
import sys
import re
import zipfile

sys.stdout.reconfigure(encoding='utf-8')


# Where the Quest's session logs land in the test folder, pulled loose off the headset.
QUEST_LOGS_DIR = "Report Logs"

# The one log carrying the whole session.
SESSION_LOG = "Global.log"

OUTPUT_NAME = "functionality.json"

SESSION_RE = re.compile(r'\[ReportUtility\] Session \[([^,]+), device: ([^,]+), isEditor: (\w+)\]')
JOINED_ROOM_RE = re.compile(r'Joined room \(ID:([^)]+)\) with level (.+?) using groupToken')
FOUND_MATCH_RE = re.compile(r'Found match: roomID=(\S+) roomLevel=(.+?) sessionID=(\d+)')
PLAYER_CONNECTED_RE = re.compile(r'\[MultiplayerScene\] Player connected: (\d+) - (.+?)\s*$')
KILL_RE = re.compile(r'\[KillAttribution\] (.+?) \(ID:(\d+)\) killed (.+?) \(ID:(\d+)\)')


def read_session_log(logs_root, filename=SESSION_LOG):
    """Read the session log out of the logs folder, whether it was pulled loose or zipped
    into a .udlog. Returns (label, text), or (None, None) when neither holds it."""
    if not os.path.isdir(logs_root):
        return None, None

    loose = glob.glob(os.path.join(logs_root, "**", filename), recursive=True)
    if loose:
        path = max(loose, key=os.path.getmtime)
        label = os.path.basename(os.path.dirname(path)) or os.path.basename(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return label, f.read()

    # Newest first, but only an archive that actually holds the log: a force-killed
    # session leaves an empty .udlog behind, and one of those can be the newest file.
    for udlog in sorted(glob.glob(os.path.join(logs_root, "**", "*.udlog"), recursive=True),
                        key=os.path.getmtime, reverse=True):
        try:
            with zipfile.ZipFile(udlog) as z:
                if filename not in z.namelist():
                    continue
                return os.path.basename(udlog), z.read(filename).decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError) as e:
            print(f"  WARNING: could not read {filename} from {os.path.basename(udlog)}: {e}")

    return None, None


def parse_log(text):
    """The three facts the checks are built from, pulled out of the Quest's Global.log."""
    facts = {"version": None, "device": None, "room_id": None, "room_level": None,
             "players": [], "kills": 0, "killers": set(), "victims": set()}
    seen = set()

    for line in text.splitlines():
        m = SESSION_RE.search(line)
        if m:
            facts["version"], facts["device"] = m.group(1).strip(), m.group(2).strip()
            continue

        m = JOINED_ROOM_RE.search(line) or FOUND_MATCH_RE.search(line)
        if m:
            facts["room_id"] = facts["room_id"] or m.group(1)
            facts["room_level"] = facts["room_level"] or m.group(2).strip()
            continue

        m = PLAYER_CONNECTED_RE.search(line)
        if m:
            # Once per player: a reconnect logs the line again, and the roster is who was
            # in the room, not how many connect events there were.
            player_id = m.group(1)
            if player_id not in seen:
                seen.add(player_id)
                facts["players"].append({"id": player_id, "name": m.group(2).strip()})
            continue

        m = KILL_RE.search(line)
        if m:
            facts["kills"] += 1
            facts["killers"].add(m.group(2))
            facts["victims"].add(m.group(4))

    facts["killers"] = len(facts["killers"])
    facts["victims"] = len(facts["victims"])
    return facts


def build_checks(facts, expected_players):
    if facts is None:
        missing = f"no {SESSION_LOG} found"
        return [{"id": i, "label": l, "state": "unknown", "detail": missing} for i, l in (
            ("quest_room", "Quest in room"),
            ("players", "Players in room"),
            ("kills", "Players can be killed"))]

    in_room = bool(facts["room_id"])
    room_detail = (f"{facts['room_level']} - room {facts['room_id'][:8]}" if in_room
                   else f"{facts['version'] or 'unknown build'} launched, never reached a room")

    joined = len(facts["players"])
    if expected_players is None:
        players = ("unknown", f"{joined} joined, expected count not provided")
    else:
        players = ("pass" if joined == expected_players else "fail",
                   f"{joined}/{expected_players} joined")

    kills = facts["kills"]
    kills_detail = (f"{kills} kills - {facts['killers']} killers / {facts['victims']} victims"
                    if kills else "no kills logged for the whole run")

    return [
        {"id": "quest_room", "label": "Quest in room",
         "state": "pass" if in_room else "fail", "detail": room_detail},
        {"id": "players", "label": "Players in room",
         "state": players[0], "detail": players[1]},
        {"id": "kills", "label": "Players can be killed",
         "state": "pass" if kills else "fail", "detail": kills_detail},
    ]


def analyze(test_dir, expected_players=None):
    label, text = read_session_log(os.path.join(test_dir, QUEST_LOGS_DIR))
    if text is None:
        print(f"[CHECKS] No {SESSION_LOG} under {QUEST_LOGS_DIR}/ - the checks report unknown.")
        facts = None
    else:
        print(f"[CHECKS] Reading quest session log: {label}")
        facts = parse_log(text)

    checks = build_checks(facts, expected_players)
    return {
        "quest": facts,
        "expected_players": expected_players,
        "checks": checks,
        "passed": sum(1 for c in checks if c["state"] == "pass"),
        "failed": sum(1 for c in checks if c["state"] == "fail"),
        "unknown": sum(1 for c in checks if c["state"] == "unknown"),
    }


def main():
    parser = argparse.ArgumentParser(description="Functionality checks for a bots test run")
    parser.add_argument("test_dir", help="Path to the test output directory")
    parser.add_argument("--expected-players", type=int, default=None,
                        help="Players the room should hold (PC bots + the XR bot)")
    parser.add_argument("--out", default=None, help=f"Where to write (default: <test-dir>/{OUTPUT_NAME})")
    args = parser.parse_args()

    if not os.path.isdir(args.test_dir):
        print(f"ERROR: {args.test_dir} is not a directory")
        return 1

    result = analyze(args.test_dir, args.expected_players)

    icon = {"pass": "PASS", "fail": "FAIL"}
    print()
    for c in result["checks"]:
        print(f"  [{icon.get(c['state'], '????')}] {c['label']}: {c['detail']}")
    print(f"\n  {result['passed']} passed, {result['failed']} failed, {result['unknown']} unknown")

    out_path = args.out or os.path.join(args.test_dir, OUTPUT_NAME)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"[CHECKS] Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

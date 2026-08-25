r"""
Functionality checks for an UNDERDOGS bots test run.

The performance numbers say how fast the run was, not whether it was a real game.
This reads the session logs both clients wrote - the Quest's under "Report Logs" and the
Steam build's .udlog under "PC Logs" - and answers what a green run still leaves open:
did each client launch and reach the room, was the room the size it should have been,
and did players die.

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
import re
import sys
import zipfile
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')


# Where each client's session logs land in the test folder. The Quest's are pulled loose
# off the headset; the Steam build's arrive as the .udlog it zips on a graceful quit.
QUEST_LOGS_DIR = "Report Logs"
STEAM_LOGS_DIR = "PC Logs"

# The one log carrying the whole session. Both clients write the same file set, so the
# same reader and the same patterns serve either side.
SESSION_LOG = "Global.log"

OUTPUT_NAME = "functionality.json"

LOG_TIME_FORMAT = "%m/%d/%Y %H:%M:%S"

TIMESTAMP_RE = re.compile(r'^\[(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})\]')
SESSION_RE = re.compile(r'\[ReportUtility\] Session \[([^,]+), device: ([^,]+), isEditor: (\w+)\]')
SIGNED_IN_RE = re.compile(r'\[UGSUD\] Player signed in\. id: "([^"]*)"')
FOUND_MATCH_RE = re.compile(r'Found match: roomID=(\S+) roomLevel=(.+?) sessionID=(\d+)')
JOINED_ROOM_RE = re.compile(r'Joined room \(ID:([^)]+)\) with level (.+?) using groupToken')
REALTIME_RE = re.compile(r'Realtime: Connected to Room "([^"]*)"')
MECH_SPAWN_RE = re.compile(r'Connected to room, spawning mech')
PLAYER_CONNECTED_RE = re.compile(r'\[MultiplayerScene\] Player connected: (\d+) - (.+?)\s*$')
KILL_RE = re.compile(r'\[KillAttribution\] (.+?) \(ID:(\d+)\) killed (.+?) \(ID:(\d+)\)')


def _read_session_log(logs_root, filename=SESSION_LOG):
    """Read one session log out of a client's logs folder, whether it was pulled loose or
    zipped into a .udlog. Returns (label, text), or (None, None) when neither holds it."""
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


def _line_time(line):
    m = TIMESTAMP_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), LOG_TIME_FORMAT)
    except ValueError:
        return None


def parse_client(label, text):
    """Pull the run's facts out of one client's Global.log."""
    facts = {
        "log": label,
        "launched": False, "version": None, "device": None,
        "signed_in": False, "player_id": None,
        "found_match": False, "room_id": None, "room_level": None, "session_id": None,
        "joined_room": False,
        "realtime_connected": False, "room_name": None,
        "mech_spawned": False,
        "players": [],
        "kills": [],
        "first_time": None, "last_time": None,
    }

    seen_players = set()
    last_time = None

    for line in text.splitlines():
        stamp = _line_time(line)
        if stamp is not None:
            last_time = stamp
            if facts["first_time"] is None:
                facts["first_time"] = stamp

        m = SESSION_RE.search(line)
        if m:
            facts["launched"] = True
            facts["version"], facts["device"] = m.group(1).strip(), m.group(2).strip()
            continue

        m = SIGNED_IN_RE.search(line)
        if m:
            facts["signed_in"] = True
            facts["player_id"] = facts["player_id"] or m.group(1)
            continue

        m = FOUND_MATCH_RE.search(line)
        if m:
            facts["found_match"] = True
            facts["room_id"] = m.group(1)
            facts["room_level"] = m.group(2).strip()
            facts["session_id"] = m.group(3)
            continue

        m = JOINED_ROOM_RE.search(line)
        if m:
            facts["joined_room"] = True
            facts["room_id"] = facts["room_id"] or m.group(1)
            facts["room_level"] = facts["room_level"] or m.group(2).strip()
            continue

        m = REALTIME_RE.search(line)
        if m:
            facts["realtime_connected"] = True
            facts["room_name"] = m.group(1)
            continue

        if MECH_SPAWN_RE.search(line):
            facts["mech_spawned"] = True
            continue

        m = PLAYER_CONNECTED_RE.search(line)
        if m:
            # Once per player: a reconnect logs the line again, and the roster is who was
            # in the room, not how many connect events there were.
            player_id, name = m.group(1), m.group(2).strip()
            if player_id not in seen_players:
                seen_players.add(player_id)
                facts["players"].append({"id": player_id, "name": name})
            continue

        m = KILL_RE.search(line)
        if m:
            when = stamp or last_time
            facts["kills"].append({
                "time": when.strftime(LOG_TIME_FORMAT) if when else None,
                "killer": m.group(1).strip(), "killer_id": m.group(2),
                "victim": m.group(3).strip(), "victim_id": m.group(4),
            })

    facts["last_time"] = last_time
    for key in ("first_time", "last_time"):
        if facts[key] is not None:
            facts[key] = facts[key].strftime(LOG_TIME_FORMAT)
    return facts


def _client_checks(tag, title, facts):
    """The two verdicts per client: it started and signed in, and it reached the room."""
    if facts is None:
        missing = f"no {SESSION_LOG} found"
        return [
            {"id": f"{tag}_launched", "label": f"{title} launched", "state": "unknown", "detail": missing},
            {"id": f"{tag}_room", "label": f"{title} in room", "state": "unknown", "detail": missing},
        ]

    launched = facts["launched"] and facts["signed_in"]
    if launched:
        launched_detail = f"{facts['version']} on {facts['device']}"
    elif facts["launched"]:
        launched_detail = f"{facts['version']} started, never signed in"
    else:
        launched_detail = "no session start logged"

    in_room = facts["joined_room"] and facts["realtime_connected"] and facts["mech_spawned"]
    if in_room:
        room_detail = f"{facts['room_level']} - room {facts['room_id'][:8]}"
    else:
        reached = [name for name, done in (
            ("found match", facts["found_match"]),
            ("joined room", facts["joined_room"]),
            ("realtime connected", facts["realtime_connected"]),
            ("mech spawned", facts["mech_spawned"])) if done]
        room_detail = f"got as far as: {', '.join(reached)}" if reached else "never reached a room"

    return [
        {"id": f"{tag}_launched", "label": f"{title} launched",
         "state": "pass" if launched else "fail", "detail": launched_detail},
        {"id": f"{tag}_room", "label": f"{title} in room",
         "state": "pass" if in_room else "fail", "detail": room_detail},
    ]


def _players_check(quest, expected):
    """Whether the room filled up. Counted from the Quest alone: the run joins on a private
    key, so everyone in its roster is from this test and nobody else can be."""
    if quest is None:
        return {"id": "players", "label": "Players in room", "state": "unknown",
                "detail": f"no {SESSION_LOG} found"}

    joined = len(quest["players"])
    if expected is None:
        return {"id": "players", "label": "Players in room", "state": "unknown",
                "detail": f"{joined} joined, expected count not provided"}
    return {"id": "players", "label": "Players in room",
            "state": "pass" if joined == expected else "fail",
            "detail": f"{joined}/{expected} joined"}


def _kills_check(combat):
    """Whether players died at all - a full room at a good frame rate that never fights is
    still a broken run."""
    if combat is None:
        return {"id": "kills", "label": "Players can be killed", "state": "unknown",
                "detail": f"no {SESSION_LOG} to read kills from"}
    total = combat["kills"]
    return {"id": "kills", "label": "Players can be killed",
            "state": "pass" if total else "fail",
            "detail": (f"{total} kills - {combat['killers']} killers / {combat['victims']} victims"
                       if total else "no kills logged for the whole run")}


def analyze(test_dir, expected_players=None):
    clients = {}
    for tag, logs_dir in (("quest", QUEST_LOGS_DIR), ("steam", STEAM_LOGS_DIR)):
        label, text = _read_session_log(os.path.join(test_dir, logs_dir))
        if text is None:
            print(f"[CHECKS] No {SESSION_LOG} under {logs_dir}/ - {tag} checks report unknown.")
            clients[tag] = None
            continue
        print(f"[CHECKS] Reading {tag} session log: {label}")
        clients[tag] = parse_client(label, text)

    # Kills come from the Quest, the client under test; the Steam bot's own view is the
    # fallback for a run whose headset log did not survive.
    source = clients["quest"] or clients["steam"]
    combat = None
    if source is not None:
        kills = source["kills"]
        combat = {
            "source": source["log"],
            "kills": len(kills),
            "killers": len({k["killer_id"] for k in kills}),
            "victims": len({k["victim_id"] for k in kills}),
            "first_kill": kills[0]["time"] if kills else None,
            "last_kill": kills[-1]["time"] if kills else None,
        }

    checks = []
    checks += _client_checks("quest", "Quest", clients["quest"])
    checks += _client_checks("steam", "Steam", clients["steam"])
    checks.append(_players_check(clients["quest"], expected_players))
    checks.append(_kills_check(combat))

    # The per-kill list is working data - the counts above are what anything reads, and a
    # long run logs hundreds of them.
    for facts in clients.values():
        if facts is not None:
            facts.pop("kills", None)

    return {
        "clients": clients,
        "combat": combat,
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
    parser.add_argument("--out", default=None,
                        help=f"Where to write the JSON (default: <test_dir>/{OUTPUT_NAME})")
    args = parser.parse_args()

    if not os.path.isdir(args.test_dir):
        print(f"ERROR: Directory not found: {args.test_dir}")
        return 1

    result = analyze(args.test_dir, args.expected_players)

    label = {"pass": "PASS", "fail": "FAIL", "unknown": "????"}
    print()
    for check in result["checks"]:
        print(f"  [{label[check['state']]}] {check['label']}: {check['detail']}")
    print(f"\n  {result['passed']} passed, {result['failed']} failed, {result['unknown']} unknown")

    out_path = args.out or os.path.join(args.test_dir, OUTPUT_NAME)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"[CHECKS] Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

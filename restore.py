#!/usr/bin/env python3
"""Open the Claude Code sessions from an earlier snapshot again.

The script reads the files that snapshot.py writes. It shows one line for each
session, and it opens a Terminator window for each session that you pick. Each
window runs an independent Terminator process, so a crash in one window cannot
stop the other windows.

Without a flag the script reads the last list of the boot before this one. A
session that you open after the boot therefore cannot hide the list that the
crash killed. The flag --list shows every archived list, and the flag --scan
builds a list from the transcripts when every snapshot file is gone.
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from snapshot import (  # noqa: E402
    ARCHIVE,
    OUT,
    PREV,
    PROJECTS,
    archive_files,
    boot_id,
    entry_from,
    live_sessions,
    load_snapshot,
)

TAIL_BYTES = 262144
SCAN_WINDOW = "2d"
UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def age_text(seconds):
    """Return the age of the snapshot in plain words."""
    if seconds < 90:
        return "less than a minute ago"
    if seconds < 5400:
        return "%d minutes ago" % round(seconds / 60)
    if seconds < 172800:
        return "%d hours ago" % round(seconds / 3600)
    return "%d days ago" % round(seconds / 86400)


def tail_lines(path):
    """Return the last lines of a file, and then all of the lines."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
                handle.readline()
            return handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def all_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def read_meta(path):
    """Return the title and the last prompt of a transcript."""
    title = ""
    prompt = ""
    for lines in (tail_lines(path), all_lines(path)):
        for line in reversed(lines):
            if not line.strip():
                continue
            if not title and '"ai-title"' not in line and '"last-prompt"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not title and row.get("type") == "ai-title":
                title = (row.get("aiTitle") or "").strip()
            if not prompt and row.get("type") == "last-prompt":
                prompt = " ".join((row.get("lastPrompt") or "").split())
            if title and prompt:
                return title, prompt
        if title or prompt:
            break
    return title, prompt


def short_dir(cwd):
    """Return a short form of a directory."""
    text = cwd.replace(HOME, "~", 1) if cwd.startswith(HOME) else cwd
    if len(text) <= 46:
        return text
    parts = text.split("/")
    return ".../" + "/".join(parts[-2:])


def describe(entry):
    """Add the title and the last prompt to one snapshot record."""
    title, prompt = read_meta(entry.get("transcript", ""))
    entry["title"] = title or entry.get("name") or os.path.basename(entry.get("cwd", ""))
    entry["prompt"] = prompt
    return entry


def parse_picks(text, count):
    """Turn your answer into a list of index numbers."""
    text = text.strip().lower()
    if text in ("a", "all"):
        return list(range(count))
    picks = []
    for part in text.replace(",", " ").split():
        if "-" in part[1:]:
            start, _, end = part.partition("-")
            try:
                for n in range(int(start), int(end) + 1):
                    picks.append(n - 1)
            except ValueError:
                continue
        else:
            try:
                picks.append(int(part) - 1)
            except ValueError:
                continue
    return [n for n in dict.fromkeys(picks) if 0 <= n < count]


def command_of(entry):
    """Return the shell command that resumes one session."""
    return "cd %s && claude --resume %s" % (entry["cwd"], entry["sessionId"])


def terminal_program():
    """Return the terminal program, and a note when the script finds none.

    The variable CLAUDE_RESTORE_TERMINAL names a program of your choice. Without
    that variable the script looks for terminator, and then for the terminal of
    the system.
    """
    wanted = os.environ.get("CLAUDE_RESTORE_TERMINAL", "").strip()
    names = [wanted] if wanted else ["terminator", "x-terminal-emulator"]
    for name in names:
        if shutil.which(name):
            return name, ""
    return "", "This machine has no %s." % " and no ".join(names)


def window_argv(program, entry):
    """Build the command that opens one window for one session."""
    title = entry["title"][:60]
    if os.path.basename(program) == "terminator":
        inner = "claude --resume %s; exec bash" % entry["sessionId"]
        return [
            program,
            "--no-dbus",
            "--working-directory=%s" % entry["cwd"],
            "-T",
            title,
            "-x",
            "bash",
            "-c",
            inner,
        ]
    inner = "cd %s && claude --resume %s; exec bash" % (
        shlex.quote(entry["cwd"]),
        entry["sessionId"],
    )
    return [program, "-e", "bash", "-c", inner]


def open_window(program, entry):
    """Open one terminal window that resumes one session."""
    subprocess.Popen(
        window_argv(program, entry),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def parse_window(text):
    """Turn a text such as 90m, 12h or 2d into seconds.

    A bare number counts as days, because the default window is also in days.
    """
    text = (text or "").strip().lower()
    units = {"m": 60, "h": 3600, "d": 86400}
    scale = units.get(text[-1:], 0)
    try:
        value = float(text[:-1] if scale else text)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value * (scale or 86400)


def cwd_of(path):
    """Return the directory of a session, from the head of its transcript.

    Do not build the directory from the name of the project folder. That name
    turns every slash into a dash, and no rule can undo that.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _ in range(40):
                line = handle.readline()
                if not line:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("cwd"):
                    return row["cwd"]
    except OSError:
        pass
    return ""


def from_transcripts(window):
    """Build a session list from the transcript files.

    This source needs no snapshot at all. It helps after the loss of every
    snapshot file, and on a machine where the timer never ran. The glob has one
    star only, so a transcript of a subagent stays out of the list.
    """
    limit = time.time() - window
    sessions = []
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        session_id = os.path.basename(path)[:-len(".jsonl")]
        if not UUID.match(session_id):
            continue
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            continue
        cwd = cwd_of(path) if stamp >= limit else ""
        if not cwd:
            continue
        sessions.append(
            entry_from({"sessionId": session_id, "cwd": cwd,
                        "startedAt": stamp * 1000, "kind": "interactive"})
        )
    return {
        "savedAt": time.time(),
        "scan": True,
        "sessions": sorted(sessions, key=lambda s: s["startedAt"]),
    }


def choose_default():
    """Return the best list to open again, and a note about its source.

    The rule is the boot. After a crash you want the sessions of the boot
    before this one. A session that you start after the boot also writes a
    snapshot, but that snapshot carries the id of the current boot. It can
    therefore never hide the list that the crash killed.
    """
    now = boot_id()
    for path in reversed(archive_files()):
        payload = load_snapshot(path)
        if not payload or not payload.get("sessions"):
            continue
        mark = payload.get("bootId") or ""
        if mark and mark != now:
            return payload, "the last list of the boot before this one"
    for path, note in ((OUT, "the list of now"), (PREV, "the list from before")):
        payload = load_snapshot(path)
        if payload and payload.get("sessions"):
            return payload, note
    return None, "No list on this machine. Try: claude-restore --scan"


def list_archive():
    """Print every archived list, with the newest at the end."""
    paths = archive_files()
    if not paths:
        print("\nNo archived list in %s." % ARCHIVE)
        print("The timer writes one when the list of sessions changes.\n")
        return 1
    now = boot_id()
    print()
    for number, path in enumerate(paths, 1):
        payload = load_snapshot(path) or {}
        sessions = payload.get("sessions", [])
        stamp = time.strftime("%m-%d %H:%M", time.localtime(payload.get("savedAt", 0)))
        mark = "  (this boot)" if payload.get("bootId") == now else ""
        print("%3d  %s  %2d sessions%s" % (number, stamp, len(sessions), mark))
        places = list(dict.fromkeys(short_dir(s.get("cwd", "")) for s in sessions))
        for text in places[:3]:
            print("       %s" % text)
        extra = len(places) - 3
        if extra > 0:
            print("       and %d more %s" % (extra, "directory" if extra == 1 else "directories"))
    print("\nOpen one of them with: claude-restore --snapshot N\n")
    return 0


def source_of(args):
    """Return the list that the flags ask for, and a note about its source.

    The note holds the reason when the list is None.
    """
    if args.scan:
        window = parse_window(args.scan)
        if window is None:
            return None, "The window %s needs a number and m, h or d." % args.scan
        return from_transcripts(window), (
            "a scan of the transcripts of the last %s" % args.scan)
    if args.snapshot is not None:
        paths = archive_files()
        if not 1 <= args.snapshot <= len(paths):
            return None, "There is no list %d. Run: claude-restore --list" % args.snapshot
        return (load_snapshot(paths[args.snapshot - 1]),
                "list %d of the archive" % args.snapshot)
    if args.file:
        return load_snapshot(args.file), "the file %s" % args.file
    if args.current:
        return load_snapshot(OUT), "the list of now"
    return choose_default()


def main():
    parser = argparse.ArgumentParser(
        description="Open the Claude sessions of an earlier list again.")
    parser.add_argument("--all", action="store_true", help="open every session, with no menu")
    parser.add_argument("--print", dest="show", action="store_true", help="print the commands only")
    parser.add_argument("--list", dest="index", action="store_true",
                        help="print the archived lists, and stop")
    parser.add_argument("--snapshot", type=int, metavar="N",
                        help="read list N of the archive")
    parser.add_argument("--current", action="store_true",
                        help="read the list of now, not the list of the last boot")
    parser.add_argument("--scan", nargs="?", const=SCAN_WINDOW, metavar="WINDOW",
                        help="build the list from the transcripts (default %s)" % SCAN_WINDOW)
    parser.add_argument("--file", help="read one snapshot file by its path")
    args = parser.parse_args()

    if args.index:
        return list_archive()

    snap, note = source_of(args)
    if not snap or not isinstance(snap, dict):
        print("\n%s\n" % note)
        return 1

    live = {s["sessionId"] for s in live_sessions()}
    gone = []
    ready = []
    running = []
    for entry in snap.get("sessions", []):
        if not entry.get("transcript") or not os.path.exists(entry["transcript"]):
            gone.append(entry)
        elif entry["sessionId"] in live:
            running.append(describe(entry))
        else:
            ready.append(describe(entry))

    if snap.get("scan"):
        print("\n%s. %d sessions to open." % (note[:1].upper() + note[1:], len(ready)))
    else:
        age = age_text(max(0.0, time.time() - snap.get("savedAt", 0)))
        stamp = time.strftime("%m-%d %H:%M", time.localtime(snap.get("savedAt", 0)))
        print("\nList of %s (%s), %s. %d sessions to open." % (stamp, age, note, len(ready)))
    hint = "Other lists: claude-restore --list"
    if snap.get("bootAt"):
        hint = "That boot started at %s.  %s" % (
            time.strftime("%m-%d %H:%M", time.localtime(snap["bootAt"])), hint)
    print(hint)

    for entry in running:
        print("  runs now: %s  [%s]" % (entry["title"], entry["sessionId"][:8]))
    for entry in gone:
        print("  no transcript: %s" % entry.get("sessionId", "")[:8])

    if not ready:
        print("\nNothing to open.\n")
        return 0

    if args.show:
        print()
        for entry in ready:
            print(command_of(entry))
        print()
        return 0

    picks = list(range(len(ready)))
    if not args.all:
        print()
        for number, entry in enumerate(ready, 1):
            print("%3d  %s" % (number, entry["title"]))
            tail = '  last: "%s"' % entry["prompt"][:60] if entry["prompt"] else ""
            print("     %s%s" % (short_dir(entry["cwd"]), tail))
        print("\nPick numbers, a range such as 2-5, or 'a' for all.")
        try:
            answer = input("Your pick (Enter quits): ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        picks = parse_picks(answer, len(ready))
        if not picks:
            print("Nothing to open.\n")
            return 0

    program, note = terminal_program()
    if not program:
        print("\n%s" % note)
        print("Run these commands yourself:\n")
        for number in picks:
            print(command_of(ready[number]))
        print()
        return 1

    print()
    for number in picks:
        entry = ready[number]
        open_window(program, entry)
        print("  opened: %s  [%s]" % (entry["title"], entry["sessionId"][:8]))
        time.sleep(0.3)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

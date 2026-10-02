#!/usr/bin/env python3
"""Save the list of live Claude Code sessions to a file.

Claude Code holds a registry of the live sessions in ~/.claude/sessions. A crash
or a forced shutdown removes that registry. This script keeps a copy on disk, so
the command claude-restore can open the same sessions after the next boot.

The script writes the copy only when it finds at least one live session. That
rule protects the last good copy at a shutdown and at a boot.

The script also keeps every past list under ~/.claude/session-snapshots. One
short list from one quick session can therefore never destroy the record of a
long day of work.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

HOME = os.path.expanduser("~")
REGISTRY_GLOB = os.path.join(HOME, ".claude", "sessions", "*.json")
PROJECTS = os.path.join(HOME, ".claude", "projects")
OUT = os.path.join(HOME, ".claude", "open-sessions.json")
PREV = os.path.join(HOME, ".claude", "open-sessions.prev.json")
ARCHIVE = os.path.join(HOME, ".claude", "session-snapshots")
KEEP_FILES = 500
KEEP_DAYS = 60
# The name of an archived file. Only a name of this shape sorts by age, so the
# rules below ignore any other file that lands in that directory.
STAMP = re.compile(r"^\d{8}-\d{6}\.json$")


def alive(pid):
    """Return True if the process runs now."""
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def transcript_of(session_id):
    """Return the transcript file of a session, or an empty text."""
    if not session_id:
        return ""
    hits = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
    return hits[0] if hits else ""


def entry_from(raw):
    """Build one output record from one registry record."""
    return {
        "sessionId": raw.get("sessionId", ""),
        "cwd": raw.get("cwd", ""),
        "name": raw.get("name", ""),
        "pid": raw.get("pid"),
        "startedAt": raw.get("startedAt"),
        "kind": raw.get("kind", ""),
        "transcript": transcript_of(raw.get("sessionId", "")),
    }


def from_registry():
    """Read the live sessions from the registry files."""
    out = {}
    for path in glob.glob(REGISTRY_GLOB):
        try:
            with open(path) as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            continue
        if not raw.get("sessionId") or not alive(raw.get("pid")):
            continue
        out[raw["sessionId"]] = entry_from(raw)
    return list(out.values())


UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)


def from_proc():
    """Read the sessions that show an id on the command line.

    A session needs some seconds to reach the registry. This source finds a
    session in that first period, but only when the command line holds the id.
    The command claude-restore always starts a session that way.
    """
    out = {}
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        pid = path.split("/")[2]
        try:
            with open(path, "rb") as handle:
                argv = handle.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if not argv or os.path.basename(argv[0]) != "claude":
            continue
        flags = {"--resume", "-r", "--session-id"}
        ids = [
            argv[n + 1]
            for n, word in enumerate(argv[:-1])
            if word in flags and UUID.fullmatch(argv[n + 1] or "")
        ]
        if not ids:
            continue
        try:
            cwd = os.readlink("/proc/%s/cwd" % pid)
            started = os.path.getmtime("/proc/%s" % pid) * 1000
        except OSError:
            continue
        out[ids[0]] = entry_from(
            {"sessionId": ids[0], "cwd": cwd, "pid": int(pid),
             "startedAt": started, "kind": "interactive"}
        )
    return list(out.values())


def from_cli():
    """Read the live sessions with the claude command, as a second source."""
    try:
        text = subprocess.run(
            ["claude", "agents", "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        rows = json.loads(text)
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [entry_from(r) for r in rows if r.get("sessionId")]


def live_sessions():
    """Return every live session that this machine shows."""
    found = {}
    for source in (from_registry, from_proc):
        for entry in source():
            found.setdefault(entry["sessionId"], entry)
    if not found:
        for entry in from_cli():
            found.setdefault(entry["sessionId"], entry)
    return list(found.values())


def boot_id():
    """Return the id of the current boot, or an empty text."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def boot_at():
    """Return the time of the current boot, in seconds."""
    try:
        with open("/proc/uptime") as handle:
            return time.time() - float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def write_json(path, payload):
    """Write one file in one step, so a crash cannot leave half a file."""
    handle, temp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as out:
            json.dump(payload, out, indent=1)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def archive_files():
    """Return the archived snapshots, from the oldest to the newest.

    The name of each file starts with the date and the time, so a sort by name
    is also a sort by age.
    """
    try:
        names = sorted(n for n in os.listdir(ARCHIVE) if STAMP.match(n))
    except OSError:
        return []
    return [os.path.join(ARCHIVE, n) for n in names]


def load_snapshot(path):
    """Return the content of one snapshot file, or None."""
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def ids_of(payload):
    """Return the set of session ids in one snapshot."""
    return {s.get("sessionId") for s in (payload or {}).get("sessions", [])}


def prune():
    """Delete the archived snapshots that are too old or too many."""
    paths = archive_files()
    stale = paths[:-KEEP_FILES] if len(paths) > KEEP_FILES else []
    limit = time.time() - KEEP_DAYS * 86400
    for path in paths[len(stale):]:
        try:
            if os.path.getmtime(path) < limit:
                stale.append(path)
        except OSError:
            continue
    for path in stale:
        try:
            os.unlink(path)
        except OSError:
            pass


def archive(payload):
    """Keep one copy of the snapshot, so a later short list cannot destroy it.

    The script writes a new file only when the list of sessions changes, or
    when the machine boots again. Without that rule the timer would write
    1440 identical files each day.
    """
    try:
        os.makedirs(ARCHIVE, mode=0o700, exist_ok=True)
    except OSError:
        return
    paths = archive_files()
    if paths:
        last = load_snapshot(paths[-1])
        same_boot = (last or {}).get("bootId") == payload.get("bootId")
        if same_boot and ids_of(last) == ids_of(payload):
            return
    name = time.strftime("%Y%m%d-%H%M%S", time.localtime(payload["savedAt"]))
    try:
        write_json(os.path.join(ARCHIVE, name + ".json"), payload)
    except OSError:
        return
    prune()


def write(sessions):
    """Replace the copy on disk, and keep the copies from before."""
    payload = {
        "savedAt": time.time(),
        "host": os.uname().nodename,
        "bootId": boot_id(),
        "bootAt": boot_at(),
        "sessions": sorted(sessions, key=lambda s: s.get("startedAt") or 0),
    }
    # The archive comes first. A crash after this line still leaves the copy.
    archive(payload)
    if os.path.exists(OUT):
        try:
            shutil.copy2(OUT, PREV)
        except OSError:
            pass
    write_json(OUT, payload)


def main():
    sessions = live_sessions()
    if not sessions:
        return 0
    write(sessions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

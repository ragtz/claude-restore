# claude-restore

Open your Claude Code sessions again after a crash or a forced shutdown.

## Why

Claude Code keeps a list of the live sessions in `~/.claude/sessions`. That list disappears when the
machine stops. After a crash you then have no record of the sessions that were open.

## The two parts

1. `snapshot.py` saves the list of live sessions. A timer of systemd runs it every minute.
2. `restore.py` reads a saved list and opens the sessions again. The link
   `~/.local/bin/claude-restore` points to it.

## Install

```bash
./install.sh
```

The script `install.sh` puts both parts in place. It does these steps:

1. It links this directory to `~/.claude/session-restore`.
2. It links `restore.py` to `~/.local/bin/claude-restore`.
3. It writes the timer of systemd, and it starts the timer.

The script needs `python3` and a systemd user session. It does not use `sudo`. You can run it
again with no risk. Run it again after you move this directory.

## The three places for a list

| Path | Content |
| --- | --- |
| `~/.claude/open-sessions.json` | The list of the last minute. |
| `~/.claude/open-sessions.prev.json` | The list of the minute before that. |
| `~/.claude/session-snapshots/` | One file for each change of the list, for 60 days. |

The archive is the important one. The snapshot script adds a file only when the set of sessions
changes, or when the machine boots again. A quiet day therefore costs a few files, not 1440. The
script deletes a file after 60 days. It also keeps 500 files at the most.

Each saved list also holds the id of the boot and the time of the boot.

## What the archive protects

The first two files roll every minute. One quick session after a crash can fill both of them with a
one-session list inside two minutes. The record of a whole day of work then disappears. The archive
keeps that record, because a new list never replaces an old file.

The snapshot script also writes nothing when it finds no live session. An empty list at a shutdown
cannot destroy the last good copy.

## Use

```bash
claude-restore                 # the last list of the boot before this one
claude-restore --list          # show every archived list, and stop
claude-restore --snapshot 4    # open list 4 of that archive
claude-restore --current       # the list of this minute
claude-restore --scan          # build a list from the transcripts of the last 2 days
claude-restore --scan 12h      # the same, with another window
claude-restore --all           # open every session, with no menu
claude-restore --print         # print the commands, open nothing
claude-restore --file PATH     # read one saved list by its path
```

### The default

Without a flag the command reads the last list of the boot before this one. That list holds the
sessions that the crash killed. A session that you open after the boot writes a new list, but that
new list carries the id of the current boot. It can therefore never hide the list that you want.

Use `--list` and `--snapshot` when Claude Code crashed and the machine stayed up. Every list then
carries the same boot id, and only you know which one is right.

### The menu

The menu shows the title of each session, its directory and your last prompt. Pick single numbers,
a range such as `2-5`, or `a` for all. Each pick opens a separate Terminator window with its own
process.

A session that runs now does not appear in the menu. The script lists it above the menu instead.

### The scan

`--scan` ignores every saved list. It reads the modification time of each transcript under
`~/.claude/projects`, and it reads the directory of the session from the transcript itself. Use it
when no saved list survives, or on a machine where the timer never ran. A bare number counts as
days, and the suffixes `m`, `h` and `d` also work.

## The terminal program

The script looks for `terminator` first, and then for `x-terminal-emulator`. To name another
program, set the variable `CLAUDE_RESTORE_TERMINAL`:

```bash
CLAUDE_RESTORE_TERMINAL=xterm claude-restore
```

The script prints the commands instead, when it finds no program of that name.

## The timer

```bash
systemctl --user status claude-session-snapshot.timer
systemctl --user list-timers claude-session-snapshot.timer
journalctl --user -u claude-session-snapshot.service -n 20
```

To stop the snapshot, run `systemctl --user disable --now claude-session-snapshot.timer`.

## Where the list comes from

The snapshot script reads three sources, in this order:

1. The files in `~/.claude/sessions`, which hold the id, the directory and the pid of each session.
2. The processes under `/proc`, for a session that shows its id on the command line. A new session
   needs some minutes to reach the registry, and this source covers that period.
3. The command `claude agents --json`, if the first two sources give nothing.

## Limits

- A session that you close cleanly stays in the list for up to one minute.
- A new session with no id on its command line is missing from the list until the registry holds it.
  A session with no message has no history to restore, so this costs you little.
- The restore command cannot rebuild your window positions or your window sizes.
- `--scan` finds a session that you closed on purpose, because a closed session and a killed session
  leave the same transcript.

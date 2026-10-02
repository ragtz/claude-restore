#!/bin/bash
#
# install.sh: install claude-restore for the current user.
#
# The script links this directory to ~/.claude/session-restore. It links
# restore.py to ~/.local/bin/claude-restore. It writes a service and a timer of
# systemd for the user, and the timer runs snapshot.py every minute. You can run
# the script again with no risk, because each step checks the current state.
#
#   ./install.sh          # install, or repair an install
#   ./install.sh --help   # show this text
#
# The script needs python3 and a systemd user session. It does not use sudo.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLAUDE_DIR="$HOME/.claude"
LOCAL_BIN="$HOME/.local/bin"
RESTORE_DEST="$CLAUDE_DIR/session-restore"
RESTORE_CMD="$LOCAL_BIN/claude-restore"
SNAPSHOT_UNIT="claude-session-snapshot"
SNAPSHOT_ARCHIVE="$CLAUDE_DIR/session-snapshots"
UNIT_DIR="$HOME/.config/systemd/user"

FOLLOW_UPS=()

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
  C_INFO=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_INFO=''; C_WARN=''; C_ERR=''; C_OFF=''
fi

info() { printf '%s==>%s %s\n' "$C_INFO" "$C_OFF" "$*"; }
warn() { printf '%swarn:%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
  # The header comment block, minus the shebang, is the help text.
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# backup <path>: move an existing file or directory aside, and log the new path.
backup() {
  local path="$1" dest
  dest="$path.bak.$(date +%Y%m%d%H%M%S)"
  mv "$path" "$dest"
  warn "moved existing $path -> $dest"
}

# link <target> <linkname>: make a symlink. The function replaces an old
# symlink, and it moves a real file or directory aside before it makes the link.
link() {
  local target="$1" linkname="$2"

  mkdir -p "$(dirname "$linkname")"

  if [[ -L "$linkname" ]]; then
    local current
    current="$(readlink -f "$linkname")"
    if [[ "$current" == "$(readlink -f "$target")" ]]; then
      info "$linkname already links to $target"
      return
    fi
    warn "$linkname pointed at $current -- repointing to $target"
    rm "$linkname"
  elif [[ -e "$linkname" ]]; then
    backup "$linkname"
  fi

  ln -s "$target" "$linkname"
  info "linked $linkname -> $target"
}

# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

preflight() {
  [[ $EUID -ne 0 ]] || die "run as your normal user, not root -- the install goes into \$HOME"

  local file
  for file in restore.py snapshot.py; do
    [[ -f "$REPO_DIR/$file" ]] || die "$REPO_DIR/$file not found"
  done

  command -v python3 >/dev/null 2>&1 || die "python3 is required"

  info "repo: $REPO_DIR"
}

install_links() {
  info "step 1: links"

  link "$REPO_DIR" "$RESTORE_DEST"
  link "$REPO_DIR/restore.py" "$RESTORE_CMD"

  case ":$PATH:" in
    *":$LOCAL_BIN:"*) ;;
    *)
      warn "$LOCAL_BIN is not on PATH"
      FOLLOW_UPS+=("Add $LOCAL_BIN to PATH, so that the shell finds claude-restore.")
      ;;
  esac
}

# The loop at the end reads $service and $timer through ${!name}.
# shellcheck disable=SC2034
install_timer() {
  info "step 2: snapshot timer"

  # The list of live sessions under ~/.claude/sessions disappears when the
  # machine stops, so a timer saves a copy every minute.
  mkdir -p "$UNIT_DIR"

  # The units name $RESTORE_DEST, not the repo path. If you move the repo, only
  # the symlink changes, and the unit files stay the same.
  local service timer
  service="$(cat <<EOF
[Unit]
Description=Save the list of open Claude Code sessions

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 %h/.claude/session-restore/snapshot.py
Nice=10
EOF
)"

  # OnActiveSec gives the first run, because OnUnitActiveSec alone waits for a
  # run that never comes. OnBootSec covers the boot of the machine.
  timer="$(cat <<EOF
[Unit]
Description=Save the list of open Claude Code sessions every minute

[Timer]
OnBootSec=2min
OnActiveSec=30s
OnUnitActiveSec=1min
AccuracySec=10s
Persistent=false
Unit=$SNAPSHOT_UNIT.service

[Install]
WantedBy=timers.target
EOF
)"

  local name content path
  for name in service timer; do
    path="$UNIT_DIR/$SNAPSHOT_UNIT.$name"
    content="${!name}"
    if [[ -f "$path" ]] && [[ "$(cat "$path")" == "$content" ]]; then
      info "$path already up to date"
    else
      printf '%s\n' "$content" >"$path"
      info "wrote $path"
    fi
  done

  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found -- cannot enable $SNAPSHOT_UNIT.timer here"
    FOLLOW_UPS+=("Enable the snapshot timer: systemctl --user enable --now $SNAPSHOT_UNIT.timer")
    return
  fi

  # In a container or over plain ssh there is no user bus. The unit files then
  # exist, but nothing can start them.
  if ! systemctl --user daemon-reload 2>/dev/null; then
    warn "no systemd user session available -- units written but not enabled"
    FOLLOW_UPS+=("Log into a graphical session and run: systemctl --user enable --now $SNAPSHOT_UNIT.timer")
    return
  fi

  if systemctl --user is-active "$SNAPSHOT_UNIT.timer" >/dev/null 2>&1; then
    # A changed unit file takes effect only after a restart of the timer.
    systemctl --user restart "$SNAPSHOT_UNIT.timer" >/dev/null 2>&1 || true
    info "$SNAPSHOT_UNIT.timer already running"
  elif systemctl --user enable --now "$SNAPSHOT_UNIT.timer" >/dev/null 2>&1; then
    info "enabled $SNAPSHOT_UNIT.timer (saves the open sessions every minute)"
  else
    warn "could not start $SNAPSHOT_UNIT.timer"
    FOLLOW_UPS+=("Start the snapshot timer: systemctl --user enable --now $SNAPSHOT_UNIT.timer")
  fi
}

verify() {
  info "step 3: verifying"

  if python3 -m py_compile "$REPO_DIR/snapshot.py" "$REPO_DIR/restore.py" 2>/dev/null; then
    info "scripts compile"
    rm -rf "$REPO_DIR/__pycache__"
  else
    warn "scripts do not compile"
    FOLLOW_UPS+=("Check $REPO_DIR for a broken script.")
  fi

  # The archive holds one file for each change of the session list.
  local archived=0
  if [[ -d "$SNAPSHOT_ARCHIVE" ]]; then
    archived="$(find "$SNAPSHOT_ARCHIVE" -maxdepth 1 -name '*.json' | wc -l)"
  fi
  info "$archived archived session lists in $SNAPSHOT_ARCHIVE"

  if systemctl --user is-active "$SNAPSHOT_UNIT.timer" >/dev/null 2>&1; then
    info "$SNAPSHOT_UNIT.timer is running"
  else
    warn "$SNAPSHOT_UNIT.timer is not running -- no session list is saved"
    FOLLOW_UPS+=("Start the snapshot timer: systemctl --user enable --now $SNAPSHOT_UNIT.timer")
  fi
}

summary() {
  echo
  info "install complete"
  echo "  command: $RESTORE_CMD -> $REPO_DIR/restore.py"
  echo "  scripts: $RESTORE_DEST -> $REPO_DIR"
  echo "  timer:   $UNIT_DIR/$SNAPSHOT_UNIT.timer"

  [[ ${#FOLLOW_UPS[@]} -gt 0 ]] || return 0

  echo
  info "follow-ups:"
  local item
  local -A seen=()
  for item in "${FOLLOW_UPS[@]}"; do
    [[ -n "${seen[$item]:-}" ]] && continue
    seen[$item]=1
    echo "  - $item"
  done
}

main() {
  case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac

  preflight
  install_links
  install_timer
  verify
  summary
}

# The guard lets you source the functions for a test, with no install.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

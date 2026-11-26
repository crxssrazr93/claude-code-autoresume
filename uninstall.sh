#!/usr/bin/env bash
# Remove the detector and cancel every pending auto-resume job.
#
#   ./uninstall.sh [--dir DIR] [--purge]
#
# Linux removes the systemd timer, macOS (beta) the LaunchAgents. On Windows use
# uninstall.ps1. DIR is the install directory (default $AUTORESUME_DIR, then
# ~/.claude/auto-resume). It is deleted only when install.sh marked it. With --purge
# the logs and state are deleted too.
# Remove the SessionStart hook and autoContinueAtUsageLimit from settings.json yourself,
# if you added them.
set -uo pipefail

DEST="${AUTORESUME_DIR:-$HOME/.claude/auto-resume}"
PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DEST="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
DEST="${DEST/#\~/$HOME}"

finish() {
  if [[ $PURGE -eq 1 ]]; then
    rm -rf "$DATA"
    echo "purged $DATA"
  fi
  if [[ -f "$DEST/.autoresume-install" ]]; then
    rm -rf "$DEST"
    echo "removed $DEST"
  fi
  echo "uninstalled"
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  DATA="${XDG_DATA_HOME:-$HOME/Library/Application Support}/auto-resume"
  AGENTS="$HOME/Library/LaunchAgents"
  uid="$(id -u)"
  for plist in "$AGENTS"/com.claude-code-autoresume.*.plist; do
    [[ -e "$plist" ]] || continue
    label="$(basename "$plist" .plist)"
    launchctl bootout "gui/$uid/$label" 2>/dev/null && echo "cancelled $label"
    rm -f "$plist"
  done
  finish
  exit 0
fi

UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/auto-resume"

systemctl --user disable --now claude-autoresume-detect.timer 2>/dev/null
systemctl --user stop claude-autoresume-detect.service 2>/dev/null
for t in $(systemctl --user list-units 'claude-autoresume-*' --all --no-legend --plain | awk '{print $1}'); do
  systemctl --user stop "$t" 2>/dev/null && echo "cancelled $t"
done
rm -f "$UNITS/claude-autoresume-detect.service" "$UNITS/claude-autoresume-detect.timer"
systemctl --user daemon-reload
finish

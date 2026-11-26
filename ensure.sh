#!/bin/bash
# SessionStart hook: make sure the auto-resume detector is enabled and running,
# so Claude Code sessions on this machine are resumed after a usage limit resets.
# Silent when all is well; prints one JSON systemMessage when it had to repair something.
if [ "$(uname -s)" = "Darwin" ]; then
  label=com.claude-code-autoresume.detect
  target="gui/$(id -u)/$label"
  plist="$HOME/Library/LaunchAgents/$label.plist"
  if launchctl print "$target" >/dev/null 2>&1; then
    exit 0
  fi
  if [ -f "$plist" ] && launchctl bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1; then
    printf '{"systemMessage": "Auto-resume detector was started again."}\n'
  else
    printf '{"systemMessage": "Auto-resume is OFF: the LaunchAgent is not loaded. Re-run the auto-resume install.sh"}\n'
  fi
  exit 0
fi
unit=claude-autoresume-detect.timer
fixed=""
if ! systemctl --user is-enabled --quiet "$unit" 2>/dev/null; then
  systemctl --user enable "$unit" >/dev/null 2>&1 && fixed="enabled"
fi
if ! systemctl --user is-active --quiet "$unit" 2>/dev/null; then
  systemctl --user start "$unit" >/dev/null 2>&1 && fixed="${fixed:+$fixed and }started"
fi
if ! systemctl --user is-active --quiet "$unit" 2>/dev/null; then
  printf '{"systemMessage": "Auto-resume is OFF: %s could not start. Re-run the auto-resume install.sh"}\n' "$unit"
elif [ -n "$fixed" ]; then
  printf '{"systemMessage": "Auto-resume detector was %s."}\n' "$fixed"
fi
exit 0

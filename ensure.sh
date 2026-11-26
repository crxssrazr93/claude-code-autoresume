#!/bin/bash
# SessionStart hook: make sure the auto-resume detector timer is enabled and running,
# so Claude Code sessions on this machine are resumed after a usage limit resets.
# Silent when all is well; prints one JSON systemMessage when it had to repair something.
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

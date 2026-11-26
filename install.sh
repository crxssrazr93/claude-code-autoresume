#!/usr/bin/env bash
# Install the auto-resume detector as a systemd user timer.
#
#   ./install.sh [--dir DIR] [--skip-tests]
#
# DIR defaults to $AUTORESUME_DIR, then ~/.claude/auto-resume. The scripts are
# copied there, the two systemd user units are written, and the timer is enabled.
# Your Claude Code settings.json is never edited. The two settings worth adding
# are printed at the end for you to merge by hand.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${AUTORESUME_DIR:-$HOME/.claude/auto-resume}"
RUN_TESTS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DEST="$2"; shift 2 ;;
    --skip-tests) RUN_TESTS=0; shift ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
DEST="${DEST/#\~/$HOME}"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/auto-resume"
PYTHON="$(command -v python3)"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "systemd user session not available. This tool needs Linux with systemd user sessions." >&2
  exit 1
fi

if [[ $RUN_TESTS -eq 1 ]]; then
  python3 -m unittest discover -s "$SRC/tests" -p "test_*.py" >/dev/null 2>&1 \
    || { echo "tests failed, not installing (run: python3 -m unittest discover -s $SRC/tests)" >&2; exit 1; }
fi

if [[ "$DEST" != "$SRC" ]]; then
  mkdir -p "$DEST"
  cp "$SRC"/{detect.py,fire.py,lib.py,peer.py,reach.py,ensure.sh,uninstall.sh} "$DEST/"
  chmod +x "$DEST"/{detect.py,fire.py,reach.py,ensure.sh,uninstall.sh}
fi
touch "$DEST/.autoresume-install"

ENVLINES=""
[[ -n "${CLAUDE_CONFIG_DIR:-}" ]] && ENVLINES+="Environment=CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR"$'\n'
[[ -n "${XDG_DATA_HOME:-}" ]] && ENVLINES+="Environment=XDG_DATA_HOME=$XDG_DATA_HOME"$'\n'
[[ -n "${AUTORESUME_DELAY:-}" ]] && ENVLINES+="Environment=AUTORESUME_DELAY=$AUTORESUME_DELAY"$'\n'
[[ -n "${AUTORESUME_MESSAGE:-}" ]] && ENVLINES+="Environment=\"AUTORESUME_MESSAGE=$AUTORESUME_MESSAGE\""$'\n'

mkdir -p "$UNITS" "$DATA"
sed -e "s|@PYTHON@|$PYTHON|" -e "s|@DIR@|$DEST|" \
    "$SRC/systemd/claude-autoresume-detect.service" > "$UNITS/claude-autoresume-detect.service"
if [[ -n "$ENVLINES" ]]; then
  printf '%s' "$ENVLINES" >> "$UNITS/claude-autoresume-detect.service"
fi
sed -i '/^@ENV@$/d' "$UNITS/claude-autoresume-detect.service"
cp "$SRC/systemd/claude-autoresume-detect.timer" "$UNITS/"

systemctl --user daemon-reload
systemctl --user enable --now claude-autoresume-detect.timer

echo "Installed to $DEST"
systemctl --user list-timers claude-autoresume-detect.timer --no-pager
cat <<EOF

Optional, merge these into ~/.claude/settings.json yourself (nothing was changed):

1. Keep the timer alive. A SessionStart hook repairs it if it was disabled:

  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "$DEST/ensure.sh" } ] }
    ]
  }

2. Let Claude Code continue by itself at the limit reset (it pairs with this tool):

  "autoContinueAtUsageLimit": true

Verify with:
  python3 -I $DEST/reach.py
  python3 $DEST/detect.py status
  python3 $DEST/detect.py scan --dry-run
EOF

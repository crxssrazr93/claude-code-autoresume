#!/usr/bin/env bash
# Install the auto-resume detector.
#
#   ./install.sh [--dir DIR] [--skip-tests]
#
# Linux: a systemd user timer. macOS (beta): a launchd LaunchAgent. On Windows use
# install.ps1 instead. DIR defaults to $AUTORESUME_DIR, then ~/.claude/auto-resume.
# The scripts are copied there and the detector is started. Your Claude Code
# settings.json is never edited. The two settings worth adding are printed at the end
# for you to merge by hand.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${AUTORESUME_DIR:-$HOME/.claude/auto-resume}"
RUN_TESTS=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DEST="$2"; shift 2 ;;
    --skip-tests) RUN_TESTS=0; shift ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
DEST="${DEST/#\~/$HOME}"
FILES=(detect.py fire.py lib.py peer.py reach.py platform_base.py platform_linux.py platform_macos.py platform_windows.py ensure.sh uninstall.sh)

print_next_steps() {
  cat <<EOF

Optional, merge these into ~/.claude/settings.json yourself (nothing was changed):

1. Keep the detector alive. A SessionStart hook repairs it if it was disabled:

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
}

install_macos() {
  local label="com.claude-code-autoresume.detect"
  local agents="$HOME/Library/LaunchAgents"
  local data="${XDG_DATA_HOME:-$HOME/Library/Application Support}/auto-resume"
  local py ver major minor uid
  py="$(command -v python3 || true)"
  [[ -n "$py" ]] || { echo "python3 not found. Install Python 3.11 or newer (for example: brew install python)." >&2; return 1; }
  ver="$("$py" --version 2>&1 | awk '{print $2}')"
  major="${ver%%.*}"; minor="${ver#*.}"; minor="${minor%%.*}"
  if [[ "$major" -lt 3 || ( "$major" -eq 3 && "$minor" -lt 11 ) ]]; then
    echo "Python $ver is too old. This tool needs 3.11 or newer (for example: brew install python)." >&2
    return 1
  fi
  if [[ $RUN_TESTS -eq 1 ]]; then
    "$py" -m unittest discover -s "$SRC/tests" -p "test_*.py" >/dev/null 2>&1 \
      || { echo "tests failed, not installing (run: $py -m unittest discover -s $SRC/tests)" >&2; return 1; }
  fi
  if [[ "$DEST" != "$SRC" ]]; then
    mkdir -p "$DEST"
    for f in "${FILES[@]}"; do cp "$SRC/$f" "$DEST/"; done
    chmod +x "$DEST"/{detect.py,fire.py,reach.py,ensure.sh,uninstall.sh}
  fi
  touch "$DEST/.autoresume-install"
  # psutil lists processes on macOS. A private venv avoids the "externally managed" pip error.
  if ! "$py" -m pip show psutil >/dev/null 2>&1; then
    "$py" -m venv "$DEST/venv"
    "$DEST/venv/bin/pip" install --quiet psutil
    py="$DEST/venv/bin/python"
  fi
  mkdir -p "$agents" "$data"
  "$py" "$DEST/platform_macos.py" detect-plist --python "$py" --dir "$DEST" \
    --log "$data/launchd.log" --out "$agents/$label.plist"
  uid="$(id -u)"
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$uid" "$agents/$label.plist"
  launchctl enable "gui/$uid/$label"
  echo "Installed to $DEST (macOS, beta). LaunchAgent: $agents/$label.plist"
  launchctl print "gui/$uid/$label" | grep -E "state|run interval" || true
  print_next_steps
}

if [[ "$(uname -s)" == "Darwin" ]]; then
  install_macos
  exit $?
fi

UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/auto-resume"
PYTHON="$(command -v python3)"

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "systemd user session not available." >&2
  if grep -qi microsoft /proc/version 2>/dev/null; then
    # WSL without systemd: install the scripts and let a Windows scheduled task run the scan.
    # The scan delivers due resumes itself (poll mode), so no timer is needed.
    mkdir -p "$DEST"
    if [[ "$DEST" != "$SRC" ]]; then
      for f in "${FILES[@]}"; do cp "$SRC/$f" "$DEST/"; done
      chmod +x "$DEST"/{detect.py,fire.py,reach.py,ensure.sh,uninstall.sh}
    fi
    touch "$DEST/.autoresume-install"
    echo "WSL without systemd. Scripts copied to $DEST. No timer was created." >&2
    echo "Run this once in Windows PowerShell or cmd, so a Windows scheduled task runs the scan every 2 minutes:" >&2
    echo "" >&2
    echo "  schtasks /Create /TN claude-autoresume-wsl /SC MINUTE /MO 2 /TR \"wsl.exe -e env AUTORESUME_SCHEDULER=poll python3 $DEST/detect.py scan\"" >&2
    echo "" >&2
    echo "Or enable systemd instead: add [boot] and systemd=true to /etc/wsl.conf, run 'wsl --shutdown' from Windows, then run ./install.sh again." >&2
    exit 0
  fi
  echo "This tool needs Linux with systemd user sessions (or macOS or Windows)." >&2
  exit 1
fi

if [[ $RUN_TESTS -eq 1 ]]; then
  python3 -m unittest discover -s "$SRC/tests" -p "test_*.py" >/dev/null 2>&1 \
    || { echo "tests failed, not installing (run: python3 -m unittest discover -s $SRC/tests)" >&2; exit 1; }
fi

if [[ "$DEST" != "$SRC" ]]; then
  mkdir -p "$DEST"
  for f in "${FILES[@]}"; do cp "$SRC/$f" "$DEST/"; done
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
print_next_steps

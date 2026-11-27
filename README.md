# claude-code-autoresume

Resumes Claude Code sessions that were stopped by a usage limit, at the limit reset plus 5 minutes, without anyone typing "continue".

This is an unofficial community tool. It is not affiliated with or endorsed by Anthropic.

## Platform support

| Platform | Status | Scheduler | Process discovery | Delivery routes |
| --- | --- | --- | --- | --- |
| Linux (systemd) | Stable, used daily | systemd user timer | `/proc` (psutil if installed) | inbox socket, tmux, Konsole D-Bus |
| macOS | Beta, tested in CI only | launchd LaunchAgent | psutil | inbox socket, tmux, iTerm2 Python API |
| Windows | Beta, tested in CI only | Task Scheduler (`schtasks`) | psutil | inbox named pipe |
| WSL 2 | Works like Linux with systemd. Without systemd, use the poll mode below | systemd, or the Windows scheduler | `/proc` | same as Linux |

### macOS and Windows: beta, help wanted

These are tested in CI only. If you run it on a Mac or Windows machine, please open an issue with the output of `reach.py` and `detect.py status`, or send a PR. One short report, good or bad, helps a lot. The issue template for it is `platform-report.md`.

### Tip: Claude Code can do this by itself

Claude Code's built in `autoContinueAtUsageLimit` setting works on every platform. This tool is a safety net on top of it: it covers sessions where that setting did not fire, and it works from outside the Claude process. Turn the setting on first. Install this tool as well if you want the extra cover.

    "autoContinueAtUsageLimit": true

## The problem

When Claude Code hits a usage limit, the session stops and waits. If you leave your desk, long running work sits idle for hours after the limit has already reset. Someone has to come back and type "continue".

## How it works

A scheduler runs a plain Python detector every 2 minutes. Claude is not involved in the polling. Claude receives text only when a resume is actually due.

1. The detector scans recent Claude Code transcripts. When a limit stops a turn, Claude Code writes an assistant record with `isApiErrorMessage: true`, `apiError: "usage_limit_reached"` and `apiErrorParams.rate_limit_info.resetsAt` (epoch seconds). The detector reads that. The visible text ("resets 11:20pm (Asia/Kolkata)") is only a fallback.
2. It maps the stopped session to a live `claude` process, using the session registry that Claude Code writes (`sessions/<pid>.json` in its config directory).
3. It schedules a one shot job for the reset plus 5 minutes.
4. At that time `fire.py` checks everything again and sends one message to the session.

Each platform has one module that holds its process discovery, scheduling and delivery code: `platform_linux.py`, `platform_macos.py`, `platform_windows.py`. `platform_base.py` holds the shared interface.

### Where the session registry lives

Claude Code writes it to `sessions/` inside its config directory. The default is `~/.claude` (on Windows `%USERPROFILE%\.claude`). Set `CLAUDE_CONFIG_DIR` to use another one. Each entry holds the process id, the session id, and `messagingSocketPath`, which is where the session listens for messages. This tool never hard codes a socket directory. It always reads the path from the registry.

### Delivery order

No window focus is ever used. The routes, in order:

1. The session's native inbox. Claude Code binds an inbox for each session and records its path in the registry as `messagingSocketPath`. `peer.py` writes the same one line frame that Claude Code's own cross session `SendMessage` writes. An idle session starts a turn from it. No terminal is involved.
   * Linux and macOS: a Unix socket. On Linux it is usually `/run/user/<uid>/cc-socks/<pid>.sock`.
   * Windows: a named pipe (`\\.\pipe\...`) is opened as a binary file and gets the same frame. If the registry holds no usable path, nothing is delivered and the log says that Claude Code's own `autoContinueAtUsageLimit` should handle the resume.
2. tmux, when `TMUX` and `TMUX_PANE` are in the process environment: `send-keys -l` for the text, then `Enter`.
3. A terminal API for one named session:
   * Linux, Konsole: `sendText` over D-Bus with the message, then a separate `sendText` with a carriage return. Konsole blocks `sendText` by default. Enable "Security sensitive parts of the DBus API" in its settings if you need this route.
   * macOS, iTerm2: the iTerm2 Python API sends text to the one session whose id is in `ITERM_SESSION_ID`. It needs `python3 -m pip install iterm2` and "Enable Python API" in iTerm2 settings (General, Magic). It never touches the focused window.
4. Anything else: the session is not resumed.

Before sending, the target is verified. A Konsole shell, tmux pane or iTerm2 session must be an ancestor of the `claude` process. In Konsole `claude` must also be the foreground job.

### Why Terminal.app is not supported

Terminal.app can only be scripted through AppleScript. A tab can be found by its tty without focus, but sending text to it needs the Automation permission, which an unattended background job cannot ask for, and it cannot be tested without a Mac. It would also be the one route where a wrong tab gets the text. A resume that goes to the wrong place is worse than a late one, so it is left out. Use the inbox socket, tmux or iTerm2 instead. A PR with a safe, tab targeted version is welcome.

### Why focused window typing is never used

A tool that types into "the window with focus" will type into whatever has focus. On Wayland nothing can report which window that is. At 3 a.m. the text could land in a browser, a chat, or a terminal running something else. A resume that goes to the wrong place is worse than a late one, so a session with no verified route is skipped.

## Requirements

* Python 3.11 or newer.
* Claude Code, using its default config directory (or `CLAUDE_CONFIG_DIR`).
* Linux: systemd user sessions (`systemctl --user` works). No packages are needed. If `psutil` is installed it is used for process discovery, and without it the `/proc` code runs.
* macOS: `psutil` (the installer puts it in a private venv if it is missing). Optional: `tmux`, or iTerm2 with the `iterm2` package.
* Windows: `psutil` and `tzdata` (`install.ps1` installs them). Windows has no time zone database of its own, and `tzdata` supplies it.

## Install

### Linux

    git clone <this repository> claude-code-autoresume
    cd claude-code-autoresume
    ./install.sh                       # installs to ~/.claude/auto-resume
    ./install.sh --dir ~/tools/autoresume --skip-tests

`install.sh` runs the tests, copies the scripts to the install directory, writes two systemd user units, and enables the timer. It then prints two snippets for you to merge into `~/.claude/settings.json` yourself. It never edits your settings.

1. A SessionStart hook that re-enables the timer if it was ever turned off:

        "hooks": {
          "SessionStart": [
            { "hooks": [ { "type": "command", "command": "<install dir>/ensure.sh" } ] }
          ]
        }

2. Claude Code's own setting, which pairs with this tool:

        "autoContinueAtUsageLimit": true

### macOS (beta)

    git clone <this repository> claude-code-autoresume
    cd claude-code-autoresume
    ./install.sh

The same script detects macOS. It checks for Python 3.11 or newer, installs `psutil` into `~/.claude/auto-resume/venv` if the system Python lacks it, copies the scripts, and writes `~/Library/LaunchAgents/com.claude-code-autoresume.detect.plist` (`StartInterval` 120, also run at login). It loads it with `launchctl bootstrap gui/<uid>`. Each resume is a second LaunchAgent that fires at the reset time. It is removed by a later scan.

Logs and state go to `~/Library/Application Support/auto-resume`. The LaunchAgent has a minimal PATH, so the installer adds the Homebrew folders so that `tmux` is found.

### Windows (beta)

In PowerShell, from the cloned folder:

    .\install.ps1                      # installs to %USERPROFILE%\.claude\auto-resume
    .\install.ps1 -Dir D:\tools\autoresume -SkipTests

`install.ps1` needs Python 3.11 or newer on PATH. It installs `psutil` and `tzdata` with `pip --user`, copies the scripts, and creates a Task Scheduler task named `claude-autoresume-detect` that runs every 2 minutes, hidden, as you, without admin rights. Each resume is a one time task named `claude-autoresume-<session>-<reset>`. A later scan deletes it. If your script execution policy blocks it, run `powershell -ExecutionPolicy Bypass -File .\install.ps1`.

Logs and state go to `%LOCALAPPDATA%\auto-resume`. To change the delay or the message, set user environment variables with `setx AUTORESUME_DELAY 300`, then sign out and in. Task Scheduler tasks cannot carry their own variables.

### WSL

WSL 2 with systemd (`[boot]` then `systemd=true` in `/etc/wsl.conf`, then `wsl --shutdown` from Windows) works as Linux. Use `./install.sh` inside the distribution.

Without systemd, the tool has a poll mode with no timer of its own. The scan delivers a resume when it is due, up to about 2 minutes late. Run the scan from a Windows scheduled task, every 2 minutes:

    schtasks /Create /TN claude-autoresume-wsl /SC MINUTE /MO 2 /TR "wsl.exe -e env AUTORESUME_SCHEDULER=poll python3 /home/<you>/.claude/auto-resume/detect.py scan"

Run `./install.sh` inside the distribution first. When it finds no systemd on WSL, it copies the scripts, creates no timer, and prints that exact `schtasks` command with your install path filled in. `AUTORESUME_SCHEDULER=poll` selects the poll mode. It also works on any other platform if a native timer misbehaves.

## Verify

    python3 -I <install dir>/reach.py                # which live sessions can be reached, and by which route
    python3 <install dir>/detect.py status           # pending resumes
    python3 <install dir>/detect.py scan --dry-run   # print decisions, change nothing

On Windows use `python` in place of `python3`. `reach.py` starts with one line that names the platform, the process source and the registry folder. It then prints `OK` or `NO` for each live session. A `NO` means that session would be skipped, and the reason is on the line.

## Uninstall

Linux and macOS:

    <install dir>/uninstall.sh            # remove the detector, cancel pending resumes, delete the install dir
    <install dir>/uninstall.sh --purge    # also delete logs and state

Windows:

    & "<install dir>\uninstall.ps1"           # remove the tasks, delete the install dir
    & "<install dir>\uninstall.ps1" -Purge    # also delete logs and state

Then remove the two settings from `settings.json` if you added them.

## Configuration

* `detect.py scan --tz ZONE`: IANA zone used when a reset text names no zone. The default is the system zone.
* `AUTORESUME_DELAY`: seconds to wait after the reset (default 300).
* `AUTORESUME_MESSAGE`: the text sent to the session.
* `AUTORESUME_SCHEDULER=poll`: no timer, the scan delivers due resumes (see WSL).
* `CLAUDE_CONFIG_DIR`: Claude Code config directory (default `~/.claude`).
* `XDG_DATA_HOME`: logs and state go to `$XDG_DATA_HOME/auto-resume` on any platform when set. The defaults are `~/.local/share/auto-resume` (Linux), `~/Library/Application Support/auto-resume` (macOS) and `%LOCALAPPDATA%\auto-resume` (Windows).
* `install.sh --dir DIR` or `AUTORESUME_DIR`: the install directory.

On Linux, variables that are set when you run `install.sh` are written into the service unit. To change them later, run `systemctl --user edit claude-autoresume-detect.service`. On macOS they are written into the LaunchAgent plist.

## Safety

The only text this tool sends is this message, or your `AUTORESUME_MESSAGE`:

> Usage limit has reset. Continue where you left off: check your running background agents and tasks, resume any that stopped, and carry on with the plan. If you were finished, reply with one status line.

A session is resumed only when all of these hold:

* its transcript's last turn is a usage limit error with a parseable reset;
* its `claude` process is alive and maps to that session;
* it was active before the limit: a user or assistant turn within 30 minutes before the error, or subagent or background task files written in that window. Idle sessions are left alone;
* that error has not been handled before;
* no other resume is already pending near the reset, such as a native `CronCreate` job in that transcript;
* a verified delivery route exists;
* the target time is not more than 90 minutes in the past, and fewer than 8 resumes fired for the session in 24 hours.

At fire time everything is checked again. If the session has moved on, nothing is sent. A delivered message is never retried.

### When only the background agents stop

Sometimes Claude Code lets the main session finish its turn normally (a grace allowance) while its background agents stop on the limit. The main transcript then has no error. The detector also reads each agent transcript in `<session>/subagents/agent-*.jsonl`. If an agent's last record is a usage limit stop with a parseable reset, the main session is idle (no user turn, and no assistant turn more than 10 minutes after the stop), and its process is alive, one resume is scheduled for the main session at the reset plus the delay. The message names the stopped agents, so the session can SendMessage each one. One resume is sent per reset, however many agents stopped. If the session or an agent continues first, nothing is sent. The same stale, native cron and delivery route rules apply.

## Limits

* Subagents are not resumed directly. Only the top level session gets the message, and it decides what to restart.
* The inbox frame format is not a documented API. It may change with a Claude Code update. If it does, the inbox route fails and tmux and the terminal APIs are the fallbacks. `reach.py` shows what works.
* Background activity is inferred from file modification times, because the transcript does not record running tasks.
* Native cron detection reads `CronCreate` calls in the transcript and cannot see whether one was deleted later, so it errs toward skipping.
* On Linux, timers are transient. After a reboot or logout the 2 minute scan recreates any that are still needed. On macOS and Windows the resume jobs are files or tasks that a later scan removes.
* On macOS and Windows the registry's `procStart` value is not compared, because psutil cannot read it in the same form. Process identity is checked by name and by the registry's process id.
* If a companion tool that types a resume into a focused window leaves timer files in `~/.local/share/scheduled-resume`, this tool skips sessions those timers already cover. If the folder does not exist, nothing happens.
* macOS and Windows are beta. They have not run on real machines owned by the author.

## The stale cron lesson

Claude Code's native `CronCreate` jobs live inside the session process. When that process exits, the jobs die with it, and no error is shown. A resume plan that relies only on a `CronCreate` job set before bedtime can silently never fire. That is why this tool runs outside Claude Code, as a job that survives the session. It also reads transcripts for `CronCreate` calls so it does not double resume a session that does have a live job.

## Tests

    python3 -m unittest discover -s tests

The tests use fixture transcripts and a fake `/proc`. Nothing touches your real sessions. Tests that need a POSIX fake `/proc` or Unix sockets are skipped on Windows. Tests for the real `schtasks`, `plutil` and Windows named pipes run only on the OS that has them. GitHub Actions runs the suite on Ubuntu, macOS and Windows with Python 3.11 and 3.12. See `CONTRIBUTING.md`.

## Files

* `detect.py`: scans transcripts, decides, schedules. Commands: `scan [--dry-run]`, `status`.
* `fire.py`: run by the one shot job. Re-verifies, then delivers the message.
* `lib.py`: parsing, process mapping, delivery targets, eligibility.
* `platform_base.py`, `platform_linux.py`, `platform_macos.py`, `platform_windows.py`: the platform layer.
* `peer.py`: post one message to a session inbox by hand.
* `reach.py`: show which live sessions can be reached.
* `ensure.sh`, `ensure.ps1`: SessionStart hooks that keep the detector enabled.
* `install.sh`, `uninstall.sh`, `install.ps1`, `uninstall.ps1`, `systemd/`: installers and the Linux units.

## License

MIT. See `LICENSE`.

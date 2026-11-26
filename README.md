# claude-code-autoresume

Resumes Claude Code sessions that were stopped by a usage limit, at the limit reset plus 5 minutes, without anyone typing "continue".

This is an unofficial community tool. It is not affiliated with or endorsed by Anthropic.

## The problem

When Claude Code hits a usage limit, the session stops and waits. If you leave your desk, long running work sits idle for hours after the limit has already reset. Someone has to come back and type "continue".

## How it works

A systemd user timer runs a plain Python detector every 2 minutes. Claude is not involved in the polling. Claude receives text only when a resume is actually due.

1. The detector scans recent Claude Code transcripts. When a limit stops a turn, Claude Code writes an assistant record with `isApiErrorMessage: true`, `apiError: "usage_limit_reached"` and `apiErrorParams.rate_limit_info.resetsAt` (epoch seconds). The detector reads that. The visible text ("resets 11:20pm (Asia/Kolkata)") is only a fallback.
2. It maps the stopped session to a live `claude` process.
3. It schedules a one-shot systemd timer for the reset plus 5 minutes.
4. At that time `fire.py` checks everything again and sends one message to the session.

### Delivery order

No window focus is ever used. The routes, in order:

1. The session's native inbox socket. Claude Code binds `/run/user/<uid>/cc-socks/<pid>.sock` for each session and records the path in `~/.claude/sessions/<pid>.json` as `messagingSocketPath`. `peer.py` writes the same one line frame that Claude Code's own cross-session `SendMessage` writes. An idle session starts a turn from it. No terminal is involved.
2. tmux, when `TMUX` and `TMUX_PANE` are in the process environment: `send-keys -l` for the text, then `Enter`.
3. Konsole, when `KONSOLE_DBUS_SERVICE` and `KONSOLE_DBUS_SESSION` are present: `sendText` with the message, then a separate `sendText` with a carriage return. Konsole blocks `sendText` by default. Enable "Security sensitive parts of the DBus API" in its settings if you need this route.
4. Anything else: the session is not resumed.

Before sending, the target is verified. The Konsole shell or tmux pane must be an ancestor of the `claude` process, and in Konsole `claude` must be the foreground job.

### Why focused-window typing is never used

A tool that types into "the window with focus" will type into whatever has focus. On Wayland nothing can report which window that is. At 3 a.m. the text could land in a browser, a chat, or a terminal running something else. A resume that goes to the wrong place is worse than a late one, so a session with no verified route is skipped.

## Requirements

* Linux with systemd user sessions (`systemctl --user` works). Other systems are not supported.
* Python 3.11 or newer. There are no third party packages.
* Claude Code, using its default `~/.claude` layout (or `CLAUDE_CONFIG_DIR`).
* For the fallbacks: `tmux`, or Konsole with `qdbus`.

## Install

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

## Verify

    python3 -I <install dir>/reach.py                # which live sessions can be reached, and by which route
    python3 <install dir>/detect.py status           # pending resumes
    python3 <install dir>/detect.py scan --dry-run   # print decisions, change nothing

`reach.py` prints `OK` or `NO` for each live session. A `NO` means that session would be skipped, and the reason is on the line.

## Uninstall

    <install dir>/uninstall.sh            # disable the timer, cancel pending resumes, delete the install dir
    <install dir>/uninstall.sh --purge    # also delete logs and state

Then remove the two settings from `settings.json` if you added them.

## Configuration

* `detect.py scan --tz ZONE`: IANA zone used when a reset text names no zone. The default is the system zone.
* `AUTORESUME_DELAY`: seconds to wait after the reset (default 300).
* `AUTORESUME_MESSAGE`: the text sent to the session.
* `CLAUDE_CONFIG_DIR`: Claude Code config directory (default `~/.claude`).
* `XDG_DATA_HOME`: logs and state go to `$XDG_DATA_HOME/auto-resume` (default `~/.local/share/auto-resume`).
* `install.sh --dir DIR` or `AUTORESUME_DIR`: the install directory.

Variables that are set when you run `install.sh` are written into the service unit. To change them later, run `systemctl --user edit claude-autoresume-detect.service`.

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

## Limits

* Subagents are not resumed. Only the top level session gets the message, and it decides what to restart.
* The inbox socket frame format is not a documented API. It may change with a Claude Code update. If it does, the socket route fails and tmux and Konsole are the fallbacks. `reach.py` shows what works.
* Background activity is inferred from file modification times, because the transcript does not record running tasks.
* Native cron detection reads `CronCreate` calls in the transcript and cannot see whether one was deleted later, so it errs toward skipping.
* Timers are transient. After a reboot or logout the 2 minute scan recreates any that are still needed.
* If a companion tool that types a resume into a focused window leaves timer files in `~/.local/share/scheduled-resume`, this tool skips sessions those timers already cover. If the folder does not exist, nothing happens.

## The stale cron lesson

Claude Code's native `CronCreate` jobs live inside the session process. When that process exits, the jobs die with it, and no error is shown. A resume plan that relies only on a `CronCreate` job set before bedtime can silently never fire. That is why this tool runs outside Claude Code, as a systemd timer that survives the session. It also reads transcripts for `CronCreate` calls so it does not double resume a session that does have a live job.

## Tests

    python3 -m unittest discover -s tests

The tests use fixture transcripts and a mocked `/proc`. Nothing touches your real sessions.

## Files

* `detect.py`: scans transcripts, decides, schedules. Commands: `scan [--dry-run]`, `status`.
* `fire.py`: run by the one-shot timer. Re-verifies, then delivers the message.
* `lib.py`: parsing, process mapping, delivery targets, eligibility.
* `peer.py`: post one message to a session inbox socket by hand.
* `reach.py`: show which live sessions can be reached.
* `ensure.sh`: SessionStart hook that keeps the timer enabled.
* `install.sh`, `uninstall.sh`, `systemd/`: the timer and service units.

## License

MIT. See `LICENSE`.

"""The small platform interface, plus the pieces every platform shares.

One module per platform implements it:

    platform_linux.py     systemd user timers, /proc (psutil when installed)
    platform_macos.py     launchd LaunchAgents, psutil
    platform_windows.py   Task Scheduler (schtasks), psutil, named pipes

A platform object has these members:

    name                    "linux", "macos" or "windows"
    home_dir(environ)       the user's home directory
    default_data_dir(home)  where state and logs live when nothing overrides it
    no_route_reason         text for a session with no delivery route
    proc_source(paths)      a process source (see below)
    Scheduler()             schedules the one-shot resume; see PollScheduler

A process source answers questions about running processes. Every method
returns None, an empty value, or an empty list for a process that is gone:

    pids()                  all process ids
    stat(pid)               (ppid, pgrp, start_token) or None
    name(pid)               executable name
    cmdline(pid)            argument list
    cwd(pid)                working directory or ""
    environ(pid)            environment as a dict
    open_files(pid)         paths the process holds open
    start_epoch(pid)        start time in epoch seconds or None

A scheduler has schedule(unit, fire_at, event_id, runner), active(unit, runner),
cancel(unit, runner), and the flags needs_cleanup and polls.
"""

import os
import sys
from pathlib import Path

DEFAULT_PROC = Path("/proc")

PSUTIL_HINT = ("psutil is required on this platform. Install it with: "
               "python3 -m pip install --user psutil")


class PsutilSource:
    """Process source built on psutil. `token_source` (optional) supplies the exact
    start token and open file list from /proc on Linux, so the registry's
    `procStart` check and the tasks directory check keep working there."""

    def __init__(self, psutil_module=None, token_source=None):
        if psutil_module is None:
            try:
                import psutil as psutil_module
            except ImportError as e:
                raise ImportError(PSUTIL_HINT) from e
        self.ps = psutil_module
        self.token_source = token_source

    def _proc(self, pid):
        try:
            return self.ps.Process(pid)
        except Exception:
            return None

    def _call(self, pid, method, default):
        p = self._proc(pid)
        if p is None:
            return default
        try:
            return getattr(p, method)()
        except Exception:
            return default

    def pids(self):
        try:
            return list(self.ps.pids())
        except Exception:
            return []

    def stat(self, pid):
        ppid = self._call(pid, "ppid", None)
        if ppid is None:
            return None
        try:
            pgrp = os.getpgid(pid)
        except (AttributeError, OSError):
            pgrp = pid
        token = None
        if self.token_source is not None:
            exact = self.token_source.stat(pid)
            token = exact[2] if exact else None
        return ppid, pgrp, token

    def name(self, pid):
        return self._call(pid, "name", "") or ""

    def cmdline(self, pid):
        return list(self._call(pid, "cmdline", []) or [])

    def cwd(self, pid):
        return self._call(pid, "cwd", "") or ""

    def environ(self, pid):
        return dict(self._call(pid, "environ", {}) or {})

    def open_files(self, pid):
        if self.token_source is not None:
            return self.token_source.open_files(pid)
        files = self._call(pid, "open_files", []) or []
        return [getattr(f, "path", str(f)) for f in files]

    def start_epoch(self, pid):
        return self._call(pid, "create_time", None)


class PollScheduler:
    """No timer at all. `schedule` only records the event, and the 2 minute scan
    delivers it once it is due (up to about 2 minutes late). Used where there is no
    timer service, such as WSL without systemd. Select it with AUTORESUME_SCHEDULER=poll."""

    needs_cleanup = False
    polls = True

    def schedule(self, unit, fire_at, event_id, runner=None):
        return True, ""

    def active(self, unit, runner=None):
        return True

    def cancel(self, unit, runner=None):
        return True


def current(name=None):
    """The platform object for this machine. AUTORESUME_PLATFORM overrides it
    (linux, macos, windows), which the tests use."""
    name = (name or os.environ.get("AUTORESUME_PLATFORM") or sys.platform).lower()
    if name.startswith(("win", "cygwin")):
        import platform_windows
        return platform_windows.PLATFORM
    if name in ("darwin", "macos", "mac"):
        import platform_macos
        return platform_macos.PLATFORM
    import platform_linux
    return platform_linux.PLATFORM

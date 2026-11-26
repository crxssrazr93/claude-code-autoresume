"""Linux (and WSL): systemd user timers, /proc, Konsole D-Bus."""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import lib
import platform_base
from platform_base import DEFAULT_PROC, PollScheduler, PsutilSource

HERE = Path(__file__).resolve().parent


# ------------------------------------------------------------------ processes --
def _read(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


class ProcfsSource:
    """Process source that reads a /proc directory. It needs no packages. The tests
    point it at a fake /proc."""

    def __init__(self, root=DEFAULT_PROC):
        self.root = Path(root)

    def pids(self):
        if not self.root.is_dir():
            return []
        return [int(e.name) for e in self.root.iterdir() if e.name.isdigit()]

    def stat(self, pid):
        raw = _read(self.root / str(pid) / "stat").decode("utf-8", "replace")
        if not raw:
            return None
        rest = raw[raw.rfind(")") + 2:].split()
        try:
            return int(rest[1]), int(rest[2]), rest[19]
        except (IndexError, ValueError):
            return None

    def name(self, pid):
        return _read(self.root / str(pid) / "comm").decode("utf-8", "replace").strip()

    def cmdline(self, pid):
        raw = _read(self.root / str(pid) / "cmdline").split(b"\0")
        return [c.decode("utf-8", "replace") for c in raw if c]

    def cwd(self, pid):
        try:
            return os.readlink(self.root / str(pid) / "cwd")
        except OSError:
            return ""

    def environ(self, pid):
        env = {}
        for item in _read(self.root / str(pid) / "environ").split(b"\0"):
            k, _, v = item.partition(b"=")
            if k:
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        return env

    def open_files(self, pid):
        out = []
        try:
            for fd in (self.root / str(pid) / "fd").iterdir():
                try:
                    out.append(os.readlink(fd))
                except OSError:
                    continue
        except OSError:
            pass
        return out

    def start_epoch(self, pid):
        try:
            return (self.root / str(pid)).stat().st_ctime
        except OSError:
            return None


def is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


# ------------------------------------------------------------------- scheduling --
class SystemdScheduler:
    needs_cleanup = False  # transient timers remove themselves
    polls = False

    def schedule(self, unit, fire_at, event_id, runner=lib.run):
        when = datetime.fromtimestamp(fire_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        cmd = ["systemd-run", "--user", "--quiet", f"--unit={unit}",
               f"--description=auto-resume {event_id}",
               f"--on-calendar={when}",
               "--timer-property=AccuracySec=1s",
               "--timer-property=RemainAfterElapse=no",
               *[f"--setenv={k}={os.environ[k]}" for k in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "AUTORESUME_MESSAGE") if k in os.environ],
               sys.executable, str(HERE / "fire.py"), event_id]
        rc, _, err = runner(cmd)
        return rc == 0, err

    def active(self, unit, runner=lib.run):
        rc, out, _ = runner(["systemctl", "--user", "is-active", f"{unit}.timer"])
        return out == "active"

    def cancel(self, unit, runner=lib.run):
        return True


def systemd_user_available(runner=lib.run):
    rc, _, _ = runner(["systemctl", "--user", "show-environment"])
    return rc == 0


def make_scheduler(environ=None, wsl=None, runner=lib.run):
    """systemd by default. On WSL without systemd, or when AUTORESUME_SCHEDULER=poll,
    the scan itself delivers due resumes."""
    environ = os.environ if environ is None else environ
    choice = environ.get("AUTORESUME_SCHEDULER", "").lower()
    if choice == "poll":
        return PollScheduler()
    if choice == "systemd":
        return SystemdScheduler()
    wsl = is_wsl() if wsl is None else wsl
    if wsl and not systemd_user_available(runner):
        return PollScheduler()
    return SystemdScheduler()


# ---------------------------------------------------------------------- Konsole --
def _qdbus():
    for name in ("qdbus6", "qdbus", "qdbus-qt6"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if os.access(os.path.join(d, name), os.X_OK):
                return os.path.join(d, name)
    return "qdbus"


def konsole_verify(paths, pid, target, runner=lib.run):
    chain = lib.ancestors(paths, pid)
    q = _qdbus()
    if not q:
        return False, "qdbus not found"
    rc, out, err = runner([q, target["service"], target["session"],
                           "org.kde.konsole.Session.processId"])
    if rc != 0 or not out.isdigit():
        return False, f"konsole session unreachable: {err or out}"
    if int(out) not in chain:
        return False, f"konsole shell pid {out} is not an ancestor of claude {pid}"
    rc, fg, err = runner([q, target["service"], target["session"],
                          "org.kde.konsole.Session.foregroundProcessId"])
    st = lib.proc_stat(paths, pid)
    if rc == 0 and fg.isdigit() and st and int(fg) != st[1] and int(fg) != pid:
        return False, f"claude is not the foreground job (foreground={fg})"
    # An empty sendText types nothing, but is refused when Konsole's
    # "security sensitive DBus API" setting is off, so it is a safe probe.
    rc, _, err = runner([q, target["service"], target["session"],
                         "org.kde.konsole.Session.sendText", ""])
    if rc != 0:
        if "ecurity" in err or "AccessDenied" in err:
            return False, ("Konsole refuses sendText: enable 'security sensitive DBus API' "
                           "in Konsole settings (Configure Konsole, General)")
        return False, f"konsole sendText probe failed: {err}"
    return True, f"konsole shell {out}"


def konsole_send(target, message, runner=lib.run, pause=0.7):
    q = _qdbus()
    base = [q, target["service"], target["session"], "org.kde.konsole.Session.sendText"]
    rc, _, err = runner(base + [message])
    if rc != 0:
        return False, err
    if pause:
        time.sleep(pause)
    rc, _, err = runner(base + ["\r"])
    return rc == 0, err


# --------------------------------------------------------------------- platform --
class LinuxPlatform:
    name = "linux"
    no_route_reason = ("no inbox socket and no addressable terminal (not Konsole or tmux); "
                       "focused-window injection is never used")

    def home_dir(self, environ=None):
        return Path.home()

    def default_data_dir(self, home):
        return Path(home) / ".local/share/auto-resume"

    def proc_source(self, paths):
        procfs = ProcfsSource(paths.proc)
        if paths.proc != DEFAULT_PROC:
            return procfs  # a fake or custom /proc (tests)
        try:
            return PsutilSource(token_source=procfs)
        except ImportError:
            return procfs

    def Scheduler(self):
        return make_scheduler()

    def process_source_name(self, paths):
        return type(self.proc_source(paths)).__name__


PLATFORM = LinuxPlatform()

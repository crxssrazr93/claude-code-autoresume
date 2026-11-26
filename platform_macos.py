"""macOS (beta): launchd LaunchAgents, psutil, iTerm2 Python API.

Delivery order on macOS: the session inbox socket (a Unix socket, same as Linux),
then tmux, then a targeted iTerm2 session. Terminal.app is not supported, see the
README for the reason.

    python3 platform_macos.py detect-plist --python PY --dir DIR [--out FILE]
"""

import argparse
import os
import plistlib
import sys
from datetime import datetime
from pathlib import Path

import lib
from platform_base import DEFAULT_PROC, PollScheduler, PsutilSource

HERE = Path(__file__).resolve().parent
LABEL_PREFIX = "com.claude-code-autoresume"
DETECT_LABEL = f"{LABEL_PREFIX}.detect"
# launchd starts jobs with a minimal PATH. tmux usually lives in Homebrew's prefix.
JOB_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PASS_THROUGH = ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME", "AUTORESUME_DELAY", "AUTORESUME_MESSAGE")


def job_environment(environ=None):
    environ = os.environ if environ is None else environ
    env = {"PATH": JOB_PATH}
    env.update({k: environ[k] for k in PASS_THROUGH if k in environ})
    return env


def detect_plist(python, install_dir, log_path, environ=None):
    """LaunchAgent that runs `detect.py scan` every 120 seconds and at login."""
    return plistlib.dumps({
        "Label": DETECT_LABEL,
        "ProgramArguments": [str(python), str(Path(install_dir) / "detect.py"), "scan"],
        "StartInterval": 120,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "Nice": 10,
        "EnvironmentVariables": job_environment(environ),
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    })


def fire_plist(label, python, fire_script, event_id, fire_at, environ=None):
    """One-shot LaunchAgent. launchd has no true one-time trigger, so this fires at a
    calendar time (it would repeat yearly). detect.py removes it after fire.py ran."""
    when = datetime.fromtimestamp(fire_at)
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(python), str(fire_script), event_id],
        "StartCalendarInterval": {"Month": when.month, "Day": when.day,
                                  "Hour": when.hour, "Minute": when.minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": job_environment(environ),
    })


class LaunchdScheduler:
    needs_cleanup = True
    polls = False

    def __init__(self, agents_dir=None, uid=None):
        self.agents_dir = Path(agents_dir) if agents_dir else Path.home() / "Library/LaunchAgents"
        self.uid = os.getuid() if uid is None else uid

    def label(self, unit):
        return f"{LABEL_PREFIX}.{unit}"

    def plist_path(self, unit):
        return self.agents_dir / f"{self.label(unit)}.plist"

    def domain(self):
        return f"gui/{self.uid}"

    def schedule(self, unit, fire_at, event_id, runner=lib.run):
        path = self.plist_path(unit)
        try:
            self.agents_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fire_plist(self.label(unit), sys.executable, HERE / "fire.py",
                                        event_id, fire_at))
        except OSError as e:
            return False, str(e)
        runner(["launchctl", "bootout", f"{self.domain()}/{self.label(unit)}"])  # replace a stale copy
        rc, _, err = runner(["launchctl", "bootstrap", self.domain(), str(path)])
        return rc == 0, err

    def active(self, unit, runner=lib.run):
        rc, _, _ = runner(["launchctl", "print", f"{self.domain()}/{self.label(unit)}"])
        return rc == 0

    def cancel(self, unit, runner=lib.run):
        runner(["launchctl", "bootout", f"{self.domain()}/{self.label(unit)}"])
        try:
            self.plist_path(unit).unlink()
        except OSError:
            pass
        return True


# ----------------------------------------------------------------------- iTerm2 --
def iterm2_session_id(env):
    """ITERM_SESSION_ID looks like 'w0t0p0:<uuid>'. The uuid names one session."""
    raw = env.get("ITERM_SESSION_ID", "")
    return raw.split(":", 1)[1] if ":" in raw else ""


def _run_iterm2(work):
    """Run `work(iterm2, connection, app)` against the iTerm2 Python API. Returns
    (ok, detail). Needs the `iterm2` package and Settings > General > Magic >
    'Enable Python API' in iTerm2."""
    try:
        import asyncio
        import iterm2
    except ImportError:
        return False, "iTerm2 Python API not installed: python3 -m pip install --user iterm2"
    out = {}

    async def main():
        connection = await iterm2.Connection.async_create()
        app = await iterm2.async_get_app(connection)
        out["result"] = await work(iterm2, connection, app)

    try:
        asyncio.run(main())
    except Exception as e:
        return False, f"iTerm2 API unreachable (is the Python API enabled?): {e}"
    return out.get("result", (False, "no result"))


def iterm2_verify(paths, pid, target, runner=None):
    chain = lib.ancestors(paths, pid)

    async def work(iterm2, connection, app):
        session = app.get_session_by_id(target["session"])
        if session is None:
            return False, "iTerm2 has no session with that id"
        shell = await session.async_get_variable("pid")
        if not shell or int(shell) not in chain:
            return False, f"iTerm2 session shell pid {shell} is not an ancestor of claude {pid}"
        return True, f"iTerm2 session {target['session'][:8]}"

    return _run_iterm2(work)


def iterm2_send(target, message, runner=None, pause=0.7):
    async def work(iterm2, connection, app):
        import asyncio
        session = app.get_session_by_id(target["session"])
        if session is None:
            return False, "iTerm2 has no session with that id"
        await session.async_send_text(message)
        if pause:
            await asyncio.sleep(pause)
        await session.async_send_text("\r")
        return True, ""

    return _run_iterm2(work)


# --------------------------------------------------------------------- platform --
class MacPlatform:
    name = "macos"
    no_route_reason = ("no inbox socket and no addressable terminal (not tmux or iTerm2); "
                       "focused-window injection is never used")

    def home_dir(self, environ=None):
        return Path.home()

    def default_data_dir(self, home):
        return Path(home) / "Library/Application Support/auto-resume"

    def proc_source(self, paths):
        if paths.proc != DEFAULT_PROC:
            from platform_linux import ProcfsSource
            return ProcfsSource(paths.proc)  # fake /proc in tests
        return PsutilSource()

    def Scheduler(self):
        if os.environ.get("AUTORESUME_SCHEDULER", "").lower() == "poll":
            return PollScheduler()
        return LaunchdScheduler()


PLATFORM = MacPlatform()


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("detect-plist", help="print the detector LaunchAgent plist")
    p.add_argument("--python", required=True)
    p.add_argument("--dir", required=True, help="install directory")
    p.add_argument("--log", help="log file (default: <data dir>/launchd.log)")
    p.add_argument("--out", help="write here instead of stdout")
    args = ap.parse_args(argv)
    log = args.log or str(lib.Paths.from_env().data / "launchd.log")
    data = detect_plist(args.python, args.dir, log)
    if args.out:
        Path(args.out).write_bytes(data)
    else:
        sys.stdout.write(data.decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())

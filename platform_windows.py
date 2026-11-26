"""Windows (beta): Task Scheduler (schtasks), psutil, named pipe delivery.

Delivery on Windows: the session inbox path from the registry
(`messagingSocketPath`). When it is a named pipe (`\\\\.\\pipe\\...`) the same one line
frame is written to it with plain binary file I/O. When there is no usable path,
nothing is delivered and the log says so: Claude Code's own
`autoContinueAtUsageLimit` setting handles the resume.

    python platform_windows.py detect-task-xml --python PY --dir DIR [--out FILE]
"""

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import lib
from platform_base import DEFAULT_PROC, PollScheduler, PsutilSource

HERE = Path(__file__).resolve().parent
TASK_PREFIX = "claude-autoresume"
DETECT_TASK = f"{TASK_PREFIX}-detect"
PIPE_PREFIXES = ("\\\\.\\pipe\\", "//./pipe/", "\\\\?\\pipe\\")


# ------------------------------------------------------------------ named pipes --
def is_pipe_path(path):
    return isinstance(path, str) and path.lower().startswith(PIPE_PREFIXES)


def send_pipe(path, data, opener=open):
    """Write `data` (bytes) to a named pipe by opening it as a binary file.
    Returns (ok, detail)."""
    last = None
    for mode in ("r+b", "wb"):  # a duplex pipe opens read/write, an inbound only pipe needs write
        try:
            with opener(path, mode, buffering=0) as f:
                f.write(data)
            return True, f"frame written to {path}"
        except OSError as e:
            last = e
    return False, f"inbox pipe unreachable: {last}"


# -------------------------------------------------------------------- scheduling --
def windowless(python):
    """pythonw.exe has no console window, so a task that runs every 2 minutes
    does not flash one. Falls back to the given interpreter."""
    p = Path(python)
    if p.name.lower() == "python.exe":
        w = p.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(python)


def _stamp(dt):
    """Local time with its UTC offset, which Task Scheduler accepts and which does not
    depend on the machine's date format."""
    return dt.astimezone().isoformat(timespec="seconds")


def quote(arg):
    return '"' + str(arg).replace('"', '\\"') + '"'


def task_xml(trigger, command, arguments, workdir, description):
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f"  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>\n"
        f"  <Triggers>{trigger}</Triggers>\n"
        "  <Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType>"
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        f"    <Exec><Command>{escape(str(command))}</Command>"
        f"<Arguments>{escape(arguments)}</Arguments>"
        f"<WorkingDirectory>{escape(str(workdir))}</WorkingDirectory></Exec>\n"
        "  </Actions>\n"
        "</Task>\n")


def detect_task_xml(python, install_dir, now=None):
    """Repeating task: `detect.py scan` every 2 minutes, forever."""
    now = now or datetime.now()
    trigger = ("<TimeTrigger><Repetition><Interval>PT2M</Interval></Repetition>"
               f"<StartBoundary>{_stamp(now)}</StartBoundary><Enabled>true</Enabled></TimeTrigger>")
    return task_xml(trigger, windowless(python), quote(Path(install_dir) / "detect.py") + " scan",
                    install_dir, "Claude Code auto-resume detector, every 2 minutes")


def fire_task_xml(python, fire_script, event_id, fire_at):
    """One-time task: `fire.py <event>` at fire_at (epoch seconds)."""
    start = datetime.fromtimestamp(fire_at)
    trigger = (f"<TimeTrigger><StartBoundary>{_stamp(start)}</StartBoundary>"
               f"<EndBoundary>{_stamp(start + timedelta(hours=2))}</EndBoundary>"
               "<Enabled>true</Enabled></TimeTrigger>")
    return task_xml(trigger, windowless(python), f"{quote(fire_script)} {event_id}",
                    Path(fire_script).parent, f"Claude Code auto-resume {event_id}")


class SchtasksScheduler:
    needs_cleanup = True
    polls = False

    def schedule(self, unit, fire_at, event_id, runner=lib.run):
        xml = fire_task_xml(sys.executable, HERE / "fire.py", event_id, fire_at)
        fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="autoresume-")
        os.close(fd)
        try:
            Path(tmp).write_text(xml, encoding="utf-16")
            rc, out, err = runner(["schtasks", "/Create", "/TN", unit, "/XML", tmp, "/F"])
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return rc == 0, err or out if rc != 0 else ""

    def active(self, unit, runner=lib.run):
        rc, _, _ = runner(["schtasks", "/Query", "/TN", unit])
        return rc == 0

    def cancel(self, unit, runner=lib.run):
        runner(["schtasks", "/Delete", "/TN", unit, "/F"])
        return True


# --------------------------------------------------------------------- platform --
class WindowsPlatform:
    name = "windows"
    no_route_reason = ("no usable inbox pipe in the session registry; nothing was delivered. "
                       "Claude Code's own autoContinueAtUsageLimit setting handles the resume")

    def home_dir(self, environ=None):
        environ = os.environ if environ is None else environ
        profile = environ.get("USERPROFILE")
        return Path(profile) if profile else Path.home()

    def default_data_dir(self, home):
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path(home) / "AppData/Local") / "auto-resume"

    def proc_source(self, paths):
        if paths.proc != DEFAULT_PROC:
            from platform_linux import ProcfsSource
            return ProcfsSource(paths.proc)  # fake /proc in tests
        return PsutilSource()

    def Scheduler(self):
        if os.environ.get("AUTORESUME_SCHEDULER", "").lower() == "poll":
            return PollScheduler()
        return SchtasksScheduler()


PLATFORM = WindowsPlatform()


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("detect-task-xml", help="print the detector task definition")
    p.add_argument("--python", required=True)
    p.add_argument("--dir", required=True, help="install directory")
    p.add_argument("--out", help="write here (UTF-16, as schtasks needs) instead of stdout")
    args = ap.parse_args(argv)
    xml = detect_task_xml(args.python, args.dir)
    if args.out:
        Path(args.out).write_text(xml, encoding="utf-16")
    else:
        sys.stdout.write(xml)
    return 0


if __name__ == "__main__":
    sys.exit(main())

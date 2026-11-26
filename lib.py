"""Shared logic for the auto-resume system. Pure functions where possible so the
tests can drive them with fixture transcripts and a mocked /proc."""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

MESSAGE = os.environ.get("AUTORESUME_MESSAGE") or (
    "Usage limit has reset. Continue where you left off: check your running "
    "background agents and tasks, resume any that stopped, and carry on with the "
    "plan. If you were finished, reply with one status line."
)


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


DELAY_AFTER_RESET = _env_int("AUTORESUME_DELAY", 300)  # seconds after the reset
ACTIVE_WINDOW = 30 * 60  # seconds before the error that count as "was working"
CONFLICT_BEFORE = 15 * 60  # other resumes this long before the reset count
CONFLICT_AFTER = DELAY_AFTER_RESET + 15 * 60  # and this long after the reset
STALE_AFTER = 90 * 60  # past the target by more than this: leave it alone
MAX_FIRES_PER_DAY = 8


# ------------------------------------------------------------------ paths --
@dataclass
class Paths:
    home: Path = field(default_factory=Path.home)
    proc: Path = Path("/proc")
    claude_dir: Path = None  # defaults to <home>/.claude
    data_dir: Path = None  # defaults to <home>/.local/share/auto-resume

    @classmethod
    def from_env(cls):
        """Real install: honour CLAUDE_CONFIG_DIR and XDG_DATA_HOME when set. The
        home directory comes from the platform (USERPROFILE on Windows)."""
        claude = os.environ.get("CLAUDE_CONFIG_DIR")
        xdg = os.environ.get("XDG_DATA_HOME")
        return cls(home=current_platform().home_dir(os.environ),
                   claude_dir=Path(claude) if claude else None,
                   data_dir=Path(xdg) / "auto-resume" if xdg else None)

    @property
    def claude(self):
        return self.claude_dir or self.home / ".claude"

    @property
    def projects(self):
        return self.claude / "projects"

    @property
    def sessions(self):
        return self.claude / "sessions"

    @property
    def data(self):
        return self.data_dir or current_platform().default_data_dir(self.home)

    @property
    def state_file(self):
        return self.data / "state.json"

    @property
    def log_file(self):
        return self.data / "auto-resume.log"

    @property
    def scheduled_resume(self):
        return self.home / ".local/share/scheduled-resume"


# ---------------------------------------------------------------- reset time --
_RESET_RE = re.compile(
    r"resets?\s+(?:(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)\s*(?:\((?P<tz>[^)]+)\))?",
    re.I,
)
_PIPE_RE = re.compile(r"limit reached\|(\d{9,11})", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_reset(text, ref_epoch, default_tz=None):
    """Return the reset as epoch seconds, or None.

    Handles the legacy 'limit reached|<epoch>' form, 'resets 9:40pm (Zone)',
    and 'resets Nov 28, 3pm'. A bare clock time that is not after the moment of
    the error rolls to the next day (reset after midnight)."""
    m = _PIPE_RE.search(text or "")
    if m:
        return int(m.group(1))
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    tz = default_tz or datetime.now().astimezone().tzinfo
    if m.group("tz") and ZoneInfo:
        try:
            tz = ZoneInfo(m.group("tz").strip())
        except Exception:
            pass
    ref = datetime.fromtimestamp(ref_epoch, tz)
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    if m.group("mon"):
        mon = _MONTHS.get(m.group("mon")[:3].lower())
        if not mon:
            return None
        cand = ref.replace(month=mon, day=int(m.group("day")), hour=hour,
                           minute=minute, second=0, microsecond=0)
        if cand < ref - timedelta(days=1):
            cand = cand.replace(year=cand.year + 1)
    else:
        cand = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= ref:
            cand += timedelta(days=1)
    return int(cand.timestamp())


def parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------- transcripts --
def iter_records_reverse(path, chunk=256 * 1024):
    """Yield parsed JSON records from the end of a JSONL file backwards."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            buf = lines[0]
            for line in reversed(lines[1:]):
                rec = _loads(line)
                if rec is not None:
                    yield rec
        rec = _loads(buf)
        if rec is not None:
            yield rec


def _loads(line):
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _text_of(rec):
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


def limit_event(rec, default_tz=None):
    """If rec is an assistant usage-limit error, describe it, else None."""
    if rec.get("type") != "assistant" or not rec.get("isApiErrorMessage"):
        return None
    text = _text_of(rec)
    is_limit = rec.get("apiError") == "usage_limit_reached" or bool(
        re.search(r"(hit your|usage).{0,30}limit", text, re.I))
    if not is_limit:
        return None
    try:
        ts = parse_iso(rec["timestamp"])
    except Exception:
        return None
    info = (rec.get("apiErrorParams") or {}).get("rate_limit_info") or {}
    reset = info.get("resetsAt") or (rec.get("quotaLimits") or {}).get("resetsAt")
    source = "resetsAt"
    if not reset:
        reset = parse_reset(text, ts, default_tz)
        source = "text"
    if not reset:
        return None
    return {
        "event_id": rec.get("uuid"),
        "ts": ts,
        "reset": int(reset),
        "reset_source": source,
        "limit_type": info.get("rateLimitType") or "unknown",
        "text": text,
    }


def is_turn(rec):
    return rec.get("type") in ("user", "assistant") and not rec.get("isSidechain")


def analyze_transcript(path, default_tz=None):
    """Return (event, prev_turn_ts) when the last turn record is a limit error,
    (None, None) otherwise. prev_turn_ts is the last real turn before it."""
    event = None
    for rec in iter_records_reverse(path):
        if not is_turn(rec):
            continue
        if event is None:
            event = limit_event(rec, default_tz)
            if event is None:
                return None, None
            continue
        if rec.get("isApiErrorMessage"):
            continue
        try:
            return event, parse_iso(rec["timestamp"])
        except Exception:
            continue
    return (event, None) if event else (None, None)


def last_turn_event_id(path):
    for rec in iter_records_reverse(path):
        if is_turn(rec):
            return rec.get("uuid")
    return None


def background_activity_ts(paths, cwd_slug, session_id):
    """Newest mtime among this session's subagent transcripts and background
    task output files (None if there are none)."""
    newest = None
    roots = [paths.projects / cwd_slug / session_id / "subagents"]
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover
        uid = 0
    roots.append(Path(f"/tmp/claude-{uid}") / cwd_slug / session_id / "tasks")
    if sys.platform != "linux":  # macOS and Windows keep temp files elsewhere
        import tempfile
        roots.append(Path(tempfile.gettempdir()) / f"claude-{uid}" / cwd_slug / session_id / "tasks")
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _, files in os.walk(root):
            for name in files:
                try:
                    m = os.stat(os.path.join(dirpath, name)).st_mtime
                except OSError:
                    continue
                newest = m if newest is None or m > newest else newest
    return newest


# ------------------------------------------------------------------ processes --
@dataclass
class Proc:
    pid: int
    session_id: str
    cwd: str
    how: str


DEFAULT_PROC = Path("/proc")


def current_platform():
    """The platform object (see platform_base.py)."""
    import platform_base
    return platform_base.current()


def proc_source(paths):
    """Where process facts come from: psutil, or /proc (always, for a fake /proc)."""
    return current_platform().proc_source(paths)


def exe_name(name):
    """'C:\\x\\Claude.EXE' -> 'claude'."""
    n = re.split(r"[\\/]", name or "")[-1].lower()
    return n[:-4] if n.endswith(".exe") else n


def proc_stat(paths, pid, source=None):
    """Return (ppid, pgrp, starttime) or None."""
    return (source or proc_source(paths)).stat(pid)


def proc_is_claude(paths, pid, source=None):
    comm = exe_name((source or proc_source(paths)).name(pid))
    return comm in ("claude", "node") or comm.startswith("claude")


def proc_environ(paths, pid, source=None):
    return (source or proc_source(paths)).environ(pid)


def ancestors(paths, pid, source=None):
    source = source or proc_source(paths)
    seen = []
    while pid and pid > 1 and pid not in seen and len(seen) < 64:
        seen.append(pid)
        st = source.stat(pid)
        if not st:
            break
        pid = st[0]
    return seen


def find_claude_processes(paths):
    """Map session id -> Proc for live claude processes.

    Order of evidence: the session registry (`sessions/<pid>.json` in the Claude
    config directory), validated against the process start time where the platform
    can tell it, a `--resume <uuid>` argument, a tasks directory held open as a file
    descriptor, and finally a cwd that matches exactly one transcript project with a
    recently written transcript."""
    source = proc_source(paths)
    found = {}
    # 1. session registry written by Claude Code itself
    if paths.sessions.is_dir():
        for f in paths.sessions.glob("*.json"):
            try:
                d = json.loads(f.read_text())
                pid = int(d["pid"])
                sid = d["sessionId"]
            except Exception:
                continue
            st = source.stat(pid)
            if not st or not proc_is_claude(paths, pid, source):
                continue
            if d.get("procStart") and st[2] is not None and str(d["procStart"]) != st[2]:
                continue  # pid was reused by something else
            found[sid] = Proc(pid, sid, d.get("cwd", ""), "sessions-registry")
    known_pids = {p.pid for p in found.values()}
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for pid in source.pids():
        if pid in known_pids or not proc_is_claude(paths, pid, source):
            continue
        cmd = [c for c in source.cmdline(pid) if c]
        if not cmd or exe_name(cmd[0]) != "claude" or "--chrome-native-host" in cmd:
            continue
        cwd = source.cwd(pid)
        sid = None
        how = ""
        for i, a in enumerate(cmd):
            if a in ("--resume", "-r", "--session-id") and i + 1 < len(cmd) \
                    and uuid_re.fullmatch(cmd[i + 1]):
                sid, how = cmd[i + 1], "cmdline"
        if not sid:
            for target in source.open_files(pid):
                m = re.search(r"/claude-\d+/[^/]+/(" + uuid_re.pattern + r")/tasks", target)
                if m:
                    sid, how = m.group(1), "fd-tasks"
                    break
        if not sid and cwd:
            slug = cwd_slug(cwd)
            proj = paths.projects / slug
            if proj.is_dir():
                files = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                if files:
                    sid, how = files[0].stem, "cwd-latest-transcript"
        if sid and sid not in found:
            found[sid] = Proc(pid, sid, cwd, how)
    return found


def describe_environment(paths):
    """One line for bug reports: platform, process source, registry directory."""
    plat = current_platform()
    try:
        src = type(plat.proc_source(paths)).__name__
    except ImportError as e:
        src = f"unavailable ({e})"
    return (f"platform={plat.name} python={sys.version.split()[0]} process-source={src} "
            f"registry={paths.sessions} data={paths.data}")


def try_lock(fileobj):
    """Take a non blocking exclusive lock on an open file. False when another
    process holds it."""
    try:
        if os.name == "nt":
            import msvcrt
            fileobj.seek(0)
            msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fileobj, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def cwd_slug(cwd):
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


# ------------------------------------------------------- native peer message --
# Claude Code binds an inbox socket per session (documented under "The session's
# inbox socket" in the cross-session messaging docs). The same frame that
# SendMessage writes can be written by a plain script, so no Claude process is
# needed to deliver it. An idle receiver starts its own turn from it.
PEER_FROM = "auto-resume"


def peer_socket(paths, session_id):
    """Inbox socket path of a live session, from the registry Claude Code writes."""
    if not paths.sessions.is_dir():
        return None
    for f in paths.sessions.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("sessionId") == session_id and d.get("messagingSocketPath"):
            return d["messagingSocketPath"]
    return None


def permission_mode(transcript):
    """Last permission mode recorded in a transcript (e.g. bypassPermissions)."""
    if not transcript:
        return None
    for rec in iter_records_reverse(transcript):
        if rec.get("type") == "permission-mode":
            return rec.get("permissionMode")
    return None


def peer_frame(text, name=PEER_FROM, mode="bypass", msg_id=None):
    """`mode` is the sender's claimed permission class. A receiver that bypasses
    permission prompts only accepts a message from a sender claiming bypass, and
    a prompting receiver holds a message that claims bypass, so match the target
    and leave the attribute out (None) for a prompting target."""
    import uuid
    attr = f' from-mode="{mode}"' if mode else ""
    content = (f'<cross-session-message from="{name}" from-name="{name}"{attr}>\n'
               f"{text}\n</cross-session-message>")
    return json.dumps({"msgV": 1, "msg_id": msg_id or str(uuid.uuid4()), "type": "user",
                       "message": {"role": "user", "content": content},
                       "priority": "next", "from": name}) + "\n"


def send_peer(sock_path, text, name=PEER_FROM, timeout=5, mode="bypass", opener=open):
    """Write one message frame to a session inbox. Returns (ok, detail).

    The path comes from the session registry. A Unix socket path is used on Linux
    and macOS. A Windows named pipe path (\\\\.\\pipe\\...) gets the same frame
    through plain binary file I/O."""
    import socket
    frame = peer_frame(text, name, mode).encode()
    import platform_windows
    if platform_windows.is_pipe_path(sock_path):
        return platform_windows.send_pipe(sock_path, frame, opener)
    if not hasattr(socket, "AF_UNIX"):
        return False, f"no Unix socket support on this platform, cannot use {sock_path}"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(frame)
        return True, f"frame written to {sock_path}"
    except OSError as e:
        return False, f"inbox socket unreachable: {e}"
    finally:
        s.close()


# ------------------------------------------------------------------- terminals --
def candidate_targets(paths, session_id, pid, transcript=None):
    """Delivery routes in preference order: the session's own inbox socket or pipe
    (works while idle, needs no terminal), then tmux, then a terminal API (Konsole
    D-Bus on Linux, the iTerm2 Python API on macOS)."""
    out = []
    sock = peer_socket(paths, session_id)
    if sock:
        mode = "bypass" if permission_mode(transcript) == "bypassPermissions" else None
        out.append({"kind": "peer", "socket": sock, "mode": mode})
    term = terminal_target(paths, pid)
    if term:
        out.append(term)
    return out


def terminal_target(paths, pid):
    """Describe how to reach the terminal of claude process `pid`, or None.
    Never relies on window focus."""
    env = proc_environ(paths, pid)
    if env.get("TMUX") and env.get("TMUX_PANE"):
        return {"kind": "tmux", "socket": env["TMUX"].split(",")[0], "pane": env["TMUX_PANE"]}
    if env.get("ITERM_SESSION_ID") and ":" in env["ITERM_SESSION_ID"]:
        return {"kind": "iterm2", "session": env["ITERM_SESSION_ID"].split(":", 1)[1]}
    if env.get("KONSOLE_DBUS_SERVICE") and env.get("KONSOLE_DBUS_SESSION"):
        return {"kind": "konsole", "service": env["KONSOLE_DBUS_SERVICE"],
                "session": env["KONSOLE_DBUS_SESSION"]}
    return None


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def verify_target(paths, pid, target, runner=run):
    """Confirm the target terminal really hosts claude process `pid`.
    Returns (ok, detail)."""
    chain = ancestors(paths, pid)
    if target["kind"] == "peer":
        import platform_windows
        if platform_windows.is_pipe_path(target["socket"]):
            # A pipe cannot be probed without taking a connection from the session.
            # The registry entry was already tied to a live claude process.
            return True, "session inbox pipe (named pipe, not probed)"
        import stat
        try:
            st = os.stat(target["socket"])
        except OSError as e:
            return False, f"inbox socket missing: {e}"
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            return False, "inbox socket is not a socket owned by this user"
        return True, "session inbox socket"
    if target["kind"] == "konsole":
        import platform_linux
        return platform_linux.konsole_verify(paths, pid, target, runner)
    if target["kind"] == "iterm2":
        import platform_macos
        return platform_macos.iterm2_verify(paths, pid, target, runner)
    if target["kind"] == "tmux":
        rc, out, err = runner(["tmux", "-S", target["socket"], "display-message", "-p",
                               "-t", target["pane"], "#{pane_pid}"])
        if rc != 0 or not out.isdigit():
            return False, f"tmux pane unreachable: {err or out}"
        if int(out) not in chain:
            return False, f"tmux pane pid {out} is not an ancestor of claude {pid}"
        return True, f"tmux pane {target['pane']}"
    return False, "unknown target kind"


def send_message(target, message, runner=run, pause=None):
    """Type `message` into the target terminal and press Enter (separately)."""
    import time
    pause = 0.7 if pause is None else pause
    if target["kind"] == "peer":
        return send_peer(target["socket"], message, mode=target.get("mode"))
    if target["kind"] == "konsole":
        import platform_linux
        return platform_linux.konsole_send(target, message, runner, pause)
    if target["kind"] == "iterm2":
        import platform_macos
        return platform_macos.iterm2_send(target, message, runner, pause)
    if target["kind"] == "tmux":
        base = ["tmux", "-S", target["socket"], "send-keys", "-t", target["pane"]]
        rc, _, err = runner(base + ["-l", "--", message])
        if rc != 0:
            return False, err
        if pause:
            time.sleep(pause)
        rc, _, err = runner(base + ["Enter"])
        return rc == 0, err
    return False, "no usable terminal target"


# ---------------------------------------------------------- other pending resumes --
def _cron_field_match(spec, value, lo, hi):
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step != 1 else a
        if a <= value <= b and (value - a) % step == 0:
            return True
    return False


def cron_matches(expr, dt):
    f = expr.split()
    if len(f) != 5:
        return False
    try:
        dow = (dt.weekday() + 1) % 7
        dom_ok = _cron_field_match(f[2], dt.day, 1, 31)
        dow_ok = _cron_field_match(f[4].replace("7", "0") if f[4].isdigit() else f[4], dow, 0, 6)
        day_ok = (dom_ok or dow_ok) if (f[2] != "*" and f[4] != "*") else (dom_ok and dow_ok)
        return (_cron_field_match(f[0], dt.minute, 0, 59)
                and _cron_field_match(f[1], dt.hour, 0, 23)
                and _cron_field_match(f[3], dt.month, 1, 12) and day_ok)
    except ValueError:
        return False


def cron_fires_in(expr, start, end, tz=None):
    """First fire time (epoch) of a 5 field cron expression in [start, end]."""
    t = int(start) // 60 * 60
    if t < start:
        t += 60
    while t <= end:
        if cron_matches(expr, datetime.fromtimestamp(t, tz)):
            return t
        t += 60
    return None


CRON_MAX_AGE = 7 * 86400  # Claude Code deletes recurring CronCreate jobs after 7 days


def native_cron_conflict(transcript, lo, hi, tz=None, since=None):
    """A CronCreate in this session that fires inside [lo, hi] would resume the
    session by itself. Returns (fire_epoch, cron_expr) or None.

    Native crons live only in the process that made them. A cron counts only when
    it was created after `since` (the live process's start), is less than 7 days
    old, and was not removed by a CronDelete. On 2026-11-26 a stale cron from an
    earlier process made the detector skip a real stop, and the session sat for
    two hours after the reset."""
    creates, results, deleted = [], {}, set()
    try:
        with open(transcript, "rb") as f:
            for line in f:
                if b'"CronCreate"' not in line and b'"CronDelete"' not in line and b'"tool_result"' not in line:
                    continue
                rec = _loads(line)
                content = ((rec or {}).get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                try:
                    ts = parse_iso(rec["timestamp"])
                except (KeyError, TypeError, ValueError):
                    ts = None
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and c.get("name") == "CronCreate":
                        creates.append((c.get("id"), (c.get("input") or {}).get("cron", ""), ts))
                    elif c.get("type") == "tool_use" and c.get("name") == "CronDelete":
                        job = (c.get("input") or {}).get("id")
                        if job:
                            deleted.add(str(job))
                    elif c.get("type") == "tool_result" and c.get("tool_use_id"):
                        body = c.get("content")
                        if isinstance(body, list):
                            body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                        results[c["tool_use_id"]] = str(body or "")
    except OSError:
        return None
    for use_id, expr, ts in creates:
        if ts is None or (since is not None and ts < since) or ts + CRON_MAX_AGE < lo:
            continue
        body = results.get(use_id, "")
        if any(job in body for job in deleted):
            continue
        fire = cron_fires_in(expr, lo, hi, tz)
        if fire:
            return fire, expr
    return None


def proc_start_epoch(paths, pid):
    """When a process started; None if unknown."""
    return proc_source(paths).start_epoch(pid)


def scheduled_resume_conflict(paths, session_id, slug, lo, hi, runner=run):
    """Pending scheduled-resume timers (focused-window injection) that fire in
    [lo, hi] and whose message names this session id or its exact project dir."""
    d = paths.scheduled_resume
    if not d.is_dir():
        return None
    for msg in d.glob("claude-resume-*.txt"):
        name = msg.stem
        rc, out, _ = runner(["systemctl", "--user", "show", f"{name}.timer",
                             "-p", "NextElapseUSecRealtime", "-p", "ActiveState",
                             "--timestamp=unix"])
        if rc != 0:
            continue
        props = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        nxt = props.get("NextElapseUSecRealtime", "")
        if props.get("ActiveState") != "active" or not nxt.startswith("@"):
            continue
        fire = int(nxt[1:])
        if not lo <= fire <= hi:
            continue
        try:
            body = msg.read_text(errors="replace")
        except OSError:
            continue
        if session_id in body or f"/{slug}/" in body:
            return fire, name
    return None


# ------------------------------------------------------------------------ state --
def load_state(paths):
    try:
        return json.loads(paths.state_file.read_text())
    except (OSError, ValueError):
        return {"events": {}}


def save_state(paths, state):
    paths.data.mkdir(parents=True, exist_ok=True)
    tmp = paths.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, paths.state_file)


def log(paths, msg, echo=False):
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}"
    if echo:
        print(line)
    try:
        paths.data.mkdir(parents=True, exist_ok=True)
        with open(paths.log_file, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fmt(epoch):
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


# --------------------------------------------------------------------- evaluate --
@dataclass
class Decision:
    session_id: str
    action: str  # schedule | skip
    reason: str
    event: dict = None
    pid: int = None
    target: dict = None
    fire_at: int = None
    transient: bool = False


def evaluate(paths, session_id, transcript, procs, state, now, runner=run, tz=None):
    """Decide what to do for one transcript. Returns a Decision or None when the
    session is not stopped on a usage limit."""
    event, prev_ts = analyze_transcript(transcript, tz)
    if not event:
        return None

    def skip(reason, transient=False):
        return Decision(session_id, "skip", reason, event, transient=transient)

    known = state.get("events", {}).get(event["event_id"])
    if known:
        status = known.get("status")
        retry = (status == "failed" and known.get("attempts", 1) < 3) or \
                (status == "skipped" and known.get("transient"))
        if not retry:
            return skip(f"already handled ({status})")

    target_epoch = event["reset"] + DELAY_AFTER_RESET
    if now > target_epoch + STALE_AFTER:
        return skip("stale: reset passed long ago")

    proc = procs.get(session_id)
    if not proc:
        return skip("no live claude process for this session", transient=True)

    slug = transcript.parent.name
    bg_ts = background_activity_ts(paths, slug, session_id)
    last_active = max(x for x in (prev_ts, bg_ts) if x is not None) if (prev_ts or bg_ts) else None
    if last_active is None or event["ts"] - last_active > ACTIVE_WINDOW:
        idle = "never active" if last_active is None else f"idle {int((event['ts'] - last_active) / 60)} min before the limit"
        return skip(f"not active before the limit ({idle})")
    why_active = ("turn %d min before the limit" % int((event["ts"] - prev_ts) / 60)
                  if prev_ts and event["ts"] - prev_ts <= ACTIVE_WINDOW else "background tasks or agents running")

    lo, hi = event["reset"] - CONFLICT_BEFORE, event["reset"] + CONFLICT_AFTER
    conflict = native_cron_conflict(transcript, max(lo, event["ts"]), hi, tz,
                                    since=proc_start_epoch(paths, proc.pid))
    if conflict:
        # Transient: if that cron does not resume the session, a later scan
        # (after the conflict window) schedules the resume itself.
        return skip(f"session already has a native cron at {fmt(conflict[0])} ({conflict[1]})", transient=True)
    conflict = scheduled_resume_conflict(paths, session_id, slug, lo, hi, runner)
    if conflict:
        return skip(f"session already has scheduled-resume '{conflict[1]}' at {fmt(conflict[0])}")

    cands = candidate_targets(paths, session_id, proc.pid, transcript)
    if not cands:
        return skip(current_platform().no_route_reason, transient=True)
    target, detail, problems = None, "", []
    for cand in cands:
        ok, why = verify_target(paths, proc.pid, cand, runner)
        if ok:
            target, detail = cand, why
            break
        problems.append(f"{cand['kind']}: {why}")
    if not target:
        return skip("no delivery route passed verification (" + "; ".join(problems) + ")",
                    transient=True)

    fires = [e for e in state.get("events", {}).values()
             if e.get("session_id") == session_id and e.get("status") == "fired"
             and now - e.get("fired_at", 0) < 86400]
    if len(fires) >= MAX_FIRES_PER_DAY:
        return skip("resume cap reached for this session in 24h")

    fire_at = max(target_epoch, now + 5)
    return Decision(session_id, "schedule",
                    f"limit stop, active ({why_active}), pid {proc.pid} via {proc.how}, {detail}",
                    event, proc.pid, target, fire_at)

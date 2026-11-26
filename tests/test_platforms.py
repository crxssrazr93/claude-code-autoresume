"""Platform layer tests: registry paths, process sources, plist and schtasks
generation, named pipe delivery, scheduler selection. Most run on every OS with
fakes. A few run only where the real thing exists, and say so."""

import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import detect  # noqa: E402
import lib  # noqa: E402
import platform_base  # noqa: E402
import platform_linux  # noqa: E402
import platform_macos  # noqa: E402
import platform_windows  # noqa: E402
from test_autoresume import (Env, FakeScheduler, NOW, RESET, SID, SLUG, TZ,  # noqa: E402
                             posix_only)

PIPE = r"\\.\pipe\claude-session-1234"
HAS_PSUTIL = importlib.util.find_spec("psutil") is not None
NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="arp")
    test.addCleanup(shutil.rmtree, d, True)
    return Path(d)


# ------------------------------------------------------------ platform choice --
class TestPlatformChoice(unittest.TestCase):
    def test_names(self):
        self.assertEqual(platform_base.current("linux").name, "linux")
        self.assertEqual(platform_base.current("darwin").name, "macos")
        self.assertEqual(platform_base.current("win32").name, "windows")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"AUTORESUME_PLATFORM": "windows"}):
            self.assertEqual(platform_base.current().name, "windows")

    def test_this_machine_picks_its_own(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTORESUME_PLATFORM", None)
            expect = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
            self.assertEqual(platform_base.current().name, expect)


# ------------------------------------------------------------- registry paths --
class TestRegistryPaths(unittest.TestCase):
    def test_default_registry_is_dot_claude_sessions_under_home(self):
        for home in (Path("/Users/me"), Path("C:/Users/me"), Path("/home/me")):
            p = lib.Paths(home=home)
            self.assertEqual(p.sessions, home / ".claude" / "sessions")
            self.assertEqual(p.projects, home / ".claude" / "projects")

    def test_config_dir_overrides_the_registry_location(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(Path("/cfg/claude"))}):
            p = lib.Paths.from_env()
        self.assertEqual(p.sessions, Path("/cfg/claude") / "sessions")

    def test_windows_home_comes_from_userprofile(self):
        profile = str(Path("/winhome/me"))
        env = {"AUTORESUME_PLATFORM": "windows", "USERPROFILE": profile}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            p = lib.Paths.from_env()
        self.assertEqual(p.sessions, Path(profile) / ".claude" / "sessions")

    def test_data_dir_per_platform(self):
        home = Path("/h")
        self.assertEqual(platform_base.current("linux").default_data_dir(home),
                         home / ".local/share/auto-resume")
        self.assertEqual(platform_base.current("macos").default_data_dir(home),
                         home / "Library/Application Support/auto-resume")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path("/h/AppData/Local"))}):
            self.assertEqual(platform_base.current("windows").default_data_dir(home),
                             Path("/h/AppData/Local/auto-resume"))

    def test_registry_entry_gives_the_inbox_path(self):
        root = tmpdir(self)
        paths = lib.Paths(home=root)
        paths.sessions.mkdir(parents=True)
        (paths.sessions / "77.json").write_text(json.dumps(
            {"pid": 77, "sessionId": SID, "messagingSocketPath": PIPE}))
        self.assertEqual(lib.peer_socket(paths, SID), PIPE)
        self.assertIsNone(lib.peer_socket(paths, "no-such-session"))


# ------------------------------------------------------------- process source --
class FakeProc:
    def __init__(self, d):
        self.d = d

    def ppid(self):
        return self.d["ppid"]

    def name(self):
        return self.d["name"]

    def cmdline(self):
        return self.d.get("cmdline", [self.d["name"]])

    def cwd(self):
        return self.d.get("cwd", "")

    def environ(self):
        return self.d.get("environ", {})

    def create_time(self):
        return self.d.get("start", 1000.0)

    def open_files(self):
        return [types.SimpleNamespace(path=p) for p in self.d.get("files", [])]


class FakePsutil:
    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    def __init__(self, procs):
        self.procs = procs

    def pids(self):
        return list(self.procs)

    def Process(self, pid):
        if pid not in self.procs:
            raise self.NoSuchProcess(pid)
        return FakeProc(self.procs[pid])


S2 = "22222222-aaaa-bbbb-cccc-000000000002"


class TestPsutilSource(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir(self)
        self.paths = lib.Paths(home=self.root)
        self.procs = {
            100: {"ppid": 1, "name": "explorer.exe"},
            101: {"ppid": 100, "name": "WindowsTerminal.exe"},
            110: {"ppid": 101, "name": "claude.exe", "cwd": r"C:\work\proj",
                  "cmdline": [r"C:\Users\me\.local\bin\claude.exe"]},
            120: {"ppid": 101, "name": "Claude.EXE", "cmdline": [r"C:\bin\Claude.EXE", "--resume", S2]},
            130: {"ppid": 101, "name": "python.exe", "cmdline": ["python.exe", "x.py"]},
            140: {"ppid": 101, "name": "claude.exe",
                  "cmdline": ["claude.exe", "--chrome-native-host"]},
        }
        self.src = platform_base.PsutilSource(FakePsutil(self.procs))
        patcher = mock.patch.object(lib, "proc_source", return_value=self.src)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, pid, sid, **extra):
        self.paths.sessions.mkdir(parents=True, exist_ok=True)
        (self.paths.sessions / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "cwd": r"C:\work\proj", **extra}))

    def test_stat_and_missing_process(self):
        self.assertEqual(self.src.stat(110)[0], 101)
        self.assertIsNone(self.src.stat(9999))
        self.assertEqual(self.src.cmdline(9999), [])

    def test_registry_maps_session_to_exe_named_process(self):
        self.register(110, SID)
        found = lib.find_claude_processes(self.paths)
        self.assertEqual(found[SID].pid, 110)
        self.assertEqual(found[SID].how, "sessions-registry")

    def test_start_token_is_not_compared_when_the_source_has_none(self):
        self.register(110, SID, procStart="12345")
        self.assertIn(SID, lib.find_claude_processes(self.paths))

    def test_resume_argument_and_case_insensitive_exe(self):
        found = lib.find_claude_processes(self.paths)
        self.assertEqual(found[S2].pid, 120)
        self.assertEqual(found[S2].how, "cmdline")

    def test_python_and_native_host_are_not_sessions(self):
        pids = {p.pid for p in lib.find_claude_processes(self.paths).values()}
        self.assertNotIn(130, pids)
        self.assertNotIn(140, pids)

    def test_registry_entry_for_non_claude_process_is_rejected(self):
        self.register(130, SID)
        self.assertNotIn(SID, lib.find_claude_processes(self.paths))

    def test_ancestors(self):
        self.assertEqual(lib.ancestors(self.paths, 110), [110, 101, 100])

    def test_exe_name(self):
        self.assertEqual(lib.exe_name(r"C:\x\Claude.EXE"), "claude")
        self.assertEqual(lib.exe_name("/usr/local/bin/claude"), "claude")

    def test_missing_psutil_gives_an_install_hint(self):
        with mock.patch.dict(sys.modules, {"psutil": None}):
            with self.assertRaises(ImportError) as cm:
                platform_base.PsutilSource()
        self.assertIn("pip install", str(cm.exception))


@unittest.skipUnless(HAS_PSUTIL, "psutil is not installed")
class TestRealPsutil(unittest.TestCase):
    """The real library, on whatever OS this runs on."""

    def test_own_process_is_described(self):
        src = platform_base.PsutilSource()
        me = os.getpid()
        st = src.stat(me)
        self.assertIsNotNone(st)
        self.assertEqual(st[0], os.getppid())
        self.assertTrue(src.name(me))
        self.assertTrue(src.cmdline(me))
        self.assertTrue(any(k.upper() == "PATH" for k in src.environ(me)))
        self.assertIsNotNone(src.start_epoch(me))
        self.assertIn(me, src.pids())

    def test_own_process_is_not_a_claude_session(self):
        paths = lib.Paths(home=tmpdir(self))
        self.assertFalse(lib.proc_is_claude(paths, os.getpid()))
        self.assertIn(os.getpid(), lib.ancestors(paths, os.getpid()))

    def test_environment_line_names_the_platform(self):
        line = lib.describe_environment(lib.Paths(home=tmpdir(self)))
        self.assertIn("platform=", line)
        self.assertIn("registry=", line)


class TestLinuxPsutilHybrid(unittest.TestCase):
    def test_fake_proc_always_uses_procfs(self):
        paths = lib.Paths(home=Path("/h"), proc=Path("/fake/proc"))
        self.assertIsInstance(platform_linux.PLATFORM.proc_source(paths), platform_linux.ProcfsSource)

    def test_real_proc_falls_back_when_psutil_is_missing(self):
        with mock.patch.dict(sys.modules, {"psutil": None}):
            src = platform_linux.PLATFORM.proc_source(lib.Paths(home=Path("/h")))
        self.assertIsInstance(src, platform_linux.ProcfsSource)

    def test_real_proc_uses_psutil_with_procfs_tokens_when_present(self):
        fake = FakePsutil({})
        with mock.patch.dict(sys.modules, {"psutil": fake}):
            src = platform_linux.PLATFORM.proc_source(lib.Paths(home=Path("/h")))
        self.assertIsInstance(src, platform_base.PsutilSource)
        self.assertIsInstance(src.token_source, platform_linux.ProcfsSource)


# ------------------------------------------------------------------------ macOS --
class TestMacLaunchd(unittest.TestCase):
    def test_detect_plist(self):
        data = plistlib.loads(platform_macos.detect_plist(
            "/usr/bin/python3", "/Users/me/.claude/auto-resume", "/tmp/launchd.log",
            environ={"CLAUDE_CONFIG_DIR": "/cfg", "AUTORESUME_DELAY": "60", "OTHER": "x"}))
        self.assertEqual(data["Label"], "com.claude-code-autoresume.detect")
        self.assertEqual(data["StartInterval"], 120)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["ProgramArguments"][0], "/usr/bin/python3")
        self.assertTrue(data["ProgramArguments"][1].endswith("detect.py"))
        self.assertEqual(data["ProgramArguments"][2], "scan")
        env = data["EnvironmentVariables"]
        self.assertIn("/opt/homebrew/bin", env["PATH"])  # so launchd finds tmux
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/cfg")
        self.assertEqual(env["AUTORESUME_DELAY"], "60")
        self.assertNotIn("OTHER", env)

    def test_fire_plist_uses_the_local_calendar_time(self):
        fire_at = 1795456500
        data = plistlib.loads(platform_macos.fire_plist(
            "com.claude-code-autoresume.u1", "/usr/bin/python3", "/x/fire.py", "evt-1", fire_at,
            environ={}))
        when = datetime.fromtimestamp(fire_at)
        self.assertEqual(data["StartCalendarInterval"],
                         {"Month": when.month, "Day": when.day, "Hour": when.hour, "Minute": when.minute})
        self.assertEqual(data["ProgramArguments"][1:], ["/x/fire.py", "evt-1"])
        self.assertFalse(data["RunAtLoad"])

    def test_scheduler_bootstraps_checks_and_boots_out(self):
        agents = tmpdir(self)
        calls = []

        def runner(cmd, timeout=10):
            calls.append(cmd)
            return 0, "", ""

        s = platform_macos.LaunchdScheduler(agents_dir=agents, uid=501)
        ok, _ = s.schedule("claude-autoresume-abc12345-99", 1795456500, "evt-1", runner)
        self.assertTrue(ok)
        plist = agents / "com.claude-code-autoresume.claude-autoresume-abc12345-99.plist"
        self.assertTrue(plist.exists())
        self.assertEqual(calls[-1], ["launchctl", "bootstrap", "gui/501", str(plist)])
        self.assertTrue(s.active("claude-autoresume-abc12345-99", runner))
        self.assertEqual(calls[-1][:2], ["launchctl", "print"])
        s.cancel("claude-autoresume-abc12345-99", runner)
        self.assertEqual(calls[-1][:2], ["launchctl", "bootout"])
        self.assertFalse(plist.exists())

    def test_bootstrap_failure_is_reported(self):
        s = platform_macos.LaunchdScheduler(agents_dir=tmpdir(self), uid=501)
        ok, err = s.schedule("u", 1795456500, "e",
                             lambda cmd, timeout=10: (5, "", "Bootstrap failed") if cmd[1] == "bootstrap" else (0, "", ""))
        self.assertFalse(ok)
        self.assertIn("Bootstrap failed", err)

    def test_iterm2_session_id_parsing(self):
        self.assertEqual(platform_macos.iterm2_session_id({"ITERM_SESSION_ID": "w0t1p2:ABCD-1234"}), "ABCD-1234")
        self.assertEqual(platform_macos.iterm2_session_id({}), "")

    @unittest.skipUnless(sys.platform == "darwin", "plutil exists only on macOS")
    def test_plutil_accepts_the_plist(self):
        f = tmpdir(self) / "d.plist"
        f.write_bytes(platform_macos.detect_plist(sys.executable, "/x", "/tmp/l.log", environ={}))
        r = subprocess.run(["plutil", "-lint", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class FakeIterm2:
    """Stands in for the `iterm2` package."""

    def __init__(self, shell_pid, known="SESS-UUID"):
        self.sent = []
        outer = self

        class Session:
            async def async_get_variable(self, name):
                return shell_pid

            async def async_send_text(self, text):
                outer.sent.append(text)

        class App:
            def get_session_by_id(self, sid):
                return Session() if sid == known else None

        class Connection:
            @staticmethod
            async def async_create():
                return object()

        async def async_get_app(connection):
            return App()

        self.module = types.ModuleType("iterm2")
        self.module.Connection = Connection
        self.module.async_get_app = async_get_app


class TestIterm2Route(unittest.TestCase):
    def setUp(self):
        self.paths = lib.Paths(home=tmpdir(self))
        self.target = {"kind": "iterm2", "session": "SESS-UUID"}
        patcher = mock.patch.object(lib, "ancestors", return_value=[600, 500])
        patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, fake):
        p = mock.patch.dict(sys.modules, {"iterm2": fake.module})
        p.start()
        self.addCleanup(p.stop)

    def test_terminal_target_from_environment(self):
        with mock.patch.object(lib, "proc_environ", return_value={"ITERM_SESSION_ID": "w0t0p0:SESS-UUID"}):
            self.assertEqual(lib.terminal_target(self.paths, 600), self.target)

    def test_tmux_wins_over_iterm2(self):
        env = {"ITERM_SESSION_ID": "w0t0p0:SESS-UUID", "TMUX": "/tmp/t,1,0", "TMUX_PANE": "%1"}
        with mock.patch.object(lib, "proc_environ", return_value=env):
            self.assertEqual(lib.terminal_target(self.paths, 600)["kind"], "tmux")

    def test_verify_accepts_a_session_whose_shell_is_an_ancestor(self):
        self.use(FakeIterm2(shell_pid=500))
        ok, why = lib.verify_target(self.paths, 600, self.target)
        self.assertTrue(ok, why)

    def test_verify_refuses_a_session_with_another_shell(self):
        self.use(FakeIterm2(shell_pid=4242))
        ok, why = lib.verify_target(self.paths, 600, self.target)
        self.assertFalse(ok)
        self.assertIn("not an ancestor", why)

    def test_verify_refuses_an_unknown_session(self):
        self.use(FakeIterm2(shell_pid=500, known="OTHER"))
        ok, why = lib.verify_target(self.paths, 600, self.target)
        self.assertFalse(ok)

    def test_send_types_the_text_then_enter_into_that_session_only(self):
        fake = FakeIterm2(shell_pid=500)
        self.use(fake)
        ok, _ = lib.send_message(self.target, "go on", pause=0)
        self.assertTrue(ok)
        self.assertEqual(fake.sent, ["go on", "\r"])

    def test_missing_package_is_reported_not_raised(self):
        with mock.patch.dict(sys.modules, {"iterm2": None}):
            ok, why = lib.verify_target(self.paths, 600, self.target)
        self.assertFalse(ok)
        self.assertIn("pip install", why)


# ---------------------------------------------------------------------- Windows --
class TestWindowsTaskXml(unittest.TestCase):
    @staticmethod
    def parse(xml):
        return ET.fromstring(xml.split("?>", 1)[1])

    def test_detect_task_repeats_every_two_minutes(self):
        xml = platform_windows.detect_task_xml(r"C:\Python312\python.exe", r"C:\Users\me\.claude\auto-resume",
                                               now=datetime(2026, 11, 27, 12, 0, 0))
        root = self.parse(xml)
        self.assertEqual(root.find(f".//{NS}Repetition/{NS}Interval").text, "PT2M")
        start = root.find(f".//{NS}TimeTrigger/{NS}StartBoundary").text
        self.assertTrue(start.startswith("2026-11-27T12:00:00"))
        self.assertIn(start[19], "+-Z")  # carries a UTC offset
        self.assertTrue(root.find(f".//{NS}Command").text.endswith("python.exe"))
        args = root.find(f".//{NS}Arguments").text
        self.assertTrue(args.startswith('"') and args.endswith(" scan"))
        self.assertIn("detect.py", args)
        self.assertEqual(root.find(f".//{NS}Hidden").text, "true")
        self.assertEqual(root.find(f".//{NS}RunLevel").text, "LeastPrivilege")
        self.assertEqual(root.find(f".//{NS}MultipleInstancesPolicy").text, "IgnoreNew")

    def test_fire_task_is_one_time_with_the_event_id(self):
        xml = platform_windows.fire_task_xml(r"C:\Python312\python.exe", Path("C:/x/fire.py"),
                                             "evt-9", 1795456500)
        root = self.parse(xml)
        self.assertIsNone(root.find(f".//{NS}Repetition"))
        self.assertIsNotNone(root.find(f".//{NS}TimeTrigger/{NS}EndBoundary"))
        self.assertTrue(root.find(f".//{NS}Arguments").text.endswith(" evt-9"))

    def test_xml_special_characters_in_paths_are_escaped(self):
        xml = platform_windows.detect_task_xml(r"C:\R&D\python.exe", r"C:\A&B\auto-resume")
        root = self.parse(xml)  # raises if not well formed
        self.assertIn("A&B", root.find(f".//{NS}Arguments").text)

    def test_declares_utf16_as_schtasks_needs(self):
        self.assertIn('encoding="UTF-16"', platform_windows.detect_task_xml("p.exe", "d"))

    def test_scheduler_creates_queries_and_deletes(self):
        calls, seen = [], {}

        def runner(cmd, timeout=10):
            calls.append(cmd)
            if cmd[1] == "/Create":
                path = Path(cmd[cmd.index("/XML") + 1])
                seen["path"] = path
                seen["xml"] = path.read_bytes().decode("utf-16")
            return 0, "", ""

        s = platform_windows.SchtasksScheduler()
        ok, _ = s.schedule("claude-autoresume-abc12345-99", 1795456500, "evt-1", runner)
        self.assertTrue(ok)
        self.assertEqual(calls[0][:4], ["schtasks", "/Create", "/TN", "claude-autoresume-abc12345-99"])
        self.assertEqual(calls[0][-1], "/F")
        self.assertIn("evt-1", seen["xml"])
        self.assertFalse(seen["path"].exists(), "temp XML must be removed")
        self.assertTrue(s.active("claude-autoresume-abc12345-99", runner))
        self.assertEqual(calls[-1][:3], ["schtasks", "/Query", "/TN"])
        s.cancel("claude-autoresume-abc12345-99", runner)
        self.assertEqual(calls[-1], ["schtasks", "/Delete", "/TN", "claude-autoresume-abc12345-99", "/F"])

    def test_create_failure_is_reported(self):
        s = platform_windows.SchtasksScheduler()
        ok, err = s.schedule("u", 1795456500, "e", lambda cmd, timeout=10: (1, "", "Access is denied"))
        self.assertFalse(ok)
        self.assertIn("denied", err)

    @unittest.skipUnless(sys.platform == "win32", "schtasks exists only on Windows")
    def test_real_schtasks_accepts_the_one_time_task(self):
        name = f"claude-autoresume-citest-{os.getpid()}"
        s = platform_windows.SchtasksScheduler()
        fire_at = time.time() + 30 * 86400
        try:
            ok, err = s.schedule(name, fire_at, "citest", lib.run)
            self.assertTrue(ok, err)
            self.assertTrue(s.active(name, lib.run))
        finally:
            s.cancel(name, lib.run)
        self.assertFalse(s.active(name, lib.run))


class TestNamedPipePaths(unittest.TestCase):
    def test_pipe_path_detection(self):
        for p in (PIPE, r"\\.\PIPE\x", "//./pipe/x", r"\\?\pipe\x"):
            self.assertTrue(platform_windows.is_pipe_path(p), p)
        for p in ("/run/user/1000/cc-socks/1.sock", r"C:\tmp\x.sock", "", None):
            self.assertFalse(platform_windows.is_pipe_path(p), p)

    def test_frame_is_written_to_the_pipe_as_binary(self):
        opened = []

        class Sink:
            def __init__(self):
                self.data = b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, b):
                self.data += b

        sink = Sink()

        def opener(path, mode, buffering=-1):
            opened.append((path, mode, buffering))
            return sink

        ok, detail = lib.send_peer(PIPE, "resume now", opener=opener)
        self.assertTrue(ok, detail)
        self.assertEqual(opened, [(PIPE, "r+b", 0)])
        frame = json.loads(sink.data.decode())
        self.assertEqual(frame["msgV"], 1)
        self.assertIn("resume now", frame["message"]["content"])
        self.assertTrue(sink.data.endswith(b"\n"))

    def test_falls_back_to_write_only_when_the_pipe_is_inbound_only(self):
        modes = []

        def opener(path, mode, buffering=-1):
            modes.append(mode)
            if mode == "r+b":
                raise PermissionError("access denied")
            return open(os.devnull, "wb")

        ok, _ = platform_windows.send_pipe(PIPE, b"x\n", opener)
        self.assertTrue(ok)
        self.assertEqual(modes, ["r+b", "wb"])

    def test_missing_pipe_is_reported_not_raised(self):
        def opener(path, mode, buffering=-1):
            raise FileNotFoundError("no such pipe")

        ok, detail = platform_windows.send_pipe(PIPE, b"x\n", opener)
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)

    def test_verify_does_not_probe_a_pipe(self):
        paths = lib.Paths(home=tmpdir(self))
        with mock.patch.object(lib, "ancestors", return_value=[1]):
            ok, why = lib.verify_target(paths, 1, {"kind": "peer", "socket": PIPE, "mode": None})
        self.assertTrue(ok, why)
        self.assertIn("pipe", why)

    def test_candidate_comes_from_the_registry_pipe_path(self):
        root = tmpdir(self)
        paths = lib.Paths(home=root)
        paths.sessions.mkdir(parents=True)
        (paths.sessions / "77.json").write_text(json.dumps(
            {"pid": 77, "sessionId": SID, "messagingSocketPath": PIPE}))
        with mock.patch.object(lib, "proc_environ", return_value={}):
            cands = lib.candidate_targets(paths, SID, 77)
        self.assertEqual([c["kind"] for c in cands], ["peer"])
        self.assertEqual(cands[0]["socket"], PIPE)

    def test_no_registry_path_means_no_route(self):
        root = tmpdir(self)
        paths = lib.Paths(home=root)
        paths.sessions.mkdir(parents=True)
        (paths.sessions / "77.json").write_text(json.dumps({"pid": 77, "sessionId": SID}))
        with mock.patch.object(lib, "proc_environ", return_value={}):
            self.assertEqual(lib.candidate_targets(paths, SID, 77), [])
        self.assertIn("autoContinueAtUsageLimit", platform_base.current("windows").no_route_reason)


@unittest.skipUnless(sys.platform == "win32", "needs real Windows named pipes")
class TestRealNamedPipe(unittest.TestCase):
    def test_frame_arrives_through_a_real_pipe(self):
        import ctypes
        import threading
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateNamedPipeW.restype = wintypes.HANDLE
        k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        name = rf"\\.\pipe\claude-autoresume-citest-{os.getpid()}"
        handle = k32.CreateNamedPipeW(name, 3, 0, 1, 65536, 65536, 0, None)  # duplex, byte mode, wait
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        got = []

        def serve():
            k32.ConnectNamedPipe(handle, None)  # returns 0 if the client was faster; that is fine
            data = b""
            buf = ctypes.create_string_buffer(65536)
            n = wintypes.DWORD(0)
            while not data.endswith(b"\n"):
                if not k32.ReadFile(handle, buf, 65536, ctypes.byref(n), None) or n.value == 0:
                    break
                data += buf.raw[:n.value]
            got.append(data)

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        ok, detail = lib.send_peer(name, "hello from the test")
        t.join(10)
        k32.CloseHandle(handle)
        self.assertTrue(ok, detail)
        self.assertEqual(len(got), 1)
        self.assertIn("hello from the test", json.loads(got[0].decode())["message"]["content"])


# -------------------------------------------------------------------- schedulers --
class TestSchedulerChoice(unittest.TestCase):
    def test_systemd_is_the_default(self):
        s = platform_linux.make_scheduler(environ={}, wsl=False)
        self.assertIsInstance(s, platform_linux.SystemdScheduler)

    def test_poll_when_asked(self):
        s = platform_linux.make_scheduler(environ={"AUTORESUME_SCHEDULER": "poll"}, wsl=False)
        self.assertIsInstance(s, platform_base.PollScheduler)

    def test_wsl_without_systemd_polls(self):
        s = platform_linux.make_scheduler(environ={}, wsl=True, runner=lambda c, timeout=10: (1, "", ""))
        self.assertIsInstance(s, platform_base.PollScheduler)

    def test_wsl_with_systemd_uses_it(self):
        s = platform_linux.make_scheduler(environ={}, wsl=True, runner=lambda c, timeout=10: (0, "", ""))
        self.assertIsInstance(s, platform_linux.SystemdScheduler)

    def test_poll_scheduler_never_fails_and_never_needs_cleanup(self):
        s = platform_base.PollScheduler()
        self.assertEqual(s.schedule("u", 1, "e"), (True, ""))
        self.assertTrue(s.active("u"))
        self.assertFalse(s.needs_cleanup)
        self.assertTrue(s.polls)

    def test_linux_systemd_command_is_unchanged(self):
        calls = []
        platform_linux.SystemdScheduler().schedule(
            "claude-autoresume-abc12345-99", 1795456500, "evt-1",
            lambda cmd, timeout=10: calls.append(cmd) or (0, "", ""))
        cmd = calls[0]
        self.assertEqual(cmd[:3], ["systemd-run", "--user", "--quiet"])
        self.assertIn("--unit=claude-autoresume-abc12345-99", cmd)
        self.assertIn("--timer-property=RemainAfterElapse=no", cmd)
        self.assertEqual(cmd[-1], "evt-1")


class CleanupScheduler(FakeScheduler):
    needs_cleanup = True

    def __init__(self):
        super().__init__()
        self.cancelled = []

    def cancel(self, unit, runner=None):
        self.cancelled.append(unit)
        return True


class PollingFake(FakeScheduler):
    polls = True


@posix_only
class TestScanWithPlatformSchedulers(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()

    def scan(self, sched, now=NOW):
        return detect.scan(self.env.paths, now=now, scheduler=sched, runner=self.env.runner, tz=TZ)

    def test_finished_jobs_are_cleaned_up_once(self):
        sched = CleanupScheduler()
        self.scan(sched)
        state = lib.load_state(self.env.paths)
        state["events"]["err1"].update(status="fired", fired_at=NOW)
        lib.save_state(self.env.paths, state)
        self.scan(sched)
        self.scan(sched)
        self.assertEqual(len(sched.cancelled), 1)
        self.assertTrue(lib.load_state(self.env.paths)["events"]["err1"]["cleaned"])

    def test_pending_jobs_are_not_cleaned_up(self):
        sched = CleanupScheduler()
        self.scan(sched)
        self.scan(sched)
        self.assertEqual(sched.cancelled, [])

    def test_linux_style_schedulers_get_no_cleanup_state(self):
        self.scan(FakeScheduler())
        self.assertNotIn("cleaned", lib.load_state(self.env.paths)["events"]["err1"])

    def test_poll_scheduler_fires_the_event_once_it_is_due(self):
        sched = PollingFake()
        with mock.patch.object(detect, "run_due_polled") as run_due:
            self.scan(sched, now=NOW)
            self.assertEqual(run_due.call_args[0][1], [])  # not due yet
            self.scan(sched, now=RESET + 400)
            self.assertEqual(run_due.call_args[0][1], ["err1"])

    def test_poll_scheduler_does_not_fire_in_a_dry_run(self):
        with mock.patch.object(detect, "run_due_polled") as run_due:
            detect.scan(self.env.paths, now=RESET + 400, scheduler=PollingFake(),
                        runner=self.env.runner, tz=TZ, dry_run=True)
        run_due.assert_not_called()

    def test_reason_text_names_the_platform_route(self):
        for name, needle in (("macos", "iTerm2"), ("linux", "Konsole"),
                             ("windows", "autoContinueAtUsageLimit")):
            self.assertIn(needle, platform_base.current(name).no_route_reason)


class TestLock(unittest.TestCase):
    def test_second_lock_is_refused_while_the_first_is_held(self):
        path = tmpdir(self) / "scan.lock"
        with open(path, "w") as a:
            self.assertTrue(lib.try_lock(a))
            if sys.platform == "win32":
                b = open(path, "w")
            else:
                b = open(path, "w")
            try:
                self.assertFalse(lib.try_lock(b))
            finally:
                b.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

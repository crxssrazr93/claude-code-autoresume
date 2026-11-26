import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import detect  # noqa: E402
import fire  # noqa: E402
import lib  # noqa: E402

FIX = HERE / "fixtures"
TZ = ZoneInfo("Asia/Kolkata")
ERROR_TS = 1795452633      # 2026-11-23T16:50:33Z
RESET = 1795456200         # 2026-11-23T17:50:00Z = 23:20 Kolkata
NOW = ERROR_TS + 70
SID = "11111111-aaaa-bbbb-cccc-000000000001"
SLUG = "-home-user-proj"
MSG = lib.MESSAGE


class FakeScheduler:
    def __init__(self):
        self.calls = []
        self.live = set()

    def schedule(self, unit, fire_at, event_id, runner=None):
        self.calls.append((unit, fire_at, event_id))
        self.live.add(unit)
        return True, ""

    def active(self, unit, runner=None):
        return unit in self.live


class Env:
    """A fake HOME plus a fake /proc."""

    def __init__(self):
        base = HERE / ".work"
        base.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base))
        self.home = self.root / "home"
        self.proc = self.root / "proc"
        self.home.mkdir()
        self.proc.mkdir()
        self.paths = lib.Paths(home=self.home, proc=self.proc)
        self.calls = []
        self.timers = {}  # unit name -> epoch
        self.qdbus_shell = {}

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def transcript(self, fixture, sid=SID, slug=SLUG, extra=""):
        d = self.paths.projects / slug
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        f.write_text((FIX / fixture).read_text() + extra)
        return f

    def process(self, pid, ppid=500, pgrp=None, comm="claude", cmdline=None, env=None,
                cwd="/home/user/proj", start="9000"):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "comm").write_text(comm + "\n")
        pgrp = pid if pgrp is None else pgrp
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pgrp} " + "0 " * 16 + f"{start} 0\n")
        (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in (cmdline or ["claude"])) + b"\0")
        (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()) + b"\0")
        link = d / "cwd"
        if link.is_symlink():
            link.unlink()
        os.symlink(cwd, link)
        return d

    def konsole_shell(self, pid=500):
        self.process(pid, ppid=1, comm="fish")
        self.qdbus_shell[(":1.9", "/Sessions/1")] = pid

    def register(self, pid, sid=SID, start="9000", cwd="/home/user/proj"):
        self.paths.sessions.mkdir(parents=True, exist_ok=True)
        (self.paths.sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "sessionId": sid, "cwd": cwd, "procStart": start}))

    def live_session(self, pid=600, sid=SID, tmux=False):
        """claude process wired to a Konsole tab (or tmux pane) plus registry entry."""
        self.konsole_shell(500)
        env = {"KONSOLE_DBUS_SERVICE": ":1.9", "KONSOLE_DBUS_SESSION": "/Sessions/1"}
        if tmux:
            env.update(TMUX="/run/tmux-1000/default,77,0", TMUX_PANE="%3")
        self.process(pid, env=env)
        self.register(pid, sid)

    def runner(self, cmd, timeout=10):
        self.calls.append(cmd)
        if cmd[0] == "systemctl" and "show" in cmd:
            name = cmd[3][:-len(".timer")]
            if name in self.timers:
                return 0, f"ActiveState=active\nNextElapseUSecRealtime=@{self.timers[name]}", ""
            return 0, "ActiveState=inactive\nNextElapseUSecRealtime=", ""
        if cmd[0].endswith("qdbus") or "qdbus" in cmd[0]:
            key = (cmd[1], cmd[2])
            if cmd[3].endswith("processId") and not cmd[3].endswith("foregroundProcessId"):
                return (0, str(self.qdbus_shell[key]), "") if key in self.qdbus_shell else (1, "", "no service")
            if cmd[3].endswith("foregroundProcessId"):
                return 0, "600", ""
            return 0, "", ""
        if cmd[0] == "tmux":
            if "display-message" in cmd:
                return 0, "500", ""
            return 0, "", ""
        return 0, "", ""


class TestParse(unittest.TestCase):
    def test_reset_after_midnight(self):
        # error at 23:50 Kolkata, "resets 12:10am" must be the next day
        err = 1795392000 + 18 * 3600 + 20 * 60
        self.assertEqual(lib.parse_reset("limit · resets 12:10am (Asia/Kolkata)", err), 1795459200)

    def test_reset_same_day_pm(self):
        self.assertEqual(lib.parse_reset("resets 11:20pm (Asia/Kolkata)", ERROR_TS), RESET)

    def test_reset_with_date(self):
        got = lib.parse_reset("resets Nov 28, 3pm (Asia/Kolkata)", ERROR_TS)
        from datetime import datetime
        self.assertEqual(datetime.fromtimestamp(got, TZ).strftime("%m-%d %H:%M"), "11-28 15:00")

    def test_legacy_pipe_epoch(self):
        self.assertEqual(lib.parse_reset("Claude AI usage limit reached|1795456200", 0), 1795456200)

    def test_twelve_noon_and_midnight(self):
        noon = lib.parse_reset("resets 12pm (Asia/Kolkata)", ERROR_TS)
        self.assertEqual(noon % 86400, (12 * 3600 - 19800) % 86400)

    def test_cron_matcher(self):
        from datetime import datetime
        dt = datetime(2026, 11, 23, 23, 25, tzinfo=TZ)
        self.assertTrue(lib.cron_matches("25 23 23 11 *", dt))
        self.assertTrue(lib.cron_matches("*/5 * * * *", dt))
        self.assertFalse(lib.cron_matches("47 * * * *", dt))


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def evaluate(self, state=None, now=NOW):
        procs = lib.find_claude_processes(self.env.paths)
        t = self.env.paths.projects / SLUG / f"{SID}.jsonl"
        return lib.evaluate(self.env.paths, SID, t, procs, state or {"events": {}}, now,
                            self.env.runner, TZ)

    def test_limit_and_active_is_scheduled(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        d = self.evaluate()
        self.assertEqual(d.action, "schedule", d.reason)
        self.assertEqual(d.fire_at, RESET + 300)
        self.assertEqual(d.event["reset_source"], "resetsAt")
        self.assertEqual(d.target["kind"], "konsole")
        self.assertEqual(d.pid, 600)

    def test_idle_for_three_hours_is_left_alone(self):
        self.env.transcript("limit_idle.jsonl")
        self.env.live_session()
        d = self.evaluate()
        self.assertEqual(d.action, "skip")
        self.assertIn("not active", d.reason)

    def test_idle_but_background_agents_running_is_resumed(self):
        self.env.transcript("limit_idle.jsonl")
        self.env.live_session()
        agent = self.env.paths.projects / SLUG / SID / "subagents" / "agent-1.jsonl"
        agent.parent.mkdir(parents=True)
        agent.write_text("{}\n")
        os.utime(agent, (ERROR_TS - 120, ERROR_TS - 120))
        d = self.evaluate()
        self.assertEqual(d.action, "schedule", d.reason)
        self.assertIn("background", d.reason)

    def test_stale_background_files_do_not_count(self):
        self.env.transcript("limit_idle.jsonl")
        self.env.live_session()
        agent = self.env.paths.projects / SLUG / SID / "subagents" / "agent-1.jsonl"
        agent.parent.mkdir(parents=True)
        agent.write_text("{}\n")
        os.utime(agent, (ERROR_TS - 7200, ERROR_TS - 7200))
        self.assertEqual(self.evaluate().action, "skip")

    def test_no_error_is_ignored(self):
        self.env.transcript("no_error.jsonl")
        self.env.live_session()
        self.assertIsNone(self.evaluate())

    def test_session_that_already_continued_is_ignored(self):
        self.env.transcript("limit_then_continued.jsonl")
        self.env.live_session()
        self.assertIsNone(self.evaluate())

    def test_already_resumed_event_is_skipped(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        state = {"events": {"err1": {"session_id": SID, "status": "fired"}}}
        d = self.evaluate(state)
        self.assertEqual(d.action, "skip")
        self.assertIn("already handled", d.reason)

    def test_reset_after_midnight_from_text_only_error(self):
        self.env.transcript("limit_midnight_text_only.jsonl")
        self.env.live_session()
        d = self.evaluate(now=1795392000 + 18 * 3600 + 21 * 60)
        self.assertEqual(d.action, "schedule", d.reason)
        self.assertEqual(d.event["reset"], 1795459200)
        self.assertEqual(d.event["reset_source"], "text")
        self.assertEqual(d.fire_at, 1795459200 + 300)

    def test_dead_process_is_skipped(self):
        self.env.transcript("limit_active.jsonl")
        d = self.evaluate()
        self.assertEqual(d.action, "skip")
        self.assertIn("no live claude process", d.reason)

    def test_stale_event_is_skipped(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        d = self.evaluate(now=RESET + 300 + 3 * 3600)
        self.assertIn("stale", d.reason)

    def test_past_target_but_recent_fires_soon(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        now = RESET + 600
        d = self.evaluate(now=now)
        self.assertEqual(d.action, "schedule", d.reason)
        self.assertEqual(d.fire_at, now + 5)

    def _with_cron(self, extra=()):
        cron = {"type": "assistant", "uuid": "c1", "timestamp": "2026-11-23T16:45:00.000Z",
                "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "CronCreate",
                                         "input": {"cron": "25 23 23 11 *", "recurring": False,
                                                   "prompt": "resume"}}]}}
        f = self.env.transcript("limit_active.jsonl")
        text = f.read_text().splitlines()
        for i, rec in enumerate([cron, *extra]):
            text.insert(2 + i, json.dumps(rec))
        f.write_text("\n".join(text) + "\n")
        self.env.live_session()

    def _process_started(self, epoch):
        orig = lib.proc_start_epoch
        lib.proc_start_epoch = lambda paths, pid: epoch
        self.addCleanup(setattr, lib, "proc_start_epoch", orig)

    def test_native_cron_inside_window_skips(self):
        self._with_cron()
        self._process_started(0)
        d = self.evaluate()
        self.assertEqual(d.action, "skip")
        self.assertIn("native cron", d.reason)
        self.assertTrue(d.transient)

    def test_native_cron_from_an_earlier_process_does_not_skip(self):
        # 2026-11-26: a cron made by a process that has since exited cannot fire.
        self._with_cron()
        self._process_started(lib.parse_iso("2026-11-23T17:00:00Z"))
        self.assertEqual(self.evaluate().action, "schedule")

    def test_deleted_native_cron_does_not_skip(self):
        result = {"type": "user", "uuid": "c2", "timestamp": "2026-11-23T16:45:01.000Z",
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1",
                                           "content": "Scheduled job abc123 (one-shot)"}]}}
        delete = {"type": "assistant", "uuid": "c3", "timestamp": "2026-11-23T16:46:00.000Z",
                  "message": {"content": [{"type": "tool_use", "id": "tu2", "name": "CronDelete",
                                           "input": {"id": "abc123"}}]}}
        self._with_cron([result, delete])
        self._process_started(0)
        self.assertEqual(self.evaluate().action, "schedule")

    def test_native_cron_far_from_reset_does_not_skip(self):
        cron = {"type": "assistant", "uuid": "c1", "timestamp": "2026-11-23T16:45:00.000Z",
                "message": {"content": [{"type": "tool_use", "name": "CronCreate",
                                         "input": {"cron": "30 3 24 11 *", "recurring": False}}]}}
        f = self.env.transcript("limit_active.jsonl")
        text = f.read_text().splitlines()
        text.insert(2, json.dumps(cron))
        f.write_text("\n".join(text) + "\n")
        self.env.live_session()
        self.assertEqual(self.evaluate().action, "schedule")

    def test_scheduled_resume_timer_for_this_session_skips(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        d = self.env.paths.scheduled_resume
        d.mkdir(parents=True)
        (d / "claude-resume-myproj.txt").write_text(f"read /tmp/claude-1000/{SLUG}/{SID}/scratchpad/x.md")
        self.env.timers["claude-resume-myproj"] = RESET + 16 * 60
        r = self.evaluate()
        self.assertEqual(r.action, "skip")
        self.assertIn("scheduled-resume", r.reason)

    def test_scheduled_resume_for_another_session_does_not_skip(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        d = self.env.paths.scheduled_resume
        d.mkdir(parents=True)
        (d / "claude-resume-other.txt").write_text("a different project entirely")
        self.env.timers["claude-resume-other"] = RESET + 16 * 60
        self.assertEqual(self.evaluate().action, "schedule")

    def test_scheduled_resume_far_away_does_not_skip(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        d = self.env.paths.scheduled_resume
        d.mkdir(parents=True)
        (d / "claude-resume-late.txt").write_text(SID)
        self.env.timers["claude-resume-late"] = RESET + 3 * 3600
        self.assertEqual(self.evaluate().action, "schedule")

    def test_terminal_not_hosting_process_is_refused(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        self.env.qdbus_shell[(":1.9", "/Sessions/1")] = 4242  # some other shell
        d = self.evaluate()
        self.assertEqual(d.action, "skip")
        self.assertIn("not an ancestor", d.reason)

    def test_konsole_with_dbus_send_disabled_is_reported_not_attempted(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        base = self.env.runner

        def denied(cmd, timeout=10):
            if "sendText" in " ".join(cmd):
                return 1, "", "org.freedesktop.DBus.Error.AccessDenied Security sensitive DBus API is disabled"
            return base(cmd, timeout)

        self.env.runner = denied
        d = self.evaluate()
        self.assertEqual(d.action, "skip")
        self.assertIn("security sensitive DBus API", d.reason)

    def test_no_known_terminal_is_never_injected(self):
        self.env.transcript("limit_active.jsonl")
        self.env.process(600, env={"TERM": "xterm"})
        self.env.register(600)
        d = self.evaluate()
        self.assertEqual(d.action, "skip")
        self.assertIn("focused-window injection is never used", d.reason)

    def test_tmux_is_preferred_when_present(self):
        self.env.transcript("limit_active.jsonl")
        self.env.live_session(tmux=True)
        d = self.evaluate()
        self.assertEqual(d.action, "schedule", d.reason)
        self.assertEqual(d.target["kind"], "tmux")
        self.assertEqual(d.target["pane"], "%3")


class TestProcessMapping(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_registry_maps_pid_to_session(self):
        self.env.process(600)
        self.env.register(600)
        p = lib.find_claude_processes(self.env.paths)[SID]
        self.assertEqual((p.pid, p.how), (600, "sessions-registry"))

    def test_reused_pid_is_rejected(self):
        self.env.process(600, start="9000")
        self.env.register(600, start="1234")
        self.assertEqual(lib.find_claude_processes(self.env.paths), {})

    def test_registry_entry_for_dead_pid_is_rejected(self):
        self.env.register(700)
        self.assertEqual(lib.find_claude_processes(self.env.paths), {})

    def test_non_claude_process_is_rejected(self):
        self.env.process(600, comm="bash")
        self.env.register(600)
        self.assertEqual(lib.find_claude_processes(self.env.paths), {})

    def test_resume_argument(self):
        self.env.process(601, cmdline=["claude", "--dangerously-skip-permissions", "--resume", SID])
        p = lib.find_claude_processes(self.env.paths)[SID]
        self.assertEqual((p.pid, p.how), (601, "cmdline"))

    def test_open_tasks_directory_fd(self):
        d = self.env.process(602)
        fd = d / "fd"
        fd.mkdir()
        os.symlink(f"/tmp/claude-1000/{SLUG}/{SID}/tasks", fd / "69")
        p = lib.find_claude_processes(self.env.paths)[SID]
        self.assertEqual((p.pid, p.how), (602, "fd-tasks"))

    def test_cwd_falls_back_to_latest_transcript(self):
        self.env.transcript("limit_active.jsonl")
        self.env.process(603, cwd="/home/user/proj")
        p = lib.find_claude_processes(self.env.paths)[SID]
        self.assertEqual((p.pid, p.how), (603, "cwd-latest-transcript"))

    def test_native_host_helper_is_not_a_session(self):
        self.env.transcript("limit_active.jsonl")
        self.env.process(604, cmdline=["claude", "--chrome-native-host"])
        self.assertEqual(lib.find_claude_processes(self.env.paths), {})

    def test_ancestors(self):
        self.env.konsole_shell(500)
        self.env.process(600, ppid=500)
        self.assertEqual(lib.ancestors(self.env.paths, 600), [600, 500])

    def test_cwd_slug(self):
        self.assertEqual(lib.cwd_slug("/home/a/Desktop/Tools/Demo/Client Projects"),
                         "-home-a-Desktop-Tools-Demo-Client-Projects")


class TestScanAndFire(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        self.sched = FakeScheduler()

    def scan(self, **kw):
        return detect.scan(self.env.paths, now=NOW, scheduler=self.sched,
                           runner=self.env.runner, tz=TZ, **kw)

    def test_scan_is_idempotent(self):
        self.scan()
        self.scan()
        self.scan()
        self.assertEqual(len(self.sched.calls), 1)
        unit, fire_at, eid = self.sched.calls[0]
        self.assertEqual((fire_at, eid), (RESET + 300, "err1"))
        state = lib.load_state(self.env.paths)
        self.assertEqual(state["events"]["err1"]["status"], "scheduled")

    def test_dry_run_changes_nothing(self):
        res = self.scan(dry_run=True)
        self.assertEqual(res[0].action, "schedule")
        self.assertEqual(self.sched.calls, [])
        self.assertFalse(self.env.paths.state_file.exists())

    def test_missing_timer_is_rescheduled(self):
        self.scan()
        self.sched.live.clear()
        self.scan()
        self.assertEqual(len(self.sched.calls), 2)

    def test_fire_types_message_then_enter_into_the_konsole_tab(self):
        self.scan()
        r = fire.fire(self.env.paths, "err1", runner=self.env.runner, now=RESET + 300,
                      pause=0, verify_wait=0)
        self.assertEqual(r, "fired")
        sends = [c for c in self.env.calls
                 if "qdbus" in c[0] and c[3].endswith("sendText") and c[4] != ""]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0][1:3], [":1.9", "/Sessions/1"])
        self.assertEqual(sends[0][4], MSG)
        self.assertEqual(sends[1][4], "\r")
        self.assertEqual(lib.load_state(self.env.paths)["events"]["err1"]["status"], "fired")
        # a second firing is a no-op, never a second message
        before = len(self.env.calls)
        self.assertEqual(fire.fire(self.env.paths, "err1", runner=self.env.runner, pause=0, verify_wait=0), "noop")
        self.assertEqual(len(self.env.calls), before)

    def test_fire_aborts_if_session_already_moved_on(self):
        self.scan()
        t = self.env.paths.projects / SLUG / f"{SID}.jsonl"
        with open(t, "a") as f:
            f.write(json.dumps({"type": "user", "uuid": "u9", "timestamp": "2026-11-23T17:51:00.000Z",
                                "message": {"content": "continue"}}) + "\n")
        r = fire.fire(self.env.paths, "err1", runner=self.env.runner, pause=0, verify_wait=0)
        self.assertEqual(r, "superseded")
        self.assertFalse([c for c in self.env.calls
                          if c[-2:-1] == ["org.kde.konsole.Session.sendText"] and c[-1] != ""])

    def test_fire_aborts_if_process_died(self):
        self.scan()
        shutil.rmtree(self.env.proc / "600")
        r = fire.fire(self.env.paths, "err1", runner=self.env.runner, pause=0, verify_wait=0)
        self.assertEqual(r, "failed")

    def test_fire_dry_run_sends_nothing(self):
        self.scan()
        r = fire.fire(self.env.paths, "err1", dry_run=True, runner=self.env.runner)
        self.assertEqual(r, "dry-run")
        self.assertFalse([c for c in self.env.calls if "sendText" in " ".join(c) and c[-1] != ""])

    def test_tmux_send_uses_literal_keys_then_enter(self):
        self.env.live_session(tmux=True)
        self.scan()
        fire.fire(self.env.paths, "err1", runner=self.env.runner, pause=0, verify_wait=0)
        tm = [c for c in self.env.calls if c[0] == "tmux" and "send-keys" in c]
        self.assertEqual(tm[0][-3:], ["-l", "--", MSG])
        self.assertEqual(tm[1][-1], "Enter")


class TestPeerRoute(unittest.TestCase):
    """Native delivery: a frame written to the session's own inbox socket."""

    def setUp(self):
        import socket
        import threading
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.transcript("limit_active.jsonl")
        self.env.live_session()
        self.sock_path = str(self.env.root / "s.sock")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.sock_path)
        self.srv.listen(4)
        self.received = []

        def serve():
            try:
                while True:
                    conn, _ = self.srv.accept()
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    self.received.append(data.decode())
                    conn.close()
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(self.srv.close)
        reg = self.env.paths.sessions / "600.json"
        d = json.loads(reg.read_text())
        d["messagingSocketPath"] = self.sock_path
        reg.write_text(json.dumps(d))

    def add_mode(self, mode):
        t = self.env.paths.projects / SLUG / f"{SID}.jsonl"
        with open(t, "a") as f:
            f.write(json.dumps({"type": "permission-mode", "permissionMode": mode}) + "\n")

    def test_peer_is_the_preferred_route(self):
        procs = lib.find_claude_processes(self.env.paths)
        t = self.env.paths.projects / SLUG / f"{SID}.jsonl"
        d = lib.evaluate(self.env.paths, SID, t, procs, {"events": {}}, NOW, self.env.runner, TZ)
        self.assertEqual(d.action, "schedule", d.reason)
        self.assertEqual(d.target["kind"], "peer")

    def test_frame_has_the_wire_shape_claude_code_expects(self):
        self.add_mode("bypassPermissions")
        sched = FakeScheduler()
        detect.scan(self.env.paths, now=NOW, scheduler=sched, runner=self.env.runner, tz=TZ)
        r = fire.fire(self.env.paths, "err1", runner=self.env.runner, now=RESET + 300,
                      pause=0, verify_wait=0)
        self.assertEqual(r, "fired")
        import time
        for _ in range(50):
            if self.received:
                break
            time.sleep(0.02)
        frame = json.loads(self.received[0])
        self.assertEqual(frame["msgV"], 1)
        self.assertEqual(frame["type"], "user")
        self.assertEqual(frame["priority"], "next")
        self.assertIn(MSG, frame["message"]["content"])
        self.assertIn('from-mode="bypass"', frame["message"]["content"])
        # nothing was typed into a terminal
        self.assertFalse([c for c in self.env.calls if "sendText" in " ".join(c) and c[-1] != ""])

    def test_prompting_target_gets_no_bypass_claim(self):
        self.add_mode("default")
        sched = FakeScheduler()
        detect.scan(self.env.paths, now=NOW, scheduler=sched, runner=self.env.runner, tz=TZ)
        fire.fire(self.env.paths, "err1", runner=self.env.runner, now=RESET + 300,
                  pause=0, verify_wait=0)
        import time
        for _ in range(50):
            if self.received:
                break
            time.sleep(0.02)
        self.assertNotIn("from-mode", json.loads(self.received[0])["message"]["content"])

    def test_dead_socket_falls_back_to_the_terminal(self):
        sched = FakeScheduler()
        detect.scan(self.env.paths, now=NOW, scheduler=sched, runner=self.env.runner, tz=TZ)
        self.srv.close()
        os.unlink(self.sock_path)
        r = fire.fire(self.env.paths, "err1", runner=self.env.runner, now=RESET + 300,
                      pause=0, verify_wait=0)
        self.assertEqual(r, "fired")
        sends = [c for c in self.env.calls if "qdbus" in c[0] and c[3].endswith("sendText") and c[4] != ""]
        self.assertEqual(len(sends), 2)

    def test_registry_without_socket_has_no_peer_route(self):
        reg = self.env.paths.sessions / "600.json"
        d = json.loads(reg.read_text())
        del d["messagingSocketPath"]
        reg.write_text(json.dumps(d))
        kinds = [c["kind"] for c in lib.candidate_targets(self.env.paths, SID, 600)]
        self.assertEqual(kinds, ["konsole"])


class TestPathsFromEnv(unittest.TestCase):
    def test_env_overrides(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/x/claude", "XDG_DATA_HOME": "/x/data"}):
            p = lib.Paths.from_env()
        self.assertEqual(p.claude, Path("/x/claude"))
        self.assertEqual(p.state_file, Path("/x/data/auto-resume/state.json"))

    def test_defaults_follow_home(self):
        p = lib.Paths(home=Path("/h"))
        self.assertEqual(p.projects, Path("/h/.claude/projects"))
        self.assertEqual(p.data, Path("/h/.local/share/auto-resume"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

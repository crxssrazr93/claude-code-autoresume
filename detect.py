#!/usr/bin/env python3
"""Find Claude Code sessions stopped by a usage limit and schedule one resume
each, at the limit reset plus 5 minutes.

    detect.py scan [--dry-run] [--lookback-hours N] [--tz ZONE]   (default command)
    detect.py status
"""

import argparse
import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

HERE = Path(__file__).resolve().parent


def unit_name(session_id, reset):
    return f"claude-autoresume-{session_id[:8]}-{reset}"


class SystemdScheduler:
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


def candidate_transcripts(paths, now, lookback_hours):
    """Top level transcripts written recently."""
    cutoff = now - lookback_hours * 3600
    out = []
    if not paths.projects.is_dir():
        return out
    for proj in paths.projects.iterdir():
        if not proj.is_dir():
            continue
        try:
            for f in proj.glob("*.jsonl"):
                if f.stat().st_mtime >= cutoff:
                    out.append(f)
        except OSError:
            continue
    return out


def scan(paths, now=None, dry_run=False, lookback_hours=48, scheduler=None,
         runner=lib.run, echo=False, tz=None):
    """Returns a list of Decisions. Writes state and timers unless dry_run."""
    now = now if now is not None else time.time()
    scheduler = scheduler or SystemdScheduler()
    state = lib.load_state(paths)
    events = state.setdefault("events", {})
    procs = lib.find_claude_processes(paths)
    results = []
    changed = False

    # Reconcile: a scheduled event whose timer vanished is rescheduled or failed.
    for eid, e in list(events.items()):
        if e.get("status") != "scheduled":
            continue
        if scheduler.active(e["unit"]):
            continue
        if e["fire_at"] > now + 2 and not dry_run:
            ok, err = scheduler.schedule(e["unit"], e["fire_at"], eid)
            lib.log(paths, f"reschedule {e['unit']}: {'ok' if ok else err}", echo)
        elif now > e["fire_at"] + 60:
            e["status"] = "failed"
            e["attempts"] = e.get("attempts", 0) + 1
            e["reason"] = "timer vanished without firing"
            changed = True
            lib.log(paths, f"{e['unit']} vanished without firing", echo)

    seen_sessions = set()
    for tpath in sorted(candidate_transcripts(paths, now, lookback_hours)):
        sid = tpath.stem
        if sid in seen_sessions:
            continue
        seen_sessions.add(sid)
        try:
            d = lib.evaluate(paths, sid, tpath, procs, state, now, runner, tz)
        except Exception as exc:  # one bad transcript must never stop the scan
            lib.log(paths, f"error evaluating {sid}: {exc!r}", echo)
            continue
        if d is None:
            continue
        results.append(d)
        eid = d.event["event_id"]
        if d.action == "skip":
            already = events.get(eid, {})
            if already.get("reason") == d.reason:
                continue  # said it once already
            lib.log(paths, f"skip {sid[:8]}: {d.reason}", echo or dry_run)
            if not dry_run and not d.reason.startswith("already handled"):
                events[eid] = {"session_id": sid, "status": "skipped", "reason": d.reason,
                               "transient": d.transient, "reset": d.event["reset"],
                               "seen_at": now, "attempts": already.get("attempts", 0)}
                changed = True
            continue
        unit = unit_name(sid, d.event["reset"])
        lib.log(paths, f"{'WOULD schedule' if dry_run else 'schedule'} {sid[:8]} at "
                       f"{lib.fmt(d.fire_at)} (reset {lib.fmt(d.event['reset'])} via "
                       f"{d.event['reset_source']}): {d.reason}", echo or dry_run)
        if dry_run:
            continue
        ok, err = scheduler.schedule(unit, d.fire_at, eid, runner)
        prev = events.get(eid, {})
        if ok:
            events[eid] = {"session_id": sid, "pid": d.pid, "status": "scheduled", "unit": unit,
                           "reset": d.event["reset"], "fire_at": d.fire_at, "target": d.target,
                           "limit_type": d.event["limit_type"], "created_at": now,
                           "attempts": prev.get("attempts", 0)}
        else:
            lib.log(paths, f"systemd-run failed for {unit}: {err}", echo)
            events[eid] = {"session_id": sid, "status": "failed", "reason": err,
                           "reset": d.event["reset"], "attempts": prev.get("attempts", 0) + 1}
        changed = True

    if changed and not dry_run:
        lib.save_state(paths, state)
    return results


def status(paths, runner=lib.run, now=None):
    now = now if now is not None else time.time()
    state = lib.load_state(paths)
    rows = [(e["fire_at"], eid, e) for eid, e in state.get("events", {}).items()
            if e.get("status") == "scheduled"]
    if not rows:
        print("No pending resumes.")
    for fire_at, eid, e in sorted(rows):
        active = SystemdScheduler().active(e["unit"], runner)
        print(f"{lib.fmt(fire_at)}  session {e['session_id'][:8]}  pid {e.get('pid')}  "
              f"{e['unit']}  timer={'active' if active else 'MISSING'}  "
              f"({int((fire_at - now) / 60)} min away)")
    recent = [(e.get("fired_at") or e.get("seen_at") or 0, e) for e in state.get("events", {}).values()
              if e.get("status") in ("fired", "failed", "superseded")]
    for ts, e in sorted(recent, key=lambda x: x[0])[-5:]:
        print(f"  last: {e['status']:10} session {e['session_id'][:8]}  {e.get('reason', '')}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", nargs="?", default="scan", choices=["scan", "status"])
    ap.add_argument("--dry-run", action="store_true", help="print decisions, change nothing")
    ap.add_argument("--lookback-hours", type=float, default=48)
    ap.add_argument("--tz", help="IANA zone for reset texts that name none (default: system zone)")
    ap.add_argument("--home", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    paths = lib.Paths(home=Path(args.home)) if args.home else lib.Paths.from_env()
    tz = ZoneInfo(args.tz) if args.tz else None
    if args.command == "status":
        status(paths)
        return 0
    paths.data.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        results = scan(paths, dry_run=True, lookback_hours=args.lookback_hours, echo=True, tz=tz)
        procs = lib.find_claude_processes(paths)
        print(f"-- dry run: {len(procs)} live claude sessions, "
              f"{len(results)} stopped on a usage limit, "
              f"{sum(1 for r in results if r.action == 'schedule')} would be scheduled")
        return 0
    with open(paths.data / "scan.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another scan is running
        scan(paths, lookback_hours=args.lookback_hours, tz=tz)
    return 0


if __name__ == "__main__":
    sys.exit(main())

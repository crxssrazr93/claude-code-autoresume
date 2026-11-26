#!/usr/bin/env python3
"""Find Claude Code sessions stopped by a usage limit and schedule one resume
each, at the limit reset plus 5 minutes.

    detect.py scan [--dry-run] [--lookback-hours N] [--tz ZONE]   (default command)
    detect.py status
"""

import argparse
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402
from platform_linux import SystemdScheduler  # noqa: E402,F401  (kept for older imports)

HERE = Path(__file__).resolve().parent


def unit_name(session_id, reset):
    return f"claude-autoresume-{session_id[:8]}-{reset}"


def run_due_polled(paths, event_ids, runner):
    """Deliver resumes that are due when the scheduler has no timer of its own."""
    import fire
    for eid in event_ids:
        fire.fire(paths, eid, runner=runner)


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
    scheduler = scheduler or lib.current_platform().Scheduler()
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

    # Schedulers that leave a job behind (launchd, Task Scheduler) get it removed
    # once the event is done. This runs in a later scan, never inside the job itself.
    if getattr(scheduler, "needs_cleanup", False) and not dry_run:
        for e in events.values():
            if e.get("unit") and e.get("status") in ("fired", "failed", "superseded") \
                    and not e.get("cleaned"):
                scheduler.cancel(e["unit"], runner)
                e["cleaned"] = True
                changed = True

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
    if getattr(scheduler, "polls", False) and not dry_run:
        due = [eid for eid, e in events.items()
               if e.get("status") == "scheduled" and e.get("fire_at", now + 1) <= now]
        run_due_polled(paths, due, runner)
    return results


def status(paths, runner=lib.run, now=None):
    now = now if now is not None else time.time()
    print(lib.describe_environment(paths))
    scheduler = lib.current_platform().Scheduler()
    state = lib.load_state(paths)
    rows = [(e["fire_at"], eid, e) for eid, e in state.get("events", {}).items()
            if e.get("status") == "scheduled"]
    if not rows:
        print("No pending resumes.")
    for fire_at, eid, e in sorted(rows):
        active = scheduler.active(e["unit"], runner)
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
        if not lib.try_lock(lock):
            return 0  # another scan is running
        try:
            scan(paths, lookback_hours=args.lookback_hours, tz=tz)
        except ImportError as e:  # psutil missing on macOS or Windows
            lib.log(paths, f"scan cannot run: {e}", echo=True)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fired by a one-shot systemd timer. Re-verifies everything, then types the
resume message into the session's own terminal.

    fire.py <event_id> [--dry-run]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def fire(paths, event_id, dry_run=False, runner=lib.run, now=None, pause=None, verify_wait=15):
    now = now if now is not None else time.time()
    state = lib.load_state(paths)
    e = state.get("events", {}).get(event_id)
    if not e or e.get("status") != "scheduled":
        lib.log(paths, f"fire {event_id}: nothing to do ({e and e.get('status')})")
        return "noop"
    sid = e["session_id"]
    transcript = next(iter(paths.projects.glob(f"*/{sid}.jsonl")), None)

    def finish(status, reason):
        e.update(status=status, reason=reason, fired_at=now)
        if status == "failed":
            e["attempts"] = e.get("attempts", 0) + 1
        if not dry_run:
            lib.save_state(paths, state)
        lib.log(paths, f"fire {sid[:8]}: {status} ({reason})", echo=dry_run)
        return status

    if transcript is None:
        return finish("failed", "transcript not found")
    if lib.last_turn_event_id(transcript) != event_id:
        return finish("superseded", "the session moved on by itself, nothing to resume")
    procs = lib.find_claude_processes(paths)
    proc = procs.get(sid)
    if not proc:
        return finish("failed", "claude process is gone")
    cands = lib.candidate_targets(paths, sid, proc.pid, transcript)
    if not cands:
        return finish("failed", "no inbox socket and no addressable terminal")
    target, detail, problems = None, "", []
    for cand in cands:
        ok, why = lib.verify_target(paths, proc.pid, cand, runner)
        if not ok:
            problems.append(f"{cand['kind']}: {why}")
            continue
        if dry_run:
            lib.log(paths, f"DRY RUN would send to {sid[:8]} via {cand['kind']} ({why})", echo=True)
            return "dry-run"
        sent, err = lib.send_message(cand, lib.MESSAGE, runner, pause)
        if sent:  # a delivered message is never retried on another route
            target, detail = cand, why
            break
        problems.append(f"{cand['kind']}: send failed: {err}")
    if not target:
        return finish("failed", "no route delivered: " + "; ".join(problems))
    # Confirm the session picked it up: the last turn must no longer be the error.
    time.sleep(verify_wait)
    if lib.last_turn_event_id(transcript) == event_id:
        # Never retry a message that was delivered: a double resume is worse than a late one.
        return finish("fired", f"sent via {target['kind']}, reaction unconfirmed after {verify_wait}s")
    return finish("fired", f"sent via {target['kind']} ({detail})")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("event_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--home", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    paths = lib.Paths(home=Path(args.home)) if args.home else lib.Paths.from_env()
    result = fire(paths, args.event_id, args.dry_run)
    return 0 if result in ("fired", "noop", "superseded", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())

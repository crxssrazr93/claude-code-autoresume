"""Show every live Claude Code session and whether auto-resume can reach it (read-only).

    python3 -I reach.py

The first line names the platform, the process source and the registry directory.
Include it when you report a problem.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402


def main():
    paths = lib.Paths.from_env()
    print(lib.describe_environment(paths))
    try:
        procs = lib.find_claude_processes(paths)
    except ImportError as e:
        print(f"cannot list processes: {e}")
        return 1
    if not procs:
        print("no live Claude Code sessions found")
        return 0
    bad = 0
    for sid, proc in sorted(procs.items()):
        transcript = next(paths.projects.glob(f"*/{sid}.jsonl"), None)
        cands = lib.candidate_targets(paths, sid, proc.pid, transcript)
        ok, why = False, lib.current_platform().no_route_reason
        for cand in cands:
            ok, why = lib.verify_target(paths, proc.pid, cand, lib.run)
            if ok:
                why = f"{cand['kind']}: {why}"
                break
        bad += not ok
        print(f"{'OK  ' if ok else 'NO  '} {sid[:8]} pid {proc.pid:<8} {proc.cwd[:50]:<50} {why}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

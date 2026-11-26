#!/usr/bin/env python3
"""Post a cross-session message to one session's inbox socket, no Claude involved.

    peer.py <session-id> <text...>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

if len(sys.argv) < 3:
    sys.exit(__doc__)
sock = lib.peer_socket(lib.Paths.from_env(), sys.argv[1])
if not sock:
    sys.exit("no live inbox socket for that session")
ok, detail = lib.send_peer(sock, " ".join(sys.argv[2:]))
print(detail)
sys.exit(0 if ok else 1)

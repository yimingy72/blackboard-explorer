"""Hold one advisory work-directory lock for the lifetime of the stdin pipe."""

import fcntl
import sys

with open(sys.argv[1], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(73) from None
    print("locked", flush=True)
    while sys.stdin.buffer.read(4096):
        pass

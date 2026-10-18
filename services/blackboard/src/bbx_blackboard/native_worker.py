"""Start the official decoder without service secrets, privileges or network syscalls."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import resource
import sys


def main() -> None:
    if sys.platform != "linux" or len(sys.argv) not in {2, 3}:
        raise SystemExit("Native analysis requires the Linux decoder sandbox")
    memory_limit = int(sys.argv[2]) if len(sys.argv) == 3 else 1024**3
    if memory_limit not in {1024**3, 2 * 1024**3}:
        raise SystemExit("Invalid native decoder memory budget")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    if os.geteuid() == 0:
        owner = os.stat(os.environ["HOME"]).st_uid
        if not owner:
            raise SystemExit("Native analysis requires an unprivileged cache owner")
        os.setgroups([])
        os.setgid(owner)
        os.setuid(owner)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise SystemExit("Native analysis privilege restriction failed")
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise SystemExit("Native analysis network sandbox is unavailable")
    seccomp = ctypes.CDLL(library)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)
    if not context:
        raise SystemExit("Native analysis network sandbox could not be created")
    try:
        for name in (b"socket", b"socketpair", b"socketcall", b"connect"):
            syscall = seccomp.seccomp_syscall_resolve_name(name)
            if (
                syscall >= 0
                and seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0) != 0
            ):
                raise SystemExit("Native analysis network sandbox rule failed")
        if seccomp.seccomp_load(context) != 0:
            raise SystemExit("Native analysis network sandbox could not be loaded")
    finally:
        seccomp.seccomp_release(context)
    os.execve(sys.argv[1], [sys.argv[1], "-"], dict(os.environ))


if __name__ == "__main__":
    main()

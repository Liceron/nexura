# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Liceron
"""Native cBPF generation using the Linux UAPI; no compiler or Python bindings.

Numbers come from asm/unistd_64.h and asm-generic/unistd.h. Unknown architectures
fail closed. The architecture check also rejects compat ABIs (including x32).
"""

from __future__ import annotations

import errno
import platform
import struct


def default_bpf(machine: str | None = None) -> bytes:
    machine = machine or platform.machine()
    if machine == "x86_64":
        arch = 0xC000003E
        denied = [
            101,
            165,
            166,
            248,
            249,
            250,
            272,
            298,
            308,
            310,
            311,
            312,
            319,
            321,
            323,
            29,
            30,
            31,
            67,
            68,
            69,
            70,
            71,  # SysV IPC is not AS-accounted.
        ]
        sockets = (41, 53)
    elif machine in {"aarch64", "arm64"}:
        arch = 0xC00000B7
        denied = [
            117,
            40,
            39,
            217,
            218,
            219,
            97,
            241,
            268,
            270,
            271,
            272,
            279,
            280,
            282,
            194,
            195,
            196,
            197,
            186,
            187,
            188,
            189,
        ]
        sockets = (198, 199)
    else:
        raise ValueError(f"No default bubblewrap seccomp policy for {machine}")
    # Shared modern syscall numbers: io_uring, new mount API, memfd_secret.
    denied += [425, 426, 427, 428, 429, 430, 431, 432, 433, 442, 447]
    instructions: list[tuple[int, int, int, int]] = []

    def emit(code: int, jt: int = 0, jf: int = 0, k: int = 0) -> None:
        instructions.append((code, jt, jf, k))

    deny = 0x00050000 | errno.EPERM  # SECCOMP_RET_ERRNO
    emit(0x20, k=4)  # LD W ABS seccomp_data.arch
    emit(0x15, jt=1, k=arch)  # JEQ native arch
    emit(0x06, k=deny)
    emit(0x20, k=0)  # LD W ABS seccomp_data.nr
    if machine == "x86_64":
        emit(0x45, jf=1, k=0x40000000)  # JSET __X32_SYSCALL_BIT
        emit(0x06, k=deny)
    for nr in sorted(set(denied)):
        emit(0x15, jf=1, k=nr)
        emit(0x06, k=deny)
    for nr in sockets:
        emit(0x15, jf=4, k=nr)
        emit(0x20, k=16)  # Low 32 bits of args[0] (socket domain).
        emit(0x15, jt=1, k=1)  # AF_UNIX
        emit(0x06, k=deny)
        emit(0x06, k=0x7FFF0000)  # SECCOMP_RET_ALLOW
    emit(0x06, k=0x7FFF0000)
    return b"".join(struct.pack("=HBBI", *insn) for insn in instructions)


def setup_launcher(inner_args: list[str], *, tmpfs_bytes: int, inodes: int) -> bytes:
    """Opt-in trusted outer PID 1: cap metadata, then enter the payload user namespace.

    bwrap has no nr_inodes option. Only this fixed launcher has CAP_SYS_ADMIN in
    the outer user namespace. Inner bwrap drops it, disables further userns, and
    loads the mandatory policy before starting any request-controlled code.
    Ubuntu's default AppArmor restrictions can deny this remount. It is never
    used by the default profile; an explicit inode-cap request fails closed.
    """
    return (
        "import ctypes, os, sys\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        f"options = b'size={tmpfs_bytes},nr_inodes={inodes}'\n"
        "if libc.mount(None, b'/tmp', b'tmpfs', 32 | 2 | 4, options) != 0:\n"
        "    raise OSError(ctypes.get_errno(), 'tmpfs inode remount failed')\n"
        f"args = {inner_args!r}\n"
        "args.extend(sys.argv[1:])\n"
        "for path in ['/nexura-internal/default.bpf', '/nexura-internal/operator.bpf']:\n"
        "    if os.path.exists(path):\n"
        "        fd = os.open(path, os.O_RDONLY)\n"
        "        os.set_inheritable(fd, True)\n"
        "        args[1:1] = ['--add-seccomp-fd', str(fd)]\n"
        "os.execv(args[0], args)\n"
    ).encode()

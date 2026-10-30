# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Linux Landlock boundary for a private filesystem worker thread.

The Linux UAPI is described in linux/landlock.h (Linux-syscall-note). Only the
worker restricts itself; the calling thread and process defaults are unchanged.
No user code, shell, scripts or arbitrary operations are accepted by this module.
"""

from __future__ import annotations

import ctypes
import os
import platform

from nexura_agents.errors import SDKValidationError


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _Beneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def restrict_worker(root_fd: int) -> None:
    """Enforce filesystem access beneath root at kernel operations, or fail closed.

    ABI 3 includes REFER (cross-directory rename/link) and TRUNCATE. All known
    filesystem rights through ABI 3 are handled; only ordinary file/directory
    reads and writes are granted. Executables, links and special files are denied.
    """
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise SDKValidationError(
            "Disk workspaces require Linux Landlock ABI >= 3; use memory mode."
        )
    libc = ctypes.CDLL(None, use_errno=True)

    def check(result: int) -> int:
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    abi = check(libc.syscall(444, 0, 0, 1))
    if abi < 3:
        raise SDKValidationError(
            "Disk workspaces require Linux Landlock ABI >= 3; use memory mode."
        )
    handled = (1 << 15) - 1
    allowed = sum(1 << bit for bit in (1, 2, 3, 4, 5, 7, 8, 13, 14))
    rules = _Ruleset(handled)
    fd = check(libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0))
    try:
        beneath = _Beneath(allowed, root_fd)
        check(libc.syscall(445, fd, 1, ctypes.byref(beneath), 0))
        check(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS, this worker only
        check(libc.syscall(446, fd, 0))
    finally:
        os.close(fd)

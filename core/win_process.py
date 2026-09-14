"""
Windows process QoS helpers for encode workloads.

Windows 11 may put background / windowless workers into EcoQoS (Efficiency Mode),
which parks them on E-cores and can leave a 20-thread CPU running only ~4 SVT
workers until a foreground window is focused again. Opt the encode tree out of
execution-speed throttling and raise priority slightly so encodes stay saturated.
"""

from __future__ import annotations

import sys
from typing import Optional


def boost_process(pid: Optional[int]) -> bool:
    """
    Disable EcoQoS / power throttling and set ABOVE_NORMAL priority for pid.
    No-op on non-Windows or invalid pid. Returns True if both calls succeeded.
    """
    if sys.platform != "win32" or not pid:
        return False
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    PROCESS_SET_INFORMATION = 0x0200
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
    ProcessPowerThrottling = 4
    PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
    PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1

    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [
            ("Version", wintypes.ULONG),
            ("ControlMask", wintypes.ULONG),
            ("StateMask", wintypes.ULONG),
        ]

    handle = kernel32.OpenProcess(
        PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid_i,
    )
    if not handle:
        return False

    ok_throttle = False
    ok_prio = False
    try:
        state = PROCESS_POWER_THROTTLING_STATE(
            Version=PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            ControlMask=PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            StateMask=0,  # explicitly disable execution-speed throttling
        )
        ok_throttle = bool(
            kernel32.SetProcessInformation(
                handle,
                ProcessPowerThrottling,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
        ok_prio = bool(kernel32.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False
    finally:
        kernel32.CloseHandle(handle)

    return bool(ok_throttle and ok_prio)


def boost_current_process() -> bool:
    """Apply boost_process to the calling process."""
    try:
        import os
        return boost_process(os.getpid())
    except Exception:
        return False

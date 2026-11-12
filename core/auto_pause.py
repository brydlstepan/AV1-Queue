"""
Auto pause / resume: run the queue while you're away, step aside while you use the PC.

Two independent Settings toggles, OR'd together — the queue runs while ANY
enabled condition holds and is auto-paused when none does:

- ``auto_pause_lock``: the Windows session is locked
- ``auto_pause_cpu``:  other programs' CPU (this server's own process tree —
  HandBrakeCLI, ffmpeg, vspipe — excluded) is below ``auto_pause_cpu_threshold``

The CPU condition has hysteresis: busy after a 60 s average at/above the
threshold, idle again after a 2 min average below it.

Only a running queue is touched (never Start). A manual Pause is never resumed;
a manual Resume during an auto-pause holds until the condition clears once
(QueueManager.auto_pause_overridden).
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

POLL_SECONDS = 5.0
CPU_BUSY_WINDOW = 60.0    # average this long at/above the threshold → busy
CPU_IDLE_WINDOW = 120.0   # average this long below it → idle again
LOCK_SETTLE = 10.0        # locked this long before it counts (UAC's secure desktop also reads as locked)


def session_locked() -> Optional[bool]:
    """True if the interactive desktop is the lock screen (Winlogon), None if unknown.

    Works from the windowless tray process: it only needs to run in the user's
    session, not to own a window.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    DESKTOP_READOBJECTS = 0x0001
    UOI_NAME = 2
    h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not h:
        return True  # access denied to the input desktop = Winlogon (locked / UAC)
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return None
        return buf.value.lower() != "default"
    finally:
        user32.CloseDesktop(h)


class OtherCpuSampler:
    """Share of total CPU used by everything except this server's process tree."""

    def __init__(self) -> None:
        self._last: Optional[Tuple[float, float, float, Dict[int, float]]] = None

    def sample(self) -> Optional[float]:
        import psutil

        t = psutil.cpu_times()
        total = sum(t)
        busy = total - t.idle
        wall = time.time()
        own: Dict[int, float] = {}
        new_pids = set()
        try:
            me = psutil.Process()
            procs = [me] + me.children(recursive=True)
        except Exception:
            procs = []
        last = self._last
        for p in procs:
            try:
                ct = p.cpu_times()
                own[p.pid] = ct.user + ct.system
                if last is not None and p.pid not in last[3] and p.create_time() >= last[2]:
                    new_pids.add(p.pid)  # started since the last sample: all its time is new
            except Exception:
                pass
        self._last = (total, busy, wall, own)
        if last is None:
            return None
        d_total = total - last[0]
        if d_total <= 0:
            return None
        d_busy = busy - last[1]
        d_own = 0.0
        for pid, secs in own.items():
            prev = 0.0 if pid in new_pids else last[3].get(pid, secs)
            d_own += max(0.0, secs - prev)
        return max(0.0, min(100.0, (d_busy - d_own) / d_total * 100.0))


class AutoPauseMonitor:
    def __init__(
        self,
        queue_mgr,
        load_settings: Callable[[], dict],
        lock_probe: Callable[[], Optional[bool]] = session_locked,
        cpu_probe: Optional[Callable[[], Optional[float]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.q = queue_mgr
        self._load_settings = load_settings
        self._lock_probe = lock_probe
        self._cpu_probe = cpu_probe or OtherCpuSampler().sample
        self._clock = clock
        self._samples: Deque[Tuple[float, float]] = deque()
        self._cpu_busy: Optional[bool] = None
        self._locked_since: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="auto-pause", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.tick()
            except Exception as e:
                print(f"[auto-pause] {e}")

    def _avg(self, now: float, window: float) -> Optional[float]:
        """Average over the last `window` seconds, None until that much history exists."""
        if not self._samples or now - self._samples[0][0] < window - POLL_SECONDS:
            return None
        vals = [v for t, v in self._samples if now - t <= window]
        return sum(vals) / len(vals) if vals else None

    def tick(self) -> None:
        s = self._load_settings()
        use_lock = bool(s.get("auto_pause_lock"))
        use_cpu = bool(s.get("auto_pause_cpu"))
        try:
            threshold = float(s.get("auto_pause_cpu_threshold") or 50)
        except (TypeError, ValueError):
            threshold = 50.0
        now = self._clock()

        if not (use_lock or use_cpu):
            self._samples.clear()
            self._cpu_busy = None
            if self.q.auto_pause_reason:
                self.q.resume_queue(auto="auto-pause turned off")
            self.q.auto_pause_overridden = False
            return

        locked = None
        if use_lock:
            raw = self._lock_probe()
            if raw:
                if self._locked_since is None:
                    self._locked_since = now
                locked = now - self._locked_since >= LOCK_SETTLE
            else:
                self._locked_since = None
                locked = False if raw is False else None

        cpu_avg = None
        if use_cpu:
            v = self._cpu_probe()
            if v is not None:
                self._samples.append((now, v))
            while self._samples and now - self._samples[0][0] > CPU_IDLE_WINDOW:
                self._samples.popleft()
            busy_avg = self._avg(now, CPU_BUSY_WINDOW)
            idle_avg = self._avg(now, CPU_IDLE_WINDOW)
            if self._cpu_busy is not True and busy_avg is not None and busy_avg >= threshold:
                self._cpu_busy = True
            elif self._cpu_busy is not False and idle_avg is not None and idle_avg < threshold:
                self._cpu_busy = False
            cpu_avg = busy_avg
        else:
            self._samples.clear()
            self._cpu_busy = None

        conds = []
        if use_lock:
            conds.append(locked)
        if use_cpu:
            conds.append(None if self._cpu_busy is None else not self._cpu_busy)
        if any(c is True for c in conds):
            should_run = True
        elif all(c is False for c in conds):
            should_run = False
        else:
            return  # still measuring / unknown: leave the queue as it is

        if should_run:
            self.q.auto_pause_overridden = False  # the condition cleared: a manual Resume's hold ends
            if self.q.auto_pause_reason and self.q.is_paused:
                why = "PC locked" if locked else f"other apps' CPU below {threshold:.0f}%"
                self.q.resume_queue(auto=why)
            return

        if not self.q.is_running or self.q.is_paused or self.q.auto_pause_overridden:
            return
        parts = []
        if use_lock:
            parts.append("PC unlocked")
        if use_cpu:
            parts.append(f"other apps' CPU {cpu_avg:.0f}% >= {threshold:.0f}%" if cpu_avg is not None
                         else f"other apps' CPU >= {threshold:.0f}%")
        self.q.pause_queue(auto=", ".join(parts))

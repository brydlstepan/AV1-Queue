"""
In-place server restart (POST /api/server/restart, the sidebar button).

The running server starts a replacement ``core/run_server.py`` first and only
then exits, so a failed launch leaves the current server untouched. The
replacement waits for this process to be gone before it binds the port
(AV1QUEUE_WAIT_PID, handled in run_server.py) — on Windows a second socket can
bind a port that is still listening, so it must not race the old process.

Environment (AV1QUEUE_PORT / _HOST / _USERNAME …) is inherited, and stdout /
stderr keep pointing at wherever this process's went (logs/server.log under the
tray launcher, the console under start_queue.ps1), so a restart looks the same
as the original launch. An encode that was running is killed with the old
process; on startup the queue marks it "Interrupted — ready to retry".
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
PID_FILE = BASE_DIR / "logs" / "server.pid"


def _stream(handle):
    """A file descriptor the child can inherit, or DEVNULL if there isn't one."""
    try:
        return handle.fileno()
    except Exception:
        return subprocess.DEVNULL


def spawn_replacement() -> int:
    """Launch the replacement server (it waits for this PID to exit). Returns its PID."""
    env = dict(os.environ)
    env["AV1QUEUE_WAIT_PID"] = str(os.getpid())

    kwargs = {}
    try:
        on_console = sys.stdout.isatty()
    except Exception:
        on_console = False
    if os.name == "nt" and not on_console:
        # Tray launcher: no window, own process group. A foreground run keeps the
        # shared console so Ctrl+C and the log output still work.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "core" / "run_server.py")],
        cwd=str(BASE_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=_stream(sys.stdout),
        stderr=_stream(sys.stderr),
        **kwargs,
    )

    # The tray launcher tracks the server by this file; keep it pointing at a live process
    try:
        if PID_FILE.exists():
            PID_FILE.write_text(str(proc.pid), encoding="ascii")
    except Exception:
        pass
    return proc.pid


def schedule_exit(before_exit: Optional[Callable[[], None]] = None, delay: float = 0.6) -> None:
    """Exit this process shortly (after the HTTP response has flushed)."""

    def _run() -> None:
        time.sleep(delay)
        if before_exit is not None:
            try:
                before_exit()  # e.g. kill encoder children so they don't outlive us
            except Exception as e:
                print(f"[restart] cleanup before exit failed: {e}")
        os._exit(0)

    threading.Thread(target=_run, daemon=True, name="server-restart").start()

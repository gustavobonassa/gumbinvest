"""Uvicorn hosted in a thread of the tray process.

One process means one log file and a deterministic shutdown: setting
``should_exit`` on the server object is all it takes. Binds ``0.0.0.0`` so a
phone on the same network can reach the dashboard.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from app.core.logging import get_logger

logger = get_logger(__name__)


def pick_port(preferred: int, attempts: int = 10) -> int:
    """``preferred``, or the first free port after it when something owns it."""
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("0.0.0.0", candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError(f"no free port in {preferred}-{preferred + attempts - 1}")


class ServerThread:
    def __init__(self, port: int) -> None:
        self.port = port
        config = uvicorn.Config(
            "app.main:app", host="0.0.0.0", port=port, log_config=None, access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def wait_until_healthy(self, timeout: float = 300.0) -> bool:
        """Poll ``/api/health`` until the app serves.

        Normally answers within seconds — the heavy startup pass (downloads,
        auto-import, reclassify) runs in a background thread after the server
        starts serving. The generous timeout is a safety margin, not the
        expected wait.
        """
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/api/health"
        while time.monotonic() < deadline:
            if not self.thread.is_alive():
                return False
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return True
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.5)
        return False

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)

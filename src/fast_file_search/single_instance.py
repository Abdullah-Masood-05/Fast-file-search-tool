"""One GUI instance per user (design G.2).

The first instance takes a ``QLockFile`` and listens on a ``QLocalServer``. Later launches
find the lock taken, send ``{"cmd": "activate", "query": ...}`` to the server and exit.
The first instance emits ``activated(query)`` so the window can raise itself.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

_MAX_MESSAGE = 64 * 1024


def server_name(data_dir: Path) -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    digest = hashlib.sha1(f"{user}\0{data_dir}".encode("utf-8", "surrogatepass")).hexdigest()
    return "fast-file-search-pro-" + digest[:12]


class SingleInstance(QObject):
    """``acquire()`` returns True for the first instance. False means another one is
    running and has been asked to activate (the caller should exit with code 0)."""

    activated = Signal(str)  # query, or "" when the launch carried none

    def __init__(self, query: str = "", data_dir: Optional[Path] = None, parent=None):
        super().__init__(parent)
        if data_dir is None:
            from .utils.platform_utils import get_state_dir
            data_dir = get_state_dir()
        self._dir = Path(data_dir)
        self._query = query or ""
        self._lock: Optional[QLockFile] = None
        self._server: Optional[QLocalServer] = None
        self._buffers: dict = {}
        self.name = server_name(self._dir)

    # ------------------------------------------------------------------ public
    def acquire(self) -> bool:
        self._dir.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self._dir / "gui.lock"))
        lock.setStaleLockTime(0)
        if lock.tryLock(100):
            self._lock = lock
            self._listen()
            return True
        self._send_activate()
        return False

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None

    # ------------------------------------------------------------------ server
    def _listen(self) -> None:
        QLocalServer.removeServer(self.name)  # stale socket file from a crash (POSIX)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.name):
            log.warning("single-instance server could not listen: %s", server.errorString())
            return
        server.newConnection.connect(self._on_connection)
        self._server = server

    def _on_connection(self) -> None:
        server = self._server
        while server is not None and server.hasPendingConnections():
            sock = server.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._on_ready(s, final=True))
            if sock.bytesAvailable():
                self._on_ready(sock)

    def _on_ready(self, sock: QLocalSocket, final: bool = False) -> None:
        buf = self._buffers.get(sock)
        if buf is None:
            return
        buf += bytes(sock.readAll())
        if len(buf) > _MAX_MESSAGE:
            self._drop(sock)
            return
        if b"\n" in buf or final:
            line = bytes(buf).split(b"\n", 1)[0]
            self._drop(sock)
            self._handle(line)

    def _drop(self, sock: QLocalSocket) -> None:
        self._buffers.pop(sock, None)
        try:
            sock.disconnectFromServer()
            sock.deleteLater()
        except RuntimeError:
            pass

    def _handle(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            log.warning("single-instance: ignoring malformed message")
            return
        if isinstance(msg, dict) and msg.get("cmd") == "activate":
            query = msg.get("query")
            self.activated.emit(query if isinstance(query, str) else "")

    # ------------------------------------------------------------------ client
    def _send_activate(self, timeout: float = 1.5) -> bool:
        """Connect to the first instance (it may still be starting its server)
        and send the activate message. Returns True when it was delivered."""
        payload = json.dumps({"cmd": "activate", "query": self._query}).encode("utf-8") + b"\n"
        deadline = time.monotonic() + timeout
        while True:
            sock = QLocalSocket()
            sock.connectToServer(self.name)
            if sock.waitForConnected(200):
                sock.write(QByteArray(payload))
                ok = sock.waitForBytesWritten(500)
                sock.disconnectFromServer()
                if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                    sock.waitForDisconnected(200)
                return ok
            if time.monotonic() >= deadline:
                log.warning("single-instance: first instance did not answer")
                return False
            time.sleep(0.05)

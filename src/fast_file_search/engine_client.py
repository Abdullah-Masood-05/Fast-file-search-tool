"""GUI side of the engine (design B.1, B.2): spawns the engine process, sends
requests from a sender thread (the GUI thread never blocks on the pipe),
receives on a reader QThread and re-emits protocol objects as Qt signals on
the GUI thread. Supervises the engine: on an unexpected exit it restarts after
1 s, 5 s and 30 s (at most 3 restarts per 10 minutes), replays ``Configure``
and the current search, and otherwise reports ``engine_state='failed'``.

No sqlite3 and no index work here.
"""
from __future__ import annotations

import dataclasses
import logging
import multiprocessing
import queue
import threading
import time
from typing import Optional, Sequence

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from .engine import protocol as P

_log = logging.getLogger("fast_file_search.engine_client")

RESTART_DELAYS = (1.0, 5.0, 30.0)
RESTART_WINDOW = 600.0
_BACKLOG_MAX = 200


class _Reader(QThread):
    received = Signal(int, object)
    closed = Signal(int)

    def __init__(self, conn, epoch: int, parent=None):
        super().__init__(parent)
        self._conn = conn
        self._epoch = epoch
        self.setObjectName(f"ffs-engine-reader-{epoch}")

    def run(self) -> None:
        conn = self._conn
        try:
            while True:
                msg = conn.recv()
                self.received.emit(self._epoch, msg)
        except (EOFError, OSError):
            pass
        except Exception:
            _log.exception("engine reader failed")
        self.closed.emit(self._epoch)


class _Sender:
    def __init__(self, conn, epoch: int):
        self.conn = conn
        self.q: "queue.Queue" = queue.Queue()
        self.thread = threading.Thread(target=self._run, name=f"ffs-engine-sender-{epoch}", daemon=True)
        self.thread.start()

    def put(self, msg) -> None:
        self.q.put(msg)

    def close(self) -> None:
        self.q.put(None)

    def _run(self) -> None:
        while True:
            msg = self.q.get()
            if msg is None:
                return
            try:
                self.conn.send(msg)
            except (OSError, EOFError, ValueError):
                return          # the reader sees EOF and supervision takes over
            except Exception:
                _log.exception("engine send failed for %s", type(msg).__name__)


class EngineClient(QObject):
    ready = Signal(object)            # protocol.Ready
    search_result = Signal(object)    # protocol.SearchResult (first page, pages, refreshes)
    index_status = Signal(object)     # protocol.IndexStatus
    index_changed = Signal(int)       # index generation
    item_info = Signal(object)        # protocol.ItemInfo
    notice = Signal(object)           # protocol.Notice
    engine_state = Signal(str)        # 'starting'|'ready'|'restarting'|'failed'

    _inproc = Signal(int, object)

    def __init__(self, boot: P.EngineBootstrap, *, in_process: bool = False, parent=None):
        super().__init__(parent)
        self._boot = boot
        self._in_process = in_process
        self._config: Optional[P.EngineConfig] = None
        self.current_gen = 0
        self.is_ready = False
        self.last_status: Optional[P.IndexStatus] = None
        self.last_ready: Optional[P.Ready] = None
        self.state = "stopped"
        self._epoch = 0
        self._proc = None
        self._conn = None
        self._reader: Optional[_Reader] = None
        self._sender: Optional[_Sender] = None
        self._engine = None
        self._backlog: list = []
        self._last_request: Optional[P.SearchRequest] = None
        self._restart_times: list = []
        self._shutting_down = False
        self._req_id = 0
        self._spawn_t = 0.0
        self.spawn_to_ready_ms: Optional[float] = None
        self._inproc.connect(self._on_message)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.state not in ("stopped", "failed"):
            return
        self._shutting_down = False
        self._restart_times = []
        self._set_state("starting")
        self._spawn()

    def _current_boot(self) -> P.EngineBootstrap:
        if self._config is not None:
            return dataclasses.replace(self._boot, config=self._config)
        return self._boot

    def _spawn(self) -> None:
        if self._shutting_down:
            return
        self._epoch += 1
        epoch = self._epoch
        self.is_ready = False
        self._spawn_t = time.perf_counter()
        boot = self._current_boot()
        if self._in_process:
            from .engine.service import Engine
            self._engine = Engine(boot, lambda msg, e=epoch: self._inproc.emit(e, msg))
            self._engine.start()
            self._sender = None
        else:
            from .engine.service import engine_main
            ctx = multiprocessing.get_context("spawn")
            parent_conn, child_conn = ctx.Pipe(duplex=True)
            proc = ctx.Process(target=engine_main, args=(child_conn, boot), name="ffs-engine", daemon=True)
            proc.start()
            child_conn.close()
            self._proc = proc
            self._conn = parent_conn
            self._sender = _Sender(parent_conn, epoch)
            reader = _Reader(parent_conn, epoch)
            reader.received.connect(self._on_message)
            reader.closed.connect(self._on_closed)
            self._reader = reader
            reader.start()
        self._send(P.Hello())
        backlog, self._backlog = self._backlog, []
        for msg in backlog:
            self._send(msg)

    def shutdown(self, timeout: float = 3.0) -> None:
        self._shutting_down = True
        self.is_ready = False
        if self._engine is not None:
            eng, self._engine = self._engine, None
            eng.stop(timeout)
        if self._sender is not None:
            self._sender.put(P.Shutdown())
            self._sender.close()
            self._sender.thread.join(min(1.0, timeout))
            self._sender = None
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.join(timeout)
            if proc.is_alive():
                _log.warning("engine did not exit in %.1f s; terminating", timeout)
                proc.terminate()
                proc.join(1.0)
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._reader is not None:
            self._reader.wait(1000)
            self._reader = None
        self._set_state("stopped")

    @property
    def engine_pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    @property
    def process(self):
        return self._proc

    # ------------------------------------------------------------------ requests
    def _send(self, msg) -> None:
        if self._engine is not None:
            self._engine.handle(msg)
        elif self._sender is not None:
            self._sender.put(msg)
        elif not isinstance(msg, (P.SearchRequest, P.Hello, P.Shutdown)):
            if len(self._backlog) < _BACKLOG_MAX:
                self._backlog.append(msg)

    def configure(self, cfg: P.EngineConfig) -> None:
        self._config = cfg
        self._send(P.Configure(cfg))

    def search(self, text: str, filters: Optional[P.FilterState] = None, sort: Optional[P.SortSpec] = None, *,
               limit: int = 200, suggestions: bool = True, commit_history: bool = False) -> int:
        self.current_gen += 1
        req = P.SearchRequest(gen=self.current_gen, text=text, filters=filters or P.FilterState(),
                              sort=sort or P.SortSpec(), limit=limit, suggestions=suggestions,
                              commit_history=commit_history)
        self._last_request = req
        self._send(req)
        return req.gen

    def fetch_page(self, gen: int, offset: int, limit: int = 500) -> None:
        self._send(P.FetchPage(gen, offset, limit))

    def index(self, action: str, root_ids: Optional[tuple] = None) -> None:
        self._send(P.IndexCommand(action, tuple(root_ids) if root_ids else None))

    def user_op(self, op: str, paths: tuple = (), value: object = None) -> None:
        self._send(P.UserOp(op, tuple(paths), value))

    def request_item_info(self, path: str) -> int:
        self._req_id += 1
        self._send(P.ItemInfoRequest(self._req_id, path))
        return self._req_id

    def forget(self, paths: Sequence[str]) -> None:
        self.user_op("forget", tuple(paths))

    # ------------------------------------------------------------------ incoming
    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if state in ("starting", "ready", "restarting", "failed"):   # the frozen signal values
                self.engine_state.emit(state)

    @Slot(int, object)
    def _on_message(self, epoch: int, msg) -> None:
        if epoch != self._epoch:
            return
        if isinstance(msg, P.SearchResult):
            if msg.gen < self.current_gen:
                return
            self.search_result.emit(msg)
        elif isinstance(msg, P.IndexStatus):
            self.last_status = msg
            self.index_status.emit(msg)
        elif isinstance(msg, P.IndexChanged):
            self.index_changed.emit(msg.index_generation)
        elif isinstance(msg, P.ItemInfo):
            self.item_info.emit(msg)
        elif isinstance(msg, P.Notice):
            self.notice.emit(msg)
        elif isinstance(msg, P.Ready):
            self.is_ready = True
            self.last_ready = msg
            self.last_status = msg.status
            self.spawn_to_ready_ms = (time.perf_counter() - self._spawn_t) * 1000
            replay = self.state == "restarting"
            self.ready.emit(msg)
            self.index_status.emit(msg.status)
            self._set_state("ready")
            if replay:
                if self._config is not None:
                    self._send(P.Configure(self._config))
                if self._last_request is not None:
                    self._send(self._last_request)
            elif self._last_request is not None and self._last_request.gen == self.current_gen:
                pass  # sent before Ready: the engine queued it
        elif isinstance(msg, P.Fatal):
            _log.error("engine fatal: %s\n%s", msg.text, msg.traceback)
            self.notice.emit(P.Notice("error", "engine_fatal", msg.text))
            if self._in_process:
                self._engine_lost()
        else:
            _log.debug("unexpected engine message %r", type(msg).__name__)

    @Slot(int)
    def _on_closed(self, epoch: int) -> None:
        if epoch != self._epoch or self._shutting_down:
            return
        self._engine_lost()

    def _engine_lost(self) -> None:
        self.is_ready = False
        code = None
        if self._proc is not None:
            self._proc.join(0.5)
            code = self._proc.exitcode
        _log.warning("engine exited unexpectedly (exit code %s)", code)
        if self._sender is not None:
            self._sender.close()
            self._sender = None
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        self._proc = None
        if self._engine is not None:
            eng, self._engine = self._engine, None
            threading.Thread(target=eng.stop, daemon=True).start()
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if now - t < RESTART_WINDOW]
        n = len(self._restart_times)
        if n >= len(RESTART_DELAYS):
            self._set_state("failed")
            return
        self._restart_times.append(now)
        self._set_state("restarting")
        QTimer.singleShot(int(RESTART_DELAYS[n] * 1000), self._spawn)

"""Preview pane: file header, breadcrumb, code/image preview and details.

``PreviewPane`` is cheap to update (the header is set synchronously from the result
row); everything that touches the disk runs in one persistent ``PreviewWorker`` thread
with a latest-wins request slot.  The worker never touches SQLite and never creates a
``QPixmap`` (it hands a ``QImage`` to the GUI thread).
"""
from __future__ import annotations

import codecs
import hashlib
import os
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import PurePath
from typing import List, Optional

from PySide6.QtCore import QRect, QSize, QTimer, Qt, QThread, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QGuiApplication, QImage, QImageReader, QKeySequence, QPainter,
    QPainterPath, QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QFormLayout, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy,
    QStackedWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from ..engine.protocol import DATALESS, FOLDER, PACKAGE, ItemInfo, ResultRow
from . import theme
from .code_view import CodeView
from .highlight import normalize_language

try:  # kinds.py is owned by the engine package
    from .. import kinds as _kinds
except ImportError:  # pragma: no cover - until kinds.py lands
    _kinds = None

TEXT_LIMIT = 64 * 1024
LINE_LIMIT = 2000
FOLDER_COUNT_LIMIT = 10_000
HASH_LIMIT = 512 * 1024 * 1024
HASH_CHUNK = 1024 * 1024
IMAGE_FILE_LIMIT = 128 * 1024 * 1024
IMAGE_PIXEL_LIMIT = 150_000_000
HASH_CACHE_SIZE = 64


# --------------------------------------------------------------------------- kinds
_FALLBACK_KINDS = {
    "py": ("Python", "PY", "python"), "js": ("JavaScript", "JS", "js"), "mjs": ("JavaScript", "JS", "js"),
    "jsx": ("JavaScript", "JSX", "js"), "ts": ("TypeScript", "TS", "ts"), "tsx": ("TypeScript", "TSX", "ts"),
    "json": ("JSON", "JSON", "json"), "html": ("HTML", "HTML", "html"), "htm": ("HTML", "HTML", "html"),
    "xml": ("XML", "XML", "xml"), "css": ("CSS", "CSS", "css"), "scss": ("SCSS", "SCSS", "css"),
    "c": ("C", "C", "c"), "h": ("C header", "H", "c"), "cpp": ("C++", "C++", "c"),
    "cs": ("C#", "C#", "csharp"), "java": ("Java", "JAVA", "java"), "go": ("Go", "GO", "go"),
    "rs": ("Rust", "RS", "rust"), "sh": ("Shell script", "SH", "shell"), "ps1": ("PowerShell", "PS1", "powershell"),
    "yml": ("YAML", "YML", "yaml"), "yaml": ("YAML", "YAML", "yaml"), "toml": ("TOML", "TOML", "toml"),
    "ini": ("INI", "INI", "ini"), "md": ("Markdown", "MD", "markdown"), "sql": ("SQL", "SQL", "sql"),
    "txt": ("Text", "TXT", None), "pdf": ("PDF document", "PDF", None), "png": ("PNG image", "PNG", None),
    "jpg": ("JPEG image", "JPG", None), "jpeg": ("JPEG image", "JPG", None), "gif": ("GIF image", "GIF", None),
}


def _ext(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name.strip(".") else ""


def kind_label(name: str, flags: int) -> str:
    if _kinds is not None:
        return _kinds.kind_label(name, flags)
    if flags & PACKAGE:
        return "Application"
    if flags & FOLDER:
        return "Folder"
    ext = _ext(name)
    hit = _FALLBACK_KINDS.get(ext)
    return hit[0] if hit else (f"{ext.upper()} file" if ext else "File")


def badge_text(name: str, flags: int) -> str:
    if _kinds is not None:
        return _kinds.badge_text(name, flags)
    if flags & FOLDER:
        return ""
    ext = _ext(name)
    hit = _FALLBACK_KINDS.get(ext)
    return hit[1] if hit else ext.upper()[:4]


def language_of(name: str) -> Optional[str]:
    if _kinds is not None:
        return _kinds.language_of(name)
    hit = _FALLBACK_KINDS.get(_ext(name))
    return hit[2] if hit else None


# --------------------------------------------------------------------------- worker
@dataclass
class PreviewRequest:
    sel: int
    path: str
    flags: int = 0
    load: bool = True
    hashes: bool = False
    width: int = 0   # image target in device pixels
    height: int = 0
    dpr: float = 1.0


@dataclass
class PreviewResult:
    sel: int
    path: str
    kind: str  # text | image | folder | binary | empty | toolarge | dataless | error
    text: str = ""
    message: str = ""
    truncated: str = ""  # human note when only the start of a file is shown
    size: int = -1
    mtime: float = 0.0
    ctime: float = 0.0
    highlight: bool = True
    image: Optional[QImage] = None
    image_size: tuple = (0, 0)
    image_format: str = ""
    count: int = 0
    count_capped: bool = False


@dataclass
class HashResult:
    sel: int
    path: str
    md5: str = ""
    sha256: str = ""
    note: str = ""


_TEXT_BYTES = bytes(set(range(32, 256)) | {7, 8, 9, 10, 12, 13, 27})
_image_exts_cache: Optional[frozenset] = None


def image_extensions() -> frozenset:
    global _image_exts_cache
    if _image_exts_cache is None:
        exts = {bytes(f).decode("ascii", "ignore").lower() for f in QImageReader.supportedImageFormats()}
        exts.discard("pdf")
        if "jpeg" in exts or "jpg" in exts:
            exts |= {"jpg", "jpeg"}
        if "tiff" in exts or "tif" in exts:
            exts |= {"tif", "tiff"}
        _image_exts_cache = frozenset(exts)
    return _image_exts_cache


def decode_text(raw: bytes, final: bool = True) -> Optional[str]:
    """Decode the start of a file (BOM, UTF-16, UTF-8 with replacement); None = binary."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        dec = codecs.getincrementaldecoder("utf-16")("replace")
        return dec.decode(raw, final)
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if b"\x00" in raw:
        return None
    if raw and len(raw.translate(None, _TEXT_BYTES)) * 10 > len(raw):
        return None
    return codecs.getincrementaldecoder("utf-8")("replace").decode(raw, final)


def _human_size(n: int) -> str:
    return theme.format_size(n)


class PreviewWorker(QThread):
    """One persistent reader thread.  ``submit`` replaces any request not started yet
    (latest wins); hashing checks for a newer request or a cancel once per 1 MB chunk."""

    loaded = Signal(object)  # PreviewResult
    hashed = Signal(object)  # HashResult

    def __init__(self, parent=None, image_exts: Optional[frozenset] = None):
        super().__init__(parent)
        self._cond = threading.Condition()
        self._pending: Optional[PreviewRequest] = None
        self._stop = False
        self._epoch = 0
        self._image_exts = image_exts if image_exts is not None else image_extensions()
        self._hash_cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._active = 0
        # counters (read by tests)
        self.max_concurrent = 0
        self.processed = 0
        self.hash_chunks = 0

    # ---- GUI-thread API
    def submit(self, req: PreviewRequest) -> None:
        with self._cond:
            self._pending = req
            self._epoch += 1
            self._cond.notify()
        if not self.isRunning():
            self.start()

    def cancel_hashes(self) -> None:
        with self._cond:
            self._epoch += 1

    def stop(self, timeout_ms: int = 3000) -> None:
        with self._cond:
            self._stop = True
            self._pending = None
            self._epoch += 1
            self._cond.notify()
        if self.isRunning():
            self.wait(timeout_ms)

    # ---- worker thread
    def run(self) -> None:  # noqa: D401
        while True:
            with self._cond:
                while self._pending is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                req = self._pending
                self._pending = None
                epoch = self._epoch
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
            try:
                self._process(req, epoch)
            except Exception as exc:  # never let the thread die
                self.loaded.emit(PreviewResult(req.sel, req.path, "error", message=f"No preview: {exc}"))
            finally:
                self._active -= 1
                self.processed += 1

    def _superseded(self, sel: int) -> bool:
        with self._cond:
            return self._stop or (self._pending is not None and self._pending.sel != sel)

    def _process(self, req: PreviewRequest, epoch: int) -> None:
        if req.load:
            result = self._load(req)
            if not self._superseded(req.sel):
                self.loaded.emit(result)
            if result.kind in ("error",) or not req.hashes:
                return
        if req.hashes and not (req.flags & (FOLDER | DATALESS)):
            self._hash(req, epoch)

    def _load(self, req: PreviewRequest) -> PreviewResult:
        path = req.path
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return PreviewResult(req.sel, path, "error", message="This item no longer exists.")
        except PermissionError:
            return PreviewResult(req.sel, path, "error", message="You do not have permission to open this item.")
        except OSError as exc:
            return PreviewResult(req.sel, path, "error", message=f"The item could not be read: {exc.strerror or exc}")
        res = PreviewResult(req.sel, path, "binary", size=st.st_size, mtime=st.st_mtime, ctime=st.st_ctime)

        if stat.S_ISDIR(st.st_mode):
            res.kind = "folder"
            n = 0
            try:
                with os.scandir(path) as it:
                    for _ in it:
                        n += 1
                        if n >= FOLDER_COUNT_LIMIT:
                            res.count_capped = True
                            break
            except OSError as exc:
                res.kind = "error"
                res.message = f"The folder could not be read: {exc.strerror or exc}"
            res.count = n
            return res
        if req.flags & DATALESS:
            res.kind = "dataless"
            res.message = "Not downloaded (iCloud). Download the file to preview it."
            return res
        ext = _ext(os.path.basename(path))
        try:
            if ext in self._image_exts:
                return self._load_image(req, res)
            with open(path, "rb") as f:
                raw = f.read(TEXT_LIMIT + 1)
        except PermissionError:
            res.kind, res.message = "error", "You do not have permission to read this file."
            return res
        except OSError as exc:
            res.kind, res.message = "error", f"The file could not be read: {exc.strerror or exc}"
            return res
        cut = len(raw) > TEXT_LIMIT
        raw = raw[:TEXT_LIMIT]
        if not raw:
            res.kind = "empty"
            return res
        text = decode_text(raw, final=not cut)
        if text is None:
            res.kind = "binary"
            return res
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        note = ""
        if len(lines) > LINE_LIMIT:
            lines = lines[:LINE_LIMIT]
            text = "\n".join(lines)
            note = f"Showing the first {LINE_LIMIT:,} lines"
        elif cut:
            note = f"Showing the first {TEXT_LIMIT // 1024} KB of {_human_size(st.st_size)}"
        res.kind = "text"
        res.text = text
        res.truncated = note
        res.highlight = max(map(len, lines)) <= 2000
        return res

    def _load_image(self, req: PreviewRequest, res: PreviewResult) -> PreviewResult:
        if res.size > IMAGE_FILE_LIMIT:
            res.kind, res.message = "toolarge", "This image is too large to preview."
            return res
        reader = QImageReader(req.path)
        reader.setAutoTransform(True)
        src = reader.size()
        res.image_format = bytes(reader.format()).decode("ascii", "ignore").upper()
        if src.isValid():
            res.image_size = (src.width(), src.height())
            if src.width() * src.height() > IMAGE_PIXEL_LIMIT:
                res.kind, res.message = "toolarge", "This image is too large to preview."
                return res
            target = QSize(max(req.width, 64), max(req.height, 64))
            if src.width() > target.width() or src.height() > target.height():
                reader.setScaledSize(src.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            res.kind, res.message = "binary", "This image could not be loaded."
            return res
        if not src.isValid():
            res.image_size = (image.width(), image.height())
        image.setDevicePixelRatio(req.dpr or 1.0)
        res.kind = "image"
        res.image = image
        return res

    def _hash(self, req: PreviewRequest, epoch: int) -> None:
        path = req.path
        try:
            st = os.stat(path)
        except OSError as exc:
            self.hashed.emit(HashResult(req.sel, path, note=f"Could not read the file: {exc.strerror or exc}"))
            return
        if st.st_size > HASH_LIMIT:
            self.hashed.emit(HashResult(req.sel, path, note="Not calculated for files over 512 MB"))
            return
        key = (path, st.st_size, st.st_mtime_ns)
        cached = self._hash_cache.get(key)
        if cached is not None:
            self._hash_cache.move_to_end(key)
            self.hashed.emit(HashResult(req.sel, path, cached[0], cached[1]))
            return
        md5 = hashlib.md5(usedforsecurity=False)
        sha = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    with self._cond:
                        if self._epoch != epoch:
                            return
                    chunk = f.read(HASH_CHUNK)
                    if not chunk:
                        break
                    self.hash_chunks += 1
                    md5.update(chunk)
                    sha.update(chunk)
        except OSError as exc:
            self.hashed.emit(HashResult(req.sel, path, note=f"Could not read the file: {exc.strerror or exc}"))
            return
        self._hash_cache[key] = (md5.hexdigest(), sha.hexdigest())
        while len(self._hash_cache) > HASH_CACHE_SIZE:
            self._hash_cache.popitem(last=False)
        self.hashed.emit(HashResult(req.sel, path, md5.hexdigest(), sha.hexdigest()))


# --------------------------------------------------------------------------- small widgets
class _ElidedLabel(QLabel):
    """One-line label that elides with "…" instead of forcing the pane wider."""

    def __init__(self, mode=Qt.TextElideMode.ElideRight, parent=None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def set_full_text(self, text: str) -> None:
        if text == self._full:
            return
        self._full = text
        self.setAccessibleName(text)
        self.update()
        self._update_tip()

    def full_text(self) -> str:
        return self._full

    def _update_tip(self) -> None:
        fits = QFontMetrics(self.font()).horizontalAdvance(self._full) <= self.width()
        self.setToolTip("" if fits else self._full)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_tip()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self._full), fm.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, QFontMetrics(self.font()).height())

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setFont(self.font())
        p.setPen(self.palette().color(QPalette.ColorGroup.Current, QPalette.ColorRole.WindowText))
        rect = self.contentsRect()
        p.drawText(rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   QFontMetrics(self.font()).elidedText(self._full, self._mode, rect.width()))


class _SelectableValue(QLabel):
    """Wrapping, selectable value label (used for the hashes)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw = ""
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)

    def set_value(self, value: str, wrap_every: int = 0) -> None:
        self._raw = value
        if wrap_every and len(value) > wrap_every:
            value = "\n".join(value[i:i + wrap_every] for i in range(0, len(value), wrap_every))
        self.setText(value)
        self.setMinimumHeight(self.fontMetrics().lineSpacing() * (len(value.splitlines()) or 1) + 4)

    def value(self) -> str:
        return self._raw

    def _copy(self) -> None:
        text = "".join(self.selectedText().split()) if self.hasSelectedText() else self._raw
        QGuiApplication.clipboard().setText(text or self._raw)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.matches(QKeySequence.StandardKey.Copy):
            self._copy()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        act = menu.addAction("Copy")
        act.setEnabled(bool(self._raw))
        act.triggered.connect(self._copy)
        menu.exec(event.globalPos())


class KindBadge(QWidget):
    """36 x 36 rounded square with the extension abbreviation, or a folder glyph."""

    SIZE = 36

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._folder = False
        self._package = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAccessibleName("File type")

    def set_badge(self, text: str, flags: int = 0) -> None:
        folder = bool(flags & FOLDER)
        package = bool(flags & PACKAGE)
        if (text, folder, package) == (self._text, self._folder, self._package):
            return
        self._text, self._folder, self._package = text, folder, package
        self.update()

    def text(self) -> str:
        return self._text

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        text_color = pal.color(QPalette.ColorRole.Text)
        border = theme._mix(pal.color(QPalette.ColorRole.Mid), pal.color(QPalette.ColorRole.Window), 0.8)
        fill = theme._mix(pal.color(QPalette.ColorRole.AlternateBase), pal.color(QPalette.ColorRole.Mid), 0.7)
        rect = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 6, 6)
        if self._folder or self._package:
            self._paint_folder(p, rect, text_color)
            return
        if not self._text:
            return
        font = theme.ui_font(0.8, QFont.Weight.DemiBold)
        fm = QFontMetrics(font)
        limit = rect.width() - 6
        while fm.horizontalAdvance(self._text) > limit and font.pointSizeF() > 5:
            font.setPointSizeF(font.pointSizeF() - 0.5)
            fm = QFontMetrics(font)
        p.setFont(font)
        p.setPen(text_color)
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self._text)

    def _paint_folder(self, p: QPainter, rect: QRect, color: QColor) -> None:
        w, h = 20.0, 15.0
        x = rect.center().x() - w / 2 + 0.5
        y = rect.center().y() - h / 2 + 1
        path = QPainterPath()
        path.moveTo(x, y + 2)
        path.lineTo(x, y + h)
        path.lineTo(x + w, y + h)
        path.lineTo(x + w, y + 3.5)
        path.lineTo(x + 8.5, y + 3.5)
        path.lineTo(x + 7, y)
        path.lineTo(x + 1.5, y)
        path.closeSubpath()
        p.setPen(QPen(color, 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        if self._package:
            p.drawLine(int(x), int(y + 7), int(x + w), int(y + 7))


def breadcrumb_segments(folder: str, root_path: Optional[str] = None) -> List[tuple]:
    """``[(label, absolute path), ...]`` from the library root (or drive) to ``folder``."""
    if not folder:
        return []
    norm = os.path.normcase
    parts = PurePath(folder).parts
    start = 0
    if root_path:
        rp = PurePath(root_path)
        if norm(str(rp)) == norm(folder) or norm(folder).startswith(norm(str(rp)).rstrip("\\/") + os.sep):
            start = max(0, len(rp.parts) - 1)
    out = []
    for i in range(start, len(parts)):
        path = str(PurePath(*parts[: i + 1]))
        label = parts[i]
        if i == start and i > 0 and not root_path:
            pass
        out.append((theme.display_text(label.rstrip("\\/") or label), path))
    return out


class Breadcrumb(QWidget):
    """Flat segment buttons ("Documents › Projects › src").  Click = search in that
    folder; the context menu also offers to reveal it.  Middle segments collapse into a
    "…" menu when the pane is narrow."""

    scope_requested = Signal(str)
    reveal_requested = Signal(str)

    POOL = 8
    SEP = "›"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._segs: List[tuple] = []
        self._buttons: List[QToolButton] = []
        self._more = self._make_button()
        self._more.setText("…")
        self._more.setToolTip("Show hidden folders")
        self._more.clicked.connect(self._show_more)
        self._hidden: List[tuple] = []
        self._seps: List[int] = []
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Folder path")

    def _make_button(self) -> QToolButton:
        b = QToolButton(self)
        b.setAutoRaise(True)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.hide()
        return b

    def _button(self, i: int) -> QToolButton:
        while len(self._buttons) <= i:
            b = self._make_button()
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.clicked.connect(lambda _c=False, b=b: self.scope_requested.emit(b.property("path")))
            b.customContextMenuRequested.connect(lambda pos, b=b: self._context_menu(b, pos))
            self._buttons.append(b)
        return self._buttons[i]

    def segments(self) -> List[tuple]:
        return list(self._segs)

    def visible_labels(self) -> List[str]:
        return [b.text() for b in self._buttons if not b.isHidden()]

    def set_path(self, folder: str, root_path: Optional[str] = None) -> None:
        segs = breadcrumb_segments(folder, root_path)
        if segs == self._segs:
            return
        self._segs = segs
        self.setToolTip("")
        self._relayout()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self._row_height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, self._row_height())

    def _row_height(self) -> int:
        return QFontMetrics(self.font()).height() + 8

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _width_of(self, text: str) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(text) + 12

    def _relayout(self) -> None:
        segs = self._segs
        for b in self._buttons:
            b.hide()
        self._more.hide()
        self._seps = []
        self._hidden = []
        self.update()
        if not segs:
            return
        fm = QFontMetrics(self.font())
        sep_w = fm.horizontalAdvance(self.SEP) + 6
        avail = max(0, self.width())
        widths = [self._width_of(label) for label, _ in segs]
        more_w = self._width_of("…")
        n = len(segs)

        def total(indices, with_more):
            w = sum(widths[i] for i in indices) + (more_w if with_more else 0)
            slots = len(indices) + (1 if with_more else 0)
            return w + sep_w * max(0, slots - 1)

        shown = list(range(n))
        with_more = False
        if n > self.POOL or total(shown, False) > avail:
            with_more = n > 1
            tail = 1
            best = [0, n - 1] if n > 1 else [0]
            while tail < min(n - 1, self.POOL - 2):
                trial = [0] + list(range(n - tail - 1, n))
                if total(trial, True) <= avail:
                    best = trial
                    tail += 1
                else:
                    break
            shown = best if n > 1 else [0]
            with_more = n > len(shown)
        hidden_idx = [i for i in range(n) if i not in shown]
        self._hidden = [segs[i] for i in hidden_idx]
        row_h = self.height()
        x = 0
        items = []
        for pos, i in enumerate(shown):
            items.append(("seg", i))
            if pos == 0 and with_more:
                items.append(("more", None))
        slot = 0
        for k, (kind, i) in enumerate(items):
            if k:
                self._seps.append(x + sep_w // 2)
                x += sep_w
            if kind == "more":
                w = more_w
                btn = self._more
                btn.setText("…")
            else:
                btn = self._button(slot)
                slot += 1
                w = widths[i]
                label, path = segs[i]
                btn.setProperty("path", path)
                btn.setToolTip(path)
                room = avail - x
                if room < w and k == len(items) - 1:  # last segment: elide rather than overflow
                    w = max(24, room)
                    btn.setText(fm.elidedText(label, Qt.TextElideMode.ElideMiddle, max(12, w - 12)))
                else:
                    btn.setText(label)
            btn.setGeometry(x, 0, w, row_h)
            btn.show()
            x += w

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._seps:
            return
        p = QPainter(self)
        p.setPen(theme.secondary_text_color(self.palette(), self.palette().color(QPalette.ColorRole.Window)))
        for cx in self._seps:
            p.drawText(QRect(cx - 8, 0, 16, self.height()), int(Qt.AlignmentFlag.AlignCenter), self.SEP)

    def _show_more(self) -> None:
        menu = QMenu(self)
        for label, path in self._hidden:
            act = menu.addAction(label)
            act.triggered.connect(lambda _c=False, p=path: self.scope_requested.emit(p))
        menu.exec(self._more.mapToGlobal(self._more.rect().bottomLeft()))

    def _context_menu(self, button: QToolButton, pos) -> None:
        path = button.property("path")
        menu = QMenu(self)
        menu.addAction("Search in this folder").triggered.connect(lambda: self.scope_requested.emit(path))
        menu.addAction(theme.reveal_label()).triggered.connect(lambda: self.reveal_requested.emit(path))
        menu.exec(button.mapToGlobal(pos))


class _MessageView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE_L, theme.SPACE_L, theme.SPACE_L, theme.SPACE_L)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.make_secondary(self.label)
        lay.addWidget(self.label)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)

    def set_message(self, text: str) -> None:
        self.label.setText(text)


class _ImageView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE_S, theme.SPACE_S, theme.SPACE_S, theme.SPACE_S)
        lay.setSpacing(theme.SPACE_XS)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.picture.setAccessibleName("Image preview")
        self.caption = theme.SecondaryLabel()
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.picture, 1)
        lay.addWidget(self.caption)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Base)

    def set_image(self, pixmap: QPixmap, caption: str) -> None:
        self.picture.setPixmap(pixmap)
        self.caption.setText(caption)

    def target_size(self, outer: Optional[QSize] = None) -> QSize:
        # hidden stack pages are not resized, so the pane passes the stack's size
        m = self.layout().contentsMargins()
        room = (outer or self.size()) - QSize(m.left() + m.right(), m.top() + m.bottom() + self.caption.sizeHint().height()
                                   + self.layout().spacing())
        return QSize(max(room.width(), 64), max(room.height(), 64))


# --------------------------------------------------------------------------- the pane
class PreviewPane(QFrame):
    open_requested = Signal(str)
    reveal_requested = Signal(str)
    copy_path_requested = Signal(str)
    scope_requested = Signal(str)

    PAGE_MESSAGE, PAGE_CODE, PAGE_IMAGE = 0, 1, 2
    TAB_PREVIEW, TAB_DETAILS = 0, 1

    def __init__(self, client=None, parent=None):
        super().__init__(parent)
        self.setObjectName("PreviewPane")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._client = client
        self._row: Optional[ResultRow] = None
        self._root: Optional[str] = None
        self._sel = 0
        self._loaded_sel = -1
        self._hashes_sel = -1
        self._terms: List[str] = []
        self._kind = ""
        self._info_req = -1
        self._shut = False

        self._worker = PreviewWorker(self)
        self._worker.loaded.connect(self._on_loaded)
        self._worker.hashed.connect(self._on_hashed)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACE_M, theme.SPACE_M, theme.SPACE_M, theme.SPACE_M)
        root.setSpacing(theme.SPACE_S)

        # header
        head = QHBoxLayout()
        head.setSpacing(theme.SPACE_M)
        self.badge = KindBadge()
        head.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.name_label = _ElidedLabel(Qt.TextElideMode.ElideMiddle)
        self.name_label.setFont(theme.ui_font(1.1, QFont.Weight.DemiBold))
        self.kind_label = _ElidedLabel()
        kf = theme.ui_font(0.82)
        kf.setCapitalization(QFont.Capitalization.AllUppercase)
        kf.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
        self.kind_label.setFont(kf)
        theme.make_secondary(self.kind_label)
        names.addStretch(1)
        names.addWidget(self.name_label)
        names.addWidget(self.kind_label)
        names.addStretch(1)
        head.addLayout(names, 1)
        root.addLayout(head)

        self.breadcrumb = Breadcrumb()
        self.breadcrumb.scope_requested.connect(self.scope_requested)
        self.breadcrumb.reveal_requested.connect(self.reveal_requested)
        root.addWidget(self.breadcrumb)

        # tabs
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setAccessibleName("Preview and details")
        self.stack = QStackedWidget()
        self.message_view = _MessageView()
        self.code = CodeView()
        self.code_page = QWidget()
        code_lay = QVBoxLayout(self.code_page)
        code_lay.setContentsMargins(0, 0, 0, 0)
        code_lay.setSpacing(0)
        code_lay.addWidget(self.code, 1)
        self.note = theme.SecondaryLabel()
        self.note.setContentsMargins(theme.SPACE_S, theme.SPACE_XS, theme.SPACE_S, theme.SPACE_XS)
        self.note.setAutoFillBackground(True)
        self.note.setBackgroundRole(QPalette.ColorRole.Window)
        self.note.hide()
        code_lay.addWidget(self.note)
        self.image_view = _ImageView()
        self.stack.addWidget(self.message_view)
        self.stack.addWidget(self.code_page)
        self.stack.addWidget(self.image_view)
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(self.stack)
        preview_page = QWidget()
        pl = QVBoxLayout(preview_page)
        pl.setContentsMargins(0, theme.SPACE_S, 0, 0)
        pl.addWidget(frame)
        self.tabs.addTab(preview_page, "Preview")
        self.tabs.addTab(self._build_details(), "Details")
        root.addWidget(self.tabs, 1)

        # buttons
        buttons = QHBoxLayout()
        buttons.setSpacing(theme.SPACE_S)
        self.open_button = QPushButton("Open")
        self.reveal_button = QPushButton(theme.reveal_label())
        self.copy_button = QPushButton("Copy path")
        for b, name in ((self.open_button, "Open file"), (self.reveal_button, theme.reveal_label()),
                        (self.copy_button, "Copy path")):
            b.setAccessibleName(name)
            buttons.addWidget(b)
        buttons.addStretch(1)
        root.addLayout(buttons)
        self.open_button.clicked.connect(lambda: self._emit_path(self.open_requested))
        self.reveal_button.clicked.connect(lambda: self._emit_path(self.reveal_requested))
        self.copy_button.clicked.connect(lambda: self._emit_path(self.copy_path_requested))

        # timers
        self._stale = QTimer(self)
        self._stale.setSingleShot(True)
        self._stale.setInterval(150)
        self._stale.timeout.connect(self._show_loading)
        self._info_timer = QTimer(self)
        self._info_timer.setSingleShot(True)
        self._info_timer.setInterval(120)
        self._info_timer.timeout.connect(self._request_info)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(250)
        self._resize_timer.timeout.connect(self._reload_image)

        self.tabs.currentChanged.connect(self._on_tab_changed)
        if client is not None and hasattr(client, "item_info"):
            client.item_info.connect(self._on_item_info)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self._show_empty()

    # ------------------------------------------------------------------ building
    def _build_details(self) -> QWidget:
        page = QWidget()
        self.details_form = QFormLayout(page)
        form = self.details_form
        form.setContentsMargins(0, theme.SPACE_M, 0, 0)
        form.setHorizontalSpacing(theme.SPACE_L)
        form.setVerticalSpacing(theme.SPACE_S)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.values: dict = {}
        self._rows: dict = {}
        mono = theme.fixed_font()
        for key in ("Size", "Modified", "Created", "Kind", "Location", "Dimensions", "Tags", "Note", "MD5", "SHA-256"):
            if key == "Location":
                value = _ElidedLabel(Qt.TextElideMode.ElideMiddle)
            elif key in ("MD5", "SHA-256"):
                value = _SelectableValue()
                value.setFont(mono)
            else:
                value = QLabel()
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
            value.setAccessibleName(key)
            label = theme.SecondaryLabel(key)
            form.addRow(label, value)
            self.values[key] = value
            self._rows[key] = form.rowCount() - 1
        for key in ("Dimensions", "Tags", "Note"):
            form.setRowVisible(self._rows[key], False)
        return page

    def _set_value(self, key: str, text: str) -> None:
        w = self.values[key]
        if isinstance(w, _ElidedLabel):
            w.set_full_text(text)
        elif isinstance(w, _SelectableValue):
            w.set_value(text, 32 if key == "SHA-256" and len(text) == 64 else 0)
        else:
            w.setText(text)

    def _show_row_of(self, key: str, visible: bool) -> None:
        self.details_form.setRowVisible(self._rows[key], visible)

    # ------------------------------------------------------------------ public API
    def show_row(self, row: Optional[ResultRow], root_path: Optional[str] = None) -> None:
        if row is None:
            self._row = None
            self._sel += 1
            self._worker.cancel_hashes()
            self._show_empty()
            return
        if row == self._row and root_path == self._root:
            return
        same_file = self._row is not None and row.path == self._row.path
        self._row, self._root = row, root_path
        self._sel += 1
        self._loaded_sel = -1
        self._hashes_sel = -1
        self._kind = ""
        flags = row.flags
        name = theme.display_text(row.name)
        kind = kind_label(row.name, flags)
        self.badge.set_badge(badge_text(row.name, flags), flags)
        self.name_label.set_full_text(name)
        self.kind_label.set_full_text(kind)
        self.breadcrumb.set_path(os.path.dirname(row.path), root_path)
        for b in (self.open_button, self.reveal_button, self.copy_button):
            b.setEnabled(True)
        folder = bool(flags & FOLDER)
        self._set_value("Size", "" if folder else theme.format_size_exact(row.size))
        self._set_value("Modified", theme.format_timestamp_iso(row.mtime) if row.mtime else "")
        if not same_file:
            self._set_value("Created", "")
        self._set_value("Kind", kind)
        self._set_value("Location", theme.display_text(os.path.dirname(row.path)))
        for key in ("Dimensions", "Tags", "Note"):
            self._show_row_of(key, False)
        self._reset_hashes()
        self.code.set_terms(self._terms)
        self._stale.start()
        self._submit(load=True, hashes=self._want_hashes())
        if self._details_active():
            self._info_timer.start()

    def set_terms(self, terms: list) -> None:
        self._terms = list(terms or [])
        self.code.set_terms(self._terms)

    def shutdown(self) -> None:
        self._shut = True
        for t in (self._stale, self._info_timer, self._resize_timer):
            t.stop()
        self._worker.stop()

    # ------------------------------------------------------------------ state helpers
    def _details_active(self) -> bool:
        return self.tabs.currentIndex() == self.TAB_DETAILS and self.isVisible()

    def _hash_block_reason(self) -> str:
        row = self._row
        if row is None:
            return ""
        if row.flags & FOLDER:
            return "Not available for folders"
        if row.flags & DATALESS:
            return "Not downloaded (iCloud)"
        if row.size > HASH_LIMIT:
            return "Not calculated for files over 512 MB"
        return ""

    def _reset_hashes(self) -> None:
        reason = self._hash_block_reason()
        for key in ("MD5", "SHA-256"):
            self._set_value(key, reason)

    def _submit(self, load: bool, hashes: bool = False) -> None:
        row = self._row
        if row is None or self._shut:
            return
        target = self.image_view.target_size(self.stack.size())
        dpr = self.devicePixelRatioF()
        self._worker.submit(PreviewRequest(
            self._sel, row.path, row.flags, load=load, hashes=hashes,
            width=round(target.width() * dpr), height=round(target.height() * dpr), dpr=dpr))
        if hashes and not self._hash_block_reason():
            self._set_value("MD5", "Calculating…")
            self._set_value("SHA-256", "Calculating…")

    def _want_hashes(self) -> bool:
        return self._row is not None and self._details_active() and not self._hash_block_reason()

    def _emit_path(self, signal) -> None:
        if self._row is not None:
            signal.emit(self._row.path)

    # ------------------------------------------------------------------ views
    def _show_empty(self) -> None:
        self._stale.stop()
        self.badge.set_badge("", 0)
        self.name_label.set_full_text("No file selected")
        self.kind_label.set_full_text("")
        self.breadcrumb.set_path("")
        for b in (self.open_button, self.reveal_button, self.copy_button):
            b.setEnabled(False)
        for key in self.values:
            self._set_value(key, "")
        for key in ("Dimensions", "Tags", "Note"):
            self._show_row_of(key, False)
        self.code.clear_content()
        self.note.hide()
        self.message_view.set_message("Select a result to preview it.")
        self.stack.setCurrentIndex(self.PAGE_MESSAGE)

    def _show_loading(self) -> None:
        if self._row is not None and self._loaded_sel != self._sel:
            self.code.clear_content()
            self.note.hide()
            self.message_view.set_message("Loading…")
            self.stack.setCurrentIndex(self.PAGE_MESSAGE)

    def _message(self, text: str) -> None:
        self.code.clear_content()
        self.note.hide()
        self.message_view.set_message(text)
        self.stack.setCurrentIndex(self.PAGE_MESSAGE)

    # ------------------------------------------------------------------ worker results
    def _on_loaded(self, res: PreviewResult) -> None:
        if res.sel != self._sel or self._row is None:
            return
        self._stale.stop()
        self._loaded_sel = res.sel
        self._kind = res.kind
        if res.ctime:
            self._set_value("Created", theme.format_timestamp_iso(res.ctime))
        if res.kind == "text":
            lang = normalize_language(language_of(self._row.name))
            self.code.set_content(res.text, lang if res.highlight else None)
            self.note.setText(res.truncated)
            self.note.setVisible(bool(res.truncated))
            self.stack.setCurrentIndex(self.PAGE_CODE)
        elif res.kind == "image" and res.image is not None:
            w, h = res.image_size
            fmt = f" · {res.image_format}" if res.image_format else ""
            self.image_view.set_image(QPixmap.fromImage(res.image), f"{w} × {h}{fmt}")  # GUI thread
            self.values["Dimensions"].setText(f"{w} × {h} pixels")
            self._show_row_of("Dimensions", True)
            self.code.clear_content()
            self.stack.setCurrentIndex(self.PAGE_IMAGE)
        elif res.kind == "folder":
            n = f"{res.count:,}{'+' if res.count_capped else ''}"
            noun = "item" if res.count == 1 and not res.count_capped else "items"
            self._set_value("Size", f"{n} {noun}")
            self._message(f"Folder with {n} {noun}")
        elif res.kind == "empty":
            self._message("This file is empty.")
        elif res.kind == "binary":
            self._message(res.message or "No preview is available for this type of file.")
        else:  # toolarge, dataless, error
            self._message(res.message or "No preview is available.")

    def _on_hashed(self, res: HashResult) -> None:
        if res.sel != self._sel or self._row is None or not self._details_active():
            return
        self._hashes_sel = res.sel
        if res.note:
            self._set_value("MD5", res.note)
            self._set_value("SHA-256", res.note)
        else:
            self._set_value("MD5", res.md5)
            self._set_value("SHA-256", res.sha256)

    # ------------------------------------------------------------------ tabs and details data
    def _on_tab_changed(self, index: int) -> None:
        if self._row is None:
            return
        if index == self.TAB_DETAILS:
            self._request_details_data()
        else:
            self._worker.cancel_hashes()
            self._info_timer.stop()
            if self._hashes_sel != self._sel:
                self._reset_hashes()

    def _request_details_data(self) -> None:
        if self._row is None or not self._details_active():
            return
        if self._want_hashes() and self._hashes_sel != self._sel:
            self._submit(load=self._loaded_sel != self._sel, hashes=True)
        self._info_timer.start()

    def _request_info(self) -> None:
        if self._row is None or self._client is None or not self._details_active():
            return
        self._info_req = self._client.request_item_info(self._row.path)

    def _on_item_info(self, info: ItemInfo) -> None:
        if self._row is None or info.path != self._row.path:
            return
        tags = ", ".join(info.tags)
        self._set_value("Tags", tags)
        self._show_row_of("Tags", bool(tags))
        self._set_value("Note", info.note or "")
        self._show_row_of("Note", bool(info.note))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._request_details_data()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._worker.cancel_hashes()
        if self._row is not None and self._hashes_sel != self._sel:
            self._reset_hashes()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._kind == "image":
            self._resize_timer.start()

    def _reload_image(self) -> None:
        if self._kind == "image" and self._row is not None:
            self._submit(load=True, hashes=self._want_hashes() and self._hashes_sel != self._sel)

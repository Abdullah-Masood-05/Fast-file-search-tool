from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from .config import AppConfig
from .persistent_index import PersistentIndex
from .utils.file_utils import FileHash, format_size, is_binary_file

_SYSTEM_FOLDERS: Set[str] = {
    "$Recycle.Bin", "System Volume Information", "Windows",
    "WinSxS", "Program Files", "Program Files (x86)",
    ".Trash", ".Trashes", ".TemporaryItems",
    "tmp", "var", "sys", "proc", "dev",
}

_MAX_CONTENT_SIZE = 10 * 1024 * 1024  # 10 MB


@dataclass
class IndexStats:
    total_files: int = 0
    total_folders: int = 0
    total_size: int = 0
    content_indexed: int = 0
    content_skipped: int = 0
    extension_dist: Counter = field(default_factory=Counter)
    start_time: float = 0.0
    duration: float = 0.0

    @property
    def avg_file_size(self) -> float:
        count = self.total_files
        return self.total_size / max(1, count)


class Indexer:
    def __init__(
        self,
        config: Optional[AppConfig] = None,
        persistent_index: Optional[PersistentIndex] = None,
    ):
        self.config = config or AppConfig()
        self.persistent_index = persistent_index or PersistentIndex()
        self._stats = IndexStats()
        self._file_hashes: Dict[str, FileHash] = {}
        self._exclude_patterns: List[str] = list(self.config.exclude_patterns)
        self._progress_callback: Optional[Callable[[int, int], None]] = None
        self._done_callback: Optional[Callable[[IndexStats], None]] = None
        self._is_running = False

    def on_progress(self, cb: Callable[[int, int], None]) -> None:
        self._progress_callback = cb

    def on_done(self, cb: Callable[[IndexStats], None]) -> None:
        self._done_callback = cb

    def stop(self) -> None:
        self._is_running = False

    def _should_skip(self, path: Path) -> bool:
        name = path.name
        if name in _SYSTEM_FOLDERS:
            return True
        for pattern in self._exclude_patterns:
            if pattern in path.parts:
                return True
            if pattern in str(path):
                return True
        return False

    def _extract_content_text(self, path: Path) -> Optional[str]:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > _MAX_CONTENT_SIZE:
            return None
        ext = path.suffix.lower()

        try:
            if ext in {".txt", ".md", ".py", ".js", ".ts", ".java", ".c", ".cpp",
                        ".h", ".hpp", ".rs", ".go", ".rb", ".php", ".css", ".html",
                        ".xml", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
                        ".log", ".csv", ".sh", ".bat", ".ps1", ".sql", ".r",
                        ".swift", ".kt", ".scala", ".clj", ".lua", ".pl", ".pm"}:
                try:
                    return path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    try:
                        return path.read_text(encoding="latin-1", errors="replace")
                    except Exception:
                        return None

            if ext == ".pdf":
                return self._extract_pdf(path)
            if ext in {".doc", ".docx"}:
                return self._extract_docx(path)
            if ext in {".xls", ".xlsx"}:
                return self._extract_xlsx(path)
            if ext in {".zip", ".tar", ".gz", ".bz2", ".7z", ".rar"}:
                return self._extract_archive(path)

            if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"}:
                return self._extract_image_ocr(path)
        except Exception:
            pass
        return None

    def _extract_pdf(self, path: Path) -> Optional[str]:
        try:
            import pdfplumber
            text_parts = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages[:50]:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            return "\n".join(text_parts) if text_parts else None
        except ImportError:
            pass
        try:
            import PyPDF2
            text_parts = []
            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages[:50]:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            return "\n".join(text_parts) if text_parts else None
        except ImportError:
            return None

    def _extract_docx(self, path: Path) -> Optional[str]:
        try:
            import docx
            doc = docx.Document(path)
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return None

    def _extract_xlsx(self, path: Path) -> Optional[str]:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            text_parts = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    parts = [str(c) for c in row if c is not None]
                    if parts:
                        text_parts.append(" ".join(parts))
            wb.close()
            return "\n".join(text_parts) if text_parts else None
        except ImportError:
            return None

    def _extract_archive(self, path: Path) -> Optional[str]:
        try:
            import zipfile
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path, "r") as zf:
                    return "\n".join(zf.namelist())
        except Exception:
            pass
        try:
            import tarfile
            if path.suffix.lower() in {".tar", ".gz", ".bz2"}:
                with tarfile.open(path, "r") as tf:
                    return "\n".join(tf.getnames())
        except Exception:
            pass
        return None

    def _extract_image_ocr(self, path: Path) -> Optional[str]:
        try:
            import pytesseract
            from PIL import Image
            img = Image.open(path)
            text = pytesseract.image_to_string(img)
            return text.strip() or None
        except ImportError:
            return None

    def _index_file(self, path: Path, parent_path: str) -> dict:
        st = path.stat()
        name = path.name
        ext = path.suffix.lower()
        is_dir = path.is_dir()
        content_hash = None
        content_text = None

        if not is_dir and not is_binary_file(path):
            text = self._extract_content_text(path)
            if text is not None:
                content_text = text
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        return {
            "path": str(path),
            "name": name,
            "extension": ext or ("Folder" if is_dir else "File"),
            "size": st.st_size,
            "modified": int(st.st_mtime),
            "created": int(st.st_ctime),
            "is_folder": is_dir,
            "parent_path": parent_path,
            "content_hash": content_hash,
            "content_text": content_text,
        }

    def _detect_language(self, path: Path) -> Optional[str]:
        ext_map = {
            ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
            ".java": "Java", ".c": "C", ".cpp": "C++", ".cs": "C#",
            ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
            ".swift": "Swift", ".kt": "Kotlin", ".scala": "Scala",
            ".r": "R", ".pl": "Perl", ".lua": "Lua", ".hs": "Haskell",
            ".clj": "Clojure", ".erl": "Erlang", ".ex": "Elixir",
        }
        return ext_map.get(path.suffix.lower())

    def index_path(self, path: str) -> IndexStats:
        self._is_running = True
        self._stats = IndexStats(start_time=time.time())
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {path}")

        self.persistent_index.rebuild_index()
        all_files: List[Path] = []
        all_folders: List[Path] = []

        try:
            for entry in root.rglob("*"):
                if not self._is_running:
                    break
                if self._should_skip(entry):
                    continue
                try:
                    if entry.is_dir():
                        all_folders.append(entry)
                    else:
                        all_files.append(entry)
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass

        self._stats.total_folders = len(all_folders)
        self._stats.total_files = len(all_files)
        total = len(all_folders) + len(all_files)
        processed = 0

        folder_infos = []
        for folder in all_folders:
            info = self._index_file(folder, str(folder.parent))
            folder_infos.append(info)
            self._stats.total_size += info["size"]
            processed += 1
            if processed % 500 == 0 and self._progress_callback:
                self._progress_callback(processed, total)

        self.persistent_index.add_files_batch(folder_infos)

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
            fut_map = {}
            for file in all_files:
                if not self._is_running:
                    break
                parent = str(file.parent)
                fut = executor.submit(self._index_file, file, parent)
                fut_map[fut] = file

            for fut in as_completed(fut_map):
                if not self._is_running:
                    break
                try:
                    info = fut.result()
                    ext = info["extension"]
                    self._stats.extension_dist[ext] += 1
                    self._stats.total_size += info["size"]
                    if info["content_text"] is not None:
                        self._stats.content_indexed += 1
                    else:
                        self._stats.content_skipped += 1
                    self.persistent_index.add_file(info)
                except Exception:
                    self._stats.content_skipped += 1
                processed += 1
                if processed % 200 == 0 and self._progress_callback:
                    self._progress_callback(processed, total)

        self._stats.duration = time.time() - self._stats.start_time
        self.persistent_index.update_metadata("last_updated", str(int(time.time())))
        self.persistent_index.update_metadata(
            "index_stats", str({
                "total_files": self._stats.total_files,
                "total_folders": self._stats.total_folders,
                "total_size": self._stats.total_size,
                "duration": self._stats.duration,
            })
        )

        if self._done_callback:
            self._done_callback(self._stats)

        return self._stats

    def incremental_update(self, path: str) -> IndexStats:
        self._is_running = True
        self._stats = IndexStats(start_time=time.time())
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {path}")

        current_paths: Set[str] = set()
        new_files: List[Path] = []

        try:
            for entry in root.rglob("*"):
                if not self._is_running:
                    break
                if self._should_skip(entry):
                    continue
                try:
                    current_paths.add(str(entry))
                    if entry.is_file():
                        new_files.append(entry)
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass

        indexed_paths: Set[str] = set()
        for info in self.persistent_index.iter_all_files():
            indexed_paths.add(info["path"])

        added = current_paths - indexed_paths
        removed = indexed_paths - current_paths

        if removed:
            self.persistent_index.remove_files_batch(list(removed))

        total = len(added)
        processed = 0

        batch = []
        for p_str in added:
            p = Path(p_str)
            try:
                parent = str(p.parent)
                info = self._index_file(p, parent)
                ext = info["extension"]
                self._stats.extension_dist[ext] += 1
                self._stats.total_size += info["size"]
                if info["is_folder"]:
                    self._stats.total_folders += 1
                else:
                    self._stats.total_files += 1
                if info["content_text"] is not None:
                    self._stats.content_indexed += 1
                else:
                    self._stats.content_skipped += 1
                batch.append(info)
                processed += 1
                if len(batch) >= 100:
                    self.persistent_index.add_files_batch(batch)
                    batch.clear()
                if processed % 200 == 0 and self._progress_callback:
                    self._progress_callback(processed, total)
            except Exception:
                self._stats.content_skipped += 1
                processed += 1

        if batch:
            self.persistent_index.add_files_batch(batch)

        changed = 0
        for info in self.persistent_index.iter_all_files():
            if not self._is_running:
                break
            p = Path(info["path"])
            if not p.exists():
                continue
            try:
                new_hash = FileHash.from_path(p, sample=True)
                old = self._file_hashes.get(info["path"])
                if new_hash.changed(old) if old else False:
                    parent = str(p.parent)
                    new_info = self._index_file(p, parent)
                    self.persistent_index.add_file(new_info)
                    changed += 1
                self._file_hashes[info["path"]] = new_hash
            except Exception:
                continue

        self._stats.duration = time.time() - self._stats.start_time
        self._stats.total_files = self._stats.total_files or len(new_files)

        if self._done_callback:
            self._done_callback(self._stats)

        return self._stats

    @property
    def stats(self) -> IndexStats:
        return self._stats

    def get_index_stats(self) -> dict:
        return self.persistent_index.get_index_stats()

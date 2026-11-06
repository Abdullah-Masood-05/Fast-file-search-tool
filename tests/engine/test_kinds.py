"""kinds.py (the single kind table) and the no-Qt rule for engine/ and kinds.py."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from fast_file_search import kinds
from fast_file_search.engine.protocol import FOLDER, PACKAGE

SRC = Path(__file__).resolve().parents[2] / "src" / "fast_file_search"


def test_categories_match_the_design_table():
    assert set(kinds.CATEGORIES) == {"image", "document", "code", "audio", "video", "archive"}
    assert {"jpg", "png", "heic", "svg", "psd"} <= kinds.CATEGORIES["image"]
    assert {"pdf", "docx", "md", "csv", "key"} <= kinds.CATEGORIES["document"]
    assert {"py", "js", "ts", "c", "json", "yaml", "makefile", "dockerfile"} <= kinds.CATEGORIES["code"]
    assert {"mp3", "flac"} <= kinds.CATEGORIES["audio"]
    assert {"mp4", "mkv"} <= kinds.CATEGORIES["video"]
    assert {"zip", "7z", "dmg", "deb"} <= kinds.CATEGORIES["archive"]
    for exts in kinds.CATEGORIES.values():
        assert all(e == e.lower() and not e.startswith(".") for e in exts)


@pytest.mark.parametrize("name, flags, kind", [
    ("photo.JPG", 0, "image"), ("a.tar.gz", 0, "archive"), ("main.py", 0, "code"), ("notes.md", 0, "document"),
    ("song.flac", 0, "audio"), ("clip.MOV", 0, "video"), ("Makefile", 0, "code"), ("Dockerfile", 0, "code"),
    ("LICENSE", 0, "document"), ("CMakeLists.txt", 0, "code"), ("data.bin", 0, "file"), (".bashrc", 0, "file"),
    ("folder.png", FOLDER, "folder"), ("Foo.app", FOLDER | PACKAGE, "package"), ("Report.pages", FOLDER | PACKAGE,
                                                                                  "package"),
    ("Résumé.PDF", 0, "document"),
])
def test_kind_of(name, flags, kind):
    assert kinds.kind_of(name, flags) == kind


def test_labels_badges_languages():
    assert kinds.kind_label("app.js", 0) == "JavaScript"
    assert kinds.kind_label("x.png", 0) == "PNG image"
    assert kinds.kind_label("dir", FOLDER) == "Folder"
    assert kinds.kind_label("Foo.app", FOLDER | PACKAGE) == "Application"
    assert kinds.kind_label("blob.xyz", 0) == "XYZ file"
    assert kinds.kind_label("noext", 0) == "File"
    assert kinds.badge_text("app.js", 0) == "JS"
    assert kinds.badge_text("x.png", 0) == "PNG"
    assert kinds.badge_text("dir", FOLDER) == ""
    assert kinds.badge_text("thing.verylongext", 0) == "VERY"
    assert kinds.language_of("a.py") == "python"
    assert kinds.language_of("a.tsx") == "js"
    assert kinds.language_of("a.json") == "json"
    assert kinds.language_of("a.yml") == "yaml"
    assert kinds.language_of("a.png") is None
    assert kinds.language_of("Dockerfile") == "shell"


def test_language_keys_are_known_to_the_highlighter_table():
    # The preview's highlighter accepts these keys (ui/highlight.py normalize_language aliases).
    known = {"python", "js", "json", "html", "css", "c", "csharp", "java", "go", "rust", "swift", "shell",
             "powershell", "sql", "yaml", "toml", "ini", "markdown"}
    langs = {kinds.language_of("x." + e) for exts in kinds.CATEGORIES.values() for e in exts}
    assert langs - {None} <= known


def test_ext_rule():
    assert kinds.ext_of_key("a.b.c") == "c"
    assert kinds.ext_of_key(".bashrc") == ""
    assert kinds.ext_of_key("makefile") == ""
    assert kinds.ext_of_key("trailing.") == ""
    assert kinds.category_of_key("makefile", 0) == "code"


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_no_qt_imports_in_engine_or_kinds_source():
    files = sorted((SRC / "engine").glob("*.py")) + [SRC / "kinds.py"]
    assert len(files) > 5
    bad = [(f.name, m) for f in files for m in _imports(f) if m.split(".")[0] in ("PySide6", "PyQt6", "PyQt5")]
    assert bad == []


def test_importing_the_core_does_not_load_pyside6():
    code = ("import sys\n"
            "import fast_file_search.engine.textfold, fast_file_search.engine.query, fast_file_search.engine.ops\n"
            "import fast_file_search.engine.store, fast_file_search.engine.memindex\n"
            "import fast_file_search.engine.snapshot, fast_file_search.engine.userdata, fast_file_search.kinds\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('PySide6', 'PyQt6', 'shiboken6')]\n"
            "assert not bad, bad\n"
            "print('ok')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "ok" in res.stdout

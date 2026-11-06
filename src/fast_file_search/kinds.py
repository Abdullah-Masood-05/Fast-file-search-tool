"""File kinds: the single table used by the engine filters, the chips, the icons and the
preview (design F.2). Pure Python, no Qt.

Extensions are folded (lower case, no dot). ``kind_of`` looks at the last extension only,
plus a few well-known extension-less names (``Makefile``, ``Dockerfile``, ``LICENSE``).
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Tuple

from .engine.protocol import FOLDER, PACKAGE
from .engine.textfold import fold


def _set(words: str) -> FrozenSet[str]:
    return frozenset(words.split())


CATEGORIES: Dict[str, FrozenSet[str]] = {
    "image": _set("jpg jpeg png gif bmp tif tiff webp heic heif avif svg ico icns raw cr2 cr3 nef arw dng "
                  "orf rw2 psd xcf"),
    "document": _set("pdf doc docx odt rtf txt md markdown pages xls xlsx ods csv tsv numbers ppt pptx odp "
                     "key epub tex"),
    "code": _set("py pyw ipynb js mjs cjs ts tsx jsx java kt kts c h cpp cc cxx hpp hh cs go rs rb php swift "
                 "m mm scala sh bash zsh fish ps1 bat cmd sql html htm css scss sass less json jsonc yaml yml "
                 "toml ini cfg xml lua r dart vue svelte gradle cmake makefile dockerfile"),
    "audio": _set("mp3 m4a aac flac wav aiff aif ogg opus wma alac mid midi"),
    "video": _set("mp4 m4v mov mkv avi wmv webm flv mpg mpeg 3gp"),
    "archive": _set("zip 7z rar tar gz tgz bz2 xz zst dmg iso pkg deb rpm"),
}

# Extension-less names with a well-known kind (folded name -> category).
NAME_KINDS: Dict[str, str] = {
    "makefile": "code",
    "gnumakefile": "code",
    "dockerfile": "code",
    "containerfile": "code",
    "cmakelists.txt": "code",
    "license": "document",
    "licence": "document",
    "copying": "document",
    "readme": "document",
}

_EXT_CATEGORY: Dict[str, str] = {}
for _cat, _exts in CATEGORIES.items():
    for _e in _exts:
        _EXT_CATEGORY.setdefault(_e, _cat)

# ext -> (kind label, badge, highlighter language key)
_DETAILS: Dict[str, Tuple[str, str, Optional[str]]] = {
    "py": ("Python", "PY", "python"), "pyw": ("Python", "PY", "python"),
    "ipynb": ("Jupyter notebook", "IPYNB", "json"),
    "js": ("JavaScript", "JS", "js"), "mjs": ("JavaScript", "JS", "js"), "cjs": ("JavaScript", "JS", "js"),
    "jsx": ("JavaScript", "JSX", "js"), "ts": ("TypeScript", "TS", "js"), "tsx": ("TypeScript", "TSX", "js"),
    "json": ("JSON", "JSON", "json"), "jsonc": ("JSON", "JSON", "json"),
    "html": ("HTML", "HTML", "html"), "htm": ("HTML", "HTML", "html"), "xml": ("XML", "XML", "html"),
    "svg": ("SVG image", "SVG", "html"), "vue": ("Vue component", "VUE", "html"),
    "svelte": ("Svelte component", "SVLT", "html"),
    "css": ("CSS", "CSS", "css"), "scss": ("SCSS", "SCSS", "css"), "sass": ("Sass", "SASS", "css"),
    "less": ("Less", "LESS", "css"),
    "c": ("C", "C", "c"), "h": ("C header", "H", "c"), "cpp": ("C++", "C++", "c"), "cc": ("C++", "C++", "c"),
    "cxx": ("C++", "C++", "c"), "hpp": ("C++ header", "HPP", "c"), "hh": ("C++ header", "HH", "c"),
    "m": ("Objective-C", "M", "c"), "mm": ("Objective-C++", "MM", "c"),
    "cs": ("C#", "C#", "csharp"), "java": ("Java", "JAVA", "java"), "kt": ("Kotlin", "KT", "java"),
    "kts": ("Kotlin script", "KTS", "java"), "scala": ("Scala", "SCALA", "java"),
    "go": ("Go", "GO", "go"), "rs": ("Rust", "RS", "rust"), "swift": ("Swift", "SWIFT", "swift"),
    "rb": ("Ruby", "RB", None), "php": ("PHP", "PHP", None), "lua": ("Lua", "LUA", None),
    "r": ("R", "R", None), "dart": ("Dart", "DART", None), "gradle": ("Gradle", "GRDL", None),
    "cmake": ("CMake", "CMAKE", None),
    "sh": ("Shell script", "SH", "shell"), "bash": ("Shell script", "BASH", "shell"),
    "zsh": ("Shell script", "ZSH", "shell"), "fish": ("Shell script", "FISH", "shell"),
    "ps1": ("PowerShell", "PS1", "powershell"), "psm1": ("PowerShell", "PSM1", "powershell"),
    "bat": ("Batch file", "BAT", None), "cmd": ("Batch file", "CMD", None),
    "sql": ("SQL", "SQL", "sql"),
    "yaml": ("YAML", "YAML", "yaml"), "yml": ("YAML", "YML", "yaml"), "toml": ("TOML", "TOML", "toml"),
    "ini": ("INI", "INI", "ini"), "cfg": ("Configuration", "CFG", "ini"), "conf": ("Configuration", "CONF", "ini"),
    "md": ("Markdown", "MD", "markdown"), "markdown": ("Markdown", "MD", "markdown"),
    "txt": ("Plain text", "TXT", None), "log": ("Log file", "LOG", None), "csv": ("CSV", "CSV", None),
    "tsv": ("TSV", "TSV", None), "tex": ("TeX document", "TEX", None),
    "pdf": ("PDF document", "PDF", None), "doc": ("Word document", "DOC", None),
    "docx": ("Word document", "DOCX", None), "odt": ("OpenDocument text", "ODT", None),
    "rtf": ("Rich text", "RTF", None), "pages": ("Pages document", "PAGES", None),
    "xls": ("Excel spreadsheet", "XLS", None), "xlsx": ("Excel spreadsheet", "XLSX", None),
    "ods": ("OpenDocument spreadsheet", "ODS", None), "numbers": ("Numbers spreadsheet", "NMBR", None),
    "ppt": ("PowerPoint presentation", "PPT", None), "pptx": ("PowerPoint presentation", "PPTX", None),
    "odp": ("OpenDocument presentation", "ODP", None), "key": ("Keynote presentation", "KEY", None),
    "epub": ("EPUB book", "EPUB", None),
    "jpg": ("JPEG image", "JPG", None), "jpeg": ("JPEG image", "JPG", None), "png": ("PNG image", "PNG", None),
    "gif": ("GIF image", "GIF", None), "bmp": ("BMP image", "BMP", None), "tif": ("TIFF image", "TIFF", None),
    "tiff": ("TIFF image", "TIFF", None), "webp": ("WebP image", "WEBP", None),
    "heic": ("HEIC image", "HEIC", None), "heif": ("HEIF image", "HEIF", None),
    "avif": ("AVIF image", "AVIF", None), "ico": ("Icon", "ICO", None), "icns": ("Icon", "ICNS", None),
    "psd": ("Photoshop document", "PSD", None), "xcf": ("GIMP image", "XCF", None),
    "raw": ("RAW image", "RAW", None), "cr2": ("RAW image", "CR2", None), "cr3": ("RAW image", "CR3", None),
    "nef": ("RAW image", "NEF", None), "arw": ("RAW image", "ARW", None), "dng": ("RAW image", "DNG", None),
    "orf": ("RAW image", "ORF", None), "rw2": ("RAW image", "RW2", None),
    "mp3": ("MP3 audio", "MP3", None), "m4a": ("MPEG-4 audio", "M4A", None), "aac": ("AAC audio", "AAC", None),
    "flac": ("FLAC audio", "FLAC", None), "wav": ("WAV audio", "WAV", None), "aiff": ("AIFF audio", "AIFF", None),
    "aif": ("AIFF audio", "AIF", None), "ogg": ("Ogg audio", "OGG", None), "opus": ("Opus audio", "OPUS", None),
    "wma": ("WMA audio", "WMA", None), "alac": ("ALAC audio", "ALAC", None), "mid": ("MIDI", "MID", None),
    "midi": ("MIDI", "MIDI", None),
    "mp4": ("MPEG-4 video", "MP4", None), "m4v": ("MPEG-4 video", "M4V", None),
    "mov": ("QuickTime movie", "MOV", None), "mkv": ("Matroska video", "MKV", None),
    "avi": ("AVI video", "AVI", None), "wmv": ("WMV video", "WMV", None), "webm": ("WebM video", "WEBM", None),
    "flv": ("Flash video", "FLV", None), "mpg": ("MPEG video", "MPG", None), "mpeg": ("MPEG video", "MPEG", None),
    "3gp": ("3GP video", "3GP", None),
    "zip": ("ZIP archive", "ZIP", None), "7z": ("7-Zip archive", "7Z", None), "rar": ("RAR archive", "RAR", None),
    "tar": ("Tar archive", "TAR", None), "gz": ("Gzip archive", "GZ", None), "tgz": ("Gzip archive", "TGZ", None),
    "bz2": ("Bzip2 archive", "BZ2", None), "xz": ("XZ archive", "XZ", None), "zst": ("Zstandard archive", "ZST", None),
    "dmg": ("Disk image", "DMG", None), "iso": ("Disk image", "ISO", None), "pkg": ("Installer package", "PKG", None),
    "deb": ("Debian package", "DEB", None), "rpm": ("RPM package", "RPM", None),
    "exe": ("Application", "EXE", None), "msi": ("Installer package", "MSI", None),
    "app": ("Application", "APP", None),
}

_NAME_DETAILS: Dict[str, Tuple[str, str, Optional[str]]] = {
    "makefile": ("Makefile", "MAKE", None), "gnumakefile": ("Makefile", "MAKE", None),
    "dockerfile": ("Dockerfile", "DOCK", "shell"), "containerfile": ("Containerfile", "CONT", "shell"),
    "cmakelists.txt": ("CMake", "CMAKE", None),
    "license": ("License", "LIC", None), "licence": ("License", "LIC", None), "copying": ("License", "LIC", None),
    "readme": ("Read me", "TXT", None),
}

_PACKAGE_LABELS: Dict[str, str] = {
    "app": "Application", "appex": "App extension", "framework": "Framework", "bundle": "Bundle",
    "kext": "Kernel extension", "plugin": "Plug-in", "prefpane": "Preference pane",
    "photoslibrary": "Photos library", "musiclibrary": "Music library", "pages": "Pages document",
    "numbers": "Numbers spreadsheet", "key": "Keynote presentation", "rtfd": "Rich text with attachments",
    "sparsebundle": "Disk image", "xcarchive": "Xcode archive", "playground": "Playground",
}


def ext_of_key(key: str) -> str:
    """Extension of an already folded name. The one rule used everywhere: the text after the
    last dot, provided a dot occurs after the first character ('' for '.bashrc', 'Makefile')."""
    if "." not in key[1:]:
        return ""
    return key.rpartition(".")[2]


def _ext(name: str) -> str:
    return ext_of_key(fold(name))


def kind_of(name: str, flags: int) -> str:
    """'folder', 'package', a CATEGORIES key, or 'file'."""
    if flags & PACKAGE:
        return "package"
    if flags & FOLDER:
        return "folder"
    key = fold(name)
    named = NAME_KINDS.get(key)
    if named:
        return named
    return _EXT_CATEGORY.get(ext_of_key(key), "file")


def category_of_key(key: str, flags: int) -> str:
    """``kind_of`` for an already folded name (the engine's per-slot rule)."""
    if flags & PACKAGE:
        return "package"
    if flags & FOLDER:
        return "folder"
    return NAME_KINDS.get(key) or _EXT_CATEGORY.get(ext_of_key(key), "file")


def _details(name: str) -> Optional[Tuple[str, str, Optional[str]]]:
    hit = _NAME_DETAILS.get(fold(name))
    if hit:
        return hit
    return _DETAILS.get(_ext(name))


def kind_label(name: str, flags: int) -> str:
    """Human label: "JavaScript", "PNG image", "Folder", "Application"."""
    if flags & PACKAGE:
        return _PACKAGE_LABELS.get(_ext(name), "Package")
    if flags & FOLDER:
        return "Folder"
    hit = _details(name)
    if hit:
        return hit[0]
    ext = _ext(name)
    return f"{ext.upper()} file" if ext else "File"


def badge_text(name: str, flags: int) -> str:
    """Short badge for the preview header: "JS", "PNG"; "" for folders."""
    if flags & FOLDER and not flags & PACKAGE:
        return ""
    hit = _details(name)
    if hit:
        return hit[1]
    return _ext(name).upper()[:4]


def language_of(name: str) -> Optional[str]:
    """Highlighter key ('python', 'js', 'json', 'html', 'css', 'c', 'csharp', 'java', 'go',
    'rust', 'swift', 'shell', 'powershell', 'sql', 'yaml', 'toml', 'ini', 'markdown') or None."""
    hit = _details(name)
    return hit[2] if hit else None

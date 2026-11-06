"""Lightweight syntax highlighting for the preview pane.

One ``Highlighter`` (a ``QSyntaxHighlighter``) drives per-language rule tables.  Each
language compiles to a single "master" regular expression made of named alternatives
(multi-line openers, comments, strings, keywords, numbers, extras), so a line costs one
``search`` call per token.  Multi-line constructs (block comments, triple-quoted
strings, fenced code) are carried between lines with ``setCurrentBlockState``.

Colours come from the palette only: keyword = Highlight, string and number = a mix of
Text and Highlight, comment = the theme's secondary text colour.  Every foreground is
pushed towards Text until it reaches a contrast of 4.5:1 against Base.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtGui import QColor, QFont, QPalette, QSyntaxHighlighter, QTextCharFormat, QTextDocument
from PySide6.QtWidgets import QApplication

from . import theme

MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
MIN_CONTRAST = 4.5

# ----------------------------------------------------------------------------- languages
_ALIASES = {
    "py": "python", "python": "python",
    "js": "js", "javascript": "js", "jsx": "js", "mjs": "js", "cjs": "js",
    "ts": "js", "tsx": "js", "typescript": "js",
    "json": "json", "jsonc": "json",
    "html": "html", "htm": "html", "xml": "html", "svg": "html", "xhtml": "html",
    "css": "css", "scss": "css", "less": "css", "sass": "css",
    "c": "c", "h": "c", "cpp": "c", "c++": "c", "cc": "c", "cxx": "c", "hpp": "c",
    "objc": "c", "objective-c": "c", "m": "c", "mm": "c",
    "cs": "csharp", "csharp": "csharp", "c#": "csharp",
    "java": "java", "kt": "java", "kotlin": "java", "scala": "java",
    "go": "go", "golang": "go",
    "rs": "rust", "rust": "rust",
    "swift": "swift",
    "sh": "shell", "bash": "shell", "zsh": "shell", "shell": "shell", "fish": "shell",
    "ps1": "powershell", "powershell": "powershell", "psm1": "powershell",
    "sql": "sql",
    "yaml": "yaml", "yml": "yaml",
    "toml": "toml",
    "ini": "ini", "cfg": "ini", "conf": "ini", "properties": "ini",
    "md": "markdown", "markdown": "markdown",
}

LANGUAGES = tuple(sorted(set(_ALIASES.values())))


def normalize_language(key: Optional[str]) -> Optional[str]:
    """Map a language key or extension ('ts', 'C++', 'yml') to a supported language."""
    if not key:
        return None
    return _ALIASES.get(key.strip().lower().lstrip("."))


def _words(s: str) -> str:
    return r"\b(?:" + "|".join(sorted(s.split(), key=len, reverse=True)) + r")\b"


_NUMBER = r"\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?[a-zA-Z]{0,3})\b"
_DQ = r'"(?:[^"\\]|\\.)*(?:"|$)'
_SQ = r"'(?:[^'\\]|\\.)*(?:'|$)"
_CHAR = r"'(?:\\.[^']{0,8}|[^'\\])'"
_C_LINE = r"//.*"
_HASH = r"#.*"

# Rule items: ("ml", open, close, kind) | ("r", kind, pattern)
_KW = {
    "python": "False None True and as assert async await break class continue def del elif else except "
              "finally for from global if import in is lambda nonlocal not or pass raise return try while "
              "with yield self cls match case print",
    "js": "async await break case catch class const continue debugger default delete do else enum export "
          "extends false finally for from function if implements import in instanceof interface let new null "
          "of package private protected public return static super switch this throw true try type typeof "
          "undefined var void while with yield as readonly abstract declare namespace",
    "c": "alignas alignof auto bool break case catch char class const constexpr continue default delete do "
         "double else enum explicit extern false final float for friend goto if inline int long mutable "
         "namespace new noexcept nullptr operator override private protected public register return short "
         "signed sizeof static struct switch template this throw true try typedef typename union unsigned "
         "using virtual void volatile while NULL @interface @implementation @end @property id nil YES NO self",
    "csharp": "abstract as async await base bool break byte case catch char checked class const continue "
              "decimal default delegate do double else enum event explicit extern false finally fixed float "
              "for foreach goto if implicit in int interface internal is lock long namespace new null object "
              "operator out override params private protected public readonly ref return sbyte sealed short "
              "sizeof static string struct switch this throw true try typeof uint ulong unchecked unsafe "
              "ushort using var virtual void volatile while record init get set",
    "java": "abstract assert boolean break byte case catch char class const continue default do double else "
            "enum extends false final finally float for goto if implements import instanceof int interface "
            "long native new null package private protected public return short static strictfp super "
            "switch synchronized this throw throws transient true try void volatile while fun val var when "
            "object override data sealed open lateinit companion inline suspend",
    "go": "break case chan const continue default defer else fallthrough false for func go goto if import "
          "interface map nil package range return select struct switch true type var iota",
    "rust": "as async await break const continue crate dyn else enum extern false fn for if impl in let loop "
            "match mod move mut pub ref return self Self static struct super trait true type unsafe use "
            "where while",
    "swift": "actor associatedtype async await break case catch class continue default defer deinit do else "
             "enum extension fallthrough false fileprivate for func guard if import in init inout "
             "internal is let nil open operator private protocol public repeat rethrows return self Self "
             "static struct subscript super switch throw throws true try typealias var where while",
    "shell": "if then else elif fi for while until do done case esac in function select time return exit "
             "break continue local export readonly declare unset shift source alias echo cd set trap eval exec",
    "powershell": "begin break catch class continue data do dynamicparam else elseif end enum exit filter "
                  "finally for foreach from function if in param process return switch throw trap try until "
                  "using var while workflow $true $false $null",
    "sql": "add all alter and any as asc between by case check column commit constraint create cross "
           "database default delete desc distinct drop else end exists foreign from full group having if in "
           "index inner insert into is join key left like limit not null offset on or order outer primary "
           "references replace right rollback select set table then top transaction truncate union unique "
           "update values view when where with true false",
    "yaml": "true false null yes no on off True False Null",
    "toml": "true false",
    "ini": "true false yes no on off",
}


def _kw(lang: str, flags_ci: bool = False) -> Tuple[str, str]:
    return ("r", "keyword", _words(_KW[lang]))


def _c_like(lang: str, extra: Optional[list] = None, line_comment: str = _C_LINE) -> list:
    items: list = [
        ("ml", r"/\*", r"\*/", "comment"),
        ("r", "comment", line_comment),
    ]
    if extra:
        items += extra
    items += [
        ("r", "string", _DQ),
        ("r", "string", _CHAR),
        _kw(lang),
        ("r", "number", _NUMBER),
    ]
    return items


def _rules(lang: str) -> Tuple[list, int]:
    """Rule items and regex flags for a canonical language."""
    if lang == "python":
        return [
            ("ml", r'[rRbBuUfF]{0,2}"""', r'(?<!\\)"""', "string"),
            ("ml", r"[rRbBuUfF]{0,2}'''", r"(?<!\\)'''", "string"),
            ("r", "comment", _HASH),
            ("r", "string", r"[rRbBuUfF]{0,2}" + _DQ),
            ("r", "string", r"[rRbBuUfF]{0,2}" + _SQ),
            ("r", "keyword", r"@[A-Za-z_][\w.]*"),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], 0
    if lang == "js":
        return [
            ("ml", r"/\*", r"\*/", "comment"),
            ("ml", r"`", r"(?<!\\)`", "string"),
            ("r", "comment", _C_LINE),
            ("r", "string", _DQ),
            ("r", "string", _SQ),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], 0
    if lang == "json":
        return [
            ("r", "comment", _C_LINE),
            ("r", "keyword", r'"(?:[^"\\]|\\.)*"(?=\s*:)'),
            ("r", "string", _DQ),
            ("r", "keyword", r"\b(?:true|false|null)\b"),
            ("r", "number", r"-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"),
        ], 0
    if lang == "html":
        return [
            ("ml", r"<!--", r"-->", "comment"),
            ("r", "comment", r"<!\[CDATA\[.*?\]\]>|<\?.*?\?>"),
            ("r", "keyword", r"</?[A-Za-z][\w:.-]*|/?>|<!DOCTYPE"),
            ("r", "string", r'"[^"]*"|\'[^\']*\''),
            ("r", "number", r"&#?\w+;"),
        ], re.IGNORECASE
    if lang == "css":
        return [
            ("ml", r"/\*", r"\*/", "comment"),
            ("r", "comment", _C_LINE),
            ("r", "string", _DQ),
            ("r", "string", _SQ),
            ("r", "keyword", r"@[\w-]+"),
            ("r", "keyword", r"[\w-]+(?=\s*:(?!:))(?![^{]*\{)"),
            ("r", "number", r"#[0-9a-fA-F]{3,8}\b"),
            ("r", "number", r"-?(?:\d+\.\d+|\.\d+|\d+)(?:px|em|rem|ex|ch|vh|vw|vmin|vmax|pt|cm|mm|in|s|ms|deg|fr|%)?"),
        ], 0
    if lang == "c":
        return _c_like(lang, [("r", "keyword", r"^\s*#\s*\w+")]), 0
    if lang == "csharp":
        return _c_like(lang, [("r", "string", r'@"(?:[^"]|"")*"'), ("r", "keyword", r"^\s*#\s*\w+")]), 0
    if lang == "java":
        return _c_like(lang, [("r", "keyword", r"@[A-Za-z_]\w*")]), 0
    if lang == "go":
        return [
            ("ml", r"/\*", r"\*/", "comment"),
            ("ml", r"`", r"`", "string"),
            ("r", "comment", _C_LINE),
            ("r", "string", _DQ),
            ("r", "string", _CHAR),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], 0
    if lang == "rust":
        return _c_like(lang, [("r", "keyword", r"#!?\[[^\]]*\]")]), 0
    if lang == "swift":
        return _c_like(lang, [("r", "keyword", r"@[A-Za-z_]\w*")]), 0
    if lang == "shell":
        return [
            ("r", "comment", r"(?:(?<=\s)|^)#.*"),
            ("r", "string", _DQ),
            ("r", "string", r"'[^']*(?:'|$)"),
            ("r", "number", r"\$(?:\{[^}]*\}|\w+|[?#@*$!0-9])"),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], 0
    if lang == "powershell":
        return [
            ("ml", r"<#", r"#>", "comment"),
            ("r", "comment", _HASH),
            ("r", "string", _DQ),
            ("r", "string", r"'[^']*(?:'|$)"),
            ("r", "number", r"\$\w+"),
            ("r", "keyword", r"\b[A-Z][a-z]+-[A-Z]\w+\b"),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], re.IGNORECASE
    if lang == "sql":
        return [
            ("ml", r"/\*", r"\*/", "comment"),
            ("r", "comment", r"--.*"),
            ("r", "string", r"'(?:[^']|'')*(?:'|$)"),
            ("r", "string", r'"[^"]*(?:"|$)'),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], re.IGNORECASE
    if lang == "yaml":
        return [
            ("r", "comment", r"(?:(?<=\s)|^)#.*"),
            ("r", "keyword", r"^---|^\.\.\."),
            ("r", "string", _DQ),
            ("r", "string", r"'[^']*(?:'|$)"),
            ("r", "keyword", r"(?:(?<=^)|(?<=[\s-]))[\w.\-/][\w.\-/ ]*?(?=\s*:(?:\s|$))"),
            ("r", "number", r"[&*]\w+"),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], 0
    if lang == "toml":
        return [
            ("r", "comment", _HASH),
            ("ml", r'"""', r'(?<!\\)"""', "string"),
            ("ml", r"'''", r"'''", "string"),
            ("r", "string", _DQ),
            ("r", "string", r"'[^']*(?:'|$)"),
            ("r", "keyword", r"^\s*\[\[?[^\]]*\]\]?"),
            ("r", "keyword", r"^\s*[\w.\-\"]+(?=\s*=)"),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], 0
    if lang == "ini":
        return [
            ("r", "comment", r"^\s*[#;].*"),
            ("r", "keyword", r"^\s*\[[^\]]*\]"),
            ("r", "keyword", r"^\s*[\w.\-]+(?=\s*[=:])"),
            ("r", "string", _DQ),
            _kw(lang),
            ("r", "number", _NUMBER),
        ], re.IGNORECASE
    if lang == "markdown":
        return [
            ("ml", r"^\s*```.*", r"^\s*```", "string"),
            ("ml", r"^\s*~~~.*", r"^\s*~~~", "string"),
            ("r", "heading", r"^\s{0,3}#{1,6}\s.*"),
            ("r", "comment", r"^\s*>.*"),
            ("r", "string", r"`[^`]+`"),
            ("r", "number", r"!?\[[^\]]*\]\([^)]*\)"),
            ("r", "keyword", r"^\s*(?:[-*+]|\d+[.)])\s"),
            ("r", "bold", r"\*\*[^*]+\*\*|__[^_]+__"),
            ("r", "italic", r"(?<![\w*])\*[^*\s][^*]*\*(?![\w*])|(?<![\w_])_[^_\s][^_]*_(?![\w_])"),
        ], 0
    raise KeyError(lang)


@dataclass
class _Lang:
    master: "re.Pattern"
    groups: Dict[str, Tuple[str, int]]  # group name -> (kind, multi-line index or -1)
    closers: List[Tuple["re.Pattern", str]]  # multi-line closers and their kind


_COMPILED: Dict[str, _Lang] = {}


def _compile(lang: str) -> _Lang:
    cached = _COMPILED.get(lang)
    if cached is not None:
        return cached
    items, flags = _rules(lang)
    parts: List[str] = []
    groups: Dict[str, Tuple[str, int]] = {}
    closers: List[Tuple[re.Pattern, str]] = []
    for i, item in enumerate(items):
        name = f"g{i}"
        if item[0] == "ml":
            _, opener, closer, kind = item
            groups[name] = (kind, len(closers))
            closers.append((re.compile(closer, flags), kind))
            parts.append(f"(?P<{name}>{opener})")
        else:
            _, kind, pattern = item
            groups[name] = (kind, -1)
            parts.append(f"(?P<{name}>{pattern})")
    master = re.compile("|".join(parts), flags)
    result = _Lang(master, groups, closers)
    _COMPILED[lang] = result
    return result


# ----------------------------------------------------------------------------- colours
def _toward_text(color: QColor, text: QColor, background: QColor, minimum: float = MIN_CONTRAST) -> QColor:
    """Blend ``color`` towards the text colour until it reaches ``minimum`` contrast."""
    t = 0.0
    out = QColor(color)
    while theme.contrast_ratio(out, background) < minimum and t < 1.0:
        t = min(1.0, t + 0.05)
        out = theme._mix(text, color, t)
    return out


def build_formats(palette: Optional[QPalette] = None) -> Dict[str, QTextCharFormat]:
    pal = palette or QApplication.palette()
    text = pal.color(QPalette.ColorRole.Text)
    base = pal.color(QPalette.ColorRole.Base)
    accent = pal.color(QPalette.ColorRole.Highlight)

    def fg(color: QColor) -> QColor:
        return _toward_text(color, text, base)

    keyword = fg(accent)
    string = fg(theme._mix(text, accent, 0.45))
    number = fg(theme._mix(text, accent, 0.7))
    comment = theme.secondary_text_color(pal, base)

    def make(color: Optional[QColor] = None, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        f = QTextCharFormat()
        if color is not None:
            f.setForeground(color)
        if bold:
            f.setFontWeight(QFont.Weight.DemiBold)
        if italic:
            f.setFontItalic(True)
        return f

    return {
        "keyword": make(keyword),
        "string": make(string),
        "number": make(number),
        "comment": make(comment, italic=True),
        "heading": make(keyword, bold=True),
        "bold": make(None, bold=True),
        "italic": make(None, italic=True),
    }


# ----------------------------------------------------------------------------- highlighter
class Highlighter(QSyntaxHighlighter):
    """Syntax colours for one language (``None`` = no highlighting)."""

    def __init__(self, document: Optional[QTextDocument] = None, language: Optional[str] = None):
        super().__init__(document)
        self._lang: Optional[_Lang] = None
        self._language: Optional[str] = None
        self.formats: Dict[str, QTextCharFormat] = build_formats()
        self._fmt_by_group: Dict[str, QTextCharFormat] = {}
        self._set(language)

    @property
    def language(self) -> Optional[str]:
        return self._language

    def _set(self, language: Optional[str]) -> None:
        lang = normalize_language(language)
        self._language = lang
        self._lang = _compile(lang) if lang else None
        self._fmt_by_group = {}
        if self._lang is not None:
            self._fmt_by_group = {name: self.formats[kind] for name, (kind, _) in self._lang.groups.items()}

    def set_language(self, language: Optional[str], rehighlight: bool = True) -> None:
        self._set(language)
        if rehighlight and self.document() is not None:
            self.rehighlight()

    def refresh_formats(self, palette: Optional[QPalette] = None) -> None:
        """Rebuild the formats after a palette change."""
        self.formats = build_formats(palette)
        self._set(self._language)
        if self.document() is not None and self._lang is not None:
            self.rehighlight()

    def highlightBlock(self, text: Optional[str]) -> None:  # noqa: N802 (Qt naming)
        lang = self._lang
        self.setCurrentBlockState(0)
        if lang is None or not text and self.previousBlockState() <= 0:
            return
        n = len(text)
        pos = 0
        set_format = self.setFormat
        formats = self.formats
        state = self.previousBlockState()
        if state > 0:
            closer, kind = lang.closers[state - 1]
            m = closer.search(text)
            if m is None:
                set_format(0, n, formats[kind])
                self.setCurrentBlockState(state)
                return
            pos = m.end()
            set_format(0, pos, formats[kind])
        by_group = self._fmt_by_group
        groups = lang.groups
        search = lang.master.search
        while pos < n:
            m = search(text, pos)
            if m is None:
                break
            start, end = m.span()
            name = m.lastgroup
            idx = groups[name][1]
            if idx >= 0:
                closer, kind = lang.closers[idx]
                c = closer.search(text, end)
                if c is None:
                    set_format(start, n - start, formats[kind])
                    self.setCurrentBlockState(idx + 1)
                    return
                end = c.end()
                set_format(start, end - start, formats[kind])
            elif end > start:
                set_format(start, end - start, by_group[name])
            pos = end if end > pos else pos + 1


def highlight_allowed(text: str) -> bool:
    """Highlighting is skipped for very long files or lines."""
    lines = text.split("\n")
    return len(lines) <= MAX_LINES and (not lines or max(map(len, lines)) <= MAX_LINE_LENGTH)

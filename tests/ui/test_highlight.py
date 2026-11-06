"""Syntax highlighter: rule tables, multi-line state, contrast, speed."""
import time

import pytest
from PySide6.QtGui import QColor, QPalette, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication

from fast_file_search.ui import highlight, theme
from fast_file_search.ui.highlight import Highlighter, build_formats, normalize_language


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def spans(doc: QTextDocument, h: Highlighter):
    """[(block, start, length, kind)] for every formatted range."""
    out = []
    block = doc.begin()
    while block.isValid():
        for r in block.layout().formats():
            for kind, f in h.formats.items():
                if kind == "heading":
                    continue
                if (f.foreground() == r.format.foreground() and f.fontItalic() == r.format.fontItalic()
                        and f.fontWeight() == r.format.fontWeight()):
                    out.append((block.blockNumber(), r.start, r.length, kind))
                    break
        block = block.next()
    return out


def run(text: str, lang):
    doc = QTextDocument()
    doc.setPlainText(text)
    h = Highlighter(doc, lang)
    h.rehighlight()
    return doc, h


def kinds_of(doc, h, block_no):
    text = doc.findBlockByNumber(block_no).text()
    return {text[a:a + n]: k for b, a, n, k in spans(doc, h) if b == block_no}


SAMPLES = {
    "python": ("def f(x):  # note\n    return 'a' + str(42)\n",
               {"def": "keyword", "# note": "comment", "'a'": "string", "42": "number"}),
    "js": ("const a = `x`; // c\nlet n = 3.5;\n", {"const": "keyword", "`x`": "string", "// c": "comment"}),
    "json": ('{"k": [1, true, "v"]}\n', {'"k"': "keyword", "true": "keyword", '"v"': "string", "1": "number"}),
    "html": ('<a href="x">t</a><!-- c -->\n', {"<a": "keyword", '"x"': "string", "<!-- c -->": "comment"}),
    "css": ("a { color: #fff; margin: 4px; } /* c */\n",
            {"#fff": "number", "4px": "number", "/* c */": "comment"}),
    "c": ("#include <x>\nint main() { return 0; } // c\n", {"#include": "keyword", "// c": "comment"}),
    "csharp": ('public class A { string s = "x"; }\n', {"public": "keyword", '"x"': "string"}),
    "java": ("@Override\nfinal int x = 1;\n", {"@Override": "keyword", "final": "keyword"}),
    "go": ("func main() { s := `raw` }\n", {"func": "keyword", "`raw`": "string"}),
    "rust": ("fn main() { let x = 1; }\n", {"fn": "keyword", "let": "keyword", "1": "number"}),
    "swift": ('let x = "s"\n', {"let": "keyword", '"s"': "string"}),
    "shell": ('if [ "$X" ]; then echo $HOME # c\nfi\n', {"if": "keyword", "$HOME": "number", "# c": "comment"}),
    "powershell": ("Get-ChildItem $x # c\n", {"Get-ChildItem": "keyword", "$x": "number", "# c": "comment"}),
    "sql": ("SELECT a FROM t WHERE x = 'y' -- c\n",
            {"SELECT": "keyword", "FROM": "keyword", "'y'": "string", "-- c": "comment"}),
    "yaml": ('name: "v"  # c\nitems:\n  - 3\n', {"name": "keyword", '"v"': "string", "# c": "comment"}),
    "toml": ('[tool]\nname = "x"\n', {"[tool]": "keyword", '"x"': "string"}),
    "ini": ("[s]\nk = 1 ; c\n", {"[s]": "keyword", "k": "keyword"}),
    "markdown": ("# Title\n\nsome `code` and **bold**\n", {"`code`": "string", "**bold**": "bold"}),
}


@pytest.mark.parametrize("lang", sorted(SAMPLES))
def test_language_rules(qapp, lang):
    text, expected = SAMPLES[lang]
    doc, h = run(text, lang)
    found = {}
    for b in range(doc.blockCount()):
        found.update(kinds_of(doc, h, b))
    for token, kind in expected.items():
        assert found.get(token) == kind, (lang, token, found)


def test_every_language_has_a_sample():
    assert set(SAMPLES) == set(highlight.LANGUAGES)


def test_aliases():
    assert normalize_language("TS") == "js"
    assert normalize_language(".yml") == "yaml"
    assert normalize_language("c++") == "c"
    assert normalize_language("kotlin") == "java"
    assert normalize_language("nope") is None
    assert normalize_language(None) is None


def test_python_multiline_string_state(qapp):
    doc, h = run('x = """a\nb # not comment\nc""" + 1\n', "python")
    assert kinds_of(doc, h, 1) == {"b # not comment": "string"}
    assert kinds_of(doc, h, 2).get('c"""') == "string"
    assert kinds_of(doc, h, 2).get("1") == "number"


def test_c_block_comment_state(qapp):
    doc, h = run("int a; /* start\nmiddle int\nend */ int b;\n", "c")
    assert kinds_of(doc, h, 1) == {"middle int": "comment"}
    k2 = kinds_of(doc, h, 2)
    assert k2["end */"] == "comment" and k2["int"] == "keyword"


def test_markdown_fence(qapp):
    doc, h = run("```py\nint x # y\n```\ntext **b**\n", "markdown")
    assert kinds_of(doc, h, 1) == {"int x # y": "string"}
    assert kinds_of(doc, h, 3).get("**b**") == "bold"


def test_editing_a_comment_reflows_following_lines(qapp):
    doc, h = run("a\nb\nc\n", "c")
    QTextCursor(doc).insertText("/* ")
    h.rehighlight()
    assert kinds_of(doc, h, 1) == {"b": "comment"}


def test_language_none_is_plain(qapp):
    doc, h = run("def x(): pass\n", None)
    assert spans(doc, h) == []


def test_highlight_allowed_limits():
    assert highlight.highlight_allowed("a\n" * 1999)
    assert not highlight.highlight_allowed("a\n" * 2001)
    assert not highlight.highlight_allowed("x" * 2001)


_PALETTES = {
    "light": lambda: theme._fallback_palette(False, QColor("#0067c0")),
    "dark": lambda: theme._fallback_palette(True, QColor("#0067c0")),
    "dark-dim-accent": lambda: theme._fallback_palette(True, QColor("#1a3a6e")),
    "light-pale-accent": lambda: theme._fallback_palette(False, QColor("#9ecbff")),
}


@pytest.mark.parametrize("name", sorted(_PALETTES))
def test_format_contrast_against_base(qapp, name):
    pal = _PALETTES[name]()
    base = pal.color(QPalette.ColorRole.Base)
    checked = 0
    for kind, f in build_formats(pal).items():
        if kind in ("bold", "italic"):  # no colour of their own: they inherit Text
            continue
        ratio = theme.contrast_ratio(f.foreground().color(), base)
        assert ratio >= 3.0, (name, kind, ratio)
        checked += 1
    assert checked == 5  # keyword, string, number, comment, heading


def test_system_palette_contrast(qapp):
    pal = QApplication.palette()
    base = pal.color(QPalette.ColorRole.Base)
    for kind, f in build_formats(pal).items():
        if kind not in ("bold", "italic"):
            assert theme.contrast_ratio(f.foreground().color(), base) >= 3.0


def test_2000_line_python_highlights_within_50ms(qapp):
    block = (
        'import os\n\n@decorator\ndef function_name(arg, other=42):\n    """Docstring with words."""\n'
        '    value = {"key": [1, 2.5, None], "s": \'text\'}  # trailing comment\n'
        "    if arg and not other:\n        return value\n    for i in range(10):\n        print(i, f'{i}')\n"
    )
    text = "\n".join((block * 200).split("\n")[:2000])
    best = 1e9
    for _ in range(5):
        doc = QTextDocument()
        doc.setPlainText(text)
        h = Highlighter(None, "python")
        h.setDocument(doc)
        t = time.perf_counter()
        h.rehighlight()
        best = min(best, (time.perf_counter() - t) * 1000)
        h.setDocument(None)
    assert doc.blockCount() == 2000
    print(f"2000-line python rehighlight: {best:.1f} ms")
    assert best <= 50

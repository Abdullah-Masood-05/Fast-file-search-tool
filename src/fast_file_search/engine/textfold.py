"""Text normalization shared by the index, the query parser and highlighting (design A.2).

``fold`` produces the search key of a name or a query term: case- and accent-insensitive,
NFC/NFD-agnostic, with ``ß`` -> ``ss`` and similar full case folds. Keys are never stored
in SQLite, so changing ``FOLD_VERSION`` only invalidates the in-memory snapshot.

Documented behaviour of ``str.casefold`` that users may notice:

* Turkish dotted capital ``İ`` (U+0130) folds to ``i`` + U+0307; the combining dot is
  stripped again after case folding, so ``İstanbul`` matches ``istanbul``.
* Turkish dotless ``ı`` (U+0131) stays ``ı``: ``ı`` does not match ``i`` (and vice versa).
* Only the Latin/Greek/Cyrillic combining diacritics U+0300-U+036F are stripped; Indic vowel
  signs, Hangul jamo and other marks are kept (NFC recomposes them).
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Sequence, Tuple

FOLD_VERSION = 1

_COMBINING = re.compile("[̀-ͯ]")
_SURROGATE = re.compile("[\ud800-\udfff]")
# Characters after which a new word starts (A.2); a separator itself never starts a word.
SEPARATORS = frozenset(" _-.()[]{}+,;@#~'")
# ASCII word boundaries: after a separator, lower->upper, letter->digit and digit->letter.
_ASCII_BOUNDARY = re.compile(
    r"(?<=[ _\-.()\[\]{}+,;@#~'])(?=[^ _\-.()\[\]{}+,;@#~'])"
    r"|(?<=[a-z])(?=[A-Z])"
    r"|(?<=[A-Za-z])(?=[0-9])"
    r"|(?<=[0-9])(?=[A-Za-z])",
    re.S,
)


def fold(s: str) -> str:
    """Search key: lone surrogates -> U+FFFD, NFD, strip U+0300-U+036F (Latin/Greek/Cyrillic
    combining diacritics only; Indic vowel signs etc. are kept), NFC, casefold."""
    if s.isascii():
        return s.lower()
    if _SURROGATE.search(s):
        s = _SURROGATE.sub("�", s)
    s = _COMBINING.sub("", unicodedata.normalize("NFD", s))
    s = unicodedata.normalize("NFC", s).casefold()
    if not s.isascii():
        # casefold can re-introduce a stripped mark (U+0130 -> "i" + U+0307).
        s2 = _COMBINING.sub("", unicodedata.normalize("NFD", s))
        if len(s2) != len(s) or s2 != s:
            s = unicodedata.normalize("NFC", s2)
    return s


def nfc(s: str) -> str:
    """NFC with lone surrogates replaced: the comparison form for ``case:`` matching."""
    if s.isascii():
        return s
    if _SURROGATE.search(s):
        s = _SURROGATE.sub("�", s)
    return unicodedata.normalize("NFC", s)


# ----------------------------------------------------------------------------- index maps

def _is_extender(c: str) -> bool:
    """True if ``c`` attaches to the previous character (it never starts a cluster)."""
    if c.isascii():
        return False
    if unicodedata.category(c) in ("Mn", "Mc", "Me"):
        return True
    return "ᅠ" <= c <= "ᇿ"  # Hangul medial/final jamo (macOS NFD names)


def _clusters(s: str) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    start = 0
    for i in range(1, len(s)):
        if not _is_extender(s[i]):
            out.append((start, i))
            start = i
    if s:
        out.append((start, len(s)))
    return out


def _mapped(s: str, func) -> Tuple[str, List[int], List[int]]:
    """``func(s)`` plus, for every output index, the start and end (exclusive) of the
    original character cluster it came from."""
    if s.isascii():
        out = func(s)
        if len(out) == len(s):
            idx = list(range(len(s)))
            return out, idx, [i + 1 for i in idx]
    full = func(s)
    parts: List[str] = []
    starts: List[int] = []
    ends: List[int] = []
    for a, b in _clusters(s):
        piece = func(s[a:b])
        parts.append(piece)
        starts.extend([a] * len(piece))
        ends.extend([b] * len(piece))
    if "".join(parts) != full:
        # Normalization crossed a cluster boundary (very rare): proportional mapping.
        n, m = len(s), max(1, len(full))
        starts = [min(n - 1, j * n // m) for j in range(len(full))]
        ends = [min(n, max(st + 1, (j + 1) * n // m)) for j, st in enumerate(starts)]
    return full, starts, ends


def fold_with_map(s: str) -> Tuple[str, List[int]]:
    """Folded text plus, for each folded index, the index of the original character."""
    folded, starts, _ = _mapped(s, fold)
    return folded, starts


def _merge(spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    spans.sort()
    out: List[Tuple[int, int]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def match_spans(name: str, terms: Sequence[str], case_sensitive: bool = False) -> List[Tuple[int, int]]:
    """Every occurrence of every term as ``(start, end)`` spans in the ORIGINAL ``name``.

    Terms must already be in the comparison form (``fold``ed, or ``nfc`` with
    ``case_sensitive``). Overlapping spans are merged; the result is sorted.
    """
    terms = [t for t in terms if t]
    if not terms or not name:
        return []
    text, starts, ends = _mapped(name, nfc if case_sensitive else fold)
    spans: List[Tuple[int, int]] = []
    for t in terms:
        p = text.find(t)
        while p >= 0:
            spans.append((starts[p], ends[p + len(t) - 1]))
            p = text.find(t, p + len(t))
    return _merge(spans)


def regex_spans(name: str, rx: "re.Pattern[str]", case_sensitive: bool = False) -> List[Tuple[int, int]]:
    """Spans of the non-empty matches of ``rx`` over the folded (or NFC) name."""
    text, starts, ends = _mapped(name, nfc if case_sensitive else fold)
    spans = [(starts[m.start()], ends[m.end() - 1]) for m in rx.finditer(text) if m.end() > m.start()]
    return _merge(spans)


# ----------------------------------------------------------------------------- boundaries

def _boundary_positions(name: str, folded: str) -> List[int]:
    """Folded positions that start a word (always includes 0 for a non-empty name)."""
    if not folded:
        return []
    if name.isascii():
        return [0] + [m.start() for m in _ASCII_BOUNDARY.finditer(name)]
    if len(folded) == len(name):
        src = name          # assume a 1:1 mapping: camelCase boundaries are usable
        camel = True
    else:
        src = folded        # lengths differ: separator and digit boundaries only
        camel = False
    pos = [0]
    prev = src[0]
    for i in range(1, len(src)):
        c = src[i]
        if prev in SEPARATORS:
            if c not in SEPARATORS:
                pos.append(i)
        elif camel and prev.islower() and c.isupper():
            pos.append(i)
        elif (prev.isalpha() and c.isdigit()) or (prev.isdigit() and c.isalpha()):
            pos.append(i)
        prev = c
    return pos


def boundaries(name: str, folded: str | None = None) -> Tuple[int, str]:
    """``(boundary_mask(name), initials(name))`` in one pass (used by the index build)."""
    if folded is None:
        folded = fold(name)
    pos = _boundary_positions(name, folded)
    if not pos:
        return 0, ""
    mask = 0
    for p in pos:
        if p < 64:
            mask |= 1 << p
    n = len(folded)
    return mask, "".join([folded[p] for p in pos if p < n])


def boundary_mask(name: str) -> int:
    """Bit p set if folded position p (< 64) starts a word."""
    return boundaries(name)[0]


def initials(name: str) -> str:
    """Folded characters at word starts: ``"GiftTopIcon123.png"`` -> ``"gti1p"``."""
    return boundaries(name)[1]


def initials_spans(name: str, term: str) -> List[Tuple[int, int]]:
    """Spans (one character each) of the word starts that spell ``term`` (abbreviation tier)."""
    folded, starts, ends = _mapped(name, fold)
    pos = [p for p in _boundary_positions(name, folded) if p < len(folded)]
    ini = "".join(folded[p] for p in pos)
    k = ini.find(term)
    if k < 0:
        return []
    return _merge([(starts[pos[j]], ends[pos[j]]) for j in range(k, k + len(term))])

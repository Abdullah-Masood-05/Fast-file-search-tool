"""Search query language (design A.5).

``parse(text)`` never raises: a bad regex or an unknown unit is reported in
``ParsedQuery.error``. Semantics that changed from v1:

* OR binds tighter than AND: ``a OR b c`` is ``(a or b) and c``.
* ``*`` / ``?`` globs are anchored to the whole name: ``report*`` no longer matches
  ``my_report_x`` (``*report*`` does).

Term texts are stored in comparison form: ``fold()``ed, or ``nfc()``ed under ``case:``.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import FrozenSet, List, Optional, Tuple

from .protocol import FilterState
from .textfold import fold, nfc

GLOB_CHARS = frozenset("*?")
_FIELD_RE = re.compile(r"([A-Za-z]+):(.*)\Z", re.S)
_CASE_PREFIX = re.compile(r"\s*(?:case|cs):", re.I)
_REGEX_FIELDS = frozenset({"re", "regex"})
_FIELDS = frozenset({"name", "path", "ext", "extension", "type", "kind", "size", "date", "modified", "dm",
                     "created", "dc", "re", "regex"})

_KIND_WORDS = {
    "folder": "folder", "folders": "folder", "dir": "folder", "dirs": "folder", "directory": "folder",
    "directories": "folder",
    "file": "file", "files": "file",
    "image": "image", "images": "image", "img": "image", "picture": "image", "pictures": "image",
    "photo": "image", "photos": "image", "pic": "image", "pics": "image",
    "doc": "document", "docs": "document", "document": "document", "documents": "document",
    "code": "code", "source": "code", "src": "code",
    "audio": "audio", "music": "audio", "sound": "audio", "sounds": "audio",
    "video": "video", "videos": "video", "movie": "video", "movies": "video",
    "archive": "archive", "archives": "archive", "compressed": "archive",
}

_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "kib": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
               "mib": 1024 ** 2, "g": 1024 ** 3, "gb": 1024 ** 3, "gib": 1024 ** 3, "t": 1024 ** 4,
               "tb": 1024 ** 4, "tib": 1024 ** 4}
_SIZE_RE = re.compile(r"(\d+(?:\.\d*)?|\.\d+)\s*([a-z]*)\Z")
# Everything-style size words: [lo, hi) in bytes.
_SIZE_WORDS = {
    "empty": (0, 1), "tiny": (0, 10 * 1024), "small": (10 * 1024, 100 * 1024),
    "medium": (100 * 1024, 1024 ** 2), "large": (1024 ** 2, 16 * 1024 ** 2),
    "huge": (16 * 1024 ** 2, 128 * 1024 ** 2), "gigantic": (128 * 1024 ** 2, None),
}
_REL_RE = re.compile(r"(\d+)\s*(h|d|w|m|y)\Z")
_REL_UNIT = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
_DATE_RE = re.compile(r"(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?\Z")
_OPS = (">=", "<=", "=>", "=<", "==", ">", "<", "=")


class QueryError(ValueError):
    """A precise, user-facing parse error."""


@dataclass(frozen=True)
class Atom:
    """One matchable unit. ``kind`` is 'sub' (substring of the name), 'glob' (fnmatch over
    the whole name) or 'path' (``dir_part`` must end the folder path, ``name_part`` must
    start the name, or match it as a glob when it contains wildcards)."""
    kind: str
    text: str
    dir_part: str = ""
    name_part: str = ""


@dataclass
class ParsedQuery:
    positive_terms: List[str] = field(default_factory=list)
    any_groups: List[Tuple[Atom, ...]] = field(default_factory=list)
    negative_terms: List[Atom] = field(default_factory=list)
    globs: List[str] = field(default_factory=list)
    path_terms: List[Atom] = field(default_factory=list)
    regex: Optional[str] = None
    case_sensitive: bool = False
    token_filters: FilterState = field(default_factory=FilterState)
    ctime_min: Optional[float] = None
    ctime_max: Optional[float] = None
    exclude_exts: FrozenSet[str] = frozenset()
    path_needles: Tuple[str, ...] = ()     # every path: value (token_filters.path_contains is the first)
    error: Optional[str] = None

    def has_text(self) -> bool:
        """True if any name/path text constraint is present (not just filters)."""
        return bool(self.positive_terms or self.any_groups or self.negative_terms or self.globs
                    or self.path_terms or self.regex)

    def has_token_filters(self) -> bool:
        return (not self.token_filters.is_empty() or self.ctime_min is not None or self.ctime_max is not None
                or bool(self.exclude_exts) or len(self.path_needles) > 1)

    def is_empty(self) -> bool:
        return not self.has_text() and not self.has_token_filters()

    def first_term(self) -> Optional[str]:
        """The ranking term: the first positive plain term in query order."""
        return self.positive_terms[0] if self.positive_terms else None

    def highlight_terms(self) -> List[str]:
        out = list(self.positive_terms)
        for a in self.path_terms:
            if a.name_part and not GLOB_CHARS & set(a.name_part):
                out.append(a.name_part)
        for group in self.any_groups:
            out.extend(a.text for a in group if a.kind == "sub")
            out.extend(a.name_part for a in group if a.kind == "path" and a.name_part
                       and not GLOB_CHARS & set(a.name_part))
        for g in self.globs:
            out.extend(c for c in re.split(r"[*?]", g) if c)
        seen = set()
        return [t for t in out if not (t in seen or seen.add(t))]


# ----------------------------------------------------------------------------- scanning

def _scan(text: str) -> List[Tuple[str, int]]:
    """Split on whitespace outside double quotes; returns ``(raw token, start offset)``.
    An unbalanced quote runs to the end of the text."""
    out: List[Tuple[str, int]] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        in_q = False
        while i < n and (in_q or not text[i].isspace()):
            if text[i] == '"':
                in_q = not in_q
            i += 1
        out.append((text[start:i], start))
    return out


def _unquote(raw: str) -> Tuple[str, bool]:
    """``(value, literal)``: quotes removed; ``literal`` when the token contained quotes."""
    if '"' not in raw:
        return raw, False
    return raw.replace('"', ""), True


def glob_regex(pattern: str) -> str:
    """Anchored regex source for a ``*``/``?`` glob (``[`` is literal, unlike fnmatch)."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "(?s:" + "".join(out) + r")\Z"


def literal_chunks(pattern: str) -> List[str]:
    return [c for c in re.split(r"[*?]", pattern) if c]


def make_atom(value: str, literal: bool, case_sensitive: bool) -> Optional[Atom]:
    """Classify and normalize one term (None if it folds to nothing)."""
    text = nfc(value) if case_sensitive else fold(value)
    if not text:
        return None
    if literal:
        return Atom("sub", text)
    if "/" in text or "\\" in text:
        t = text.replace("\\", "/")
        dir_part, _, name_part = t.rpartition("/")
        return Atom("path", t, dir_part, name_part)
    if GLOB_CHARS & set(text):
        if not text.strip("*"):
            return Atom("glob", "*")
        return Atom("glob", text)
    return Atom("sub", text)


def fold_regex(pattern: str) -> str:
    """Fold the non-ASCII literals of a regex so it can run over folded keys (regex syntax
    is ASCII, so this never changes its meaning). ``re.I`` handles ASCII case."""
    if pattern.isascii():
        return pattern
    return "".join(fold(c) if ord(c) > 127 else c for c in nfc(pattern))


# ----------------------------------------------------------------------------- values

def parse_size(text: str) -> int:
    """``"10mb"``, ``"2.5 MB"``, ``"512"``, ``"1k"`` -> bytes (1024-based)."""
    s = text.strip().lower().replace(",", "")
    m = _SIZE_RE.match(s)
    if not m or m.group(2) not in _SIZE_UNITS:
        raise QueryError(f"Unknown size: {text!r} (use e.g. 500kb, 10mb, 2gb)")
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2)])


def _split_op(v: str) -> Tuple[str, str]:
    for op in _OPS:
        if v.startswith(op):
            return op.replace("=>", ">=").replace("=<", "<=").replace("==", "="), v[len(op):].strip()
    return "", v


def parse_size_range(value: str) -> Tuple[Optional[int], Optional[int]]:
    """Size expression -> ``(min inclusive, max exclusive)``."""
    v = value.strip().lower()
    if v in _SIZE_WORDS:
        return _SIZE_WORDS[v]
    if ".." in v:
        a, _, b = v.partition("..")
        lo = parse_size(a) if a.strip() else None
        hi = parse_size(b) + 1 if b.strip() else None
        return lo, hi
    op, rest = _split_op(v)
    x = parse_size(rest)
    if op == ">":
        return x + 1, None
    if op == ">=":
        return x, None
    if op == "<":
        return None, x
    if op == "<=":
        return None, x + 1
    return x, x + 1


def _midnight(now: float) -> datetime:
    d = datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _period(v: str, now: float) -> Tuple[Optional[float], Optional[float], bool]:
    """``(start, end, relative)`` of a date word / absolute date. Relative ages (7d) return
    ``(now - age, None, True)``."""
    if v == "today":
        m = _midnight(now)
        return m.timestamp(), (m + timedelta(days=1)).timestamp(), False
    if v == "yesterday":
        m = _midnight(now)
        return (m - timedelta(days=1)).timestamp(), m.timestamp(), False
    rel = _REL_RE.match(v)
    if rel:
        return now - int(rel.group(1)) * _REL_UNIT[rel.group(2)], None, True
    m = _DATE_RE.match(v)
    if not m:
        raise QueryError(f"Unknown date: {v!r} (use e.g. 2024, 2024-03, 2024-03-15, today, 7d)")
    year, month, day = int(m.group(1)), m.group(2), m.group(3)
    try:
        if month is None:
            start, stop = datetime(year, 1, 1), datetime(year + 1, 1, 1)
        elif day is None:
            mo = int(month)
            start = datetime(year, mo, 1)
            stop = datetime(year + (mo == 12), mo % 12 + 1, 1)
        else:
            start = datetime(year, int(month), int(day))
            stop = start + timedelta(days=1)
        return start.timestamp(), stop.timestamp(), False
    except (ValueError, OverflowError, OSError):
        raise QueryError(f"Invalid date: {v!r}") from None


def parse_time_range(value: str, now: Optional[float] = None) -> Tuple[Optional[float], Optional[float]]:
    """Date expression -> ``(min inclusive, max exclusive)`` epoch seconds (local time).

    ``2024`` / ``2024-03`` / ``2024-03-15`` / ``today`` / ``yesterday`` mean "within";
    ``>2024`` after it, ``>=2024`` from its start, ``<2024`` before it, ``<=2024`` up to its
    end; ``a..b`` from the start of a to the end of b; ``7d`` / ``4w`` / ``6m`` / ``1y`` (and
    ``<7d``) mean "in the last ...", ``>7d`` "more than ... ago"."""
    now = time.time() if now is None else now
    v = value.strip().lower()
    if ".." in v:
        a, _, b = v.partition("..")
        pts: List[float] = []
        lo = hi = None
        if a.strip():
            s, e, rel = _period(a.strip(), now)
            lo = s
            if rel:
                pts.append(s)  # type: ignore[arg-type]
        if b.strip():
            s, e, rel = _period(b.strip(), now)
            hi = s if rel else e
            if rel:
                pts.append(s)  # type: ignore[arg-type]
        if len(pts) == 2:
            lo, hi = min(pts), max(pts)
        return lo, hi
    op, rest = _split_op(v)
    s, e, rel = _period(rest, now)
    if rel:
        return (None, s) if op in (">", ">=") else (s, None)
    if op == ">":
        return e, None
    if op == ">=":
        return s, None
    if op == "<":
        return None, s
    if op == "<=":
        return None, e
    return s, e


def _intersect(a: Tuple[Optional[float], Optional[float]], b: Tuple[Optional[float], Optional[float]]):
    lo = b[0] if a[0] is None else a[0] if b[0] is None else max(a[0], b[0])
    hi = b[1] if a[1] is None else a[1] if b[1] is None else min(a[1], b[1])
    return lo, hi


def _is_absolute(v: str) -> bool:
    return v.startswith(("/", "\\", "~")) or bool(re.match(r"[A-Za-z]:[\\/]", v))


# ----------------------------------------------------------------------------- parser

@dataclass
class _Item:
    atoms: List[Atom]
    negated: bool


def parse(text: str, now: Optional[float] = None) -> ParsedQuery:
    """Parse a query; never raises (errors go to ``ParsedQuery.error``)."""
    q = ParsedQuery()
    try:
        _parse_into(q, text or "", time.time() if now is None else now)
    except QueryError as exc:
        q.error = str(exc)
    return q


def _parse_into(q: ParsedQuery, text: str, now: float) -> None:
    m = _CASE_PREFIX.match(text)
    if m:
        q.case_sensitive = True
        text = text[m.end():]
    cs = q.case_sensitive
    items: List[_Item] = []
    pending_or = pending_not = False
    kinds: set = set()
    exts: set = set()
    excl_exts: set = set()
    size = (None, None)
    mtime: Tuple[Optional[float], Optional[float]] = (None, None)
    ctime: Tuple[Optional[float], Optional[float]] = (None, None)
    folder: Optional[str] = None
    needles: List[str] = []

    for raw, start in _scan(text):
        if raw in ("OR", "|"):
            pending_or = bool(items)
            continue
        if raw in ("AND", "&", "&&"):
            continue
        if raw in ("NOT", "!"):
            pending_not = True
            continue
        neg = pending_not
        body = raw
        if len(body) > 1 and body[0] in "-!":
            neg = True
            body = body[1:]
        fm = _FIELD_RE.match(body) if not body.startswith('"') else None
        key = fm.group(1).lower() if fm else ""
        if fm and key in _FIELDS:
            if key in _REGEX_FIELDS:
                # The regex is the raw rest of the query (it may contain spaces and quotes).
                offset = start + (len(raw) - len(body)) + len(fm.group(1)) + 1
                pattern = text[offset:].strip()
                if pattern:
                    _set_regex(q, pattern)
                break
            value, literal = _unquote(fm.group(2))
            value = value.strip()
            pending_or = pending_not = False
            if not value or value in _OPS:
                continue  # still being typed
            if key == "name":
                atom = make_atom(value, literal, cs)
                if atom:
                    items.append(_Item([atom], neg))
            elif key in ("ext", "extension"):
                if value.startswith("!"):
                    neg, value = True, value[1:]
                vals = {fold(x.strip().lstrip(".")) for x in value.split(",") if x.strip().lstrip(".")}
                if any(GLOB_CHARS & set(x) for x in vals):
                    raise QueryError("Wildcards are not supported in ext: (list extensions: ext:jpg,png)")
                (excl_exts if neg else exts).update(vals)
            elif neg:
                raise QueryError(f"{key}: cannot be negated")
            elif key in ("type", "kind"):
                for x in value.split(","):
                    x = x.strip().lower()
                    if not x:
                        continue
                    if x not in _KIND_WORDS:
                        raise QueryError(f"Unknown type: {x!r} (use folder, file, image, document, code, "
                                         "audio, video or archive)")
                    kinds.add(_KIND_WORDS[x])
            elif key == "size":
                size = _intersect(size, parse_size_range(value))
            elif key in ("date", "modified", "dm"):
                mtime = _intersect(mtime, parse_time_range(value, now))
            elif key in ("created", "dc"):
                ctime = _intersect(ctime, parse_time_range(value, now))
            elif key == "path":
                if _is_absolute(value) and not literal or (literal and _is_absolute(value)):
                    folder = value
                else:
                    needle = (nfc(value) if cs else fold(value)).replace("\\", "/")
                    if needle:
                        needles.append(needle)
            continue

        value, literal = _unquote(body)
        if not literal and "|" in value and len(value) > 1:
            parts = [p for p in value.split("|") if p]
            atoms = [a for a in (make_atom(p, False, cs) for p in parts) if a]
        else:
            a = make_atom(value, literal, cs)
            atoms = [a] if a else []
        if not atoms:
            pending_or = pending_not = False
            continue
        if pending_or and items and not neg and not items[-1].negated:
            items[-1].atoms.extend(atoms)
        else:
            items.append(_Item(atoms, neg))
        pending_or = pending_not = False

    seen_pos: set = set()
    for it in items:
        if it.negated:
            q.negative_terms.extend(it.atoms)
        elif len(it.atoms) == 1:
            a = it.atoms[0]
            if a.kind == "sub":
                if a.text not in seen_pos:
                    seen_pos.add(a.text)
                    q.positive_terms.append(a.text)
            elif a.kind == "glob":
                if a.text not in q.globs:
                    q.globs.append(a.text)
            else:
                q.path_terms.append(a)
        else:
            uniq: List[Atom] = []
            for a in it.atoms:
                if a not in uniq:
                    uniq.append(a)
            if len(uniq) == 1:
                items.append(_Item(uniq, False))  # degenerate "a OR a"
                continue
            q.any_groups.append(tuple(uniq))

    if size[0] is not None and size[1] is not None and size[0] >= size[1]:
        size = (size[0], size[0])  # empty range: matches nothing, stays explicit
    q.token_filters = FilterState(
        kinds=frozenset(kinds), exts=frozenset(exts), size_min=size[0], size_max=size[1],
        mtime_min=mtime[0], mtime_max=mtime[1], folder=folder,
        path_contains=needles[0] if needles else None)
    q.ctime_min, q.ctime_max = ctime
    q.exclude_exts = frozenset(excl_exts)
    q.path_needles = tuple(needles)


def _set_regex(q: ParsedQuery, pattern: str) -> None:
    src = pattern if q.case_sensitive else fold_regex(pattern)
    try:
        re.compile(src, 0 if q.case_sensitive else re.I)
    except re.error as exc:
        raise QueryError(f"Invalid regular expression: {exc}") from None
    q.regex = src


def ceil_time(t: Optional[float]) -> Optional[int]:
    """Integer bound equivalent to a float bound for integer mtimes."""
    return None if t is None else int(math.ceil(t))

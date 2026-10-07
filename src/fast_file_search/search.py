from __future__ import annotations

import heapq
import math
import re
import threading
import time

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Pattern, Set, Tuple

from .persistent_index import PersistentIndex

# Upper bound on candidate rows pulled from the trigram index per search.
_CANDIDATE_LIMIT = 20000

_RANGE_FIELDS = ("size", "date", "modified", "created")


@dataclass
class SearchQuery:
    raw: str
    terms: List[str] = field(default_factory=list)
    exact_phrase: Optional[str] = None
    field_filters: Dict[str, str] = field(default_factory=dict)
    range_filters: Dict[str, Tuple[Optional[str], Optional[str]]] = field(default_factory=dict)
    boolean_groups: List[Tuple[str, str]] = field(default_factory=list)
    is_regex: bool = False
    case_sensitive: bool = False


@dataclass
class SearchResult:
    path: str
    name: str
    extension: str
    size: int
    modified: int
    is_folder: bool
    score: float = 0.0
    matches: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "extension": self.extension,
            "size": self.size,
            "modified": self.modified,
            "is_folder": self.is_folder,
            "score": self.score,
            "matches": self.matches,
        }


@dataclass
class SearchResponse:
    results: List[SearchResult] = field(default_factory=list)
    total_count: int = 0
    page: int = 1
    page_size: int = 50
    total_pages: int = 0
    query_time_ms: float = 0.0
    did_you_mean: Optional[str] = None
    error: Optional[str] = None


def _glob_to_regex(pattern: str) -> str:
    """Translate a ``*`` / ``?`` wildcard pattern into a regex source string."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return "".join(out)


@lru_cache(maxsize=512)
def _compile_glob(pattern: str, case_sensitive: bool = False) -> Pattern[str]:
    return re.compile(_glob_to_regex(pattern), 0 if case_sensitive else re.IGNORECASE)


def _is_glob(term: str) -> bool:
    return "*" in term or "?" in term


class QueryParser:
    _FIELD_PATTERN = re.compile(
        r'\b(name|path|ext|extension|size|date|modified|created):\s*(\S+)'
    )
    _FIELD_QUOTED = re.compile(r'\b(name|path|ext|extension):\s*"([^"]+)"')
    _RANGE_PATTERN = re.compile(r'\b(size|date|modified|created):\s*([<>=!]+)\s*(\S+)')
    _RANGE_DOTDOT = re.compile(r'\b(size|date|modified|created):\s*(\S*?)\.\.(\S*)')
    _QUOTED = re.compile(r'"([^"]+)"')
    _BOOL_WORDS = {"AND", "OR", "NOT"}
    _BOOL_SYMBOLS = {"&": "AND", "|": "OR", "!": "NOT"}
    _PREFIX_REGEX = re.compile(r'^(re|regex):', re.IGNORECASE)
    _PREFIX_CASE = re.compile(r'^(cs|case):', re.IGNORECASE)

    @classmethod
    def parse(cls, raw: str) -> SearchQuery:
        q = SearchQuery(raw=(raw or "").strip())
        if not q.raw:
            return q

        if cls._PREFIX_REGEX.match(q.raw):
            q.is_regex = True
            q.raw = cls._PREFIX_REGEX.sub("", q.raw, count=1).strip()
            q.terms = [q.raw] if q.raw else []
            return q

        if cls._PREFIX_CASE.match(q.raw):
            q.case_sensitive = True
            q.raw = cls._PREFIX_CASE.sub("", q.raw, count=1).strip()

        # field:"value with spaces"
        for m in cls._FIELD_QUOTED.finditer(q.raw):
            fld, val = m.groups()
            q.field_filters["extension" if fld == "ext" else fld] = val
        q.raw = cls._FIELD_QUOTED.sub(" ", q.raw)

        phrases = cls._QUOTED.findall(q.raw)
        if phrases:
            q.exact_phrase = phrases[0]
            q.raw = cls._QUOTED.sub(" ", q.raw)
        # Drop stray (unbalanced) quote characters so they do not poison terms.
        q.raw = q.raw.replace('"', " ").strip()

        for m in cls._RANGE_DOTDOT.finditer(q.raw):
            fld, lo, hi = m.groups()
            q.range_filters[fld] = (lo or None, hi or None)
        q.raw = cls._RANGE_DOTDOT.sub(" ", q.raw)

        for m in cls._RANGE_PATTERN.finditer(q.raw):
            fld, op, val = m.groups()
            if op in (">", ">=", "=>"):
                q.range_filters[fld] = (val, None)
            elif op in ("<", "<=", "=<"):
                q.range_filters[fld] = (None, val)
            elif op in ("=", "=="):
                q.range_filters[fld] = (val, val)
            elif op in ("!", "!="):
                q.field_filters[fld] = f"!{val}"
        q.raw = cls._RANGE_PATTERN.sub(" ", q.raw)

        for m in cls._FIELD_PATTERN.finditer(q.raw):
            fld, val = m.groups()
            if fld == "ext":
                fld = "extension"
            if fld in _RANGE_FIELDS and not val.startswith("!"):
                # "size:100" / "date:2024-01-05" mean equality.
                q.range_filters[fld] = (val, val)
            else:
                q.field_filters[fld] = val
        q.raw = cls._FIELD_PATTERN.sub(" ", q.raw)

        tokens = q.raw.split()
        i = 0
        while i < len(tokens):
            t = tokens[i]
            op = t if t in cls._BOOL_WORDS else cls._BOOL_SYMBOLS.get(t)
            if op:
                if i + 1 < len(tokens):
                    q.boolean_groups.append((op, tokens[i + 1]))
                    i += 2
                    continue
            else:
                q.terms.append(t)
            i += 1

        # Additional quoted phrases are required substrings too.
        for extra in phrases[1:]:
            q.terms.append(extra)

        return q


class Levenshtein:
    @staticmethod
    def distance(s1: str, s2: str) -> int:
        if abs(len(s1) - len(s2)) > 10:
            return 10
        m, n = len(s1), len(s2)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev = dp[0]
            dp[0] = i
            for j in range(1, n + 1):
                tmp = dp[j]
                cost = 0 if s1[i - 1] == s2[j - 1] else 1
                dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + cost)
                prev = tmp
        return dp[n]

    @staticmethod
    def fuzzy_match(term: str, target: str, max_dist: int = 2) -> bool:
        if abs(len(term) - len(target)) > max_dist:
            return False
        return Levenshtein.distance(term.lower(), target.lower()) <= max_dist


class LRUCache:
    def __init__(self, maxsize: int = 128):
        self._maxsize = maxsize
        self._cache: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[object]:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def remove(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)


def _copy_response(resp: SearchResponse) -> SearchResponse:
    """Shallow copy so callers can post-filter a response without corrupting the cache."""
    return replace(resp, results=list(resp.results))


class SearchEngine:
    def __init__(
        self,
        persistent_index: Optional[PersistentIndex] = None,
        cache_size: int = 128,
        typo_tolerance: int = 2,
    ):
        self.index = persistent_index or PersistentIndex()
        self.cache = LRUCache(maxsize=cache_size)
        self.typo_tolerance = typo_tolerance
        self._on_search_callbacks: List[Callable[[str, int], None]] = []

    def on_search(self, cb: Callable[[str, int], None]) -> None:
        self._on_search_callbacks.append(cb)

    def invalidate_cache(self) -> None:
        self.cache.invalidate()

    def compute_tfidf(self, term: str, file_name: str, total_files: int, term_freq: int) -> float:
        if not term or not file_name:
            return 0.0
        tf = file_name.lower().count(term.lower()) / max(1, len(file_name))
        idf = math.log((1 + total_files) / max(1, 1 + term_freq))
        return tf * idf

    def rank_result(
        self,
        file: dict,
        query: SearchQuery,
        total_files: int,
        term_freq: int,
        now: float,
    ) -> float:
        name = file.get("name", "") or ""
        modified = file.get("modified", 0)
        path = file.get("path", "") or ""
        name_lower = name.lower()
        score = 0.0

        if query.exact_phrase:
            if query.exact_phrase.lower() in name_lower:
                score += 10.0
            if query.exact_phrase.lower() == name_lower:
                score += 20.0

        for term in query.terms:
            if _is_glob(term):
                matches = bool(_compile_glob(term, query.case_sensitive).search(name))
            elif query.case_sensitive:
                matches = term in name
            else:
                matches = term.lower() in name_lower
            if matches and not _is_glob(term):
                score += self.compute_tfidf(term, name, total_files, term_freq) * 2.0
            elif matches:
                score += 1.0

            if not _is_glob(term) and Levenshtein.fuzzy_match(term, name, self.typo_tolerance):
                score += 0.5

        path_depth = path.count("/") + path.count("\\")
        score += max(0, 5 - path_depth) * 0.3

        # Files with a modification time in the future must not crash log1p.
        age_hours = max(0.0, (now - modified) / 3600) if modified else 1e9
        recent_bonus = max(0.0, 1.0 - math.log1p(age_hours) / 100.0)
        score += recent_bonus * 2.0

        if query.terms:
            plain = [t for t in query.terms if not _is_glob(t)]
            exact_matches = sum(1 for t in plain if t.lower() == name_lower)
            score += exact_matches * 15.0
            partial_matches = sum(1 for t in plain if len(t) > 2 and t.lower() in name_lower)
            score += partial_matches * 3.0

        return float(score)

    def _matches_field_filter(self, file: dict, field: str, value: str) -> bool:
        negate = value.startswith("!")
        val = (value[1:] if negate else value).strip().lower()
        if not val:
            return True

        if field == "extension":
            file_val = str(file.get("extension") or "").lower().lstrip(".")
            alternatives = [v.strip().lstrip(".") for v in val.split(",") if v.strip()]
            match = False
            for alt in alternatives:
                if _is_glob(alt):
                    if _compile_glob(alt).fullmatch(file_val):
                        match = True
                        break
                elif alt == file_val:
                    match = True
                    break
            return not match if negate else match

        if field in ("name", "path"):
            file_val = str(file.get(field) or "").lower()
            if field == "path":
                # Accept either separator on every platform.
                file_val = file_val.replace("\\", "/")
                val = val.replace("\\", "/")
            if _is_glob(val):
                match = bool(_compile_glob(val).search(file_val))
            else:
                match = val in file_val
            return not match if negate else match

        return True

    @staticmethod
    def _parse_date(s: str, end: bool = False) -> Optional[float]:
        """Parse YYYY, YYYY-MM or YYYY-MM-DD (also with '/' or '.') to a local timestamp."""
        s = s.strip().replace("/", "-").replace(".", "-")
        m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$", s)
        if not m:
            return None
        year, month, day = int(m.group(1)), m.group(2), m.group(3)
        try:
            if month is None:
                start = datetime(year, 1, 1)
                stop = datetime(year + 1, 1, 1)
            elif day is None:
                start = datetime(year, int(month), 1)
                stop = datetime(year + (int(month) == 12), int(month) % 12 + 1, 1)
            else:
                start = datetime(year, int(month), int(day))
                stop = start + timedelta(days=1)
            return (stop - timedelta(microseconds=1)).timestamp() if end else start.timestamp()
        except (ValueError, OverflowError, OSError):
            return None

    def _matches_range(self, file: dict, field: str, lo: Optional[str], hi: Optional[str]) -> bool:
        key = "modified" if field == "date" else field
        val = file.get(key)
        if val is None or not isinstance(val, (int, float)):
            return False
        lo_val: Optional[float]
        hi_val: Optional[float]
        if key == "size":
            lo_val = self._parse_size(lo) if lo is not None else None
            hi_val = self._parse_size(hi) if hi is not None else None
        else:
            lo_val = self._parse_date(lo) if lo is not None else None
            hi_val = self._parse_date(hi, end=True) if hi is not None else None
        if lo_val is not None and val < lo_val:
            return False
        if hi_val is not None and val > hi_val:
            return False
        return True

    def _parse_size(self, s: str) -> Optional[int]:
        s = s.strip().lower()
        multipliers = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
        m = re.match(r"^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)?$", s)
        if m:
            num = float(m.group(1))
            unit = m.group(2) or "b"
            return int(num * multipliers.get(unit, 1))
        try:
            return int(s)
        except ValueError:
            return None

    @staticmethod
    def _text_match(term: str, name: str, case_sensitive: bool) -> bool:
        if _is_glob(term):
            return bool(_compile_glob(term, case_sensitive).search(name))
        if case_sensitive:
            return term in name
        return term.lower() in name.lower()

    def _matches_text(self, name: str, query: SearchQuery) -> bool:
        """Evaluate terms / phrase / AND / OR / NOT groups against a file name."""
        cs = query.case_sensitive
        required = list(query.terms)
        if query.exact_phrase:
            required.append(query.exact_phrase)
        or_terms: List[str] = []
        for op, term in query.boolean_groups:
            if op == "NOT":
                if self._text_match(term, name, cs):
                    return False
            elif op == "AND":
                required.append(term)
            elif op == "OR":
                or_terms.append(term)

        required_ok = all(self._text_match(t, name, cs) for t in required)
        if not or_terms:
            return required_ok
        or_ok = any(self._text_match(t, name, cs) for t in or_terms)
        return (required_ok and bool(required)) or or_ok

    def _apply_boolean(self, file: dict, query: SearchQuery) -> bool:
        return self._matches_text(file.get("name", "") or "", query)

    def _index_generation(self) -> Tuple[int, int]:
        """Cheap fingerprint of the index so cached responses never outlive index changes."""
        try:
            conn = self.index._get_conn()
            row = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM files").fetchone()
            if row:
                return int(row[0]), int(row[1])
        except Exception:
            pass
        return (0, 0)

    def search_persistent(
        self,
        query_str: str,
        page: int = 1,
        page_size: int = 50,
        use_cache: bool = True,
    ) -> SearchResponse:
        start = time.time()
        try:
            page = max(1, int(page))
            page_size = max(1, int(page_size))
        except (TypeError, ValueError):
            page, page_size = 1, 50
        resp = SearchResponse(page=page, page_size=page_size)

        query = QueryParser.parse(query_str)
        if (
            not query.raw
            and not query.exact_phrase
            and not query.field_filters
            and not query.range_filters
        ):
            resp.query_time_ms = (time.time() - start) * 1000
            return resp

        generation = self._index_generation()
        total_files = generation[0]
        cache_key = f"{query_str}\x00p{page}\x00ps{page_size}\x00g{generation}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if isinstance(cached, SearchResponse):
                resp = _copy_response(cached)
                resp.query_time_ms = (time.time() - start) * 1000
                return resp

        regex: Optional[Pattern[str]] = None
        if query.is_regex:
            if not query.terms:
                resp.query_time_ms = (time.time() - start) * 1000
                return resp
            try:
                regex = re.compile(query.terms[0], re.IGNORECASE)
            except re.error as exc:
                resp.error = f"Invalid regular expression: {exc}"
                resp.query_time_ms = (time.time() - start) * 1000
                return resp

        candidates = self._collect_candidates(query)
        now = time.time()

        term_freq_map: Dict[str, int] = defaultdict(int)
        if isinstance(candidates, list) and query.terms:
            lowered = [t.lower() for t in query.terms if not _is_glob(t)]
            for f in candidates:
                name_lower = (f.get("name", "") or "").lower()
                for t in lowered:
                    if t in name_lower:
                        term_freq_map[t] += 1
        term_freq = max(term_freq_map.values(), default=0)

        count = 0
        seen: Set[str] = set()

        def matching() -> Iterator[SearchResult]:
            nonlocal count
            for f in candidates:
                p = f.get("path", "")
                if p in seen:
                    continue
                seen.add(p)
                name = f.get("name", "") or ""

                if regex is not None:
                    if not regex.search(name):
                        continue
                elif not self._matches_text(name, query):
                    continue

                if any(
                    not self._matches_field_filter(f, fld, val)
                    for fld, val in query.field_filters.items()
                ):
                    continue
                if any(
                    not self._matches_range(f, fld, lo, hi)
                    for fld, (lo, hi) in query.range_filters.items()
                ):
                    continue

                score = self.rank_result(f, query, total_files, term_freq, now)
                name_lower = name.lower()
                count += 1
                yield SearchResult(
                    path=p,
                    name=name,
                    extension=f.get("extension", ""),
                    size=f.get("size", 0),
                    modified=f.get("modified", 0),
                    is_folder=bool(f.get("is_folder", False)),
                    score=score,
                    matches=[t for t in query.terms if not _is_glob(t) and t.lower() in name_lower],
                )

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        # Only the requested window is kept in memory, however many files match.
        top = heapq.nsmallest(end_idx, matching(), key=lambda r: (-r.score, r.name.lower()))
        resp.results = top[start_idx:end_idx]
        resp.total_count = count
        resp.total_pages = max(1, math.ceil(count / page_size))

        resp.query_time_ms = (time.time() - start) * 1000

        if count == 0 and query.terms and not query.is_regex:
            resp.did_you_mean = self._suggest_correction(query)

        if use_cache:
            self.cache.put(cache_key, _copy_response(resp))

        for cb in list(self._on_search_callbacks):
            try:
                cb(query_str, resp.total_count)
            except Exception:
                continue

        return resp

    @staticmethod
    def _literal_chunk(term: str) -> str:
        """Longest wildcard-free fragment of a term (what the trigram index can look up)."""
        chunks = [c.strip() for c in re.split(r"[*?]", term)]
        return max(chunks, key=len) if chunks else ""

    def _collect_candidates(self, query: SearchQuery) -> Iterable[dict]:
        if query.is_regex:
            return self.index.iter_all_files()

        required = list(query.terms)
        if query.exact_phrase:
            required.append(query.exact_phrase)
        or_terms: List[str] = []
        for op, term in query.boolean_groups:
            if op == "AND":
                required.append(term)
            elif op == "OR":
                or_terms.append(term)

        lookups: List[str] = []
        scan_all = False
        # A file must satisfy every required term, so the most selective one is enough
        # to produce the candidate set; _matches_text() verifies the rest.
        if required:
            best = max((self._literal_chunk(t) for t in required), key=len)
            if best:
                lookups.append(best)
            elif not or_terms:
                scan_all = True
        for t in or_terms:
            chunk = self._literal_chunk(t)
            if chunk:
                lookups.append(chunk)
            else:
                scan_all = True
        if not required and not or_terms:
            scan_all = True
        if required and or_terms and not lookups:
            scan_all = True

        if scan_all or not lookups:
            return self.index.iter_all_files()

        candidates_map: Dict[str, dict] = {}
        for t in lookups:
            for item in self.index.search_trigram(t, limit=_CANDIDATE_LIMIT):
                candidates_map[item["path"]] = item
        return list(candidates_map.values())

    def _suggest_correction(self, query: SearchQuery) -> Optional[str]:
        terms = [t for t in query.terms if not _is_glob(t)]
        if not terms:
            return None
        all_names: List[str] = []
        for f in self.index.iter_all_files():
            all_names.append(f.get("name", ""))
            if len(all_names) >= 5000:
                break

        best: Optional[Tuple[str, int]] = None
        for term in terms:
            for name in all_names:
                name_stem = Path(name).stem.lower()
                d = Levenshtein.distance(term.lower(), name_stem)
                if 0 < d <= self.typo_tolerance:
                    if best is None or d < best[1]:
                        best = (name_stem, d)

        return best[0] if best else None

    def parse_query(self, query: str) -> dict:
        q = QueryParser.parse(query)
        must = []
        must_not = []
        any_of = []
        for op, term in q.boolean_groups:
            if op == "AND":
                must.append(term)
            elif op == "NOT":
                must_not.append(term)
            elif op == "OR":
                any_of.append(term)
        wildcards = [t for t in q.terms if "*" in t or "?" in t]
        plain_terms = [t for t in q.terms if "*" not in t and "?" not in t]
        return {
            "terms": plain_terms,
            "must": must,
            "must_not": must_not,
            "any_of": any_of,
            "exact": [q.exact_phrase] if q.exact_phrase else [],
            "wildcards": wildcards,
            "fields": {**q.field_filters, **{k: f"{v[0] or ''}-{v[1] or ''}" for k, v in q.range_filters.items()}},
        }

    def search(
        self,
        query: str,
        doc_store: dict,
        inverted_index: dict,
        extension_filter: str | None = None,
    ) -> list:
        parsed = self.parse_query(query)
        all_terms = (
            parsed["terms"] + parsed["must"] + parsed["any_of"] + parsed["exact"] + parsed["wildcards"]
        )
        if not all_terms and not parsed["fields"]:
            return []

        candidate_ids: set = set()
        for term in all_terms:
            term_lower = term.lower()
            term_trigrams = set()
            for i in range(len(term_lower) - 2):
                term_trigrams.add(term_lower[i : i + 3])
            if not term_trigrams and len(term_lower) > 0:
                if term_lower in {t for ts in inverted_index for t in [ts]}:
                    term_trigrams = {term_lower}

            if term_trigrams:
                sorted_grams = sorted(term_trigrams, key=lambda t: len(inverted_index.get(t, [])))
                term_ids = set(inverted_index.get(sorted_grams[0], []))
                for gram in sorted_grams[1:]:
                    if not term_ids:
                        break
                    term_ids &= set(inverted_index.get(gram, []))
                candidate_ids |= term_ids

        if not all_terms:
            candidate_ids = set(doc_store.keys())

        results = []
        for doc_id in candidate_ids:
            doc = doc_store.get(doc_id)
            if not doc:
                continue

            name = doc.get("name", "")
            ext = doc.get("ext", "")
            name_lower = name.lower()

            if extension_filter and extension_filter != "All Types":
                if ext != extension_filter:
                    continue

            ok = True
            for m_term in parsed["must"]:
                if m_term.lower() not in name_lower:
                    ok = False
                    break
            if not ok:
                continue

            for n_term in parsed["must_not"]:
                if n_term.lower() in name_lower:
                    ok = False
                    break
            if not ok:
                continue

            if parsed["exact"]:
                if parsed["exact"][0].lower() not in name_lower:
                    continue

            if parsed["wildcards"]:
                wc_match = False
                for wc in parsed["wildcards"]:
                    if _compile_glob(wc).search(name_lower):
                        wc_match = True
                        break
                if not wc_match:
                    continue

            for field, val in parsed["fields"].items():
                alt_keys = []
                if field == "extension":
                    alt_keys = ["ext", "extension"]
                elif field == "ext":
                    alt_keys = ["ext", "extension"]
                else:
                    alt_keys = [field]
                file_val = ""
                for k in alt_keys:
                    v = doc.get(k)
                    if v is not None:
                        file_val = str(v).lower()
                        break
                if val.startswith("!"):
                    if val[1:].lower() in file_val or val[1:].lower() == file_val:
                        ok = False
                        break
                else:
                    val_lower = val.lower()
                    if field in ("extension", "ext"):
                        match = val_lower == file_val or f".{val_lower}" == file_val or val_lower == file_val.lstrip(".")
                    else:
                        match = val_lower in file_val or val_lower == file_val
                    if not match:
                        ok = False
                        break
            if not ok:
                continue

            if parsed["terms"] or parsed["any_of"]:
                terms_ok = all(t.lower() in name_lower for t in parsed["terms"]) if parsed["terms"] else True
                any_of_ok = any(t.lower() in name_lower for t in parsed["any_of"]) if parsed["any_of"] else False
                if not (terms_ok or any_of_ok):
                    continue

            results.append(doc)

        results.sort(key=lambda x: (len(x.get("name", "")), x.get("name", "")))
        return results

    def suggest(self, prefix: str, limit: int = 10) -> List[str]:
        if not prefix:
            return []
        prefix_lower = prefix.lower()
        seen: Set[str] = set()
        suggestions: List[Tuple[str, int]] = []

        freq_map: Dict[str, int] = defaultdict(int)
        for f in self.index.iter_all_files():
            name = f.get("name", "")
            freq_map[name] += 1

        for name, freq in freq_map.items():
            if name.lower().startswith(prefix_lower) and name not in seen:
                seen.add(name)
                suggestions.append((name, freq))

        suggestions.sort(key=lambda x: (-x[1], x[0]))
        return [s[0] for s in suggestions[:limit]]

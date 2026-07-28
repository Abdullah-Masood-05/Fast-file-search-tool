from __future__ import annotations

import math
import re
import threading
import time

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .persistent_index import PersistentIndex


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


class QueryParser:
    _FIELD_PATTERN = re.compile(r'(name|path|ext|extension|size|date|modified|created):\s*(\S+)')
    _RANGE_PATTERN = re.compile(r'(size|date|modified|created):\s*([<>=!]+)\s*(\S+)')
    _RANGE_DOTDOT = re.compile(r'(size|date|modified|created):\s*(\S+?)\.\.(\S+)')
    _QUOTED = re.compile(r'"([^"]+)"')
    _BOOL_OPS = {"AND", "OR", "NOT", "&", "|", "!"}

    @classmethod
    def parse(cls, raw: str) -> SearchQuery:
        q = SearchQuery(raw=raw.strip())
        if not q.raw:
            return q

        q.is_regex = q.raw.startswith("re:") or q.raw.startswith("regex:")
        if q.is_regex:
            q.raw = re.sub(r'^(re|regex):', '', q.raw).strip()
            q.terms = [q.raw]
            return q

        q.case_sensitive = q.raw.startswith("cs:") or "case:" in q.raw[:6]
        if q.case_sensitive:
            q.raw = re.sub(r'^(cs|case):', '', q.raw).strip()

        exact = cls._QUOTED.search(q.raw)
        if exact:
            q.exact_phrase = exact.group(1)
            q.raw = cls._QUOTED.sub("", q.raw).strip()

        for m in cls._RANGE_DOTDOT.finditer(q.raw):
            field, lo, hi = m.groups()
            q.range_filters[field] = (lo, hi)
            q.raw = q.raw.replace(m.group(0), "")

        for m in cls._RANGE_PATTERN.finditer(q.raw):
            field, op, val = m.groups()
            if op == ">":
                q.range_filters[field] = (val, None)
            elif op == "<":
                q.range_filters[field] = (None, val)
            elif op in (">=", ">="):
                q.range_filters[field] = (val, None)
            elif op in ("<=", "=<"):
                q.range_filters[field] = (None, val)
            elif op == "=":
                q.field_filters[field] = val
            elif op == "!":
                q.field_filters[field] = f"!{val}"
            q.raw = q.raw.replace(m.group(0), "")

        for m in cls._FIELD_PATTERN.finditer(q.raw):
            field, val = m.groups()
            if field == "ext":
                field = "extension"
            if val.startswith("!"):
                q.field_filters[field] = val
            else:
                q.field_filters[field] = val
            q.raw = q.raw.replace(m.group(0), "")

        tokens = q.raw.split()
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t.upper() in cls._BOOL_OPS or t in cls._BOOL_OPS:
                op = t.upper().replace("&", "AND").replace("|", "OR").replace("!", "NOT")
                if i + 1 < len(tokens):
                    q.boolean_groups.append((op, tokens[i + 1]))
                    i += 2
                    continue
            else:
                q.terms.append(t)
            i += 1

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
        return Levenshtein.distance(term.lower(), target.lower()) <= max_dist


class LRUCache:
    def __init__(self, maxsize: int = 128):
        self._maxsize = maxsize
        self._cache: OrderedDict = OrderedDict()
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
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def remove(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)



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
        name = file.get("name", "")
        modified = file.get("modified", 0)
        path = file.get("path", "")
        score = 0.0

        if query.exact_phrase:
            if query.exact_phrase.lower() in name.lower():
                score += 10.0
            if query.exact_phrase.lower() == name.lower():
                score += 20.0

        for term in query.terms:
            if query.case_sensitive:
                matches = term in name
            else:
                matches = term.lower() in name.lower()
            if matches:
                score += self.compute_tfidf(term, name, total_files, term_freq) * 2.0

            if Levenshtein.fuzzy_match(term, name, self.typo_tolerance):
                score += 0.5

        path_depth = path.count("/") + path.count("\\")
        score += max(0, 5 - path_depth) * 0.3

        age_hours = (now - modified) / 3600 if modified else 1e9
        recent_bonus = max(0, 1.0 - math.log1p(age_hours) / 100.0)
        score += recent_bonus * 2.0

        if query.terms:
            exact_matches = sum(1 for t in query.terms if t.lower() == name.lower())
            score += exact_matches * 15.0
            partial_matches = sum(
                1 for t in query.terms if len(t) > 2 and t.lower() in name.lower()
            )
            score += partial_matches * 3.0

        return score

    def _matches_field_filter(self, file: dict, field: str, value: str) -> bool:
        file_val = str(file.get(field, "")).lower()
        negate = value.startswith("!")
        val = value.lstrip("!").lower()

        if field in ("name", "path", "extension"):
            match = val in file_val or file_val == val
            if "*" in val:
                pat = ".*".join(re.escape(p) for p in val.split("*"))
                match = bool(re.search(pat, file_val))
            return not match if negate else match

        return True

    def _matches_range(self, file: dict, field: str, lo: Optional[str], hi: Optional[str]) -> bool:
        val = file.get(field)
        if val is None:
            return False
        if isinstance(val, (int, float)):
            if lo is not None:
                lo_val = self._parse_size(lo)
                if lo_val is not None and val < lo_val:
                    return False
            if hi is not None:
                hi_val = self._parse_size(hi)
                if hi_val is not None and val > hi_val:
                    return False
            return True
        if isinstance(val, str) and field in ("date", "modified", "created"):
            return True
        return False

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

    def _apply_boolean(self, file: dict, query: SearchQuery) -> bool:
        name_lower = file.get("name", "").lower()
        for op, term in query.boolean_groups:
            term_lower = term.lower()
            match = term_lower in name_lower
            if op == "NOT" and match:
                return False
            if op == "AND" and not match:
                return False
        return True

    def search_persistent(
        self,
        query_str: str,
        page: int = 1,
        page_size: int = 50,
        use_cache: bool = True,
    ) -> SearchResponse:
        start = time.time()
        resp = SearchResponse(page=page, page_size=page_size)

        query = QueryParser.parse(query_str)
        if not query.raw and not query.exact_phrase and not query.field_filters:
            resp.query_time_ms = (time.time() - start) * 1000
            return resp

        cache_key = f"{query_str}:p{page}:ps{page_size}" if use_cache else ""
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                resp = cached
                resp.query_time_ms = (time.time() - start) * 1000
                return resp

        candidates = self._collect_candidates(query)
        now = time.time()
        
        try:
            conn = self.index._get_conn()
            row = conn.execute("SELECT COUNT(*) FROM files").fetchone()
            total_files = row[0] if row else len(candidates)
        except Exception:
            total_files = len(candidates)

        term_freq_map: Dict[str, int] = defaultdict(int)
        for f in candidates:
            name_lower = f.get("name", "").lower()
            for t in query.terms:
                if t.lower() in name_lower:
                    term_freq_map[t.lower()] += 1


        scored: List[SearchResult] = []
        seen: Set[str] = set()
        for f in candidates:
            p = f.get("path", "")
            if p in seen:
                continue
            seen.add(p)

            if not self._apply_boolean(f, query):
                continue

            ok = True
            for field, val in query.field_filters.items():
                if not self._matches_field_filter(f, field, val):
                    ok = False
                    break
            if not ok:
                continue

            for field, (lo, hi) in query.range_filters.items():
                if not self._matches_range(f, field, lo, hi):
                    ok = False
                    break
            if not ok:
                continue

            term_freq = max(term_freq_map.get(t.lower(), 0) for t in query.terms) if query.terms else 0
            score = self.rank_result(f, query, total_files, term_freq, now)

            sr = SearchResult(
                path=p,
                name=f.get("name", ""),
                extension=f.get("extension", ""),
                size=f.get("size", 0),
                modified=f.get("modified", 0),
                is_folder=f.get("is_folder", False),
                score=score,
                matches=[t for t in query.terms if t.lower() in f.get("name", "").lower()],
            )
            scored.append(sr)

        if query.exact_phrase:
            scored = [r for r in scored if query.exact_phrase.lower() in r.name.lower()]

        scored.sort(key=lambda r: (-r.score, r.name.lower()))
        resp.total_count = len(scored)
        resp.total_pages = max(1, math.ceil(resp.total_count / page_size))

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        resp.results = scored[start_idx:end_idx]

        resp.query_time_ms = (time.time() - start) * 1000

        if resp.total_count == 0 and query.terms:
            resp.did_you_mean = self._suggest_correction(query)

        if use_cache:
            self.cache.put(cache_key, resp)

        for cb in self._on_search_callbacks:
            try:
                cb(query_str, resp.total_count)
            except Exception:
                continue

        return resp

    def _collect_candidates(self, query: SearchQuery) -> List[dict]:
        terms = list(query.terms)
        if query.exact_phrase:
            terms.append(query.exact_phrase)
        if not terms:
            return list(self.index.iter_all_files())

        candidates_map: Dict[str, dict] = {}
        for t in terms:
            clean_t = t.strip()
            if not clean_t:
                continue
            matches = self.index.search_trigram(clean_t)
            for item in matches:
                candidates_map[item["path"]] = item

        return list(candidates_map.values())


    def _suggest_correction(self, query: SearchQuery) -> Optional[str]:
        all_names: List[str] = []
        for f in self.index.iter_all_files():
            all_names.append(f.get("name", ""))
            if len(all_names) >= 5000:
                break

        best: Optional[Tuple[str, int]] = None
        for term in query.terms:
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
            "fields": {**q.field_filters, **{k: f"{v[0]}-{v[1]}" for k, v in q.range_filters.items()}},
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
                    pat = ".*".join(
                        re.escape(p) if c != "*" else ".*"
                        for p in wc.split("*")
                        for c in [""]
                    )
                    if "?" in wc:
                        pat = wc.replace("?", ".")
                    else:
                        pat = ".*".join(re.escape(p) for p in wc.split("*"))
                    if re.search(pat, name_lower):
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

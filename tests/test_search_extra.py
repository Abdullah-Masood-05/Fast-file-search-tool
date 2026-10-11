"""Regression tests for the search engine, search history and the search/browse UI."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fast_file_search.persistent_index import PersistentIndex
from fast_file_search.search import (
    Levenshtein,
    QueryParser,
    SearchEngine,
    SearchResponse,
    SearchResult,
)
from fast_file_search.search_history import Autocomplete, SearchHistory


def _file(path, size=100, modified=1_700_000_000, is_folder=False):
    name = path.rsplit("/", 1)[-1]
    ext = "" if is_folder or "." not in name else "." + name.rsplit(".", 1)[-1].lower()
    return {
        "path": path,
        "name": name,
        "extension": ext,
        "size": size,
        "modified": modified,
        "created": modified,
        "is_folder": is_folder,
        "parent_path": path.rsplit("/", 1)[0],
    }


@pytest.fixture
def engine(tmp_path):
    index = PersistentIndex(db_path=tmp_path / "index.db")
    now = int(time.time())
    files = [
        _file("/docs/annual_report_2024.pdf", 2 * 1024 * 1024, 1_700_000_000),
        _file("/docs/draft_notes.txt", 5 * 1024, 1_710_000_000),
        _file("/docs/my docs/final_draft.txt", 50, 1_720_000_000),
        _file("/code/main.c", 300, now),
        _file("/code/main.cpp", 400, now),
        _file("/code/Readme.md", 1000, now),
        _file("/code/README.txt", 1000, now),
        _file("/code/future.py", 10, now + 10 * 24 * 3600),  # mtime in the future
        _file("/pics/photo_a.jpg", 3 * 1024 * 1024, 1_600_000_000),
        _file("/pics/photo_b.png", 4 * 1024 * 1024, 1_650_000_000),
        _file("/odd/50%_off_my_file.txt", 5, 1_700_000_000),
    ]
    index.add_files_batch(files)
    eng = SearchEngine(persistent_index=index)
    yield eng
    index.close()


def names(resp):
    return sorted(r.name for r in resp.results)


# ---------------------------------------------------------------- query parser

def test_parser_case_prefix_and_regex_prefix():
    q = QueryParser.parse("cs:Readme")
    assert q.case_sensitive and q.terms == ["Readme"]
    q = QueryParser.parse("case:Readme")
    assert q.case_sensitive and q.terms == ["Readme"]
    # "case:" appearing later in the query is not a case-sensitivity switch
    assert not QueryParser.parse("a case:b").case_sensitive
    q = QueryParser.parse("re:^main\\.c$")
    assert q.is_regex and q.terms == ["^main\\.c$"]


def test_parser_boolean_words_must_be_uppercase():
    q = QueryParser.parse("rock and roll")
    assert q.terms == ["rock", "and", "roll"] and not q.boolean_groups
    q = QueryParser.parse("a OR b")
    assert q.boolean_groups == [("OR", "b")]


def test_parser_field_word_boundary_and_quoted_values():
    q = QueryParser.parse("myname:foo")
    assert not q.field_filters and q.terms == ["myname:foo"]
    q = QueryParser.parse('path:"my docs" draft')
    assert q.field_filters["path"] == "my docs"
    assert q.terms == ["draft"] and q.exact_phrase is None


def test_parser_ranges_and_extra_quotes():
    q = QueryParser.parse("size:1mb..5mb")
    assert q.range_filters["size"] == ("1mb", "5mb")
    q = QueryParser.parse("size:<=5mb date:>=2024-01-01")
    assert q.range_filters["size"] == (None, "5mb")
    assert q.range_filters["date"] == ("2024-01-01", None)
    q = QueryParser.parse('"first one" "second one"')
    assert q.exact_phrase == "first one" and q.terms == ["second one"]
    # unbalanced quote does not poison terms
    assert QueryParser.parse('"foo').terms == ["foo"]


# ------------------------------------------------------------------- searching

def test_multiple_terms_are_anded(engine):
    resp = engine.search_persistent("draft notes", use_cache=False)
    assert names(resp) == ["draft_notes.txt"]


def test_or_and_not(engine):
    resp = engine.search_persistent("photo_a OR photo_b", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]
    resp = engine.search_persistent("draft NOT final", use_cache=False)
    assert names(resp) == ["draft_notes.txt"]
    resp = engine.search_persistent("draft AND final", use_cache=False)
    assert names(resp) == ["final_draft.txt"]


def test_wildcards(engine):
    resp = engine.search_persistent("*.jpg", use_cache=False)
    assert names(resp) == ["photo_a.jpg"]
    resp = engine.search_persistent("photo_?.*", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]


def test_extension_filter_is_exact_not_substring(engine):
    resp = engine.search_persistent("main ext:c", use_cache=False)
    assert names(resp) == ["main.c"]
    resp = engine.search_persistent("main ext:.cpp", use_cache=False)
    assert names(resp) == ["main.cpp"]
    resp = engine.search_persistent("photo ext:jpg,png", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]
    resp = engine.search_persistent("main ext:!c", use_cache=False)
    assert names(resp) == ["main.cpp"]


def test_filter_only_queries(engine):
    resp = engine.search_persistent("ext:png", use_cache=False)
    assert names(resp) == ["photo_b.png"]
    resp = engine.search_persistent("size:>2mb", use_cache=False)
    assert names(resp) == ["annual_report_2024.pdf", "photo_a.jpg", "photo_b.png"]


def test_size_and_date_ranges(engine):
    resp = engine.search_persistent("photo size:3mb..3.5mb", use_cache=False)
    assert names(resp) == ["photo_a.jpg"]
    # "date" is an alias of modified; previously it matched nothing at all
    resp = engine.search_persistent("photo date:2020", use_cache=False)
    assert names(resp) == ["photo_a.jpg"]
    resp = engine.search_persistent("photo modified:2021-01-01..2022-12-31", use_cache=False)
    assert names(resp) == ["photo_b.png"]


def test_path_filter_accepts_forward_slashes_on_any_platform(engine):
    # stored paths may use either separator; the filter must not care
    engine.index.add_file(_file("/x/y/win_style.txt"))
    engine.index.add_file(
        {**_file("/x/y/win_style2.txt"), "path": "C:\\x\\y\\win_style2.txt", "parent_path": "C:\\x\\y"}
    )
    resp = engine.search_persistent("win_style path:x/y", use_cache=False)
    assert names(resp) == ["win_style.txt", "win_style2.txt"]


def test_quoted_path_filter_with_spaces(engine):
    resp = engine.search_persistent('draft path:"my docs"', use_cache=False)
    assert names(resp) == ["final_draft.txt"]


def test_case_sensitive_search(engine):
    assert names(engine.search_persistent("readme", use_cache=False)) == ["README.txt", "Readme.md"]
    assert names(engine.search_persistent("cs:Readme", use_cache=False)) == ["Readme.md"]
    assert names(engine.search_persistent("case:README", use_cache=False)) == ["README.txt"]


def test_regex_query(engine):
    resp = engine.search_persistent(r"re:^main\.(c|cpp)$", use_cache=False)
    assert names(resp) == ["main.c", "main.cpp"]
    resp = engine.search_persistent("re:photo_[ab]", use_cache=False)
    assert names(resp) == ["photo_a.jpg", "photo_b.png"]


def test_invalid_regex_reports_error_instead_of_raising(engine):
    resp = engine.search_persistent("re:([", use_cache=False)
    assert resp.total_count == 0
    assert resp.error and "regular expression" in resp.error


def test_future_modified_time_does_not_crash_ranking(engine):
    resp = engine.search_persistent("future", use_cache=False)
    assert names(resp) == ["future.py"]


def test_literal_percent_and_underscore(engine):
    resp = engine.search_persistent("50%_off", use_cache=False)
    assert names(resp) == ["50%_off_my_file.txt"]


def test_pagination_clamps_bad_values(engine):
    resp = engine.search_persistent("txt", page=0, page_size=0, use_cache=False)
    assert resp.page == 1 and resp.page_size == 1
    assert len(resp.results) == 1
    assert resp.total_count >= 3 and resp.total_pages == resp.total_count
    resp2 = engine.search_persistent("txt", page=2, page_size=1, use_cache=False)
    assert resp2.results and resp2.results[0].path != resp.results[0].path


def test_cache_returns_copies_so_post_filtering_does_not_corrupt_it(engine):
    first = engine.search_persistent("txt")
    count = first.total_count
    assert count >= 3
    first.results = []          # what a caller doing post-filtering does
    first.total_count = 0
    second = engine.search_persistent("txt")
    assert second.total_count == count and len(second.results) == count
    third = engine.search_persistent("txt")
    assert third is not second and third.results is not second.results


def test_cache_is_invalidated_when_index_changes(engine):
    assert engine.search_persistent("zebra").total_count == 0
    engine.index.add_file(_file("/z/zebra.txt"))
    assert engine.search_persistent("zebra").total_count == 1
    engine.index.remove_file("/z/zebra.txt")
    assert engine.search_persistent("zebra").total_count == 0


def test_did_you_mean_for_misspelling(engine):
    resp = engine.search_persistent("photo_c", use_cache=False)
    assert resp.total_count == 0
    assert resp.did_you_mean in ("photo_a", "photo_b")


def test_fuzzy_match_length_shortcut():
    assert not Levenshtein.fuzzy_match("a", "abcdefgh", 2)
    assert Levenshtein.fuzzy_match("report", "reprot", 2)


def test_only_requested_page_is_materialised(engine):
    resp = engine.search_persistent("txt", page=1, page_size=2, use_cache=False)
    assert len(resp.results) == 2 and resp.total_count >= 3


# --------------------------------------------------------------- search history

def test_history_dedupes_and_bounds_size(tmp_path):
    h = SearchHistory(db_path=tmp_path / "h.db")
    h.add_search("alpha", 1)
    h.add_search("beta", 2)
    h.add_search("alpha", 3)
    h.add_search("   ")      # ignored
    h.add_search("")         # ignored
    recent = h.recent()
    assert [e.query for e in recent] == ["alpha", "beta"]
    assert recent[0].result_count == 3
    assert dict(h.frequent_queries())["alpha"] == 2

    for i in range(250):
        h.add_search(f"q{i}", i)
    total = h._conn.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
    assert total == SearchHistory.MAX_ENTRIES
    assert h.recent(1)[0].query == "q249"      # stable newest-first even with equal timestamps
    h.close()


def test_history_pinned_flag_and_persistence(tmp_path):
    path = tmp_path / "h.db"
    h = SearchHistory(db_path=path)
    h.add_search("keep me", 4)
    h.pin_search("keep me")
    assert h.recent()[0].pinned is True
    h.unpin_search("keep me")
    assert h.recent()[0].pinned is False
    h.close()
    h2 = SearchHistory(db_path=path)
    assert [e.query for e in h2.recent()] == ["keep me"]
    h2.close()


def test_history_recovers_from_corrupt_database(tmp_path):
    path = tmp_path / "h.db"
    path.write_bytes(b"this is definitely not an sqlite database" * 50)
    h = SearchHistory(db_path=path)          # must not raise
    h.add_search("works again", 1)
    assert [e.query for e in h.recent()] == ["works again"]
    h.close()
    assert (tmp_path / "h.db.corrupt").exists()


def test_history_falls_back_to_memory_when_location_unusable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    h = SearchHistory(db_path=blocker / "sub" / "h.db")   # parent is a file -> mkdir fails
    h.add_search("still ok", 1)
    assert h.recent()[0].query == "still ok"
    h.close()


def test_autocomplete_treats_like_wildcards_literally(tmp_path):
    index = PersistentIndex(db_path=tmp_path / "i.db")
    index.add_files_batch([_file("/a/my_file.txt"), _file("/a/myXfile.txt"), _file("/a/50%.txt")])
    ac = Autocomplete(persistent_index=index)
    assert ac.suggest("my_f") == ["my_file.txt"]
    assert ac.suggest("50%") == ["50%.txt"]
    assert ac.suggest("%") == ["50%.txt"]
    index.close()


def test_context_suggest_prefers_current_folder(tmp_path):
    index = PersistentIndex(db_path=tmp_path / "i.db")
    index.add_files_batch([_file("/a/one/report.txt"), _file("/b/two/report_final.txt"),
                           _file("/b/two/other.txt")])
    ac = Autocomplete(persistent_index=index)
    assert ac.context_suggest("report", "/b/two") == ["report_final.txt", "report.txt"]
    assert ac.context_suggest("report", "") [0] in ("report.txt", "report_final.txt")
    assert ac.context_suggest("", "/b") == []
    index.close()

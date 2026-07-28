import pytest
from fast_file_search.search import QueryParser, SearchEngine, SearchQuery
from fast_file_search.persistent_index import PersistentIndex


def test_query_parser():
    # Test simple search term
    q = QueryParser.parse("report")
    assert "report" in q.terms
    assert not q.exact_phrase
    assert not q.field_filters

    # Test exact phrase quotes
    q = QueryParser.parse('"annual report"')
    assert q.exact_phrase == "annual report"

    # Test fields filters
    q = QueryParser.parse("ext:pdf size:>10mb")
    assert q.field_filters["extension"] == "pdf"
    assert q.range_filters["size"] == ("10mb", None)

    # Test boolean grouping
    q = QueryParser.parse("report AND draft NOT final")
    assert ("AND", "draft") in q.boolean_groups
    assert ("NOT", "final") in q.boolean_groups


def test_search_engine_basic(tmp_path):
    # Setup temporary index database
    db_path = tmp_path / "index.db"
    index = PersistentIndex(db_path=db_path)
    
    file_info1 = {
        "path": "/docs/annual_report_2024.pdf",
        "name": "annual_report_2024.pdf",
        "extension": ".pdf",
        "size": 2 * 1024 * 1024,
        "modified": 1700000000,
        "created": 1700000000,
        "is_folder": False,
        "parent_path": "/docs"
    }
    file_info2 = {
        "path": "/docs/draft_notes.txt",
        "name": "draft_notes.txt",
        "extension": ".txt",
        "size": 5 * 1024,
        "modified": 1710000000,
        "created": 1710000000,
        "is_folder": False,
        "parent_path": "/docs"
    }
    index.add_file(file_info1)
    index.add_file(file_info2)

    engine = SearchEngine(persistent_index=index)
    
    # Test basic trigram search
    resp = engine.search_persistent("report", use_cache=False)
    assert resp.total_count == 1
    assert resp.results[0].name == "annual_report_2024.pdf"

    # Test file type filter match
    resp = engine.search_persistent("notes ext:txt", use_cache=False)
    assert resp.total_count == 1
    assert resp.results[0].extension == ".txt"

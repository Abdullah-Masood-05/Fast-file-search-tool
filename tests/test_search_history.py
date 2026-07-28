import pytest
import time
from fast_file_search.search_history import SearchHistory, Autocomplete, SearchRecommender
from fast_file_search.persistent_index import PersistentIndex


def test_search_history(tmp_path):
    db_path = tmp_path / "history.db"
    history = SearchHistory(db_path=db_path)

    # Test adding items
    history.add_search("annual report", 15)
    history.add_search("config files", 5)

    recent = history.recent(limit=5)
    assert len(recent) == 2
    assert recent[0].query == "config files"
    assert recent[0].result_count == 5

    # Test favorites
    assert not history.is_favorite("/docs/file.txt")
    history.add_favorite("/docs/file.txt")
    assert history.is_favorite("/docs/file.txt")

    favs = history.get_favorites()
    assert "/docs/file.txt" in favs

    history.remove_favorite("/docs/file.txt")
    assert not history.is_favorite("/docs/file.txt")

    # Test annotations / notes
    history.set_note("/docs/file.txt", "Important document")
    assert history.get_note("/docs/file.txt") == "Important document"

    # Test tags
    history.add_tag("/docs/file.txt", "Work")
    history.add_tag("/docs/file.txt", "Finance")
    tags = history.get_tags("/docs/file.txt")
    assert "Work" in tags
    assert "Finance" in tags

    history.remove_tag("/docs/file.txt", "Work")
    assert "Work" not in history.get_tags("/docs/file.txt")

    history.close()


def test_autocomplete_and_recommender(tmp_path):
    db_path = tmp_path / "index.db"
    index = PersistentIndex(db_path=db_path)
    
    file_info = {
        "path": "/docs/invoice.pdf",
        "name": "invoice.pdf",
        "extension": ".pdf",
        "size": 150000,
        "modified": int(time.time()),
        "created": int(time.time()),
        "is_folder": False,
        "parent_path": "/docs"
    }
    index.add_file(file_info)

    autocomplete = Autocomplete(persistent_index=index)
    recommender = SearchRecommender(persistent_index=index)

    # Test suggestions
    suggestions = autocomplete.suggest("invo", limit=5)
    assert len(suggestions) >= 1
    assert "invoice.pdf" in suggestions

    # Test spelling correction did you mean
    dym = recommender.did_you_mean("invice")
    assert dym is not None
    assert "invoice" in dym

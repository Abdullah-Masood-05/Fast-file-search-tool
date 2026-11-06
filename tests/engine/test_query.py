"""Query parser table tests (design A.5), including the v1 behaviours that still apply."""
from __future__ import annotations

from datetime import datetime

import pytest

from fast_file_search.engine.query import (Atom, glob_regex, parse, parse_size, parse_size_range,
                                           parse_time_range)

NOW = datetime(2025, 6, 15, 12, 0, 0).timestamp()


def P(text):
    return parse(text, now=NOW)


def test_plain_terms_are_folded_and_anded():
    q = P("Annual  REPORT résumé")
    assert q.positive_terms == ["annual", "report", "resume"]
    assert not q.any_groups and not q.negative_terms and q.error is None
    assert q.first_term() == "annual"
    assert P("a a b").positive_terms == ["a", "b"]           # duplicates collapse


def test_empty_and_whitespace():
    for t in ("", "   ", "\t"):
        q = P(t)
        assert q.is_empty() and q.error is None


def test_or_binds_tighter_than_and():
    # v1 parsed this as (a and c) or b; 2.0 is Everything-style (a or b) and c
    q = P("a OR b c")
    assert q.any_groups == [(Atom("sub", "a"), Atom("sub", "b"))]
    assert q.positive_terms == ["c"]
    q = P("a | b | c d")
    assert q.any_groups == [(Atom("sub", "a"), Atom("sub", "b"), Atom("sub", "c"))]
    assert q.positive_terms == ["d"]
    q = P("jpg|png photo")
    assert q.any_groups == [(Atom("sub", "jpg"), Atom("sub", "png"))] and q.positive_terms == ["photo"]
    # a dangling OR is ignored
    assert P("OR a").positive_terms == ["a"]
    assert P("a OR").positive_terms == ["a"]


def test_boolean_words_must_be_uppercase():
    q = P("rock and roll")
    assert q.positive_terms == ["rock", "and", "roll"] and not q.any_groups
    q = P("rock or roll")
    assert q.positive_terms == ["rock", "or", "roll"]
    q = P("draft AND final")
    assert q.positive_terms == ["draft", "final"]


def test_negation_forms():
    for text in ("draft NOT final", "draft -final", "draft !final", "draft ! final"):
        q = P(text)
        assert q.positive_terms == ["draft"], text
        assert q.negative_terms == [Atom("sub", "final")], text
    q = P('-"two words" x')
    assert q.negative_terms == [Atom("sub", "two words")]
    q = P("-*.tmp")
    assert q.negative_terms == [Atom("glob", "*.tmp")]
    assert P("-").positive_terms == ["-"]     # a lone dash is a literal term


def test_phrases_and_quotes():
    q = P('"annual report"')
    assert q.positive_terms == ["annual report"]
    q = P('"first one" "second one"')
    assert q.positive_terms == ["first one", "second one"]
    assert P('"foo').positive_terms == ["foo"]               # unbalanced quote does not poison terms
    q = P('"*.txt"')                                         # quoted wildcards are literal
    assert q.positive_terms == ["*.txt"] and not q.globs
    q = P('"a/b"')
    assert q.positive_terms == ["a/b"] and not q.path_terms


def test_globs_are_anchored():
    q = P("*.min.js")
    assert q.globs == ["*.min.js"] and not q.positive_terms
    q = P("photo_?.*")
    assert q.globs == ["photo_?.*"]
    import re
    assert re.match(glob_regex("report*"), "report_final.txt")
    assert not re.match(glob_regex("report*"), "my_report_x")   # changed from v1 (substring)
    assert re.match(glob_regex("*report*"), "my_report_x")
    assert re.match(glob_regex("x[1].txt"), "x[1].txt")          # "[" is literal
    assert P("*").globs == ["*"] and P("**").globs == ["*"]


def test_path_terms():
    q = P("src/index")
    assert q.path_terms == [Atom("path", "src/index", "src", "index")]
    q = P("src\\Components\\Index.ts")
    assert q.path_terms == [Atom("path", "src/components/index.ts", "src/components", "index.ts")]
    q = P("src/")
    assert q.path_terms == [Atom("path", "src/", "src", "")]
    q = P("/index")
    assert q.path_terms == [Atom("path", "/index", "", "index")]
    q = P("components/*.ts")
    assert q.path_terms[0].name_part == "*.ts"


def test_case_prefix():
    q = P("case:Readme")
    assert q.case_sensitive and q.positive_terms == ["Readme"]
    q = P("cs:Readme")
    assert q.case_sensitive and q.positive_terms == ["Readme"]
    q = P("CASE:Re\u0301sume\u0301")
    assert q.case_sensitive and q.positive_terms == ["Résumé"]     # NFC, not folded
    assert not P("a case:b").case_sensitive                          # only as a prefix
    assert P("a case:b").positive_terms == ["a", "case:b"]


def test_regex_prefix_and_errors():
    q = P(r"re:^main\.(c|cpp)$")
    assert q.regex == r"^main\.(c|cpp)$" and q.has_text()
    q = P("regex:a b")
    assert q.regex == "a b"
    q = P("ext:py re:^test_ ")
    assert q.regex == "^test_" and q.token_filters.exts == frozenset({"py"})
    q = P("re:Café")
    assert q.regex == "Cafe"                                         # non-ASCII literals folded (re.I does ASCII case)
    q = P("case:re:Café")
    assert q.case_sensitive and q.regex == "Café"
    q = P("re:([")
    assert q.error and "regular expression" in q.error
    assert P("re:").regex is None


def test_field_word_boundary_and_quoted_values():
    q = P("myname:foo")
    assert q.positive_terms == ["myname:foo"] and q.token_filters.is_empty()
    q = P('path:"my docs" draft')
    assert q.token_filters.path_contains == "my docs" and q.positive_terms == ["draft"]
    q = P("name:index")
    assert q.positive_terms == ["index"]
    q = P("name:*.txt")
    assert q.globs == ["*.txt"]
    q = P("path:x\\y")
    assert q.token_filters.path_contains == "x/y"                    # either separator
    q = P("path:C:\\Users\\me")
    assert q.token_filters.folder == "C:\\Users\\me" and q.token_filters.path_contains is None
    q = P("path:/home/me")
    assert q.token_filters.folder == "/home/me"
    q = P("path:a path:b")
    assert q.path_needles == ("a", "b")
    q = P("C:\\x")                                                   # not a field
    assert q.path_terms


def test_ext_type_and_kind():
    q = P("ext:pdf,.DOCX notes")
    assert q.token_filters.exts == frozenset({"pdf", "docx"}) and q.positive_terms == ["notes"]
    q = P("main ext:!c")
    assert q.exclude_exts == frozenset({"c"}) and not q.token_filters.exts
    q = P("main -ext:c")
    assert q.exclude_exts == frozenset({"c"})
    q = P("ext:j*")
    assert q.error and "ext:" in q.error
    q = P("type:image")
    assert q.token_filters.kinds == frozenset({"image"})
    q = P("kind:folder type:docs,code")
    assert q.token_filters.kinds == frozenset({"folder", "document", "code"})
    q = P("type:spreadsheet")
    assert q.error and "Unknown type" in q.error
    q = P("ext:")                                                    # still typing: ignored
    assert q.is_empty() and q.error is None


def test_size_parsing():
    assert parse_size("10mb") == 10 * 1024 ** 2
    assert parse_size("2.5 MB") == int(2.5 * 1024 ** 2)
    assert parse_size("512") == 512
    assert parse_size("1k") == 1024 and parse_size("1KiB") == 1024 and parse_size("3gb") == 3 * 1024 ** 3
    with pytest.raises(ValueError):
        parse_size("10 parsecs")
    assert parse_size_range(">10mb") == (10 * 1024 ** 2 + 1, None)
    assert parse_size_range(">=10mb") == (10 * 1024 ** 2, None)
    assert parse_size_range("<5k") == (None, 5 * 1024)
    assert parse_size_range("<=5k") == (None, 5 * 1024 + 1)
    assert parse_size_range("1kb..1mb") == (1024, 1024 ** 2 + 1)
    assert parse_size_range("..1mb") == (None, 1024 ** 2 + 1)
    assert parse_size_range("100") == (100, 101)
    assert parse_size_range("empty") == (0, 1)
    q = P("size:3mb..3.5mb photo")
    assert (q.token_filters.size_min, q.token_filters.size_max) == (3 * 1024 ** 2, int(3.5 * 1024 ** 2) + 1)
    q = P("size:>1kb size:<1mb")                                     # repeated fields intersect
    assert (q.token_filters.size_min, q.token_filters.size_max) == (1025, 1024 ** 2)
    q = P("size:>10parsecs")
    assert q.error and "size" in q.error.lower()
    assert P("size:>").is_empty()                                    # incomplete: ignored


def ts(*a):
    return datetime(*a).timestamp()


def test_date_parsing():
    assert parse_time_range("2024", NOW) == (ts(2024, 1, 1), ts(2025, 1, 1))
    assert parse_time_range("2024-03", NOW) == (ts(2024, 3, 1), ts(2024, 4, 1))
    assert parse_time_range("2024-12", NOW) == (ts(2024, 12, 1), ts(2025, 1, 1))
    assert parse_time_range("2024-03-15", NOW) == (ts(2024, 3, 15), ts(2024, 3, 16))
    assert parse_time_range("2024/03/15", NOW) == (ts(2024, 3, 15), ts(2024, 3, 16))
    assert parse_time_range(">2025-01-01", NOW) == (ts(2025, 1, 2), None)
    assert parse_time_range(">=2025-01-01", NOW) == (ts(2025, 1, 1), None)
    assert parse_time_range("<2024", NOW) == (None, ts(2024, 1, 1))
    assert parse_time_range("<=2024", NOW) == (None, ts(2025, 1, 1))
    assert parse_time_range("2025-01..2025-06", NOW) == (ts(2025, 1, 1), ts(2025, 7, 1))
    assert parse_time_range("today", NOW) == (ts(2025, 6, 15), ts(2025, 6, 16))
    assert parse_time_range("yesterday", NOW) == (ts(2025, 6, 14), ts(2025, 6, 15))
    assert parse_time_range("7d", NOW) == (NOW - 7 * 86400, None)
    assert parse_time_range("4w", NOW) == (NOW - 28 * 86400, None)
    assert parse_time_range("6m", NOW) == (NOW - 180 * 86400, None)
    assert parse_time_range("1y", NOW) == (NOW - 365 * 86400, None)
    assert parse_time_range("<7d", NOW) == (NOW - 7 * 86400, None)
    assert parse_time_range(">7d", NOW) == (None, NOW - 7 * 86400)
    assert parse_time_range("30d..7d", NOW) == (NOW - 30 * 86400, NOW - 7 * 86400)
    with pytest.raises(ValueError):
        parse_time_range("someday", NOW)
    with pytest.raises(ValueError):
        parse_time_range("2024-13", NOW)


def test_date_fields():
    q = P("photo date:2020")                                         # "date" is an alias of modified
    assert (q.token_filters.mtime_min, q.token_filters.mtime_max) == (ts(2020, 1, 1), ts(2021, 1, 1))
    q = P("photo modified:2021-01-01..2022-12-31")
    assert (q.token_filters.mtime_min, q.token_filters.mtime_max) == (ts(2021, 1, 1), ts(2023, 1, 1))
    q = P("created:>=2024-01-01")
    assert q.ctime_min == ts(2024, 1, 1) and q.ctime_max is None and q.has_token_filters()
    q = P("dm:today")
    assert q.token_filters.mtime_min == ts(2025, 6, 15)
    q = P("date:soon")
    assert q.error and "date" in q.error.lower()
    q = P("size:-1kb")
    assert q.error


def test_highlight_terms():
    q = P('index "two words" src/main *.min.js a|b')
    assert q.highlight_terms() == ["index", "two words", "main", "a", "b", ".min.js"]


def test_literal_percent_and_underscore_are_plain_terms():
    assert P("50%_off").positive_terms == ["50%_off"]
    assert P("%").positive_terms == ["%"]
    assert P("_").positive_terms == ["_"]


def test_parse_never_raises():
    import random
    rnd = random.Random(5)
    alphabet = 'ab "-!|*?/\\:().,<>=0123456789kmgb' + "reextsizedatepathtypecase" + "é日"
    for _ in range(3000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30)))
        P(s)   # must not raise
        P("re:" + s)
        P("case:" + s)

"""Text normalization: fold, index maps, spans, word boundaries and initials (design A.2)."""
from __future__ import annotations

import random
import unicodedata

import pytest

from fast_file_search.engine.textfold import (FOLD_VERSION, boundaries, boundary_mask, fold, fold_with_map,
                                              initials, initials_spans, match_spans, nfc, regex_spans)


def test_fold_version_is_an_int():
    assert isinstance(FOLD_VERSION, int) and FOLD_VERSION >= 1


@pytest.mark.parametrize("raw, key", [
    ("README.md", "readme.md"),
    ("Résumé.pdf", "resume.pdf"),
    ("Résumé.pdf", "resume.pdf"),       # NFD (macOS) folds like NFC
    ("Straße", "strasse"),                           # full case fold
    ("STRASSE", "strasse"),
    ("Café", "cafe"),
    ("über_naïve", "uber_naive"),
    ("日本語ファイル", "日本語ファイル"),              # CJK unchanged
    ("ＡＢＣ", "ａｂｃ"),                              # full-width letters keep their width
    ("İstanbul", "istanbul"),                        # U+0130 -> i + U+0307 -> dot stripped again
    ("ıi", "ıi"),                                    # dotless i stays dotless (documented)
    ("ﬁle", "file"),                                 # ligature casefold
    ("ΣΊΣΥΦΟΣ", "σισυφοσ"),                          # Greek: accents stripped, final sigma folded
    ("Ёлка", "елка"),                                # Cyrillic diaeresis is a U+0308 mark
])
def test_fold_table(raw, key):
    assert fold(raw) == key


def test_fold_keeps_indic_vowel_signs():
    # Devanagari vowel signs are not U+0300-U+036F marks: they must not be stripped.
    s = "किताब"   # किताब
    assert fold(s) == s


def test_fold_replaces_lone_surrogates():
    assert fold("bad\udcffname") == "bad�name"
    assert fold("\ud800x") == "�x"
    assert fold("bad\udcffname").encode("utf-8")   # valid UTF-8 now


def test_fold_ascii_fast_path_is_lower():
    for s in ("ABC def_123.TXT", "", "  ", "%_.[]"):
        assert fold(s) == s.lower()


def test_fold_is_idempotent_and_nfc_insensitive_random():
    rnd = random.Random(1)
    alphabet = "aAeEiIoOuUßẞçÇñÑıİéÉèàâäöüÅåøæœ日本語ｶﾞ̧́̈ _-.ABCxyz0123456789ǅǈﬀﬁ"
    for _ in range(3000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
        k = fold(s)
        assert fold(k) == k, s
        assert fold(unicodedata.normalize("NFD", s)) == k, s
        assert fold(unicodedata.normalize("NFC", s)) == k, s


def test_nfc():
    assert nfc("é") == "é"
    assert nfc("abc") == "abc"
    assert nfc("x\udcff") == "x�"


@pytest.mark.parametrize("name", ["Résumé.pdf", "Résumé.pdf", "Straße.txt", "İstanbul", "日本語", "abc",
                                  "bad\udcffname", "é́x", "한국어", unicodedata.normalize("NFD", "한국어")])
def test_fold_with_map_points_into_the_original(name):
    folded, idx = fold_with_map(name)
    assert folded == fold(name)
    assert len(idx) == len(folded)
    assert all(0 <= i < len(name) for i in idx)
    assert idx == sorted(idx)


def test_match_spans_map_back_to_original_characters():
    assert match_spans("Résumé.pdf", ["resume"]) == [(0, 6)]
    nfd = "Résumé.pdf"                         # 8 code points before ".pdf"
    assert match_spans(nfd, ["resume"]) == [(0, 8)]
    assert match_spans(nfd, ["sume"]) == [(3, 8)]
    assert match_spans("Straße.txt", ["ss"]) == [(4, 5)]   # "ß" covers the whole "ss"
    assert match_spans("Straße.txt", ["strasse"]) == [(0, 6)]
    assert match_spans("index_index.js", ["index"]) == [(0, 5), (6, 11)]
    assert match_spans("abcabc", ["bc", "ca"]) == [(1, 6)]   # overlaps merge
    assert match_spans("abc", []) == [] and match_spans("", ["a"]) == []
    assert match_spans("日本語ファイル.txt", ["ファイル"]) == [(3, 7)]


def test_match_spans_case_sensitive_uses_nfc():
    assert match_spans("Résumé.pdf", ["Résumé"], case_sensitive=True) == [(0, 8)]
    assert match_spans("README", ["readme"], case_sensitive=True) == []


def test_regex_spans():
    import re
    rx = re.compile(r"\d+", re.I)
    assert regex_spans("file123_45.txt", rx) == [(4, 7), (8, 10)]
    assert regex_spans("Café", re.compile("cafe", re.I)) == [(0, 4)]


@pytest.mark.parametrize("name, ini", [
    ("GiftTopIcon123.png", "gti1p"),
    ("my_index_file.ts", "mift"),
    ("README.md", "rm"),
    ("readme", "r"),
    ("UserProfile123.json", "up1j"),
    ("report 2024 (final).xlsx", "r2fx"),
    ("a-b+c,d;e@f#g~h'i", "abcdefghi"),
    ("x[1].txt", "x1t"),
    ("file2go", "f2g"),
    ("", ""),
])
def test_initials(name, ini):
    assert initials(name) == ini


def test_boundary_mask_bits():
    m = boundary_mask("GiftTopIcon123.png")
    assert [p for p in range(20) if m >> p & 1] == [0, 4, 7, 11, 15]
    assert boundary_mask("") == 0
    assert boundary_mask("a" * 100 + "_b") & 1 == 1      # positions >= 64 are simply not recorded
    assert boundary_mask("a" * 100 + "_b") >> 64 == 0


def test_boundaries_non_ascii():
    mask, ini = boundaries("ÜberNaïveFile2.txt")
    assert ini == "unf2t"
    # folded length differs ("ß" -> "ss"): only separator and digit boundaries are used
    mask, ini = boundaries("GroßeDatei_2.txt")
    assert ini == "g2t"


def test_initials_spans():
    assert initials_spans("GiftTopIcon.png", "gti") == [(0, 1), (4, 5), (7, 8)]
    assert initials_spans("GiftTopIcon.png", "zz") == []

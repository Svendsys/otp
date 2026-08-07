"""Tests for the codeword vocabulary and the header fit that constrains it.

The wordlist tests are the only thing standing between a curated vocabulary
and a slow drift into obscure, confusable, or unpronounceable words. A
codeword gets read aloud over a noisy channel and carried in someone's head
from a handover to a radio, so "sayable and picturable" is a functional
requirement, not a style preference.
"""
import re
import string
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as g

NATO = {
    "ALFA", "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF",
    "HOTEL", "INDIA", "JULIETT", "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER",
    "OSCAR", "PAPA", "QUEBEC", "ROMEO", "SIERRA", "TANGO", "UNIFORM", "VICTOR",
    "WHISKEY", "XRAY", "YANKEE", "ZULU",
}

MODIFIERS, NOUNS = g.load_vocabulary()
FLAT_NOUNS = [w for words in NOUNS.values() for w in words]


def soundex(word):
    codes = {**dict.fromkeys("BFPV", "1"), **dict.fromkeys("CGJKQSXZ", "2"),
             **dict.fromkeys("DT", "3"), "L": "4",
             **dict.fromkeys("MN", "5"), "R": "6"}
    out, prev = word[0], codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != prev:
            out += code
        if ch not in "HW":
            prev = code
    return (out + "000")[:4]


def edit_distance(a, b):
    if abs(len(a) - len(b)) > 1:
        return 2
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class TestVocabularyShape:
    # The printed header fits 17 characters at the tightest format, split
    # 7 + 1 + 9 rather than 8 + 1 + 8: the noun carries the picture, so it
    # gets the spare character and BUTTERFLY, CROCODILE and ACCORDION fit.
    MODIFIER_MAX = 7
    NOUN_MAX = 9

    def test_lists_are_populated(self):
        assert len(MODIFIERS) > 300
        assert len(FLAT_NOUNS) > 600
        assert len(NOUNS) >= 13, "categories drive the two-press browse on 3 buttons"

    def test_every_word_is_plain_uppercase(self):
        for word in MODIFIERS:
            assert re.fullmatch(r"[A-Z]{3,%d}" % self.MODIFIER_MAX, word), word
        for word in FLAT_NOUNS:
            assert re.fullmatch(r"[A-Z]{3,%d}" % self.NOUN_MAX, word), word

    def test_the_header_budget_is_spent_but_not_overspent(self):
        # Both halves reach their cap, so the split is actually in use, and
        # the longest pair still lands on the 17 characters the header has.
        assert max(len(w) for w in MODIFIERS) == self.MODIFIER_MAX
        assert max(len(w) for w in FLAT_NOUNS) == self.NOUN_MAX
        assert self.MODIFIER_MAX + 1 + self.NOUN_MAX == 17

    def test_no_noun_is_also_a_modifier(self):
        # Otherwise the vocabulary can roll SCARLET-SCARLET.
        assert not set(MODIFIERS) & set(FLAT_NOUNS)

    def test_no_duplicates_within_a_list(self):
        assert len(set(MODIFIERS)) == len(MODIFIERS)
        assert len(set(FLAT_NOUNS)) == len(FLAT_NOUNS)

    def test_no_nato_alphabet_collisions(self):
        assert not (set(MODIFIERS) | set(FLAT_NOUNS)) & NATO


class TestPhoneticDistinctness:
    """Two codewords that sound alike is the failure this list must not have."""

    def test_modifiers_have_distinct_soundex(self):
        seen = {}
        for word in MODIFIERS:
            code = soundex(word)
            assert code not in seen, f"{word} sounds like {seen.get(code)}"
            seen[code] = word

    def test_nouns_have_distinct_soundex(self):
        seen = {}
        for word in FLAT_NOUNS:
            code = soundex(word)
            assert code not in seen, f"{word} sounds like {seen.get(code)}"
            seen[code] = word

    def test_no_near_misses_by_edit_distance(self):
        for words in (MODIFIERS, FLAT_NOUNS):
            for i, a in enumerate(words):
                for b in words[i + 1:]:
                    if abs(len(a) - len(b)) <= 1:
                        assert edit_distance(a, b) >= 2, f"{a} vs {b}"


class TestRandomCodewords:
    def test_shape_and_uniqueness(self):
        words = g.random_codewords(200)
        assert len(set(words)) == 200
        for word in words:
            modifier, _, noun = word.partition("-")
            assert modifier in MODIFIERS
            assert noun in FLAT_NOUNS

    def test_filename_safe(self):
        # otp_generator rejects anything else; codewords become filenames.
        for word in g.random_codewords(100):
            assert all(ch.isalnum() or ch in "-_" for ch in word)

    def test_refuses_to_exceed_the_vocabulary(self):
        try:
            g.random_codewords(len(MODIFIERS) * len(FLAT_NOUNS) + 1)
        except ValueError:
            return
        raise AssertionError("should refuse more codewords than combinations exist")

    def test_draws_from_the_csprng_not_random_module(self, monkeypatch):
        calls = []
        real = g.get_random_bytes
        monkeypatch.setattr(g, "get_random_bytes", lambda n: calls.append(n) or real(n))
        g.random_codewords(5)
        assert calls, "codeword selection must use the same CSPRNG as the key material"


class TestHeaderFitsTheVocabulary:
    """Every codeword the vocabulary can produce has to actually print."""

    def _longest_possible(self):
        return f"{max(MODIFIERS, key=len)}-{max(FLAT_NOUNS, key=len)}"

    def test_longest_codeword_fits_every_format(self):
        word = self._longest_possible()
        for a7 in (True, False):
            for with_auth in (True, False):
                size = g.fit_codeword_size(word, 9, a7, with_auth)
                assert size is not None, (word, a7, with_auth)
                assert size >= g.CODEWORD_MIN_FONT

    def test_a6_fits_the_longest_codeword_at_any_auth_size(self):
        word = self._longest_possible()
        for auth_size in range(3, 10):
            assert g.fit_codeword_size(word, 9, False, True, auth_size) == 9

    def test_a7_trades_auth_size_against_codeword_length(self):
        # A7 is the tight format: a bigger AUTH group eats the header room the
        # codeword shrinks into. Up to 6 the longest codeword still prints;
        # past that it is rejected outright, which is the right failure — an
        # illegible codeword is worse than a refusal at generation time.
        word = self._longest_possible()
        for auth_size in range(3, 7):
            assert g.fit_codeword_size(word, 9, True, True, auth_size) is not None
        for auth_size in range(7, 10):
            assert g.fit_codeword_size(word, 9, True, True, auth_size) is None

    def test_short_codewords_are_not_shrunk(self):
        assert g.fit_codeword_size("WALRUS", 9, a7=True) == 9
        assert g.fit_codeword_size("RUSTED-BADGER", 9, a7=False) == 9

    def test_long_codewords_shrink_on_a7(self):
        size = g.fit_codeword_size("RUSTED-BADGER", 9, a7=True)
        assert g.CODEWORD_MIN_FONT <= size < 9

    def test_returns_none_past_the_floor(self):
        assert g.fit_codeword_size("X" * 40, 9, a7=True) is None

    def test_max_fitted_len_exceeds_full_size_limit(self):
        # Shrinking is what buys room for two-word codewords.
        assert g.max_fitted_codeword_len(9, True, True) > g.max_codeword_len(9, True, True)

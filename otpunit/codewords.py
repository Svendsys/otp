"""Codeword selection for the print unit.

Three ways to land on a codeword, because they serve different moments:
roll one at random (the fast path), browse a category when you want to pick
the noun deliberately, or type one that was agreed elsewhere.

Everything here draws from otp_generator's CSPRNG helpers rather than the
`random` module -- the same source as the key material.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import otp_generator as gen

SEPARATOR = "-"


class Vocabulary:
    """The bundled modifier and noun lists, and the ways to draw from them."""

    def __init__(self, base_dir: str | None = None):
        self.modifiers, self.nouns_by_category = gen.load_vocabulary(base_dir)
        self.categories = list(self.nouns_by_category)
        self.all_nouns = [w for words in self.nouns_by_category.values() for w in words]

    @property
    def combinations(self) -> int:
        return len(self.modifiers) * len(self.all_nouns)

    @property
    def modifier_maxlen(self) -> int:
        """Longest modifier the bundled list can produce."""
        return max(len(w) for w in self.modifiers)

    @property
    def noun_maxlen(self) -> int:
        """Longest noun the bundled list can produce.

        Read from the lists rather than pinned to a constant: the two halves
        share a 17-character header, and the split between them (7 + 1 + 9
        today, so that BUTTERFLY and CROCODILE fit) is a decision made in
        codewords/build_lists.py. Typing a codeword in by hand should reach
        exactly as far as rolling one does.
        """
        return max(len(w) for w in self.all_nouns)

    def random(self) -> str:
        """A fresh <MODIFIER>-<NOUN>."""
        return join(gen.random_choice(self.modifiers), gen.random_choice(self.all_nouns))

    def random_modifier(self) -> str:
        return gen.random_choice(self.modifiers)

    def random_noun(self, category: str | None = None) -> str:
        pool = self.nouns_by_category[category] if category else self.all_nouns
        return gen.random_choice(pool)

    def nouns(self, category: str) -> list[str]:
        return self.nouns_by_category[category]


def join(modifier: str, noun: str) -> str:
    return f"{modifier}{SEPARATOR}{noun}"


def split(codeword: str) -> tuple[str, str]:
    modifier, _, noun = codeword.partition(SEPARATOR)
    return modifier, noun


def is_filename_safe(codeword: str) -> bool:
    """Codewords become PDF filenames; otp_generator enforces the same rule."""
    return bool(codeword) and all(ch.isalnum() or ch in "-_" for ch in codeword)


def validate(codeword: str, font_size: float, a7: bool, with_auth: bool,
             auth_size: int = gen.GROUP_SIZE) -> str | None:
    """
    Return None if `codeword` is usable, else a short reason fit for a
    128x64 panel. Called as the operator types, so it must be cheap.
    """
    if not codeword:
        return "EMPTY"
    if not is_filename_safe(codeword):
        return "BAD CHARACTERS"
    if gen.fit_codeword_size(codeword, font_size, a7, with_auth, auth_size) is None:
        limit = gen.max_fitted_codeword_len(font_size, a7, with_auth, auth_size)
        return f"TOO LONG (MAX {limit})"
    return None

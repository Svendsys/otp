"""Codeword selection for the print unit.

Two ways to land on a codeword: roll one, or type one that was agreed
elsewhere. There is deliberately no way to pick a word off the lists.

A codeword names a pad set without naming its holders, and an operator who
chooses one puts their own taste into that name. Taste is stable, so it is a
fingerprint: pads picked by the same hand end up looking like a set, and a
category picked to suit a particular contact is worse still. Rolling draws
uniformly over every modifier-noun pair; a human picking a noun does not.
So the categories below are an authoring device -- they are how the lists
are curated and how build_lists.py spends its phonetic budget -- and nothing
in the running unit narrows a draw to one of them.

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
        modifiers, nouns_by_category = gen.load_vocabulary(base_dir)
        self.modifiers = modifiers
        # Flattened on the way in, and the per-category lists are not kept:
        # the unit has no reason to hold a handle it could draw from.
        self.all_nouns = [w for words in nouns_by_category.values() for w in words]

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
        """A fresh <MODIFIER>-<NOUN>, uniform over every pair.

        The only draw there is. Reroll until you like one -- each roll is
        independent and costs nothing, so an operator never has to reach for
        a narrower pool to get a codeword they can remember.
        """
        return join(gen.random_choice(self.modifiers), gen.random_choice(self.all_nouns))


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

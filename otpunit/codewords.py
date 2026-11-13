"""Codeword selection for the print unit.

Two ways to land on a codeword: roll one, or type one that was agreed
elsewhere. There is deliberately no way to pick a word off the lists.

A codeword names a pad set without naming its holders. An operator who
chooses one puts their own taste into it, and taste is a fingerprint;
rolling draws uniformly over every modifier-noun pair. The noun categories
are an authoring device for curating the lists, and nothing in the running
unit narrows a draw to one of them.

Everything here draws from otp_generator's CSPRNG helpers, not the `random`
module: the same source as the key material.
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
        # Flattened on the way in; the per-category lists are not kept, so
        # the unit has nothing narrower to draw from.
        self.all_nouns = [w for words in nouns_by_category.values() for w in words]
        # Every codeword this power-on has produced, so no two pads printed
        # in one sitting share a name. Held in RAM only. It is deliberately
        # not written to the card, even hashed: the whole space is small
        # enough to enumerate, so a stored list would reveal which pads
        # this machine made, which is what a codeword exists to hide.
        self._issued: set[str] = set()

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

        Read from the lists, not a constant, so typing a codeword in by
        hand reaches exactly as far as rolling one does.
        """
        return max(len(w) for w in self.all_nouns)

    def random(self) -> str:
        """A fresh <MODIFIER>-<NOUN>, never one already issued this session.

        Sampling is without replacement across the power-on session, so two
        pads printed in one sitting cannot carry the same name. Rejected
        rolls count as issued too, so a reroll never shows the same
        codeword twice. Each draw is uniform over what is left.
        """
        if len(self._issued) >= self.combinations:
            raise ValueError("every codeword has been issued this session")
        while True:
            word = join(gen.random_choice(self.modifiers),
                        gen.random_choice(self.all_nouns))
            if word not in self._issued:
                self._issued.add(word)
                return word


def join(modifier: str, noun: str) -> str:
    return f"{modifier}{SEPARATOR}{noun}"


def split(codeword: str) -> tuple[str, str]:
    modifier, _, noun = codeword.partition(SEPARATOR)
    return modifier, noun


def is_filename_safe(codeword: str) -> bool:
    """
    Codewords become PDF filenames; otp_generator enforces the same rule.

    ASCII only: str.isalnum() is true for every Unicode letter, but the
    Courier on the page is a WinAnsi font and substitutes silently.
    """
    return bool(codeword) and all(
        (ch.isascii() and ch.isalnum()) or ch in "-_" for ch in codeword)


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

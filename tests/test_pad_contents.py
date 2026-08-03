"""Tests that read the key material off a rendered pad page.

Mutation testing found the suite's largest blind spot: nothing anywhere read
a pad's body text. The entire key body could be replaced with the letter A,
or os.urandom swapped for the `random` module's Mersenne Twister, and all
218 tests still passed. Every existing randomness test calls
generate_random_letters directly and never checks that its output is what
lands on the page.

So these tests extract the printed letters from real PDFs and assert on
them. They are the ones that would notice if the unit started printing
predictable pads.
"""
import collections
import io
import re
import string
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as g

GROUP = re.compile(r"^[A-Z]{5}$")


def glyph_rows(data: bytes, page: int = 0):
    """Text rows of a rendered page, reconstructed from glyph coordinates."""
    pypdf = pytest.importorskip("pypdf")
    cells = []

    def visit(text, cm, tm, font_dict, font_size):
        stripped = text.strip()
        if stripped:
            cells.append((round(tm[5], 1), tm[4], stripped))

    pypdf.PdfReader(io.BytesIO(data)).pages[page].extract_text(visitor_text=visit)
    rows = collections.defaultdict(list)
    for y, x, s in cells:
        rows[y].append((x, s))
    return [[s for _, s in sorted(items)]
            for _, items in sorted(rows.items(), key=lambda kv: -kv[0])]


SEGMENT = re.compile(
    r"([-\d.]+)\s+([-\d.]+)\s+m\s+([-\d.]+)\s+([-\d.]+)\s+l\b")


def line_segments(data: bytes, page: int = 0) -> set[tuple]:
    """
    Straight strokes on a page, as (x0, y0, x1, y1) in points.

    Only single-segment `m ... l` strokes, which is all the crop marks and
    rules are. Coordinates come straight out of the content stream, so this
    sees what the printer sees rather than what the code meant to draw.
    """
    pypdf = pytest.importorskip("pypdf")
    content = pypdf.PdfReader(io.BytesIO(data)).pages[page] \
        .get_contents().get_data().decode("latin-1")
    return {tuple(float(n) for n in match)
            for match in SEGMENT.findall(content)}


def segments_match(a, b, tolerance: float = 0.05) -> bool:
    """Same segment, drawn from either end, within rounding."""
    reversed_b = (b[2], b[3], b[0], b[1])
    return (all(abs(p - q) <= tolerance for p, q in zip(a, b))
            or all(abs(p - q) <= tolerance for p, q in zip(a, reversed_b)))


def key_groups(data: bytes, page: int = 0) -> list[str]:
    """The five-letter key groups printed on a pad page."""
    out = []
    for row in glyph_rows(data, page):
        # Header and footer rows are not runs of bare five-letter groups.
        if len(row) >= 2 and all(GROUP.match(cell) for cell in row):
            out.extend(row)
    return out


def all_key_groups(data: bytes, pages: int) -> list[str]:
    out = []
    for page in range(pages):
        out.extend(key_groups(data, page))
    return out


def a6_pad(pages=2, **kwargs) -> bytes:
    sink = io.BytesIO()
    g.generate_set_pdf_a6(sink, kwargs.pop("codeword", "RUSTED-BADGER"), pages,
                          665, 9, progress=lambda d, t: None, **kwargs)
    return sink.getvalue()


class TestTheKeyBodyIsRealKeyMaterial:
    """
    The body printed on the page must be the CSPRNG's output.

    Every one of these fails if the body is constant, repeats a group, or is
    drawn from a predictable source -- the mutations that previously slipped
    through the whole suite untouched.
    """

    def test_a_page_carries_the_expected_number_of_groups(self):
        groups = key_groups(a6_pad(1))
        assert len(groups) == 665 // g.GROUP_SIZE == 133

    def test_the_body_is_not_constant(self):
        groups = key_groups(a6_pad(1))
        assert len(set(groups)) > 120, "a page of 133 groups must not repeat"

    def test_every_letter_of_the_alphabet_appears_across_a_pad(self):
        letters = "".join(all_key_groups(a6_pad(4), 4))
        assert set(letters) == set(string.ascii_uppercase)

    def test_the_printed_letters_are_roughly_uniform(self):
        # 8 pages is 5320 letters, ~205 per letter with sigma ~14, so a 40%
        # band is about 14 sigma -- it cannot flake, while a constant body, a
        # repeated group or a skewed mapping all fail it outright.
        letters = "".join(all_key_groups(a6_pad(8), 8))
        assert len(letters) == 8 * 665
        counts = collections.Counter(letters)
        expected = len(letters) / 26
        for letter in string.ascii_uppercase:
            assert 0.6 * expected < counts[letter] < 1.4 * expected, (letter, counts[letter])

    def test_two_pages_of_one_pad_differ(self):
        data = a6_pad(2)
        assert key_groups(data, 0) != key_groups(data, 1)

    def test_the_body_comes_from_os_urandom(self):
        """
        Structural, not statistical. A Mersenne Twister is uniform too, so
        no distribution test can tell the difference -- the only way to
        catch a swapped source is to watch os.urandom itself.
        """
        import os as os_mod
        calls = []
        real = os_mod.urandom
        try:
            os_mod.urandom = lambda n: calls.append(n) or real(n)
            a6_pad(1)
        finally:
            os_mod.urandom = real
        assert calls, "pad generation must draw from os.urandom"
        assert sum(calls) >= 665, "and draw enough of it for the page"


class TestTheAuthGroupIsNeverPartOfTheBody:
    """otp.md: the AUTH group is "generated alongside the key body but never
    part of it". Reusing body letters would leak the authenticator to anyone
    holding a ciphertext."""

    def _auth_and_body(self, data, page=0):
        auth = None
        for row in glyph_rows(data, page):
            joined = "".join(row)
            match = re.search(r"AUTH\s*([A-Z]+)", joined)
            if match:
                auth = match.group(1)
                break
        return auth, key_groups(data, page)

    def test_the_auth_group_is_printed(self):
        auth, _ = self._auth_and_body(a6_pad(1))
        assert auth and len(auth) == g.GROUP_SIZE

    def test_the_auth_group_is_not_the_first_body_group(self):
        for _ in range(5):
            auth, body = self._auth_and_body(a6_pad(1))
            assert auth != body[0]

    def test_the_auth_group_does_not_appear_in_the_body(self):
        # Not a proof -- a 1-in-11.9M coincidence exists -- but it fails
        # instantly if AUTH is sliced out of the body rather than drawn extra.
        collisions = 0
        for _ in range(5):
            auth, body = self._auth_and_body(a6_pad(1))
            if auth in body:
                collisions += 1
        assert collisions == 0


class TestCodewordIsScrubbedFromEveryLayout:
    """
    The metadata leak previously covered only the A4 path.

    jobs.py picks the builder from the paper setting, so A6 and A7 are live
    code -- putting the codeword back in either one's title went unnoticed.
    """

    def _build(self, builder, **kwargs):
        sink = io.BytesIO()
        builder(sink, "RUSTED-BADGER", 2, kwargs.pop("chars", 665), 9,
                progress=lambda d, t: None, **kwargs)
        return sink.getvalue()

    def test_no_layout_leaks_the_codeword(self):
        for builder, kwargs in (
            (g.generate_set_pdf_a6, {}),
            (g.generate_set_pdf_a7, {"chars": 375}),
            (g.generate_set_pdf_a4, {}),
            (g.generate_set_pdf_a4, {"page_size": g.LETTER}),
        ):
            data = self._build(builder, **kwargs)
            assert b"RUSTED" not in data, builder.__name__
            assert b"BADGER" not in data, builder.__name__

    def test_no_layout_is_dated(self):
        for builder, kwargs in ((g.generate_set_pdf_a6, {}),
                                (g.generate_set_pdf_a7, {"chars": 375}),
                                (g.generate_set_pdf_a4, {})):
            data = self._build(builder, **kwargs)
            assert b"D:2000" in data, builder.__name__

    def test_the_codeword_is_still_on_the_page(self):
        # It is scrubbed from the metadata, not from the print.
        rows = glyph_rows(self._build(g.generate_set_pdf_a6))
        assert any("RUSTED-BADGER" in "".join(row) for row in rows)


class TestTrainingIsUnmistakable:
    """otp.md requires practice material to be unmistakably marked. Four
    independent edits could each remove the marking with no test failing."""

    def _training_marks(self, data) -> int:
        text = "\n".join("".join(r) for r in glyph_rows(data))
        return text.count("TRAINING")

    def test_both_the_watermark_and_the_footer_are_printed(self):
        # Two distinct marks: the large diagonal watermark and the footer
        # line. Counting them matters -- asserting only that the word
        # appears somewhere cannot tell the two apart, so removing the
        # watermark alone would go unnoticed.
        assert self._training_marks(a6_pad(1, training=True)) == 2

    def test_the_watermark_is_large_and_diagonal(self):
        pypdf = pytest.importorskip("pypdf")
        content = pypdf.PdfReader(io.BytesIO(a6_pad(1, training=True))).pages[0] \
            .get_contents().get_data()
        assert b"cm" in content, "a rotated watermark needs a transform"
        # The watermark font is box_width/9 ~ 33pt against a 9pt body.
        sizes = [float(m) for m in re.findall(rb"([\d.]+) Tf", content)]
        assert sizes and max(sizes) > 20, sizes

    def test_live_pads_carry_no_training_mark(self):
        assert self._training_marks(a6_pad(1)) == 0


class TestLayoutsDrawWhatTheyPromise:
    def test_a7_draws_both_halves_of_the_sheet(self):
        # Drawing only the left half would silently drop every even page of
        # the pad; the progress counter would still report them.
        sink = io.BytesIO()
        g.generate_set_pdf_a7(sink, "RUSTED-BADGER", 2, 375, 9,
                              progress=lambda d, t: None)
        rows = glyph_rows(sink.getvalue())
        numbers = re.findall(r"\d{4}", "".join("".join(r) for r in rows))
        assert "0001" in numbers and "0002" in numbers

    @pytest.mark.parametrize("page_size", [g.A4, g.LETTER])
    def test_a4_sheets_carry_crop_marks(self, page_size):
        """
        The marks the guillotine is lined up against.

        This assertion used to count " l\\n" -- PDF lineto -- across the whole
        page and require four. The page furniture already emits four on its
        own, so deleting every crop mark left the count at four and the test
        passed: it never tested the thing it was named for. A mutation that
        removed the call survived the entire suite and was committed. So
        check the geometry instead: each mark at its own edge, the right
        length, on the cut line.
        """
        sink = io.BytesIO()
        g.generate_set_pdf_a4(sink, "RUSTED-BADGER", 4, 665, 9,
                              page_size=page_size, progress=lambda d, t: None)
        page_w, page_h = page_size
        inset, tick = g.CROP_INSET, g.CROP_TICK
        expected = {
            # Vertical cut, marked near the bottom and top edges.
            (page_w / 2, inset, page_w / 2, inset + tick),
            (page_w / 2, page_h - inset - tick, page_w / 2, page_h - inset),
            # Horizontal cut, marked near the left and right edges.
            (inset, page_h / 2, inset + tick, page_h / 2),
            (page_w - inset - tick, page_h / 2, page_w - inset, page_h / 2),
        }
        drawn = line_segments(sink.getvalue())
        for mark in expected:
            assert any(segments_match(mark, seg) for seg in drawn), (
                f"crop mark {mark} missing from {sorted(drawn)}")

    def test_a7_sheets_carry_a_cut_line(self):
        """
        The line the A7 sheet is cut down.

        Same flaw as the crop marks above: this used to look for the PDF `d`
        operator, but setDash emits that whether or not a line follows, so
        deleting the stroke left the test green. Check the stroke itself --
        full height, down the middle of the landscape A6 sheet.
        """
        sink = io.BytesIO()
        g.generate_set_pdf_a7(sink, "RUSTED-BADGER", 2, 375, 9,
                              progress=lambda d, t: None)
        # Two A7 pages side by side, so the sheet is A6 turned on its side.
        sheet_w, sheet_h = g.A6[1], g.A6[0]
        cut = (sheet_w / 2, 0.0, sheet_w / 2, sheet_h)
        drawn = line_segments(sink.getvalue())
        assert any(segments_match(cut, seg) for seg in drawn), (
            f"cut line {cut} missing from {sorted(drawn)}")


class TestGenerationReallyWritesNoFile:
    def test_no_file_appears_anywhere_under_tmp(self, tmp_path, monkeypatch):
        """
        The previous version of this test set TMPDIR and watched tmp_path,
        but tempfile caches its directory at first use, so the environment
        variable was inert and a NamedTemporaryFile would have gone to the
        real /tmp unnoticed. Point tempfile itself at the sandbox and watch
        that, so a stray temp file has nowhere to hide.
        """
        import tempfile
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        before = set(tmp_path.rglob("*"))

        data = a6_pad(3)

        assert data[:5] == b"%PDF-"
        assert set(tmp_path.rglob("*")) == before
        assert tempfile.gettempdir() == str(tmp_path), "sandbox must be in effect"

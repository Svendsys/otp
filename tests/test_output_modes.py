"""Tests for in-memory generation, cancellation, and the new print jobs.

The load-bearing test here is test_nothing_touches_the_filesystem. The print
unit generates key material straight into RAM and pipes it to the printer so
that no pad ever becomes a file; if anyone reintroduces a temporary file, this
must fail loudly.
"""
import collections
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as g

SCRIPT = str(REPO / "otp_generator.py")
MANUAL = (REPO / "otp.md").read_text(encoding="utf-8")


def run(args, cwd):
    return subprocess.run(
        [sys.executable, SCRIPT, *args], cwd=cwd, capture_output=True
    )


def grid_from_pdf(data: bytes):
    """Reconstruct laid-out text rows from a PDF, by glyph coordinates."""
    pypdf = pytest.importorskip("pypdf")
    cells = []

    def visit(text, cm, tm, font_dict, font_size):
        stripped = text.strip()
        if stripped:
            cells.append((round(tm[5], 1), tm[4], stripped))

    pypdf.PdfReader(io.BytesIO(data)).pages[0].extract_text(visitor_text=visit)
    rows = collections.defaultdict(list)
    for y, x, s in cells:
        rows[y].append((x, s))
    return [
        "".join(s for _, s in sorted(items))
        for _, items in sorted(rows.items(), key=lambda kv: -kv[0])
    ]


class TestInMemoryGeneration:
    def test_a6_set_into_a_buffer(self):
        buf = io.BytesIO()
        g.generate_set_pdf_a6(buf, "RUSTED-BADGER", 3, 665, 9, progress=lambda d, t: None)
        assert buf.getvalue()[:5] == b"%PDF-"

    def test_a7_set_into_a_buffer(self):
        buf = io.BytesIO()
        g.generate_set_pdf_a7(buf, "RUSTED-BADGER", 4, 375, 9, progress=lambda d, t: None)
        assert buf.getvalue()[:5] == b"%PDF-"

    def test_worksheets_and_tabula_into_a_buffer(self):
        for fn, args in ((g.generate_worksheets_pdf, (2,)), (g.generate_tabula_recta_pdf, ())):
            buf = io.BytesIO()
            fn(buf, *args)
            assert buf.getvalue()[:5] == b"%PDF-"

    def test_nothing_touches_the_filesystem(self, tmp_path, monkeypatch):
        """A pad generated into RAM must leave no trace on any filesystem."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        before = set(tmp_path.rglob("*"))

        buf = io.BytesIO()
        g.generate_set_pdf_a6(buf, "SILENT-OSPREY", 5, 665, 9, progress=lambda d, t: None)

        assert buf.getvalue()[:5] == b"%PDF-"
        assert set(tmp_path.rglob("*")) == before, "generation must not create files"

    def test_two_runs_never_share_key_material(self):
        """
        Regenerating is not a way to reprint a pad.

        The pair must come from one generation submitted twice; two runs
        under one codeword produce two different pads that look identical
        from the outside and fail authentication on first use.
        """
        first, second = io.BytesIO(), io.BytesIO()
        for sink in (first, second):
            g.generate_set_pdf_a6(sink, "FROZEN-LANTERN", 3, 665, 9,
                                  progress=lambda d, t: None)
        assert first.getvalue() != second.getvalue()


def page_numbers_by_slot(data: bytes):
    """Per sheet, the four corner page numbers as (TL, TR, BL, BR)."""
    pypdf = pytest.importorskip("pypdf")
    sheets = []
    for page in pypdf.PdfReader(io.BytesIO(data)).pages:
        found = []

        def visit(text, cm, tm, font_dict, font_size, _found=found):
            stripped = text.strip()
            if stripped.isdigit() and len(stripped) == 4:
                _found.append((round(tm[5]), round(tm[4]), int(stripped)))

        page.extract_text(visitor_text=visit)
        # Sort top row first, then left to right within each row.
        ordered = sorted(found, key=lambda t: (-t[0], t[1]))
        sheets.append([n for _, _, n in ordered])
    return sheets


class TestA4Imposition:
    """
    Four A6 pad pages per sheet, imposed for a guillotine.

    The stack is cut twice, leaving four piles. Cut-and-stack order means
    each pile is already sequential, so the pad is assembled by piling them
    up. Reading order would force a page-by-page interleave instead, and
    getting this wrong is silent -- the pages all print, just in an order
    that makes the pad useless.
    """

    def _impose(self, pages, **kwargs):
        buf = io.BytesIO()
        g.generate_set_pdf_a4(buf, "RUSTED-BADGER", pages, 665, 9,
                              progress=lambda d, t: None, **kwargs)
        return buf.getvalue()

    def test_four_pad_pages_per_sheet(self):
        assert len(page_numbers_by_slot(self._impose(12))) == 3
        assert len(page_numbers_by_slot(self._impose(13))) == 4

    def test_each_pile_is_sequential_after_cutting(self):
        sheets = page_numbers_by_slot(self._impose(12))
        piles = list(zip(*sheets))
        assert piles == [(1, 2, 3), (4, 5, 6), (7, 8, 9), (10, 11, 12)]

    def test_stacking_the_piles_gives_the_whole_pad_in_order(self):
        sheets = page_numbers_by_slot(self._impose(20))
        assembled = [page for pile in zip(*sheets) for page in pile]
        assert assembled == list(range(1, 21))

    def test_short_last_sheet_leaves_gaps_not_wrong_pages(self):
        sheets = page_numbers_by_slot(self._impose(6))
        seen = sorted(n for sheet in sheets for n in sheet)
        assert seen == [1, 2, 3, 4, 5, 6]

    def test_letter_paper_uses_letter_geometry(self):
        pypdf = pytest.importorskip("pypdf")
        from reportlab.lib.pagesizes import LETTER

        data = self._impose(4, page_size=LETTER)
        box = pypdf.PdfReader(io.BytesIO(data)).pages[0].mediabox
        assert round(float(box.width)) == round(LETTER[0])

    def test_generates_into_memory_only(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        before = set(tmp_path.rglob("*"))
        assert self._impose(8)[:5] == b"%PDF-"
        assert set(tmp_path.rglob("*")) == before

    def test_cancel_aborts_imposed_generation(self):
        with pytest.raises(g.GenerationCancelled):
            self._impose(400, should_cancel=lambda: True)


class TestProgressAndCancel:
    def test_progress_reports_every_page(self):
        seen = []
        g.generate_set_pdf_a6(io.BytesIO(), "WALRUS", 5, 665, 9,
                              progress=lambda done, total: seen.append((done, total)))
        assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]

    def test_a7_progress_counts_pad_pages_not_sheets(self):
        seen = []
        g.generate_set_pdf_a7(io.BytesIO(), "WALRUS", 5, 375, 9,
                              progress=lambda done, total: seen.append(done))
        assert seen == [2, 4, 5]

    def test_cancel_aborts_generation(self):
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 3

        with pytest.raises(g.GenerationCancelled):
            g.generate_set_pdf_a6(io.BytesIO(), "WALRUS", 100, 665, 9,
                                  progress=lambda d, t: None, should_cancel=cancel)

    def test_no_cancel_callback_runs_to_completion(self):
        buf = io.BytesIO()
        g.generate_set_pdf_a6(buf, "WALRUS", 2, 665, 9, progress=lambda d, t: None)
        assert buf.getvalue()[:5] == b"%PDF-"


class TestTabulaRecta:
    """The card and the manual must agree; the manual is already verified."""

    def _manual_rows(self):
        rows = re.findall(r"^([A-Z]) \| ([A-Z](?: [A-Z]){25})$", MANUAL, re.M)
        return {key: row.replace(" ", "") for key, row in rows}

    def test_card_matches_the_manuals_table(self):
        buf = io.BytesIO()
        g.generate_tabula_recta_pdf(buf)
        rows = grid_from_pdf(buf.getvalue())

        manual = self._manual_rows()
        assert len(manual) == 26

        body = [r for r in rows if len(r) == 27 and r[0] == r[1]]
        assert len(body) == 26, f"expected 26 shift alphabets, got {len(body)}"
        for row in body:
            key_letter, alphabet = row[0], row[1:]
            assert alphabet == manual[key_letter], key_letter

    def test_header_row_is_the_plain_alphabet(self):
        buf = io.BytesIO()
        g.generate_tabula_recta_pdf(buf)
        rows = grid_from_pdf(buf.getvalue())
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in rows

    def test_copies_produce_multiple_pages(self):
        pypdf = pytest.importorskip("pypdf")
        buf = io.BytesIO()
        g.generate_tabula_recta_pdf(buf, copies=3)
        assert len(pypdf.PdfReader(io.BytesIO(buf.getvalue())).pages) == 3


class TestNewCliFlags:
    def test_tabula_only(self, tmp_path):
        r = run(["--tabula", "2", "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert (tmp_path / "TABULA_RECTA.pdf").read_bytes()[:5] == b"%PDF-"
        assert b"no key material" in r.stdout

    def test_stdout_emits_pdf_and_keeps_chatter_on_stderr(self, tmp_path):
        r = run(["--tabula", "1", "--stdout"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout[:5] == b"%PDF-"
        assert b"DONE" in r.stderr
        assert not list(tmp_path.iterdir()), "--stdout must not write files"

    def test_stdout_pad_set_writes_no_file(self, tmp_path):
        r = run(["--random-codewords", "1", "--pages", "2", "--stdout"], cwd=tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout[:5] == b"%PDF-"
        assert not list(tmp_path.iterdir())

    def test_stdout_refuses_multiple_outputs(self, tmp_path):
        r = run(["--tabula", "1", "--worksheets", "1", "--stdout"], cwd=tmp_path)
        assert r.returncode != 0
        assert b"single PDF" in r.stderr

    def test_random_codewords(self, tmp_path):
        r = run(["--random-codewords", "2", "--pages", "2",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        pdfs = sorted(p.name for p in tmp_path.glob("*.pdf"))
        assert len(pdfs) == 2
        for name in pdfs:
            assert re.fullmatch(r"[A-Z]+-[A-Z]+\.pdf", name), name

    def test_a4_flag(self, tmp_path):
        r = run(["--random-codewords", "1", "--pages", "8", "--a4",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert b"4-up on A4" in r.stdout
        pdf = next(tmp_path.glob("*.pdf")).read_bytes()
        assert len(page_numbers_by_slot(pdf)) == 2

    def test_a4_and_a7_conflict(self, tmp_path):
        r = run(["--random-codewords", "1", "--a4", "--a7"], cwd=tmp_path)
        assert r.returncode != 0
        assert b"cannot be combined" in r.stderr

    def test_letter_rejects_a_layout_that_would_overflow_the_quarter(self, tmp_path):
        # A Letter quarter is 139.7mm tall against A6's 148mm. Measuring
        # against A6 let the last key rows fall below the guillotine line.
        r = run(["--random-codewords", "1", "--pages", "4", "--chars", "1225",
                 "--fontsize", "10", "--letter", "--output", str(tmp_path)],
                cwd=tmp_path)
        assert r.returncode != 0
        assert b"won't fit" in r.stdout

    def test_letter_and_a4_defaults_still_fit(self, tmp_path):
        for flag in ("--a4", "--letter"):
            r = run(["--random-codewords", "1", "--pages", "4", flag,
                     "--output", str(tmp_path)], cwd=tmp_path)
            assert r.returncode == 0, r.stdout + r.stderr

    def test_font_too_wide_is_rejected(self, tmp_path):
        # calc_max_chars only measures height. Above ~13pt on A6 the
        # inter-group gap goes negative and the five-letter groups print
        # superimposed -- inside the content box, so nothing overflows
        # visibly and both copies of the pair are equally unreadable.
        r = run(["--random-codewords", "1", "--pages", "1", "--fontsize", "14",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert b"would overlap" in r.stdout

    def test_the_largest_usable_font_is_still_accepted(self, tmp_path):
        r = run(["--random-codewords", "1", "--pages", "1", "--fontsize", "13",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_groups_never_overlap_at_any_accepted_font_size(self):
        from reportlab.pdfbase.pdfmetrics import stringWidth
        for a7 in (False, True):
            limit = g.max_body_font_size(a7)
            groups = g.A7_GROUPS_PER_ROW if a7 else g.A6_GROUPS_PER_ROW
            width = (g.SHEET_HEIGHT / 2) if a7 else g.SHEET_WIDTH
            content = width - 2 * g.HEADER_MARGIN_H
            used = stringWidth("X" * g.GROUP_SIZE, "Courier", limit) * groups
            assert used <= content + 1e-6, (a7, used, content)

    def test_chars_must_be_positive(self, tmp_path):
        # A negative value slices the key string from the end, producing a
        # blank pad and a silently shortened AUTH group.
        for bad in ("-500", "0"):
            r = run(["--random-codewords", "1", "--pages", "1", "--chars", bad,
                     "--output", str(tmp_path)], cwd=tmp_path)
            assert r.returncode != 0, bad
            assert b"--chars must be at least 1" in r.stderr

    def test_non_ascii_codewords_are_rejected(self, tmp_path):
        # str.isalnum() accepts every Unicode letter, but Courier is a
        # WinAnsi font: two distinct codewords would print one header.
        words = tmp_path / "w.txt"
        words.write_text("中文-BADGER\n", encoding="utf-8")
        r = run(["--codewords", str(words), "--sets", "1", "--pages", "1",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert b"unsafe as a filename" in r.stdout

    def test_a4_and_letter_conflict(self, tmp_path):
        r = run(["--random-codewords", "1", "--a4", "--letter"], cwd=tmp_path)
        assert r.returncode != 0
        assert b"not both" in r.stderr

    def test_random_codewords_conflicts_with_codewords_file(self, tmp_path):
        words = tmp_path / "w.txt"
        words.write_text("WALRUS\n")
        r = run(["--codewords", str(words), "--random-codewords", "1"], cwd=tmp_path)
        assert r.returncode != 0
        assert b"not both" in r.stderr

    def test_auth_size_changes_the_header(self, tmp_path):
        r = run(["--random-codewords", "1", "--pages", "1", "--auth-size", "8",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        pdf = next(tmp_path.glob("*.pdf")).read_bytes()
        rows = grid_from_pdf(pdf)
        assert any(re.search(r"AUTH ([A-Z]{8})\d{4}$", r) for r in rows), rows[:3]

    def test_auth_size_must_be_positive(self, tmp_path):
        r = run(["--tabula", "1", "--auth-size", "0", "--output", str(tmp_path)],
                cwd=tmp_path)
        assert r.returncode != 0
        assert b"--auth-size must be at least 1" in r.stderr

    def test_nothing_to_do_mentions_tabula(self, tmp_path):
        r = run(["--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert b"--tabula" in r.stderr

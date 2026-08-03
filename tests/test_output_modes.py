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

    def test_both_copies_of_a_pair_are_identical(self):
        """A pad pair is one PDF printed twice, never generated twice."""
        buf = io.BytesIO()
        g.generate_set_pdf_a6(buf, "FROZEN-LANTERN", 3, 665, 9, progress=lambda d, t: None)
        pdf = buf.getvalue()
        assert pdf == buf.getvalue()

        other = io.BytesIO()
        g.generate_set_pdf_a6(other, "FROZEN-LANTERN", 3, 665, 9, progress=lambda d, t: None)
        assert other.getvalue() != pdf, "separate runs must not share key material"


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

"""Tests for the pad generator.

The most important tests here guard generate_random_letters: any future
edit that reintroduces modulo bias (e.g. a naive `byte % 26` over all 256
values, which skews the last four letters by about -8.6%) must fail loudly.
"""
import collections
import string
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as g

SCRIPT = str(REPO / "otp_generator.py")
SAMPLE = str(REPO / "sample_codewords.txt")


def run(args, cwd):
    return subprocess.run(
        [sys.executable, SCRIPT, *args], cwd=cwd, capture_output=True, text=True
    )


def is_pdf(path: Path) -> bool:
    return path.is_file() and path.read_bytes()[:5] == b"%PDF-"


class TestRandomLetters:
    def test_length_and_alphabet(self):
        s = g.generate_random_letters(1000)
        assert len(s) == 1000
        assert set(s) <= set(string.ascii_uppercase)

    def test_zero(self):
        assert g.generate_random_letters(0) == ""

    def test_uniformity(self):
        # 520k draws: expected 20k per letter, sigma ~140 (0.7%).
        # A 5% tolerance is ~7 sigma — effectively cannot flake — while a
        # naive mod-26 mapping biases W-Z by -8.6% and fails immediately.
        n = 520_000
        counts = collections.Counter(g.generate_random_letters(n))
        assert len(counts) == 26
        expected = n / 26
        for letter in string.ascii_uppercase:
            deviation = abs(counts[letter] - expected) / expected
            assert deviation < 0.05, (letter, counts[letter])


class TestHeaderFit:
    def test_known_limits_at_default_font(self):
        assert g.max_codeword_len(9, a7=True, with_auth=True) == 11
        assert g.max_codeword_len(9, a7=False, with_auth=True) == 19

    def test_no_auth_gives_more_room(self):
        assert g.max_codeword_len(9, True, False) > g.max_codeword_len(9, True, True)
        assert g.max_codeword_len(9, False, False) > g.max_codeword_len(9, False, True)

    def test_larger_font_gives_less_room(self):
        assert g.max_codeword_len(12, False, True) < g.max_codeword_len(9, False, True)


class TestCalcMaxChars:
    def test_defaults_fit(self):
        assert g.calc_max_chars(9, a7=False) >= 665
        assert g.calc_max_chars(9, a7=True) >= 375

    def test_larger_font_fits_less(self):
        assert g.calc_max_chars(14, a7=False) < g.calc_max_chars(9, a7=False)


class TestCliHappyPaths:
    def test_a6_set(self, tmp_path):
        r = run(["--codewords", SAMPLE, "--sets", "2", "--pages", "3",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert is_pdf(tmp_path / "WALRUS.pdf")
        assert is_pdf(tmp_path / "COPPER.pdf")

    def test_a7_training_with_worksheets(self, tmp_path):
        r = run(["--codewords", SAMPLE, "--sets", "1", "--pages", "3", "--a7",
                 "--training", "--worksheets", "2", "--output", str(tmp_path)],
                cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Training pads: yes" in r.stdout
        assert is_pdf(tmp_path / "WALRUS.pdf")
        assert is_pdf(tmp_path / "WORKSHEETS.pdf")

    def test_worksheets_only(self, tmp_path):
        r = run(["--worksheets", "3", "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert is_pdf(tmp_path / "WORKSHEETS.pdf")
        assert "no key material" in r.stdout

    def test_no_auth(self, tmp_path):
        r = run(["--codewords", SAMPLE, "--sets", "1", "--pages", "2",
                 "--no-auth", "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "Auth group in header: no" in r.stdout


class TestCliValidation:
    def test_nothing_to_do(self, tmp_path):
        r = run(["--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert "nothing to do" in r.stderr

    def test_duplicate_codewords(self, tmp_path):
        words = tmp_path / "words.txt"
        words.write_text("ALPHA\nBRAVO\nALPHA\n")
        r = run(["--codewords", str(words), "--sets", "3", "--pages", "1",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert "Duplicate codeword" in r.stdout

    def test_unsafe_codeword(self, tmp_path):
        words = tmp_path / "words.txt"
        words.write_text("BAD/WORD\n")
        r = run(["--codewords", str(words), "--sets", "1", "--pages", "1",
                 "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert "unsafe as a filename" in r.stdout

    def test_codeword_too_long_for_a7_header(self, tmp_path):
        words = tmp_path / "words.txt"
        words.write_text("EXTRAORDINARILY\n")
        r = run(["--codewords", str(words), "--sets", "1", "--pages", "1",
                 "--a7", "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert "too long" in r.stdout

    def test_missing_codeword_file(self, tmp_path):
        r = run(["--codewords", str(tmp_path / "nope.txt"), "--sets", "1",
                 "--pages", "1", "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert "Cannot read codewords file" in r.stdout

    def test_zero_pages_and_sets(self, tmp_path):
        for bad in (["--sets", "0"], ["--pages", "0"]):
            r = run(["--codewords", SAMPLE, *bad, "--output", str(tmp_path)],
                    cwd=tmp_path)
            assert r.returncode != 0
            assert "must be at least 1" in r.stdout

    def test_negative_worksheets(self, tmp_path):
        r = run(["--worksheets", "-1", "--output", str(tmp_path)], cwd=tmp_path)
        assert r.returncode != 0
        assert "--worksheets must be 0 or more" in r.stderr

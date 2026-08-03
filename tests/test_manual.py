"""Verify every worked number in otp.md.

The manual is a teaching document: a single wrong digit in a printed
example costs a classroom an afternoon. These tests recompute each
example, table, and exercise directly from the file text, so any future
edit that breaks the arithmetic fails CI.
"""
import re
from pathlib import Path

MD = (Path(__file__).resolve().parent.parent / "otp.md").read_text()


def encrypt(msg, key):
    return "".join(chr(65 + ((ord(m) + ord(k) - 130) % 26)) for m, k in zip(msg, key))


def decrypt(ct, key):
    return "".join(chr(65 + ((ord(c) - ord(k)) % 26)) for c, k in zip(ct, key))


def degroup(s):
    return s.replace(" ", "")


def find(pattern):
    m = re.search(pattern, MD)
    assert m, f"pattern not found in otp.md: {pattern}"
    return m.group(1)


KEY = "FEKXETQSIFCBIAHNZENEUURWQ"
MSG = "ACTIVATENAUGHTFIREXXXXXXX"


class TestWorkedExample:
    def test_key_page_as_printed(self):
        assert degroup(find(r"\n(FEKXE TQSIF CBIAH NZENE UURWQ)")) == KEY

    def test_number_conversions(self):
        key_row = find(r"\n(05 04 10 23 04 19 16 18 08 05 02 01 08 00 07 13 25 04 13 04 20 20 17 22 16)")
        msg_row = find(r"\n(00 02 19 08 21 00 19 04 13 00 20 06 07 19 05 08 17 04 23 23 23 23 23 23 23)")
        assert [int(x) for x in key_row.split()] == [ord(c) - 65 for c in KEY]
        assert [int(x) for x in msg_row.split()] == [ord(c) - 65 for c in MSG]

    def test_ciphertext(self):
        ct = degroup(find(r"\n(FGDFZ TJWVF WHPTM VQIKB RROTN)"))
        assert encrypt(MSG, KEY) == ct
        assert decrypt(ct, KEY) == MSG

    def test_a_padding_tail_is_raw_key(self):
        padded = "ACTIVATENAUGHTFIREAAAAAAA"
        ct = encrypt(padded, KEY)
        assert ct[18:] == KEY[18:]


class TestTechnicalities:
    def test_dawn_to_dusk_deltas(self):
        deltas = [(ord(b) - ord(a)) % 26 for a, b in zip("DAWN", "DUSK")]
        assert deltas == [0, 20, 22, 23]
        assert "00 20 22 23" in MD


class TestDiceTable:
    def test_all_rows(self):
        # idx = (first-1)*6 + (second-1); 0-25 letter, 26-35 re-roll
        rows = {
            1: find(r"first  1  (.+?)\n"),
            2: find(r"die    2  (.+?)\n"),
            3: find(r"       3  (.+?)\n"),
            4: find(r"       4  (.+?)\n"),
            5: find(r"       5  (Y.+?)\n"),
        }
        for first, text in rows.items():
            cells = text.split()[:6]
            expect = []
            for second in range(1, 7):
                idx = (first - 1) * 6 + (second - 1)
                expect.append(chr(65 + idx) if idx < 26 else "-")
            assert cells == expect, f"dice row {first}"


class TestTabulaRecta:
    def test_all_26_rows(self):
        rows = re.findall(r"^([A-Z]) \| ([A-Z](?: [A-Z]){25})$", MD, re.M)
        assert len(rows) == 26
        for key_letter, row in rows:
            k = ord(key_letter) - 65
            expect = [chr(65 + ((k + p) % 26)) for p in range(26)]
            assert row.split() == expect, f"tabula recta row {key_letter}"


class TestExercises:
    def _section(self):
        return MD.split("# Exercises")[1].split("# Glossary")[0]

    def test_exercise_1_encrypt(self):
        sec = self._section()
        key = degroup(re.search(r"Key:      (BMYTT.+)", sec).group(1))
        answer = degroup(re.search(r"\n(CDGGZ.+)", sec).group(1))
        plain = "BRINGTHREESHOVELS" + "A" * 8
        assert encrypt(plain, key) == answer
        assert answer[18:] == key[18:]  # the A-padding lesson

    def test_exercise_2_decrypt(self):
        sec = self._section()
        ct = degroup(re.search(r"Received: (CCTDC.+)", sec).group(1))
        key = degroup(re.search(r"Key:      (JVPYO.+)", sec).group(1))
        assert decrypt(ct, key) == "THEFOXISINTHEHENHOUSE" + "A" * 4

    def test_exercise_3_garble_and_repair(self):
        sec = self._section()
        ct = degroup(re.search(r"Received: (CCJAX.+)", sec).group(1))
        key = degroup(re.search(r"Key:      (OLHTX.+)", sec).group(1))
        naive = degroup(re.search(r"\n(ORCHA RDGAR.+)", sec).group(1))
        fixed = degroup(re.search(r"\n(ORCHA RDGAT.+)", sec).group(1))
        plain = "ORCHARDGATEATNINEPM" + "A" * 6
        assert decrypt(ct, key) == naive          # straight decrypt garbles
        assert naive[:9] == plain[:9]             # clean until the slip
        assert naive[9:] != plain[9:]             # then nonsense
        assert fixed == plain
        # realignment: from the tenth letter, key shifted one position back
        assert decrypt(ct[:9], key[:9]) + decrypt(ct[9:], key[8:24]) == plain

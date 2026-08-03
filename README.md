# One-Time Pad Kit

A complete kit for practical, pencil-and-paper one-time pad (OTP) encryption:
a printable field manual for teaching the method, and a generator that
produces real, ready-to-print pads.

The one-time pad is the only cipher with a proof of unbreakability — no
computer, no password, no math beyond adding letters. Its price is discipline:
truly random keys, exchanged in person, used once, and destroyed. The manual
teaches both the arithmetic and the discipline; the generator takes care of
producing the key material.

## Contents

| File | Purpose |
|------|---------|
| `otp.md` | The manual. Written to be printed and handed to students: the full encrypt/decrypt walkthrough, the rules that make OTP secure, key generation by hand, communication protocols, authentication, a printable tabula recta, and exercises with an answer key. |
| `otp_generator.py` | Generates pad sets as pocket-sized PDFs (A6, or two-up A7) from the operating system's cryptographic randomness. Also produces training pads and blank worksheets. |
| `sample_codewords.txt` | Example codeword list — one codeword per pad set, one per line. |
| `tests/` | Test suite (run by CI): guards the generator's randomness against bias and re-verifies every worked number printed in the manual. |

## The manual

`otp.md` is self-contained and assumes no prior cryptography. Print it
double-sided and hand it out; everything a student needs is inside, including
the tabula recta table that replaces all the arithmetic once the concept has
landed. It renders anywhere Markdown renders (it is also Obsidian-friendly).

Suggested classroom flow:

1. Work through the **Example transmission** section together, by hand, with the numbers.
2. Introduce the **tabula recta** (Tools section) and repeat the exercise letters-only.
3. Set the **Exercises** appendix — three graded problems with an answer key,
   including a garbled-decrypt repair. Print worksheets for them (`--worksheets`).
4. Generate a small training set (`--pages 20 --training`), pair students up, and
   run full two-way exchanges: encrypt, transmit by voice, authenticate, decrypt,
   destroy. Training pads are watermarked so they can never be mistaken for live
   material.
5. Finish with **Critical technicalities** and **Common Mistakes** — the rules
   mean more after students have felt the procedure.

To produce the manual itself as a PDF handout: `pandoc otp.md -o otp.pdf`
(any Markdown-to-PDF route works; the tabula recta needs a monospace font,
which code blocks get by default).

## The generator

### Requirements

Python 3.9+ and [reportlab](https://pypi.org/project/reportlab/):

```
pip install reportlab
```

### Quick start

```
python3 otp_generator.py --codewords sample_codewords.txt --sets 2 --pages 50 --output ./pads
```

This produces one PDF per set, named after its codeword (`WALRUS.pdf`, ...).
**Print each PDF twice** — that is your A and B copy. The two printouts are the
pad pair; the digital file is a liability to be destroyed after printing.

For classroom material in one go — a marked training set plus worksheets:

```
python3 otp_generator.py --codewords sample_codewords.txt --sets 1 --pages 20 --training --worksheets 10 --output ./classroom
```

No A6 paper? Print the pad PDFs on A4 with your print dialog set to 4 pages
per sheet and cut twice — the result is four A6 pages per A4 sheet. Worksheets
are A5: two per A4 sheet, one cut.

### Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--codewords` | (required) | File with one codeword per line; the first `--sets` words are used |
| `--sets` | 10 | Number of pad sets to generate |
| `--pages` | 1000 | Pages per set |
| `--output` | `./output` | Output directory |
| `--chars` | 665 (A6) / 375 (A7) | Key characters per page |
| `--fontsize` | 9 | Font size in points |
| `--a7` | off | Two pad pages per landscape A6 sheet; cut along the dashed line |
| `--no-auth` | off | Omit the AUTH group from page headers |
| `--training` | off | Watermark every page as TRAINING material (the manual requires practice pads to be unmistakably marked) |
| `--worksheets` | 0 | Also generate N blank A5 worksheet pages as `WORKSHEETS.pdf` — M/K/C rows in five-letter group cells. They contain no key material, so print as many copies as you need. `--codewords` is optional when only worksheets are requested. |

### Anatomy of a page

```
CODEWORD        AUTH QJXKV        0001     ← codeword, auth group, page number
──────────────────────────────────────
HAJUT SHIFN RCFVF YVIIM TLVIG ...          ← key body, five-letter groups
...
        USE ONCE — DESTROY AFTER USE
```

- **Codeword** identifies the set without identifying its holders.
- **AUTH group** — five key letters reserved for message authentication, never
  part of the key body. The manual's Authentication section defines the
  procedure it supports.
- **Page number** keeps the two ends synchronized; it is sent in clear with
  each message.

## A note on randomness

The generator draws from the operating system's CSPRNG (`os.urandom`) and maps
bytes to letters with rejection sampling, so every letter is exactly equally
likely. An OS CSPRNG is continuously re-seeded with physical noise and is not
practically attackable — but strictly speaking it makes these pads
*computationally* secure rather than *information-theoretically* secure. For
teaching and hobby use that distinction is academic. If your threat model
disagrees, the manual describes hand-generation methods (dice with rejection
sampling, shake-and-draw) that keep the proof intact.

## Generation hygiene

If the pads are to protect anything real, treat generation as the sensitive
step it is — the manual's Security & Integrity section is the full version:

- Generate on an offline, dedicated machine.
- Printers have memory and spoolers; treat every machine that touched key
  material as part of the pad.
- Delete the PDFs after printing and wipe the machine when the batch is done.
- Seal each printed copy in an opaque envelope labeled only with its codeword.

## License

Free for any non-commercial use — see [LICENSE](LICENSE) for the full terms.

- **Code** (`otp_generator.py`, `tests/`): [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)
- **Manual and content** (`otp.md`, images): [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)

Print it, teach with it, adapt it, share it — just not commercially. For
commercial licensing, contact the author.

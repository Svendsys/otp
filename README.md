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
| `otp_generator.py` | Generates pad sets as pocket-sized PDFs (A6, four-up on A4, or two-up A7) from the machine's hardware RNG mixed with the OS CSPRNG. Also produces tabula recta cards, training pads and blank worksheets. |
| `otpunit/` | The print unit: a headless Raspberry Pi appliance that prints pad pairs straight from RAM. See [the print unit](#the-print-unit). |
| `codewords/` | The codeword vocabulary — concrete nouns and modifiers, curated to be picturable and phonetically distinct. |
| `device/`, `image/` | Everything needed to provision a Pi or build a flashable image. |
| `sample_codewords.txt` | Example codeword list — one codeword per pad set, one per line. |
| `tests/` | Test suite (run by CI): guards the generator's randomness against bias, re-verifies every worked number printed in the manual, and drives the print unit's whole interface without hardware. |

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
| `--a4` | off | Four A6 pad pages per A4 sheet with crop marks, imposed **cut-and-stack** — see [Printing on A4](#printing-on-a4) |
| `--letter` | off | The same, on US Letter |
| `--tabula` | 0 | Also generate N tabula recta cards as `TABULA_RECTA.pdf` — the manual's 26×26 table, pocket-sized to go in the envelope with the pad. No key material. |
| `--auth-size` | 5 | Letters in the AUTH group. The letters are always CSPRNG output; only the length is adjustable. |
| `--random-codewords` | 0 | Generate N random `<MODIFIER>-<NOUN>` codewords instead of reading a file |
| `--stdout` | off | Write the single generated PDF to stdout instead of a file: `otp_generator.py --random-codewords 1 --pages 100 --a4 --stdout \| lp -n 2 -o Collate=True`. Note the `-n 2`: a pad set is two identical copies, and once the bytes are down the pipe they are gone — re-running makes a *different* pad, not the twin. `Collate=True` is not optional: without it the copies come out interleaved (page 1, page 1, page 2, page 2), which is one stack to deal out by hand rather than two pads. See the caveat below; this keeps key material out of *this program's* output, not out of your spooler. |

### What `--stdout` does and does not do

It stops the generator writing a PDF file. Everything downstream of the pipe
still applies, and on an ordinary desktop that means quite a lot: CUPS always
spools the job to `/var/spool/cups`, and `PreserveJobFiles` defaults to 24
hours, so the whole pad sits on disk for a day after printing. `job.cache`
keeps job history indefinitely, `page_log` records a line per page, and your
swap and hibernation files are live. `--random-codewords` also prints the
codeword to stderr, which means terminal scrollback.

The print unit survives all of that because it forces the CUPS spool onto
tmpfs, runs without swap, and forgets everything at power-off. A laptop does
none of those things. If the pads are protecting anything real, read
[Generation hygiene](#generation-hygiene) — or build the unit.

### Printing on A4

Pad pages are A6 and most people do not have A6 paper. `--a4` tiles four pad
pages onto each A4 sheet with crop marks at the edges, ready to be cut down
on a guillotine.

The imposition is **cut-and-stack**, not reading order. Cut the printed
stack twice — once down the middle, once across — and you have four piles,
each already in page order. Assemble the pad by dropping them on top of one
another: top-left, then top-right, then bottom-left, then bottom-right.

The tiling is in the PDF rather than left to the print dialog, because a
guillotine cuts the whole stack at once and every sheet therefore needs
identical geometry. For the same reason, do not add scaling or N-up in your
print dialog — it will move the cut line away from the crop marks.

**Cut copy A and copy B separately.** The two copies are deliberately
identical, which means once their sheets touch you cannot tell them apart.
Cutting both stacks in one pass risks a sheet crossing over, and the result
is not a harmless mix — one pad ends up with page 0007 twice and no 0008,
its twin the mirror image. That passes the page count the manual tells you
to check at handover, and surfaces weeks later as a failed authentication
that is indistinguishable from tampering. It also puts a duplicated key page
in someone's pocket, which is the one thing a one-time pad must never have.

Mark each stack before you cut it, keep them apart, and before sealing each
envelope fan the pad and confirm the page numbers run 1..N with no repeats.

### Anatomy of a page

```
RUSTED-BADGER   AUTH QJXKV        0001     ← codeword, auth group, page number
──────────────────────────────────────
HAJUT SHIFN RCFVF YVIIM TLVIG ...          ← key body, five-letter groups
...
        USE ONCE — DESTROY AFTER USE
```

- **Codeword** identifies the set without identifying its holders. It is
  drawn as `<MODIFIER>-<NOUN>` from a curated vocabulary of concrete,
  picturable words — an operator has to carry it from a handover to a radio.
  Two words rather than one because a single-word list collides sooner than
  it looks: by the birthday bound, 2000 bare words repeat with ~10%
  probability within twenty sets. It renders smaller than the key body if it
  needs to, since it is a label rather than something read letter by letter.
- **AUTH group** — five key letters reserved for message authentication, never
  part of the key body. The manual's Authentication section defines the
  procedure it supports.
- **Page number** keeps the two ends synchronized; it is sent in clear with
  each message.

## The print unit

A Raspberry Pi that does nothing but print pads. It boots straight into a
single-purpose appliance: plug in a USB laser printer, pick a codeword and a
page count on a small OLED with three buttons, and it prints the A and B
copies back to back, then wipes itself. No screen, no keyboard, no network.

This exists because the manual's advice about generation hygiene is hard to
follow with a laptop and easy to follow with a dedicated box:

- **No pad ever reaches the SD card.** The PDF is generated into RAM and
  piped to `lp` on stdin — this process opens no file. CUPS still spools
  each job, which is unavoidable short of writing raw to `/dev/usb/lp0` and
  only PostScript and PCL printers accept that; so its spool, temp and cache
  directories are all forced onto tmpfs and purged after every job.
- **The codeword is not sent to the printer.** It is neither the CUPS job
  title, because job names persist in CUPS's own records, nor part of the
  PDF metadata — page content is compressed but the document Info dictionary
  is not, so a `strings` pass over a stored job would otherwise read it. The
  PDF also carries no timestamp, so a captured job cannot date the pad. The
  codeword does appear on the printed page, which is the point of it.
- **No swap**, so nothing holding key material can be paged to disk.
- **Volatile logs**, no core dumps, and job metadata only.
- **Read-only root with a RAM overlay** — see [docs/IMAGE.md](docs/IMAGE.md).
  This is one manual step after first boot, not the state you are handed.

Be clear about what is *not* claimed. Key material is not scrubbed from RAM
when a job ends: the working buffer is zeroed, but reportlab's intermediates
and the immutable bytes handed to the subprocess cannot be, and copies stay
resident until that memory is reused. Nothing is paged to disk and nothing
survives a power cycle, so **powering the unit off is the wipe** — which is
what the panel tells the operator to do.

It also prints worksheets, tabula recta cards, and this manual.

Everything needed is in this repository:

```bash
./image/build.sh          # build a flashable .img.xz
sudo ./device/install.sh  # or convert a Pi you already have
python3 -m otpunit --sim  # or just try the interface in a terminal
```

See [docs/HARDWARE.md](docs/HARDWARE.md) for parts and wiring,
[docs/IMAGE.md](docs/IMAGE.md) for building and flashing, and
[docs/PRINTERS.md](docs/PRINTERS.md) for what works and what does not.

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

This project is licensed for non-commercial use: the code
(`otp_generator.py`, `otpunit/`, `device/`, `image/`, `tests/`) under
[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/),
and the manual and other content (`otp.md`, images) under
[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).
See [LICENSE](LICENSE) for the full terms.

Print it, teach with it, adapt it, share it — just not commercially.
For alternative licenses, contact the author.

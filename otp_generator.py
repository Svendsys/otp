#!/usr/bin/env python3
"""
OTP Pad Generator

Generates one-time pad sets as pocket-sized PDFs ready for printing.
Each set is a PDF containing pages of random key material.
Print each PDF twice to get your A and B copies.

Usage:
    python3 otp_generator.py --codewords words.txt --sets 10 --pages 1000 --output ./pads
    python3 otp_generator.py --codewords words.txt --sets 10 --pages 1000 --a7 --output ./pads

Arguments:
    --codewords   Path to file with one codeword per line
    --sets        Number of paired sets to generate (default: 10)
    --pages       Number of pages per set (default: 1000)
    --output      Output directory for PDFs (default: ./output)
    --chars       Characters of key material per pad page (default: 665 for A6, 375 for A7)
    --fontsize    Font size in points (default: 9)
    --a7          Layout two pad pages per A6 sheet (cut along the dashed line to get A7)
    --no-auth     Omit the AUTH group from page headers
    --training    Watermark every page as TRAINING material
    --worksheets  Also generate N A5 worksheet pages as WORKSHEETS.pdf
                  (no key material — print as many copies as you need)

Each page header carries an AUTH group: five extra key letters reserved for
message authentication, generated alongside the key body but never part of it.
See the manual (otp.md, Authentication) for the procedure it supports.

Training pads support the manual's rule that practice material must be
unmistakably marked: --training watermarks TRAINING across every page.
"""

import argparse
import math
import os
import sys
import time
from reportlab.lib.units import mm
from reportlab.lib.pagesizes import A4, A5, A6, LETTER
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


# A6 = 105mm x 148mm (the paper we print on)
SHEET_WIDTH, SHEET_HEIGHT = A6

# Group formatting
GROUP_SIZE = 5
A6_GROUPS_PER_ROW = 7
A6_CHARS_PER_ROW = A6_GROUPS_PER_ROW * GROUP_SIZE  # 35
A7_GROUPS_PER_ROW = 5
A7_CHARS_PER_ROW = A7_GROUPS_PER_ROW * GROUP_SIZE  # 25

# Header geometry, shared by the drawing code and the fit calculations
HEADER_MARGIN_H = 4 * mm
HEADER_GAP = 2 * mm

# Crop marks for the guillotine, on imposed sheets. Inset because a laser
# leaves the outer few millimetres of a sheet unprinted (about 4.23mm on
# common models); 5mm clears that on every printer in docs/PRINTERS.md.
CROP_INSET = 5 * mm
CROP_TICK = 7 * mm

# Two-word codewords (RUSTED-BADGER) do not fit the A7 header at the body
# font size, so the codeword shrinks to fit. AUTH and the page number stay
# at full size because they are read letter by letter.
CODEWORD_MIN_FONT = 5.5


# The Raspberry Pi SoC's hardware TRNG, as exposed by Linux. Overridable so
# tests can point at a fake, and so a laptop can opt out.
HWRNG_PATH = os.environ.get("OTP_HWRNG_PATH", "/dev/hwrng")

# TRNG throughput varies by orders of magnitude between devices, so reads
# are bounded. Past the deadline the whole request falls back to the CSPRNG
# and says so.
HWRNG_TIMEOUT = float(os.environ.get("OTP_HWRNG_TIMEOUT", "60"))
# Small enough that the deadline is checked often on a slow device, large
# enough that syscalls are not the bottleneck on a fast one.
_HWRNG_CHUNK = 4096
# Pause between polls of a non-blocking TRNG that has nothing ready yet.
_HWRNG_POLL = 0.001

# Probe size for entropy_source: enough bytes for _looks_alive to detect a
# stuck TRNG, which otherwise fails silently.
_HEALTH_SAMPLE = 256


def _hwrng_bytes(n: int, path: str | None = None) -> bytes | None:
    """
    n bytes straight off the hardware TRNG, or None if there is not one.

    A short read, a timeout or unhealthy output is None, never a partial
    answer: the caller must not pad key material from another source
    without knowing it. The path is resolved per call so HWRNG_PATH stays
    overridable at runtime.
    """
    path = path or HWRNG_PATH
    deadline = time.monotonic() + HWRNG_TIMEOUT
    # O_NONBLOCK is the only thing that bounds this: a blocking read can sit
    # in the kernel past the deadline, and select() is useless because the
    # hwrng char device implements no .poll and always reports ready. On a
    # regular file (the test fakes) O_NONBLOCK is a no-op.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    chunks, got = [], 0
    try:
        while got < n:
            if time.monotonic() > deadline:
                # Abandon the whole request rather than splice a CSPRNG
                # tail onto a TRNG head; nothing downstream could tell.
                return None
            try:
                chunk = os.read(fd, min(_HWRNG_CHUNK, n - got))
            except BlockingIOError:
                # A non-blocking read returns only what the driver has
                # buffered, so the poll interval caps throughput at
                # buffer/sleep. Keep it short.
                time.sleep(_HWRNG_POLL)
                continue
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            got += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    return data if len(data) == n and _looks_alive(data) else None


def _looks_alive(data: bytes) -> bool:
    """
    Reject output no working TRNG would produce.

    This cannot make a key stronger, since the result is XORed with the
    CSPRNG either way; it stops the unit claiming hardware entropy it did
    not get. Checks the whole buffer: a floor on distinct values, and
    periodicity for a latched or counting register.
    """
    n = len(data)
    if n < 16:
        return len(set(data)) > 1
    # For random bytes, expected distinct values = 256(1 - e^(-n/256)).
    # Half of that is far below sampling noise and far above a stuck bus.
    if len(set(data)) < 256 * (1 - math.exp(-n / 256)) * 0.5:
        return False
    for period in (1, 2, 3, 4, 8, 16, 32, 64, 128, 256):
        if n >= period * 3 and data[period:] == data[:-period]:
            return False
    return True


# What get_random_bytes actually drew on, as opposed to what the
# entropy_source probe predicts: a device can die mid-pad. Callers reset
# this before a job and read it after.
class Tally:
    """A count of which source each request was actually served from."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.hardware = 0
        self.software = 0

    def record(self, hardware: bool) -> None:
        if hardware:
            self.hardware += 1
        else:
            self.software += 1

    @property
    def total(self) -> int:
        return self.hardware + self.software

    def summary(self) -> str:
        """One line, true for the material just generated."""
        if not self.total:
            return "nothing generated yet"
        if not self.software:
            return "hardware TRNG, mixed with the system CSPRNG"
        if not self.hardware:
            return ("NO HARDWARE RNG. This key came from the system CSPRNG "
                    "alone, which makes it a very strong stream cipher "
                    "rather than a true one-time pad.")
        return (f"MIXED SOURCES: {self.hardware} of {self.total} draws used "
                f"the hardware TRNG; the rest fell back to the system "
                f"CSPRNG alone. Treat this pad as a stream cipher.")


TALLY = Tally()


def entropy_source(path: str | None = None) -> str:
    """
    Which source get_random_bytes would draw on right now.

    Probes with _HEALTH_SAMPLE bytes because a single byte can never fail
    the health check. Only a prediction; for what a particular pad was
    made from, read TALLY.
    """
    return "hwrng+urandom" if _hwrng_bytes(_HEALTH_SAMPLE, path) is not None \
        else "urandom"


def _source_note(source: str) -> str:
    """One line naming the source, and what it means for the output."""
    if source == "hwrng+urandom":
        return (f"hardware TRNG ({HWRNG_PATH}) XORed with the system CSPRNG "
                f"-- never weaker than the CSPRNG, and a true one-time pad "
                f"to the extent the TRNG delivers full entropy, which this "
                f"program cannot measure")
    return ("system CSPRNG only -- no hardware TRNG on this machine. These "
            "pads are a very strong stream cipher, not information-"
            "theoretically secure one-time pads.")


# --- is the kernel CSPRNG up yet ---------------------------------------
#
# Not a security gate. os.urandom goes through getrandom(), which blocks
# until the CRNG is seeded, so a pad cannot be drawn from an unseeded pool.
# But the kernel blocks silently, and on a freshly flashed Pi that freezes
# the panel (or prints nothing when headless), which looks like a crash and
# invites a power cycle that discards the entropy collected so far. These
# probes turn the silent wait into a report; they never decide whether key
# material is drawn.

# The kernel's entropy estimate. For display only, never a gate: on modern
# kernels it is pinned at the pool size once the CRNG is seeded and
# getrandom() ignores it.
ENTROPY_AVAIL = "/proc/sys/kernel/random/entropy_avail"

# How often to re-ask while waiting. A boot that waits at all waits
# seconds, so this clears promptly and costs nothing otherwise.
CRNG_POLL_SECONDS = 0.5


def crng_seeded() -> bool:
    """
    Whether the kernel's CSPRNG is initialised.

    getrandom(GRND_NONBLOCK) returns EAGAIN exactly when the getrandom()
    behind os.urandom would block. Where the question cannot be asked the
    answer is True: the kernel's own blocking still protects the key
    material, and a False here would be a wait that never clears.
    """
    getrandom = getattr(os, "getrandom", None)
    nonblock = getattr(os, "GRND_NONBLOCK", None)
    if getrandom is None or nonblock is None:
        return True
    try:
        # One byte, discarded: nothing drawn here reaches a pad.
        getrandom(1, nonblock)
    except BlockingIOError:
        return False
    except OSError:
        # Any other errno is the probe failing, not the CRNG being down.
        return True
    return True


def entropy_bits() -> int | None:
    """
    The kernel's entropy estimate, or None where it cannot be read.

    Only meaningful while crng_seeded() is False; once seeded it is a
    constant.
    """
    try:
        with open(ENTROPY_AVAIL) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def wait_for_crng(on_wait=None, sleep=None, poll: float = None) -> float:
    """
    Block until the CRNG is seeded. Returns the seconds waited (0.0 if
    already seeded).

    `on_wait(waited)` is called once before each sleep. A caller with its
    own event loop should poll crng_seeded() itself instead.

    There is no timeout: giving up would mean generating anyway, and
    getrandom() would block silently at the first draw.
    """
    sleep = sleep or time.sleep
    poll = CRNG_POLL_SECONDS if poll is None else poll
    waited = 0.0
    while not crng_seeded():
        if on_wait is not None:
            on_wait(waited)
        sleep(poll)
        waited += poll
    return waited


def get_random_bytes(n: int) -> bytes:
    """
    n bytes for key material: the hardware TRNG XORed with the CSPRNG.

    A one-time pad has to come from a physical process; os.urandom alone
    makes a stream cipher. The TRNG is XORed with os.urandom rather than
    used raw because it can be biased or stuck: if either source is good
    the output is good, and neither can weaken the other.

    Falls back to os.urandom alone when there is no usable /dev/hwrng;
    TALLY records which happened.
    """
    system = os.urandom(n)
    hardware = _hwrng_bytes(n)
    TALLY.record(hardware is not None)
    if hardware is None:
        return system
    mixed = int.from_bytes(hardware, "big") ^ int.from_bytes(system, "big")
    return mixed.to_bytes(n, "big")


def generate_random_letters(count: int) -> str:
    """
    Generate `count` uniformly random uppercase letters A-Z
    using rejection sampling over CSPRNG bytes for unbiased output.
    """
    letters = []
    while len(letters) < count:
        raw = get_random_bytes((count - len(letters)) * 2)
        for byte in raw:
            if len(letters) >= count:
                break
            # Rejection sampling: 26 * 9 = 234, reject 234-255
            if byte < 234:
                letters.append(chr(65 + (byte % 26)))
    return "".join(letters)


def load_codewords(filepath: str) -> list[str]:
    """Load codewords from file, one per line, stripped and uppercased."""
    try:
        with open(filepath, "r") as f:
            words = [line.strip().upper() for line in f if line.strip()]
    except OSError as e:
        print(f"ERROR: Cannot read codewords file: {e}")
        sys.exit(1)
    return words


CODEWORDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codewords")


def _read_words(path: str) -> list[str]:
    with open(path, "r") as f:
        return [line.strip().upper() for line in f if line.strip()]


def load_vocabulary(base_dir: str = None):
    """
    Load the bundled codeword vocabulary.

    Returns (modifiers, {category: nouns}). Codewords are <MODIFIER>-<NOUN>
    so that two sets are very unlikely to collide, while both halves stay
    concrete enough to carry from a handover to a radio.
    """
    base = base_dir or CODEWORDS_DIR
    modifiers = _read_words(os.path.join(base, "modifiers.txt"))
    noun_dir = os.path.join(base, "nouns")
    nouns = {
        name[:-4]: _read_words(os.path.join(noun_dir, name))
        for name in sorted(os.listdir(noun_dir)) if name.endswith(".txt")
    }
    return modifiers, nouns


def random_choice(seq):
    """
    Uniform choice from the OS CSPRNG, by rejection sampling.

    Deliberately not the `random` module: everything that picks anything in
    this program draws from the same source as the key material.
    """
    n = len(seq)
    if n == 0:
        raise ValueError("cannot choose from an empty sequence")
    limit = (2 ** 32 // n) * n
    while True:
        value = int.from_bytes(get_random_bytes(4), "big")
        if value < limit:
            return seq[value % n]


def random_codewords(count: int, base_dir: str = None) -> list[str]:
    """Generate `count` distinct <MODIFIER>-<NOUN> codewords."""
    modifiers, nouns = load_vocabulary(base_dir)
    flat_nouns = [word for words in nouns.values() for word in words]
    total = len(modifiers) * len(flat_nouns)
    if count > total:
        raise ValueError(f"asked for {count} codewords but only {total} exist")
    seen, out = set(), []
    while len(out) < count:
        word = f"{random_choice(modifiers)}-{random_choice(flat_nouns)}"
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def draw_pad_page(
    c: canvas.Canvas,
    codeword: str,
    page_num: int,
    chars_per_page: int,
    font_size: float,
    groups_per_row: int,
    chars_per_row: int,
    box_left: float,
    box_bottom: float,
    box_width: float,
    box_height: float,
    with_auth: bool = True,
    training: bool = False,
    auth_size: int = GROUP_SIZE,
):
    """Draw a single pad page within the given bounding box."""
    margin_h = HEADER_MARGIN_H
    margin_top = 5 * mm
    margin_bottom = 4 * mm

    left = box_left + margin_h
    right = box_left + box_width - margin_h
    content_width = right - left

    # Key material: the body plus an optional AUTH group, reserved for
    # authentication and excluded from the key body (see otp.md).
    extra = auth_size if with_auth else 0
    key_chars = generate_random_letters(chars_per_page + extra)
    body_chars = key_chars[:chars_per_page]
    auth_group = key_chars[chars_per_page:] if with_auth else None

    # Header. The codeword shrinks to fit if it must; AUTH and the page
    # number stay at font_size, since those get read character by character.
    y = box_bottom + box_height - margin_top
    a7_box = box_width < SHEET_WIDTH
    cw_size = fit_codeword_size(codeword, font_size, a7_box, with_auth, auth_size)
    c.setFont("Courier-Bold", cw_size or CODEWORD_MIN_FONT)
    c.drawString(left, y, codeword)
    c.setFont("Courier-Bold", font_size)
    if auth_group:
        c.drawCentredString(box_left + box_width / 2, y, f"AUTH {auth_group}")
    c.drawRightString(right, y, f"{page_num:04d}")

    # Separator
    y -= 2 * mm
    c.setLineWidth(0.3)
    c.line(left, y, right, y)
    header_bottom = y

    # Footer
    footer_y = box_bottom + margin_bottom
    c.setFont("Courier-Bold", 5.5)
    if training:
        footer_text = "TRAINING \u2014 USE ONCE \u2014 DESTROY AFTER USE"
    else:
        footer_text = "USE ONCE \u2014 DESTROY AFTER USE"
    c.drawCentredString(box_left + box_width / 2, footer_y - 1 * mm, footer_text)
    footer_top = footer_y + 1 * mm

    # Body: distribute rows evenly vertically
    num_rows = -(-chars_per_page // chars_per_row)
    body_height = header_bottom - footer_top

    if num_rows > 1:
        row_spacing = body_height / (num_rows + 1)
    else:
        row_spacing = body_height / 2

    min_spacing = font_size * 0.38 * mm
    row_spacing = max(row_spacing, min_spacing)

    # Shading
    shade_height = font_size * 0.45 * mm
    shade_color = 0.88

    # Measure group width for horizontal distribution
    c.setFont("Courier", font_size)
    single_group_width = c.stringWidth("X" * GROUP_SIZE, "Courier", font_size)

    # Horizontal spacing: distribute groups evenly across content width
    if groups_per_row > 1:
        total_groups_width = single_group_width * groups_per_row
        total_gap = content_width - total_groups_width
        group_gap = total_gap / (groups_per_row - 1)
    else:
        group_gap = 0

    all_groups = [body_chars[i:i + GROUP_SIZE] for i in range(0, len(body_chars), GROUP_SIZE)]
    row_ys = [header_bottom - row_spacing * (r + 1) for r in range(num_rows)]

    # Alternating row shading
    for row_num, y in enumerate(row_ys):
        if row_num % 2 == 1:
            c.saveState()
            c.setFillGray(shade_color)
            c.rect(
                left - 1 * mm,
                y - shade_height * 0.3,
                content_width + 2 * mm,
                shade_height,
                fill=1, stroke=0,
            )
            c.restoreState()

    # Training watermark: large, light, diagonal — over the shading,
    # under the key text so the letters stay fully legible
    if training:
        c.saveState()
        c.setFillGray(0.80)
        c.setFont("Courier-Bold", box_width / 9)
        c.translate(box_left + box_width / 2, (header_bottom + footer_top) / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, "TRAINING")
        c.restoreState()

    # Key text
    c.setFont("Courier", font_size)
    group_idx = 0
    for y in row_ys:
        for g in range(groups_per_row):
            if group_idx >= len(all_groups):
                break
            x = left + g * (single_group_width + group_gap)
            c.drawString(x, y, all_groups[group_idx])
            group_idx += 1


class GenerationCancelled(Exception):
    """Raised when a should_cancel callback asks for generation to stop."""


def new_canvas(output, pagesize, title: str = "OTP"):
    """
    A canvas whose metadata gives nothing away.

    The PDF Info dictionary is not compressed, so `strings` over a spooled
    job reads it: the title must never carry the codeword and the
    timestamps must not date the pad. `invariant=1` fixes the dates and the
    trailer /ID, which also keeps copies A and B byte-identical.
    """
    c = canvas.Canvas(output, pagesize=pagesize, invariant=1)
    c.setTitle(title)
    c.setAuthor("")
    c.setSubject("")
    c.setCreator("")
    return c


def _printing_progress(codeword: str, stream=None):
    """The CLI's progress reporter: a line every hundred pad pages."""
    def report(done: int, total: int):
        if done % 100 == 0:
            print(f"  [{codeword}] {done}/{total} pages generated",
                  file=stream if stream is not None else sys.stdout)
    return report


def generate_set_pdf_a6(
    output,
    codeword: str,
    num_pages: int,
    chars_per_page: int,
    font_size: float,
    with_auth: bool = True,
    training: bool = False,
    auth_size: int = GROUP_SIZE,
    progress=None,
    should_cancel=None,
):
    """
    Generate OTP set as A6 pages (one pad page per sheet).

    `output` is a path or a writable binary stream \u2014 the stream form keeps key
    material out of the filesystem entirely (see otpunit.jobs).
    """
    if progress is None:
        progress = _printing_progress(codeword)
    c = new_canvas(output, A6)

    for page_num in range(1, num_pages + 1):
        if should_cancel is not None and should_cancel():
            raise GenerationCancelled(f"cancelled at page {page_num} of {num_pages}")
        draw_pad_page(
            c, codeword, page_num, chars_per_page, font_size,
            groups_per_row=A6_GROUPS_PER_ROW,
            chars_per_row=A6_CHARS_PER_ROW,
            box_left=0, box_bottom=0,
            box_width=SHEET_WIDTH, box_height=SHEET_HEIGHT,
            with_auth=with_auth,
            training=training,
            auth_size=auth_size,
        )
        c.showPage()
        progress(page_num, num_pages)

    c.save()


def generate_set_pdf_a7(
    output,
    codeword: str,
    num_pages: int,
    chars_per_page: int,
    font_size: float,
    with_auth: bool = True,
    training: bool = False,
    auth_size: int = GROUP_SIZE,
    progress=None,
    should_cancel=None,
):
    """
    Generate OTP set as A7 — two pad pages side by side on landscape A6.

    `output` is a path or a writable binary stream.
    """
    if progress is None:
        progress = _printing_progress(codeword)
    # Landscape A6: 148mm wide x 105mm tall
    landscape_a6 = (SHEET_HEIGHT, SHEET_WIDTH)
    c = new_canvas(output, landscape_a6)

    sheet_w, sheet_h = landscape_a6
    half_width = sheet_w / 2

    page_num = 1
    while page_num <= num_pages:
        if should_cancel is not None and should_cancel():
            raise GenerationCancelled(f"cancelled at page {page_num} of {num_pages}")
        # Left half
        draw_pad_page(
            c, codeword, page_num, chars_per_page, font_size,
            groups_per_row=A7_GROUPS_PER_ROW,
            chars_per_row=A7_CHARS_PER_ROW,
            box_left=0, box_bottom=0,
            box_width=half_width, box_height=sheet_h,
            with_auth=with_auth,
            training=training,
            auth_size=auth_size,
        )

        # Right half (if it exists)
        if page_num + 1 <= num_pages:
            draw_pad_page(
                c, codeword, page_num + 1, chars_per_page, font_size,
                groups_per_row=A7_GROUPS_PER_ROW,
                chars_per_row=A7_CHARS_PER_ROW,
                box_left=half_width, box_bottom=0,
                box_width=half_width, box_height=sheet_h,
                with_auth=with_auth,
                training=training,
                auth_size=auth_size,
            )

        # Cut line: dashed vertical line down the middle
        c.saveState()
        c.setStrokeGray(0.5)
        c.setLineWidth(0.3)
        c.setDash(2, 2)
        c.line(half_width, 0, half_width, sheet_h)
        c.restoreState()

        c.showPage()
        progress(min(page_num + 1, num_pages), num_pages)

        page_num += 2

    c.save()


def generate_set_pdf_a4(
    output,
    codeword: str,
    num_pages: int,
    chars_per_page: int,
    font_size: float,
    with_auth: bool = True,
    training: bool = False,
    auth_size: int = GROUP_SIZE,
    progress=None,
    should_cancel=None,
    page_size=A4,
):
    """
    Generate OTP set as four A6 pad pages per A4 sheet, for guillotine work.

    Imposed cut-and-stack, not in reading order: the printed stack is cut
    twice into four piles, each already in page order, and the pad is
    assembled by stacking them top-left, top-right, bottom-left,
    bottom-right.

    The tiling is exact, not driver-scaled, because a guillotine cuts the
    whole stack at once and every sheet needs identical geometry.
    """
    if progress is None:
        progress = _printing_progress(codeword)
    # Quarter whatever sheet is given, so the cut lines are centred on
    # Letter as well as A4.
    page_w, page_h = page_size
    half_w, half_h = page_w / 2, page_h / 2
    # Cut-and-stack: position p carries pages p*sheets+1 .. (p+1)*sheets.
    sheets = -(-num_pages // 4)
    positions = [
        (0, half_h),        # top-left     pile 1
        (half_w, half_h),   # top-right    pile 2
        (0, 0),             # bottom-left  pile 3
        (half_w, 0),        # bottom-right pile 4
    ]

    c = new_canvas(output, page_size)

    done = 0
    for sheet in range(sheets):
        if should_cancel is not None and should_cancel():
            raise GenerationCancelled(f"cancelled at sheet {sheet + 1} of {sheets}")
        for slot, (box_left, box_bottom) in enumerate(positions):
            page_num = slot * sheets + sheet + 1
            if page_num > num_pages:
                continue
            draw_pad_page(
                c, codeword, page_num, chars_per_page, font_size,
                groups_per_row=A6_GROUPS_PER_ROW,
                chars_per_row=A6_CHARS_PER_ROW,
                box_left=box_left, box_bottom=box_bottom,
                box_width=half_w, box_height=half_h,
                with_auth=with_auth,
                training=training,
                auth_size=auth_size,
            )
            done += 1
        _draw_crop_marks(c, page_w, page_h)
        c.showPage()
        progress(min(done, num_pages), num_pages)

    c.save()


def _draw_crop_marks(c: canvas.Canvas, page_w: float, page_h: float):
    """
    Ticks near the sheet edges marking the two cuts.

    Edge ticks, not full rules, which would run through the key area. They
    start at CROP_INSET because a laser cannot print the outer few
    millimetres of a sheet. Both cuts run along page boundaries, clear of
    any content.
    """
    c.saveState()
    c.setStrokeGray(0.45)
    c.setLineWidth(0.4)
    inset, tick = CROP_INSET, CROP_TICK
    # Vertical cut, marked bottom and top
    c.line(page_w / 2, inset, page_w / 2, inset + tick)
    c.line(page_w / 2, page_h - inset - tick, page_w / 2, page_h - inset)
    # Horizontal cut, marked left and right
    c.line(inset, page_h / 2, inset + tick, page_h / 2)
    c.line(page_w - inset - tick, page_h / 2, page_w - inset, page_h / 2)
    c.restoreState()


def generate_worksheets_pdf(output, num_pages: int):
    """
    Generate blank A5 worksheets: blocks of M/K/C rows in five-letter
    group cells. Worksheets carry no key material, so unlike pads they
    can be printed in any quantity.

    `output` is a path or a writable binary stream.
    """
    page_w, page_h = A5  # 148mm x 210mm
    c = new_canvas(output, A5, "OTP WORKSHEETS")

    margin = 8 * mm
    label_w = 6 * mm
    groups_per_line = 5
    gap = 2 * mm
    cell_h = 6 * mm
    block_gap = 5 * mm
    row_labels = ("M", "K", "C")

    left = margin + label_w
    content_w = page_w - margin - left
    cell_w = (content_w - (groups_per_line - 1) * gap) / (groups_per_line * GROUP_SIZE)

    header_y = page_h - 10 * mm
    footer_y = 6 * mm
    top_of_blocks = header_y - 8 * mm
    block_h = 3 * cell_h
    usable = top_of_blocks - (footer_y + 6 * mm)
    blocks_per_page = int((usable + block_gap) // (block_h + block_gap))

    for _ in range(num_pages):
        c.setFont("Courier-Bold", 9)
        c.drawString(margin, header_y, "OTP WORKSHEET")
        c.drawRightString(page_w - margin, header_y, "PAGE ________")
        # The rows are labelled for encryption, so say how to use them for
        # a decrypt, where the ciphertext goes in the top row.
        c.setFont("Courier", 5.5)
        c.drawCentredString(page_w / 2, header_y - 5.5 * mm,
                            "ENCRYPT M+K=C   DECRYPT: PUT C IN THE TOP ROW, C-K=M")
        c.setLineWidth(0.3)
        c.line(margin, header_y - 2 * mm, page_w - margin, header_y - 2 * mm)

        c.setFont("Courier-Bold", 5.5)
        c.drawCentredString(page_w / 2, footer_y - 1 * mm,
                            "WORK ON A HARD SURFACE \u2014 DESTROY WITH THE PAGE")

        block_top = top_of_blocks
        for _block in range(blocks_per_page):
            c.saveState()
            for r, label in enumerate(row_labels):
                row_top = block_top - r * cell_h
                row_bottom = row_top - cell_h

                # Shade the result row so students always know where output goes
                if label == "C":
                    c.setFillGray(0.93)
                    c.rect(left, row_bottom, content_w, cell_h, fill=1, stroke=0)

                c.setFont("Courier", 6)
                c.setFillGray(0.45)
                c.drawCentredString(margin + label_w / 2, row_bottom + cell_h / 2 - 1 * mm, label)

                c.setLineWidth(0.4)
                c.setStrokeGray(0.65)
                x = left
                for _g in range(groups_per_line):
                    for _i in range(GROUP_SIZE):
                        c.rect(x, row_bottom, cell_w, cell_h, fill=0, stroke=1)
                        x += cell_w
                    x += gap
            c.restoreState()
            block_top -= block_h + block_gap

        c.showPage()

    c.save()


def generate_tabula_recta_pdf(output, page_size=A6, copies: int = 1):
    """
    Generate the 26x26 tabula recta as a card, sized to fill the page.

    This is the table from the manual's Tools section: row = key letter,
    column = message letter, intersection = ciphertext. It replaces the
    number conversion and modulo arithmetic once the concept has landed
    (see otp.md, Tabula Recta). It carries no key material, so unlike pads
    it can be printed in any quantity.
    """
    letters = [chr(65 + i) for i in range(26)]
    page_w, page_h = page_size
    c = new_canvas(output, page_size, "OTP TABULA RECTA")

    margin = 5 * mm
    title_h = 6 * mm
    footer_h = 5 * mm

    # 27 cells each way: one header row/column plus the 26 shift alphabets.
    content_w = page_w - 2 * margin
    content_h = page_h - 2 * margin - title_h - footer_h
    cell_w = content_w / 27
    cell_h = content_h / 27

    # Courier glyphs are 0.6 em wide; leave a little air on both axes.
    font_size = min(cell_w / 0.75, cell_h * 0.72)

    for _ in range(copies):
        c.setFont("Courier-Bold", min(9.0, font_size * 1.3))
        c.drawString(margin, page_h - margin - title_h * 0.7, "TABULA RECTA")
        c.setFont("Courier", min(5.5, font_size))
        c.drawRightString(page_w - margin, page_h - margin - title_h * 0.7,
                          "ROW=KEY  COL=MSG")

        top = page_h - margin - title_h
        left = margin

        # Shade the header row and column so the axes stay findable at speed
        c.setFillGray(0.85)
        c.rect(left, top - cell_h, content_w, cell_h, fill=1, stroke=0)
        c.rect(left, top - 27 * cell_h, cell_w, 27 * cell_h, fill=1, stroke=0)
        c.setFillGray(0)

        # Alternating band shading across the body, matching the pad pages
        c.setFillGray(0.93)
        for row in range(26):
            if row % 2 == 1:
                y = top - (row + 2) * cell_h
                c.rect(left + cell_w, y, content_w - cell_w, cell_h, fill=1, stroke=0)
        c.setFillGray(0)

        c.setLineWidth(0.25)
        c.setStrokeGray(0.6)
        for i in range(28):
            x = left + i * cell_w
            c.line(x, top, x, top - 27 * cell_h)
            y = top - i * cell_h
            c.line(left, y, left + 27 * cell_w, y)

        c.setFont("Courier-Bold", font_size)
        for col, letter in enumerate(letters):
            x = left + (col + 1.5) * cell_w
            c.drawCentredString(x, top - cell_h * 0.72, letter)
        for row, letter in enumerate(letters):
            y = top - (row + 1.72) * cell_h
            c.drawCentredString(left + cell_w * 0.5, y, letter)

        c.setFont("Courier", font_size)
        for row in range(26):
            y = top - (row + 2.72) * cell_h + cell_h
            for col in range(26):
                x = left + (col + 1.5) * cell_w
                c.drawCentredString(x, y, letters[(row + col) % 26])

        c.setFont("Courier-Bold", min(5.5, font_size))
        c.drawCentredString(page_w / 2, margin * 0.6,
                            "ENCRYPT: KEY ROW, MSG COLUMN — DECRYPT: KEY ROW, FIND CT, READ COLUMN")
        c.showPage()

    c.save()


def codeword_space(
    font_size: float,
    a7: bool,
    with_auth: bool,
    auth_size: int = GROUP_SIZE,
) -> float:
    """
    Horizontal room the codeword has in the page header, in points.

    The AUTH group and page number are always drawn at `font_size`, so the
    space available to the codeword does not depend on the codeword's own
    size — which is what lets it shrink independently.
    """
    box_width = (SHEET_HEIGHT / 2) if a7 else SHEET_WIDTH
    content_width = box_width - 2 * HEADER_MARGIN_H
    if with_auth:
        auth_w = stringWidth("AUTH " + "X" * auth_size, "Courier-Bold", font_size)
        return (content_width - auth_w) / 2 - HEADER_GAP
    pagenum_w = stringWidth("0000", "Courier-Bold", font_size)
    return content_width - pagenum_w - HEADER_GAP


def max_codeword_len(
    font_size: float,
    a7: bool,
    with_auth: bool,
    auth_size: int = GROUP_SIZE,
) -> int:
    """
    Longest codeword that fits the page header *at font_size*, without
    colliding with the centered AUTH group or the right-aligned page number.

    Longer codewords are not necessarily rejected: draw_pad_page shrinks the
    codeword down to CODEWORD_MIN_FONT before giving up. Use
    fit_codeword_size() for the "will this print at all" question.
    """
    char_w = stringWidth("X", "Courier-Bold", font_size)
    return max(0, int(codeword_space(font_size, a7, with_auth, auth_size) // char_w))


def fit_codeword_size(
    codeword: str,
    font_size: float,
    a7: bool = False,
    with_auth: bool = True,
    auth_size: int = GROUP_SIZE,
) -> float | None:
    """
    Largest size at or below `font_size` at which `codeword` fits the header,
    or None if it will not fit even at CODEWORD_MIN_FONT.
    """
    if not codeword:
        return font_size
    # Courier is monospaced and scales linearly, so one measurement suffices.
    width_at_1pt = stringWidth(codeword, "Courier-Bold", 1)
    space = codeword_space(font_size, a7, with_auth, auth_size)
    size = min(font_size, space / width_at_1pt)
    return size if size >= CODEWORD_MIN_FONT else None


def max_auth_size(font_size: float, a7: bool = False, box_width: float = None) -> int:
    """
    Longest AUTH group that clears the right-aligned page number.

    AUTH and the page number are both kept at full size because they are
    read character by character, so they must not overprint.
    """
    if box_width is None:
        box_width = (SHEET_HEIGHT / 2) if a7 else SHEET_WIDTH
    content_width = box_width - 2 * HEADER_MARGIN_H
    pagenum_w = stringWidth("0000", "Courier-Bold", font_size)
    label_w = stringWidth("AUTH ", "Courier-Bold", font_size)
    char_w = stringWidth("X", "Courier-Bold", font_size)
    # AUTH is centred, so it has to clear the page number on both sides.
    room = content_width - 2 * (pagenum_w + HEADER_GAP) - label_w
    return max(0, int(room // char_w))


def max_body_font_size(a7: bool = False, box_width: float = None) -> float:
    """
    Largest body font size at which a row of groups still fits across the page.

    calc_max_chars only measures height. Past this size the inter-group gap
    goes negative and the groups overprint one another, unreadably and
    identically on both copies.
    """
    if box_width is None:
        box_width = (SHEET_HEIGHT / 2) if a7 else SHEET_WIDTH
    groups = A7_GROUPS_PER_ROW if a7 else A6_GROUPS_PER_ROW
    content_width = box_width - 2 * HEADER_MARGIN_H
    width_per_point = stringWidth("X" * GROUP_SIZE, "Courier", 1) * groups
    return content_width / width_per_point


def max_fitted_codeword_len(
    font_size: float,
    a7: bool,
    with_auth: bool,
    auth_size: int = GROUP_SIZE,
) -> int:
    """
    Longest codeword that can be printed at all, allowing for shrinking down
    to CODEWORD_MIN_FONT. The AUTH group and page number stay at font_size,
    so only the codeword's own width shrinks.
    """
    char_w = stringWidth("X", "Courier-Bold", CODEWORD_MIN_FONT)
    return max(0, int(codeword_space(font_size, a7, with_auth, auth_size) // char_w))


def calc_max_chars(font_size: float, a7: bool = False, box_height: float = None) -> int:
    """
    Calculate maximum characters that fit on one pad page.

    `box_height` overrides the assumed page height for imposed layouts: a
    Letter quarter-sheet is shorter than A6, and measuring against A6 would
    push the last key rows off the box.
    """
    margin_top = 5 * mm
    margin_bottom = 4 * mm
    header_space = 4 * mm
    footer_space = 2 * mm

    if a7:
        # A7 portrait: 74mm wide x 105mm tall (half of landscape A6)
        page_height = SHEET_WIDTH  # 105mm
        chars_per_row = A7_CHARS_PER_ROW
    else:
        page_height = SHEET_HEIGHT
        chars_per_row = A6_CHARS_PER_ROW
    if box_height is not None:
        page_height = box_height

    available = page_height - margin_top - margin_bottom - header_space - footer_space
    min_spacing = font_size * 0.38 * mm
    max_rows = int(available / min_spacing)
    return max_rows * chars_per_row


def main():
    parser = argparse.ArgumentParser(description="Generate OTP pad sets as pocket-sized PDFs")
    parser.add_argument("--codewords", help="Path to codewords file (one per line)")
    parser.add_argument("--sets", type=int, default=10, help="Number of sets to generate")
    parser.add_argument("--pages", type=int, default=1000, help="Pad pages per set")
    parser.add_argument("--output", default="./output", help="Output directory")
    parser.add_argument("--chars", type=int, default=None, help="Key chars per pad page (default: 665 for A6, 375 for A7)")
    parser.add_argument("--fontsize", type=float, default=None, help="Font size in pt (default: 9)")
    parser.add_argument("--a7", action="store_true", help="Two pad pages per A6 sheet (cut to get A7)")
    parser.add_argument("--a4", action="store_true",
                        help="Four A6 pad pages per A4 sheet, imposed cut-and-stack "
                             "with crop marks (guillotine the stack twice, then pile "
                             "the four stacks in order)")
    parser.add_argument("--letter", action="store_true",
                        help="Like --a4 but on US Letter")
    parser.add_argument("--no-auth", action="store_true", help="Omit the AUTH group from page headers")
    parser.add_argument("--training", action="store_true", help="Watermark every page as TRAINING material")
    parser.add_argument("--worksheets", type=int, default=0,
                        help="Also generate N A5 worksheet pages as WORKSHEETS.pdf")
    parser.add_argument("--tabula", type=int, default=0, metavar="N",
                        help="Also generate N A6 tabula recta cards as TABULA_RECTA.pdf")
    parser.add_argument("--auth-size", type=int, default=GROUP_SIZE,
                        help=f"Letters in the AUTH group (default: {GROUP_SIZE})")
    parser.add_argument("--random-codewords", type=int, default=0, metavar="N",
                        help="Generate N random <MODIFIER>-<NOUN> codewords instead of "
                             "reading --codewords, and print them to stderr")
    parser.add_argument("--stdout", action="store_true",
                        help="Write the single generated PDF to stdout instead of a file, "
                             "so key material never reaches the filesystem "
                             "(e.g. otp_generator.py ... --stdout | lp)")
    parser.add_argument("--entropy", action="store_true",
                        help="Report which random source this machine will use "
                             "and exit (status 0 with a hardware TRNG, 2 without)")
    args = parser.parse_args()

    # Before the "nothing to do" check: --entropy is a complete request.
    if args.entropy:
        source = entropy_source()
        print(_source_note(source))
        if source != "hwrng+urandom":
            print(f"No usable {HWRNG_PATH}. Every Raspberry Pi has one; most "
                  f"laptops do not.")
        # An unseeded CRNG would make the first os.urandom call block.
        if crng_seeded():
            print("Kernel CSPRNG: seeded -- generation will start at once.")
        else:
            print("Kernel CSPRNG: NOT SEEDED. Generation would block inside "
                  "getrandom() until the kernel has collected enough noise. "
                  "Use the machine -- keys, disks, interrupts all help.")
        sys.exit(0 if source == "hwrng+urandom" else 2)

    with_auth = not args.no_auth
    auth_size = args.auth_size
    generating_sets = args.codewords is not None or args.random_codewords > 0

    if args.worksheets < 0:
        parser.error("--worksheets must be 0 or more")
    if args.tabula < 0:
        parser.error("--tabula must be 0 or more")
    if auth_size < 1:
        parser.error("--auth-size must be at least 1")
    if args.codewords and args.random_codewords:
        parser.error("use either --codewords or --random-codewords, not both")
    # --random-codewords N implies N sets; resolve it before anything counts them
    if args.random_codewords:
        args.sets = args.random_codewords
    if not generating_sets and args.worksheets == 0 and args.tabula == 0:
        parser.error("nothing to do \u2014 provide --codewords for pad sets and/or "
                     "--worksheets N and/or --tabula N")

    # --stdout emits one PDF, so it cannot disambiguate multiple outputs
    if args.stdout:
        requested = (args.sets if generating_sets else 0) + \
                    (1 if args.worksheets else 0) + (1 if args.tabula else 0)
        if requested != 1:
            parser.error("--stdout writes a single PDF: use exactly one of "
                         "--sets 1, --worksheets N, or --tabula N")

    if args.chars is not None and args.chars < 1:
        parser.error("--chars must be at least 1")
    # The page number is a fixed-width four-digit field on the page and in
    # the message header.
    if args.pages > 9999:
        parser.error("--pages cannot exceed 9999: the page number is a "
                     "four-digit field on the page and in the message header")
    if args.fontsize is not None and (not math.isfinite(args.fontsize)
                                      or args.fontsize <= 0):
        parser.error("--fontsize must be a positive number")
    if args.a7 and (args.a4 or args.letter):
        parser.error("--a7 lays out on A6 sheets; it cannot be combined with "
                     "--a4 or --letter")
    if args.a4 and args.letter:
        parser.error("use either --a4 or --letter, not both")

    # Defaults based on format
    if args.a7:
        font_size = args.fontsize or 9
        chars_per_page = args.chars or 375
        chars_per_row = A7_CHARS_PER_ROW
        groups_per_row = A7_GROUPS_PER_ROW
        format_label = "A7 (2-up on A6)"
    else:
        font_size = args.fontsize or 9
        chars_per_page = args.chars or 665
        chars_per_row = A6_CHARS_PER_ROW
        groups_per_row = A6_GROUPS_PER_ROW
        if args.a4:
            format_label = "A6 (4-up on A4, cut-and-stack)"
        elif args.letter:
            format_label = "A6 (4-up on Letter, cut-and-stack)"
        else:
            format_label = "A6"

    # With --stdout the PDF owns stdout, so all chatter goes to stderr.
    stream = sys.stderr if args.stdout else sys.stdout

    def log(*parts):
        print(*parts, file=stream)

    if not args.stdout:
        os.makedirs(args.output, exist_ok=True)

    if generating_sets:
        if args.sets < 1:
            log("ERROR: --sets must be at least 1")
            sys.exit(1)
        if args.pages < 1:
            log("ERROR: --pages must be at least 1")
            sys.exit(1)

        # An unseeded CRNG makes os.urandom block; say so rather than stop
        # dead with no output.
        def announce(seconds):
            # Once, not once per poll.
            if seconds == 0.0:
                log("Waiting for the kernel CSPRNG to be seeded. No key "
                    "material can be drawn until it is; using the machine "
                    "-- keys, disks, interrupts -- helps.")

        waited_already = [False]

        def ensure_entropy():
            """Block until the CRNG is seeded, at most once, saying so.

            Called immediately before the first random draw of each path,
            so deterministic errors (a missing or invalid codeword file)
            are reported without waiting.
            """
            if waited_already[0]:
                return
            waited_already[0] = True
            waited = wait_for_crng(on_wait=announce)
            if waited:
                log(f"Kernel CSPRNG seeded after {waited:.0f}s.")

        if args.random_codewords:
            # The first draw on this path.
            ensure_entropy()
            codewords = random_codewords(args.random_codewords)
            log(f"Random codewords: {' '.join(codewords)}")
        else:
            codewords = load_codewords(args.codewords)
        if len(codewords) < args.sets:
            log(f"ERROR: Need {args.sets} codewords but file only contains {len(codewords)}")
            sys.exit(1)

        # Codewords become filenames, and each set must be unique
        seen = set()
        for word in codewords[:args.sets]:
            if word in seen:
                log(f"ERROR: Duplicate codeword '{word}' \u2014 its set would overwrite the previous PDF")
                sys.exit(1)
            seen.add(word)
            # ASCII only. str.isalnum() is true for every Unicode letter,
            # but the page font is WinAnsi Courier and reportlab substitutes
            # silently, so distinct codewords could print the same header.
            if not all((ch.isascii() and ch.isalnum()) or ch in "-_" for ch in word):
                log(f"ERROR: Codeword '{word}' is unsafe as a filename (use A-Z, 0-9, '-', '_')")
                sys.exit(1)

        # Imposed layouts get a quarter of the sheet, which on Letter is
        # shorter and wider than A6, so measure against that box.
        box_height = box_width = None
        if args.a4 or args.letter:
            sheet_size = LETTER if args.letter else A4
            box_height, box_width = sheet_size[1] / 2, sheet_size[0] / 2

        # AUTH first: an oversized AUTH group squeezes the codeword's
        # budget, and the codeword check would then blame the codeword.
        auth_limit = max_auth_size(font_size, args.a7, box_width)
        if with_auth and auth_size > auth_limit:
            log(f"ERROR: an AUTH group of {auth_size} would overprint the page "
                f"number on {format_label}. Maximum is {auth_limit}.")
            sys.exit(1)

        # Codewords shrink to fit the header, so the limit is what fits at
        # the smallest legible size, not at the body font size.
        codeword_limit = max_fitted_codeword_len(font_size, args.a7, with_auth, auth_size)
        for word in codewords[:args.sets]:
            if fit_codeword_size(word, font_size, args.a7, with_auth, auth_size) is None:
                log(f"ERROR: Codeword '{word}' is too long for the {format_label} header "
                    f"(max {codeword_limit} characters)")
                sys.exit(1)

        # Width before height: see max_body_font_size.
        max_font = max_body_font_size(args.a7, box_width)
        if font_size > max_font:
            # Round down, so the maximum quoted in the error is itself
            # accepted when retyped.
            usable = math.floor(max_font * 10) / 10
            log(f"ERROR: {font_size}pt is too wide for {format_label} — "
                f"the five-letter groups would overlap. Maximum is {usable}pt.")
            sys.exit(1)

        max_chars = calc_max_chars(font_size, args.a7, box_height)
        if chars_per_page > max_chars:
            log(f"ERROR: {chars_per_page} chars won't fit on {format_label} at {font_size}pt. Maximum is {max_chars}.")
            sys.exit(1)

        num_rows = -(-chars_per_page // chars_per_row)

        log(f"Format: {format_label}")
        # Named before generating, because it decides whether these pages
        # are a one-time pad or a stream cipher's keystream.
        log(f"Key source: {_source_note(entropy_source())}")
        log(f"Generating {args.sets} OTP sets, {args.pages} pad pages each, {chars_per_page} chars/page")
        log(f"Auth group in header: {'yes' if with_auth else 'no'}")
        log(f"Training pads: {'yes' if args.training else 'no'}")
        log(f"Font: Courier {font_size}pt")
        log(f"Layout: {num_rows} rows of {groups_per_row}x{GROUP_SIZE} groups ({chars_per_row} chars/row)")
        log(f"Max chars per pad page at this font size: {max_chars}")
        if args.a7:
            num_sheets = -(-args.pages // 2)
            log(f"Paper: {num_sheets} A6 sheets per set (cut vertically to separate)")
        log(f"Output: {'<stdout>' if args.stdout else args.output}")
        log()

        if args.a7:
            generate = generate_set_pdf_a7
        elif args.a4 or args.letter:
            sheet = LETTER if args.letter else A4
            def generate(out, *rest, **kwargs):
                return generate_set_pdf_a4(out, *rest, page_size=sheet, **kwargs)
        else:
            generate = generate_set_pdf_a6

        # The first draw on the --codewords path is the pad itself. A no-op
        # if --random-codewords already waited.
        ensure_entropy()

        for i in range(args.sets):
            codeword = codewords[i]
            log(f"Set {i + 1}/{args.sets}: {codeword}")
            if args.stdout:
                generate(sys.stdout.buffer, codeword, args.pages, chars_per_page,
                         font_size, with_auth, args.training, auth_size,
                         progress=_printing_progress(codeword, stream))
                log("  Written to stdout")
            else:
                filepath = os.path.join(args.output, f"{codeword}.pdf")
                generate(filepath, codeword, args.pages, chars_per_page,
                         font_size, with_auth, args.training, auth_size)
                log(f"  Saved: {filepath}")
            log()

    if args.worksheets:
        log(f"Worksheets: {args.worksheets} A5 pages")
        if args.stdout:
            generate_worksheets_pdf(sys.stdout.buffer, args.worksheets)
            log("  Written to stdout")
        else:
            worksheet_path = os.path.join(args.output, "WORKSHEETS.pdf")
            generate_worksheets_pdf(worksheet_path, args.worksheets)
            log(f"  Saved: {worksheet_path}")
        log()

    if args.tabula:
        log(f"Tabula recta: {args.tabula} A6 cards")
        if args.stdout:
            generate_tabula_recta_pdf(sys.stdout.buffer, A6, args.tabula)
            log("  Written to stdout")
        else:
            tabula_path = os.path.join(args.output, "TABULA_RECTA.pdf")
            generate_tabula_recta_pdf(tabula_path, A6, args.tabula)
            log(f"  Saved: {tabula_path}")
        log()

    log("=" * 50)
    # What the pages were actually made from, as recorded, not as
    # predicted: a TRNG that dies mid-run shows up only here.
    if generating_sets and TALLY.total:
        log(f"Key source: {TALLY.summary()}")
    log("DONE")
    log()
    if generating_sets:
        log("Each PDF contains one complete set.")
        if args.stdout:
            # Re-running produces different key material, not the twin, so
            # both copies must come from this one job. Collate=True is
            # required: without it cups-filters interleaves the copies
            # into one stack.
            log("You piped one copy. A pad set is two IDENTICAL copies, so")
            log("ask the printer for both from this one job:")
            log("  ... --stdout | lp -n 2 -o Collate=True")
            log("Collate=True keeps them as two pads rather than")
            log("interleaving them into one stack. Re-running would make")
            log("a DIFFERENT pad, not the twin.")
        else:
            log("PRINT EACH PDF TWICE to get your A and B copies.")
        if args.a7:
            log("Cut each sheet vertically along the dashed line")
            log("to separate into A7 pad pages.")
        log("Store both copies in a sealed envelope labeled")
        log("with the codeword. Destroy this digital data")
        log("and wipe the generation machine when finished.")
    if args.worksheets:
        log("Worksheets contain no key material \u2014 print as many")
        log("copies as you need.")
    if args.tabula:
        log("Tabula recta cards contain no key material \u2014 print as many")
        log("copies as you need.")


if __name__ == "__main__":
    main()

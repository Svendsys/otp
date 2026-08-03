"""Print jobs.

The pad-pair job is the one that must not be got wrong. A pad set is two
identical copies, A and B; the manual is explicit that there is no backup
and no third copy. So the PDF is generated exactly once into memory and
submitted twice from the same buffer -- generating it twice would produce
two different pads wearing the same codeword.

Key material lives in a bytearray, never a file, and is zeroed when the job
ends. That only holds because the unit runs without swap; otherwise the
kernel could page the buffer to the SD card behind our backs.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from enum import Enum

import otp_generator as gen

from . import printer as printer_mod
from .config import Settings


class JobKind(Enum):
    PAD_PAIR = "pad pair"
    WORKSHEETS = "worksheets"
    TABULA = "tabula recta"
    MANUAL = "manual"
    TEST_PAGE = "test page"


MANUAL_PDF = "/opt/otp-unit/assets/otp-manual-a5.pdf"


@dataclass
class JobSpec:
    kind: JobKind
    codeword: str = ""
    settings: Settings = None
    count: int = 1

    @property
    def copies(self) -> int:
        """Only a pad is a pair; everything else prints once."""
        return 2 if self.kind is JobKind.PAD_PAIR else 1

    @property
    def carries_key_material(self) -> bool:
        return self.kind is JobKind.PAD_PAIR


def zero(buffer: bytearray) -> None:
    """Overwrite a key-material buffer in place."""
    for i in range(len(buffer)):
        buffer[i] = 0


def generate(spec: JobSpec, progress=None, should_cancel=None) -> bytearray:
    """Render a job to PDF bytes in memory. Never touches the filesystem."""
    settings = spec.settings or Settings()
    sink = io.BytesIO()

    if spec.kind is JobKind.PAD_PAIR:
        extra = {}
        if settings.a7:
            build = gen.generate_set_pdf_a7
        elif settings.imposed:
            # Four A6 pad pages per sheet, imposed cut-and-stack so the four
            # piles left by the guillotine are each already in page order.
            build = gen.generate_set_pdf_a4
            extra["page_size"] = gen.LETTER if settings.paper == "LETTER" else gen.A4
        else:
            build = gen.generate_set_pdf_a6
        build(
            sink, spec.codeword, settings.pages, settings.chars_per_page,
            settings.font_size, settings.with_auth, settings.training,
            settings.auth_size, progress=progress, should_cancel=should_cancel,
            **extra,
        )
    elif spec.kind is JobKind.WORKSHEETS:
        gen.generate_worksheets_pdf(sink, spec.count)
    elif spec.kind is JobKind.TABULA:
        gen.generate_tabula_recta_pdf(sink, copies=spec.count)
    elif spec.kind is JobKind.TEST_PAGE:
        _draw_test_page(sink)
    elif spec.kind is JobKind.MANUAL:
        with open(MANUAL_PDF, "rb") as handle:
            return bytearray(handle.read())
    else:
        raise ValueError(f"unknown job kind: {spec.kind}")

    return bytearray(sink.getbuffer())


def _draw_test_page(sink) -> None:
    """
    A single page that proves the printer works before a long run.

    Deliberately exercises what a pad page needs: Courier at the body size,
    the header layout, and the alternating shading bands. A printer that
    renders this correctly will render a pad.
    """
    from reportlab.lib.pagesizes import A6
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    width, height = A6
    c = canvas.Canvas(sink, pagesize=A6)
    c.setTitle("OTP PRINTER TEST")
    c.setFont("Courier-Bold", 11)
    c.drawCentredString(width / 2, height - 14 * mm, "OTP PRINT UNIT")
    c.setFont("Courier", 8)
    c.drawCentredString(width / 2, height - 20 * mm, "PRINTER TEST PAGE")

    c.setFont("Courier", 9)
    y = height - 32 * mm
    for row in range(8):
        if row % 2 == 1:
            c.saveState()
            c.setFillGray(0.88)
            c.rect(8 * mm, y - 1 * mm, width - 16 * mm, 4 * mm, fill=1, stroke=0)
            c.restoreState()
        c.drawString(10 * mm, y, " ".join(["ABCDE", "FGHIJ", "KLMNO", "PQRST"]))
        y -= 6 * mm

    c.setFont("Courier", 6)
    c.drawCentredString(width / 2, 12 * mm, "IF THESE LETTERS ARE SHARP AND")
    c.drawCentredString(width / 2, 9 * mm, "THE BANDS ARE EVEN, PADS WILL PRINT")
    c.showPage()
    c.save()


class PadPairJob:
    """
    Drives one job to completion, one step at a time.

    Kept as an explicit state machine rather than a loop so the UI can render
    between steps and the operator can be prompted to lift copy A off the
    tray before copy B starts.
    """

    def __init__(self, spec: JobSpec, cups: printer_mod.Cups, queue: str = printer_mod.QUEUE):
        self.spec = spec
        self.cups = cups
        self.queue = queue
        self._buffer: bytearray | None = None
        self.copies_done = 0

    def generate(self, progress=None, should_cancel=None) -> int:
        self._buffer = generate(self.spec, progress, should_cancel)
        return len(self._buffer)

    def print_next_copy(self) -> str:
        if self._buffer is None:
            raise RuntimeError("generate() must run before printing")
        if self.copies_done >= self.spec.copies:
            raise RuntimeError("all copies already submitted")
        letter = "AB"[self.copies_done] if self.spec.copies > 1 else ""
        title = f"{self.spec.codeword or self.spec.kind.value}{' ' + letter if letter else ''}"
        # Pad pages and tabula cards are A6; worksheets and the manual are
        # already laid out for their own paper, so leave those to the driver.
        if self.spec.kind in (JobKind.PAD_PAIR, JobKind.TABULA, JobKind.TEST_PAGE):
            options = (self.spec.settings or Settings()).lp_options
        else:
            options = {}
        job = self.cups.submit(self._buffer, self.queue, title, options)
        self.copies_done += 1
        return job

    @property
    def done(self) -> bool:
        return self.copies_done >= self.spec.copies

    def finish(self) -> None:
        """Zero the key material and empty the spool. Safe to call twice."""
        if self._buffer is not None:
            zero(self._buffer)
            self._buffer = None
        if self.spec.carries_key_material:
            self.cups.purge(self.queue)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.finish()
        return False

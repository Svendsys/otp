"""Display back-ends for the print unit's front panel.

The unit ships with a 128x64 SSD1306 OLED, but nothing above this module
knows that. Screens draw into a Frame -- a list of text lines plus a couple
of decorations -- and a Display renders it. That keeps the whole UI testable
without hardware and gives `--sim` a terminal rendering for free.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field

WIDTH = 128
HEIGHT = 64

# The 6x8 bitmap font this panel's geometry is built on: 21 columns, 8 rows.
# Every number in this file depends on it; see _panel_font().
COLS = 21
ROWS = 8
# Pixels between the tops of consecutive rows. 8 * 8 = 64, exactly the panel.
ROW_HEIGHT = 8
# Rows start two pixels above the panel. Capital and digit ink occupies
# cell rows 2..9 in this font, so eight rows at a pitch of 8 fit the 64px
# panel only from -2. The one pixel still lost is a lowercase descender on
# the last row, which only ever holds an uppercase footer.
# tests/test_panel_pixels.py asserts the result over every real screen.
ROW_TOP = -2


def _panel_font():
    """
    The 6x8 bitmap font, explicitly, not ImageFont.load_default().

    Since Pillow 10.1 load_default() may return Aileron, a proportional
    TrueType face, and the 21-column grid is only correct for a monospace
    one. load_default_imagefont() (Pillow 10.4+) returns the bitmap font;
    on 10.1-10.3 there is nothing to fall back to.

    In that case the wrong font is returned, not refused: raising here is
    reported as "no OLED", which drops a working panel and sends the unit
    down the unattended path to print a pad nobody asked for. draw_frame
    fits each row in pixels, so a proportional face degrades to visible
    truncation. is_panel_font() tells which was returned.
    """
    from PIL import ImageFont

    loader = getattr(ImageFont, "load_default_imagefont", None)
    return loader() if loader is not None else ImageFont.load_default()


def is_panel_font(font) -> bool:
    """Whether this is the 6x8 bitmap face the 21x8 grid is built on."""
    from PIL import ImageFont

    return isinstance(font, ImageFont.ImageFont)


@dataclass
class Frame:
    """One screenful, in characters rather than pixels."""

    title: str = ""
    lines: list[str] = field(default_factory=list)
    # Index into `lines` to mark with a selection caret, if any.
    selected: int | None = None
    # 0.0-1.0 progress bar along the bottom, if any.
    progress: float | None = None
    footer: str = ""

    def rendered(self) -> list[str]:
        """The frame as the rows of characters a 128x64 panel would show."""
        body_rows = ROWS - (1 if self.title else 0) - (1 if self.footer else 0) \
            - (1 if self.progress is not None else 0)
        out = []
        if self.title:
            out.append(self.title[:COLS].ljust(COLS))
        # The caret column only exists on screens that have a selection;
        # otherwise every line gets the full 21 characters.
        for i, line in enumerate(self.lines[:body_rows]):
            if self.selected is None:
                out.append(line[:COLS].ljust(COLS))
            else:
                caret = ">" if i == self.selected else " "
                out.append(f"{caret}{line}"[:COLS].ljust(COLS))
        while len(out) < ROWS - (1 if self.footer else 0) - (1 if self.progress is not None else 0):
            out.append(" " * COLS)
        if self.progress is not None:
            filled = int(round(max(0.0, min(1.0, self.progress)) * (COLS - 2)))
            out.append("[" + "#" * filled + "-" * (COLS - 2 - filled) + "]")
        if self.footer:
            out.append(self.footer[:COLS].ljust(COLS))
        return out[:ROWS]


    def overflowing(self) -> list[str]:
        """
        Content too wide or too tall for the panel, so tests can catch it.

        A truncated string on a 21-column display is a real defect -- it is
        how "POWER-CYCLE THE PRINTER" silently becomes "POWER-CYCLE THE PRIN".
        """
        budget = COLS - (0 if self.selected is None else 1)
        too_wide = [t for t in (self.title, self.footer) if len(t) > COLS]
        too_wide += [line for line in self.lines if len(line) > budget]
        body_rows = ROWS - (1 if self.title else 0) - (1 if self.footer else 0) \
            - (1 if self.progress is not None else 0)
        if len(self.lines) > body_rows:
            too_wide.append(f"<{len(self.lines)} lines, only {body_rows} fit>")
        return too_wide


class Display:
    """Interface every back-end implements."""

    def show(self, frame: Frame) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class FakeDisplay(Display):
    """Records frames so tests can assert on what the operator would see."""

    def __init__(self):
        self.frames: list[Frame] = []

    def show(self, frame: Frame) -> None:
        self.frames.append(frame)

    @property
    def last(self) -> Frame | None:
        return self.frames[-1] if self.frames else None

    def text(self) -> str:
        """The current screen as one newline-joined string, for assertions."""
        return "\n".join(self.last.rendered()) if self.last else ""


class ConsoleDisplay(Display):
    """Draws the panel as an ASCII box in the terminal, for --sim."""

    def __init__(self, stream=None, hint=True):
        self.stream = stream or sys.stdout
        # The key hint is simulator furniture: on a real unit [q] must not
        # be advertised, since quitting ends the process.
        self.hint = hint

    def show(self, frame: Frame) -> None:
        width = shutil.get_terminal_size((80, 24)).columns
        pad = " " * max(0, (min(width, 60) - COLS - 4) // 2)
        border = f"{pad}+{'-' * (COLS + 2)}+"
        print("\033[2J\033[H", end="", file=self.stream)
        print(border, file=self.stream)
        for row in frame.rendered():
            print(f"{pad}| {row} |", file=self.stream)
        print(border, file=self.stream)
        if self.hint:
            print(f"\n{pad}  [u] up   [d] down   [k] ok   [K] hold-ok   [q] quit",
                  file=self.stream)
        else:
            print(f"\n{pad}  arrows move   ENTER select   SHIFT+K back",
                  file=self.stream)
        self.stream.flush()


class Ssd1306Display(Display):
    """The real panel: 128x64 SSD1306 over I2C, via luma.oled."""

    # 90 and 270 degrees turn the panel into 64x128, and the whole
    # character grid is landscape, so half of every line would be lost.
    # Refused, not half-drawn.
    SQUARE_ON = (0, 2)

    def __init__(self, port: int = 1, address: int = 0x3C, rotate: int = 0):
        # Before the hardware imports, so a bad argument is rejected
        # without opening a bus.
        if rotate not in self.SQUARE_ON:
            raise ValueError(
                f"rotate={rotate} gives a {HEIGHT}x{WIDTH} portrait panel, "
                f"but the {COLS}x{ROWS} character grid this UI is built on "
                f"needs {WIDTH}x{HEIGHT}; only {self.SQUARE_ON} are supported")

        self._port, self._address, self._rotate = port, address, rotate
        self._device = self._open()
        self._font = _panel_font()

    def _open(self):
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306

        return ssd1306(i2c(port=self._port, address=self._address),
                       rotate=self._rotate)

    def reopen(self) -> None:
        """Re-run the panel's init sequence after it lost power or sync."""
        self._device = self._open()

    def show(self, frame: Frame) -> None:
        from luma.core.render import canvas

        with canvas(self._device) as draw:
            self.draw_frame(draw, frame)

    def draw_frame(self, draw, frame: Frame) -> None:
        """
        Put a frame on a PIL draw context.

        Separate from show() so a test can draw into a framebuffer it can
        read back. Each row is fitted to the panel in pixels as well as
        characters, so a proportional font degrades to visible truncation
        instead of ink lost off the right edge. A no-op on the bitmap font.
        """
        for row, text in enumerate(frame.rendered()):
            # The bitmap face is Latin-1 and Pillow raises on anything
            # outside it, which would blank the whole frame over one
            # character in a printer's name.
            text = text.encode("latin-1", "replace").decode("latin-1")
            fitted, left = self._fit(text.rstrip())
            draw.text((left, row * ROW_HEIGHT + ROW_TOP), fitted,
                      font=self._font, fill=255)

    def _fit(self, text: str):
        """The longest prefix of `text` whose ink stays on the panel.

        Returns (text, x). Immediate for the bitmap face, which fits by
        construction. For a substituted font the row is rendered and the
        actual ink measured, because getlength and getbbox under-report
        what a proportional face rasterises. x is nudged right when a
        glyph has a negative left bearing, which trimming cannot fix.
        """
        if is_panel_font(self._font):
            return text, 0

        from PIL import Image, ImageDraw

        # Drawn at an offset, because PIL discards negative coordinates and
        # would hide a negative bearing.
        margin = WIDTH
        def ink(candidate: str):
            probe = Image.new("1", (WIDTH * 8, ROW_HEIGHT * 4), 0)
            ImageDraw.Draw(probe).text((margin, 0), candidate,
                                       font=self._font, fill=1)
            box = probe.getbbox()
            return None if box is None else (box[0] - margin, box[2] - margin)

        try:
            while text:
                extent = ink(text)
                if extent is None:
                    return text, 0
                left, right = extent
                nudge = max(0, -left)
                if right + nudge <= WIDTH:
                    return text, nudge
                text = text[:-1]
        except Exception:                        # noqa: BLE001
            # A font that cannot be rendered to a scratch image is not one
            # to second-guess; let the panel show whatever it shows.
            return text, 0
        return text, 0

    def close(self) -> None:
        try:
            self._device.cleanup()
        except Exception:
            # Never let a display teardown failure mask the real error.
            pass

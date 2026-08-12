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
# See _panel_font() -- it is no longer what PIL hands out by default, and
# every number in this file depends on it.
COLS = 21
ROWS = 8
# Pixels between the tops of consecutive rows. 8 * 8 = 64, exactly the panel.
ROW_HEIGHT = 8
# And two pixels up, which is not a fudge -- it is the only offset that
# fits. Measured over the whole printable set in this font: ink occupies
# cell rows 2..9 for capitals, digits and the punctuation the panel uses,
# and 1..10 once lowercase descenders are counted. Eight rows at a pitch of
# 8 therefore span 7*8 + 8 = 64 pixels of capital ink, which is the panel
# exactly -- but only if the block starts at -2. At the shipped 0 the last
# row ran to 67 and every footer lost its bottom pixels.
#
#   ROW_TOP    0    -1    -2    -3
#   off bottom 3     2     1     0
#   off top    0     0     0     2
#
# -2 is the minimum, and the pixel it still loses belongs to a lowercase
# descender on the LAST row only -- where the unit only ever puts an
# uppercase footer. tests/test_panel_pixels.py asserts the RESULT over
# every real screen rather than this reasoning, so a font change is caught
# even if the comment rots.
ROW_TOP = -2


def _panel_font():
    """
    The 6x8 bitmap font, explicitly -- NOT ImageFont.load_default().

    Pillow 10.1 changed what load_default() returns: where FreeType is
    available it now hands back Aileron, a PROPORTIONAL TrueType face. The
    unit installs python3-pil from apt (device/packages.txt), so it gets
    whatever the distro ships. Measured against Aileron on Pillow 12.3:

        'X' * 21    126 px   fits
        'M' * 21    189 px   61 px past the right edge
        'W' * 21    210 px   82 px past the right edge

    Frame.rendered() truncates to 21 CHARACTERS, which is the correct unit
    only for a monospace font. With a proportional one the panel silently
    cut lines off at the right edge -- and the taller face also pushed 43
    pixels of the footer off the bottom of a 64-pixel screen.

    Three eras, and the middle one is the trap:

        Pillow < 10.1    load_default() is already the bitmap font.
        Pillow 10.1-10.3 load_default() is Aileron and there is NO
                         load_default_imagefont() to ask for instead.
        Pillow >= 10.4   load_default_imagefont() returns the bitmap font.

    load_default_imagefont() arrived in 10.4, not 11 -- verified against the
    sdists: absent from 10.3.0/src/PIL/ImageFont.py, present at line 909 of
    10.4.0's. So on 10.1-10.3 there is nothing to fall back TO, and a
    getattr() fallback lands on the exact face this function exists to
    refuse. Ubuntu 24.04's python3-pil is 10.2.0, squarely inside the window.

    Hence the type check rather than a version check: whatever we are handed
    has to BE the bitmap font, and if it is not we refuse to start. A panel
    that quietly cuts "DESTROY PRINTED PAGES" off at the right edge is worse
    than a unit that will not boot and says why.
    """
    from PIL import ImageFont

    loader = getattr(ImageFont, "load_default_imagefont", None)
    font = loader() if loader is not None else ImageFont.load_default()
    # FreeTypeFont and ImageFont are siblings, not parent and child, so this
    # is an exact test for "the bitmap face" and not a subclass accident.
    if not isinstance(font, ImageFont.ImageFont):
        import PIL

        raise RuntimeError(
            f"Pillow {PIL.__version__} gave the panel a {type(font).__name__} "
            f"where the 6x8 bitmap font was required; a proportional face "
            f"overruns the 128x64 panel by up to 82px per row. Install "
            f"Pillow >= 10.4 (which has load_default_imagefont) or < 10.1 "
            f"(whose load_default is still the bitmap font)."
        )
    return font


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
        # The key hint is simulator furniture. On a real unit it told the
        # operator to press [q], which ended the process with status 0 --
        # and Restart=on-failure reads that as success and does not
        # restart. One advertised keystroke turned the appliance off.
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

    # 90 and 270 degrees turn the 128x64 panel into a 64x128 one, and every
    # number above -- COLS=21, ROWS=8, the whole grid -- is landscape. luma
    # swaps width and height for odd rotations, so draw_frame's 126px rows
    # were being cut to 64px: measured against a rotated dummy device, the
    # ink bbox for a full screen is (64,0,127,64) at rotate=1 and (1,0,64,64)
    # at rotate=3, i.e. half of every line gone and the other half of the
    # panel dark. Refused rather than half-drawn.
    SQUARE_ON = (0, 2)

    def __init__(self, port: int = 1, address: int = 0x3C, rotate: int = 0):
        # Before the hardware imports: a bad argument should be rejected
        # without opening a bus, and the check is worth testing on a machine
        # that has no luma.oled at all.
        if rotate not in self.SQUARE_ON:
            raise ValueError(
                f"rotate={rotate} gives a {HEIGHT}x{WIDTH} portrait panel, "
                f"but the {COLS}x{ROWS} character grid this UI is built on "
                f"needs {WIDTH}x{HEIGHT}; only {self.SQUARE_ON} are supported")

        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306

        self._device = ssd1306(i2c(port=port, address=address), rotate=rotate)
        self._font = _panel_font()

    def show(self, frame: Frame) -> None:
        from luma.core.render import canvas

        with canvas(self._device) as draw:
            self.draw_frame(draw, frame)

    def draw_frame(self, draw, frame: Frame) -> None:
        """
        Put a frame on a PIL draw context.

        Split out of show() so a test can drive the real drawing against a
        framebuffer it can read back. Everything above this module deals in
        characters; this is the only place the panel becomes pixels, and
        until tests/test_panel_pixels.py it was the only place with no
        coverage at all -- the SSD1306 init sequence ran under i2c-stub and
        nothing was ever drawn.
        """
        for row, text in enumerate(frame.rendered()):
            draw.text((0, row * ROW_HEIGHT + ROW_TOP), text.rstrip(),
                      font=self._font, fill=255)

    def close(self) -> None:
        try:
            self._device.cleanup()
        except Exception:
            # Never let a display teardown failure mask the real error.
            pass

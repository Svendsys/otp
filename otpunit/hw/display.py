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

# The 6x8 bitmap font that luma/PIL gives us by default: 21 columns, 8 rows.
COLS = 21
ROWS = 8


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

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def show(self, frame: Frame) -> None:
        width = shutil.get_terminal_size((80, 24)).columns
        pad = " " * max(0, (min(width, 60) - COLS - 4) // 2)
        border = f"{pad}+{'-' * (COLS + 2)}+"
        print("\033[2J\033[H", end="", file=self.stream)
        print(border, file=self.stream)
        for row in frame.rendered():
            print(f"{pad}| {row} |", file=self.stream)
        print(border, file=self.stream)
        print(f"\n{pad}  [u] up   [d] down   [k] ok   [K] hold-ok   [q] quit",
              file=self.stream)
        self.stream.flush()


class Ssd1306Display(Display):
    """The real panel: 128x64 SSD1306 over I2C, via luma.oled."""

    def __init__(self, port: int = 1, address: int = 0x3C, rotate: int = 0):
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306
        from PIL import ImageFont

        self._device = ssd1306(i2c(port=port, address=address), rotate=rotate)
        self._font = ImageFont.load_default()

    def show(self, frame: Frame) -> None:
        from luma.core.render import canvas

        with canvas(self._device) as draw:
            for row, text in enumerate(frame.rendered()):
                draw.text((0, row * 8), text.rstrip(), font=self._font, fill=255)

    def close(self) -> None:
        try:
            self._device.cleanup()
        except Exception:
            # Never let a display teardown failure mask the real error.
            pass

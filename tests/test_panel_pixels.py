"""The panel as pixels, which is how the operator actually reads it.

Everything above `otpunit/hw/display.py` deals in characters, and the whole
suite asserted characters. `Frame.overflowing()` catches a line longer than
21 of them; nothing ever checked that 21 characters are 128 pixels, or that
eight rows are 64 -- and `Ssd1306Display.show` was never executed at all.
The i2c-stub harness gets as far as the SSD1306 init sequence and then
fails on block writes, so the init ran and NOTHING WAS EVER DRAWN.

Route 1 from the issue: drive the real drawing code through luma's `dummy`
device, which renders to a PIL image, and assert on the framebuffer. No
kernel support, no I2C, fast suite.

WHAT IT FOUND, immediately, on Pillow 12.3:

  * `ImageFont.load_default()` has not been the 6x8 bitmap font since
    Pillow 10.1 -- with FreeType present it returns Aileron, which is
    PROPORTIONAL. `'W' * 21` is 210 pixels on a 128-pixel panel. Frame
    truncates to 21 characters, the right unit for a monospace font and
    the wrong one for this. The unit installs python3-pil from apt, so it
    gets whatever the distro ships.
  * The same face is taller, and put 43 pixels of the footer off the
    bottom of the screen.
  * Even with the bitmap font restored, drawing row r at r*8 put the
    eighth row's ink at 59..64 on a panel whose last line is 63, so every
    footer lost its bottom pixel row. Hence ROW_TOP.

WHAT THIS DOES NOT COVER, stated because the issue is explicit about it:
the I2C wire. Wrong command bytes or bad page addressing are invisible
here -- that is route 2, a virtual adapter with block-transfer support,
and it catches a disjoint set of failures. This catches the layout and
truncation bugs, which are the ones that actually get shipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

pytest.importorskip("luma.core")
pytest.importorskip("PIL")

from luma.core.device import dummy                # noqa: E402
from luma.core.render import canvas               # noqa: E402
from PIL import Image, ImageDraw                  # noqa: E402

from otpunit import codewords as cw               # noqa: E402
from otpunit import config, jobs, printer, ui     # noqa: E402
from otpunit.hw import display as display_mod     # noqa: E402
from otpunit.hw.buttons import FakeButtons, Press  # noqa: E402
from otpunit.hw.display import (COLS, HEIGHT, ROWS, ROW_HEIGHT, ROW_TOP,
                                WIDTH, Frame, FakeDisplay,
                                Ssd1306Display)   # noqa: E402


def panel():
    """A real Ssd1306Display with only the I2C constructor bypassed.

    `__new__` rather than a subclass, the same trick tests/test_hardware.py
    uses for GpioButtons: the drawing code under test must be the shipped
    one, and only the bus it cannot have is left out.
    """
    unit = Ssd1306Display.__new__(Ssd1306Display)
    unit._device = dummy(width=WIDTH, height=HEIGHT, mode="1")
    unit._font = display_mod._panel_font()
    return unit


def framebuffer(frame: Frame) -> Image.Image:
    """What the panel would be lit with, as a 1-bit image."""
    unit = panel()
    with canvas(unit._device) as draw:
        unit.draw_frame(draw, frame)
    return unit._device.image.convert("1")


#: Slack on every side of the panel in the oversized canvas below.
PAD = 400


class _Shifted:
    """An ImageDraw that adds PAD to every coordinate.

    So that `unclipped()` can see ink above and to the LEFT of the panel as
    well as below and right. Without it the oversized canvas started at
    (0, 0), PIL discarded anything drawn at a negative coordinate, and the
    measurement was blind in two of four directions -- which let ROW_TOP=-3
    pass while it clipped the top pixel off every title.
    """

    def __init__(self, draw):
        self._draw = draw

    def text(self, xy, *args, **kwargs):
        self._draw.text((xy[0] + PAD, xy[1] + PAD), *args, **kwargs)


def unclipped(frame: Frame) -> Image.Image:
    """
    The same drawing, in the middle of a canvas too big to clip anything.

    The comparison that makes "nothing is clipped" an observation rather
    than an assumption: ink out here that is not on the panel is ink the
    operator does not get.
    """
    unit = panel()
    image = Image.new("1", (WIDTH + 2 * PAD, HEIGHT + 2 * PAD), 0)
    unit.draw_frame(_Shifted(ImageDraw.Draw(image)), frame)
    return image


def off_panel(frame: Frame) -> list:
    """Every ink pixel outside the 128x64 panel, in panel coordinates."""
    image = unclipped(frame)
    pixels = image.load()
    wide, tall = image.size
    return [(x - PAD, y - PAD) for y in range(tall) for x in range(wide)
            if pixels[x, y] and not (PAD <= x < PAD + WIDTH
                                     and PAD <= y < PAD + HEIGHT)]


def lost_pixels(frame: Frame) -> int:
    return len(off_panel(frame))


# The glyphs whose ink reaches the bottom of the font cell. Measured, not
# assumed: everything else stops a row higher and fits exactly.
DESCENDERS = ",;_gjpqy"


def lit(image: Image.Image) -> int:
    return sum(1 for pixel in image.getdata() if pixel)


# --- the font the geometry rests on --------------------------------------


class TestTheFontIsTheOneTheLayoutAssumes:
    def test_it_is_not_pils_shifting_default(self):
        """
        The regression that started this. `load_default()` is a moving
        target across Pillow releases; the panel's 21x8 grid is not.
        """
        from PIL import ImageFont

        chosen = display_mod._panel_font()
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        assert measure.textlength("X" * COLS, font=chosen) <= WIDTH
        # Monospace, which is the property Frame's character truncation
        # depends on. A proportional face makes "21 columns" meaningless.
        widths = {measure.textlength(ch * COLS, font=chosen)
                  for ch in "XWMil. "}
        assert len(widths) == 1, f"the panel font is not monospace: {widths}"

    def test_a_full_row_of_the_widest_glyph_still_fits(self):
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        for ch in "WM@#_|":
            width = measure.textlength(ch * COLS, font=display_mod._panel_font())
            assert width <= WIDTH, f"{COLS} x {ch!r} is {width}px of {WIDTH}"

    def test_row_top_is_the_lowest_offset_that_keeps_the_glyphs_on_the_panel(self):
        """
        ROW_TOP derived rather than asserted, because the right value is a
        property of a font this project does not own.

        Ink spans ten cell rows once descenders count, and eight rows at a
        pitch of eight need sixty-six pixels of a sixty-four pixel panel --
        so two pixels are lost somewhere no matter what, and ROW_TOP only
        chooses where. The rule: take the LOWEST offset (least negative,
        so the least empty space at the top) that still puts every capital,
        digit and punctuation mark the panel uses fully inside the glass.
        Anything higher wastes a row at the top; anything lower cuts the
        bottom off the footer, which is where the button legend lives.
        """
        alphabet = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.:/()[]#>%")
        rows = [(alphabet * 3)[:COLS] for _ in range(ROWS)]

        class Alphabet(Frame):
            def rendered(self):
                return rows

        def fits(offset):
            saved = display_mod.ROW_TOP
            display_mod.ROW_TOP = offset
            try:
                return off_panel(Alphabet()) == []
            finally:
                display_mod.ROW_TOP = saved

        best = max(offset for offset in range(-8, 1) if fits(offset))
        assert ROW_TOP == best, (
            f"ROW_TOP is {ROW_TOP}; {best} is the lowest offset that keeps "
            f"every capital, digit and punctuation mark on the panel with "
            f"this font")

    def test_eight_rows_of_it_fit_the_height(self):
        assert ROWS * ROW_HEIGHT <= HEIGHT
        full = Frame(title="T" * COLS,
                     lines=["L" * COLS] * (ROWS - 2),
                     footer="F" * COLS)
        assert lost_pixels(full) == 0


# --- what reaches the glass ----------------------------------------------


class TestTheFrameReachesTheGlass:
    def test_a_blank_frame_leaves_the_panel_dark(self):
        # The control. Without it, a draw_frame that did nothing at all
        # would satisfy every clipping assertion in this file.
        assert lit(framebuffer(Frame())) == 0

    def test_every_row_of_a_full_frame_is_lit(self):
        frame = Frame(title="TITLE", lines=["ONE", "TWO", "THREE", "FOUR"],
                      footer="FOOTER")
        image = framebuffer(frame).load()
        rows = [text for text in frame.rendered() if text.strip()]
        assert len(rows) >= 6
        for index, text in enumerate(frame.rendered()):
            band = range(max(0, index * ROW_HEIGHT + ROW_TOP),
                         min(HEIGHT, (index + 1) * ROW_HEIGHT + ROW_TOP))
            ink = sum(1 for y in band for x in range(WIDTH) if image[x, y])
            if text.strip():
                assert ink > 0, f"row {index} ({text!r}) drew nothing"

    def test_the_rows_are_drawn_where_the_frame_puts_them(self):
        """
        Not a tautology: the reference is built with plain PIL at positions
        this test computes, so a draw_frame that stacked every row at y=0,
        reversed them, or dropped one would not match.
        """
        frame = Frame(title="ALPHA", lines=["BRAVO", "CHARLIE"],
                      footer="DELTA")
        reference = Image.new("1", (WIDTH, HEIGHT), 0)
        draw = ImageDraw.Draw(reference)
        for index, text in enumerate(frame.rendered()):
            draw.text((0, index * ROW_HEIGHT + ROW_TOP), text.rstrip(),
                      font=display_mod._panel_font(), fill=255)
        assert list(framebuffer(frame).getdata()) == list(reference.getdata())

    def test_two_different_screens_are_two_different_pictures(self):
        # Cheap, and it catches a draw_frame that renders a constant.
        one = framebuffer(Frame(title="ALPHA", lines=["ONE"]))
        two = framebuffer(Frame(title="BRAVO", lines=["TWO"]))
        assert list(one.getdata()) != list(two.getdata())

    def test_the_progress_bar_and_caret_are_drawn(self):
        bar = framebuffer(Frame(lines=["X"], progress=1.0))
        empty = framebuffer(Frame(lines=["X"], progress=0.0))
        assert lit(bar) > lit(empty)
        caret = framebuffer(Frame(lines=["A", "B"], selected=1))
        plain = framebuffer(Frame(lines=["A", "B"]))
        assert lit(caret) > lit(plain)


# --- every screen the unit actually shows --------------------------------


def make_app():
    class Cups(printer.Cups):
        def __init__(self):
            super().__init__(run=None)

        def devices(self):
            return [printer.Device("usb://Fake/Laser?serial=1", "Fake Laser")]

        def ensure_queue(self, device, name="OTP"):
            return name

        def submit(self, data, name="OTP", title="OTP", options=None):
            return "job-1"

        def active_jobs(self, name="OTP"):
            return 0

        def purge(self, name="OTP"):
            pass

    return ui.App(display=FakeDisplay(), buttons=FakeButtons([]), cups=Cups(),
                  settings=config.Settings(pages=2), vocabulary=cw.Vocabulary(),
                  config_path="/nonexistent", poll_seconds=0)


def longest_codeword(app) -> str:
    """The widest thing the shipped vocabulary can produce."""
    return cw.join(max(app.vocabulary.modifiers, key=len),
                   max(app.vocabulary.all_nouns, key=len))


def every_screen(app):
    """(label, Frame) for everything the panel can show.

    Deliberately built from the real screens rather than from hand-written
    frames: a screen added without a frame here is a screen nobody checked,
    and test_the_enumeration_covers_every_screen_class says so.
    """
    yield "main_menu", ui.main_menu().frame(app)
    yield "settings_menu", ui.settings_menu().frame(app)
    yield "wait_for_printer", ui.WaitForPrinter().frame(app)
    yield "codeword_menu", ui.codeword_menu(lambda a, w: None).frame(app)
    yield "codeword_roll", ui.CodewordRoll(lambda a, w: None).frame(app)
    yield "text_entry", ui.TextEntry("MODIFIER", lambda a, v: None).frame(app)
    yield "message", ui.Message("NO PRINTER", ["PLUG ONE IN AND", "POWER-CYCLE"]).frame(app)

    for menu in (ui.main_menu(), ui.settings_menu()):
        for index in range(len(menu.items)):
            menu.index = index
            yield f"menu[{index}]", menu.frame(app)

    for factory in [item[1] for item in ui.settings_menu().items[:-1]]:
        chooser = factory(app)
        for index in range(len(chooser.options)):
            chooser.index = index
            yield f"chooser[{index}]", chooser.frame(app)

    # Every RunJob stage, including the ones only reachable after a
    # failure. The codeword is the longest the vocabulary can roll, since
    # that is the string most likely to run off the edge.
    word = longest_codeword(app)
    spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, word,
                        config.Settings(pages=1000))
    screen = ui.RunJob(spec)
    for stage in ("confirm", "generating", "swap", "waiting",
                  "confirm_continue", "confirm_abandon", "abandoned",
                  "printing", "cancelled", "error", "done"):
        screen.stage = stage
        screen.done_pages = 137
        screen.error = "lp: out of paper, tray 1 jammed"
        screen.job = jobs.PadPairJob(spec, app.cups, "OTP")
        screen.job.copies_done = 1
        yield f"run_job[{stage}]", screen.frame(app)
        screen.queue_unknown = True
        yield f"run_job[{stage},unknown]", screen.frame(app)
        screen.queue_unknown = False


class TestNoScreenIsClippedAtTheEdges:
    def test_no_screen_is_clipped_horizontally(self):
        """
        Strict zero, and this is the one that was really broken. With the
        proportional default font a row of wide glyphs ran 82 pixels past
        the right edge, and Frame's 21-CHARACTER truncation could not see
        it because characters were the wrong unit.
        """
        app = make_app()
        over = {label: [p for p in off_panel(frame) if p[0] >= WIDTH]
                for label, frame in every_screen(app)}
        assert not any(over.values()), {k: v[:4] for k, v in over.items() if v}

    def test_the_only_vertical_loss_is_a_descender_tail_on_the_last_row(self):
        """
        The honest residual, stated exactly rather than rounded to zero.

        Ink spans ten cell rows once descenders count, and eight rows at a
        pitch of eight span sixty-six pixels of a sixty-four pixel panel.
        Two pixels have to go somewhere and ROW_TOP decides which; -2 puts
        the whole capital band inside the glass and leaves exactly one row
        -- y == 64 -- reachable only by ,;_gjpqy on the final line.

        The unit hits it in one place: "221,072 POSSIBLE" on the codeword
        roll screen, where the comma's tail loses its bottom pixel.
        """
        app = make_app()
        for label, frame in every_screen(app):
            for x, y in off_panel(frame):
                assert y == HEIGHT, (
                    f"{label}: ink at y={y}, more than one row below the "
                    f"panel -- ROW_TOP or the font has changed")

    def test_nothing_but_a_descender_is_ever_cut(self):
        """
        And prove the attribution rather than asserting it: strip the
        descenders out of each screen's last row and the loss goes to zero.
        A capital or a digit losing a pixel would survive the test above
        and must not survive this one.
        """
        app = make_app()
        for label, frame in every_screen(app):
            if not off_panel(frame):
                continue
            rows = frame.rendered()
            cleaned = [row if index < ROWS - 1 else
                       "".join(" " if ch in DESCENDERS else ch for ch in row)
                       for index, row in enumerate(rows)]

            class Cleaned(Frame):
                def rendered(self):
                    return cleaned

            assert off_panel(Cleaned()) == [], (
                f"{label}: something other than a descender is clipped")

    def test_the_longest_codeword_fits_every_screen_that_shows_one(self):
        """
        The issue's named case. A codeword is the one panel string built at
        run time from a 600-word vocabulary, so it is the one nobody sees
        at its worst until the day it rolls.
        """
        app = make_app()
        word = longest_codeword(app)
        assert len(word) >= 10, f"vocabulary is suspiciously short: {word}"
        showing = [(label, frame) for label, frame in every_screen(app)
                   if any(word in row for row in frame.rendered())]
        assert showing, f"no screen renders the codeword {word!r} in full"
        for label, frame in showing:
            assert lost_pixels(frame) == 0, label
            # And it must survive Frame's own truncation intact -- pixels
            # that fit are no comfort if the characters were cut first.
            assert any(word in row for row in frame.rendered()), label

    def test_the_enumeration_covers_every_screen_class(self):
        """
        A screen added to ui.py and not to every_screen() is a screen this
        file silently does not check.
        """
        seen = set()
        app = make_app()
        for _, frame in every_screen(app):
            seen.add(type(frame).__name__)
        classes = {name for name, value in vars(ui).items()
                   if isinstance(value, type)
                   and issubclass(value, ui.Screen)
                   and value is not ui.Screen}
        # Every Screen subclass has to be reachable from the enumeration
        # above, by name, in the labels it yields.
        labels = " ".join(label for label, _ in every_screen(app)).lower()
        missing = [name for name in classes
                   if name.lower().replace("screen", "") not in
                   labels.replace("_", "")]
        assert not missing, f"screens with no pixel coverage: {missing}"


# --- the guard on the guard ----------------------------------------------


class TestTheMeasurementCanSeeClipping:
    """
    Everything above passes trivially if `lost_pixels` always returns 0.
    """

    def test_a_row_too_wide_for_the_panel_is_counted(self):
        # Straight past Frame's character truncation, to prove the PIXEL
        # check is what is doing the work.
        class Overflowing(Frame):
            def rendered(self):
                return ["X" * 60] + [""] * (ROWS - 1)

        assert lost_pixels(Overflowing()) > 0

    def test_ink_above_the_panel_is_counted(self):
        # The direction the first version of this measurement was blind
        # in: PIL simply discards a negative coordinate, so a ROW_TOP that
        # sliced the top off every title read as zero loss.
        class TooHigh(Frame):
            def rendered(self):
                return ["X" * 4] * ROWS

        unit = panel()
        image = Image.new("1", (WIDTH + 2 * PAD, HEIGHT + 2 * PAD), 0)
        shifted = _Shifted(ImageDraw.Draw(image))
        shifted.text((0, -20), "XXXX", font=unit._font, fill=255)
        pixels = image.load()
        assert any(pixels[x, y] for y in range(PAD - 30, PAD)
                   for x in range(PAD, PAD + 40)), \
            "the oversized canvas cannot see ink above the panel"

    def test_a_row_below_the_panel_is_counted(self):
        class TooTall(Frame):
            def rendered(self):
                return [""] * ROWS + ["X" * 4]

        assert lost_pixels(TooTall()) > 0

    def test_frames_own_truncation_is_what_keeps_a_long_line_on_the_panel(self):
        """
        Ties the character truncation to the pixel outcome. Frame cuts each
        line to 21 characters; this asserts that is the thing standing
        between a 60-character string and ink off the right-hand edge, by
        putting one through the REAL Frame rather than a subclass.
        """
        assert off_panel(Frame(title="T" * 60, lines=["X" * 60] * 4,
                               footer="F" * 60)) == []

    def test_pils_shifting_default_font_would_be_caught(self):
        """
        The actual regression, reproduced: with `load_default()` on a
        Pillow that returns Aileron, a full row overflows the panel. If
        this ever stops overflowing, load_default() has become safe again
        and _panel_font() can be reconsidered.
        """
        from PIL import ImageFont

        default = ImageFont.load_default()
        if not hasattr(ImageFont, "load_default_imagefont"):
            pytest.skip("this Pillow has no TTF default to be caught by")
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        assert measure.textlength("W" * COLS, font=default) > WIDTH, (
            "load_default() now fits 21 columns; if it is monospace again, "
            "_panel_font() may no longer be needed")

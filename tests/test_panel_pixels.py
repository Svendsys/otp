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
from otpunit.hw.buttons import FakeButtons        # noqa: E402
from otpunit.hw.display import (COLS, HEIGHT, ROWS, ROW_HEIGHT, ROW_TOP,
                                WIDTH, Frame, FakeDisplay,
                                Ssd1306Display)   # noqa: E402


def _a_proportional_font():
    """The face Pillow 10.1+ hands back from load_default(), or None.

    None means this build has no FreeType, so load_default() can only ever
    return the bitmap font and the hazard being simulated cannot arise.
    """
    from PIL import ImageFont

    try:
        candidate = ImageFont.load_default(size=10)
    except (TypeError, OSError):  # Pillow < 10.1 takes no size argument.
        return None
    return None if isinstance(candidate, ImageFont.ImageFont) else candidate


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

    def rectangle(self, xy, *args, **kwargs):
        (x0, y0), (x1, y1) = xy[:2], xy[2:] if len(xy) == 4 else xy[1]
        self._draw.rectangle((x0 + PAD, y0 + PAD, x1 + PAD, y1 + PAD),
                             *args, **kwargs)

    def __getattr__(self, name):
        # Deliberately NOT a transparent forward. An unshifted primitive
        # would draw at panel coordinates on a padded canvas and quietly
        # report ink in the wrong place -- a measurement that lies is worse
        # than one that stops. Shift it here when draw_frame starts using it.
        raise NotImplementedError(
            f"_Shifted does not translate ImageDraw.{name}() yet; add it "
            f"with the PAD offset applied, or every clipping measurement "
            f"taken through it will be off by {PAD}px")


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
    """Every ink pixel outside the 128x64 panel, in panel coordinates.

    Erase the panel rectangle and ask what ink is left. getbbox() answers
    that in one C-level pass; the per-pixel walk then only runs over the
    offending region, and only when there is one. The previous form did
    801,792 Python-level `pixels[x, y]` lookups on every call -- 55ms each,
    which was essentially the whole runtime of this file.
    """
    image = unclipped(frame)
    ImageDraw.Draw(image).rectangle(
        (PAD, PAD, PAD + WIDTH - 1, PAD + HEIGHT - 1), fill=0)
    box = image.getbbox()
    if box is None:
        return []
    left, top, right, bottom = box
    pixels = image.load()
    return [(x - PAD, y - PAD) for y in range(top, bottom)
            for x in range(left, right) if pixels[x, y]]


def lost_pixels(frame: Frame) -> int:
    return len(off_panel(frame))


# The glyphs whose ink reaches the bottom of the font cell. Measured, not
# assumed: everything else stops a row higher and fits exactly.
DESCENDERS = ",;_gjpqy"


def lit(image: Image.Image) -> int:
    # histogram() rather than getdata(): the latter is deprecated and goes
    # away in Pillow 14, and this is a C-level pass instead of a Python one.
    return sum(image.histogram()[1:])


# --- the font the geometry rests on --------------------------------------


class TestTheFontIsTheOneTheLayoutAssumes:
    def test_it_is_not_pils_shifting_default(self):
        """
        The regression that started this. `load_default()` is a moving
        target across Pillow releases; the panel's 21x8 grid is not.
        """
        chosen = display_mod._panel_font()
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        assert measure.textlength("X" * COLS, font=chosen) <= WIDTH
        # Monospace, which is the property Frame's character truncation
        # depends on. A proportional face makes "21 columns" meaningless.
        widths = {measure.textlength(ch * COLS, font=chosen)
                  for ch in "XWMil. "}
        assert len(widths) == 1, f"the panel font is not monospace: {widths}"

    def test_a_pillow_with_no_bitmap_accessor_is_refused_not_accepted(
            self, monkeypatch):
        """
        The 10.1-10.3 window, simulated on whatever Pillow is installed.

        There, load_default() is Aileron and load_default_imagefont() does
        not exist yet -- it landed in 10.4, not 11. A getattr() fallback
        therefore returns the proportional face on exactly the versions the
        guard was written for, silently. Ubuntu 24.04 ships 10.2.0.

        So: strip the accessor, make load_default() hand back a proportional
        font, and require that _panel_font() REFUSES rather than returns it.
        """
        from PIL import ImageFont

        proportional = _a_proportional_font()
        if proportional is None:
            pytest.skip("no FreeType here, so there is no proportional face "
                        "for load_default() to have returned")

        monkeypatch.delattr(ImageFont, "load_default_imagefont", raising=False)
        monkeypatch.setattr(ImageFont, "load_default",
                            lambda *a, **k: proportional)
        with pytest.raises(RuntimeError, match="bitmap font"):
            display_mod._panel_font()

    def test_the_refusal_names_the_version_and_the_remedy(self, monkeypatch):
        """
        A unit that will not boot has to say why, or the operator is left
        with a dead panel and no thread to pull.
        """
        from PIL import ImageFont

        proportional = _a_proportional_font()
        if proportional is None:
            pytest.skip("no FreeType here")

        monkeypatch.delattr(ImageFont, "load_default_imagefont", raising=False)
        monkeypatch.setattr(ImageFont, "load_default",
                            lambda *a, **k: proportional)
        with pytest.raises(RuntimeError) as caught:
            display_mod._panel_font()
        message = str(caught.value)
        import PIL
        assert PIL.__version__ in message
        assert "10.4" in message and "10.1" in message

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
        # Every character has to be measured at the TOP row and at the
        # BOTTOM row, because those are the only two that can leave the
        # glass -- the six in between cannot clip whatever they hold. One
        # frame per 21-column window, each window repeated down all eight
        # rows, so all 48 characters sit at both extremes across the set.
        #
        # The earlier form was `[(alphabet * 3)[:COLS] for _ in range(ROWS)]`
        # -- the loop variable unused, so all eight rows were the same first
        # 21 characters and 27 of the 48 were never drawn at all. That left
        # the bound resting entirely on Q's tail; drop Q and the derivation
        # returned -1, at which the untested "()[]/#" clip off the bottom.
        windows = [alphabet[i:i + COLS].ljust(COLS)
                   for i in range(0, len(alphabet), COLS)]
        assert set("".join(windows)) >= set(alphabet), \
            "the windows do not cover the alphabet; coverage is a lie"

        def frame_for(window):
            class Alphabet(Frame):
                def rendered(self):
                    return [window] * ROWS
            return Alphabet()

        def fits(offset):
            saved = display_mod.ROW_TOP
            display_mod.ROW_TOP = offset
            try:
                return all(off_panel(frame_for(w)) == [] for w in windows)
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
        assert framebuffer(frame).tobytes() == reference.tobytes()

    def test_two_different_screens_are_two_different_pictures(self):
        # Cheap, and it catches a draw_frame that renders a constant.
        one = framebuffer(Frame(title="ALPHA", lines=["ONE"]))
        two = framebuffer(Frame(title="BRAVO", lines=["TWO"]))
        assert one.tobytes() != two.tobytes()

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

    Every label is "ClassName/detail", taken from the live object rather
    than written out, for two reasons. The class half makes the coverage
    check an exact set comparison instead of a substring search that a new
    screen called Menu or Entry would satisfy by accident. The detail half
    is unique per frame, so a caller that collects into a dict cannot
    silently discard frames that shared a key -- which is how 28 of these
    72 used to go unexamined.
    """
    def tagged(screen, detail, frame=None):
        return f"{type(screen).__name__}/{detail}", frame or screen.frame(app)

    yield tagged(ui.main_menu(), "main")
    yield tagged(ui.settings_menu(), "settings")
    yield tagged(ui.WaitForPrinter(), "waiting")
    yield tagged(ui.codeword_menu(lambda a, w: None), "codeword")
    yield tagged(ui.CodewordRoll(lambda a, w: None), "roll")
    yield tagged(ui.TextEntry("MODIFIER", lambda a, v: None), "modifier")
    yield tagged(ui.Message("NO PRINTER", ["PLUG ONE IN AND", "POWER-CYCLE"]),
                 "no-printer")

    for name, menu in (("main", ui.main_menu()), ("settings", ui.settings_menu())):
        for index in range(len(menu.items)):
            menu.index = index
            yield tagged(menu, f"{name}[{index}]")

    for factory in [item[1] for item in ui.settings_menu().items[:-1]]:
        chooser = factory(app)
        name = (chooser.title or "untitled").lower().replace(" ", "-")
        for index in range(len(chooser.options)):
            chooser.index = index
            yield tagged(chooser, f"{name}[{index}]")

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
        yield tagged(screen, stage)
        # queue_unknown only reaches the frame on the `waiting` stage, so
        # yielding it for the other ten produced ten byte-identical
        # duplicates and paid for ten more full-canvas scans.
        if stage == "waiting":
            screen.queue_unknown = True
            yield tagged(screen, f"{stage},unknown")
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
        # A LIST, not a dict keyed by label: duplicate keys used to discard
        # 28 of the 72 frames before anything looked at them.
        over = [(label, [p for p in off_panel(frame) if p[0] >= WIDTH])
                for label, frame in every_screen(app)]
        assert not any(bad for _, bad in over), \
            [(label, bad[:4]) for label, bad in over if bad]

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
        # Candidates are screens that mention the codeword AT ALL -- matched
        # on a prefix, so a screen that cut it short is still a candidate and
        # still has to answer for it. Filtering on the full word (as this
        # once did) and then re-asserting the full word is vacuous: the
        # truncating screen drops out of the set instead of failing it.
        stem = word[:8]
        assert len(word) > len(stem), "codeword too short for a prefix probe"
        showing = [(label, frame) for label, frame in every_screen(app)
                   if any(stem in row for row in frame.rendered())]
        assert showing, f"no screen renders the codeword {word!r} at all"
        for label, frame in showing:
            assert lost_pixels(frame) == 0, label
            # And it must survive Frame's own truncation intact -- pixels
            # that fit are no comfort if the characters were cut first.
            assert any(word in row for row in frame.rendered()), (
                f"{label}: codeword truncated to "
                f"{[r for r in frame.rendered() if stem in r]}")

    def test_the_enumeration_covers_every_screen_class(self):
        """
        A screen added to ui.py and not to every_screen() is a screen this
        file silently does not check.
        """
        app = make_app()
        classes = {name for name, value in vars(ui).items()
                   if isinstance(value, type)
                   and issubclass(value, ui.Screen)
                   and value is not ui.Screen}
        # An exact set comparison against the class names the enumeration
        # actually produced. The old form searched for each class name as a
        # SUBSTRING of one flattened blob of labels, which a new screen
        # called Menu, Roll, Entry, Message or Wait would satisfy with zero
        # coverage -- five of the eight names already present pass that way.
        covered = {label.split("/", 1)[0] for label, _ in every_screen(app)}
        assert not classes - covered, \
            f"screens with no pixel coverage: {sorted(classes - covered)}"
        assert not covered - classes, \
            f"labels naming things that are not ui.Screen subclasses: " \
            f"{sorted(covered - classes)}"

    def test_no_two_screens_share_a_label(self):
        """
        Labels are how a failure names its screen, and how callers key
        results. When "chooser[0]" meant seven different frames, collecting
        into a dict kept one and dropped six -- 28 of 72 overall, none of
        them examined and nothing saying so.
        """
        app = make_app()
        labels = [label for label, _ in every_screen(app)]
        duplicates = {label for label in labels if labels.count(label) > 1}
        assert not duplicates, f"labels used more than once: {sorted(duplicates)}"


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

    def test_ink_above_the_panel_is_counted(self, monkeypatch):
        """
        The direction the first version of this measurement was blind in:
        PIL discards a negative coordinate, so a ROW_TOP that sliced the top
        off every title read as zero loss.

        This has to go through off_panel(), not just prove the padded canvas
        retains the ink. The earlier version built a TooHigh frame, never
        instantiated it, and asserted only on raw pixels -- so breaking
        off_panel's upper bound left it, and all four screen tests, green.
        """
        class TooHigh(Frame):
            def rendered(self):
                return ["X" * 4] * ROWS

        # Shove the whole block far enough up that row 0 clears the glass.
        monkeypatch.setattr(display_mod, "ROW_TOP", -20)
        lost = off_panel(TooHigh())
        assert lost, "ink above the panel was not counted"
        assert any(y < 0 for _, y in lost), \
            f"nothing reported above y=0; got {lost[:6]}"

    def test_a_portrait_rotation_would_have_cut_every_row_in_half(self):
        """
        Why Ssd1306Display refuses rotate 1 and 3, measured rather than
        argued. luma swaps width and height for odd rotations, so the 126px
        row draw_frame lays out has only 64px of canvas to land on.
        """
        frame = Frame(title="OTP PRINT UNIT", lines=["PRINT PAD PAIR"],
                      footer="1/7")
        widths = {}
        for rotation in (0, 1, 2, 3):
            unit = Ssd1306Display.__new__(Ssd1306Display)
            unit._device = dummy(width=WIDTH, height=HEIGHT, mode="1",
                                 rotate=rotation)
            unit._font = display_mod._panel_font()
            with canvas(unit._device) as draw:
                unit.draw_frame(draw, frame)
            widths[rotation] = unit._device.size[0]

        assert widths[0] == widths[2] == WIDTH
        assert widths[1] == widths[3] == HEIGHT, \
            "odd rotations no longer produce a portrait panel; the refusal " \
            "in Ssd1306Display.__init__ may no longer be needed"
        assert set(Ssd1306Display.SQUARE_ON) == {0, 2}

    def test_the_constructor_refuses_a_rotation_it_cannot_lay_out(self):
        for rotation in (1, 3):
            with pytest.raises(ValueError, match="portrait"):
                Ssd1306Display(rotate=rotation)

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
        # Gate on what load_default() IS, not on whether this Pillow happens
        # to offer the newer accessor. `hasattr(load_default_imagefont)` was
        # the wrong question: it is false on 10.1-10.3, which are precisely
        # the versions where load_default() IS the proportional face, so the
        # old gate skipped this test exactly where it had something to catch.
        if isinstance(default, ImageFont.ImageFont):
            pytest.skip("this Pillow's load_default() is still the bitmap font")
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        assert measure.textlength("W" * COLS, font=default) > WIDTH, (
            "load_default() now fits 21 columns; if it is monospace again, "
            "_panel_font() may no longer be needed")

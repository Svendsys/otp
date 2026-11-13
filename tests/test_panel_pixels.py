"""The panel as pixels, which is how the operator reads it.

The real `Ssd1306Display` drawing code runs through luma's `dummy` device,
which renders to a PIL image, and the tests assert on that framebuffer: 21
characters must be 128 pixels and eight rows 64. The hazards are Pillow's
`load_default()`, a proportional face since 10.1, and the row offset (ROW_TOP).

Not covered: the I2C wire. Wrong command bytes or page addressing are invisible
here.
"""
from __future__ import annotations

import contextlib
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
    """The face Pillow 10.1+ returns from load_default(), or None when this build
    has no FreeType and can only return the bitmap font.
    """
    from PIL import ImageFont

    try:
        candidate = ImageFont.load_default(size=10)
    except (TypeError, OSError):  # Pillow < 10.1 takes no size argument.
        return None
    return None if isinstance(candidate, ImageFont.ImageFont) else candidate


@contextlib.contextmanager
def _pillow_without_the_bitmap_accessor(monkeypatch):
    """Simulate Pillow 10.1-10.3: a proportional default and no
    load_default_imagefont(). Yields False when this build has no FreeType.
    """
    from PIL import ImageFont

    proportional = _a_proportional_font()
    if proportional is None:
        yield False
        return
    monkeypatch.delattr(ImageFont, "load_default_imagefont", raising=False)
    monkeypatch.setattr(ImageFont, "load_default", lambda *a, **k: proportional)
    yield True


def panel():
    """A real Ssd1306Display with only the I2C constructor bypassed, via
    `__new__`, so the drawing code under test is the shipped one.
    """
    unit = Ssd1306Display.__new__(Ssd1306Display)
    unit._device = dummy(width=WIDTH, height=HEIGHT, mode="1")
    unit._font = display_mod._panel_font()
    return unit


def framebuffer(frame: Frame) -> Image.Image:
    """What the panel would be lit with, as a 1-bit image.

    Goes through show() itself, so the canvas context that flushes the frame to
    the device is covered.
    """
    unit = panel()
    unit.show(frame)
    return unit._device.image.convert("1")


#: Slack on every side of the panel in the oversized canvas below.
PAD = 400


class _Shifted:
    """An ImageDraw that adds PAD to every coordinate, so `unclipped()` can see
    ink above and to the left of the panel as well as below and right.
    """

    def __init__(self, draw):
        self._draw = draw

    def text(self, xy, *args, **kwargs):
        self._draw.text((xy[0] + PAD, xy[1] + PAD), *args, **kwargs)

    def rectangle(self, xy, *args, **kwargs):
        # Accept both coordinate forms PIL does: flat (x0, y0, x1, y1) and a
        # pair of points.
        flat = [value for point in xy
                for value in (point if isinstance(point, (tuple, list))
                              else (point,))]
        x0, y0, x1, y1 = flat
        self._draw.rectangle((x0 + PAD, y0 + PAD, x1 + PAD, y1 + PAD),
                             *args, **kwargs)

    def __getattr__(self, name):
        # Not a transparent forward: an unshifted primitive would report ink in
        # the wrong place. Shift it here when draw_frame starts using it.
        raise NotImplementedError(
            f"_Shifted does not translate ImageDraw.{name}() yet; add it "
            f"with the PAD offset applied, or every clipping measurement "
            f"taken through it will be off by {PAD}px")


def unclipped(frame: Frame) -> Image.Image:
    """The same drawing in the middle of a canvas too big to clip anything. Ink
    out here that is not on the panel is ink the operator does not get.
    """
    unit = panel()
    image = Image.new("1", (WIDTH + 2 * PAD, HEIGHT + 2 * PAD), 0)
    unit.draw_frame(_Shifted(ImageDraw.Draw(image)), frame)
    return image


# Where the panel sits in the oversized canvas, half-open (PIL's paste
# convention). The one definition of "on the panel" for every test in this
# file.
PANEL_BOX = (PAD, PAD, PAD + WIDTH, PAD + HEIGHT)


def off_panel(frame: Frame = None, image: Image.Image = None) -> list:
    """Every ink pixel outside the 128x64 panel, in panel coordinates.

    Erases the panel rectangle and walks only the bounding box of what is left.
    `image` feeds a pre-built canvas through the same path, so the fencepost
    test pins PANEL_BOX itself.
    """
    image = image.copy() if image is not None else unclipped(frame)
    image.paste(0, PANEL_BOX)
    box = image.getbbox()
    if box is None:
        return []
    left, top, right, bottom = box
    pixels = image.load()
    return [(x - PAD, y - PAD) for y in range(top, bottom)
            for x in range(left, right) if pixels[x, y]]


def lost_pixels(frame: Frame) -> int:
    return len(off_panel(frame))


# The glyphs whose ink reaches the bottom of the font cell.
DESCENDERS = ",;_gjpqy"


def lit(image: Image.Image) -> int:
    # histogram(), since getdata() is deprecated.
    return sum(image.histogram()[1:])


# --- the font the geometry rests on --------------------------------------


class TestTheFontIsTheOneTheLayoutAssumes:
    def test_it_is_not_pils_shifting_default(self):
        """The panel font is monospace at the cell size the 21x8 grid assumes,
        whatever `load_default()` returns on this Pillow.
        """
        chosen = display_mod._panel_font()
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        assert measure.textlength("X" * COLS, font=chosen) <= WIDTH
        # Monospace is the property Frame's character truncation depends on.
        widths = {measure.textlength(ch * COLS, font=chosen)
                  for ch in "XWMil. "}
        assert len(widths) == 1, f"the panel font is not monospace: {widths}"

    def test_the_wrong_font_is_recognised_as_wrong(self, monkeypatch):
        """On Pillow 10.1-10.3 (simulated), where load_default() is proportional
        and load_default_imagefont() does not exist, the proportional face is
        detected.
        """
        with _pillow_without_the_bitmap_accessor(monkeypatch) as available:
            if not available:
                pytest.skip("no FreeType here, so load_default() could only "
                            "ever return the bitmap font")
            assert not display_mod.is_panel_font(display_mod._panel_font())

    def test_the_wrong_font_still_leaves_a_usable_panel(self, monkeypatch):
        """With the wrong font the panel degrades instead of raising, and no
        screen runs off the sides. Raising would read as "no OLED" and turn a
        working unit into a headless auto-printer.
        """
        with _pillow_without_the_bitmap_accessor(monkeypatch) as available:
            if not available:
                pytest.skip("no FreeType here")
            # No exception, and no screen runs off the sides, which is what a
            # proportional face causes.
            app = make_app()
            for label, frame in every_screen(app):
                sideways = [p for p in off_panel(frame)
                            if p[0] < 0 or p[0] >= WIDTH]
                assert not sideways, (
                    f"{label}: the fallback font put ink off the side of the "
                    f"panel at {sideways[:4]}; draw_frame is meant to fit "
                    f"each row to {WIDTH}px")

    def test_the_wrong_font_does_not_look_like_a_missing_panel(self, monkeypatch):
        """_panel_font() does not raise for the wrong font, so hmi.open_display,
        which treats any exception as "no panel", still hands back a display.
        """
        with _pillow_without_the_bitmap_accessor(monkeypatch) as available:
            if not available:
                pytest.skip("no FreeType here")
            unit = Ssd1306Display.__new__(Ssd1306Display)
            unit._device = dummy(width=WIDTH, height=HEIGHT, mode="1")
            # The step hmi.open_display would have died on.
            unit._font = display_mod._panel_font()
            unit.show(Frame(title="OTP PRINT UNIT", lines=["PRINT PAD PAIR"],
                            footer="1/7"))
            assert lit(unit._device.image.convert("1")) > 0, \
                "the panel drew nothing at all with the fallback font"

    def test_a_full_row_of_the_widest_glyph_still_fits(self):
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        for ch in "WM@#_|":
            width = measure.textlength(ch * COLS, font=display_mod._panel_font())
            assert width <= WIDTH, f"{COLS} x {ch!r} is {width}px of {WIDTH}"

    def test_row_top_is_the_lowest_offset_that_keeps_the_glyphs_on_the_panel(self):
        """ROW_TOP equals the least negative offset that keeps every capital,
        digit and punctuation mark on the glass. Eight rows need 66 pixels of
        64, so two are lost somewhere; higher wastes the top, lower cuts the
        footer.
        """
        alphabet = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -.:/()[]#>%")
        # Only the top and bottom rows can leave the glass, so each 21-column
        # window is repeated down all eight rows and every character is
        # measured at both extremes.
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
        # The control: a draw_frame that did nothing would satisfy every
        # clipping assertion.
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
        """Each row lands at the position computed here with plain PIL, so
        stacked, reversed or dropped rows do not match.
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


@contextlib.contextmanager
def _generator_returning(stub):
    """Swap ui.generator() for one with a fixed entropy reading, so the widest
    case is reproducible.
    """
    was = ui.generator
    ui.generator = lambda: stub
    try:
        yield
    finally:
        ui.generator = was


def every_screen(app):
    """(label, Frame) for everything the panel can show, built from the real
    screens.

    Labels are "ClassName/detail": the class half makes the coverage check an
    exact set comparison, the detail half is unique per frame.
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

    # WaitForEntropy's footer combines a kernel-supplied count with a spinner,
    # so take every spinner glyph against every count shape: "?" (unreadable),
    # 0, 256 and 4096, the widest.
    class _Pool:
        def __init__(self, bits):
            self.bits = bits

        def entropy_bits(self):
            return self.bits

    entropy = ui.WaitForEntropy()
    for bits in (None, 0, 256, 4096):
        pool = _Pool(bits)
        with contextlib.ExitStack() as stack:
            stack.enter_context(_generator_returning(pool))
            for phase in range(4):
                yield tagged(entropy, f"{bits}-bits[{phase}]")

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

    # Every RunJob stage, including those only reachable after a failure, with
    # the longest codeword the vocabulary can roll.
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
        # queue_unknown only affects the `waiting` frame.
        if stage == "waiting":
            screen.queue_unknown = True
            yield tagged(screen, f"{stage},unknown")
            screen.queue_unknown = False


class TestNoScreenIsClippedAtTheEdges:
    def test_no_screen_is_clipped_horizontally(self):
        """No screen puts ink past the right edge. Frame truncates by characters,
        which cannot see a font whose glyphs are wider than a cell.
        """
        app = make_app()
        # A list, not a dict keyed by label, so no frame is discarded.
        over = [(label, [p for p in off_panel(frame) if p[0] >= WIDTH])
                for label, frame in every_screen(app)]
        assert not any(bad for _, bad in over), \
            [(label, bad[:4]) for label, bad in over if bad]

    def test_the_only_vertical_loss_is_a_descender_tail_on_the_last_row(self):
        """The only vertical loss is the row y == 64, reachable only by a
        descender (,;_gjpqy) on the last line. Eight rows need 66 pixels of 64;
        ROW_TOP puts the loss there.
        """
        app = make_app()
        for label, frame in every_screen(app):
            for x, y in off_panel(frame):
                assert y == HEIGHT, (
                    f"{label}: ink at y={y}, more than one row below the "
                    f"panel -- ROW_TOP or the font has changed")

    def test_nothing_but_a_descender_is_ever_cut(self):
        """With descenders stripped from each last row the loss is zero, so no
        capital or digit ever loses a pixel.
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
        """The longest codeword the vocabulary can roll appears whole and
        unclipped on every screen that shows a codeword.
        """
        app = make_app()
        word = longest_codeword(app)
        assert len(word) >= 10, f"vocabulary is suspiciously short: {word}"
        # Candidates are matched on a prefix of the codeword, so a screen that
        # truncates it is still a candidate and fails.
        stem = word[:8]
        assert len(word) > len(stem), "codeword too short for a prefix probe"
        showing = [(label, frame) for label, frame in every_screen(app)
                   if any(stem in row for row in frame.rendered())]
        assert showing, f"no screen renders the codeword {word!r} at all"
        for label, frame in showing:
            assert lost_pixels(frame) == 0, label
            # And it survives Frame's character truncation intact.
            assert any(word in row for row in frame.rendered()), (
                f"{label}: codeword truncated to "
                f"{[r for r in frame.rendered() if stem in r]}")

    def test_the_enumeration_covers_every_screen_class(self):
        """Every screen class in ui.py appears in every_screen(), so none goes
        unchecked.
        """
        app = make_app()
        classes = {name for name, value in vars(ui).items()
                   if isinstance(value, type)
                   and issubclass(value, ui.Screen)
                   and value is not ui.Screen}
        # An exact set comparison on class names; a substring search would let
        # a new Menu or Entry screen pass uncovered.
        covered = {label.split("/", 1)[0] for label, _ in every_screen(app)}
        assert not classes - covered, \
            f"screens with no pixel coverage: {sorted(classes - covered)}"
        assert not covered - classes, \
            f"labels naming things that are not ui.Screen subclasses: " \
            f"{sorted(covered - classes)}"

    def test_no_two_screens_share_a_label(self):
        """Labels are unique: they name the failing screen, and callers keying on
        them must not drop frames.
        """
        app = make_app()
        labels = [label for label, _ in every_screen(app)]
        duplicates = {label for label in labels if labels.count(label) > 1}
        assert not duplicates, f"labels used more than once: {sorted(duplicates)}"


# --- the guard on the guard ----------------------------------------------


class TestTheMeasurementCanSeeClipping:
    """The clipping measurement itself must be able to fail."""

    def test_a_row_too_wide_for_the_panel_is_counted(self, monkeypatch):
        # Past both truncations, Frame's by characters and draw_frame's by
        # pixels, so off_panel is what does the work.
        monkeypatch.setattr(Ssd1306Display, "_fit", lambda self, text: (text, 0))

        class Overflowing(Frame):
            def rendered(self):
                return ["X" * 60] + [""] * (ROWS - 1)

        assert lost_pixels(Overflowing()) > 0

    def test_the_pixel_fit_is_what_keeps_a_wide_font_on_the_panel(
            self, monkeypatch):
        """With a proportional font and draw_frame's pixel fit removed, ink goes
        off the edge; with it, none does.
        """
        with _pillow_without_the_bitmap_accessor(monkeypatch) as available:
            if not available:
                pytest.skip("no FreeType here")
            wide = Frame(title="W" * COLS, lines=["M" * COLS] * 3,
                         footer="W" * COLS)
            assert off_panel(wide) == [], \
                "draw_frame did not fit the rows to the panel"
            monkeypatch.setattr(Ssd1306Display, "_fit", lambda self, t: (t, 0))
            assert off_panel(wide), \
                "with the fit removed a proportional row still fit; the " \
                "font substitution is not being simulated"

    def test_ink_above_the_panel_is_counted(self, monkeypatch):
        """off_panel() reports ink above the panel. PIL discards negative
        coordinates, so this direction needs the padded canvas.
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
        """Rotations 1 and 3 clip every row: luma swaps width and height, leaving
        64px for a 126px row. This is why Ssd1306Display refuses them.
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

    def test_the_panel_rect_is_exactly_the_panel(self):
        """On each of the four edges a lit pixel just inside is ignored and one
        just outside is counted. PANEL_BOX defines "on the panel" for every
        other test here, so a one-pixel error blinds them all.
        """
        def counted(x, y):
            image = Image.new("1", (WIDTH + 2 * PAD, HEIGHT + 2 * PAD), 0)
            image.putpixel((PAD + x, PAD + y), 1)
            # Through off_panel itself, so the real box is pinned and not a
            # copy.
            return off_panel(image=image) != []

        edges = {
            "left":   ((0, 0),              (-1, 0)),
            "right":  ((WIDTH - 1, 0),      (WIDTH, 0)),
            "top":    ((0, 0),              (0, -1)),
            "bottom": ((0, HEIGHT - 1),     (0, HEIGHT)),
        }
        for edge, (inside, outside) in edges.items():
            assert not counted(*inside), \
                f"{edge}: a pixel at {inside} is ON the panel but was counted"
            assert counted(*outside), \
                f"{edge}: a pixel at {outside} is OFF the panel and was missed"

    def test_a_row_below_the_panel_is_counted(self):
        class TooTall(Frame):
            def rendered(self):
                return [""] * ROWS + ["X" * 4]

        assert lost_pixels(TooTall()) > 0

    def test_frames_own_truncation_is_what_keeps_a_long_line_on_the_panel(self):
        """A 60-character line through the real Frame leaves no ink off the right
        edge, tying the 21-character cut to the pixel outcome.
        """
        assert off_panel(Frame(title="T" * 60, lines=["X" * 60] * 4,
                               footer="F" * 60)) == []

    def test_pils_shifting_default_font_would_be_caught(self):
        """With a proportional `load_default()` a full row overflows the panel. If
        this stops overflowing, _panel_font() can be reconsidered.
        """
        from PIL import ImageFont

        default = ImageFont.load_default()
        # Gate on what load_default() returns, not on whether the newer
        # accessor exists: on 10.1-10.3 it does not, and those are the versions
        # at risk.
        if isinstance(default, ImageFont.ImageFont):
            pytest.skip("this Pillow's load_default() is still the bitmap font")
        measure = ImageDraw.Draw(Image.new("1", (1024, 64)))
        assert measure.textlength("W" * COLS, font=default) > WIDTH, (
            "load_default() now fits 21 columns; if it is monospace again, "
            "_panel_font() may no longer be needed")


def test_the_entropy_footer_keeps_its_spinner_at_every_width():
    """The bit count and the spinner both survive Frame's 21-character cut at
    every count width. An over-wide footer loses its tail without overflowing a
    pixel, and the spinner is what tells the operator the unit is not wedged.
    """
    app = make_app()
    for label, frame in every_screen(app):
        if not label.startswith("WaitForEntropy/"):
            continue
        footer = frame.rendered()[-1]
        assert footer.rstrip()[-1:] in set("|/-\\"), (
            f"{label}: the spinner was truncated away; footer renders as "
            f"{footer!r}")
        count = label.split("/", 1)[1].split("-bits")[0]
        expected = "?" if count == "None" else count
        assert f"{expected} BITS" in footer, (
            f"{label}: the bit count did not survive rendering: {footer!r}")

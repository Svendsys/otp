"""Tests for the hardware layer.

This layer had no coverage at all for three review rounds, which is exactly
why a race in the button handling survived them: FakeButtons is a
single-threaded list pop and FakeDisplay never raises, so neither the
threading nor the failure modes were ever exercised.

gpiozero and luma are not test dependencies -- the tests that need them skip
when they are absent. What does not need them is the timing logic itself,
which is deliberately written so it can be driven with a fake clock.
"""
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otpunit.hw import buttons as buttons_mod
from otpunit.hw.buttons import GpioButtons, Press
from otpunit.hw.display import ConsoleDisplay, Frame, Ssd1306Display


class Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def press_handler(monkeypatch):
    """A GpioButtons with the gpiozero constructor bypassed."""
    unit = GpioButtons.__new__(GpioButtons)
    import queue
    unit._events = queue.Queue()
    unit._buttons = []
    unit._pressed_at = None
    clock = Clock()
    monkeypatch.setattr(buttons_mod.time, "monotonic", clock)
    return unit, clock


class TestTapVersusHold:
    """
    One press must produce exactly one event.

    The previous design had gpiozero's when_held set a flag that
    when_released checked -- a check-then-act split across two threads. A
    release landing in the window between the hold thread deciding and it
    setting the flag emitted OK *and* BACK from one press and left the flag
    stuck, swallowing the next tap. On this device that is not cosmetic:
    while a job is printing OK and BACK mean opposite things, so one
    borderline press could purge the spool and destroy a pad pair.
    """

    def test_a_short_press_is_ok(self, monkeypatch):
        unit, clock = press_handler(monkeypatch)
        unit._on_press()
        clock.advance(0.05)
        unit._on_release()
        assert unit.wait(timeout=0) is Press.OK
        assert unit.wait(timeout=0) is None, "exactly one event per press"

    def test_a_long_press_is_back(self, monkeypatch):
        unit, clock = press_handler(monkeypatch)
        unit._on_press()
        clock.advance(1.5)
        unit._on_release()
        assert unit.wait(timeout=0) is Press.BACK
        assert unit.wait(timeout=0) is None

    def test_the_boundary_is_a_clean_split(self, monkeypatch):
        for held, expected in ((0.999, Press.OK), (1.0, Press.BACK),
                               (1.001, Press.BACK)):
            unit, clock = press_handler(monkeypatch)
            unit._on_press()
            clock.advance(held)
            unit._on_release()
            assert unit.wait(timeout=0) is expected, held

    def test_a_press_at_the_boundary_never_emits_both(self, monkeypatch):
        # Sweep across the deadline; every single press must yield exactly
        # one event, whichever side it lands on.
        for micros in range(-200, 201, 5):
            unit, clock = press_handler(monkeypatch)
            unit._on_press()
            clock.advance(buttons_mod.HOLD_SECONDS + micros / 1_000_000)
            unit._on_release()
            first = unit.wait(timeout=0)
            assert first in (Press.OK, Press.BACK), micros
            assert unit.wait(timeout=0) is None, f"double-fired at {micros}us"

    def test_no_state_survives_to_swallow_the_next_press(self, monkeypatch):
        unit, clock = press_handler(monkeypatch)
        for _ in range(5):
            unit._on_press()
            clock.advance(1.4)
            unit._on_release()
            assert unit.wait(timeout=0) is Press.BACK
            unit._on_press()
            clock.advance(0.02)
            unit._on_release()
            assert unit.wait(timeout=0) is Press.OK, "a hold must not eat the next tap"

    def test_a_release_without_a_press_is_ignored(self, monkeypatch):
        unit, _ = press_handler(monkeypatch)
        unit._on_release()
        assert unit.wait(timeout=0) is None

    def test_concurrent_presses_produce_one_event_each(self, monkeypatch):
        """The queue itself must not lose or duplicate under real threads."""
        unit, clock = press_handler(monkeypatch)
        done = threading.Barrier(2)

        def tap():
            done.wait()
            for _ in range(50):
                unit._events.put(Press.UP)

        threads = [threading.Thread(target=tap) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seen = 0
        while unit.wait(timeout=0) is not None:
            seen += 1
        assert seen == 100


class TestDebounceIsNotSoLongItDropsPresses:
    def test_bounce_window_is_shorter_than_a_deliberate_tap(self):
        # lgpio's debounce DROPS an edge that is not stable for the whole
        # window rather than merging it, so a bounce time near a human tap
        # makes presses vanish. Contact bounce is 1-5ms.
        assert buttons_mod.BOUNCE_SECONDS <= 0.02
        assert buttons_mod.BOUNCE_SECONDS >= 0.005


class TestDisplayFailuresDoNotEscape:
    def test_a_dead_panel_does_not_take_the_app_down(self):
        from otpunit import config, printer, ui
        from otpunit.hw.buttons import FakeButtons
        from otpunit.hw.display import Display

        class Dead(Display):
            def show(self, frame):
                raise OSError(121, "Remote I/O error")

        class Cups(printer.Cups):
            def __init__(self):
                super().__init__(run=None)
            def devices(self):
                return [printer.Device("usb://F/L?serial=1", "F L")]
            def ensure_queue(self, d, name="OTP"):
                return name
            def active_jobs(self, name="OTP"):
                return 0

        app = ui.App(Dead(), FakeButtons([Press.OK, Press.QUIT]), Cups(),
                     config.Settings(pages=1), config_path="/nonexistent",
                     poll_seconds=0)
        app.run()          # must not raise

    def test_console_display_renders_a_frame(self):
        import io
        stream = io.StringIO()
        ConsoleDisplay(stream).show(Frame(title="T", lines=["a"], footer="f"))
        assert "T" in stream.getvalue()


class TestPanelTextIsAlwaysDrawable:
    """
    Pillow's default bitmap font is latin-1, and drawing anything outside
    it raises inside the display driver where nothing catches it. Panel
    text often comes from a CUPS error, decoded with errors="replace", so
    a truncated or localised message carries U+FFFD or accents.
    """

    def test_wrap_output_is_always_ascii(self):
        from otpunit.ui import wrap
        for text in ("lp: Erreur – impression impossible",
                     "bad �� bytes", "café printer",
                     "中文", "… leading ellipsis"):
            rows = wrap(text, 21, 4)
            assert all(row.isascii() for row in rows), (text, rows)

    def test_a_latin1_font_can_encode_every_row(self):
        from otpunit.ui import wrap
        for text in ("lp: Erreur – impossible", "bad � bytes"):
            for row in wrap(text, 21, 4):
                row.encode("latin-1")      # must not raise


class TestRealGpiozero:
    """Only runs where gpiozero is installed; skipped otherwise."""

    def test_constructor_matches_the_gpiozero_api(self):
        pytest.importorskip("gpiozero")
        from gpiozero import Device
        from gpiozero.pins.mock import MockFactory

        Device.pin_factory = MockFactory()
        try:
            unit = GpioButtons()
            try:
                assert len(unit._buttons) == 3
            finally:
                unit.close()
        finally:
            Device.pin_factory.reset()
            Device.pin_factory = None

    def test_a_real_press_and_release_yields_one_event(self):
        pytest.importorskip("gpiozero")
        from gpiozero import Device
        from gpiozero.pins.mock import MockFactory

        Device.pin_factory = MockFactory()
        try:
            unit = GpioButtons()
            try:
                pin = Device.pin_factory.pin(buttons_mod.PIN_OK)
                pin.drive_low()
                pin.drive_high()
                first = unit.wait(timeout=1.0)
                assert first is Press.OK
                assert unit.wait(timeout=0.2) is None
            finally:
                unit.close()
        finally:
            Device.pin_factory.reset()
            Device.pin_factory = None

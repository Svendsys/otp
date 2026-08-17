"""Tests for the hardware layer.

This layer had no coverage at all for three review rounds, which is exactly
why a race in the button handling survived them: FakeButtons is a
single-threaded list pop and FakeDisplay never raises, so neither the
threading nor the failure modes were ever exercised.

gpiozero and luma are not test dependencies -- the tests that need them skip
when they are absent. What does not need them is the timing logic itself,
which is deliberately written so it can be driven with a fake clock.
"""
import fcntl
import os
import queue
import select
import struct
import sys
import threading
import pathlib
import tempfile
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# A path under the temp dir, not "/nonexistent": the suite runs as
# root, where that absolute path is perfectly writable, and any test
# reaching SAVE SETTINGS created a real file at the host's root.
SCRATCH_CONFIG = str(pathlib.Path(tempfile.gettempdir()) /
                     "otp-unit-test.conf")

from otpunit.hw import buttons as buttons_mod
from otpunit.hw.buttons import GpioButtons, Press
from otpunit.hw.display import ConsoleDisplay, Frame, Ssd1306Display


class Clock:
    """
    A monotonic clock the test drives by hand, optionally broken once.

    Installed with `use_clock` as buttons.py's whole `time`, rather than by
    setting `monotonic` on the real time module: that module is shared with
    every thread in the process, so patching it reaches anything else that
    happens to be timing something. It matters most for `fails_with`, which
    arms exactly ONE failure -- a stray reader could spend it, and the test
    that wanted a broken clock would quietly get a working one.

    `fails_with` is armed for the calling thread only, for the same reason.
    """

    def __init__(self, fails_with=None):
        self.now = 1000.0
        self._fails_with = fails_with
        self._armed_for = threading.get_ident() if fails_with else None

    @property
    def armed(self):
        """True while the one-shot failure has not been spent."""
        return self._armed_for is not None

    def monotonic(self):
        if self._armed_for is not None and self._armed_for == threading.get_ident():
            self._armed_for = None
            raise self._fails_with
        return self.now

    def advance(self, seconds):
        self.now += seconds


def use_clock(monkeypatch, clock=None):
    """Point buttons.py at `clock`, and nothing else in the process."""
    clock = clock if clock is not None else Clock()
    monkeypatch.setattr(buttons_mod, "time", clock)
    return clock


def press_handler(monkeypatch):
    """A GpioButtons with the gpiozero constructor bypassed."""
    unit = GpioButtons.__new__(GpioButtons)
    import queue
    unit._events = queue.Queue()
    unit._buttons = []
    unit._pressed_at = None
    return unit, use_clock(monkeypatch)


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
                     config.Settings(pages=1), config_path=SCRATCH_CONFIG,
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


# --- nothing a callback does may reach lgpio's dispatch thread -----------
#
# The defect these exercise is not in this repository: lgpio dispatches
# every GPIO edge in the process from ONE thread and puts no guard around
# the call (lgpio 0.2.2.0, lgpio.py:531-559), and that thread is a
# module-level singleton started at import (lgpio.py:562). So one exception
# out of a button callback ends it, and every button in the process --
# including buttons on a panel built later -- goes quiet for the rest of
# the process, reported only as a warning nobody reads.
#
# On a 512 MiB board with no swap making 1000-page pads
# (tests/test_memory_budget.py) the exception to expect is MemoryError, and
# the remedy for a dead panel is a power cycle, which discards the pad in
# progress. Hence the guard in GpioButtons._guarded, and hence these.
#
# What is asserted throughout is the PROPERTY -- a later press still
# arrives -- and not "an exception was caught", which a panel that has
# stopped delivering anything at all would also satisfy.


class FakeButton:
    """
    The only part of gpiozero's `Button` that `GpioButtons` uses.

    Standing in for it keeps this whole section runnable where gpiozero is
    not installed, which is every CI run of the fast suite:
    requirements-dev.txt deliberately leaves gpiozero out. Nothing about
    the CALLBACKS is faked -- they are read back off this object exactly as
    they were handed over, and `TestRealGpiozero` below drives the same
    property through real gpiozero so this stand-in cannot quietly stop
    resembling it.
    """

    def __init__(self, pin, pull_up=True, bounce_time=None):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.when_pressed = None
        self.when_released = None
        self.closed = False

    def close(self):
        self.closed = True


def build_panel(monkeypatch):
    """A real `GpioButtons`, with `FakeButton` standing in for gpiozero."""
    module = types.ModuleType("gpiozero")
    module.Button = FakeButton
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    return GpioButtons()


class MemoryStarvedQueue(queue.Queue):
    """
    The panel's event queue, with no memory for the next `failures` puts.

    `self._events.put(...)` is the one allocation every one of the three
    shipped callbacks makes, so starving it is how an exception is raised
    from INSIDE the shipped callback rather than beside it. MemoryError
    because that is the failure this board is actually exposed to: 512 MiB,
    no swap, and a pad job that peaks near the ceiling.
    """

    def __init__(self):
        super().__init__()
        self.failures = 0

    def put(self, item, *args, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise MemoryError("no memory to record a press")
        return super().put(item, *args, **kwargs)


class JournalPipe:
    """
    Stderr as the unit has it: a pipe with a reader the test can withdraw.

    On the unit stderr is a stream socket to journald. The failure that
    matters is not a closed one -- that raises, and a raise is caught --
    but a FULL one, which does not raise: the writer parks inside write(2)
    until somebody reads, and `except BaseException: pass` is no defence
    against a call that never returns. journald being restarted, wedged or
    simply behind is enough to produce it.

    Shrunk to a single page with F_SETPIPE_SZ so `wedge` is quick; the
    default 64 KiB would do the same job more slowly, and a kernel that
    refuses the resize just gets the slower version.
    """

    PAGE = 4096
    F_SETPIPE_SZ = 1031             # linux/fcntl.h; the module has no name

    def __init__(self):
        self._read, self._write = os.pipe()
        try:
            fcntl.fcntl(self._write, self.F_SETPIPE_SZ, self.PAGE)
        except OSError:                          # pragma: no cover
            pass
        self.stream = os.fdopen(self._write, "w")

    def take(self, timeout=2.0):
        """Everything written so far, waiting up to `timeout` for a first."""
        got = ""
        deadline = time.monotonic() + timeout
        while True:
            wait = 0 if got else max(0.0, deadline - time.monotonic())
            if not select.select([self._read], (), (), wait)[0]:
                return got
            got += os.read(self._read, 65536).decode("utf-8", "replace")

    def wedge(self):
        """Fill it to the brim. From here a write blocks rather than fails."""
        os.set_blocking(self._write, False)
        try:
            while True:
                os.write(self._write, b"." * self.PAGE)
        except BlockingIOError:
            pass
        finally:
            os.set_blocking(self._write, True)

    def close(self):
        # Drain first: anything parked in write(2) on this pipe has to be
        # let go before the test can end, and a daemon thread left parked
        # would take its fds with it into the next test.
        self.take(timeout=0)
        try:
            self.stream.close()
        except OSError:                          # pragma: no cover
            pass
        os.close(self._read)


def dispatch_for(button):
    """
    gpiozero's chain from an lgpio edge to `when_pressed`, in four lines.

    Deliberately unguarded, because gpiozero's real one is: measured by
    reading it, `LGPIOPin._call_when_changed` -> `PiPin._call_when_changed`
    -> `Button._pin_changed` -> `EventsMixin._fire_events` ->
    `_fire_activated` contains no try anywhere, and
    `TestRealGpiozero::test_gpiozero_lets_a_callback_exception_straight_out`
    proves it against the real library rather than by reading.
    """
    def dispatch(chip, gpio, level, tick):
        # 0 is the line pulled to ground, which for a pull_up=True Button
        # is "pressed" -- the same sense the gpio-sim harness drives.
        handler = button.when_pressed if level == 0 else button.when_released
        if handler is not None:
            handler()
    return dispatch


class StandInNotifier:
    """
    lgpio's notification thread, in the one shape that costs a panel.

    Copied from lgpio 0.2.2.0's `_callback_thread.run` (lgpio.py:531-559):
    ONE thread, one list of callbacks shared by every button in the
    process, and a dispatch with no try around it --

        for cb in self.callbacks:
            if cb.chip == chip and cb.gpio == gpio:
                cb.func(chip, gpio, level, tick)

    -- so the first exception out of `cb.func` ends the thread and every
    later edge, on any line, reaches nobody. The transport here is a Queue
    rather than lgpio's FIFO and there is no struct decoding, because
    neither of those is what the guard is about.

    `RealNotifier` runs the same scenarios through lgpio's own `run`
    wherever lgpio is installed, which is what keeps this honest.
    """

    def __init__(self):
        self.callbacks = []
        self._edges = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def register(self, gpio, func):
        self.callbacks.append((0, gpio, func))

    def fire(self, gpio, level=0):
        self._edges.put((0, gpio, level))

    def alive(self):
        return self._thread.is_alive()

    def stop(self):
        self._edges.put(None)
        self._thread.join(timeout=2)

    def _run(self):
        while True:
            edge = self._edges.get()
            if edge is None:
                return
            chip, gpio, level = edge
            for cb in self.callbacks:
                if cb[0] == chip and cb[1] == gpio:
                    cb[2](chip, gpio, level, 0)


class RealNotifier:
    """
    lgpio's actual notification thread, fed forged edges through a pipe.

    `_callback_thread.__init__` opens an lgpio notification handle and a
    `.lgd-nfy*` FIFO in the working directory. None of that is dispatch,
    and skipping it keeps a second FIFO out of the repository root -- so
    the fields `run` reads are set up by hand and `run` itself is lgpio's
    own, unmodified. Measured in a container with no gpiochip and no
    CONFIG_GPIOLIB: an edge written into the pipe reaches the callback, and
    an unguarded raise out of that callback kills the thread and every edge
    after it.
    """

    def __init__(self):
        import lgpio

        self.lgpio = lgpio
        thread = lgpio._callback_thread.__new__(lgpio._callback_thread)
        threading.Thread.__init__(thread)
        read, self._write = os.pipe()
        thread._notify = None
        thread._file = open(read, "rb", buffering=0)
        thread.go = True
        thread.daemon = True
        thread.callbacks = []
        self._thread = thread
        thread.start()

    def register(self, gpio, func):
        self._thread.append(
            self.lgpio._callback_ADT(0, gpio, self.lgpio.BOTH_EDGES, func))

    def fire(self, gpio, level=0):
        # The layout `run` unpacks: tick, chip, gpio, level, flags, pad.
        # flags must be 0 or lgpio ignores the message.
        os.write(self._write, struct.pack("QBBBBI", 1, 0, gpio, level, 0, 0))

    def alive(self):
        return self._thread.is_alive()

    def stop(self):
        self._thread.go = False
        try:
            # Wake the blocking read so the loop can see `go` and leave.
            os.write(self._write, b"\0" * 16)
        except OSError:                          # pragma: no cover
            pass
        self._thread.join(timeout=2)
        # run() closes this on a clean exit; a thread killed by a callback
        # never got there, and a leaked fd per test is not acceptable in a
        # suite this size.
        self._thread._file.close()
        os.close(self._write)


@pytest.fixture(params=["stand-in", "lgpio"])
def notifier(request):
    """The dispatch thread, standing in and for real."""
    if request.param == "lgpio":
        pytest.importorskip(
            "lgpio", reason="lgpio is not a fast-suite dependency (it is "
                            "linux-only); the stand-in run covers this")
        made = RealNotifier()
    else:
        made = StandInNotifier()
    try:
        yield made
    finally:
        made.stop()


@pytest.fixture
def panel(monkeypatch, notifier):
    """A shipped panel whose three callables are wired to `notifier`."""
    unit = build_panel(monkeypatch)
    unit._events = MemoryStarvedQueue()
    for button in unit._buttons:
        notifier.register(button.pin, dispatch_for(button))
    return unit


@pytest.fixture
def unhandled(monkeypatch):
    """
    Every exception that escaped a thread, and the only place it is seen.

    That is the point of collecting them: a dying dispatch thread reports
    itself HERE and nowhere the operator or the failing test would look.
    Replacing the hook also keeps pytest's own
    PytestUnhandledThreadExceptionWarning out of the summary for the one
    test that kills a thread on purpose.
    """
    escaped = []
    monkeypatch.setattr(threading, "excepthook", escaped.append)
    return escaped


class TestNoCallbackExceptionReachesTheDispatchThread:

    def test_an_unguarded_callback_kills_every_button_in_the_process(
            self, notifier, panel, unhandled):
        """
        The control: the same dispatcher, proving it really is fatal.

        Without this the tests below could pass against a notifier that
        swallows exceptions itself, which would make them assert nothing.
        The raiser here is what the shipped callback would be with the
        guard taken off -- `self._events.put(Press.UP)`, no wrapper -- and
        it is registered on a line of its own so the panel's own three
        callbacks stay armed and can be shown to go quiet with it.
        """
        spare = 99
        notifier.register(
            spare,
            lambda chip, gpio, level, tick: panel._events.put(Press.UP))
        panel._events.failures = 1

        notifier.fire(spare)
        for _ in range(200):                     # ~2s, and it dies at once
            if not notifier.alive():
                break
            time.sleep(0.01)
        assert not notifier.alive(), (
            "the dispatcher survived an unguarded exception, so every "
            "assertion in this class is about a dispatcher lgpio does not "
            "have")

        # A perfectly healthy guarded button, on a queue with no failures
        # left, now delivers nothing at all. That is the whole defect.
        notifier.fire(buttons_mod.PIN_UP)
        assert panel.wait(timeout=0.5) is None

        assert len(unhandled) == 1, unhandled
        assert unhandled[0].exc_type is MemoryError
        assert panel.dropped == 0, (
            "nothing was guarded here, so nothing should have been counted")

    def test_a_press_on_another_button_survives_one_that_raises(
            self, notifier, panel):
        """
        THE property, in the order it happens on the device: an edge that
        raises, then an edge that must still arrive.
        """
        panel._events.failures = 1

        notifier.fire(buttons_mod.PIN_UP)        # raises inside the callback
        notifier.fire(buttons_mod.PIN_DOWN)      # must still get through

        assert panel.wait(timeout=2) is Press.DOWN, (
            "a press after a raising press did not arrive: the dispatch "
            "thread is gone and the panel is dead for the life of the "
            f"process (alive={notifier.alive()})")
        assert notifier.alive()
        assert panel.dropped == 1

    def test_the_same_button_still_works_after_its_own_callback_raises(
            self, notifier, panel):
        panel._events.failures = 1

        notifier.fire(buttons_mod.PIN_UP)
        notifier.fire(buttons_mod.PIN_UP)

        assert panel.wait(timeout=2) is Press.UP
        assert panel.wait(timeout=0.2) is None, "one edge, one press"
        assert notifier.alive()

    def test_a_raising_release_does_not_cost_the_next_ok(
            self, notifier, panel):
        """
        OK is the expensive one: it is guarded twice, on press and on
        release, and it is the button that means opposite things while a
        job prints.
        """
        ok = buttons_mod.PIN_OK
        panel._events.failures = 1

        notifier.fire(ok, level=0)               # press: records the time
        notifier.fire(ok, level=1)               # release: put() raises
        notifier.fire(ok, level=0)
        notifier.fire(ok, level=1)               # this one must arrive

        assert panel.wait(timeout=2) is Press.OK, (
            f"the OK that followed a lost release never arrived "
            f"(alive={notifier.alive()})")
        assert panel.wait(timeout=0.2) is None
        assert notifier.alive()

    def test_a_journal_nobody_is_reading_does_not_park_the_thread(
            self, monkeypatch, notifier, panel):
        """
        The report is on the dispatch thread too, so it gets the same rule.

        Catching the exception bought a panel that survives a lost edge,
        and then handed the same thread a `print(..., flush=True)` to
        stderr -- which on the unit is a stream socket to journald. A
        socket nobody is draining does not raise, it BLOCKS, and a thread
        parked in write(2) delivers no more edges: the same dead panel the
        guard exists to prevent, only harder to see, because the thread is
        still alive and `is_alive()` still says so. Before this PR nothing
        wrote to stderr from this thread at all.

        Two halves, in this order. First the control: with a reader, the
        report really does travel down the dispatch thread into stderr --
        without which the second half could pass against a `_report` that
        never touches the journal. Then the same thing with the reader
        withdrawn and the pipe full, where the only acceptable answer is
        that the next press still arrives.
        """
        clock = use_clock(monkeypatch)
        journal = JournalPipe()
        attempts = []
        real_report = buttons_mod._report

        def watched(message):
            attempts.append(message)
            real_report(message)

        monkeypatch.setattr(buttons_mod, "_report", watched)
        saved, sys.stderr = sys.stderr, journal.stream
        try:
            panel._events.failures = 1
            notifier.fire(buttons_mod.PIN_UP)
            written = journal.take(timeout=2)

            # Due again, so the loss below is one the reporter really does
            # try to write rather than one the interval swallows.
            clock.advance(buttons_mod.REPORT_SECONDS)
            journal.wedge()

            panel._events.failures = 1
            notifier.fire(buttons_mod.PIN_UP)
            notifier.fire(buttons_mod.PIN_DOWN)
            arrived = panel.wait(timeout=3)
            alive = notifier.alive()
        finally:
            sys.stderr = saved
            journal.close()

        assert "the UP press was LOST" in written, (
            "the guard's report never reached stderr from the dispatch "
            f"thread, so the wedge below tests nothing: {written!r}")
        assert len(attempts) == 2, (
            f"the second loss was never reported, so nothing tried to "
            f"write to the wedged journal: {attempts}")
        assert arrived is Press.DOWN, (
            "no press arrived after a report was written to a journal "
            "nobody was reading. The dispatch thread is parked inside "
            f"write(2) -- alive={alive}, which is why is_alive() is not "
            "the assertion here -- and every button on the unit is dead "
            "until journald starts reading again.")
        assert panel.dropped == 2

    def test_many_failures_in_a_row_still_leave_the_panel_answering(
            self, notifier, panel):
        """
        A supervisor is deliberately NOT part of this change, so the
        behaviour under repeated failure has to be stated somewhere: the
        panel keeps taking edges, keeps losing them, and keeps counting
        them. Nothing degrades, nothing recovers, nothing gives up.

        What it does NOT do is say so twenty times. The reporting is
        bounded -- first loss in full, one line every REPORT_SECONDS after
        that -- because twenty full reports is 14 KB into a journald that
        `_report` must never wait on. That contract is
        `TestALostPressIsSaidOutLoud`'s; the count asserted here is what
        survives it.
        """
        panel._events.failures = 20
        for _ in range(20):
            notifier.fire(buttons_mod.PIN_UP)
        notifier.fire(buttons_mod.PIN_DOWN)

        assert panel.wait(timeout=2) is Press.DOWN
        assert panel.dropped == 20
        assert notifier.alive()


class TestALostPressIsSaidOutLoud:
    """
    The operator sees nothing -- the panel is what just failed -- so the
    journal is the only record there is. A guard that swallows in silence
    turns a diagnosable fault into a unit that "sometimes misses presses".

    Said out loud, but BOUNDED, which is the second half of the contract
    and the reason most of these tests drive the clock. Each full report is
    ~690 bytes and the faults that cause one cause them in bursts, so: the
    first loss in full with its traceback, then at most one summary line
    every REPORT_SECONDS. Nothing here may make the dispatch thread wait on
    a journald that is not reading (`test_a_wedged_journal_...`), because a
    thread parked in write(2) costs the panel exactly what an uncaught
    exception costs it, while still looking alive.
    """

    @pytest.fixture
    def reports(self, monkeypatch):
        said = []
        monkeypatch.setattr(buttons_mod, "_report", said.append)
        return said

    def test_the_default_report_goes_to_stderr(self, capsys):
        """
        Without this the tests below would pass just as happily against a
        `_report` that writes nowhere at all.
        """
        buttons_mod._report("otp: front panel: hello")
        captured = capsys.readouterr()
        assert "otp: front panel: hello" in captured.err
        assert captured.out == "", "stdout is the simulator's panel"

    def test_it_names_the_press_that_was_lost(self, monkeypatch, reports):
        """
        Every line that gets out names its own press, the summary lines
        included -- "an edge was lost" does not say which button the
        operator is pressing to no effect.
        """
        clock = use_clock(monkeypatch)
        unit = build_panel(monkeypatch)
        unit._events = MemoryStarvedQueue()
        by_pin = {button.pin: button for button in unit._buttons}

        unit._events.failures = 1
        by_pin[buttons_mod.PIN_UP].when_pressed()
        # Past the summary interval, so the second loss is a line of its
        # own rather than a number folded into a later one.
        clock.advance(buttons_mod.REPORT_SECONDS)
        unit._events.failures = 1
        by_pin[buttons_mod.PIN_DOWN].when_pressed()

        assert len(reports) == 2, reports
        assert "the UP press was LOST" in reports[0]
        assert "the DOWN press was LOST" in reports[1]

    def test_it_carries_the_exception_and_where_it_came_from(
            self, monkeypatch, reports):
        unit = build_panel(monkeypatch)
        unit._events = MemoryStarvedQueue()
        unit._events.failures = 1

        by_pin = {button.pin: button for button in unit._buttons}
        by_pin[buttons_mod.PIN_UP].when_pressed()

        said = reports[0]
        assert "MemoryError: no memory to record a press" in said
        # The traceback, because the journal is the only diagnostic there
        # is and "something raised" does not say which line.
        assert "Traceback (most recent call last)" in said
        assert "buttons.py" in said

    def test_it_says_how_many_have_gone(self, monkeypatch, reports):
        clock = use_clock(monkeypatch)
        unit = build_panel(monkeypatch)
        unit._events = MemoryStarvedQueue()
        unit._events.failures = 3
        by_pin = {button.pin: button for button in unit._buttons}

        for _ in range(3):
            by_pin[buttons_mod.PIN_UP].when_pressed()
            clock.advance(buttons_mod.REPORT_SECONDS)

        assert unit.dropped == 3
        assert "3 edge(s) lost since this panel was built" in reports[-1]

    def test_a_burst_says_the_first_in_full_and_then_counts(
            self, monkeypatch, reports):
        """
        The bound, stated as a property: a panel losing every edge writes a
        report, not a stream of them.

        Measured before the bound existed: one lost edge is 11 lines and
        690 bytes with flush=True, so a panel dropping presses as fast as
        an operator can make them wrote unboundedly into the journal --
        into journald, whose blocking is the hazard `_report` is built
        around, at exactly the moment the panel was in trouble.
        """
        clock = use_clock(monkeypatch)
        unit = build_panel(monkeypatch)
        unit._events = MemoryStarvedQueue()
        unit._events.failures = 50
        by_pin = {button.pin: button for button in unit._buttons}

        for _ in range(50):
            by_pin[buttons_mod.PIN_UP].when_pressed()
            clock.advance(0.01)                  # 0.5s of frantic pressing

        assert unit.dropped == 50
        assert len(reports) == 1, (
            f"50 losses inside one {buttons_mod.REPORT_SECONDS:g}s window "
            f"wrote {len(reports)} reports")
        assert "Traceback (most recent call last)" in reports[0]
        assert "at most one line every" in reports[0], (
            "the first report must say that later ones are summarised, or "
            "the gaps in the journal look like the losses stopped")

        # And when the interval is up, one line -- not a second traceback
        # -- carrying both totals.
        clock.advance(buttons_mod.REPORT_SECONDS)
        unit._events.failures = 1
        by_pin[buttons_mod.PIN_DOWN].when_pressed()

        assert len(reports) == 2, reports
        summary = reports[1]
        assert "51 edge(s) lost since this panel was built" in summary
        assert "(50 since the last line)" in summary
        assert "Traceback" not in summary
        assert summary.count("\n") == 0, f"a summary is one line: {summary!r}"

    def test_a_lost_press_start_says_the_release_will_be_ignored(
            self, monkeypatch, reports):
        """
        The one loss with a consequence beyond itself: `_on_release`
        ignores a release it saw no press for, so losing the press start
        silently costs the OK as well.
        """
        clock = use_clock(monkeypatch,
                          Clock(fails_with=OSError(9, "Bad file descriptor")))
        unit = build_panel(monkeypatch)
        by_pin = {button.pin: button for button in unit._buttons}

        by_pin[buttons_mod.PIN_OK].when_pressed()

        assert not clock.armed, (
            "the clock's one armed failure was never spent, so the press "
            "start did not fail and this test proved nothing")
        assert reports, "a lost press start was not reported at all"
        assert "the start of an OK press" in reports[0]
        assert "the release after it is ignored" in reports[0]
        # And it really is ignored, rather than guessed at.
        by_pin[buttons_mod.PIN_OK].when_released()
        assert unit.wait(timeout=0) is None

    def test_a_lost_press_start_leaves_no_press_time_behind(
            self, monkeypatch, reports):
        """
        The same loss, with the worst state it could be left in.

        If a failed `_on_press` left the PREVIOUS press's timestamp in
        place, the next release would measure against it, find more than
        HOLD_SECONDS, and emit BACK where the operator pressed OK -- which
        while a job is printing are opposite things, and is the confusion
        the whole tap-versus-hold design exists to prevent. The report
        promises the release is ignored; this is that promise holding
        whatever was there before.

        The stale timestamp is planted by hand: gpiozero alternates
        pressed/released and `_on_release` clears before it can fail, so
        there is no route to it from outside today. The clearing costs one
        line and does not depend on that staying true.
        """
        clock = use_clock(monkeypatch,
                          Clock(fails_with=OSError(9, "Bad file descriptor")))
        unit = build_panel(monkeypatch)
        by_pin = {button.pin: button for button in unit._buttons}
        unit._pressed_at = clock.now - 2 * buttons_mod.HOLD_SECONDS

        by_pin[buttons_mod.PIN_OK].when_pressed()       # the clock fails
        assert not clock.armed, "the press start did not fail"
        assert unit._pressed_at is None, (
            "a press start that failed left a timestamp behind; the next "
            "release will be read as a hold")

        by_pin[buttons_mod.PIN_OK].when_released()
        assert unit.wait(timeout=0) is None, (
            "the release after a lost press start became a press -- and at "
            "that age, a BACK")

    def test_a_report_that_itself_fails_cannot_kill_the_panel(
            self, monkeypatch):
        """
        Reporting allocates -- an f-string and a formatted traceback --
        which is precisely what a MemoryError breaks next, and stderr on
        this unit is a pipe to journald that can be closed or full. The
        guard's last resort is to lose the log entry rather than the panel.
        """
        def refuses(message):
            raise MemoryError("no memory to log with either")

        monkeypatch.setattr(buttons_mod, "_report", refuses)
        unit = build_panel(monkeypatch)
        unit._events = MemoryStarvedQueue()
        by_pin = {button.pin: button for button in unit._buttons}

        unit._events.failures = 1
        by_pin[buttons_mod.PIN_UP].when_pressed()   # must not raise

        by_pin[buttons_mod.PIN_DOWN].when_pressed()
        assert unit.wait(timeout=0) is Press.DOWN


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

    def test_gpiozero_lets_a_callback_exception_straight_out(self):
        """
        The assumption `dispatch_for` above is built on, checked against
        the library rather than by reading it.

        MockFactory dispatches on the caller's thread, so "gpiozero does
        not catch" shows up right here as a raise out of `drive_low()`. In
        production that same raise lands in lgpio's dispatch thread, where
        there is nothing above it either.
        """
        pytest.importorskip("gpiozero")
        from gpiozero import Button, Device
        from gpiozero.pins.mock import MockFactory

        Device.pin_factory = MockFactory()
        try:
            button = Button(buttons_mod.PIN_UP, pull_up=True)
            try:
                def boom():
                    raise MemoryError("no memory to record a press")

                button.when_pressed = boom
                with pytest.raises(MemoryError):
                    Device.pin_factory.pin(buttons_mod.PIN_UP).drive_low()
            finally:
                button.close()
        finally:
            Device.pin_factory.reset()
            Device.pin_factory = None

    def test_a_raising_shipped_callback_does_not_escape_gpiozero(self):
        """
        The same property as the stand-in section, through the real chain:
        a press whose callback raises, and then a press that must arrive.

        The control for it is the test directly above -- same factory, same
        pin, same kind of exception -- which shows that an unguarded
        callback does escape here. So this test passing is the guard
        working, not gpiozero quietly absorbing it.
        """
        pytest.importorskip("gpiozero")
        from gpiozero import Device
        from gpiozero.pins.mock import MockFactory

        Device.pin_factory = MockFactory()
        try:
            unit = GpioButtons()
            try:
                unit._events = MemoryStarvedQueue()
                up = Device.pin_factory.pin(buttons_mod.PIN_UP)
                down = Device.pin_factory.pin(buttons_mod.PIN_DOWN)

                unit._events.failures = 1
                up.drive_low()                   # must not raise
                assert unit.dropped == 1

                down.drive_low()
                assert unit.wait(timeout=1.0) is Press.DOWN
            finally:
                unit.close()
        finally:
            Device.pin_factory.reset()
            Device.pin_factory = None

"""Tests for the hardware layer.

This layer had no coverage at all for three review rounds, which is exactly
why a race in the button handling survived them: FakeButtons is a
single-threaded list pop and FakeDisplay never raises, so neither the
threading nor the failure modes were ever exercised.

gpiozero and luma are not test dependencies -- the tests that need them skip
when they are absent. What does not need them is the timing logic itself,
which is deliberately written so it can be driven with a fake clock.
"""
import array
import fcntl
import os
import queue
import select
import struct
import sys
import termios
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


class FakeDispatcher:
    """
    The one thing `GpioButtons._watch` asks of lgpio's thread: is it alive.

    Counting the probes is not decoration. Most of what is asserted about
    the supervisor is that NOTHING happened -- no revival, no journal line
    -- and a panel that never looked at the dispatcher at all would satisfy
    every one of those. `probes` is what tells the two apart.
    """

    def __init__(self, alive=True):
        self._alive = alive
        self.probes = 0

    def is_alive(self):
        self.probes += 1
        return self._alive

    def die(self):
        self._alive = False


class Reviver:
    """A stand-in for `revive_dispatch`: records, then answers as told."""

    def __init__(self, discarded=0, raises=None):
        self.called = []
        self.discarded = discarded
        self.raises = raises

    def __call__(self, dead):
        self.called.append(dead)
        if self.raises is not None:
            raise self.raises
        return self.discarded


def build_panel(monkeypatch, **injected):
    """
    A real `GpioButtons`, with `FakeButton` standing in for gpiozero.

    The dispatch probe is stood in for as well, unless the caller names its
    own, and that is not only convenience. The shipped default reaches into
    lgpio's module globals for a thread this process shares with everything
    else in it -- which is right on the unit, where that thread is the one
    delivering the panel's edges, and wrong here, where it belongs to
    whatever imported lgpio first. Left as it comes, every `revived == 0`
    in this file would be a statement about a library that CI does not even
    install (requirements-dev.txt leaves lgpio out), and so could not fail.
    A healthy stand-in makes those assertions mean something everywhere.
    """
    module = types.ModuleType("gpiozero")
    module.Button = FakeButton
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    # None, and not the default stand-in, when the caller brought its own
    # probe. It used to be the default either way, which is a decoy: a
    # caller that injects `dispatcher=lambda: mine` and then asserts
    # `panel.stand_in_dispatcher.probes == 0` would be asking a thread the
    # panel never touched, and the assertion would pass however many times
    # the real probe ran. An AttributeError on None is the loud version of
    # "there is nothing here to read".
    thread = None
    if "dispatcher" not in injected:
        thread = FakeDispatcher(alive=True)
        injected["dispatcher"] = lambda: thread
    injected.setdefault("reviver", Reviver())
    panel = GpioButtons(**injected)
    # What the panel was given, where a test can read it back. The panel
    # keeps these as `_dispatcher`/`_reviver`, which are callables rather
    # than the objects an assertion wants to look at.
    panel.stand_in_dispatcher = thread
    panel.stand_in_reviver = injected["reviver"]
    return panel


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
            chunk = os.read(self._read, 65536)
            if not chunk:
                return got                       # the write end went away
            got += chunk.decode("utf-8", "replace")

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

    def close(self, timeout=5.0):
        # Drain until nothing more comes, and only then close. Two reasons,
        # both learned by hanging: a writer parked in write(2) has to be let
        # go or the daemon thread holding it goes into the next test, and a
        # writer with more to say parks again the moment the buffer refills,
        # so one read is not enough. Closing underneath one of those blocks
        # on the file object's own lock -- a teardown hang, in a test that
        # has already decided it failed, which the mutation gate scores as
        # BROKEN rather than caught.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.take(timeout=0.2):
            pass
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
        # Buffered, because lgpio's own is: `open('.lgd-nfy{}'..., 'rb')`
        # (lgpio.py:504) gives a BufferedReader, and `run`'s `read(16)`
        # against one pulls up to 8192 bytes out of the pipe and keeps the
        # rest inside Python. That is not a detail: it is where edges sit
        # when a callback kills the thread mid-burst, and a drain that
        # cannot see them replays them on recovery. With `buffering=0`
        # here, which is what this was, that case cannot be reproduced at
        # all -- and TestRevivingLgpiosOwnDispatchThread would pass against
        # a drain that only reads the descriptor.
        thread._file = open(read, "rb")
        thread.go = True
        thread.daemon = True
        thread.callbacks = []
        self._thread = thread
        thread.start()

    def register(self, gpio, func):
        self._thread.append(
            self.lgpio._callback_ADT(0, gpio, self.lgpio.BOTH_EDGES, func))

    @staticmethod
    def frame(gpio, level=0):
        # The layout `run` unpacks: tick, chip, gpio, level, flags, pad.
        # flags must be 0 or lgpio ignores the message.
        return struct.pack("QBBBBI", 1, 0, gpio, level, 0, 0)

    def fire(self, gpio, level=0):
        os.write(self._write, self.frame(gpio, level))

    def fire_together(self, *frames):
        """
        Several edges in ONE write, so one read takes them all.

        Which is the case that matters for a drain: `run` reads with
        `self._file.read(16)` on a BufferedReader, and that pulls up to
        8192 bytes out of the pipe whatever it was asked for. Edges
        written together with the one that kills the thread therefore end
        up INSIDE Python, where a drain of the descriptor cannot see them.
        """
        os.write(self._write, b"".join(frames))

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
            # Wait for the reporter to have STAMPED, not merely written.
            # `_note_lost` records `_said_at` AFTER `_report` returns, so a
            # clock advanced the instant the bytes appear can get in first
            # -- the stamp is then taken from the ALREADY ADVANCED clock,
            # the second loss falls inside the interval after all, and
            # nothing ever tries to write to the wedged pipe this test is
            # about. Measured on this container with the machine
            # deliberately saturated (12 busy loops on 4 cores): 7 of 15
            # runs of this module failed exactly that way, on `assert
            # len(attempts) == 2`, before this wait was here.
            stamped = time.monotonic() + 5
            while panel._said_at is None and time.monotonic() < stamped:
                time.sleep(0.01)
            assert panel._said_at is not None, (
                "the first loss was never stamped, so advancing the clock "
                "cannot make the second one due and the wedge below would "
                "be tested against a report that is never attempted")

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
        Losing edges is not a reason to recover, and this states that.

        There is a supervisor now (`GpioButtons._watch`), and this test is
        where its boundary is written down, because the two mechanisms
        answer different faults and confusing them would make the noisy one
        drive the drastic one. The guard catching an exception means the
        dispatch thread is ALIVE and the panel is working: the edge is
        gone, the operator presses again, and nothing needs restarting. The
        supervisor keys on the thread being dead and on nothing else -- not
        on a count of losses, not on a silence, neither of which
        distinguishes a broken panel from a working one nobody is touching.

        So under twenty failures in a row the contract is what it always
        was: the panel keeps taking edges, keeps losing them, and keeps
        counting them. Nothing degrades, nothing gives up, and nothing is
        revived.

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
        assert panel.stand_in_dispatcher.probes, (
            "the panel never looked at its dispatcher at all, so the two "
            "assertions below hold for a supervisor that does not run")
        assert panel.stand_in_reviver.called == []
        assert panel.revived == 0, (
            "twenty caught exceptions made the panel restart something. "
            "The guard catching is the panel WORKING; a revival is for a "
            "dispatch thread that has stopped, which this one has not")


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

    def test_a_report_too_long_for_the_pipe_is_cut_rather_than_parked(self):
        """
        The other half of not blocking, and the half that is easy to lose.

        `select()` answering "writable" for a pipe promises PIPE_BUF (4096)
        bytes and no more: a longer write puts the first 4096 in and parks
        in the kernel for the rest. The report is built from an exception's
        `str()` and a formatted traceback, neither of which this code gets
        to choose the length of -- a deep stack or a chatty OSError is
        enough -- so it is capped below PIPE_BUF before the question is
        asked. A truncated line in the journal is a diagnostic; a parked
        dispatch thread is a dead panel.
        """
        journal = JournalPipe()
        done = threading.Event()

        def report():
            try:
                buttons_mod._report("y" * 100_000)
            finally:
                done.set()

        writer = threading.Thread(target=report, daemon=True)
        saved, sys.stderr = sys.stderr, journal.stream
        try:
            writer.start()
            finished = done.wait(5)
            written = journal.take(timeout=1)
        finally:
            sys.stderr = saved
            journal.close()

        assert finished, (
            "_report did not return: a message longer than the pipe would "
            "take parked the caller inside write(2), and on the unit that "
            "caller is the thread every button shares")
        assert 0 < len(written) <= buttons_mod.REPORT_MAX_CHARS + 64
        assert "truncated" in written, (
            f"the line was cut without saying so: {written[-80:]!r}")

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


# --- and a dispatch thread that is already dead ---------------------------
#
# The guard above answers "an exception got out of OUR callback". It cannot
# answer "the dispatch thread is already gone", because by then there is
# nothing left in the process to catch anything: lgpio's notification thread
# is a module-level singleton started at import (lgpio.py:562), nothing
# restarts it, and every button in the process -- including a panel built
# afterwards -- is inert from that moment until a power cycle, which on this
# unit discards the pad in progress and its key material.
#
# The decision this implements is "build it, log only": notice, put delivery
# back, say so in the journal, and change nothing else. The signal is the
# thread's own liveness, which is directly observable, rather than a silence
# threshold -- on a device that sits untouched between pads there is no
# defensible number of quiet seconds, and every candidate fires on a healthy
# unit.


class TestTheDispatchThreadIsWatched:
    """
    The supervisor's policy, without needing a dead lgpio to drive it.

    The probe and the repair are both injected, because there is exactly one
    place in this suite that can produce a genuine dead dispatch thread
    (`TestRevivingLgpiosOwnDispatchThread`, below, which is where the claim
    that a revival WORKS is made and measured). A policy testable only there
    would be a policy tested once, in one shape, on machines that have lgpio.
    """

    @pytest.fixture
    def reports(self, monkeypatch):
        said = []
        monkeypatch.setattr(buttons_mod, "_report", said.append)
        return said

    def test_a_healthy_dispatcher_is_never_restarted_and_never_mentioned(
            self, monkeypatch, reports):
        """
        A supervisor that logs on a working unit is one people learn to
        ignore, and then the one line that mattered goes past unread.
        """
        thread = FakeDispatcher(alive=True)
        reviver = Reviver()
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=reviver)

        for _ in range(5):
            assert panel.wait(timeout=0) is None

        assert thread.probes == 5, (
            "the panel never asked whether the dispatcher was alive, so "
            "the three assertions below are about a check that does not "
            "run rather than about a check that stays quiet")
        assert reviver.called == []
        assert reports == []
        assert panel.revived == 0

    def test_a_dead_dispatcher_is_restarted_and_the_journal_says_so(
            self, monkeypatch, reports):
        thread = FakeDispatcher(alive=False)
        reviver = Reviver(discarded=96)
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=reviver)

        assert panel.wait(timeout=0) is None

        assert reviver.called == [thread], (
            "the dead dispatcher was not handed to the repair")
        assert panel.revived == 1
        assert len(reports) == 1, reports
        said = reports[0]
        assert "dispatch thread was DEAD" in said
        assert "96 byte(s) of undelivered edges" in said, (
            f"the line does not say what was thrown away: {said!r}")
        assert "revival 1 since this panel was built" in said
        assert "bound" not in said, (
            f"a drain that emptied the pipe said it had stopped at its "
            f"bound, which would send whoever reads the journal looking "
            f"for a replay that did not happen: {said!r}")

    def test_a_drain_that_stopped_at_its_bound_says_the_rest_is_coming(
            self, monkeypatch, reports):
        """
        The one case where discarding the backlog does NOT prevent a replay.

        `_discard_backlog` gives up at BACKLOG_MAX_BYTES so that a pipe
        somebody is still filling cannot hold the app's event loop for as
        long as it likes. The price is that everything past the bound stays
        in the pipe and the replacement thread delivers it -- presses aimed
        at a panel that was not listening, arriving at machine speed, which
        is the whole hazard the drain exists for. The journal is the only
        diagnostic this device has, so the one report that says "discarded
        rather than replayed" may not stay quiet about the megabyte where
        that is not what happened.
        """
        thread = FakeDispatcher(alive=False)
        panel = build_panel(
            monkeypatch, dispatcher=lambda: thread,
            reviver=Reviver(discarded=buttons_mod.BACKLOG_MAX_BYTES))

        assert panel.wait(timeout=0) is None

        assert panel.revived == 1, (
            "nothing was revived, so no success line was written and this "
            "test is asserting about a report that does not exist")
        said = reports[0]
        assert "stopped at its" in said and "bound" in said, (
            f"the drain stopped at its bound and the report does not say "
            f"so, which leaves it claiming the backlog was discarded when "
            f"the rest of it is about to be delivered: {said!r}")
        assert "WILL be delivered by the replacement" in said

    def test_a_revival_changes_nothing_but_the_dispatcher(
            self, monkeypatch, reports):
        """
        "Log only" in the one form a test can hold it to.

        The owner's decision was to restore delivery and nothing else: no
        pixel drawn -- the panel is a 128x64 OLED with a menu on it -- and
        no job touched. What this file can check is the panel's own side of
        that: the three `Button`s are the same objects afterwards, still
        open, still carrying the same callbacks. A supervisor that closed
        and rebuilt them would take the lines down and back up under an
        operator's finger, and on a shared pin cache it is how a panel ends
        up holding pins from a factory that has closed (see
        pirig.release_gpiozero).
        """
        thread = FakeDispatcher(alive=False)
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=Reviver(discarded=0))
        before = list(panel._buttons)
        callbacks = [(b.when_pressed, b.when_released) for b in before]

        assert panel.wait(timeout=0) is None

        assert panel.revived == 1, (
            "nothing was revived, so this test is asserting that a "
            "supervisor which did not run left the buttons alone")
        assert panel._buttons == before, "the buttons were rebuilt"
        assert [(b.when_pressed, b.when_released) for b in before] == callbacks
        assert not any(b.closed for b in before), "a button was closed"

    def test_a_revival_that_raises_leaves_the_panel_answering(
            self, monkeypatch, reports):
        """
        The repair allocates a thread, and the fault it is here to meet is
        the one that breaks allocating next.

        This said an exception out of `_watch` ends `App.run()`, `main()`
        returns 0, and `Restart=on-failure` reads that as success and does
        not restart. That was wrong: `__main__.main` does not catch
        arbitrary exceptions, so they come out of it and the process exits
        1. Measured, `ui.App.run` patched to raise MemoryError on the
        `--sim` path -- the only one that ends in `return 0` -- gave a
        traceback out of `main()` and `EXIT=1`. What a raise here really
        buys is therefore a restart every RestartSec=15 for as long as the
        dispatcher stays dead, each one discarding the pad in progress and
        its key material. One failed revival must cost a log line, not the
        unit and not the pad.
        """
        thread = FakeDispatcher(alive=False)
        reviver = Reviver(raises=MemoryError("no memory to start a thread"))
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=reviver)

        assert panel.wait(timeout=0) is None     # must not raise
        assert reviver.called == [thread], "the repair was never attempted"

        by_pin = {button.pin: button for button in panel._buttons}
        by_pin[buttons_mod.PIN_DOWN].when_pressed()
        assert panel.wait(timeout=0) is Press.DOWN, (
            "a press after a failed revival did not arrive: the supervisor "
            "took the event loop down with it")

        assert panel.revived == 0, "a revival that raised was counted a win"
        said = reports[0]
        assert "could not be restarted" in said
        assert "MemoryError: no memory to start a thread" in said
        assert "Traceback (most recent call last)" in said, (
            "the first failure carries no traceback, and the journal is "
            f"the only diagnostic this device has: {said!r}")

    def test_a_dispatcher_that_stays_dead_is_not_retried_without_a_bound(
            self, monkeypatch, reports):
        """
        The volume half, and the memory half, of the same hazard.

        `_watch` runs on every `wait`, and a printing job asks with
        `timeout=0` on every progress step -- fifty times in a pad. A
        repair attempted on each of those would allocate a thread and
        format a traceback fifty times over, in the exact minutes the board
        is nearest its 512 MiB ceiling, and write fifty reports into a
        journald that `_report` must never wait on.
        """
        clock = use_clock(monkeypatch)
        thread = FakeDispatcher(alive=False)
        reviver = Reviver(raises=MemoryError("still no memory"))
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=reviver)

        for _ in range(50):
            panel.wait(timeout=0)
            clock.advance(0.1)                   # 5s of a job reporting
        assert len(reviver.called) == 1, (
            f"{len(reviver.called)} attempts inside one "
            f"{buttons_mod.REVIVE_SECONDS:g}s window")
        assert len(reports) == 1, reports

        clock.advance(buttons_mod.REVIVE_SECONDS)
        panel.wait(timeout=0)

        assert len(reviver.called) == 2, (
            "the interval elapsed and nothing tried again, so a panel that "
            "failed once stays dead for the life of the process")
        assert len(reports) == 2
        assert "2 attempt(s) have failed" in reports[1]
        assert "Traceback" not in reports[1], (
            "every failure carries a full traceback; the first one is the "
            "one that says where the fault is")

    def test_a_press_half_taken_when_the_dispatcher_died_cannot_become_back(
            self, monkeypatch, reports):
        """
        The one piece of state a revival must clear, and why.

        `_on_press` records a timestamp and `_on_release` decides OK versus
        BACK by measuring against it. A dispatch thread that dies BETWEEN
        the two leaves that timestamp behind; the release that eventually
        arrives -- minutes later, once delivery is back -- measures more
        than HOLD_SECONDS and arrives as BACK where the operator pressed
        OK. While a job is printing those are opposite things: BACK purges
        the spool and destroys the pad pair.

        The control comes first, on a panel whose dispatcher is alive: the
        same stale timestamp, the same release, and it really does come out
        as BACK. Without it this test would pass against an `_on_release`
        that had stopped emitting anything at all.
        """
        clock = use_clock(monkeypatch)
        alive = FakeDispatcher(alive=True)
        control = build_panel(monkeypatch, dispatcher=lambda: alive,
                              reviver=Reviver())
        control._pressed_at = clock.now - 5 * buttons_mod.HOLD_SECONDS
        control.wait(timeout=0)
        control._on_release()
        assert control.wait(timeout=0) is Press.BACK, (
            "a stale press time did not turn the next release into a BACK, "
            "so clearing it below cannot be shown to prevent anything")

        dead = FakeDispatcher(alive=False)
        panel = build_panel(monkeypatch, dispatcher=lambda: dead,
                            reviver=Reviver())
        panel._pressed_at = clock.now - 5 * buttons_mod.HOLD_SECONDS

        panel.wait(timeout=0)

        assert panel.revived == 1
        assert panel._pressed_at is None, (
            "the revival left a press time from before the death behind")
        panel._on_release()
        assert panel.wait(timeout=0) is None, (
            "the first release after a revival became a press -- and at "
            "that age, a BACK")

    def test_a_report_about_a_revival_cannot_kill_the_event_loop(
            self, monkeypatch):
        """
        The last resort, and the only swallow in `_watch`.

        Saying what happened allocates -- an f-string, a formatted
        traceback -- which is precisely what a MemoryError breaks next, and
        stderr on this unit is a socket to journald that can be closed.
        `_watch` runs on the app's event loop, so losing the log line has
        to beat losing the loop.
        """
        def refuses(message):
            raise MemoryError("no memory to log with either")

        monkeypatch.setattr(buttons_mod, "_report", refuses)
        thread = FakeDispatcher(alive=False)
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=Reviver())

        assert panel.wait(timeout=0) is None     # must not raise
        assert panel.revived == 1, (
            "nothing was revived, so the report that had to fail was never "
            "reached and this test proved nothing")

        by_pin = {button.pin: button for button in panel._buttons}
        by_pin[buttons_mod.PIN_DOWN].when_pressed()
        assert panel.wait(timeout=0) is Press.DOWN

    def test_a_probe_that_raises_cannot_kill_the_event_loop(
            self, monkeypatch, reports):
        """
        The probe reaches into another library's module globals. It has no
        business raising, and `_watch` may not find out the hard way on the
        thread that draws the panel.
        """
        def refuses():
            raise RuntimeError("lgpio is being reloaded under us")

        panel = build_panel(monkeypatch, dispatcher=refuses,
                            reviver=Reviver())

        assert panel.wait(timeout=0) is None     # must not raise

        by_pin = {button.pin: button for button in panel._buttons}
        by_pin[buttons_mod.PIN_UP].when_pressed()
        assert panel.wait(timeout=0) is Press.UP

    def test_a_closed_panel_stops_watching(self, monkeypatch, reports):
        """
        `close()` is the app going away. Reviving lgpio's thread on behalf
        of a panel that is being torn down is work for nobody, and it is
        the one moment the supervisor could race a teardown.
        """
        thread = FakeDispatcher(alive=False)
        reviver = Reviver()
        panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                            reviver=reviver)
        panel.close()

        assert panel.wait(timeout=0) is None
        assert thread.probes == 0, (
            f"a closed panel asked {thread.probes} time(s) anyway")
        assert reviver.called == []

        # The control: the same fakes on a panel that is not closed do get
        # a revival, so the silence above is closing and not the fakes.
        open_panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                                 reviver=reviver)
        open_panel.wait(timeout=0)
        assert reviver.called == [thread]

    def test_a_wait_with_no_deadline_still_looks_at_the_dispatcher(
            self, monkeypatch):
        """
        The main menu blocks in `wait()` with no timeout (ui.App.run), so an
        unsliced `queue.get()` there would mean a dispatcher that died while
        the unit sat idle was never looked at again -- the panel would stay
        dead until a press that could not arrive.
        """
        monkeypatch.setattr(buttons_mod, "WATCH_SECONDS", 0.01)
        panel = build_panel(monkeypatch, reviver=Reviver())
        thread = FakeDispatcher(alive=True)

        def dispatcher():
            # A press appears while the panel is already blocked, which is
            # what a press on an idle menu is.
            if thread.probes == 2:
                panel._events.put(Press.UP)
            return thread

        panel._dispatcher = dispatcher

        # On its own thread, and joined with a deadline, because the way
        # this fails is that `wait()` never comes back at all -- a test
        # that called it here would hang the run rather than fail it, and
        # the mutation gate scores a hang as BROKEN instead of caught.
        got = []
        waiter = threading.Thread(target=lambda: got.append(panel.wait()),
                                  daemon=True)
        waiter.start()
        waiter.join(10)
        try:
            assert got == [Press.UP], (
                f"wait() with no deadline looked {thread.probes} time(s) "
                f"and then blocked. A dispatcher that dies while the menu "
                f"sits idle would never be looked at again, and the panel "
                f"would stay dead waiting for a press that cannot arrive")
            assert thread.probes >= 3
        finally:
            # Let a stuck waiter go, so a failure here does not leave a
            # thread parked on this queue for the rest of the session.
            panel._events.put(Press.QUIT)
            waiter.join(2)


class TestFindingLgpiosDispatchThread:
    """
    `dispatch_thread`, which is what decides there is anything to supervise.

    It must not import lgpio to find out. lgpio's import creates a FIFO in
    the working directory (lgpio.py:503-504) and raises where that directory
    is read-only -- measured in this container, from a directory on a
    read-only mount:

        xCreatePipe: Can't set permissions ... /.lgd-nfy0
        FileNotFoundError: [Errno 2] ...: '.lgd-nfy-3'

    which is not a thing to provoke from a recovery path that may run while
    a pad is printing.
    """

    def test_a_process_that_never_imported_lgpio_has_nothing_to_supervise(
            self, monkeypatch):
        """
        Not hypothetical: gpiozero takes the first pin factory that imports
        -- lgpio, rpigpio, pigpio, native -- so a unit whose panel came up
        on any of the other three has no lgpio dispatch thread at all, and
        the honest answer about a thread that does not exist is None.

        None is the right ANSWER and it is not a solved problem. On such a
        unit the supervisor does nothing, while the fault it exists for is
        still there: `NativeDispatchThread._run` calls
        `pin._call_when_changed` with no guard either (gpiozero 2.0.1,
        native.py:353-370). Which factory the shipped unit runs on is
        issue #43, and it needs a Pi to answer.
        """
        monkeypatch.delitem(sys.modules, "lgpio", raising=False)
        assert buttons_mod.dispatch_thread() is None

    def test_it_does_not_import_lgpio_to_find_that_out(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "lgpio", raising=False)
        buttons_mod.dispatch_thread()
        assert "lgpio" not in sys.modules, (
            "asking whether there is a dispatch thread imported lgpio, "
            "which opens a notification handle and creates a FIFO in the "
            "working directory")

    def test_it_finds_the_module_level_singleton_and_not_a_copy(
            self, monkeypatch):
        """
        The control for the two above, which would both pass against a
        `dispatch_thread` that returned None unconditionally.
        """
        stand_in = types.ModuleType("lgpio")
        stand_in._notify_thread = sentinel = object()
        monkeypatch.setitem(sys.modules, "lgpio", stand_in)
        assert buttons_mod.dispatch_thread() is sentinel

    def test_an_lgpio_without_a_dispatch_thread_is_not_an_error(
            self, monkeypatch):
        # A partially imported module -- another thread inside `import
        # lgpio` -- has the module object in sys.modules and no
        # `_notify_thread` on it yet. `_watch` runs on the event loop and
        # may not raise, so this is answered rather than thrown.
        monkeypatch.setitem(sys.modules, "lgpio", types.ModuleType("lgpio"))
        assert buttons_mod.dispatch_thread() is None


class WatchedReader:
    """
    A notification pipe's read end that refuses to be parked.

    Only the two things `_discard_backlog` uses. Recording how it was asked
    is not enough on its own: a drain that reads while the descriptor can
    still block does not come back, and a test that asserted afterwards
    would hang rather than fail -- which the mutation gate scores as BROKEN
    instead of caught. So the refusal is raised at the moment it happens.

    The `blocking` list is still kept, and is the positive control: an
    empty one means nothing was ever read, which a drain that did nothing
    at all would also produce.
    """

    def __init__(self, source):
        self._source = source
        self.blocking = []
        self.raises = None

    def fileno(self):
        return self._source.fileno()

    def readinto1(self, buffer):
        blocking = os.get_blocking(self.fileno())
        self.blocking.append(blocking)
        if blocking:
            raise AssertionError(
                "the drain read a pipe that could still block. On the unit "
                "that parks the thread the app waits for presses on, which "
                "is the panel frozen by the thing that was repairing it")
        if self.raises is not None:
            raise self.raises
        return self._source.readinto1(buffer)


class TestDiscardingAStaleBacklog:
    """
    The drain, on a pipe, with no lgpio and no panel in sight.

    It is the half of a revival that decides whether recovery is safe or
    harmful: everything the operator pressed at a panel that was not
    listening is sitting in that pipe, and delivering it is driving the
    menu at machine speed with presses aimed at a screen nobody moved on.
    """

    @pytest.fixture
    def pipe(self):
        read, write = os.pipe()
        source = open(read, "rb")
        try:
            yield source, write
        finally:
            source.close()
            os.close(write)

    def test_it_takes_what_the_pipe_is_holding(self, pipe):
        source, write = pipe
        os.write(write, b"e" * 320)

        assert buttons_mod._discard_backlog(source) == 320

        os.write(write, b"NEW-EDGE-16BYTE!")
        assert source.read(16) == b"NEW-EDGE-16BYTE!", (
            "the drain ate the pipe rather than emptying it")

    def test_it_takes_what_the_reader_has_already_buffered(self, pipe):
        """
        The case a drain of the DESCRIPTOR cannot see, and the reason this
        one goes through the file object.

        lgpio reads its notifications with `self._file.read(16)` on a
        BufferedReader (lgpio.py:504, 543), and one of those pulls up to
        8192 bytes out of the pipe whatever it was asked for. So edges
        written in the same burst as the one that kills the dispatch thread
        are already inside Python when it dies -- the kernel is holding
        nothing at all, which the first assertion here states rather than
        assumes.
        """
        os.write(pipe[1], b"E" * 976)
        source = pipe[0]
        assert source.read(16) == b"E" * 16       # one edge, 960 buffered

        waiting = array.array("i", [0])
        fcntl.ioctl(source.fileno(), termios.FIONREAD, waiting, True)
        assert waiting[0] == 0, (
            f"the kernel is still holding {waiting[0]} bytes, so a drain "
            f"that only read the descriptor would find them and this test "
            f"would not say anything about the reader's own buffer")

        assert buttons_mod._discard_backlog(source) == 960

    def test_it_reads_only_while_the_descriptor_cannot_park_and_puts_it_back(
            self, pipe):
        """
        Both halves of the flag, and only the flag.

        This was named for a property it does not test and which is not
        true as stated -- "it never leaves the descriptor able to park the
        caller". What it asserts is that every read happened with
        O_NONBLOCK set and that the descriptor was blocking again
        afterwards -- and the second of those is the descriptor being left
        ABLE to park, deliberately, because lgpio's own loop needs it that
        way (`buf += self._file.read(16)`, lgpio.py:542, meets None
        otherwise and raises).

        The parking this cannot rule out is the other one: the
        `BufferedReader`'s lock, held by a reader already inside a blocking
        read(2), which no flag on the descriptor reaches. Measured -- the
        drain did not return in five seconds. It is unreachable today
        because the only caller is the app's single event-loop thread and
        it only drains a dispatch thread that `is_alive()` says has
        stopped; `_discard_backlog`'s docstring is where that argument
        lives, because it is about the caller and not about anything a
        test of this function can hold still.
        """
        source, write = pipe
        os.write(write, b"e" * 32)
        watched = WatchedReader(source)
        assert os.get_blocking(source.fileno())

        buttons_mod._discard_backlog(watched)

        assert watched.blocking and not any(watched.blocking), (
            f"the drain read the pipe while it could still block, and on "
            f"the unit that parks the thread the app waits for presses on: "
            f"{watched.blocking}")
        assert os.get_blocking(source.fileno()), (
            "the descriptor was left non-blocking, which turns lgpio's "
            "next read(16) into a busy loop delivering nothing -- a panel "
            "that looks alive and is not")

    def test_a_read_that_fails_still_leaves_it_blocking(self, pipe):
        source, _ = pipe
        watched = WatchedReader(source)
        watched.raises = OSError(5, "I/O error")

        with pytest.raises(OSError):
            buttons_mod._discard_backlog(watched)

        assert os.get_blocking(source.fileno()), (
            "a failed drain left the descriptor non-blocking")

    def test_it_gives_up_rather_than_draining_a_pipe_somebody_is_filling(
            self, pipe):
        """
        The drain runs on the app's event loop. A writer keeping up with it
        would otherwise hold that loop for as long as it cared to, which is
        the panel frozen by the thing that was repairing it.

        The stand-in stops eventually rather than never, deliberately: an
        endless one turns a missing bound into a HANG, and a test that
        hangs neither reports nor is scored as having caught anything.
        Three times the bound is enough to tell "stopped at the bound" from
        "read everything there was".
        """
        limit = 3 * buttons_mod.BACKLOG_MAX_BYTES

        class Filling(WatchedReader):
            left = limit

            def readinto1(self, buffer):
                super().readinto1(buffer)
                took = min(len(buffer), type(self).left)
                type(self).left -= took
                return took

        dropped = buttons_mod._discard_backlog(Filling(pipe[0]))

        assert dropped >= buttons_mod.BACKLOG_MAX_BYTES, (
            f"the drain stopped after {dropped} bytes, well short of the "
            f"64 KiB a Linux pipe can be holding")
        assert dropped < limit, (
            f"the drain read {dropped} bytes and would have gone on for as "
            f"long as anything kept writing, on the thread the app waits "
            f"for presses on")


class TestTheReplacementDispatchThread:
    """
    What `revive_dispatch` builds, with a stand-in for lgpio.

    lgpio is not a fast-suite dependency -- requirements-dev.txt leaves it
    out, and it is linux-only -- so `TestRevivingLgpiosOwnDispatchThread`
    below, which is where the repair is proved to actually restore edge
    delivery, does not run in CI. These do: they hold the assembly to
    account without the library, and the class they build is a real
    `threading.Thread` subclass initialised exactly the way lgpio's own is.
    """

    @pytest.fixture
    def lgpio(self, monkeypatch):
        """A module named lgpio with only what the repair touches on it."""
        module = types.ModuleType("lgpio")
        module.order = []

        class CallbackThread(threading.Thread):
            def start(self):
                # Recorded here, on the caller's thread, rather than in
                # run(): whether the module global had been repointed by
                # the time the thread began is a question about ORDER, and
                # asking it from inside the new thread would answer it
                # whenever the scheduler got round to it.
                found = module._notify_thread is self
                module.order.append("published" if found else "started")
                super().start()

            def run(self):
                module.order.append("ran")

        module._callback_thread = CallbackThread
        monkeypatch.setitem(sys.modules, "lgpio", module)
        return module

    @pytest.fixture
    def dead(self, lgpio):
        """The three attributes the repair carries over from the corpse."""
        read, write = os.pipe()
        corpse = types.SimpleNamespace(
            _notify=7, _file=open(read, "rb"), callbacks=[object()], go=True)
        lgpio._notify_thread = corpse
        try:
            yield corpse, write
        finally:
            corpse._file.close()
            os.close(write)

    def test_the_replacement_takes_over_the_handle_and_the_pipe(self, lgpio,
                                                                dead):
        corpse, _ = dead
        buttons_mod.revive_dispatch(corpse)

        fresh = lgpio._notify_thread
        assert fresh is not corpse, "nothing replaced the dead thread"
        assert fresh._notify == 7, (
            "the replacement opened a notification handle of its own. Every "
            "alert already claimed is routed to the old one (lgpio.py:1293) "
            "and would go on reaching nobody")
        assert fresh._file is corpse._file
        assert fresh.go is True and fresh.daemon is True

    def test_it_shares_the_callback_list_rather_than_copying_it(self, lgpio,
                                                                dead):
        """
        A copy diverges the moment anything registers again: `lgpio.callback`
        appends to whatever `_notify_thread` is at the time (lgpio.py:578),
        so a panel built afterwards would land its callbacks in a list the
        running thread had stopped reading -- the same silent deafness in a
        new place.
        """
        corpse, _ = dead
        buttons_mod.revive_dispatch(corpse)

        assert lgpio._notify_thread.callbacks is corpse.callbacks

    def test_it_is_published_before_it_is_started(self, lgpio, dead):
        corpse, _ = dead
        buttons_mod.revive_dispatch(corpse)
        lgpio._notify_thread.join(2)

        assert lgpio.order[0] == "published", (
            f"the thread was started before anything could find it, so a "
            f"gpio_claim_alert racing the repair would be routed to a "
            f"thread that is about to stop existing: {lgpio.order}")
        assert "ran" in lgpio.order, "the replacement was never started"

    def test_it_empties_the_pipe_before_anything_can_read_it(self, lgpio,
                                                            dead):
        corpse, write = dead
        os.write(write, b"e" * 160)

        assert buttons_mod.revive_dispatch(corpse) == 160
        lgpio._notify_thread.join(2)

    def test_the_dead_thread_is_left_exactly_as_it_was_found(self, lgpio,
                                                             dead):
        """
        Nothing is joined and nothing is stopped. A dead thread has nothing
        to wait for, and `_watch` runs on the app's event loop -- a join
        there is the panel frozen by its own supervisor.
        """
        corpse, _ = dead
        buttons_mod.revive_dispatch(corpse)
        lgpio._notify_thread.join(2)

        assert corpse.go is True
        assert not corpse._file.closed, (
            "the repair closed the pipe the replacement is reading")


class TestRevivingLgpiosOwnDispatchThread:
    """
    The claim the whole supervisor rests on, measured against lgpio itself.

    A supervisor that reports a recovery that did not happen is worse than
    no supervisor: the journal then says the buttons are back when every one
    of them is still inert. So the repair is not asserted through a stand-in
    here. lgpio's own `_callback_thread.run` dispatches these edges, lgpio's
    own dispatch loop dies of them, and the shipped `revive_dispatch` is
    what is asked to put it back.

    What is substituted is the TRANSPORT, for the reason `RealNotifier`
    gives: a pipe rather than the `.lgd-nfy*` FIFO lgpio opens at import, so
    that a test run does not depend on -- or kill -- the process-wide
    notification thread that everything else in the process shares. The read
    end is a `BufferedReader` over a pipe either way, which is what the
    drain below is about.

    Not measured here, because this container has no gpiochip: that the
    kernel keeps writing alerts into that handle while nothing reads it, and
    that they are delivered on the far side of a revival. `gpio-sim` is
    where that is answerable, and it is not available on this machine.
    """

    UP, SPARE = 5, 99

    @pytest.fixture
    def dispatcher(self, monkeypatch):
        """lgpio's dispatch thread, standing in as the process singleton."""
        pytest.importorskip(
            "lgpio", reason="lgpio is not a fast-suite dependency (it is "
                            "linux-only)")
        import lgpio

        made = RealNotifier()
        # So that `revive_dispatch`, which repoints the module global as
        # lgpio's own `callback()` and `gpio_claim_alert` read it, is
        # working on this one and not on the process's real dispatcher.
        # monkeypatch puts the real one back afterwards whatever happens.
        monkeypatch.setattr(lgpio, "_notify_thread", made._thread)
        try:
            yield made
        finally:
            # Whichever thread is now on the handle -- the revived one
            # shares this one's file object, and stopping the DEAD one
            # would close that file underneath a running reader.
            made._thread = lgpio._notify_thread
            made.stop()

    def kill(self, dispatcher, *extra):
        """
        Kill it the way an unguarded callback does, and prove it is dead.

        `extra` goes into the same write as the killing edge, which is how
        edges end up inside the reader's own buffer rather than in the pipe.
        """
        arrived = []
        dispatcher.register(self.UP, lambda c, g, l, t: arrived.append(l))
        dispatcher.register(
            self.SPARE,
            lambda c, g, l, t: (_ for _ in ()).throw(MemoryError("no room")))

        dispatcher.fire(self.UP)
        deadline = time.monotonic() + 2
        while not arrived and time.monotonic() < deadline:
            time.sleep(0.01)
        assert arrived == [0], (
            "an edge did not reach a callback before anything was made to "
            "fail, so nothing below is about a dispatcher that died")
        arrived.clear()

        dispatcher.fire_together(dispatcher.frame(self.SPARE), *extra)
        deadline = time.monotonic() + 2
        while dispatcher.alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not dispatcher.alive(), (
            "lgpio's dispatch loop survived an unguarded exception, so "
            "every assertion below is about a dispatcher lgpio does not "
            "have")
        return arrived

    def test_a_dead_dispatcher_is_deaf_even_to_callbacks_added_afterwards(
            self, dispatcher, unhandled):
        """
        Why the repair is not "rebuild the panel", stated as a measurement.

        The obvious reading of a dead panel is that fresh `Button`s would
        arm fresh callbacks and work again. They would not: `lgpio.callback`
        appends to `_notify_thread.callbacks` (lgpio.py:578) and
        `gpio_claim_alert` routes the kernel's edges to
        `_notify_thread._notify` (lgpio.py:1293) -- the module-level
        singleton, whether or not anything is still reading it. So a
        supervisor that rebuilt the panel and said so would have been
        announcing a recovery that had not happened.
        """
        arrived = self.kill(dispatcher)

        # Registered exactly as a rebuilt panel's would be, after the death.
        rebuilt = []
        dispatcher.register(6, lambda c, g, l, t: rebuilt.append(l))
        dispatcher.fire(6)
        dispatcher.fire(self.UP)
        time.sleep(0.3)

        assert rebuilt == [], (
            "a callback registered after the death received an edge, so "
            "the dispatcher is not the thing that has to be restarted")
        assert arrived == [], "a callback that was already armed still fires"
        assert len(unhandled) == 1 and unhandled[0].exc_type is MemoryError

    def test_edge_delivery_comes_back_on_the_registrations_already_there(
            self, dispatcher, unhandled):
        """
        The repair, and the property it has to buy: an edge on a callback
        that was armed BEFORE the death, delivered after it, with nothing
        re-registered and no `Button` rebuilt.
        """
        import lgpio

        arrived = self.kill(dispatcher)
        dispatcher.fire(self.UP)
        time.sleep(0.2)
        assert arrived == [], (
            "the dispatcher delivered an edge while dead, which means the "
            "revival below cannot be shown to have done anything")

        buttons_mod.revive_dispatch(dispatcher._thread)

        revived = lgpio._notify_thread
        assert revived is not dispatcher._thread, "nothing was replaced"
        assert revived.is_alive()
        assert revived.callbacks is dispatcher._thread.callbacks, (
            "the replacement took a COPY of the callback list, so anything "
            "registering afterwards lands in a list it never reads")

        dispatcher.fire(self.UP)
        deadline = time.monotonic() + 3
        while not arrived and time.monotonic() < deadline:
            time.sleep(0.01)
        assert arrived == [0], (
            f"no edge arrived after the revival: the panel is still deaf "
            f"and the journal would be saying otherwise "
            f"(alive={revived.is_alive()})")

    def test_edges_banked_while_the_dispatcher_was_dead_are_not_replayed(
            self, dispatcher, unhandled):
        """
        The half that makes recovery safe rather than harmful.

        Nothing drains lgpio's notification pipe while its thread is dead --
        the read end stays open, because a thread killed inside `cb.func`
        never reaches the `self._file.close()` at the end of `run()` -- so
        the presses of an operator jabbing at a panel that does nothing pile
        up in it. Measured without the drain: all sixty banked edges were
        delivered back to back at the moment of recovery. On this device
        that drives the menu at machine speed, and OK and BACK mean opposite
        things while a job is printing.

        The banked edges are written in the SAME call as the one that kills
        the thread, which is the case a drain of the file descriptor cannot
        see: `run` reads through a BufferedReader, so they are already
        inside Python by then. Measured, in this container: the kernel held
        0 bytes and the reader held 960.
        """
        banked = [dispatcher.frame(self.UP, level=n % 2) for n in range(60)]
        arrived = self.kill(dispatcher, *banked)
        assert arrived == [], "the banked edges were delivered before it died"

        discarded = buttons_mod.revive_dispatch(dispatcher._thread)
        time.sleep(0.3)

        assert arrived == [], (
            f"{len(arrived)} edge(s) banked against a dead dispatcher were "
            f"replayed on recovery. Those presses were aimed at a panel "
            f"that was not listening; on this unit replaying them can "
            f"cancel a job")
        assert discarded == 60 * 16, (
            f"the drain reported {discarded} bytes of a 960-byte backlog. "
            f"A drain that reads the descriptor rather than the file object "
            f"sees none of it -- the BufferedReader already has it")

        # And the panel is genuinely back, rather than quiet because the
        # drain is still eating everything.
        dispatcher.fire(self.UP)
        deadline = time.monotonic() + 3
        while not arrived and time.monotonic() < deadline:
            time.sleep(0.01)
        assert arrived == [0], "no edge arrived after the drain"


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

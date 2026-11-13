"""Tests for the hardware layer: the button threading and timing, and the failure
modes the fakes never exercise.

gpiozero, lgpio and luma are not test dependencies; tests that need them skip.
The timing logic is driven with a fake clock.
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

# Under the temp dir, not "/nonexistent": the suite runs as root, where that
# path is writable.
SCRATCH_CONFIG = str(pathlib.Path(tempfile.gettempdir()) /
                     "otp-unit-test.conf")

from otpunit.hw import buttons as buttons_mod
from otpunit.hw.buttons import GpioButtons, Press
from otpunit.hw.display import ConsoleDisplay, Frame, Ssd1306Display


class Clock:
    """A monotonic clock the test drives by hand, optionally failing once.

    Installed with `use_clock` as buttons.py's whole `time`, not patched onto
    the shared time module, so no other thread can spend the one-shot failure.
    `fails_with` is armed for the calling thread only.
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
    """One press produces exactly one event. While a job is printing OK and BACK
    mean opposite things, so a borderline press that emitted both could purge
    the spool and destroy a pad pair.
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
        # Sweep across the deadline: each press yields exactly one event,
        # whichever side it lands on.
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
        # lgpio's debounce drops an edge not stable for the whole window, so a
        # bounce time near a human tap loses presses. Contact bounce is 1-5ms.
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
    """Panel text is always encodable as latin-1, the range of Pillow's bitmap
    font; anything else raises inside the display driver. CUPS errors decoded
    with errors="replace" can carry U+FFFD or accents.
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
# lgpio dispatches every GPIO edge in the process from one unguarded singleton
# thread, so one exception out of a button callback silences every button until
# a power cycle. GpioButtons._guarded prevents that. The property asserted
# throughout is that a later press still arrives, not that an exception was
# caught.


class FakeButton:
    """The only part of gpiozero's `Button` that `GpioButtons` uses, so this
    section runs without gpiozero. The callbacks are the real ones, read back
    as handed over; `TestRealGpiozero` drives the same property through the
    real library.
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
    """The one thing `GpioButtons._watch` asks of lgpio's thread: is it alive.

    `probes` counts the questions, so "nothing happened" can be told from
    "never looked".
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
    """A real `GpioButtons`, with `FakeButton` standing in for gpiozero.

    The dispatch probe is stood in for too unless the caller supplies one: the
    shipped default reads lgpio's process-wide thread, which CI does not even
    install.
    """
    module = types.ModuleType("gpiozero")
    module.Button = FakeButton
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    # None when the caller brought its own probe, so reading
    # stand_in_dispatcher by mistake fails instead of passing vacuously.
    thread = None
    if "dispatcher" not in injected:
        thread = FakeDispatcher(alive=True)
        injected["dispatcher"] = lambda: thread
    injected.setdefault("reviver", Reviver())
    panel = GpioButtons(**injected)
    # What the panel was given, where a test can read it back.
    panel.stand_in_dispatcher = thread
    panel.stand_in_reviver = injected["reviver"]
    return panel


class MemoryStarvedQueue(queue.Queue):
    """The panel's event queue, raising MemoryError on the next `failures` puts.

    `self._events.put(...)` is the one allocation every shipped callback makes,
    so this raises from inside the shipped callback.
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
    """Stderr as the unit has it: a pipe with a reader the test can withdraw.

    The failure that matters is a full pipe, which does not raise: the writer
    parks in write(2) until somebody reads. Shrunk to one page with
    F_SETPIPE_SZ so `wedge` is quick.
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
        # Drain until nothing more comes, and only then close: a writer parked
        # in write(2) must be released, and closing underneath one blocks on
        # the file object's lock.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.take(timeout=0.2):
            pass
        try:
            self.stream.close()
        except OSError:                          # pragma: no cover
            pass
        os.close(self._read)


def dispatch_for(button):
    """gpiozero's chain from an lgpio edge to `when_pressed`.

    Unguarded, as gpiozero's real one is;
    `TestRealGpiozero::test_gpiozero_lets_a_callback_exception_straight_out`
    checks that against the library.
    """
    def dispatch(chip, gpio, level, tick):
        # 0 is the line pulled to ground, which for a pull_up=True Button is
        # "pressed".
        handler = button.when_pressed if level == 0 else button.when_released
        if handler is not None:
            handler()
    return dispatch


class StandInNotifier:
    """lgpio's notification thread in miniature: one thread, one callback list
    shared by every button, and a dispatch with no try around it, so the first
    exception out of a callback ends delivery on every line.

    The transport is a Queue, not lgpio's FIFO. `RealNotifier` runs the same
    scenarios through lgpio's own `run` where lgpio is installed.
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
    """lgpio's actual notification thread, fed forged edges through a pipe.

    The constructor's notification handle and `.lgd-nfy*` FIFO are skipped and
    the fields `run` reads are set up by hand; `run` itself is lgpio's own.
    """

    def __init__(self):
        import lgpio

        self.lgpio = lgpio
        thread = lgpio._callback_thread.__new__(lgpio._callback_thread)
        threading.Thread.__init__(thread)
        read, self._write = os.pipe()
        thread._notify = None
        # Buffered, as lgpio's own reader is: `read(16)` pulls up to 8192 bytes
        # into Python, which is where edges sit when a callback kills the
        # thread mid-burst.
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
        """Several edges in one write, so one buffered read takes them all and
        they sit inside Python where a drain of the descriptor cannot see them.
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
        # never gets there.
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
    """Every exception that escaped a thread: the only place a dying dispatch
    thread reports itself. Replacing the hook also keeps pytest's
    thread-exception warning out of the summary.
    """
    escaped = []
    monkeypatch.setattr(threading, "excepthook", escaped.append)
    return escaped


class TestNoCallbackExceptionReachesTheDispatchThread:

    def test_an_unguarded_callback_kills_every_button_in_the_process(
            self, notifier, panel, unhandled):
        """The control: an unguarded callback that raises really does silence
        every button on this dispatcher, so the tests below cannot pass against
        a notifier that swallows exceptions.
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

        # A healthy guarded button now delivers nothing at all.
        notifier.fire(buttons_mod.PIN_UP)
        assert panel.wait(timeout=0.5) is None

        assert len(unhandled) == 1, unhandled
        assert unhandled[0].exc_type is MemoryError
        assert panel.dropped == 0, (
            "nothing was guarded here, so nothing should have been counted")

    def test_a_press_on_another_button_survives_one_that_raises(
            self, notifier, panel):
        """After an edge whose callback raises, an edge on another button still
        arrives.
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
        """A raising release does not cost the next OK. OK is guarded on press and
        on release, and is the button that means opposite things while a job
        prints.
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
        """A full stderr pipe does not park the dispatch thread: a thread blocked
        in write(2) delivers no edges while still looking alive.

        First the control, that with a reader the report does reach stderr;
        then with the pipe wedged the next press must still arrive.
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
            # Wait for the reporter to have stamped `_said_at`, which happens
            # after `_report` returns; advancing the clock first would put the
            # second loss inside the interval and nothing would write to the
            # wedged pipe.
            stamped = time.monotonic() + 5
            while panel._said_at is None and time.monotonic() < stamped:
                time.sleep(0.01)
            assert panel._said_at is not None, (
                "the first loss was never stamped, so advancing the clock "
                "cannot make the second one due and the wedge below would "
                "be tested against a report that is never attempted")

            # Due again, so the next loss is one the reporter tries to write.
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
        """Twenty failures in a row leave the panel taking, losing and counting
        edges, and nothing is revived: a caught exception means the dispatch
        thread is alive, and the supervisor keys only on it being dead.

        Reporting stays bounded (see TestALostPressIsSaidOutLoud); the count
        asserted here is what survives that.
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
    """A lost press is written to the journal, the only record there is, but
    bounded: the first loss in full with its traceback, then at most one
    summary line every REPORT_SECONDS. Nothing may make the dispatch thread
    wait on a journald that is not reading.
    """

    @pytest.fixture
    def reports(self, monkeypatch):
        said = []
        monkeypatch.setattr(buttons_mod, "_report", said.append)
        return said

    def test_the_default_report_goes_to_stderr(self, capsys):
        """The default `_report` writes to stderr, so the tests below cannot pass
        against one that writes nowhere.
        """
        buttons_mod._report("otp: front panel: hello")
        captured = capsys.readouterr()
        assert "otp: front panel: hello" in captured.err
        assert captured.out == "", "stdout is the simulator's panel"

    def test_a_report_too_long_for_the_pipe_is_cut_rather_than_parked(self):
        """A report longer than PIPE_BUF is truncated, not written whole. select()
        promises only PIPE_BUF (4096) bytes without blocking, and a parked
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
        """Every line, summaries included, names its press, so the journal says
        which button is failing.
        """
        clock = use_clock(monkeypatch)
        unit = build_panel(monkeypatch)
        unit._events = MemoryStarvedQueue()
        by_pin = {button.pin: button for button in unit._buttons}

        unit._events.failures = 1
        by_pin[buttons_mod.PIN_UP].when_pressed()
        # Past the summary interval, so the second loss gets a line of its own.
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
        # The traceback says which line raised.
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
        """A burst of losses writes one full report and then counts, with a single
        summary line per interval: the journal must not be flooded just when
        the panel is in trouble.
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

        # When the interval is up, one line carrying both totals, not a second
        # traceback.
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
        """Losing a press start is reported as also costing the OK, because
        `_on_release` ignores a release it saw no press for.
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
        """A failed `_on_press` clears any earlier press timestamp; a stale one
        would make the next release measure as a hold and emit BACK for an OK.

        The stale timestamp is planted by hand, since there is no route to it
        from outside.
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
        """A report that raises is swallowed: reporting allocates and writes to a
        pipe that can fail, and the log entry is lost sooner than the panel.
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
# The guard cannot help once the dispatch thread is gone. The supervisor
# notices, restores delivery, logs, and changes nothing else. Its signal is the
# thread's liveness, not a silence threshold: an idle unit is quiet too.


class TestTheDispatchThreadIsWatched:
    """The supervisor's policy, with the probe and the repair injected so it runs
    without lgpio. `TestRevivingLgpiosOwnDispatchThread` is where a revival is
    shown to work.
    """

    @pytest.fixture
    def reports(self, monkeypatch):
        said = []
        monkeypatch.setattr(buttons_mod, "_report", said.append)
        return said

    def test_a_healthy_dispatcher_is_never_restarted_and_never_mentioned(
            self, monkeypatch, reports):
        """A healthy dispatcher causes no revival and no journal line; a
        supervisor that logs on a working unit gets ignored.
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
        """When `_discard_backlog` stops at BACKLOG_MAX_BYTES, the report says the
        remainder will be replayed, not that everything was discarded.
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
        """A revival leaves the three `Button`s the same objects, open, with the
        same callbacks. Rebuilding them would bounce the lines under the
        operator's finger and risk stale pins (see pirig.release_gpiozero).
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
        """A revival that raises costs a log line and the panel keeps answering.
        An exception out of `_watch` would exit the process and restart the
        unit every RestartSec, each time discarding the pad in progress.
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
        """A dispatcher that stays dead is retried at a bounded rate. `_watch`
        runs on every `wait`, including each progress step of a job, when
        memory is tightest.
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
        """A revival clears the press timestamp, so a release arriving after it is
        not measured as a hold and emitted as BACK, which purges the spool
        mid-job.

        The control comes first: on a live dispatcher the same stale timestamp
        does produce BACK.
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
        """A failing report about a revival is swallowed: `_watch` runs on the
        app's event loop, and losing the log line beats losing the loop.
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
        """A probe that raises does not escape `_watch`, which runs on the thread
        that draws the panel.
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
        """After `close()` no revival is attempted: the panel is being torn down,
        and a revival could race it.
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

        # The control: the same fakes on an open panel do get a revival.
        open_panel = build_panel(monkeypatch, dispatcher=lambda: thread,
                                 reviver=reviver)
        open_panel.wait(timeout=0)
        assert reviver.called == [thread]

    def test_a_wait_with_no_deadline_still_looks_at_the_dispatcher(
            self, monkeypatch):
        """A `wait()` with no timeout still probes the dispatcher periodically.
        The main menu blocks that way, and a dispatcher that died while idle
        would otherwise never be noticed.
        """
        monkeypatch.setattr(buttons_mod, "WATCH_SECONDS", 0.01)
        panel = build_panel(monkeypatch, reviver=Reviver())
        thread = FakeDispatcher(alive=True)

        def dispatcher():
            # A press appears while the panel is already blocked, as on an idle
            # menu.
            if thread.probes == 2:
                panel._events.put(Press.UP)
            return thread

        panel._dispatcher = dispatcher

        # On its own thread, joined with a deadline: the failure mode is
        # `wait()` never returning.
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
            # Release a stuck waiter so a failure does not leave a thread
            # parked.
            panel._events.put(Press.QUIT)
            waiter.join(2)


class TestFindingLgpiosDispatchThread:
    """`dispatch_thread` finds lgpio's thread without importing lgpio: the import
    creates a FIFO in the cwd and raises where that is read-only.
    """

    def test_a_process_that_never_imported_lgpio_has_nothing_to_supervise(
            self, monkeypatch):
        """With lgpio never imported the answer is None, as on a unit whose panel
        came up on another pin factory. The supervisor then does nothing,
        though other factories dispatch unguarded too (issue #43).
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
        """The control: with lgpio present it returns the module-level singleton,
        so the None cases above are not unconditional.
        """
        stand_in = types.ModuleType("lgpio")
        stand_in._notify_thread = sentinel = object()
        monkeypatch.setitem(sys.modules, "lgpio", stand_in)
        assert buttons_mod.dispatch_thread() is sentinel

    def test_an_lgpio_without_a_dispatch_thread_is_not_an_error(
            self, monkeypatch):
        # A partially imported lgpio is in sys.modules with no `_notify_thread`
        # yet; `_watch` may not raise, so that is answered too.
        monkeypatch.setitem(sys.modules, "lgpio", types.ModuleType("lgpio"))
        assert buttons_mod.dispatch_thread() is None


class WatchedReader:
    """A notification pipe's read end that raises if read while it could block, so
    a bad drain fails instead of hanging.

    `blocking` records every read; an empty list means nothing was read at all.
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
    """The drain, on a pipe, with no lgpio and no panel. Presses made at a panel
    that was not listening sit in that pipe, and replaying them would drive the
    menu at machine speed.
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
        """The drain discards what the BufferedReader already holds, not only what
        is in the pipe: lgpio's `read(16)` pulls up to 8192 bytes into Python.
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
        """Every read happens with O_NONBLOCK set, and the descriptor is blocking
        again afterwards, as lgpio's own loop requires.

        The BufferedReader's lock is not covered; see `_discard_backlog`'s
        docstring.
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
        """The drain stops at its bound when a writer keeps up, so it cannot hold
        the app's event loop.

        The stand-in writer stops at three times the bound, so a missing bound
        fails instead of hanging.
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
    """What `revive_dispatch` builds, with a stand-in module for lgpio so it runs
    in CI. The class built is a real `threading.Thread` subclass initialised as
    lgpio's own is.
    """

    @pytest.fixture
    def lgpio(self, monkeypatch):
        """A module named lgpio with only what the repair touches on it."""
        module = types.ModuleType("lgpio")
        module.order = []

        class CallbackThread(threading.Thread):
            def start(self):
                # Recorded on the caller's thread: whether the module global
                # was repointed before the thread began is a question of order.
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
        """The replacement shares the dead thread's callback list.
        `lgpio.callback` appends to whatever `_notify_thread` is, so a copy
        would leave later registrations unread.
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
        """The dead thread is neither joined nor stopped: `_watch` runs on the
        app's event loop, where a join would freeze the panel.
        """
        corpse, _ = dead
        buttons_mod.revive_dispatch(corpse)
        lgpio._notify_thread.join(2)

        assert corpse.go is True
        assert not corpse._file.closed, (
            "the repair closed the pipe the replacement is reading")


class TestRevivingLgpiosOwnDispatchThread:
    """The repair measured against lgpio itself: lgpio's own `run` dispatches the
    edges and dies of them, and the shipped `revive_dispatch` puts it back.

    Only the transport is substituted, a pipe for the `.lgd-nfy*` FIFO, so the
    process-wide notification thread is untouched. Not covered: kernel alerts
    arriving on a real gpiochip while nothing reads.
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
        # So `revive_dispatch` works on this thread and not on the process's
        # real dispatcher.
        monkeypatch.setattr(lgpio, "_notify_thread", made._thread)
        try:
            yield made
        finally:
            # Whichever thread is now on the handle: the revived one shares the
            # file object, and stopping the dead one would close it under a
            # running reader.
            made._thread = lgpio._notify_thread
            made.stop()

    def kill(self, dispatcher, *extra):
        """Kill it the way an unguarded callback does, and prove it is dead.
        `extra` goes into the same write as the killing edge, so it ends up in
        the reader's buffer.
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
        """A callback registered after the death receives nothing: lgpio routes
        registrations and edges to the module-level singleton whether or not it
        is running. So rebuilding the panel is not a repair.
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
        """After a revival an edge reaches a callback that was armed before the
        death, with nothing re-registered.
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
        """Edges banked while the dispatcher was dead are discarded on revival,
        not delivered back to back into the menu.

        They are written in the same call as the killing edge, so they sit in
        the BufferedReader where a drain of the descriptor alone would miss
        them.
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

        # And the panel is back, not quiet because the drain is still eating
        # everything.
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
        """gpiozero does not catch a callback's exception: with MockFactory it
        comes straight out of `drive_low()`. This is the assumption
        `dispatch_for` rests on.
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
        """Through real gpiozero, a press whose shipped callback raises does not
        escape and the next press arrives. The test above is its control.
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

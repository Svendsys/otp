"""Button back-ends for the print unit's front panel.

Three buttons: UP, DOWN, OK. A long press on OK is BACK, which is what makes
three buttons enough to navigate menus, edit values, and cancel a running
job without a fourth switch to wire up.
"""
from __future__ import annotations

import queue
import select
import sys
import termios
import time
import traceback
import tty
from enum import Enum


class Press(Enum):
    UP = "up"
    DOWN = "down"
    OK = "ok"
    BACK = "back"          # long press on OK
    QUIT = "quit"          # simulator only


# GPIO pins, chosen to avoid the HAT EEPROM (0/1), I2C (2/3) and UART (14/15).
PIN_UP = 5
PIN_DOWN = 6
PIN_OK = 13
HOLD_SECONDS = 1.0
BOUNCE_SECONDS = 0.015
#: After the first lost edge is reported in full, one line at most this
#: often. See GpioButtons._note_lost for why a lost edge must not be free
#: to write as often as it likes.
REPORT_SECONDS = 5.0
#: And no single report longer than this, in characters. Below PIPE_BUF
#: (4096), which is the size of write that a pipe reporting itself writable
#: promises to take without blocking -- see _report.
REPORT_MAX_CHARS = 3500


def _report(message: str) -> None:
    """
    Where a lost press goes.

    stderr, which is what the rest of this program uses (see __main__.main's
    `log`) and which on the unit is the journal. Not the panel: the panel is
    a 128x64 OLED showing a menu, and a device driven by three buttons has
    nowhere to put a message that does not cost the screen someone is
    reading. A separate function so that there is one place to change if
    that ever stops being true, and so a test can watch it.

    It must not BLOCK, and that does not come for free. On the unit stderr
    is a stream socket to journald, and writing to a socket nobody is
    draining does not fail -- it parks the caller inside write(2) until
    someone reads. The caller here is lgpio's single dispatch thread (see
    GpioButtons._guarded), so a journald that is restarting, wedged or
    merely behind would cost every button on the device for as long as it
    lasted: the same operator-visible outcome as the exception the guard
    catches, and harder to diagnose, because the thread is still alive and
    simply never comes back. Measured before this was here -- a pipe shrunk
    to 4 KiB with F_SETPIPE_SZ, no reader, sys.stderr pointed at it, one
    `_report` on a thread: the thread never returned. And the moment it
    would happen is exactly the moment the panel is losing edges, which is
    when the journal is being written to hardest.

    So ask first, and drop the line if the answer is no: select() with a
    zero timeout is a question, not a wait. A pipe reports itself writable
    only with PIPE_BUF (4096) bytes of room, and a write of at most that
    much is atomic; an AF_UNIX stream socket reports itself writable only
    with more than half its send buffer free, which is tens of kilobytes.
    A message capped below PIPE_BUF that passes the question therefore
    cannot then park in the kernel. The hole left is another thread filling
    the buffer between the question and the write, which would have to put
    kilobytes into that window; closing it would mean holding a lock on the
    dispatch thread, which is a worse trade than a log line lost during a
    journald outage.

    A stream with no file descriptor -- pytest's capture, a StringIO -- has
    nothing to ask and cannot block, so it is simply written to.
    """
    stream = sys.stderr
    if stream is None:
        # print(file=None) writes to STDOUT, which on the simulator is the
        # panel itself. Losing the line beats drawing on it.
        return
    if len(message) > REPORT_MAX_CHARS:
        message = message[:REPORT_MAX_CHARS] + " [...truncated]"
    try:
        fd = stream.fileno()
    except Exception:                           # noqa: BLE001
        fd = None                               # in memory; cannot block
    if fd is not None and not select.select((), (fd,), (), 0)[1]:
        # Nothing is reading the journal socket. The count in the next
        # line that does get out still includes whatever this one said.
        return
    print(message, file=stream, flush=True)


class Buttons:
    """Interface every back-end implements."""

    def wait(self, timeout: float | None = None) -> Press | None:
        """Block for the next press, or return None if `timeout` elapses."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class FakeButtons(Buttons):
    """Replays a scripted sequence of presses, for tests."""

    def __init__(self, script=()):
        self._script = list(script)
        self.exhausted = False

    def push(self, *presses: Press) -> None:
        self._script.extend(presses)

    def wait(self, timeout: float | None = None) -> Press | None:
        if not self._script:
            self.exhausted = True
            # timeout=0 means "is anything pending?" -- answering QUIT there
            # would make a non-blocking drain loop see an endless supply of
            # presses. Only the blocking form ends a scripted run.
            return None if timeout == 0 else Press.QUIT
        return self._script.pop(0)


class KeyboardButtons(Buttons):
    """
    Maps terminal keys to presses.

    Originally for --sim only. It is now also the input path for a real
    unit with a monitor and a USB keyboard, which changes two things.

    `allow_quit` is off by default because on a real unit QUIT is fatal:
    App.run() breaks, main() returns 0, and systemd's Restart=on-failure
    treats a zero exit as success and does NOT restart. One `q` -- a key
    the on-screen footer used to advertise -- turned the appliance off
    until someone power-cycled it. The simulator still passes it True.
    """

    KEYS = {
        "u": Press.UP, "k": Press.OK, "d": Press.DOWN,
        "K": Press.BACK,
        "\x1b[A": Press.UP, "\x1b[B": Press.DOWN,
        "\r": Press.OK, "\n": Press.OK,
    }
    QUIT_KEYS = {"q", "\x03"}

    def __init__(self, allow_quit: bool = False):
        self.allow_quit = allow_quit

    def _map(self, key):
        if key in self.QUIT_KEYS:
            return Press.QUIT if self.allow_quit else None
        return self.KEYS.get(key)

    def wait(self, timeout: float | None = None) -> Press | None:
        # Piped stdin has no terminal to put into raw mode. Falling back to
        # line reads keeps --sim scriptable, which is what lets a demo or a
        # smoke test drive the panel without a person at the keyboard.
        if not sys.stdin.isatty():
            # A non-blocking poll (timeout=0) is how the job screen discards
            # presses banked while generation blocked the loop. A script has
            # no banked presses -- every line is a deliberate step, and all
            # of them are readable the instant the pipe opens -- so report
            # nothing pending rather than swallowing the rest of the script.
            if timeout == 0:
                return None
            if timeout is not None:
                ready, _, _ = select.select([sys.stdin], [], [], timeout)
                if not ready:
                    return None
            line = sys.stdin.readline()
            if not line:
                # EOF. Only the simulator may read this as "quit" -- on a
                # real console a hangup would otherwise end the process,
                # and a zero exit tells systemd not to restart.
                return Press.QUIT if self.allow_quit else None
            return self._map(line.strip()[:1] or "\n")

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            # TCSANOW, not setraw's TCSAFLUSH default. TCSAFLUSH DISCARDS
            # the pending input queue, and wait() enters raw mode on every
            # call -- so every key pressed while the app was rendering a
            # frame was thrown away by the terminal driver. Measured at 27%
            # loss just from panel redraws, approaching 100% while a pad is
            # generating.
            tty.setraw(fd, termios.TCSANOW)
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                # An escape byte may be a lone Esc or the start of an arrow
                # sequence, and in raw mode nothing distinguishes them but
                # time. A bare read(2) here blocked FOREVER on a single Esc
                # -- the most natural key to press at a prompt -- which
                # recreated the exact hang that Interface.prove() exists to
                # prevent, inside prove() itself.
                for _ in range(2):
                    ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if not ready:
                        break
                    ch += sys.stdin.read(1)
            return self._map(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class GpioButtons(Buttons):
    """The real front panel: three momentary switches to ground."""

    # How many edges the guard below has eaten, and when it last said so.
    # Set per panel in __init__; these class-level values are only a
    # fallback, so that a unit built with __new__ -- which several tests do,
    # to drive the timing logic without a gpiozero -- still has something to
    # count with. An AttributeError here would NOT escape the guard: the
    # counting happens inside the inner try, so the cost of missing these
    # would be the log line, not the thread. The fallback is so that panel
    # still reports, not so that it survives.
    #
    # `_dropped` and not `dropped` because `self.dropped += 1` on a class
    # attribute rebinds an INSTANCE one: the class attribute would then read
    # 0 forever on a unit that had lost hundreds of edges, which is a trap
    # for anything that later goes looking for the count. The count is per
    # panel -- which is what the message says -- and is read through the
    # `dropped` property below, off a panel and never off the class.
    _dropped = 0
    _said = 0                   # `_dropped` as of the last line printed
    _said_at = None             # and when that was, on the monotonic clock

    @property
    def dropped(self) -> int:
        """Edges this panel's guard has eaten since it was built."""
        return self._dropped

    def __init__(self, up=PIN_UP, down=PIN_DOWN, ok=PIN_OK):
        from gpiozero import Button

        self._events: queue.Queue[Press] = queue.Queue()
        self._buttons = []
        self._pressed_at = None
        self._dropped = 0
        self._said = 0
        self._said_at = None

        for pin, press in ((up, Press.UP), (down, Press.DOWN)):
            button = Button(pin, pull_up=True, bounce_time=BOUNCE_SECONDS)
            button.when_pressed = self._guarded(
                lambda p=press: self._events.put(p), f"the {press.name} press")
            self._buttons.append(button)

        # OK distinguishes tap from hold by timing the press, and decides on
        # RELEASE. The obvious alternative -- gpiozero's when_held setting a
        # flag that when_released checks -- is a check-then-act split across
        # two threads: gpiozero runs when_held on its own HoldThread and
        # when_released on the pin callback thread. If a release lands in the
        # ~30us between the hold thread deciding and it setting the flag, one
        # press emits OK *and* BACK and leaves the flag stuck, swallowing the
        # next tap. On this device that is not cosmetic: OK and BACK mean
        # opposite things while a job is printing, so a single borderline
        # press could purge the spool and destroy a pad pair.
        #
        # when_pressed and when_released both run on the pin callback thread,
        # so measuring between them needs no lock and cannot double-fire.
        okay = Button(ok, pull_up=True, bounce_time=BOUNCE_SECONDS)
        okay.when_pressed = self._guarded(
            self._on_press,
            "the start of an OK press, so the release after it is ignored")
        okay.when_released = self._guarded(
            self._on_release, "the OK or BACK this release would have been")
        self._buttons.append(okay)

    def _guarded(self, action, lost):
        """
        `action`, wrapped so that nothing at all can get out of it.

        Every GPIO edge in this process is dispatched by ONE thread inside
        lgpio, and its dispatch loop has no guard around the call
        (lgpio 0.2.2.0, lgpio.py:531-559):

            for cb in self.callbacks:
                if cb.chip == chip and cb.gpio == gpio:
                    cb.func(chip, gpio, level, tick)

        That thread is a module-level singleton started at import
        (`_notify_thread = _callback_thread()`, lgpio.py:562, whose
        constructor ends in `self.start()`), and nothing anywhere restarts
        it. gpiozero adds no guard of its own on the way here either --
        `LGPIOPin._call_when_changed`, `PiPin._call_when_changed`,
        `Button._pin_changed`, `EventsMixin._fire_events` and
        `_fire_activated` each call straight through. So one exception out
        of the callback below ends that thread, and from then on NO button
        anywhere in the process ever fires again: not this one, not the
        other two, not a panel built afterwards. It is reported as a
        `PytestUnhandledThreadExceptionWarning` under pytest and as an
        ignored-exception line otherwise, which is to say nowhere anyone is
        looking. Observed exactly that way while fixing issue #12 --
        `lgpio: notify thread alive=False go=True` -- where it made the
        harness's panel work once per process and nothing else.

        On a real unit the panel is built once, so #12's own trigger (a
        collected Button's dead weakref) does not arise. The class of fault
        does: this board is a Pi Zero 2 W with 512 MiB and no swap
        (tests/test_memory_budget.py) making pads of up to 1000 pages, so a
        `MemoryError` -- or any unexpected `OSError` out of lgpio -- landing
        in a callback would permanently disable the only input the device
        has. The remedy for that is a power cycle, which throws away the
        pad in progress and the key material with it.

        So the trade this makes is deliberate and one-directional: a lost
        edge, said out loud -- at a bounded rate and never into a write
        that could block, see `_note_lost` and `_report` -- in exchange for
        a panel that is still there for the next press. It does NOT retry,
        restart or escalate; see the PR for why a supervisor is a separate
        conversation.
        """
        def guarded(_button=None):
            try:
                action()
            except BaseException as exc:        # noqa: BLE001
                # BaseException rather than Exception, and the reason is
                # local to this thread. Letting one through does not end
                # the process the way it would on the main thread --
                # Python ends the THREAD, and this thread is the only one
                # delivering edges -- so the usual argument for re-raising
                # a KeyboardInterrupt or a SystemExit buys nothing here and
                # costs three dead buttons. MemoryError, the one actually
                # expected, is an ordinary Exception either way.
                #
                # A WARNING for whoever writes the next test, though:
                # pytest.fail, pytest.skip and pytest's assertion-rewriting
                # machinery all raise BaseException subclasses, so an
                # assertion made INSIDE a callback wrapped here is caught,
                # counted as a lost edge, and the test goes green having
                # proved nothing. Assert on what the callback did -- what
                # reached the queue, what `dropped` says -- from the test's
                # own thread, which is what every test in
                # tests/test_hardware.py does.
                try:
                    self._note_lost(lost, exc)
                except BaseException:           # noqa: BLE001
                    # The only swallow in here, and it is the last one
                    # available. Reporting allocates -- an f-string, a
                    # formatted traceback -- which is exactly what a
                    # MemoryError breaks next, and the write can fail on a
                    # closed stderr. Losing the log entry is bad; losing
                    # every button on the device because the log entry
                    # could not be written is worse.
                    pass
        return guarded

    def _note_lost(self, lost, exc) -> None:
        """
        Count one lost edge and, if it is due, say so.

        Bounded on purpose, and the bound is the point. A full report is
        eleven lines and about 690 bytes, and the faults that produce one
        produce them by the dozen: a panel dropping every edge would write
        one per press, unthrottled, into the same journald whose blocking
        is what `_report` is careful about. So the FIRST loss gets the
        whole thing, traceback included, because that is the one that says
        where the fault is; after it, one line at most every
        REPORT_SECONDS, carrying the running total, which is enough to see
        the shape of a burst without paying its volume.

        What that costs, stated because the journal is the only diagnostic
        this device has: the last few losses of a burst may appear only in
        the count printed by whatever line comes next, and a later loss of
        a DIFFERENT kind names its type and its message but not its
        traceback.
        """
        self._dropped += 1
        first = self._dropped == 1
        due = (self._said_at is None
               or (time.monotonic() - self._said_at) >= REPORT_SECONDS)
        if not first and not due:
            return                              # counted, said later
        head = (f"otp: front panel: {lost} was LOST -- "
                f"{type(exc).__name__}: {exc}")
        if first:
            _report(
                f"{head}\n"
                f"otp: front panel: {self._dropped} edge(s) lost since "
                f"this panel was built; the buttons still work.\n"
                f"otp: front panel: further losses are counted and "
                f"summarised, at most one line every {REPORT_SECONDS:g}s.\n"
                f"{traceback.format_exc().rstrip()}")
        else:
            _report(
                f"{head}; {self._dropped} edge(s) lost since this panel "
                f"was built ({self._dropped - self._said} since the last "
                f"line); the buttons still work.")
        # Stamped whether or not the line reached the journal. `_report`
        # drops a line rather than wait for a wedged journald, and retrying
        # every edge would only measure the wedge; the total in the next
        # line that does get out is right either way.
        self._said, self._said_at = self._dropped, time.monotonic()

    def _on_press(self, _button=None):
        # Cleared first, then set. If the clock read below fails, the guard
        # reports that "the release after it is ignored" -- and a timestamp
        # left over from an earlier press would make that a lie in the
        # expensive direction: `_on_release` would find a start time old
        # enough to call the next release a hold and emit BACK where the
        # operator pressed OK, which while a job is printing are opposite
        # things. gpiozero alternates pressed/released and `_on_release`
        # clears before it can fail, so there is no path here today; one
        # line makes the report's promise true without needing there to
        # not be one.
        self._pressed_at = None
        self._pressed_at = time.monotonic()

    def _on_release(self, _button=None):
        started, self._pressed_at = self._pressed_at, None
        if started is None:
            # Released without a press we saw -- ignore rather than guess.
            return
        held = (time.monotonic() - started) >= HOLD_SECONDS
        self._events.put(Press.BACK if held else Press.OK)

    def wait(self, timeout: float | None = None) -> Press | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        for button in self._buttons:
            try:
                button.close()
            except Exception:
                pass

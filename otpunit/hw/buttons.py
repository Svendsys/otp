"""Button back-ends for the print unit's front panel.

Three buttons: UP, DOWN, OK. A long press on OK is BACK, which is what makes
three buttons enough to navigate menus, edit values, and cancel a running
job without a fourth switch to wire up.
"""
from __future__ import annotations

import atexit
import os
import queue
import select
import sys
import termios
import threading
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
#: Minimum interval between lost-edge reports after the first (_note_lost).
REPORT_SECONDS = 5.0
#: Cap on one report, in characters. Below PIPE_BUF (4096) so a write to a
#: pipe that reports itself writable cannot block (see _report).
REPORT_MAX_CHARS = 3500
#: Longest an untimed wait() sits before re-checking the dispatch thread:
#: the worst case for a dead dispatcher going unnoticed on an idle menu.
WATCH_SECONDS = 2.0
#: Minimum interval between revival attempts, successful or not. Starting a
#: thread allocates and the expected fault is MemoryError, so this bounds
#: the memory and journal lines spent on a panel that keeps dying.
REVIVE_SECONDS = 30.0
#: Most bytes _discard_backlog reads before giving up. Sixteen times the
#: default 64 KiB pipe, so a pipe still being written to cannot make the
#: drain loop forever.
BACKLOG_MAX_BYTES = 1 << 20
#: Preallocated: the drain runs after the dispatch thread has died, most
#: likely of a MemoryError, when allocating is least likely to work.
_BACKLOG_BUFFER = memoryview(bytearray(4096))


def _report(message: str) -> None:
    """
    Write a panel fault to stderr (the journal on the unit). Never blocks.

    The caller is lgpio's single dispatch thread, and a write to a journal
    socket nobody is draining would park it, costing every button. So
    select() with a zero timeout asks first and the line is dropped if the
    stream is not writable; a message capped below PIPE_BUF that passes
    cannot then block. A stream with no file descriptor is written directly.
    """
    stream = sys.stderr
    if stream is None:
        # print(file=None) writes to STDOUT, which on the simulator is the
        # panel itself. Losing the line beats drawing on it.
        return
    if not message.isascii():
        # PIPE_BUF counts bytes and the cap below counts characters, which
        # match only for ASCII, so escape anything else.
        message = message.encode("ascii", "backslashreplace").decode("ascii")
    if len(message) > REPORT_MAX_CHARS:
        message = message[:REPORT_MAX_CHARS] + " [...truncated]"
    try:
        fd = stream.fileno()
    except Exception:                           # noqa: BLE001
        fd = None                               # in memory; cannot block
    if fd is not None and not select.select((), (fd,), (), 0)[1]:
        # Nothing is reading the journal: drop the line rather than block.
        return
    print(message, file=stream, flush=True)


def dispatch_thread():
    """
    lgpio's edge dispatch thread, or None if this process has no such thing.

    Looked up in sys.modules, not imported: importing lgpio creates a FIFO
    in the working directory and raises if it cannot. None also covers
    gpiozero's other pin factories (rpigpio, pigpio, native), which have no
    lgpio thread; a dead dispatcher there is not supervised (issue #43).
    """
    lgpio = sys.modules.get("lgpio")
    return None if lgpio is None else getattr(lgpio, "_notify_thread", None)


def _discard_backlog(source) -> int:
    """
    Throw away every edge record waiting on `source`; return bytes dropped.

    While the dispatch thread is dead its pipe keeps filling, and a revived
    thread would replay those presses at machine speed. OK and BACK mean
    opposite things while a job prints, so they are discarded instead.

    Reads through the file object, not the descriptor: lgpio's
    BufferedReader may hold records the kernel no longer has, and
    `readinto1` empties both. Non-blocking for the duration, so reaching an
    empty pipe returns rather than parks.

    Only safe when no other thread is reading `source`: a reader parked in
    read(2) holds the BufferedReader's lock and would wedge this call with
    the descriptor left non-blocking. `_watch` ensures that by calling it
    only for a dead thread, from the app's single event-loop thread.
    """
    fd = source.fileno()
    dropped = 0
    os.set_blocking(fd, False)
    try:
        while dropped < BACKLOG_MAX_BYTES:
            try:
                got = source.readinto1(_BACKLOG_BUFFER)
            except BlockingIOError:              # pragma: no cover
                break                            # nothing left to take
            if not got:
                # None (EAGAIN) or 0 (end of file): nothing left.
                break
            dropped += got
    finally:
        # A descriptor left non-blocking would break lgpio's read loop and
        # leave the panel deaf.
        os.set_blocking(fd, True)
    return dropped


def revive_dispatch(dead) -> int:
    """
    Put a running thread back on lgpio's notification handle. Bytes dropped.

    Restores the thread over the handle and pipe the dead one had, not a
    fresh `_callback_thread()`: existing claims and callbacks are bound to
    the module-level singleton, so rebuilding Buttons would not help, and a
    fresh one creates another FIFO on disk and leaks the old handle.

    The callbacks list is shared by identity, since `lgpio.callback()`
    appends to whatever `_notify_thread` is current. The module global is
    repointed before the thread starts so a racing `gpio_claim_alert`
    cannot be routed to the dead thread.

    Not verifiable without a gpiochip: that lgpio's C side keeps writing to
    the handle while nothing reads it (`pytest -m hardware` with gpio-sim).
    """
    lgpio = sys.modules["lgpio"]
    source = dead._file
    discarded = _discard_backlog(source)

    fresh = lgpio._callback_thread.__new__(lgpio._callback_thread)
    threading.Thread.__init__(fresh)
    fresh._notify = dead._notify
    fresh._file = source
    fresh.callbacks = dead.callbacks
    fresh.go = True
    fresh.daemon = True
    lgpio._notify_thread = fresh
    fresh.start()
    return discarded


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
            # timeout=0 asks "is anything pending?"; answering QUIT there
            # would feed a non-blocking drain loop forever.
            return None if timeout == 0 else Press.QUIT
        return self._script.pop(0)


class KeyboardButtons(Buttons):
    """
    Maps terminal keys to presses, for --sim and for a real unit with a
    monitor and a USB keyboard.

    `allow_quit` is off by default because on a real unit QUIT ends the
    menu loop and the process with it. The simulator passes True.
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
        # Terminal settings from before raw mode, for close() to restore.
        # Raw mode is entered once and held: a console left cooked between
        # waits echoes keys over the panel and turns Ctrl+C into a SIGINT
        # mid-job.
        self._saved_tty = None

    def close(self) -> None:
        if self._saved_tty is not None:
            fd, saved = self._saved_tty
            self._saved_tty = None
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            except (termios.error, OSError, ValueError):
                pass

    def _map(self, key):
        if key in self.QUIT_KEYS:
            return Press.QUIT if self.allow_quit else None
        return self.KEYS.get(key)

    def wait(self, timeout: float | None = None) -> Press | None:
        # Piped stdin has no terminal to put into raw mode; line reads keep
        # --sim scriptable.
        if not sys.stdin.isatty():
            # A timeout=0 poll discards banked presses. A script has none:
            # every line is a deliberate step, so report nothing pending.
            if timeout == 0:
                return None
            if timeout is not None:
                ready, _, _ = select.select([sys.stdin], [], [], timeout)
                if not ready:
                    return None
            line = sys.stdin.readline()
            if not line:
                # EOF. Only the simulator may read this as "quit"; on a real
                # console a hangup must not end the process.
                return Press.QUIT if self.allow_quit else None
            return self._map(line.strip()[:1] or "\n")

        fd = sys.stdin.fileno()
        if self._saved_tty is None:
            self._saved_tty = (fd, termios.tcgetattr(fd))
            atexit.register(self.close)
            # TCSANOW, not setraw's TCSAFLUSH default, which would discard
            # keys typed before the first wait().
            tty.setraw(fd, termios.TCSANOW)
        # os.read on the descriptor, never sys.stdin.read: the buffered
        # text layer swallows an arrow key's three bytes in one read and
        # select() then sees an empty descriptor.
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
        ch = os.read(fd, 1).decode("latin-1")
        if ch == "\x1b":
            # A lone Esc or the start of an arrow sequence; in raw mode only
            # time distinguishes them, so the follow-up reads are bounded.
            for _ in range(2):
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    break
                ch += os.read(fd, 1).decode("latin-1")
        return self._map(ch)


class GpioButtons(Buttons):
    """The real front panel: three momentary switches to ground."""

    # Class-level fallbacks for a panel built with __new__ (tests do this);
    # __init__ sets them per panel. `_dropped` is read through the `dropped`
    # property, never off the class, because `+=` rebinds it per instance.
    _dropped = 0
    _said = 0                   # `_dropped` as of the last line printed
    _said_at = None             # and when that was, on the monotonic clock

    # Supervisor state, with the same fallback. staticmethod() so reading
    # them off the class yields the plain function; both are replaced per
    # panel in __init__.
    _closed = False
    _revived = 0                # dispatch threads this panel has restarted
    _revive_failed = 0          # and attempts that raised instead
    _revive_at = None           # when the last attempt was, either way
    _dispatcher = staticmethod(dispatch_thread)
    _reviver = staticmethod(revive_dispatch)

    @property
    def dropped(self) -> int:
        """Edges this panel's guard has eaten since it was built."""
        return self._dropped

    @property
    def revived(self) -> int:
        """
        Dead dispatch threads this panel has restarted since it was built.

        Kept separate from `dropped`: a dropped edge is one lost press on a
        working panel; a revival means every press was going nowhere.
        """
        return self._revived

    def __init__(self, up=PIN_UP, down=PIN_DOWN, ok=PIN_OK,
                 dispatcher=dispatch_thread, reviver=revive_dispatch):
        from gpiozero import Button

        self._events: queue.Queue[Press] = queue.Queue()
        self._buttons = []
        self._pressed_at = None
        self._dropped = 0
        self._said = 0
        self._said_at = None
        self._closed = False
        self._revived = 0
        self._revive_failed = 0
        self._revive_at = None
        # Injectable so the revival policy can be tested without a real lgpio.
        self._dispatcher = dispatcher
        self._reviver = reviver

        for pin, press in ((up, Press.UP), (down, Press.DOWN)):
            button = Button(pin, pull_up=True, bounce_time=BOUNCE_SECONDS)
            button.when_pressed = self._guarded(
                lambda p=press: self._events.put(p), f"the {press.name} press")
            self._buttons.append(button)

        # OK distinguishes tap from hold by timing the press, and decides on
        # release. gpiozero's when_held runs on a separate HoldThread, so a
        # flag set there and checked in when_released could race and emit
        # both OK and BACK for one press. They mean opposite things while a
        # job is printing, so that could destroy a pad pair. when_pressed
        # and when_released share the pin callback thread: no lock needed.
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

        Every GPIO edge in the process is dispatched by one lgpio thread
        with no guard around the callback, and nothing restarts it. One
        exception out of a callback (a MemoryError on this 512 MiB board,
        say) would end it and disable every button until a power cycle,
        which throws away the pad in progress.

        So a failing callback costs one lost edge, reported through
        `_note_lost`; the press is not retried. A dispatch thread that is
        already dead is `_watch`'s job.
        """
        def guarded(_button=None):
            try:
                action()
            except BaseException as exc:        # noqa: BLE001
                # BaseException, not Exception: letting anything through
                # ends this thread, the only one delivering edges, not the
                # process. Consequence for tests: pytest.fail, skip and
                # assertion failures raised inside a wrapped callback are
                # swallowed here, so assert from the test's own thread.
                try:
                    self._note_lost(lost, exc)
                except BaseException:           # noqa: BLE001
                    # Last resort. Reporting allocates and writes, either
                    # of which can fail; losing the log entry beats losing
                    # every button.
                    pass
        return guarded

    def _note_lost(self, lost, exc) -> None:
        """
        Count one lost edge and, if it is due, say so.

        Rate-limited, because a faulty panel loses edges by the dozen and
        each report goes to the journal. The first loss gets a full report
        with traceback; after that, one line at most every REPORT_SECONDS
        carrying the running total and no traceback.
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
        # Stamped whether or not the line got out: `_report` may drop it,
        # and the total in the next line is right either way.
        self._said, self._said_at = self._dropped, time.monotonic()

    def _on_press(self, _button=None):
        # Cleared first, so that if the clock read fails a stale timestamp
        # cannot make the next release look like a hold and emit BACK where
        # the operator pressed OK.
        self._pressed_at = None
        self._pressed_at = time.monotonic()

    def _on_release(self, _button=None):
        started, self._pressed_at = self._pressed_at, None
        if started is None:
            # Released without a press we saw -- ignore rather than guess.
            return
        held = (time.monotonic() - started) >= HOLD_SECONDS
        self._events.put(Press.BACK if held else Press.OK)

    def _watch(self) -> None:
        """
        Look at the dispatch thread, and start it again if it has stopped.

        Keys on the thread's liveness, which is directly observable, so a
        healthy idle unit cannot trip it. Runs on the caller's thread
        inside `wait`, which every app state reaches, including the
        `timeout=0` polls of a printing job. Writes nothing when healthy.

        Only journals and restores delivery: no draw, no job cancel, no
        Button rebuild. It clears `_pressed_at` so a press whose release
        was lost cannot later arrive as BACK.

        Never raises: an exception here would end the process, and each
        systemd restart throws away the pad in progress. As in `_guarded`,
        assertions made inside a test-supplied `_dispatcher` or `_reviver`
        are swallowed.

        Assumes a single thread waits on the panel; two would both read a
        stale `_revive_at` and both revive.
        """
        try:
            if self._closed:
                return
            thread = self._dispatcher()
            if thread is None or thread.is_alive():
                return
            now = time.monotonic()
            if (self._revive_at is not None
                    and now - self._revive_at < REVIVE_SECONDS):
                return                          # tried recently; not again
            self._revive_at = now
            # Cleared before the replacement can deliver anything, so a
            # late release cannot complete a half-finished press.
            self._pressed_at = None
            try:
                discarded = self._reviver(thread)
            except BaseException as exc:        # noqa: BLE001
                self._revive_failed += 1
                self._note_revival(None, exc)
            else:
                self._revived += 1
                self._note_revival(discarded, None)
        except BaseException:                   # noqa: BLE001
            # The whole check, including its own reporting.
            # Everything above allocates, and the expected fault is the one
            # that breaks allocation. Losing the recovery beats losing the
            # event loop.
            pass

    def _note_revival(self, discarded, exc) -> None:
        """
        Say that the dispatcher died, and whether it is back.

        Not rate-limited: a failure is tried at most once every
        REVIVE_SECONDS and a success needs a dead thread. Only the first
        failure carries a traceback.

        The success line says the discarded bytes include what the reader
        had already buffered, not only presses made at a dead panel, and
        says when the drain stopped at BACKLOG_MAX_BYTES, since the
        remainder will then be replayed.
        """
        if exc is None:
            bound = ""
            if discarded >= BACKLOG_MAX_BYTES:
                bound = (f"otp: front panel: the drain stopped at its "
                         f"{BACKLOG_MAX_BYTES}-byte bound, so whatever is "
                         f"still in the pipe WILL be delivered by the "
                         f"replacement.\n")
            _report(
                f"otp: front panel: lgpio's edge dispatch thread was DEAD "
                f"-- every button in this process had stopped answering -- "
                f"and a replacement is now running on the same notification "
                f"handle.\n"
                f"otp: front panel: {discarded} byte(s) of undelivered "
                f"edges were discarded rather than replayed: presses made "
                f"at a panel that was not listening, and whatever the "
                f"reader had already taken in when it stopped.\n"
                f"{bound}"
                f"otp: front panel: this is revival {self._revived} since "
                f"this panel was built. Nothing else changed: no job was "
                f"cancelled and the buttons were not rebuilt.")
            return
        head = (f"otp: front panel: lgpio's edge dispatch thread is DEAD "
                f"and could not be restarted -- {type(exc).__name__}: "
                f"{exc}")
        if self._revive_failed == 1:
            _report(
                f"{head}\n"
                f"otp: front panel: no button on this unit is answering "
                f"until this succeeds; it is retried at most once every "
                f"{REVIVE_SECONDS:g}s and says so each time.\n"
                f"{traceback.format_exc().rstrip()}")
        else:
            _report(f"{head}; {self._revive_failed} attempt(s) have failed "
                    f"and no button on this unit is answering.")

    def wait(self, timeout: float | None = None) -> Press | None:
        while True:
            # Before the wait, not after, so the `timeout=0` polls a
            # printing job makes are checked too.
            self._watch()
            try:
                # Only an untimed wait is sliced, so the menu that blocks
                # here forever still notices a dead dispatcher. Finite
                # timeouts pass straight through: one get, one answer.
                return self._events.get(
                    timeout=WATCH_SECONDS if timeout is None else timeout)
            except queue.Empty:
                if timeout is not None:
                    return None

    def close(self) -> None:
        # First, so `_watch` does not start a revival for a panel that is
        # going away.
        self._closed = True
        for button in self._buttons:
            try:
                button.close()
            except Exception:
                pass

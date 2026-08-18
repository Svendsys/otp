"""Button back-ends for the print unit's front panel.

Three buttons: UP, DOWN, OK. A long press on OK is BACK, which is what makes
three buttons enough to navigate menus, edit values, and cancel a running
job without a fourth switch to wire up.
"""
from __future__ import annotations

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
#: After the first lost edge is reported in full, one line at most this
#: often. See GpioButtons._note_lost for why a lost edge must not be free
#: to write as often as it likes.
REPORT_SECONDS = 5.0
#: And no single report longer than this, in characters. Below PIPE_BUF
#: (4096), which is the size of write that a pipe reporting itself writable
#: promises to take without blocking -- see _report.
REPORT_MAX_CHARS = 3500
#: The longest a panel with nothing to deliver will sit inside wait()
#: before looking at the dispatch thread again. It is the worst case for
#: how long a dead dispatcher stays dead on an idle menu; the check itself
#: is one attribute read and one is_alive(), so the number is chosen for
#: how long an operator will keep pressing before deciding the unit is
#: broken, not for what the check costs. See GpioButtons._watch.
WATCH_SECONDS = 2.0
#: The least time between two attempts at a revival, whatever the first
#: one did. Starting a thread allocates, and the fault this is expected to
#: meet is MemoryError, so a failure means retrying is likely to fail too:
#: the bound is what keeps a panel that cannot be revived from spending the
#: unit's remaining memory finding that out, and keeps its report to one
#: line per interval. It applies to a SUCCESS as well, deliberately -- a
#: dispatcher dying twice inside half a minute is something raising over
#: and over, and a thread started per death would be the same runaway with
#: an extra thread on the end of it. The price is that a second death is
#: not answered for up to this long. See GpioButtons._watch.
REVIVE_SECONDS = 30.0
#: The most banked-up notification traffic _discard_backlog will read
#: before it stops trying to empty the pipe. A Linux pipe holds 64 KiB by
#: default (4096 sixteen-byte edge records) and lgpio's is left at that, so
#: this is sixteen times more than the kernel can be holding; it exists so
#: that a pipe somebody is still writing to cannot turn the drain into a
#: loop that never ends on the thread the panel is waiting on.
BACKLOG_MAX_BYTES = 1 << 20
#: Read into by _discard_backlog, once, at import. Preallocated because the
#: moment the drain runs is the moment allocating is least likely to work:
#: the dispatch thread is dead, and on this board the reason to expect that
#: is a MemoryError out of a 1000-page pad (tests/test_memory_budget.py).
_BACKLOG_BUFFER = memoryview(bytearray(4096))


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
    if not message.isascii():
        # PIPE_BUF counts BYTES and the cap below counts characters, which
        # are the same number only for ASCII -- and an exception message
        # can carry accents or a U+FFFD from a decode with
        # errors="replace" (see ui.wrap, which has the same problem in the
        # other direction). Escaping is cheaper than encoding twice to
        # measure, and `isascii` is a flag lookup, so the common path pays
        # nothing.
        message = message.encode("ascii", "backslashreplace").decode("ascii")
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


def dispatch_thread():
    """
    lgpio's edge dispatch thread, or None if this process has no such thing.

    `sys.modules.get` and not `import lgpio`, for two separate reasons.

    The first is that importing it is not free and not safe here. lgpio's
    import runs `_notify_thread = _callback_thread()` (lgpio 0.2.2.0,
    lgpio.py:562), whose constructor opens a notification handle and
    CREATES A FIFO in the working directory (lgpio.py:503-504). Measured,
    from a directory on a read-only mount:

        xCreatePipe: Can't set permissions for .../.lgd-nfy0
        FileNotFoundError: [Errno 2] ...: '.lgd-nfy-3'

    -- lgpio does not check the negative handle it got back and formats it
    into the name it then fails to open. That is not something to provoke
    from a recovery path, and this one may run while a pad is printing.

    The second is that None is the only honest answer about a thread that
    does not exist. gpiozero picks the first pin factory that imports --
    lgpio, then rpigpio, then pigpio, then native (gpiozero 2.0.1,
    devices.py:281-302) -- so a unit whose panel is running on any of the
    other three has no lgpio dispatch thread, this returns None, and
    `_watch` does nothing at all.

    WHICH IS A LIMIT, AND IT IS NOT A SMALL ONE. Said plainly because
    everything else in this file reads as though the fault were covered:
    the same fault is UNSUPERVISED on the other three. `NativeFactory`
    dispatches on a thread of its own and calls
    `pin._call_when_changed(ticks, state)` with no guard around it either
    (gpiozero 2.0.1, native.py:353-370), so one exception out of a callback
    there ends `NativeDispatchThread` and every button in the process goes
    deaf in exactly the way #37 and this supervisor exist to answer -- and
    nothing here can even see that thread.

    And which factory a booted unit lands on is not established anywhere.
    `import lgpio` does not merely fail to be useful on a read-only
    working directory, it RAISES (above); gpiozero catches `Exception`
    rather than `ImportError` around each attempt (devices.py:299), so
    that raise is indistinguishable from lgpio not being installed and the
    chain walks on; `otp-unit.service` sets `WorkingDirectory=/opt/otp-unit`
    under `ProtectSystem=strict`; and `device/packages.txt` has neither
    python3-rpi.gpio nor python3-pigpio, so the next stop would be native.
    Answering that needs a Pi and is tracked as issue #43. Until it is
    answered, read this file as covering the lgpio case and no other.
    """
    lgpio = sys.modules.get("lgpio")
    return None if lgpio is None else getattr(lgpio, "_notify_thread", None)


def _discard_backlog(source) -> int:
    """
    Throw away every edge record waiting on `source`, and say how many bytes.

    This is the half of a revival that is about the operator rather than
    about the library, and leaving it out is the way to make recovery
    HARMFUL. While the dispatch thread is dead nothing drains lgpio's
    notification pipe, and nothing stops the writer either -- the read end
    stays open, because a thread killed inside `cb.func` never reaches the
    `self._file.close()` at the end of `run()` (lgpio.py:559). So the
    presses of an operator jabbing at a panel that does nothing pile up in
    the pipe. Measured, with forty press/release pairs written to a dead
    dispatcher's pipe and the thread then restarted over it: all eighty
    edges were delivered, back to back, in the moment of recovery.

    On this device that is not merely noise. Those presses were aimed at a
    panel that was not listening; replaying them drives the menu at machine
    speed, and OK and BACK mean opposite things while a job is printing.
    ui._drain already states the same rule for presses banked while the
    loop was blocked -- "replaying them is always wrong; on this device it
    can mean abandoning a job" -- and an edge banked by a dead dispatcher
    is the same press with a worse excuse.

    Through the FILE OBJECT and not the descriptor, which is the part that
    is easy to get wrong: `run()` reads with `self._file.read(16)` on a
    BufferedReader, and a buffered read of 16 bytes pulls up to 8192 out of
    the pipe. Measured, killing the thread with a burst written in one
    call: the kernel held 0 bytes (FIONREAD) and the BufferedReader held
    960 -- sixty edges that a drain of the descriptor would have missed
    entirely and then replayed. `readinto1` takes what is buffered first
    and only then reads, so it empties both.

    Non-blocking for the duration, because there is no way to ask how much
    is there: without it a read that reached an empty pipe would park the
    caller inside read(2), and the caller here is the thread the app waits
    for presses on.

    WHAT THAT DOES NOT BUY, because this used to claim it did. The flag is
    on the DESCRIPTOR. The drain still has to take the `BufferedReader`'s
    own lock, and a reader already parked inside a blocking read(2) is
    holding that lock and is not woken by the flag changing under it.
    Measured, with a reader thread sitting in `read(16)` on an empty pipe
    and `_discard_backlog` called on the same file object from another
    thread: the drain did not return within five seconds, and because its
    `finally` had not run either, the descriptor was left NON-BLOCKING --
    which then kills lgpio's own loop, whose `buf += self._file.read(16)`
    (lgpio.py:542) meets a `None` and raises `can't concat NoneType to
    bytes`. So the failure mode of calling this against a live reader is
    both callers wedged and the panel deaf afterwards.

    WHAT MAKES IT SAFE TODAY IS THE CALLER, not the flag. `_watch` runs
    this on the app's single event-loop thread and only after `is_alive()`
    has said the dispatch thread stopped -- a dead thread holds no lock --
    and the process has one panel; every `wait()` there is -- `ui.App.run`,
    its `poll_seconds` screens, `ui._drain`, the drain inside
    `should_cancel`, `hmi.Interface.prove` and `unattended`'s `pressed` --
    is reached from that thread, and outside this file the application
    starts no thread at all -- grepping otpunit/ for `threading` finds
    this module and nothing else. It is
    NOT made safe against a concurrent reader, deliberately: the only lock
    that would help is the `BufferedReader`'s, and taking it means waiting
    for whoever holds it -- which in the case that matters is the parked
    reader, so it turns a park into a park. A live dispatch thread is not
    something this may interrupt in any case; there would be nothing to
    repair. If a second thread ever waits on a panel, this needs a design
    and not a lock.
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
                # None is what a non-blocking BufferedReader answers when
                # the raw read said EAGAIN; 0 is end of file. Both mean the
                # same thing here, which is stop.
                break
            dropped += got
    finally:
        # Whatever happened above. A descriptor left non-blocking would
        # turn `run()`'s next `read(16)` into a busy loop that delivers
        # nothing -- a panel that looks alive and is not, which is the one
        # outcome this whole file exists to refuse.
        os.set_blocking(fd, True)
    return dropped


def revive_dispatch(dead) -> int:
    """
    Put a running thread back on lgpio's notification handle. Bytes dropped.

    WHAT WAS MEASURED, because the obvious repair does not work. The
    tempting reading of "the panel is dead, so rebuild the panel" is that
    fresh `Button`s would arm fresh callbacks and start working again. They
    do not. `lgpio.callback()` appends to `_notify_thread.callbacks`
    (lgpio.py:578) and `gpio_claim_alert` routes the kernel's edges to
    `_notify_thread._notify` (lgpio.py:1293) -- both read the module-level
    singleton, and neither cares that nothing is reading it any more.
    Measured, in this container, against the real library: with the
    singleton killed by an unguarded raise out of its own dispatch loop, a
    callback registered afterwards exactly as `lgpio.callback` registers
    one received nothing at all. A supervisor that rebuilt the panel and
    reported a recovery would have been reporting one that did not happen.

    So what is restored is the THREAD, over the handle and the pipe it
    already had. Two consequences, both of them the reason this shape was
    chosen over constructing a fresh `_callback_thread()`:

      * nothing has to be re-claimed. No line is freed and re-taken, no
        `Button` is closed and rebuilt, and a panel mid-press keeps the
        objects it had. Measured, against lgpio's own dispatch loop: the
        registration that went quiet delivered again, on the same pipe,
        with nothing else touched.

        What that measurement does NOT cover, and cannot in a container
        with no gpiochip in it (no gpio-sim, no /dev/gpiochip*), is the
        far side: that lgpio's C alert thread goes on writing into this
        notification handle while nothing is reading it, and that a claim
        made before the death is still routed here afterwards. Both are
        read off the source rather than run -- see gpio_claim_alert's
        `notify_handle = _notify_thread._notify` (lgpio.py:1293), which is
        resolved once, at claim time. `pytest -m hardware` on a kernel
        with gpio-sim is where that becomes a measurement.
      * nothing is created on disk. A fresh `_callback_thread()` calls
        `_notify_open()`, which makes another `.lgd-nfy*` FIFO in the
        directory lgpio recorded at import -- measured: the C side keeps
        that directory and the Python side opens the FIFO relative to the
        CURRENT one, so a process that has chdir'd since import gets
        `FileNotFoundError: '.lgd-nfy1'` and leaks the handle it opened.
        It also leaves the old handle, its FIFO and its descriptor behind
        for the life of the process, once per revival.

    The callbacks list is passed on by IDENTITY, not copied. A copy would
    take the registrations that exist now and then quietly diverge:
    `lgpio.callback()` appends to whatever `_notify_thread` is at the time
    it is called, so a panel built after a copy would land its callbacks in
    a list this thread had stopped reading -- the same silent deafness in a
    new place.

    The module global is repointed BEFORE the thread starts, so that a
    `gpio_claim_alert` racing with this cannot be routed to the handle of a
    thread that is about to stop existing. They are the same handle today;
    the order costs nothing and does not depend on that.
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
            # timeout=0 means "is anything pending?" -- answering QUIT there
            # would make a non-blocking drain loop see an endless supply of
            # presses. Only the blocking form ends a scripted run.
            return None if timeout == 0 else Press.QUIT
        return self._script.pop(0)


def _read_key_byte(fd: int) -> str:
    """
    One byte off the terminal, taking nothing else with it.

    os.read and NOT sys.stdin.read(1), which is what this used to be, and
    the difference is the whole of why the arrow keys did not work on a
    real terminal.

    sys.stdin is a TextIOWrapper over a BufferedReader. `read(1)` asks for
    one CHARACTER, and to produce it the buffered layer issues one raw
    read of up to 8192 bytes and keeps whatever else came back. An arrow
    key is three bytes -- ESC [ A -- delivered together, so the first
    read(1) returned "\\x1b" and swallowed "[A" into Python's buffer,
    where select() cannot see it. Measured on a pty, with the three bytes
    written before the read:

        select before first read: True
        first read(1): '\\x1b'
        bytes still in the OS queue (select): False

    So the loop below timed out twice at 0.05s, `_map("\\x1b")` returned
    None, and the two orphaned bytes were handed out as the next two
    presses -- "[" and "A", both unmapped, both None. Every arrow press
    was silently lost, on the one input path where they are the keys the
    unit's own footer advertises: ConsoleDisplay draws "arrows move
    ENTER select SHIFT+K back" on a real unit. Measured over a pty
    against the shipped mapping, one key per fresh process:

        b'k'      -> Press.OK       b'u'       -> Press.UP
        b'\\r'     -> Press.OK       b'd'       -> Press.DOWN
        b'K'      -> Press.BACK     b'\\x1b[A'  -> None
                                    b'\\x1b[B'  -> None

    Nothing noticed because the only two ways this reader had ever been
    exercised both avoid the branch: the fast suite drives `_map` and the
    line-mode path directly, and `--sim` in CI pipes its stdin, which
    takes the readline branch above. It needed a real terminal, which is
    what tests/test_hardware.py's TestTheKeyboardOnARealTerminal is.

    Reading the descriptor directly leaves the rest of the sequence in
    the kernel's queue, where the select() below is looking for it.
    latin-1 because these are control bytes and key codes rather than
    text: it maps every byte to a character and cannot raise, and a
    multi-byte UTF-8 key decodes to something `_map` does not know --
    which is None, exactly as it was before.
    """
    return os.read(fd, 1).decode("latin-1")


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
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return None
            ch = _read_key_byte(fd)
            if ch == "\x1b":
                # An escape byte may be a lone Esc or the start of an arrow
                # sequence, and in raw mode nothing distinguishes them but
                # time. A bare read(2) here blocked FOREVER on a single Esc
                # -- the most natural key to press at a prompt -- which
                # recreated the exact hang that Interface.prove() exists to
                # prevent, inside prove() itself.
                for _ in range(2):
                    ready, _, _ = select.select([fd], [], [], 0.05)
                    if not ready:
                        break
                    ch += _read_key_byte(fd)
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
    # would be the log line, not the thread. The fallback is so that such a
    # panel still reports, not so that it survives.
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

    # The supervisor's state, with the same fallback for the same reason.
    # `_dispatcher` and `_reviver` are staticmethod() so that reading them
    # off the CLASS yields the plain function rather than something bound
    # to the panel: neither takes a `self`, and both are replaced per panel
    # in __init__ so a test can drive the policy without a dead lgpio.
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

        Deliberately NOT folded into `dropped`. A dropped edge is one press
        the guard ate with the panel still working; a revival is every
        press in the process having been going nowhere until it happened.
        They have different causes, different costs and different fixes,
        and the existing tests that assert an exact `dropped` would have
        had to be loosened to let a second meaning share the counter.
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
        # Both injectable, and both injected as whole callables rather than
        # as a flag, so that the policy below can be driven -- healthy,
        # dead, revived, refusing to revive -- without a real lgpio and
        # without a real dead thread. There is exactly one place in this
        # suite that can make a genuine one, and a policy only testable
        # there is a policy that is tested once.
        self._dispatcher = dispatcher
        self._reviver = reviver

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
        a panel that is still there for the next press. It does NOT retry
        the lost press and it does not escalate: an edge the guard ate is
        gone, and the operator presses again.

        What it also cannot do is help with a dispatch thread that is
        ALREADY dead -- something else in the process raising, or this
        guard's own last-resort swallow failing -- because by then there is
        nothing left to catch anything in. That is `_watch`'s job, and it
        keys on the thread being dead rather than on anything this counts:
        losses here mean the guard is working, not that recovery is due.
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

    def _watch(self) -> None:
        """
        Look at the dispatch thread, and start it again if it has stopped.

        WHY THERE IS NO TIMER. The one signal worth building on is that the
        dispatch thread's liveness is DIRECTLY OBSERVABLE, so there is no
        threshold to pick and no way for a healthy unit to trip it. The
        alternative -- "no press for N seconds" -- has no defensible N on a
        device that is supposed to sit untouched for hours between pads,
        and would fire on a panel that is working perfectly.

        WHY IT RUNS HERE, on the caller's thread, inside `wait`. The thread
        that wants the panel is the thread already asking for a press, and
        while it is asking it has nothing else to do; a check run there
        delays nothing and needs no thread of its own to get wrong around
        `close()`. It reaches every state the app has: the menu blocks in
        `wait()` with no timeout, the entropy and printer screens poll it
        every `poll_seconds`, and a job in progress asks with `timeout=0`
        on every progress step through `JobScreen.should_cancel` -- so a
        dispatcher that dies mid-pad is noticed during the pad, not after
        it. The cost when nothing is wrong is one dict lookup and one
        `is_alive()`, and NOTHING is written: a supervisor that logs on a
        healthy unit is one people learn to ignore.

        WHAT IT IS NOT ALLOWED TO DO. Journal and nothing else. No draw --
        the panel is a 128x64 OLED with a menu on it and no room for a
        message, which is `_report`'s whole argument -- and no state change
        beyond putting delivery back: it does not cancel a job, it does not
        touch the display, it does not close or rebuild the `Button`s. It
        does clear `_pressed_at`, and that is restoration rather than
        policy: a press seen with its release lost to the death would
        otherwise be measured against a timestamp minutes old and arrive as
        BACK where the operator pressed OK, which while a job is printing
        are opposite things.

        AND IT MAY NOT RAISE -- for a reason that is not the one this
        docstring gave. It said an exception here ends `App.run()`,
        `main()` returns 0, and systemd's `Restart=on-failure` reads that
        as success and leaves the unit off. THAT WAS WRONG.
        `__main__.main` does not catch arbitrary exceptions at all; they
        propagate out of it. Measured, with `ui.App.run` patched to raise
        MemoryError and the most forgiving path taken -- `--sim`, the only
        one that ends in `return 0`:

            File "otpunit/__main__.py", line 190, in main
                app.run()
            MemoryError: no memory to draw the menu
            EXIT=1

        So `Restart=on-failure` DOES restart, every `RestartSec=15`, for
        as long as the fault lasts -- and each restart throws away the pad
        in progress and its key material. The real cost of raising here is
        therefore worse than the one claimed: not an appliance sitting
        dark, but one power-cycling itself every fifteen seconds while an
        operator watches, losing a pad each time and printing none. The
        decision to swallow stands on the better reason.

        (The `KeyboardButtons.allow_quit` failure the old text pointed at
        is fixed history rather than a live hazard: `__main__.py` returns
        1 when the menu ends without a shutdown request, and says in a
        comment that returning 0 there is what used to keep the unit off.)

        `_revive_at` is read and then written with nothing between the two
        holding anything off. That is safe for one reason and it is worth
        naming: there is only ever one thread in here, the same one
        `_discard_backlog` depends on being alone. Two threads waiting on
        one panel would both read a stale `_revive_at` and both revive.

        A WARNING FOR WHOEVER WRITES THE NEXT TEST, the same one `_guarded`
        carries and with more surface here: `pytest.fail`, `pytest.skip`
        and pytest's assertion-rewriting machinery all raise BaseException
        subclasses, and `_dispatcher` and `_reviver` are exactly the
        callables a test supplies. Measured: a reviver containing a false
        `assert` and a probe calling `pytest.fail` gave `2 passed`. Assert
        from the test's own thread, on what the panel recorded -- `revived`,
        the stand-in reviver's `called`, what `_report` was handed -- and
        never from inside a callable this file will call.

        So the same `except BaseException` the callback guard uses, with
        the same last-resort swallow around the reporting.
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
            # Cleared BEFORE the replacement can deliver anything, so that
            # a half-finished press cannot be completed by a release that
            # arrives after it. See the docstring.
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
            # The whole check, including its own reporting. Everything
            # above allocates something -- a thread, an f-string, a
            # traceback -- and the fault this is here to meet is the one
            # that breaks allocation next. Losing the recovery is bad;
            # losing the event loop that draws the panel is worse.
            pass

    def _note_revival(self, discarded, exc) -> None:
        """
        Say that the dispatcher died, and whether it is back.

        Both lines are worth the journal at full length, and neither needs
        the bound `_note_lost` carries, because the thing being reported
        cannot repeat quickly: a success is only reachable from a dead
        thread, and a failure is tried at most once every REVIVE_SECONDS.
        What the first failure gets that later ones do not is its
        traceback, for the reason a first loss gets one -- it is the line
        that says where the fault is, and after it the type and the message
        are the part that changes.

        Sized so `_report`'s cap never has to cut them: the longest of
        these is well under REPORT_MAX_CHARS before an exception's own
        `str()` is added, and that is what the cap is for.

        WHAT THE SUCCESS LINE MAY CLAIM ABOUT THE BYTES IT THREW AWAY, and
        it used to claim more. It said they were "presses made at a panel
        that was not listening", and most of them are -- but not all, and
        the measurement is in this branch's own notes. lgpio reads with
        `self._file.read(16)` on a BufferedReader, which pulls up to 8192
        bytes out of the pipe, so the edges written in the SAME `write()`
        as the one that killed the thread are already inside Python before
        it dies: measured, the kernel held 0 bytes and the reader held 960.
        Those were queued while the panel was still listening, and they go
        with the rest. Discarding them is still right -- what remains of
        such a press is half of it, and a release whose press was eaten
        arrives as nothing or as BACK -- but the line may not pretend the
        operator made every one of them at a dead panel.

        And the bound is said out loud when it is reached. The drain stops
        at BACKLOG_MAX_BYTES; past that the pipe keeps what it has and the
        replacement thread delivers it, which is precisely the replay the
        drain exists to prevent. A report silent about that would describe
        a backlog as discarded when a megabyte of it was and the remainder
        is about to arrive at machine speed.
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
            # Before the wait rather than after it: the reason to ask is
            # that the queue is about to be waited on, and a panel that
            # checked only on the way out would not check at all on the
            # `timeout=0` polls a printing job makes.
            self._watch()
            try:
                # A wait with no deadline is sliced, and only a wait with
                # no deadline. The main menu blocks here forever
                # (ui.App.run), so an unsliced get() would let a
                # dispatcher die at 09:00 and be noticed at the next
                # press, which is to say never. Finite timeouts are passed
                # straight through and behave exactly as they always have
                # -- one get, one answer -- which also keeps this loop
                # away from the fake clocks the timing tests install:
                # nothing here reads a clock to decide when to stop.
                return self._events.get(
                    timeout=WATCH_SECONDS if timeout is None else timeout)
            except queue.Empty:
                if timeout is not None:
                    return None

    def close(self) -> None:
        # First, and before anything is closed: `_watch` reads this, and a
        # revival started while the panel is going away would be work done
        # on behalf of nobody. It is not a lock -- a check already past
        # this point still finishes, harmlessly, because reviving lgpio's
        # thread touches no `Button` and no display.
        self._closed = True
        for button in self._buttons:
            try:
                button.close()
            except Exception:
                pass

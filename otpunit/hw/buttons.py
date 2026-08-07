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

    def __init__(self, up=PIN_UP, down=PIN_DOWN, ok=PIN_OK):
        from gpiozero import Button

        self._events: queue.Queue[Press] = queue.Queue()
        self._buttons = []
        self._pressed_at = None

        for pin, press in ((up, Press.UP), (down, Press.DOWN)):
            button = Button(pin, pull_up=True, bounce_time=BOUNCE_SECONDS)
            button.when_pressed = lambda _b=None, p=press: self._events.put(p)
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
        okay.when_pressed = self._on_press
        okay.when_released = self._on_release
        self._buttons.append(okay)

    def _on_press(self, _button=None):
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

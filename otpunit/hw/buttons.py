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
BOUNCE_SECONDS = 0.05


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
    """Maps terminal keys to presses, for --sim."""

    KEYS = {
        "u": Press.UP, "k": Press.OK, "d": Press.DOWN,
        "K": Press.BACK, "q": Press.QUIT,
        "\x1b[A": Press.UP, "\x1b[B": Press.DOWN,
        "\r": Press.OK, "\n": Press.OK,
    }

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
                return Press.QUIT
            return self.KEYS.get(line.strip()[:1] or "\n")

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch += sys.stdin.read(2)
            if ch == "\x03":
                return Press.QUIT
            return self.KEYS.get(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class GpioButtons(Buttons):
    """The real front panel: three momentary switches to ground."""

    def __init__(self, up=PIN_UP, down=PIN_DOWN, ok=PIN_OK):
        from gpiozero import Button

        self._events: queue.Queue[Press] = queue.Queue()
        self._buttons = []
        self._held = False

        for pin, press in ((up, Press.UP), (down, Press.DOWN)):
            button = Button(pin, pull_up=True, bounce_time=BOUNCE_SECONDS)
            button.when_pressed = lambda _b=None, p=press: self._events.put(p)
            self._buttons.append(button)

        # OK distinguishes tap from hold: the hold fires BACK on its own, and
        # the release then has to know not to also emit an OK.
        okay = Button(pin_factory=None, pin=ok, pull_up=True,
                      bounce_time=BOUNCE_SECONDS, hold_time=HOLD_SECONDS)
        okay.when_held = self._on_hold
        okay.when_released = self._on_release
        self._buttons.append(okay)

    def _on_hold(self, _button=None):
        self._held = True
        self._events.put(Press.BACK)

    def _on_release(self, _button=None):
        if self._held:
            self._held = False
            return
        self._events.put(Press.OK)

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

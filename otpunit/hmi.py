"""Choosing an interface out of whatever hardware actually turned up.

The unit wants a 128x64 OLED and three buttons. It may not get them. But
a monitor and a USB keyboard are ordinary things that turn up in ordinary
houses, and either one on its own is worth having, so display and input
are chosen independently:

    display   SSD1306 on I2C  >  a connected HDMI screen  >  nothing
    input     GPIO buttons    >  a USB keyboard           >  nothing

Any pairing works. OLED with a keyboard, a monitor with three buttons on
a breadboard, or the intended panel -- the full menu runs on all of them,
because the console renderer and the keyboard reader are the same ones
`--sim` has always used.

With no display OR no input there is no menu, and the unit falls back to
printing unattended (see unattended.py). That is the case this device
exists for, so it is a first-class path rather than an error.

Detection is deliberately conservative. Binding the service to tty1 means
stdin is always a terminal whether or not anything is plugged into the
HDMI socket, so a real connector status is checked instead of trusting
isatty(); and a keyboard is looked for as an actual input device rather
than assumed from the presence of a console.
"""
from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass

from otpunit.hw.buttons import GpioButtons, KeyboardButtons
from otpunit.hw.display import ConsoleDisplay, Ssd1306Display


@dataclass
class Interface:
    display: object | None = None
    buttons: object | None = None
    display_kind: str = "none"
    input_kind: str = "none"

    @property
    def interactive(self) -> bool:
        """A menu needs something to draw on AND something to press."""
        return self.display is not None and self.buttons is not None

    def prove(self, seconds: float = 20.0, sleep=None, clock=None) -> bool:
        """
        Wait for one press, to confirm somebody can actually drive this.

        Opening GpioButtons proves NOTHING about whether buttons exist.
        gpiozero only reserves and configures a pin; there is no presence
        detection on any of its four pin factories, so wherever gpiozero
        finds a factory at all the constructor succeeds. This used to say
        "on any Pi with lgpio installed -- every unit this targets", which
        named a factory nothing here establishes: gpiozero catches
        `Exception` and not `ImportError` when a factory fails to import
        (gpiozero 2.0.1, devices.py:299), so an lgpio whose import RAISES
        -- which it does when the working directory is read-only, and this
        unit runs with `WorkingDirectory=/opt/otp-unit` under
        `ProtectSystem=strict` -- is indistinguishable from an lgpio that
        was never installed, and the fallback walks on to native. Which
        factory a booted unit ends up on is issue #43. Nothing below
        depends on the answer, which is the point: none of them can tell
        you whether a button is wired to the pin. That made the keyboard
        branch unreachable, and worse: attaching a monitor to a working
        headless unit promoted `display` to non-None, `interactive` to
        True, and sent it into a menu whose first `buttons.wait()` blocks
        on an empty queue forever. No pad, no log line, no timeout.

        A keyboard being present says nothing either -- it says a keyboard
        is plugged in, not that anyone is at it.

        So neither is trusted. One press within the window and the panel
        is proven; silence means print instead. The cost is that an
        operator must press something at boot, which also tells them the
        panel works.
        """
        import time

        sleep = sleep or time.sleep
        clock = clock or time.monotonic
        if not self.interactive:
            return False
        deadline = clock() + seconds
        while clock() < deadline:
            try:
                if self.buttons.wait(timeout=0.5) is not None:
                    return True
            except Exception:                    # noqa: BLE001
                return False
            sleep(0)
        return False

    def describe(self) -> str:
        return f"display: {self.display_kind}, input: {self.input_kind}"

    def close(self):
        for part in (self.display, self.buttons):
            try:
                if part is not None:
                    part.close()
            except Exception:                    # noqa: BLE001
                pass


def _read(path):
    with open(path, "r") as handle:
        return handle.read()


def screen_connected() -> bool:
    """
    Whether a monitor is actually plugged in.

    Read from DRM connector status rather than inferred from isatty(),
    because the service binds to tty1 either way -- so stdin is a terminal
    on a unit with nothing attached to it at all.
    """
    try:
        connectors = glob.glob("/sys/class/drm/card*/status")
    except Exception:                            # noqa: BLE001
        return False
    for status in connectors:
        try:
            if _read(status).strip() == "connected":
                return True
        except OSError:
            continue
    return False


def keyboard_connected() -> bool:
    """
    Whether something that generates key events is attached.

    The udev symlinks are the reliable signal; /proc is the fallback for a
    system where udev has not populated them.
    """
    try:
        for pattern in ("/dev/input/by-id/*-event-kbd",
                        "/dev/input/by-path/*-event-kbd"):
            if glob.glob(pattern):
                return True
    except Exception:                            # noqa: BLE001
        pass
    try:
        blocks = _read("/proc/bus/input/devices").split("\n\n")
    except Exception:                            # noqa: BLE001
        return False
    for block in blocks:
        # EV=120013 and friends: bit 1 set means EV_KEY. Rather than
        # decode the mask, look for a handler udev already named.
        if "Handlers=" in block and "kbd" in block:
            return True
    return False


def console_usable() -> bool:
    """A terminal we can actually put into raw mode and read keys from."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:                            # noqa: BLE001
        return False


def open_display(rotate: int = 0, log=None):
    """The best display available, and what it is."""
    try:
        return Ssd1306Display(rotate=rotate), "SSD1306 OLED"
    except Exception as exc:                     # noqa: BLE001
        if log:
            log(f"no OLED ({exc})")

    if screen_connected() and console_usable():
        return ConsoleDisplay(stream=sys.stdout, hint=False), "HDMI console"
    return None, "none"


def open_buttons(log=None):
    """The best input available, and what it is."""
    try:
        return GpioButtons(), "GPIO buttons"
    except Exception as exc:                     # noqa: BLE001
        if log:
            log(f"no GPIO buttons ({exc})")

    if keyboard_connected() and console_usable():
        return KeyboardButtons(allow_quit=False), "USB keyboard"
    return None, "none"


def detect(rotate: int = 0, log=None) -> Interface:
    """Pick a display and an input independently, best first."""
    display, display_kind = open_display(rotate, log)
    buttons, input_kind = open_buttons(log)
    return Interface(display=display, buttons=buttons,
                     display_kind=display_kind, input_kind=input_kind)

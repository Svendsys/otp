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

from otpunit.hw.buttons import AnyButtons, GpioButtons, KeyboardButtons
from otpunit.hw.display import ConsoleDisplay, Ssd1306Display


OLED_ADDRESSES = (0x3C, 0x3D)


@dataclass
class Interface:
    display: object | None = None
    buttons: object | None = None
    display_kind: str = "none"
    input_kind: str = "none"

    @property
    def interactive(self) -> bool:
        """A menu needs something to draw on and something to press."""
        return self.display is not None and self.buttons is not None

    def prove(self, seconds: float = 20.0, sleep=None, clock=None) -> bool:
        """
        Wait for one press, to confirm somebody can actually drive this.

        Opening GpioButtons proves nothing about whether buttons exist:
        gpiozero only reserves a pin and has no presence detection. A
        keyboard being plugged in does not mean anyone is at it either.
        Trusting either would send a headless unit with a monitor attached
        into a menu that blocks forever instead of printing.

        So one press within the window proves the panel, and silence means
        print instead. The cost is that the operator must press something
        at boot.
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
    # 0x3C is what almost every SSD1306 module ships as; 0x3D is the other
    # position of the address jumper.
    failures = []
    for address in OLED_ADDRESSES:
        try:
            return (Ssd1306Display(address=address, rotate=rotate),
                    "SSD1306 OLED")
        except Exception as exc:                 # noqa: BLE001
            failures.append(f"0x{address:02X}: {exc}")
    if log:
        log(f"no OLED ({'; '.join(failures)})")

    if screen_connected() and console_usable():
        return ConsoleDisplay(stream=sys.stdout, hint=False), "HDMI console"
    return None, "none"


def open_buttons(log=None):
    """The best input available, and what it is."""
    # Both, when both are there. Opening the GPIO pins succeeds on any Pi
    # whether or not a switch is wired to them, so preferring them outright
    # would mean a keyboard is never listened to.
    sources, kinds = [], []
    try:
        sources.append(GpioButtons())
        kinds.append("GPIO buttons")
    except Exception as exc:                     # noqa: BLE001
        if log:
            log(f"no GPIO buttons ({exc})")

    if keyboard_connected() and console_usable():
        sources.append(KeyboardButtons(allow_quit=False))
        kinds.append("USB keyboard")
    if not sources:
        return None, "none"
    if len(sources) == 1:
        return sources[0], kinds[0]
    return AnyButtons(sources), " and ".join(kinds)


def detect(rotate: int = 0, log=None) -> Interface:
    """Pick a display and an input independently, best first."""
    display, display_kind = open_display(rotate, log)
    buttons, input_kind = open_buttons(log)
    return Interface(display=display, buttons=buttons,
                     display_kind=display_kind, input_kind=input_kind)

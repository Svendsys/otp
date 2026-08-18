"""A real USB-keyboard-shaped input device, wired to a real terminal.

The unit's second supported input is "a USB keyboard", and what
`KeyboardButtons` actually consumes is bytes on a terminal. Between a key
and those bytes a real unit has: a HID device, the kernel input subsystem,
the virtual-terminal keyboard driver applying a keymap, the tty line
discipline, and finally the read. This module reproduces every part of
that a container can have, and says plainly which part it cannot.

## What is real here

  * The device. `uinput` creates an actual input device in the kernel --
    the same mechanism harness/kernel-sim.sh uses, and the reason
    `hmi.keyboard_connected()` finds a `kbd` handler in
    /proc/bus/input/devices without anything being monkeypatched.
  * The events. A press is written to the uinput descriptor and then READ
    BACK through evdev from the device node the kernel created for it. It
    is a full round trip through the input subsystem: nothing is delivered
    that the kernel did not deliver, and `press()` refuses to return until
    the event has come back, so a test cannot pass on an event that was
    never generated.
  * The terminal. A pty, with a real line discipline, real termios state
    and a real input queue -- see tests/test_hardware.py's RealTerminal.
    The bytes go in at the master exactly as a terminal emulator or a
    console driver puts them there, and everything from that point on is
    the shipped reader.

## What is a fixture, and why it has to be

KEYMAP. On a real unit the kernel's VT keyboard driver turns keycode 103
into the three bytes ESC [ A, using a loaded keymap. There is no virtual
terminal in a container and none on a GitHub runner, so there is nothing
to do that translation, and this table does it instead.

That boundary is where it is deliberately. Everything the unit's own code
touches is on the real side of it: the presence probe reads a real device,
and the reader reads real bytes off a real tty. What is faked is one lookup
that belongs to the kernel and that no part of otpunit performs, replaces
or depends on the details of.

The table is the shipped mapping's own view of the keyboard -- see
KeyboardButtons.KEYS -- which is what a stock keymap produces for these
keys. Shift is tracked rather than assumed, because BACK is SHIFT+K and a
bridge that emitted "K" for an unshifted keypress would be testing a
keyboard nobody has.
"""
from __future__ import annotations

import os
import select
import threading
import time


def available() -> str:
    """Why a virtual keyboard cannot be made here, or "" if it can."""
    if os.geteuid() != 0:
        return "creating a uinput device needs root"
    try:
        import evdev                             # noqa: F401
    except ImportError:
        return "python3-evdev is not installed"
    if not os.path.exists("/dev/uinput"):
        return "there is no /dev/uinput: the kernel has no uinput module"
    return ""


def _keymap():
    """The keycode -> terminal bytes table, built once evdev is importable."""
    from evdev import ecodes

    plain = {
        ecodes.KEY_UP: b"\x1b[A",
        ecodes.KEY_DOWN: b"\x1b[B",
        ecodes.KEY_ENTER: b"\r",
        ecodes.KEY_K: b"k",
        ecodes.KEY_U: b"u",
        ecodes.KEY_D: b"d",
        ecodes.KEY_Q: b"q",
    }
    shifted = {ecodes.KEY_K: b"K"}               # SHIFT+K is BACK
    return plain, shifted


class UinputKeyboard:
    """
    A keyboard the kernel believes in, typing on a terminal we can read.

    The device stays open for the life of the object: a uinput node
    disappears with the descriptor that made it, and
    `hmi.keyboard_connected()` has to be able to find it during the
    probe.
    """

    NAME = "otp-console-keyboard"

    def __init__(self, terminal):
        from evdev import UInput, ecodes

        self._terminal = terminal
        self._plain, self._shifted = _keymap()
        capabilities = {ecodes.EV_KEY: sorted(self._plain)
                        + [ecodes.KEY_LEFTSHIFT]}
        self._uinput = UInput(capabilities, name=self.NAME, version=1)
        device = getattr(self._uinput, "device", None)
        if device is None:
            self._uinput.close()
            raise RuntimeError(
                "uinput created a device with no event node to read back "
                "from, so no press could be proved to have happened")
        self.device = device
        #: Keys whose events came back out of the kernel and were typed on
        #: the terminal. The only evidence that a press really happened.
        self.delivered = 0
        self.typed = bytearray()
        self._shift = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._bridge, daemon=True)
        self._thread.start()

    @property
    def path(self) -> str:
        return self.device.path

    def _bridge(self):
        """Read real events back out of the kernel and type them."""
        from evdev import ecodes

        while not self._stop.is_set():
            ready, _, _ = select.select([self.device.fd], [], [], 0.2)
            if not ready:
                continue
            try:
                events = list(self.device.read())
            except (OSError, BlockingIOError):
                return
            for event in events:
                if event.type != ecodes.EV_KEY:
                    continue
                if event.code == ecodes.KEY_LEFTSHIFT:
                    self._shift = bool(event.value)
                    continue
                if event.value != 1:             # key down only
                    continue
                table = self._shifted if self._shift else self._plain
                data = table.get(event.code) or self._plain.get(event.code)
                if data is None:
                    continue
                self.typed.extend(data)
                self._terminal.type(data)
                self.delivered += 1

    def press(self, key, shift: bool = False, timeout: float = 5.0) -> None:
        """
        One key down and up, not returning until the kernel handed it back.

        Waiting on `delivered` is what stops this being a test that writes
        bytes to a pipe. If the event never comes back -- no node, no
        reader, a kernel that dropped it -- this raises here rather than
        letting the panel test fail thirty seconds later as a press that
        "did not arrive", which is also what a broken panel looks like.
        """
        from evdev import ecodes

        before = self.delivered
        if shift:
            self._uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
            self._uinput.syn()
        self._uinput.write(ecodes.EV_KEY, key, 1)
        self._uinput.syn()
        self._uinput.write(ecodes.EV_KEY, key, 0)
        self._uinput.syn()
        if shift:
            self._uinput.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
            self._uinput.syn()
        deadline = time.monotonic() + timeout
        while self.delivered == before:
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"a press of keycode {key} was written to {self.path} "
                    f"and never came back out of the kernel in {timeout}s, "
                    f"so nothing was typed on the terminal and nothing "
                    f"below would be testing a keyboard")
            time.sleep(0.005)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self.device.close()
        except OSError:
            pass
        self._uinput.close()

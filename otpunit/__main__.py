"""Entry point for the print unit.

    python3 -m otpunit            on the Pi: real OLED, real buttons, real CUPS
    python3 -m otpunit --sim      anywhere: terminal panel, keyboard, fake printer

The simulator exists so the whole UI can be built, demonstrated and tested
without touching hardware.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otpunit import config
from otpunit.codewords import Vocabulary
from otpunit.hw.buttons import GpioButtons, KeyboardButtons
from otpunit.hw.display import ConsoleDisplay, Ssd1306Display
from otpunit.printer import Cups, Device
from otpunit.ui import App


class SimulatedCups(Cups):
    """A printer that always exists and swallows jobs, for --sim."""

    def __init__(self):
        super().__init__(run=None)
        self.submitted = []

    def devices(self):
        return [Device("usb://Simulated/Laser?serial=SIM", "Simulated Laser")]

    def ensure_queue(self, device, name="OTP"):
        return name

    def submit(self, data, name="OTP", title="OTP", options=None):
        self.submitted.append((title, len(data)))
        return f"{name}-{len(self.submitted)}"

    def active_jobs(self, name="OTP"):
        return 0

    def purge(self, name="OTP"):
        pass


def open_display(args):
    """The panel, or None if there is not one on the bus."""
    try:
        return Ssd1306Display(rotate=args.rotate)
    except Exception as exc:
        # A display that is not on the bus yet must not take the unit down.
        # With Restart=on-failure this would otherwise burn through
        # systemd's start limit in seconds and leave the service
        # permanently failed, with no panel to say why.
        print(f"OLED unavailable ({exc})", file=sys.stderr)
        return None


def build(args, display=None):
    if args.sim:
        return ConsoleDisplay(), KeyboardButtons(), SimulatedCups()
    return display or ConsoleDisplay(stream=sys.stderr), GpioButtons(), Cups()


def main(argv=None):
    parser = argparse.ArgumentParser(description="OTP pad print unit")
    parser.add_argument("--sim", action="store_true",
                        help="run in the terminal with a simulated printer")
    parser.add_argument("--rotate", type=int, default=0, choices=(0, 1, 2, 3),
                        help="rotate the OLED by N*90 degrees")
    parser.add_argument("--config", default=config.CONFIG_PATH,
                        help="settings file (default: %(default)s)")
    parser.add_argument("--diagnostic", action="store_true",
                        help="print the status sheet and exit, even if a "
                             "panel is attached")
    args = parser.parse_args(argv)

    settings = config.load(args.config)

    # Probed once, here, and the handle carried forward. Opening the panel
    # is not free and doing it twice would leave one of them unclosed.
    display = None if args.sim else open_display(args)

    # No panel means no way to choose a codeword and no way to read a
    # warning, so the unit cannot print pads and cannot say why. The
    # printer is the only output device it has left: wait for one, print
    # everything known about the unit, and say what hardware to add.
    if args.diagnostic or (not args.sim and display is None):
        from otpunit import diagnostics

        from otpunit import unattended

        if display is not None:
            display.close()
        print("no display detected; running unattended"
              if display is None else "unattended sequence requested",
              file=sys.stderr)
        # Buttons are optional here and stay optional: with no panel a
        # press only ever means "stop waiting", so a unit with nothing
        # bridging those pins still produces a pad, just later.
        buttons = unattended.open_buttons()
        try:
            return diagnostics.run_headless(
                Cups(), settings=settings, once=args.diagnostic,
                buttons=buttons,
                log=lambda message: print(message, file=sys.stderr))
        finally:
            if buttons is not None:
                buttons.close()

    display, buttons, cups = build(args, display)
    app = App(
        display=display,
        buttons=buttons,
        cups=cups,
        settings=settings,
        vocabulary=Vocabulary(),
        config_path=args.config,
    )

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        display.close()
        buttons.close()

    if app.shutdown_requested and not args.sim:
        subprocess.run(["/sbin/poweroff"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

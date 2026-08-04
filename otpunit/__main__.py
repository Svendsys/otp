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

from otpunit import config, hmi
from otpunit.codewords import Vocabulary
from otpunit.hw.buttons import KeyboardButtons
from otpunit.hw.display import ConsoleDisplay
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="OTP pad print unit")
    parser.add_argument("--sim", action="store_true",
                        help="run in the terminal with a simulated printer")
    parser.add_argument("--rotate", type=int, default=0, choices=(0, 1, 2, 3),
                        help="rotate the OLED by N*90 degrees")
    parser.add_argument("--config", default=config.CONFIG_PATH,
                        help="settings file (default: %(default)s)")
    parser.add_argument("--diagnostic", action="store_true",
                        help="run the unattended print sequence and exit, "
                             "even if an interface is attached")
    args = parser.parse_args(argv)

    settings = config.load(args.config)
    log = lambda message: print(message, file=sys.stderr)   # noqa: E731

    if args.sim:
        interface = hmi.Interface(ConsoleDisplay(), KeyboardButtons(),
                                  "terminal", "keyboard")
        cups = SimulatedCups()
    else:
        # Display and input are looked for separately, so an OLED with a
        # USB keyboard, or a monitor with three buttons on a breadboard,
        # are both perfectly good interfaces. Probed once here and the
        # handles carried forward: opening these is not free.
        interface = hmi.detect(rotate=args.rotate, log=log)
        cups = Cups()
        log(f"interface -- {interface.describe()}")

    # No menu without something to draw on AND something to press. That is
    # not an error: it is the case this device exists for, so fall through
    # to printing unattended, using the printer itself as the interface.
    if args.diagnostic or not interface.interactive:
        from otpunit import diagnostics

        buttons = interface.buttons
        if interface.display is not None:
            interface.display.close()
        log("no usable interface; printing unattended" if not interface.interactive
            else "unattended sequence requested")
        try:
            return diagnostics.run_headless(
                cups, settings=settings, once=args.diagnostic,
                buttons=buttons, log=log)
        finally:
            if buttons is not None:
                buttons.close()

    app = App(
        display=interface.display,
        buttons=interface.buttons,
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
        interface.close()

    if app.shutdown_requested and not args.sim:
        subprocess.run(["/sbin/poweroff"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

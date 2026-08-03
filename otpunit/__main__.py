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


def build(args):
    if args.sim:
        return ConsoleDisplay(), KeyboardButtons(), SimulatedCups()
    return Ssd1306Display(rotate=args.rotate), GpioButtons(), Cups()


def main(argv=None):
    parser = argparse.ArgumentParser(description="OTP pad print unit")
    parser.add_argument("--sim", action="store_true",
                        help="run in the terminal with a simulated printer")
    parser.add_argument("--rotate", type=int, default=0, choices=(0, 1, 2, 3),
                        help="rotate the OLED by N*90 degrees")
    parser.add_argument("--config", default=config.CONFIG_PATH,
                        help="settings file (default: %(default)s)")
    args = parser.parse_args(argv)

    display, buttons, cups = build(args)
    app = App(
        display=display,
        buttons=buttons,
        cups=cups,
        settings=config.load(args.config),
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

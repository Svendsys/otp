"""Entry point for the print unit.

    python3 -m otpunit            on the Pi: real OLED, real buttons, real CUPS
    python3 -m otpunit --sim      anywhere: terminal panel, keyboard, fake printer

The simulator exists so the whole UI can be built, demonstrated and tested
without touching hardware.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from otpunit import config, hmi, posture
from otpunit.codewords import Vocabulary
from otpunit.hw.buttons import KeyboardButtons
from otpunit.hw.display import ConsoleDisplay
from otpunit.printer import Cups, Device
from otpunit.ui import App


class SimulatedCups(Cups):
    """A printer that always exists and swallows jobs, for --sim."""

    def __init__(self):
        # temp_dir is a scratch path, not the inherited /run/cups/tmp:
        # _clear_temp is reachable from --sim and would otherwise unlink
        # the real daemon's scratch files on a machine with CUPS installed.
        super().__init__(run=None,
                         temp_dir=tempfile.mkdtemp(prefix="otp-sim-cups-"))
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

    def printer_fault(self, name="OTP"):
        # Stubbed like active_jobs: the inherited one shells out to the
        # host's lpstat, which may be a real queue.
        return None

    def state_reasons(self, name="OTP"):
        # [] and not None: None means "could not ask", which the UI treats
        # as unknown, not clean.
        return []

    def queue_stopped(self, name="OTP"):
        return False

    def resume(self, name="OTP"):
        return True

    def purge(self, name="OTP"):
        pass


PROVE_SECONDS = 20.0


def _prove(interface, log) -> bool:
    from otpunit.hw.display import Frame

    try:
        interface.display.show(Frame(
            title="PRESS ANY BUTTON",
            lines=["TO USE THE PANEL.", "", "OTHERWISE THIS UNIT",
                   "PRINTS ON ITS OWN."],
            footer="WAITING..."))
    except Exception:                            # noqa: BLE001
        pass
    if interface.prove(PROVE_SECONDS):
        log("panel answered; using it")
        return True
    log("nobody answered the panel; printing instead")
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description="OTP pad print unit")
    parser.add_argument("--sim", action="store_true",
                        help="run in the terminal with a simulated printer")
    # 1 and 3 are absent deliberately: they give a 64x128 portrait panel
    # (see Ssd1306Display.SQUARE_ON). luma counts rotate in 90-degree
    # steps, so 2 is half a turn.
    parser.add_argument("--rotate", type=int, default=0, choices=(0, 2),
                        help="0 for upright, 2 for upside-down (180 degrees). "
                             "90 and 270 need a portrait layout the 21x8 "
                             "character grid does not have")
    parser.add_argument("--config", default=config.CONFIG_PATH,
                        help="settings file (default: %(default)s)")
    parser.add_argument("--diagnostic", action="store_true",
                        help="run the unattended print sequence and exit, "
                             "even if an interface is attached")
    args = parser.parse_args(argv)

    settings = config.load(args.config)
    log = lambda message: print(message, file=sys.stderr)   # noqa: E731

    # The simulator makes nothing secret and runs on ordinary machines.
    refuse_live = "" if args.sim else posture.refusal(settings)
    if refuse_live:
        log(f"live pads refused -- {refuse_live}")

    if args.sim:
        # allow_quit only in the simulator: on a real unit QUIT would end
        # the process.
        interface = hmi.Interface(ConsoleDisplay(),
                                  KeyboardButtons(allow_quit=True),
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

    # No menu without something to draw on and something to press. That is
    # not an error: fall through to printing unattended, with the printer
    # as the interface. An interface nobody answers does not count either
    # (see Interface.prove). --diagnostic goes headless whatever is
    # attached, so it skips the prove window.
    driven = (not args.diagnostic and interface.interactive
              and (args.sim or _prove(interface, log)))

    if args.diagnostic or not driven:
        from otpunit import diagnostics

        buttons = interface.buttons
        if interface.display is not None:
            interface.display.close()
        log("no usable interface; printing unattended" if not interface.interactive
            else "unattended sequence requested")
        try:
            return diagnostics.run_headless(
                cups, settings=settings, once=args.diagnostic,
                buttons=buttons, log=log, refuse_live=refuse_live)
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
        refuse_live=refuse_live,
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
    if args.sim:
        return 0
    # The menu ended without a shutdown request (a hangup, a stray key, an
    # empty screen stack). Exit non-zero so systemd's Restart=on-failure
    # brings the unit back.
    log("panel exited without a shutdown request; restarting")
    return 1


if __name__ == "__main__":
    sys.exit(main())

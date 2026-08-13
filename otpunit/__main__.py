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

from otpunit import config, hmi
from otpunit.codewords import Vocabulary
from otpunit.hw.buttons import KeyboardButtons
from otpunit.hw.display import ConsoleDisplay
from otpunit.printer import Cups, Device
from otpunit.ui import App


class SimulatedCups(Cups):
    """A printer that always exists and swallows jobs, for --sim."""

    def __init__(self):
        # temp_dir to a scratch path, NOT the inherited /run/cups/tmp.
        # _clear_temp is reachable from --sim without being overridden:
        # unattended.run's finally block calls job.finish(purge=False)
        # whenever anything raises before `purge = drained` is assigned, and
        # finish() calls cups._clear_temp(). On the unit itself, or any dev
        # box that has run install.sh, that unlinks every file in the REAL
        # daemon's live spool scratch, as root, destroying another job's
        # in-flight filter output. Overriding the DIRECTORY fixes every path
        # that can reach it, rather than the one method someone remembered
        # to stub -- which is how this was missed: the completeness test
        # listed six method names by hand and _clear_temp was not among them.
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
        # Stubbed for the same reason active_jobs is. RunJob consults this
        # before it will call a copy finished, and the inherited one shells
        # out to the host's `lpstat -p OTP` -- which on the unit itself, or
        # any machine that has run install.sh, is a REAL queue. Left
        # inherited, `--sim` reported the host printer's fault over a
        # simulated pad that reached no printer at all, and blocked for up
        # to Cups.TIMEOUT doing it.
        return None

    def state_reasons(self, name="OTP"):
        # [] and not None. None means "could not ask", which the UI is
        # required to treat as unknown rather than as clean, and a simulated
        # printer that reports "I could not be asked" would park --sim on
        # the recovery path for a queue that does not exist.
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
        # allow_quit only in the simulator: on a real unit QUIT ends the
        # process with status 0, and Restart=on-failure reads that as
        # success and does not restart.
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

    # No menu without something to draw on AND something to press. That is
    # not an error: it is the case this device exists for, so fall through
    # to printing unattended, using the printer itself as the interface.
    # An interface that nobody answers is not an interface. See
    # Interface.prove: opening GPIO buttons proves only that a pin could
    # be reserved, so without this a monitor plus no buttons walks into a
    # menu that blocks forever instead of printing.
    # --diagnostic goes headless whatever is attached, so it must not sit
    # through the prove window first: it drew PRESS ANY BUTTON on the panel,
    # waited twenty seconds and then discarded the answer.
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
    if args.sim:
        return 0
    # The menu ended without anyone asking to shut down -- a hangup, a
    # stray key, an empty screen stack. Exit NON-ZERO so systemd's
    # Restart=on-failure brings the unit back. Returning 0 here told
    # systemd the appliance had finished its job, and it stayed off until
    # somebody power-cycled it.
    log("panel exited without a shutdown request; restarting")
    return 1


if __name__ == "__main__":
    sys.exit(main())

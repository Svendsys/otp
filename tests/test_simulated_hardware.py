"""The unit driven against real kernel interfaces and a real cupsd.

Every other test in this suite substitutes something. That is the right
default -- it keeps the suite fast and runnable anywhere -- but it means
the code that talks to the outside world has never executed. Six rounds of
adversarial review read that code carefully and missed the defect that
mattered most: `MaxJobs 1` makes CUPS *refuse* a second job rather than
queue it, so the shipped unit could not print a pad at all. A daemon said
so in four seconds.

So these tests substitute nothing they can avoid:

  * a real cupsd, with the directives and directory modes taken from
    `device/install.sh` itself, and a backend that records the bytes that
    reached the printer;
  * a real gpiochip from `gpio-sim`, driven from sysfs, so gpiozero's
    press and hold detection runs against the kernel;
  * a real SMBus from `i2c-stub`, so the SSD1306 init sequence runs;
  * a real DRM connector from `vkms` and a real keyboard from `uinput`,
    so the interface probes read files a kernel wrote.

None of that needs a Raspberry Pi. Anything not available is skipped with
a reason rather than failed -- a kernel without `vkms` should still get the
other four.

    sudo ./harness/kernel-sim.sh up
    sudo pytest tests/test_simulated_hardware.py -v
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import cupsrig                                   # noqa: E402
from otpunit import config, diagnostics, hmi, printer, unattended  # noqa: E402

SIM_STATE = Path("/run/otp-kernel-sim")

# Everything in this file is the harness; pytest.ini deselects it by
# default so the fast suite stays fast. `pytest -m hardware` runs it.
pytestmark = pytest.mark.hardware

WHY_NO_CUPS = cupsrig.available()
needs_cups = pytest.mark.skipif(bool(WHY_NO_CUPS), reason=WHY_NO_CUPS
                                or "cups rig unavailable")


def sim(name):
    """The value kernel-sim.sh recorded for a simulator, or None."""
    try:
        return (SIM_STATE / name).read_text().strip()
    except OSError:
        return None


def needs_sim(name, what):
    return pytest.mark.skipif(
        sim(name) is None,
        reason=f"{what} is not up -- run: sudo ./harness/kernel-sim.sh up")


@pytest.fixture
def rig(tmp_path):
    with cupsrig.CupsRig(tmp_path / "cups") as running:
        yield running


def settle(cups, queue, seconds=45):
    """Wait for the queue to go idle, as a person watching the tray would."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cups.active_jobs(queue) == 0:
            time.sleep(0.4)                      # let the backend finish
            return True
        time.sleep(0.2)
    return False


def to_printer(data: bytes) -> bytes:
    """
    What a job would look like with its per-job identifiers removed.

    Two jobs of the same document differ in exactly two places, both
    deliberate: the PJL header carries the job's title, and the PDF trailer
    carries a fresh /ID. Neither is a page. Normalising them is what lets a
    test assert the thing that actually matters -- that copy A and copy B
    carry the same key -- rather than asserting a hash that was never going
    to be stable.
    """
    body = data[data.find(b"%PDF"):] if b"%PDF" in data else data
    return re.sub(rb"/ID\s*\[[^\]]*\]", b"/ID[]", body)


# --- the print path, against a daemon rather than a double ---------------


@needs_cups
class TestTheRealPrintPath:
    def test_a_printer_is_discovered_through_real_lpinfo(self, rig):
        found = rig.cups().devices()
        assert found, "lpinfo -v reported nothing the unit would accept"
        assert found[0].uri.startswith("usb://")

    def test_a_driver_is_matched_through_real_lpinfo_m(self, rig):
        """
        _match_ppd scores candidates from `lpinfo -m`, which has hundreds
        of entries on a real system and one entry in every unit test that
        has ever exercised it.
        """
        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        assert queue == "OTP"
        assert "OTP" in rig.run(["/usr/bin/lpstat", "-p", queue]).stdout.decode()

    def test_an_unmatched_printer_leaves_no_queue_behind(self, rig):
        """
        lpadmin exits non-zero on a bad -m and CREATES THE QUEUE ANYWAY, so
        a failed setup used to leave `OTP` enabled, idle, driverless and
        perfectly happy to accept a whole pad it would never print. The
        caller keeps using the name after reporting the error, so that is a
        pad spooled into a black hole. `_remove_queue` exists for this and
        has only ever been checked against a fake lpadmin.
        """
        cups = rig.cups()
        # An unreachable IPP URI, NOT an unknown usb:// make and model.
        # With the latter _match_ppd returns None, _lpadmin is never
        # called, no queue is ever created, and _remove_queue runs against
        # a name that does not exist -- so deleting the _remove_queue call
        # from printer.py left this test passing. An IPP URI takes the
        # `-m everywhere` path, where lpadmin exits non-zero AND creates
        # the queue anyway, which is the actual defect being guarded.
        unknown = printer.Device("ipp://127.0.0.1:9/ipp/print", "Unreachable IPP")
        with pytest.raises(printer.PrinterError):
            cups.ensure_queue(unknown)
        listed = rig.run(["/usr/bin/lpstat", "-p", "OTP"])
        assert listed.returncode != 0 or b"OTP" not in listed.stdout, \
            "a queue with no driver was left able to accept key material"

    def test_the_bytes_we_submit_are_the_bytes_that_get_printed(self, rig):
        from otpunit import jobs

        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        pdf = bytes(jobs.generate(
            jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings())))
        cups.submit(bytearray(pdf), name=queue, title="TABULA RECTA",
                    options={"media": "A4"})
        assert settle(cups, queue)
        printed = rig.printed()
        assert [title for title, _ in printed] == ["TABULA RECTA"]
        assert b"%PDF" in printed[0][1]

    def test_the_two_copies_reach_the_printer_identical(self, rig):
        """
        The property that makes them a pair, asserted at the last point we
        can see: the bytes handed to the printer, after the real filter
        chain has rewritten them.
        """
        from otpunit import jobs

        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        job = jobs.PadPairJob(
            jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                         config.Settings(pages=2)), cups, queue)
        job.generate()
        job.print_next_copy()
        assert settle(cups, queue)
        job.print_next_copy()
        assert settle(cups, queue)
        job.finish()

        got = dict(rig.printed())
        assert "OTP A" in got and "OTP B" in got
        assert to_printer(got["OTP A"]) == to_printer(got["OTP B"])

    def test_the_codeword_reaches_no_uncompressed_channel(self, rig):
        """
        The title is not the only channel: measured here, the driver embeds
        it in a PJL header inside the data stream, so whatever is in it
        reaches the printer's own memory -- and the manual is explicit that
        the printer is part of the pad.

        Scope, stated precisely because the earlier name overstated it.
        This is NOT "the codeword never reaches the printer" -- it is
        printed on every pad page by design, so a device that did not send
        it could not print it. What this pins is that it appears in no
        UNCOMPRESSED side channel: not the PJL job name, not the PDF Info
        dictionary. Both were verified to make this test red when the
        codeword is injected into them.
        """
        from otpunit import jobs

        codeword = "RUSTED-BADGER"
        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        job = jobs.PadPairJob(
            jobs.JobSpec(jobs.JobKind.PAD_PAIR, codeword,
                         config.Settings(pages=2)), cups, queue)
        job.generate()
        job.print_next_copy()
        assert settle(cups, queue)
        job.finish()

        for title, data in rig.printed():
            assert codeword.encode() not in data, f"codeword in {title}"
            for half in codeword.split("-"):
                assert half.encode() not in data, f"{half} in {title}"

    def test_a_healthy_queue_reports_no_fault(self, rig):
        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        assert cups.printer_fault(queue) is None

    def test_a_failing_tray_is_reported_as_a_fault(self, rig):
        """
        printer_fault() has only been exercised against strings a test
        wrote. This runs it against what lpstat says about a queue that
        really did fail a job.
        """
        from otpunit import jobs

        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        rig.fail_jobs(True)
        pdf = bytes(jobs.generate(
            jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings())))
        cups.submit(bytearray(pdf), name=queue, title="DOOMED",
                    options={"media": "A4"})
        settle(cups, queue, seconds=30)
        fault = cups.printer_fault(queue)
        assert fault, "a queue that just failed a job reported nothing wrong"


@needs_cups
class TestTheUnattendedSequenceAgainstRealCups:
    """
    The regression tests for the two defects that made the shipped unit
    useless, both found by a daemon rather than by reading.
    """

    def sequence_settings(self, **kwargs):
        base = dict(pages=2, auto_delay=0, auto_swap_delay=0,
                    auto_manual=False, auto_codeword="SILENT-OSPREY")
        base.update(kwargs)
        return config.Settings(**base)

    def run_it(self, rig, **kwargs):
        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        return unattended.run(cups, settings=self.sequence_settings(**kwargs),
                              queue=queue, log=lambda *_: None,
                              sleep=time.sleep), cups, queue

    def test_every_sheet_reaches_the_printer_under_the_shipped_maxjobs(self, rig):
        """
        The showstopper. cupsd does not queue past MaxJobs, it refuses --
        `lp: Too many active jobs.` With waits in two of six gaps the
        status sheet and the manual printed and everything after them was
        rejected, so the operator got a sheet promising a pad pair followed
        by silence. Nothing about that is visible without a real daemon
        enforcing a real MaxJobs.
        """
        assert cupsrig.shipped_directives().get("MaxJobs"), \
            "install.sh no longer sets MaxJobs; this test is not testing it"
        result, cups, queue = self.run_it(rig)
        settle(cups, queue, seconds=60)
        titles = rig.titles()
        assert result == 0, f"sequence failed; printed {titles}"
        assert titles == ["OTP status", "TABULA RECTA", "OTP A",
                          "REMOVE COPY A", "OTP B", "WHAT TO DO NOW"]

    def test_an_empty_tray_does_not_produce_a_sheet_claiming_a_pair(self, rig):
        """
        ErrorPolicy is abort-job, so a failed job leaves the queue as empty
        as a successful one. Measured with no paper: all seven jobs
        aborted, the queue drained in four seconds, and the unit printed
        "YOUR PAD PAIR IS PRINTED / They are identical" over an empty tray,
        returned 0, and wiped the key so copy B could never be remade.
        """
        rig.fail_jobs(True)
        result, cups, queue = self.run_it(rig)
        settle(cups, queue, seconds=60)
        assert result == 1, "a tray that printed nothing was called a pair"

    def test_no_key_material_is_left_in_the_spool_afterwards(self, rig):
        result, cups, queue = self.run_it(rig)
        settle(cups, queue, seconds=60)
        leftover = [path for path in rig.spool_files()
                    if b"SILENT" in path.read_bytes()
                    or b"OSPREY" in path.read_bytes()]
        assert not leftover, f"codeword left in the spool: {leftover}"

    def test_the_codeword_reaches_no_cups_file_at_all(self, rig):
        """
        The SD card is the part most likely to be captured with the unit,
        so the rule is that a live pad's name never reaches anything CUPS
        writes: not job.cache, not printers.conf, not a log.
        """
        result, cups, queue = self.run_it(rig)
        settle(cups, queue, seconds=60)
        rig.stop()                               # flushes job.cache
        hits = []
        for path in rig.root.rglob("*"):
            if not path.is_file() or rig.jobs in path.parents:
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            if b"SILENT-OSPREY" in blob or b"OSPREY" in blob:
                hits.append(str(path.relative_to(rig.root)))
        assert not hits, f"codeword reached {hits}"


# --- the panel, against the kernel rather than a double ------------------


@needs_sim("gpio-chip", "gpio-sim")
class TestTheRealButtonPath:
    """
    gpiozero -> lgpio -> /dev/gpiochipN, with a kernel on the other end.

    The tap-versus-hold discrimination is the part worth running for real:
    OK and BACK mean opposite things while a job is printing, so a press
    that resolves the wrong way can purge the spool and destroy a pair.
    """

    def press(self, pin, seconds=0.05):
        control = Path(sim("gpio-control"))
        pull = control / f"sim_gpio{pin}" / "pull"
        pull.write_text("pull-down")             # button to ground
        time.sleep(seconds)
        pull.write_text("pull-up")               # released
        time.sleep(0.1)

    @pytest.fixture
    def buttons(self):
        """
        GpioButtons, but only if it really bound to the simulated chip.

        gpiozero picks its gpiochip from Pi board detection -- it reads
        /proc/device-tree/model or a Revision line in /proc/cpuinfo -- and
        a machine that is not a Pi has neither. Even where it constructs,
        it opens gpiochip0, which on a normal host is some real controller
        rather than the one gpio-sim just created.

        So this checks which chip the process actually opened and skips
        with that reason if it is not ours. Asserting against a chip the
        driver never opened would fail for a reason that has nothing to do
        with the code under test; quietly passing would be worse.
        """
        from otpunit.hw import buttons as buttons_mod

        try:
            panel = buttons_mod.GpioButtons()
        except Exception as exc:                 # noqa: BLE001
            pytest.skip(f"gpiozero could not open a gpiochip here: {exc}")

        wanted = f"/dev/gpiochip{sim('gpio-chip')}"
        opened = set()
        for fd in Path("/proc/self/fd").iterdir():
            try:
                target = str(fd.resolve())
            except OSError:
                continue
            if target.startswith("/dev/gpiochip"):
                opened.add(target)
        if wanted not in opened:
            panel.close()
            pytest.skip(
                f"gpiozero bound to {opened or 'no gpiochip'} rather than "
                f"{wanted}; it selects the chip from Pi board detection, "
                f"which cannot find gpio-sim. See harness/README.md.")
        yield panel
        panel.close()

    def test_a_tap_on_up_arrives_as_up(self, buttons):
        from otpunit.hw.buttons import PIN_UP, Press

        self.press(PIN_UP)
        assert buttons.wait(timeout=2) is Press.UP

    def test_a_tap_on_ok_arrives_as_ok_not_back(self, buttons):
        from otpunit.hw.buttons import PIN_OK, Press

        self.press(PIN_OK, seconds=0.05)
        assert buttons.wait(timeout=2) is Press.OK

    def test_a_hold_on_ok_arrives_as_back_exactly_once(self, buttons):
        """
        One press must produce one event. The rejected design -- when_held
        setting a flag that when_released reads -- races across two
        gpiozero threads and can emit OK *and* BACK for a single press.
        """
        from otpunit.hw.buttons import HOLD_SECONDS, PIN_OK, Press

        self.press(PIN_OK, seconds=HOLD_SECONDS + 0.3)
        assert buttons.wait(timeout=3) is Press.BACK
        assert buttons.wait(timeout=0.5) is None, "one press, two events"


@needs_sim("i2c-bus", "i2c-stub")
class TestTheRealI2CPath:
    def test_the_scan_finds_the_display_address(self):
        """
        _i2c_scan is the status sheet's answer to "is it wired up, or wired
        up at the other address?" -- and in CI it has only ever been able
        to say "smbus2 not installed".
        """
        pytest.importorskip("smbus2")
        found = diagnostics._i2c_scan(bus=int(sim("i2c-bus")))
        # Lowercased on both sides. `"0x3C" in found.upper()` cannot ever
        # match: upper() turns the address into 0X3C and the needle still
        # carries a lowercase x. The scan was working; the assertion was
        # not, and it reported the correct answer as the failure message.
        assert "0x3c" in found.lower(), found

    def test_the_display_driver_initialises_against_a_real_bus(self):
        pytest.importorskip("luma.oled")
        from otpunit.hw.display import Ssd1306Display

        try:
            panel = Ssd1306Display(port=int(sim("i2c-bus")), address=0x3C)
        except Exception as exc:                 # noqa: BLE001
            pytest.skip(f"luma could not drive the stub: {exc}")
        panel.close()


@needs_sim("vkms", "vkms")
class TestTheRealScreenProbe:
    def test_a_connected_drm_connector_is_seen(self):
        """
        screen_connected() reads DRM status rather than trusting isatty,
        because the service binds tty1 whether or not anything is plugged
        into the HDMI socket. Here the file is one a kernel wrote.
        """
        statuses = {path: Path(path).read_text().strip()
                    for path in __import__("glob").glob(
                        "/sys/class/drm/card*/status")}
        if "connected" not in statuses.values():
            pytest.skip(f"vkms reported no connected connector: {statuses}")
        assert hmi.screen_connected() is True


@needs_sim("keyboard.ready", "the uinput virtual keyboard")
class TestTheRealKeyboardProbe:
    def test_a_uinput_keyboard_is_seen(self):
        assert hmi.keyboard_connected() is True


# --- what the harness itself must not get wrong --------------------------


@needs_cups
def test_the_rig_uses_the_shipped_cups_configuration(rig):
    """
    The rig reads its directives out of device/install.sh. If that stops
    working it will quietly test defaults instead of what ships, and every
    test above becomes decorative.
    """
    shipped = cupsrig.shipped_directives()
    assert shipped.get("PreserveJobFiles") == "No"
    assert shipped.get("ErrorPolicy") == "abort-job"
    running = (rig.root / "etc" / "cupsd.conf").read_text()
    for key, value in shipped.items():
        assert f"{key} {value}" in running


def test_the_harness_script_is_syntactically_valid():
    script = REPO / "harness" / "kernel-sim.sh"
    assert script.exists()
    assert os.access(script, os.X_OK), "kernel-sim.sh is not executable"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0

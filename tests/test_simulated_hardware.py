"""The unit driven against real kernel interfaces and a real cupsd.

These tests substitute as little as they can:

* a real cupsd, configured from `device/install.sh`, with a backend that
  records the bytes that reached the printer;
* a real gpiochip from `gpio-sim`, driven from sysfs;
* a real SMBus from `i2c-stub`, so the SSD1306 init sequence runs;
* a real DRM connector from `vkms` and a real keyboard from `uinput`.

gpiozero will not open a gpiochip without a board revision, so the panel is
driven inside a mount namespace where /proc/cpuinfo says Pi Zero 2 W (see
pirig.py). Anything unavailable is skipped with a reason.

    sudo ./harness/kernel-sim.sh up
    sudo pytest tests/test_simulated_hardware.py -v
"""
from __future__ import annotations

import errno
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
import pirig                                     # noqa: E402
# Shared with the fast tier, so both tiers test the same queue semantics.
from test_hardware import MemoryStarvedQueue     # noqa: E402
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
    """A job with its per-job identifiers removed: the PJL header's title and the
    PDF trailer's /ID. What is left is comparable between copy A and copy B.
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
        """_match_ppd picks a driver from real `lpinfo -m` output, which has
        hundreds of entries where the unit tests supply one.
        """
        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        assert queue == "OTP"
        assert "OTP" in rig.run(["/usr/bin/lpstat", "-p", queue]).stdout.decode()

    def test_an_unmatched_printer_leaves_no_queue_behind(self, rig):
        """A failed setup leaves no queue. lpadmin exits non-zero on a bad -m but
        creates the queue anyway, and an enabled driverless queue accepts a
        whole pad it never prints.
        """
        cups = rig.cups()
        # An unreachable IPP URI, not an unknown usb:// model: only the `-m
        # everywhere` path makes lpadmin fail after creating the queue, which
        # is what _remove_queue is for.
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
        """Copies A and B are identical in the bytes handed to the printer, after
        the real filter chain has rewritten them.
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
        """The codeword appears in no uncompressed side channel: not the PJL job
        name the driver embeds, not the PDF Info dictionary. It is printed on
        every pad page by design, so it does reach the printer in the page
        content.
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
        """printer_fault() reports a fault from what lpstat really says about a
        queue that failed a job.
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
    """The unattended sequence against a daemon that enforces the shipped
    configuration.
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
        """Every sheet of the sequence reaches the printer under the shipped
        MaxJobs. cupsd refuses a job past the limit (`lp: Too many active
        jobs.`), so each gap needs a wait.
        """
        assert cupsrig.shipped_directives().get("MaxJobs"), \
            "install.sh no longer sets MaxJobs; this test is not testing it"
        result, cups, queue = self.run_it(rig)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        titles = rig.titles()
        assert result == 0, f"sequence failed; printed {titles}"
        assert titles == ["OTP status", "TABULA RECTA", "OTP A",
                          "REMOVE COPY A", "OTP B", "WHAT TO DO NOW"]

    def test_an_empty_tray_does_not_produce_a_sheet_claiming_a_pair(self, rig):
        """With no paper the run fails and never claims a pair. Under abort-job a
        failed job drains the queue just as a printed one does.
        """
        rig.fail_jobs(True)
        result, cups, queue = self.run_it(rig)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        assert result == 1, "a tray that printed nothing was called a pair"

    def test_no_key_material_is_left_in_the_spool_afterwards(self, rig):
        result, cups, queue = self.run_it(rig)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        leftover = [path for path in rig.spool_files()
                    if b"SILENT" in path.read_bytes()
                    or b"OSPREY" in path.read_bytes()]
        assert not leftover, f"codeword left in the spool: {leftover}"

    def test_the_codeword_reaches_no_cups_file_at_all(self, rig):
        """A live pad's codeword reaches nothing CUPS writes: not job.cache, not
        printers.conf, not a log. The SD card is likely to be captured with the
        unit.
        """
        result, cups, queue = self.run_it(rig)
        assert settle(cups, queue, seconds=60), "the queue never drained"
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


# --- printers that fail, which is most printers --------------------------


def sheet_text(data: bytes) -> str:
    """The text of a sheet the backend recorded, uppercased for matching. The
    Generic PDF Printer chain passes the PDF through unaltered behind a PJL
    header.
    """
    import io

    import pypdf

    body = data[data.find(b"%PDF"):] if b"%PDF" in data else data
    found = []
    for page in pypdf.PdfReader(io.BytesIO(body)).pages:
        found.append(page.extract_text() or "")
    return " ".join(" ".join(found).split()).upper()


@needs_cups
class TestTheRigCanActuallyFail:
    """Each failure mode is observably different from a clean print; a mode that
    degraded to success would make every later test unable to fail.

    The shapes differ: abort-job drains the queue, STOP disables it and keeps
    the job, RETRY leaves it pending.
    """

    # mode -> (queue drains, printer reports a fault, devices still listed)
    SHAPES = {
        "": (True, False, 1),
        # The dangerous one: indistinguishable from success by drain alone.
        "media-empty": (True, True, 1),
        "stop": (False, True, 1),
        "retry": (False, True, 1),
        "disconnect": (True, True, 0),
        "truncate": (True, True, 1),
    }

    def observe(self, rig, mode="", wait=30, **kwargs) -> dict:
        from otpunit import jobs

        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        rig.set_failure(mode, **kwargs)
        card = bytes(jobs.generate(
            jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings())))
        cups.submit(bytearray(card), name=queue, title="PROBE",
                    options={"media": "A4"})
        # Waits for the backend to have run and the picture to stop moving, not
        # for the queue to drain: two modes never drain. Stability, because
        # cupsd's mid-print filter chatter is also a printer-state-message. The
        # printed sizes are sampled so a file still being truncated counts as
        # moving.
        deadline = time.monotonic() + wait
        previous, unchanged = None, 0
        while time.monotonic() < deadline:
            sample = (cups.active_jobs(queue) == 0,
                      cups.printer_fault(queue),
                      len(cups.devices()),
                      rig.backend_invocations(),
                      [len(body) for _, body in rig.printed()])
            unchanged = unchanged + 1 if sample == previous else 0
            previous = sample
            if sample[3] and unchanged >= 2:
                break
            time.sleep(0.4)
        return {
            "drained": cups.active_jobs(queue) == 0,
            "fault": cups.printer_fault(queue),
            "devices": len(cups.devices()),
            "invocations": rig.backend_invocations(),
            "printed": rig.printed(),
        }

    def test_every_mode_is_shape_checked_or_excused(self, rig):
        """SHAPES covers every FAILURE_MODES entry except `retry-current`, which
        needs `until`, then clears and looks like a clean print; its invocation
        count is asserted in
        test_a_transient_that_clears_is_not_left_looking_like_a_fault.
        """
        excused = {"retry-current"}
        missing = set(cupsrig.CupsRig.FAILURE_MODES) - set(self.SHAPES) - excused
        assert not missing, f"failure modes with no asserted shape: {missing}"

    @pytest.mark.parametrize("mode", sorted(SHAPES))
    def test_each_mode_has_the_shape_it_claims(self, rig, mode):
        drains, faults, devices = self.SHAPES[mode]
        seen = self.observe(rig, mode)
        assert seen["invocations"] == 1, "the backend never ran"
        assert seen["drained"] is drains, seen
        assert bool(seen["fault"]) is faults, seen
        assert seen["devices"] == devices, seen
        if mode:
            assert (seen["drained"], bool(seen["fault"]), seen["devices"]) \
                != self.SHAPES[""], f"{mode} is indistinguishable from success"

    def test_a_stopped_queue_says_so_in_the_words_lpstat_uses(self, rig):
        # STOP is the only mode that reaches printer_fault's "disabled"
        # header-line path.
        assert "disabled" in (self.observe(rig, "stop")["fault"] or "")

    def test_truncate_records_a_short_job_rather_than_no_job(self, rig):
        """Truncate records a job shorter than a clean one from the same rig, so
        "some pages printed" is distinguishable from "nothing printed".
        """
        whole = self.observe(rig)["printed"]
        assert len(whole) == 1 and whole[0][1]
        partial = self.observe(rig, "truncate")["printed"]
        assert len(partial) == 2, "the second job was never recorded"
        assert 0 < len(partial[1][1]) < len(whole[0][1])

    def test_a_transient_that_clears_is_not_left_looking_like_a_fault(self, rig):
        """A fault that clears leaves no fault behind: a false failure sends
        someone to burn a pair that printed.
        """
        seen = self.observe(rig, "retry-current", until=2)
        assert seen["invocations"] == 3, "cupsd did not retry the job"
        assert seen["drained"] and not seen["fault"], seen

    def test_an_unknown_mode_is_refused_rather_than_ignored(self, rig):
        # A typo in a mode name must not read as a healthy printer.
        with pytest.raises(ValueError):
            rig.set_failure("out-of-paper")
        with pytest.raises(ValueError):
            rig.set_failure("retry-current")     # unbounded; would spin

    def test_refilling_the_tray_plugs_the_printer_back_in(self, rig):
        # A leftover `.unplugged` would make every later mode in this rig look
        # like a disconnect.
        assert self.observe(rig, "disconnect")["devices"] == 0
        rig.set_failure("")
        assert rig.plugged_in()
        assert self.observe(rig)["devices"] == 1


@needs_cups
class TestAFailedCopyIsNeverReportedAsAPair:
    """A false success destroys key material the operator believes is on paper, so
    a failed copy is never reported as a pair.

    The sequence is status, tabula, copy A, separator, copy B, final sheet, so
    `after=4 until=5` fails exactly copy B and leaves the final sheet
    printable.
    """

    def wired(self, rig):
        cups = rig.cups()
        return cups, cups.ensure_queue(cups.devices()[0])

    def card(self):
        from otpunit import jobs

        return bytes(jobs.generate(
            jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings())))

    def sequence_settings(self, **kwargs):
        base = dict(pages=2, auto_delay=0, auto_swap_delay=0,
                    auto_manual=False, auto_codeword="SILENT-OSPREY")
        base.update(kwargs)
        return config.Settings(**base)

    # --- the two shapes that do not drain -------------------------------

    def test_a_stopped_queue_is_never_reported_as_drained(self, rig):
        """drain() does not return True for a stopped queue, which keeps its job:
        True is what authorises the purge and the "pair printed" claim.
        """
        cups, queue = self.wired(rig)
        rig.set_failure("stop")
        cups.submit(bytearray(self.card()), name=queue, title="DOOMED",
                    options={"media": "A4"})
        assert unattended.drain(cups, queue, sleep=time.sleep,
                                log=lambda *_: None, timeout=8) is False
        assert cups.printer_fault(queue)

    def test_a_retrying_queue_is_never_reported_as_drained(self, rig):
        cups, queue = self.wired(rig)
        rig.set_failure("retry")
        cups.submit(bytearray(self.card()), name=queue, title="DOOMED",
                    options={"media": "A4"})
        assert unattended.drain(cups, queue, sleep=time.sleep,
                                log=lambda *_: None, timeout=8) is False
        assert cups.printer_fault(queue)

    # --- the shapes that drain, which is where the danger is ------------

    def test_copy_b_failing_is_not_called_a_pair(self, rig):
        cups, queue = self.wired(rig)
        rig.set_failure("media-empty", after=4, until=5)
        result = unattended.run(cups, settings=self.sequence_settings(),
                                queue=queue, log=lambda *_: None,
                                sleep=time.sleep)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        titles = rig.titles()
        assert result == 1, f"a run that lost copy B returned 0; got {titles}"
        # Both: the window is only aimed at copy B if the sequence is the six
        # sheets documented. One earlier kills the separator, and `result == 1`
        # plus "OTP A" would still hold.
        assert titles == ["OTP status", "TABULA RECTA", "OTP A",
                          "REMOVE COPY A", "OTP B", "WHAT TO DO NOW"], titles

    def test_the_final_sheet_says_what_did_and_did_not_reach_paper(self, rig):
        """When copy B fails, the final sheet says the pair is incomplete, names
        what the printer reported and does not claim the copies are identical.
        """
        cups, queue = self.wired(rig)
        rig.set_failure("media-empty", after=4, until=5)
        unattended.run(cups, settings=self.sequence_settings(), queue=queue,
                       log=lambda *_: None, sleep=time.sleep)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        final = [data for title, data in rig.printed()
                 if title == "WHAT TO DO NOW"]
        assert final, f"no final sheet reached paper; got {rig.titles()}"
        text = sheet_text(final[-1])
        assert "NOT USABLE" in text, text[:400]
        assert "DESTROY" in text
        # The sheet names what the printer reported.
        assert "OUT OF PAPER" in text, text[:400]
        # And it must not carry the DONE sheet's claim.
        assert "THEY ARE IDENTICAL" not in text
        assert "YOU NOW HAVE A PAD PAIR" not in text

    def test_a_partial_copy_b_is_not_called_a_pair(self, rig):
        """A copy B that jammed part-way is not a pair, even though the queue
        drained and a job file exists.
        """
        cups, queue = self.wired(rig)
        rig.set_failure("truncate", after=4, until=5)
        result = unattended.run(cups, settings=self.sequence_settings(),
                                queue=queue, log=lambda *_: None,
                                sleep=time.sleep)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        got = dict(rig.printed())
        assert result == 1, f"a jammed copy B returned 0; got {rig.titles()}"
        assert "OTP A" in got and "OTP B" in got
        assert len(got["OTP B"]) < len(got["OTP A"]), \
            "the rig recorded copy B whole; the jam was not modelled"

    def test_a_disconnected_printer_is_not_called_a_pair(self, rig):
        cups, queue = self.wired(rig)
        rig.set_failure("disconnect", after=4)
        result = unattended.run(cups, settings=self.sequence_settings(),
                                queue=queue, log=lambda *_: None,
                                sleep=time.sleep)
        assert result == 1
        assert not cups.devices(), "the printer is still being advertised"

    def test_a_healthy_run_still_reports_the_pair_it_printed(self, rig):
        """The control: a healthy run reports its pair. A unit that called
        everything a failure would pass every other test here.
        """
        cups, queue = self.wired(rig)
        result = unattended.run(cups, settings=self.sequence_settings(),
                                queue=queue, log=lambda *_: None,
                                sleep=time.sleep)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        assert result == 0, rig.titles()
        final = dict(rig.printed())["WHAT TO DO NOW"]
        assert "YOU NOW HAVE A PAD PAIR" in sheet_text(final)


@needs_cups
class TestThePanelAgainstAFailingPrinter:
    """The interactive path against a failing printer: the real RunJob state
    machine and the real daemon. Under abort-job a failed job drains the queue,
    so RunJob must consult printer_fault and not advance on `active_jobs == 0`
    alone.
    """

    def panel(self, rig, tmp_path, codeword="RUSTED-BADGER"):
        from otpunit import jobs, ui
        from otpunit.hw.buttons import FakeButtons
        from otpunit.hw.display import FakeDisplay

        cups = rig.cups()
        queue = cups.ensure_queue(cups.devices()[0])
        settings = config.Settings(pages=2)
        app = ui.App(display=FakeDisplay(), buttons=FakeButtons([]),
                     cups=cups, settings=settings,
                     config_path=str(tmp_path / "otp-unit.conf"),
                     poll_seconds=0)
        app.queue = queue
        screen = ui.RunJob(jobs.JobSpec(jobs.JobKind.PAD_PAIR, codeword,
                                        settings))
        return app, screen, cups, queue

    def advance(self, app, screen, cups, queue):
        """OK once the tray is clear, exactly as the footer instructs."""
        from otpunit.hw.buttons import Press

        assert settle(cups, queue, seconds=60), "the queue never drained"
        return screen.press(app, Press.OK)

    def drive_a_pair(self, rig, tmp_path):
        """confirm -> copy A -> swap -> copy B -> whatever comes next."""
        from otpunit.hw.buttons import Press

        app, screen, cups, queue = self.panel(rig, tmp_path)
        screen.press(app, Press.OK)              # confirm -> generate + copy A
        assert screen.stage == "printing", screen.stage
        self.advance(app, screen, cups, queue)   # copy A landed?
        first = screen.stage
        if first == "swap":
            screen.press(app, Press.OK)          # -> copy B
            self.advance(app, screen, cups, queue)
        return app, screen, first

    def test_a_healthy_pair_still_reaches_pair_complete(self, rig, tmp_path):
        # The control comes first: a panel that called every job a failure
        # would satisfy every other test in this class.
        app, screen, after_a = self.drive_a_pair(rig, tmp_path)
        assert after_a == "swap", after_a
        assert screen.stage == "done", screen.error
        assert "PAIR COMPLETE" in "\n".join(screen.frame(app).rendered())

    def test_a_failed_copy_a_does_not_say_copy_a_done(self, rig, tmp_path):
        rig.set_failure("media-empty")
        app, screen, after_a = self.drive_a_pair(rig, tmp_path)
        assert after_a == "error", \
            f"the panel said {after_a!r} over a tray that got nothing"
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "REMOVE THE STACK" not in shown
        # The printer's own words: the prose in printer-state-message is the
        # channel that decides (see Cups.ADVISORY_STEMS). Either wording is
        # acceptable.
        assert "OUT OF PAPER" in shown or "MEDIA EMPTY" in shown, shown

    def test_a_failed_copy_b_does_not_say_pair_complete(self, rig, tmp_path):
        # Copy A prints; copy B does not.
        rig.set_failure("media-empty", after=1)
        app, screen, after_a = self.drive_a_pair(rig, tmp_path)
        assert after_a == "swap", after_a
        assert screen.stage == "error", \
            "the panel called a lost copy B a complete pair"
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "PAIR COMPLETE" not in shown
        assert "DESTROY PRINTED PAGES" in shown, shown

    def test_a_stopped_printer_leaves_the_panel_waiting_not_finished(self, rig, tmp_path):
        # STOP keeps the job, so the queue never empties and the panel must sit
        # on `waiting`, which has a confirmed way out.
        from otpunit.hw.buttons import Press

        rig.set_failure("stop")
        app, screen, cups, queue = self.panel(rig, tmp_path)
        screen.press(app, Press.OK)
        assert screen.stage == "printing"
        # Waits for the STOP to land, not for the job to appear: a merely
        # spooled job already gives active_jobs == 1. Waits on the IPP reason
        # "paused", since the header's "disabled" is localised.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if cups.queue_stopped(queue):
                break
            time.sleep(0.2)
        assert cups.queue_stopped(queue), \
            "the queue never stopped; this test would prove nothing"

        screen.press(app, Press.OK)
        assert screen.stage == "waiting", screen.stage
        assert not screen.queue_unknown, \
            "a stopped queue answered; that is not the unknown case"
        # And it must say why: a stopped queue is not STILL PRINTING.
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "STILL PRINTING" not in shown, shown
        # The reason, not lpstat's "disabled since <date>" header.
        assert "MEDIA EMPTY" in shown, shown
        assert not any(char.isdigit() for char in shown.replace("COPY A", "")), \
            f"a timestamp is on the panel where a reason should be: {shown}"

    def test_an_unanswerable_queue_is_still_not_treated_as_a_fault(self, rig, tmp_path):
        """When cupsd cannot be asked at all, `confirm_continue` stays available
        and no fault is reported: it is the operator's only non-destructive way
        on.
        """
        from otpunit.hw.buttons import Press

        app, screen, cups, queue = self.panel(rig, tmp_path)
        screen.press(app, Press.OK)
        assert settle(cups, queue, seconds=60), "the queue never drained"
        rig.stop()                               # the daemon goes away
        screen.press(app, Press.OK)
        assert screen.stage == "waiting" and screen.queue_unknown
        screen.press(app, Press.DOWN)
        assert screen.stage == "confirm_continue"
        screen.press(app, Press.OK)
        assert screen.stage == "swap", screen.stage


# --- the panel, against the kernel rather than a double ------------------


@pytest.fixture
def identity():
    """This process, and only this process, as a Pi Zero 2 W. Without a board
    revision gpiozero builds no local pin factory (see pirig.py).
    """
    pytest.importorskip("gpiozero")
    why = pirig.available()
    if why:
        pytest.skip(why)

    forged = pirig.PiIdentity()
    try:
        forged.__enter__()
    except OSError as exc:
        # Only "this host will not let us" is a skip; any other errno means the
        # forgery went wrong.
        if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOSYS):
            raise
        pytest.skip(f"this host would not give us a mount namespace: {exc}")
    # Entered by hand, not with `with`, so an OSError from the test body cannot
    # become a skip.
    try:
        yield forged
    finally:
        forged.__exit__(None, None, None)


def cpuinfo_revision():
    """Whatever Revision /proc/cpuinfo is currently offering, or None."""
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("Revision"):
            return line.split(":", 1)[1].strip()
    return None


class TestTheFakePiIdentity:
    """The forgery itself, which needs root but no gpiochip. A revision that
    decodes to another Pi puts the GPIOs on a header that board lacks, and a
    bind mount that escapes the namespace makes the whole machine claim to be a
    Pi.
    """

    def test_the_forged_cpuinfo_does_not_contradict_itself(self, identity):
        """Every field of the forged cpuinfo agrees with REVISION, including the
        Model and Serial that diagnostics.py reads for the status sheet.
        """
        from gpiozero.pins.pi import PiBoardInfo

        planted = diagnostics._cpuinfo_field("Revision")
        assert planted == f"{pirig.REVISION:06x}", (
            f"the forged file says revision {planted}, not "
            f"{pirig.REVISION:06x}")
        assert (PiBoardInfo.from_revision(int(planted, 16)).model
                == pirig.MODEL)
        assert "Zero 2 W" in diagnostics._cpuinfo_field("Model"), (
            f"the Model line says {diagnostics._cpuinfo_field('Model')!r} "
            f"while the revision decodes to a {pirig.MODEL}; the status "
            f"sheet and gpiozero would report different boards")
        assert diagnostics._cpuinfo_field("Serial") == "00000000f1e2d3c4"

    def test_the_forged_board_carries_the_pins_the_unit_uses(self):
        # No identity fixture: PiBoardInfo.from_revision is a pure function, so
        # this needs no root.
        pytest.importorskip("gpiozero")
        from gpiozero.pins.pi import PiBoardInfo

        from otpunit.hw.buttons import PIN_DOWN, PIN_OK, PIN_UP

        board = PiBoardInfo.from_revision(pirig.REVISION)
        assert board.model == pirig.MODEL, board
        for pin in (PIN_UP, PIN_DOWN, PIN_OK):
            headers = sorted(header.name
                             for header, _ in board.find_pin(f"GPIO{pin}"))
            assert headers == ["J8"], (
                f"the forged board puts GPIO{pin} on {headers or 'no header'}"
                f"; the unit's three switches are wired to J8")

    def test_the_forgery_does_not_reach_the_host(self, identity):
        assert identity.host_sees_a_pi() is False, (
            "the bind mount propagated out of the namespace: every process "
            "on this machine now reads a Pi revision from /proc/cpuinfo")

    def test_teardown_leaves_nothing_mounted(self):
        """After teardown the mount table is exactly what it was. The process
        stays in its private namespace, and the rest of the harness runs in it,
        so a surviving bind would have diagnostics.py reporting a Pi.
        """
        pytest.importorskip("gpiozero")
        why = pirig.available()
        if why:
            pytest.skip(why)

        before_mounts = pirig.mount_points()
        before_revision = cpuinfo_revision()

        with pirig.PiIdentity():
            assert str(pirig.CPUINFO_PATH) in pirig.mount_points(), (
                "nothing got mounted, so this is about to prove that "
                "unmounting nothing leaves nothing behind")
            assert cpuinfo_revision() == f"{pirig.REVISION:06x}"

        assert pirig.mount_points() == before_mounts
        assert cpuinfo_revision() == before_revision

    def test_a_umount_that_fails_still_gives_back_the_fd_and_the_files(self):
        """A failing umount still raises, but the namespace descriptor is closed,
        the temp files removed and the path kept for a retry.

        The unmountable path is planted at the front of the list, so the real
        binds come down first.
        """
        pytest.importorskip("gpiozero")
        why = pirig.available()
        if why:
            pytest.skip(why)

        before_mounts = pirig.mount_points()
        forged = pirig.PiIdentity()
        try:
            forged.__enter__()
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOSYS):
                raise
            pytest.skip(f"this host would not give us a mount namespace: {exc}")

        never_mounted = "/tmp/otp-pirig-was-never-a-mount-point"
        forged.forged.insert(0, never_mounted)
        temporary = list(forged._temporary)
        assert temporary and all(os.path.exists(p) for p in temporary)

        with pytest.raises(OSError):
            forged.__exit__(None, None, None)

        assert pirig.mount_points() == before_mounts, (
            "the real binds did not come down, so this test has left the "
            "process reading a forged /proc/cpuinfo")
        assert forged._home is None, (
            "the namespace descriptor is still open; _home is cleared "
            "only after os.close() returns")
        assert not any(os.path.exists(p) for p in temporary), (
            f"{[p for p in temporary if os.path.exists(p)]} left in /tmp")
        assert forged.forged == [never_mounted], (
            "the path that would not unmount is not in the list any "
            "more, so nothing can retry it or say what is still mounted")


class TestTheFactoryTeardownBetweenPanels:
    """What happens to gpiozero's pin cache when one panel closes; needs no
    gpiochip.

    gpiozero shares one pin dict across every LocalPiFactory, keyed by a
    PinInfo that compares equal across factories, and close() empties it only
    if every pin.close() returned. One raising close() hands the next factory a
    pin on a dead handle, which never fires.
    """

    @pytest.fixture
    def gz(self):
        """A LocalPiFactory subclass with no kernel behind it."""
        pytest.importorskip("gpiozero")
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory
        from gpiozero.pins.pi import PiPin

        class Pin(PiPin):
            broken = False
            deaf = False
            # lgpio's callback list in miniature: appended to when a pin claims
            # its alert, pruned only by an explicit cancel.
            armed = []
            order = None

            # Override _enable/_disable_event_detect, not _set_when_changed, so
            # gpiozero's own guard in PiPin._set_when_changed stays in the code
            # path.
            def _enable_event_detect(self):
                type(self).armed.append(self.info.name)

            def _disable_event_detect(self):
                if type(self).deaf:
                    # Like LGPIOPin, cancel the callback first and only then
                    # re-claim the line, which is where it can raise.
                    type(self).armed.remove(self.info.name)
                    raise RuntimeError(
                        "gpio_claim_input: could not re-claim the line")
                type(self).armed.remove(self.info.name)
                if type(self).order is not None:
                    type(self).order.append(self.info.name)

            def close(self):
                if type(self).broken:
                    # The shape of the real failure: LGPIOPin.close ->
                    # when_changed = None -> _disable_event_detect ->
                    # gpio_get_mode(_handle).
                    raise TypeError("unsupported operand type(s) for &: "
                                    "'NoneType' and 'int'")

            def _get_function(self):
                return "input"

            def _set_function(self, value):
                pass

            def _get_state(self):
                return 0

        class Factory(LocalPiFactory):
            def __init__(self):
                super().__init__()
                self.pin_class = Pin
                # Stands in for LGPIOFactory._handle; None means gpiochip_close
                # has run.
                self._handle = object()

            def close(self):
                super().close()
                self._handle = None

            def _get_revision(self):
                return pirig.REVISION

        was = Device.pin_factory
        try:
            yield Factory, Pin
        finally:
            Pin.broken = False
            Pin.deaf = False
            Pin.order = None
            LocalPiFactory.pins.clear()
            LocalPiFactory._reservations.clear()
            Device.pin_factory = was

    @staticmethod
    def listening(factory, *numbers):
        """Pins of `factory` with an edge listener attached, plus the strong
        references that keep the listeners alive: gpiozero holds `when_changed`
        weakly, and release_gpiozero calls gc.collect().
        """
        listeners = []
        for number in numbers:
            def listener(ticks, state):          # noqa: ARG001
                raise AssertionError("no edge should reach this fake")
            factory.pin(number).when_changed = listener
            listeners.append(listener)
        return listeners

    def test_the_pin_cache_really_is_shared_between_factories(self, gz):
        # The premise: if gpiozero ever gives each factory its own dict, delete
        # everything below.
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, _ = gz
        Device.pin_factory = first = Factory()
        assert first.pins is LocalPiFactory.pins

    def test_a_raising_close_hands_the_next_factory_a_dead_pin(self, gz):
        """Without the cleanup, a raising close() leaves the next factory a stale
        pin.
        """
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, Pin = gz
        Device.pin_factory = first = Factory()
        pin = first.pin(5)

        Pin.broken = True
        with pytest.raises(TypeError):
            first.close()
        assert LocalPiFactory.pins, (
            "close() cleared the cache despite raising, so the rest of "
            "this test is describing something that cannot happen")

        Device.pin_factory = Factory()
        assert Device.pin_factory.pin(5) is pin, (
            "the second factory built a fresh pin, so a stale one could "
            "not be inherited and release_gpiozero need not clear it")
        assert pirig.stale_pins() == ["GPIO5"]

    def test_release_gpiozero_empties_the_cache_even_when_close_raises(
            self, gz):
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, Pin = gz
        Device.pin_factory = Factory()
        Device.pin_factory.pin(5)
        Pin.broken = True

        # The error propagates, and the cache is cleaned up regardless.
        with pytest.raises(TypeError):
            pirig.release_gpiozero()
        assert LocalPiFactory.pins == {}

    def test_every_edge_callback_is_cancelled_before_the_handle_closes(
            self, gz):
        """The callback list is empty after release, and emptied before close().
        lgpio dispatches edges from one unguarded process-wide thread, so a
        callback left armed on a closed factory kills every button.

        Asserted on the list the event-detect hooks maintain, so gpiozero's own
        decision about whether `when_changed = None` cancels anything stays
        under test.
        """
        from gpiozero import Device

        Factory, Pin = gz
        Device.pin_factory = factory = Factory()
        listening = self.listening(factory, 5, 6, 13)
        assert sorted(Pin.armed) == ["GPIO13", "GPIO5", "GPIO6"], (
            f"the panel never armed anything, so an empty list at the "
            f"end would prove nothing; got {Pin.armed}")

        order = []
        Pin.order = order
        closing = factory.close
        factory.close = lambda: (order.append("close"), closing())

        pirig.release_gpiozero()

        assert Pin.armed == [], (
            f"{Pin.armed} still armed on a factory that has closed. The "
            f"next edge matching one of them raises inside lgpio's "
            f"notification thread, which has no guard around its "
            f"dispatch, and takes every button in the process with it")
        assert order[-1] == "close", (
            f"close() must come last; got {order}")
        assert sorted(order[:-1]) == ["GPIO13", "GPIO5", "GPIO6"], (
            f"every pin of the factory must be silenced first; got {order}")
        assert len(listening) == 3                # kept alive to here

    def test_the_chip_handle_closes_even_when_a_pin_cannot_be_silenced(
            self, gz):
        """When a pin cannot be silenced the error is raised and the factory is
        closed anyway; otherwise its lines stay claimed by an unreachable
        handle and the next Button(5) fails "GPIO busy".

        The fake raises after dropping the callback, as LGPIOPin does.
        """
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, Pin = gz
        Device.pin_factory = factory = Factory()
        listening = self.listening(factory, 5, 6)
        assert factory._handle is not None
        Pin.deaf = True

        # A pin that would not be silenced fails the teardown.
        with pytest.raises(RuntimeError, match="could not cancel edge"):
            pirig.release_gpiozero()

        assert factory._handle is None, (
            "the gpiochip handle never closed, so this process is "
            "holding lines nothing can release for the rest of its life")
        assert LocalPiFactory.pins == {}
        assert len(listening) == 2                # kept alive to here

    def test_an_unreachable_device_is_finalised_before_the_factory_closes(
            self, gz):
        """Unreachable Devices are collected before the factory closes, so their
        finalizers do not reach a closed handle during a later test. A
        reference cycle stands in for the Device, since only gc can free one.
        """
        from gpiozero import Device

        Factory, _ = gz
        Device.pin_factory = factory = Factory()
        factory.pin(5)
        when = []

        class Ghost:
            def __del__(self):
                when.append("after close" if factory.gone else "while open")

        factory.gone = False
        closing = factory.close
        factory.close = lambda: (setattr(factory, "gone", True), closing())

        ghost = Ghost()
        ghost.cycle = ghost                      # only gc can free this
        del ghost

        pirig.release_gpiozero()
        assert when == ["while open"], (
            f"the finalizer ran {when or ['not at all']}; it must run "
            f"while the chip handle is still valid, or a real "
            f"Device.__del__ dereferences a None handle and leaves the "
            f"shared pin cache dirty for the next panel")

    def test_a_clean_teardown_leaves_no_stale_pin_and_does_not_raise(
            self, gz):
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, _ = gz
        Device.pin_factory = Factory()
        Device.pin_factory.pin(5)
        # A live factory's own pins are not stale.
        assert pirig.stale_pins() == []

        pirig.release_gpiozero()
        assert LocalPiFactory.pins == {}


@needs_sim("gpio-chip", "gpio-sim")
class TestTheRealButtonPath:
    """gpiozero -> lgpio -> /dev/gpiochipN, with a kernel on the other end. OK and
    BACK mean opposite things while a job prints, so tap-versus-hold must
    resolve correctly.
    """

    def line(self, pin):
        return Path(sim("gpio-control")) / f"sim_gpio{pin}" / "pull"

    def value(self, pin):
        """What the kernel says the line reads, right now."""
        node = Path(sim("gpio-control")) / f"sim_gpio{pin}" / "value"
        try:
            return node.read_text().strip()
        except OSError as exc:
            return f"unreadable({errno.errorcode.get(exc.errno, exc.errno)})"

    def press(self, pin, seconds=0.05):
        pull = self.line(pin)
        # Sampled either side of each write, so a missing press can be
        # attributed to the line not moving or to the edge not reaching a
        # callback.
        self.trace = [f"gpio{pin} at rest={self.value(pin)}"]
        pull.write_text("pull-down")             # button to ground
        self.trace.append(f"grounded={self.value(pin)}")
        time.sleep(seconds)
        pull.write_text("pull-up")               # released
        self.trace.append(f"released={self.value(pin)}")
        time.sleep(0.1)
        self.trace.append(f"settled={self.value(pin)}")

    def why(self, pin):
        """The state that explains a missing press: whether the line moved,
        whether lgpio's notification thread is alive, and how many callbacks
        are registered.
        """
        trace = " -> ".join(getattr(self, "trace", ["no trace recorded"]))
        try:
            import lgpio
            notify = lgpio._notify_thread
            alert = (f"notify thread alive={notify.is_alive()} "
                     f"go={notify.go} "
                     f"callbacks={len(notify.callbacks)} "
                     f"chips={sorted({c.chip for c in notify.callbacks})}")
        except Exception as exc:                 # pragma: no cover
            alert = f"could not read lgpio's notify thread: {exc!r}"
        return (f"no press arrived.\n  line: {trace}\n  lgpio: {alert}\n"
                f"  stale pins: {pirig.stale_pins()}")

    def glitch(self, pin):
        """Bounce the line as briefly as this machine can and return the duration.
        Only a bounce shorter than BOUNCE_SECONDS tests the debounce.
        """
        pull = self.line(pin)
        started = time.monotonic()
        pull.write_text("pull-down")
        pull.write_text("pull-up")
        return time.monotonic() - started

    @pytest.fixture
    def buttons(self, identity):
        """The shipped GpioButtons, bound to the chip gpio-sim made.

        Asserts, not skips, that the panel is on the simulated chip with no
        stale pins or leftover callbacks. lgpio creates its `.lgd-nfy0` FIFO in
        the cwd at import, long before this fixture runs, so no chdir here
        could move it.
        """
        pytest.importorskip("lgpio")
        from otpunit.hw import buttons as buttons_mod

        # Nothing may be armed before this panel arms anything: a leftover
        # callback belongs to a closed factory, and the next matching edge
        # kills lgpio's notification thread.
        armed = pirig.edge_callbacks()
        assert armed == 0, (
            f"{armed} edge callbacks are still armed from an earlier "
            f"panel. See pirig.silence: the next edge to match one of "
            f"them raises inside lgpio's notification thread, which has "
            f"no guard around its dispatch, and every press after that "
            f"is lost with nothing in the log but a warning.")

        chip = int(sim("gpio-chip"))
        pirig.bind_gpiozero_to(chip)
        try:
            panel = buttons_mod.GpioButtons()
        except BaseException:
            pirig.release_gpiozero()
            raise

        wanted = {f"/dev/gpiochip{chip}"}
        opened = pirig.open_gpiochips()
        try:
            assert opened == wanted, (
                f"the panel is talking to {opened or 'no gpiochip at all'} "
                f"rather than {wanted}, the chip gpio-sim just made; every "
                f"assertion below would be about the wrong hardware")
            # Which factory the pins belong to, not just which chip is open: a
            # stale pin claims its alert on a dead handle and no press arrives.
            stale = pirig.stale_pins()
            assert not stale, (
                f"the panel is holding pins from an earlier factory: "
                f"{stale}. Their alerts are claimed on a handle that is "
                f"now closed, so every press below would time out and "
                f"this class would report a panel that never fires. See "
                f"pirig.release_gpiozero for why the cache can survive.")
            # The notification thread must be alive, or a press and a broken
            # panel look identical.
            thread = pirig.notify_thread()
            assert thread is not None and thread.is_alive(), (
                "lgpio's notification thread is dead, so no edge can "
                "reach any callback in this process. Every press test "
                "below would fail as a timeout, saying nothing about "
                "why. See pirig.silence.")
            yield panel
        finally:
            panel.close()
            pirig.release_gpiozero()

    def test_gpiozero_finds_the_simulated_chip_unaided(self, identity):
        """With the identity alone and no factory named, gpiozero's own selection
        reaches lgpio and the simulated chip, which is how the shipped code
        runs.
        """
        pytest.importorskip("lgpio")
        from gpiozero import Device
        from gpiozero.pins.lgpio import LGPIOFactory

        chip = int(sim("gpio-chip"))
        if chip != 0:
            # Not a defect: lgpio addresses chips by number and gpio-sim takes
            # whichever is free. The fixture pins the factory for this case.
            pytest.skip(
                f"gpio-sim landed at gpiochip{chip}, and for a Zero 2 W "
                f"gpiozero's unaided default opens gpiochip0")

        factory = Device._default_pin_factory()
        try:
            assert isinstance(factory, LGPIOFactory), (
                f"gpiozero settled on {type(factory).__name__}; only the "
                f"lgpio factory talks to a gpiochip")
            assert factory.chip == chip
        finally:
            factory.close()

    def test_a_tap_on_up_arrives_as_up(self, buttons):
        from otpunit.hw.buttons import PIN_UP, Press

        self.press(PIN_UP)
        assert buttons.wait(timeout=2) is Press.UP, self.why(PIN_UP)

    def test_a_tap_on_down_arrives_as_down(self, buttons):
        """A tap on the DOWN line arrives as DOWN. Catches a swapped pin number,
        since DOWN is distinguishable only by which line was grounded.
        """
        from otpunit.hw.buttons import PIN_DOWN, Press

        self.press(PIN_DOWN)
        assert buttons.wait(timeout=2) is Press.DOWN, self.why(PIN_DOWN)

    def test_a_tap_on_ok_arrives_as_ok_not_back(self, buttons):
        from otpunit.hw.buttons import PIN_OK, Press

        self.press(PIN_OK, seconds=0.05)
        assert buttons.wait(timeout=2) is Press.OK, self.why(PIN_OK)

    def test_a_hold_on_ok_arrives_as_back_exactly_once(self, buttons):
        """A hold on OK produces exactly one event, BACK, and never OK as well.
        """
        from otpunit.hw.buttons import HOLD_SECONDS, PIN_OK, Press

        self.press(PIN_OK, seconds=HOLD_SECONDS + 0.3)
        assert buttons.wait(timeout=3) is Press.BACK, self.why(PIN_OK)
        assert buttons.wait(timeout=0.5) is None, "one press, two events"

    def test_a_bounce_too_short_to_be_a_press_is_not_one(self, buttons):
        """A bounce shorter than BOUNCE_SECONDS produces no press, proving the
        window reaches the kernel driver. A phantom BACK while a job prints
        purges the spool.

        A real press on the same line comes first, so "no press arrived" cannot
        be passed by a dead panel.
        """
        from otpunit.hw.buttons import BOUNCE_SECONDS, PIN_UP, Press

        # Asserted: a window of zero is a defect in the panel, not a reason to
        # skip.
        assert BOUNCE_SECONDS > 0, "the panel has no debounce window left"

        # The control: same panel, line and wait(), differing only in how long
        # the line is held.
        self.press(PIN_UP)
        assert buttons.wait(timeout=2) is Press.UP, (
            "a full-length press did not arrive either, so this panel "
            "cannot say anything about the debounce window.\n  "
            + self.why(PIN_UP))

        measured = self.glitch(PIN_UP)
        if measured >= BOUNCE_SECONDS:
            pytest.skip(
                f"this machine took {measured * 1000:.1f}ms to bounce the "
                f"line, which is longer than the {BOUNCE_SECONDS * 1000:.0f}"
                f"ms window -- that is a press, not a glitch")
        assert buttons.wait(timeout=0.5) is None, (
            f"a {measured * 1000:.1f}ms glitch arrived as a press through a "
            f"{BOUNCE_SECONDS * 1000:.0f}ms debounce window")

    def test_a_raising_callback_does_not_kill_the_panel(self, buttons):
        """A callback that raises costs one edge, not every later one. lgpio
        dispatches all edges from one unguarded singleton thread, so without
        GpioButtons._guarded a MemoryError there would kill the panel until a
        power cycle.

        Here the edge, gpiozero and lgpio are all real; the fast tier covers
        the same property with stand-ins.
        """
        from otpunit.hw.buttons import PIN_DOWN, PIN_UP, Press

        # Control first: this line delivers presses at all.
        self.press(PIN_UP)
        assert buttons.wait(timeout=2) is Press.UP, (
            "a press did not arrive before anything was made to fail, so "
            "this test cannot say what the guard did.\n  " + self.why(PIN_UP))

        starved = MemoryStarvedQueue()
        starved.failures = 1
        healthy, buttons._events = buttons._events, starved
        try:
            self.press(PIN_UP)
            # Wait for the exception to have happened: restoring the queue
            # under a late edge would put an UP ahead of the DOWN below.
            deadline = time.monotonic() + 3
            while buttons.dropped == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            assert buttons.dropped == 1, (
                "the press meant to raise was never counted as lost. "
                "Either the edge did not reach the shipped callback, in "
                "which case nothing here was tested, or there is no guard "
                "in front of it to do the counting -- and then the "
                "exception went into lgpio's dispatch thread and this "
                "panel is already dead.\n  " + self.why(PIN_UP))
        finally:
            buttons._events = healthy

        thread = pirig.notify_thread()
        assert thread is not None and thread.is_alive(), (
            "lgpio's notification thread died on a callback exception. "
            "Every button in this process is now dead for the life of the "
            "process, and on the unit that is a power cycle -- which "
            "discards the pad in progress. See GpioButtons._guarded.")

        # A later press on a different line still arrives.
        self.press(PIN_DOWN)
        assert buttons.wait(timeout=2) is Press.DOWN, (
            "no press arrived after a callback raised, which is what a "
            "dead notification thread looks like.\n  " + self.why(PIN_DOWN))
        assert buttons.wait(timeout=0.5) is None, "one press, one event"


@needs_sim("i2c-bus", "i2c-stub")
class TestTheRealI2CPath:
    def test_the_scan_finds_the_display_address(self):
        """_i2c_scan finds the display at 0x3c on a real SMBus; the status sheet
        relies on it to say whether the panel is wired up.
        """
        pytest.importorskip("smbus2")
        found = diagnostics._i2c_scan(bus=int(sim("i2c-bus")))
        # Lowercased on both sides: upper() would turn the x of 0x3c into X.
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
        """screen_connected() reads a DRM status file a kernel wrote. It does not
        trust isatty, because the service binds tty1 whether or not a monitor
        is attached.
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
    """The rig's directives come from device/install.sh; otherwise every test
    above would be testing CUPS defaults.
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

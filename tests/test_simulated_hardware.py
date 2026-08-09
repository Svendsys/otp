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


# --- printers that fail, which is most printers --------------------------


def sheet_text(data: bytes) -> str:
    """The text of a sheet the backend recorded, uppercased for matching.

    The Generic PDF Printer filter chain passes the PDF through unaltered
    behind a PJL header, so what reached "paper" can be read back.
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
    """
    The guard on the guard, and it has to come first.

    For its first several rounds the rig's backend recorded its bytes and
    exited 0, so the only failure the harness could produce was one flag's
    worth of "out of paper" and everything else was untested. Each mode
    below is asserted to be observably DIFFERENT from a clean print --
    because a mode that quietly degrades to success turns every test after
    it into a check that cannot fail, which is the exact defect this
    repository keeps rediscovering.

    The three shapes matter as much as the count. abort-job DRAINS the
    queue; STOP disables it and keeps the job; RETRY leaves it pending.
    Code that reads "the queue is empty" as "it printed" passes the first
    and fails the other two, so a harness carrying only one of them cannot
    tell a correct implementation from that bug.
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
        # Waits for the BACKEND to have run and cupsd to have recorded the
        # outcome -- NOT for the queue to drain. Two of these modes never
        # drain, and waiting for something that cannot happen would cost
        # the suite a minute per mode and teach nobody anything.
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if rig.backend_invocations() and (
                    cups.printer_fault(queue) is not None
                    or cups.active_jobs(queue) == 0):
                break
            time.sleep(0.2)
        time.sleep(0.5)
        return {
            "drained": cups.active_jobs(queue) == 0,
            "fault": cups.printer_fault(queue),
            "devices": len(cups.devices()),
            "invocations": rig.backend_invocations(),
            "printed": rig.printed(),
        }

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
        # printer_fault special-cases "disabled" on the header line, which
        # is a different code path from a state-reason on a later line.
        # STOP is the only mode that reaches it.
        assert "disabled" in (self.observe(rig, "stop")["fault"] or "")

    def test_truncate_records_a_short_job_rather_than_no_job(self, rig):
        """
        "Some pages printed" is the state an operator most needs the truth
        about, and a rig that recorded nothing could not tell it from
        "nothing printed". Measured inside one rig so the comparison is
        against this filter chain's own output rather than a guess.
        """
        whole = self.observe(rig)["printed"]
        assert len(whole) == 1 and whole[0][1]
        partial = self.observe(rig, "truncate")["printed"]
        assert len(partial) == 2, "the second job was never recorded"
        assert 0 < len(partial[1][1]) < len(whole[0][1])

    def test_a_transient_that_clears_is_not_left_looking_like_a_fault(self, rig):
        """
        The other half of failure injection: a rig that could only fail
        permanently would make "the printer recovered" untestable, and a
        false FAILURE has its own cost -- it sends someone to burn a pair
        that printed perfectly.
        """
        seen = self.observe(rig, "retry-current", until=2)
        assert seen["invocations"] == 3, "cupsd did not retry the job"
        assert seen["drained"] and not seen["fault"], seen

    def test_an_unknown_mode_is_refused_rather_than_ignored(self, rig):
        # A typo in a mode name must not read as a healthy printer, which
        # is how a test that asserts a failure ends up asserting nothing.
        with pytest.raises(ValueError):
            rig.set_failure("out-of-paper")
        with pytest.raises(ValueError):
            rig.set_failure("retry-current")     # unbounded; would spin

    def test_refilling_the_tray_plugs_the_printer_back_in(self, rig):
        # A leftover `.unplugged` would make every later mode in the same
        # rig look like a disconnect -- a fixture that lies.
        assert self.observe(rig, "disconnect")["devices"] == 0
        rig.set_failure("")
        assert rig.plugged_in()
        assert self.observe(rig)["devices"] == 1


@needs_cups
class TestAFailedCopyIsNeverReportedAsAPair:
    """
    The asymmetry that makes this P1: a false failure wastes paper, a
    false success destroys key material the operator believes is on a page
    in their hand.

    The sequence is fixed and known -- status, tabula, copy A, separator,
    copy B, final sheet -- so `after=4 until=5` fails exactly copy B and
    leaves the sheet that has to explain it printable. That is the state
    most likely to be reported wrongly, because a queue that printed one
    of two copies drains exactly like a queue that printed both.
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
        """
        drain() returning True is what authorises the purge and the "pair
        printed" claim. A stopped queue keeps the job, so True here would
        be a lie -- and the timeout path is the one that must be taken.
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
        settle(cups, queue, seconds=60)
        titles = rig.titles()
        assert result == 1, f"a run that lost copy B returned 0; got {titles}"
        assert "OTP A" in titles, titles

    def test_the_final_sheet_says_what_did_and_did_not_reach_paper(self, rig):
        """
        The operator is holding copy A of a pair that can never be
        completed. The sheet has to say that, name what the printer said,
        and not repeat the DONE sheet's promise that the two copies are
        identical.
        """
        cups, queue = self.wired(rig)
        rig.set_failure("media-empty", after=4, until=5)
        unattended.run(cups, settings=self.sequence_settings(), queue=queue,
                       log=lambda *_: None, sleep=time.sleep)
        settle(cups, queue, seconds=60)
        final = [data for title, data in rig.printed()
                 if title == "WHAT TO DO NOW"]
        assert final, f"no final sheet reached paper; got {rig.titles()}"
        text = sheet_text(final[-1])
        assert "NOT USABLE" in text, text[:400]
        assert "DESTROY" in text
        # The printer knew why. A sheet that only says "something went
        # wrong" ends nothing.
        assert "OUT OF PAPER" in text, text[:400]
        # And it must not carry the DONE sheet's claim.
        assert "THEY ARE IDENTICAL" not in text
        assert "YOU NOW HAVE A PAD PAIR" not in text

    def test_a_partial_copy_b_is_not_called_a_pair(self, rig):
        """
        Bytes reached the printer and then it jammed. The queue drains and
        a job file exists, so both of the cheap proxies for "it printed"
        say yes.
        """
        cups, queue = self.wired(rig)
        rig.set_failure("truncate", after=4, until=5)
        result = unattended.run(cups, settings=self.sequence_settings(),
                                queue=queue, log=lambda *_: None,
                                sleep=time.sleep)
        settle(cups, queue, seconds=60)
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
        """
        The control. Every assertion above is satisfied by a unit that
        calls everything a failure, and that unit would be useless.
        """
        cups, queue = self.wired(rig)
        result = unattended.run(cups, settings=self.sequence_settings(),
                                queue=queue, log=lambda *_: None,
                                sleep=time.sleep)
        settle(cups, queue, seconds=60)
        assert result == 0, rig.titles()
        final = dict(rig.printed())["WHAT TO DO NOW"]
        assert "YOU NOW HAVE A PAD PAIR" in sheet_text(final)


@needs_cups
class TestThePanelAgainstAFailingPrinter:
    """
    The same question asked of the INTERACTIVE path, which had no answer.

    unattended.run has consulted printer_fault since the empty-tray
    incident. RunJob never did: it advanced on `active_jobs == 0` alone,
    and under the shipped ErrorPolicy abort-job a failed job drains the
    queue exactly as fast as a printed one. So the panel showed COPY A
    DONE over an empty tray, then PAIR COMPLETE, and wiped the key -- the
    identical defect, in the path an operator with a panel actually uses.

    These drive the real RunJob state machine against the real daemon.
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

        settle(cups, queue, seconds=60)
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
        # The control, and it comes first: a panel that called every job a
        # failure would satisfy every other test in this class.
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
        assert "OUT OF PAPER" in shown, shown

    def test_a_failed_copy_b_does_not_say_pair_complete(self, rig, tmp_path):
        # Copy A prints; copy B does not. The operator is holding half a
        # pair and the panel used to congratulate them on it.
        rig.set_failure("media-empty", after=1)
        app, screen, after_a = self.drive_a_pair(rig, tmp_path)
        assert after_a == "swap", after_a
        assert screen.stage == "error", \
            "the panel called a lost copy B a complete pair"
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "PAIR COMPLETE" not in shown
        assert "DESTROY PRINTED PAGES" in shown, shown

    def test_a_stopped_printer_leaves_the_panel_waiting_not_finished(self, rig, tmp_path):
        # STOP keeps the job, so the queue never empties. The panel must
        # sit on `waiting` -- which has a confirmed way out -- rather than
        # declare anything.
        from otpunit.hw.buttons import Press

        rig.set_failure("stop")
        app, screen, cups, queue = self.panel(rig, tmp_path)
        screen.press(app, Press.OK)
        assert screen.stage == "printing"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and cups.active_jobs(queue) != 1:
            time.sleep(0.2)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting", screen.stage
        assert not screen.queue_unknown, \
            "a stopped queue answered; that is not the unknown case"
        # And it must say WHY. A stopped queue keeps its job for ever, so
        # "STILL PRINTING / OK TO CHECK AGAIN" is not a slightly optimistic
        # reading of the situation, it is false -- the review panel was
        # right that the first version of this test blessed that screen as
        # intended behaviour.
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "STILL PRINTING" not in shown, shown
        assert "DISABLED" in shown or "TRAY OPEN" in shown, shown

    def test_an_unanswerable_queue_is_still_not_treated_as_a_fault(self, rig, tmp_path):
        """
        The override this fix must not break. When cupsd cannot be asked at
        all, `confirm_continue` is the operator's only non-destructive way
        on -- and a daemon that cannot answer active_jobs cannot report a
        fault either, so routing that path through the fault check would
        cost them the exit and leave live key material with nowhere to go.
        """
        from otpunit.hw.buttons import Press

        app, screen, cups, queue = self.panel(rig, tmp_path)
        screen.press(app, Press.OK)
        settle(cups, queue, seconds=60)
        rig.stop()                               # the daemon goes away
        screen.press(app, Press.OK)
        assert screen.stage == "waiting" and screen.queue_unknown
        screen.press(app, Press.DOWN)
        assert screen.stage == "confirm_continue"
        screen.press(app, Press.OK)
        assert screen.stage == "swap", screen.stage


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

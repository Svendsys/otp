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

None of that needs a Raspberry Pi. One thing does need to look like one:
gpiozero will not open any gpiochip on a machine with no board revision,
so the panel is driven inside a mount namespace where /proc/cpuinfo says
Pi Zero 2 W. That forgery is the only substitution here, it lasts as long
as the test, and pirig.py documents every part of it -- including how it
is kept off the host.

Anything not available is skipped with a reason rather than failed -- a
kernel without `vkms` should still get the other four.

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
        assert settle(cups, queue, seconds=60), "the queue never drained"
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
        """
        The SD card is the part most likely to be captured with the unit,
        so the rule is that a live pad's name never reaches anything CUPS
        writes: not job.cache, not printers.conf, not a log.
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
        # Waits for the BACKEND to have run and the picture to STOP MOVING
        # -- NOT for the queue to drain. Two of these modes never drain, and
        # waiting for something that cannot happen would cost the suite a
        # minute per mode and teach nobody anything.
        #
        # Stability, rather than the old "printer_fault is not None". That
        # condition fired on cupsd's mid-print filter chatter -- "DEBUG:
        # cfFilterChain: universal (PID 21339) exited with no errors." is a
        # printer-state-message on a perfectly healthy queue -- so observe()
        # returned while the backend was still running. For retry-current
        # that meant breaking after invocation 1 of 3 and then asserting on
        # a count that had not happened yet; for truncate it could land
        # between the backend writing $OUT.data and the head -c that
        # shortens it, so the "partial" file read back at full length. Both
        # are CI flakes whose failure mode is "the mode quietly degraded to
        # success", which is what this class exists to rule out.
        #
        # The printed sizes are part of the sample precisely because of
        # truncate: a file still being rewritten is the picture moving.
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
        """
        SHAPES must keep pace with FAILURE_MODES, or a mode added later is
        one the docstring above claims to cover and does not.

        `retry-current` is the standing exception and cannot have a shape:
        set_failure refuses it without `until`, and with `until` the fault
        CLEARS, so by design it ends up looking exactly like a clean print.
        What distinguishes it is the invocation count, which
        test_a_transient_that_clears_is_not_left_looking_like_a_fault
        asserts instead.
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
        assert settle(cups, queue, seconds=60), "the queue never drained"
        titles = rig.titles()
        assert result == 1, f"a run that lost copy B returned 0; got {titles}"
        # BOTH, and this is the point: `after=4 until=5` is only aimed at
        # copy B if the sequence is the six sheets it is documented to be.
        # Moving the window one earlier kills the separator instead, copy B
        # is never submitted at all, and `result == 1` plus "OTP A" is
        # still satisfied -- a green test for a different failure. Found by
        # the review panel.
        assert titles == ["OTP status", "TABULA RECTA", "OTP A",
                          "REMOVE COPY A", "OTP B", "WHAT TO DO NOW"], titles

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
        assert settle(cups, queue, seconds=60), "the queue never drained"
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
        """
        The control. Every assertion above is satisfied by a unit that
        calls everything a failure, and that unit would be useless.
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
        # The printer's own words, which is what the panel shows: the prose
        # in printer-state-message is the channel that DECIDES (see
        # Cups.ADVISORY_STEMS -- the usb backend reports no IPP reason at
        # any severity), and it is also the more actionable of the two.
        # Either wording is acceptable here; what must not happen is a
        # drained queue reading as a printed copy.
        assert "OUT OF PAPER" in shown or "MEDIA EMPTY" in shown, shown

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
        # Waits for the STOP to LAND, not for the job to appear. A merely
        # spooled job already satisfies active_jobs == 1, so waiting on
        # that exited 8ms after submit -- before the backend had run --
        # and the test passed with the `stop` branch replaced by `exit 0`.
        # Found by the review panel; the first CI run then failed on the
        # other side of the same race, with a transient filter DEBUG line
        # standing in for the fault.
        # Waits on the IPP reason, not the header text. "paused" is a
        # keyword; "disabled" is _cupsLangPrintf output and only English.
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
        # And it must say WHY. A stopped queue keeps its job for ever, so
        # "STILL PRINTING / OK TO CHECK AGAIN" is not a slightly optimistic
        # reading of the situation, it is false -- the review panel was
        # right that the first version of this test blessed that screen as
        # intended behaviour.
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "STILL PRINTING" not in shown, shown
        # The REASON. The previous assertion was `"DISABLED" in shown or
        # "TRAY OPEN" in shown`, and against this same real cupsd it passed
        # on the first disjunct alone -- because printer_fault returned
        # lpstat's header, "printer OTP disabled since Wed Aug 12 20:21:35
        # 2026 -", whose reason is on the NEXT line and was discarded. The
        # panel read "PRINTER OTP DISABLED / SINCE WED AUG 12 20>": a name
        # and a truncated date, on the one screen whose entire job is
        # telling a stranded operator what is wrong.
        assert "MEDIA EMPTY" in shown, shown
        assert not any(char.isdigit() for char in shown.replace("COPY A", "")), \
            f"a timestamp is on the panel where a reason should be: {shown}"

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
    """
    This process, and only this process, as a Pi Zero 2 W.

    Both classes below stand on it: with no board revision anywhere,
    gpiozero declines to build a local pin factory at all, so the real
    gpiozero -> lgpio -> kernel path could not even be entered. pirig.py
    documents what is forged, in what order gpiozero reads it, and why it
    happens inside a mount namespace.
    """
    pytest.importorskip("gpiozero")
    why = pirig.available()
    if why:
        pytest.skip(why)

    forged = pirig.PiIdentity()
    try:
        forged.__enter__()
    except OSError as exc:
        # Only "this host will not let us" is a skip. A container can
        # hand out root and still refuse CLONE_NEWNS, and that is worth
        # naming rather than failing over -- but every other errno means
        # the forgery went wrong, and a skip would bury it. Measured: a
        # teardown that leaves the previous bind in place makes the next
        # one fail ENOENT, which arrived here as a confident "no mount
        # namespaces on this host".
        if exc.errno not in (errno.EPERM, errno.EACCES, errno.ENOSYS):
            raise
        pytest.skip(f"this host would not give us a mount namespace: {exc}")
    # Entered by hand rather than with a `with`, because only the setup
    # may turn an OSError into a skip. Wrapping the yield too would catch
    # one raised by the test body and skip on a genuine failure.
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
    """
    The forgery itself, which needs root but no gpiochip.

    Two of the ways it can go wrong are silent. A revision that decodes to
    some other Pi puts the unit's GPIOs on a header that board does not
    have: measured, 0x2 gets `PinInvalidPin: GPIO5 is not a valid pin
    name`, which reads like a bug in this repository and is really three
    digits in a fake file. And a bind mount that escapes the namespace
    leaves the whole machine answering "Raspberry Pi" to every process
    that asks, for as long as the box is up.
    """

    def test_the_forged_cpuinfo_does_not_contradict_itself(self, identity):
        """
        Every field of the forgery, not only the one gpiozero reads.

        This used to assert `get_pi_revision() == pirig.REVISION`, which
        `PiIdentity.__enter__` has already asserted by the time the
        fixture yields -- so the test could only ever ERROR in setup and
        had no way to fail on its own name.

        What nothing checked is the rest of the file. diagnostics.py
        pulls Model and Serial out of /proc/cpuinfo for the status
        sheet, and the comment above pirig.CPUINFO says those fields are
        there so the fake cannot answer one question and contradict
        itself on the next two. Changing REVISION without changing the
        Model line is exactly how that would happen, and this is what
        would notice.
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
        # No identity fixture: PiBoardInfo.from_revision is a pure
        # function of a constant, so the mount namespace it used to take
        # made this test need root for nothing. The importorskip comes
        # with it, since that was the fixture's too.
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
        """
        The half of the teardown contract that has to hold every time.

        Leaving does not return to the namespace we came from: the kernel
        refuses that once the process has threads, and lgpio gives it one
        permanently. pirig.py states the whole contract and the
        measurement behind it. What that leaves behind is a private copy
        of the host's mount table, so "the forgery is gone" has to mean
        the mount table is exactly what it was -- not merely that
        /proc/cpuinfo reads plausibly again.

        The rest of the harness runs in this same process. A bind mount
        surviving teardown would have diagnostics.py reporting a Pi this
        machine is not, in tests with no idea any of this ever happened.
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
        """
        One failure used to become three.

        __exit__ deliberately does not swallow a umount error -- a forged
        /proc/cpuinfo this process cannot get rid of has to be loud. But
        the raise used to take the rest of the method with it: the
        /proc/self/ns/mnt descriptor stayed open for the life of the
        process and every /tmp/otp-pirig-* file stayed on disk, neither
        of which the failed umount had anything to do with. The pop was
        wrong the same way -- it discarded the path before the call that
        needed it, so nothing could name what was still mounted or try
        again.

        An unmountable path is planted at the FRONT of the list, so the
        real binds are taken down first and this test cannot leave a
        forged /proc/cpuinfo behind for the rest of the run.
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
    """
    What happens to gpiozero's pin cache when one panel closes.

    No gpiochip needed, which is the point: this ran nowhere until run
    31699840801 failed, and it is the half of the button path that a
    machine without gpio-sim can still hold to account. There the first
    panel worked and every panel after it received nothing -- three
    timeouts, plus a debounce test that "passed" because all it asserts
    is that no press arrives.

    gpiozero shares ONE pin dict across every LocalPiFactory instance (a
    deliberate guard against two back-ends driving one pin), keyed by a
    PinInfo that compares equal across factories. PiFactory.close()
    empties it only after every pin.close() has returned. So one raising
    close() -- and a Device finalizer reaching a closed handle raises
    exactly that -- hands the next factory a pin belonging to the last
    one, which claims its alert on a dead handle and never fires.
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
            # lgpio's callback list, in miniature. The real one is a
            # module-level singleton's `callbacks`, appended to when a
            # pin claims its alert and pruned by nothing but an explicit
            # cancel; pirig.edge_callbacks() reads it and the buttons
            # fixture asserts it is empty before each panel. Here the
            # same list, driven by the same two gpiozero hooks.
            armed = []
            order = None

            # _enable/_disable_event_detect, NOT _set_when_changed.
            # PiPin._set_when_changed is where gpiozero's own guard
            # lives -- `if value is None: if self._when_changed is not
            # None: self._disable_event_detect()` -- and a fake that
            # replaces it wholesale takes that guard out of the test
            # while leaving the test's name unchanged. Overriding one
            # level down keeps the shipped code path in play: silence()
            # sets the property, gpiozero decides whether that means a
            # cancel, and this list records what it decided.
            def _enable_event_detect(self):
                type(self).armed.append(self.info.name)

            def _disable_event_detect(self):
                if type(self).deaf:
                    # LGPIOPin's does the dangerous half FIRST -- cancel
                    # the lgpio callback -- and only then re-claims the
                    # line as an input, which is where it can throw. So
                    # a raise here does not mean the callback survived,
                    # and it must not cost the handle its close().
                    type(self).armed.remove(self.info.name)
                    raise RuntimeError(
                        "gpio_claim_input: could not re-claim the line")
                type(self).armed.remove(self.info.name)
                if type(self).order is not None:
                    type(self).order.append(self.info.name)

            def close(self):
                if type(self).broken:
                    # The shape of the real one, from that run:
                    # LGPIOPin.close -> when_changed = None ->
                    # _disable_event_detect -> gpio_get_mode(_handle).
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
                # Stands in for LGPIOFactory._handle, the gpiochip
                # descriptor. None means gpiochip_close has run; while
                # it is not None the lines this factory claimed are
                # still claimed and no other factory can have them.
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
        """
        Pins of `factory` with an edge listener attached, and the strong
        references that keep them attached.

        gpiozero holds `when_changed` by weak reference on purpose -- a
        pin must not keep the Button alive -- so a listener nothing else
        refers to can be collected before the assertion that needs it.
        release_gpiozero calls gc.collect(); the returned list is what
        stops that emptying the fixture out from under the test.
        """
        listeners = []
        for number in numbers:
            def listener(ticks, state):          # noqa: ARG001
                raise AssertionError("no edge should reach this fake")
            factory.pin(number).when_changed = listener
            listeners.append(listener)
        return listeners

    def test_the_pin_cache_really_is_shared_between_factories(self, gz):
        # The premise. If gpiozero ever gives each factory its own dict,
        # everything below stops meaning anything and should be deleted
        # rather than left passing.
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, _ = gz
        Device.pin_factory = first = Factory()
        assert first.pins is LocalPiFactory.pins

    def test_a_raising_close_hands_the_next_factory_a_dead_pin(self, gz):
        """The defect itself, reproduced without a gpiochip."""
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

        # Loud, not swallowed -- and cleaned up regardless, so the next
        # panel starts from nothing whatever this one did.
        with pytest.raises(TypeError):
            pirig.release_gpiozero()
        assert LocalPiFactory.pins == {}
        # Nothing about Device.pin_factory or stale_pins() here. The
        # first is set to None by release_gpiozero's opening statement
        # whatever follows, and the second reads the dict the line above
        # has just been asserted empty -- two assertions that could not
        # have failed however this function was broken.

    def test_every_edge_callback_is_cancelled_before_the_handle_closes(
            self, gz):
        """
        The defect that actually made the panel work exactly once.

        lgpio dispatches edges from one process-wide thread with no
        try/except around the call. A callback left armed on a closed
        factory raises there on the next matching edge, the thread dies,
        and every button in the process goes with it -- reported as a
        warning in the summary and as three timeouts that say nothing.

        So each pin must be told to stop listening while its handle is
        still open. `when_changed = None` is what cancels the lgpio
        callback; this asserts the callback list is empty afterwards,
        and that it emptied BEFORE close().

        The list is what makes this test able to fail. Asking the pins
        whether they were told is not the same question: gpiozero
        decides, inside `PiPin._set_when_changed`, whether a `None`
        means anything at all -- it cancels only `if self._when_changed
        is not None` -- and a fake that answers for that method proves
        the harness called a setter, not that a callback went away.
        Measured: with `pin._when_changed = None` inserted immediately
        before `pin.when_changed = None` in silence(), which cancels
        nothing whatever on a real LGPIOPin, the earlier version of this
        test passed.
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
        """
        The other order that matters: cancel first, close ANYWAY.

        Every other broken case in this class breaks close(). This one
        breaks the step before it, which the sequential version of
        release_gpiozero handled by never reaching close() at all: no
        pin.close(), no gpiochip_close, `_handle` still set -- and the
        cache cleared regardless by the finally, so the lines stayed
        claimed by a factory nothing in the process could reach. The
        next Button(5) then dies "GPIO busy", and the callbacks that
        could not be cancelled are still in lgpio's notification thread
        waiting to kill it, which is the outcome the RuntimeError exists
        to prevent rather than to cause.

        Note where the fake raises: _disable_event_detect drops the
        callback BEFORE it re-claims the line, exactly as LGPIOPin does,
        so this failure is one where the cancel already succeeded.
        """
        from gpiozero import Device
        from gpiozero.pins.local import LocalPiFactory

        Factory, Pin = gz
        Device.pin_factory = factory = Factory()
        listening = self.listening(factory, 5, 6)
        assert factory._handle is not None
        Pin.deaf = True

        # Loud: a pin that would not be silenced is worth a red teardown.
        with pytest.raises(RuntimeError, match="could not cancel edge"):
            pirig.release_gpiozero()

        assert factory._handle is None, (
            "the gpiochip handle never closed, so this process is "
            "holding lines nothing can release for the rest of its life")
        assert LocalPiFactory.pins == {}
        assert len(listening) == 2                # kept alive to here

    def test_an_unreachable_device_is_finalised_before_the_factory_closes(
            self, gz):
        """
        The collect, and its ORDER -- the half a cache assertion misses.

        A gpiozero Device that is unreachable but not yet collected runs
        `Device.__del__` -> `close()` -> the pin -> `factory._handle` at
        whatever later moment the collector picks. Landing after the
        factory closed is what raised the TypeError, from a finalizer,
        in the middle of the next test. Collecting first is what makes
        it harmless. A reference cycle stands in for the real Device
        here because refcounting would free a plain object too early to
        prove anything about gc.collect() at all.
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
        # A live factory's own pins are not stale; only the assertion
        # about OTHER factories may fire.
        assert pirig.stale_pins() == []

        pirig.release_gpiozero()
        assert LocalPiFactory.pins == {}


@needs_sim("gpio-chip", "gpio-sim")
class TestTheRealButtonPath:
    """
    gpiozero -> lgpio -> /dev/gpiochipN, with a kernel on the other end.

    The tap-versus-hold discrimination is the part worth running for real:
    OK and BACK mean opposite things while a job is printing, so a press
    that resolves the wrong way can purge the spool and destroy a pair.
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
        # Sampled either side of each write. "No press arrived" has two
        # very different causes -- the line never moved, or it moved and
        # the edge did not reach a callback -- and they need opposite
        # fixes. Without this the failure cannot tell them apart, which
        # is what made run 31699840801 cost three rounds of guessing.
        self.trace = [f"gpio{pin} at rest={self.value(pin)}"]
        pull.write_text("pull-down")             # button to ground
        self.trace.append(f"grounded={self.value(pin)}")
        time.sleep(seconds)
        pull.write_text("pull-up")               # released
        self.trace.append(f"released={self.value(pin)}")
        time.sleep(0.1)
        self.trace.append(f"settled={self.value(pin)}")

    def why(self, pin):
        """
        The state a missing press needs explaining by.

        Whether the line moved, whether lgpio's notification thread is
        still running (it is a module-level singleton started at import,
        and if its FIFO ever reaches EOF it spins forever delivering
        nothing), and how many callbacks are registered on it.
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
        """
        Bounce the line as briefly as this machine can, and time it.

        The duration is returned rather than assumed: what makes a glitch
        a glitch is being shorter than BOUNCE_SECONDS, and a loaded box
        that took longer than that has not tested the debounce -- it has
        delivered a short press, which is a different thing entirely.
        """
        pull = self.line(pin)
        started = time.monotonic()
        pull.write_text("pull-down")
        pull.write_text("pull-up")
        return time.monotonic() - started

    @pytest.fixture
    def buttons(self, identity):
        """
        The shipped GpioButtons, bound to the chip gpio-sim made.

        This used to ask which chip the process had opened and skip when
        it was not ours, because off a Pi gpiozero would not construct a
        factory at all. With a board identity it does, so the same
        question is now an assertion: a panel wired to some other
        controller must fail loudly rather than quietly prove nothing.

        There is no chdir here any more, and no way to put one back that
        would do anything. lgpio opens ONE FIFO, `.lgd-nfy0`, and it does
        it in `_callback_thread.__init__` (lgpio.py:504) -- which runs at
        `import lgpio`, in whatever directory the interpreter is in at
        the time. Not two, not on the first callback: nothing else in
        lgpio calls notify_open at all. By the time this fixture runs the
        import has long since happened, from diagnostics.py's version
        string during the CUPS tests, and the importorskip above would
        beat any chdir to it regardless. So the FIFO really is left in
        the repository root after a harness run -- measured, from a clean
        checkout on a kernel with no gpio-sim, where this fixture never
        even executes. `git status` does not show it because it is a
        FIFO rather than a regular file, which is why it went unnoticed.
        A genuinely read-only checkout therefore breaks at the import,
        not at the panel, and nothing this fixture does can change that.
        """
        pytest.importorskip("lgpio")
        from otpunit.hw import buttons as buttons_mod

        # Nothing may be armed before this panel arms anything. lgpio's
        # notification thread is a process-wide singleton whose callback
        # list only grows -- measured at 5, then 8, then 11 as successive
        # panels were built, three more each time and never fewer. (Three
        # panels registering three each would be nine, so 5 was not the
        # first panel's; what was observed is the growth.) A leftover
        # callback belongs to a closed factory, and the next edge
        # matching it kills the thread, and with it every button in the
        # process.
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
            # Which factory the pins belong to, not just which chip is
            # open. A pin cached from an earlier, closed factory claims
            # its alert on a dead handle: the panel looks healthy, the
            # chip is right, and no press ever arrives. That is how run
            # 31699840801 read -- three timeouts and a debounce test
            # passing because it asserts nothing arrives.
            stale = pirig.stale_pins()
            assert not stale, (
                f"the panel is holding pins from an earlier factory: "
                f"{stale}. Their alerts are claimed on a handle that is "
                f"now closed, so every press below would time out and "
                f"this class would report a panel that never fires. See "
                f"pirig.release_gpiozero for why the cache can survive.")
            # The thread that has to still be running for any of the
            # tests below to mean anything. It dies silently -- a
            # warning in the summary, nothing in the failing test -- and
            # once it has, a press and a broken panel look identical.
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
        """
        The identity alone, with nobody naming a factory or a chip.

        That is how the shipped code runs: `GpioButtons` asks gpiozero for
        a `Button` and takes whatever pin factory it settled on. Worth
        knowing that a board revision is the only thing that path was
        missing, and worth failing if gpiozero's own selection stops
        reaching lgpio.
        """
        pytest.importorskip("lgpio")
        from gpiozero import Device
        from gpiozero.pins.lgpio import LGPIOFactory

        chip = int(sim("gpio-chip"))
        if chip != 0:
            # Not a defect, and not something to assert around: lgpio
            # addresses chips by NUMBER and gpio-sim takes whichever the
            # kernel had free. The fixture below pins the factory for
            # exactly this case; here there is nothing left to prove.
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
        """
        The third switch, on its own line.

        Cheap, and it is the one that would catch a pin number swapped in
        the constructor -- UP and OK are distinguishable by their events,
        DOWN only by which line was grounded.
        """
        from otpunit.hw.buttons import PIN_DOWN, Press

        self.press(PIN_DOWN)
        assert buttons.wait(timeout=2) is Press.DOWN, self.why(PIN_DOWN)

    def test_a_tap_on_ok_arrives_as_ok_not_back(self, buttons):
        from otpunit.hw.buttons import PIN_OK, Press

        self.press(PIN_OK, seconds=0.05)
        assert buttons.wait(timeout=2) is Press.OK, self.why(PIN_OK)

    def test_a_hold_on_ok_arrives_as_back_exactly_once(self, buttons):
        """
        One press must produce one event. The rejected design -- when_held
        setting a flag that when_released reads -- races across two
        gpiozero threads and can emit OK *and* BACK for a single press.
        """
        from otpunit.hw.buttons import HOLD_SECONDS, PIN_OK, Press

        self.press(PIN_OK, seconds=HOLD_SECONDS + 0.3)
        assert buttons.wait(timeout=3) is Press.BACK, self.why(PIN_OK)
        assert buttons.wait(timeout=0.5) is None, "one press, two events"

    def test_a_bounce_too_short_to_be_a_press_is_not_one(self, buttons):
        """
        BOUNCE_SECONDS exists because a switch closing is not one clean
        edge. Nothing has ever checked that the number reaches the kernel
        driver: gpiozero hands it to lgpio, and every other test in this
        repository drives the panel from above that.

        A stuck contact on OK is the expensive version of this -- a
        phantom BACK while a job prints purges the spool.

        The one assertion this test is named for is negative, and a
        negative assertion about a panel is what a dead panel passes.
        That is not hypothetical here: run 31699840801 had every button
        silently receiving nothing, and this test went green through all
        of it while its three neighbours timed out. So the glitch is
        preceded by a real press on the same line of the same panel, and
        "no press arrived" only counts once one has.
        """
        from otpunit.hw.buttons import BOUNCE_SECONDS, PIN_UP, Press

        # Asserted, not skipped over. A window of zero is a defect in the
        # panel, not a fact about this machine, and without this the test
        # for it would quietly report "could not produce a short enough
        # glitch" -- measured, with BOUNCE_SECONDS set to 0.
        assert BOUNCE_SECONDS > 0, "the panel has no debounce window left"

        # The control. Same panel, same GPIO, same wait() -- so the only
        # difference between it and the assertion below is how long the
        # line was held.
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

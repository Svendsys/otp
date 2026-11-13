"""Tests for producing pads with no panel and no buttons. They cover what the
operator cannot see or correct: the sequence printing in the wrong order, and
the unit ignoring the only abort it offers.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otpunit import config, printer, unattended

DEVICE = printer.Device("usb://HP/LaserJet?serial=1", "HP LaserJet")


class Cups:
    def __init__(self, script=None):
        self.script = list(script) if script is not None else None
        self.submitted = []

    def devices(self):
        if self.script is None:
            return [DEVICE]
        return self.script.pop(0) if self.script else [DEVICE]

    def ensure_queue(self, device, name="OTP"):
        return name

    def submit(self, data, name="OTP", title="OTP", options=None):
        self.submitted.append({"title": title, "data": bytes(data)})
        return f"job-{len(self.submitted)}"

    def active_jobs(self, name="OTP"):
        # An idle queue. Tests that model a spool override this.
        return 0

    def purge(self, name="OTP"):
        pass

    def titles(self):
        return [job["title"] for job in self.submitted]


class Buttons:
    """Presses on demand; None means nothing is bridging the pins."""

    def __init__(self, press_after=None):
        self.press_after = press_after
        self.polls = 0

    def wait(self, timeout=0):
        self.polls += 1
        if self.press_after is not None and self.polls >= self.press_after:
            from otpunit.hw.buttons import Press

            return Press.OK
        return None

    def close(self):
        pass


def settings(**kwargs):
    base = dict(pages=2, auto_delay=3, auto_swap_delay=2)
    base.update(kwargs)
    return config.Settings(**base)


def run(cups, **kwargs):
    kwargs.setdefault("settings", settings())
    kwargs.setdefault("log", lambda *_: None)
    kwargs.setdefault("sleep", lambda s: None)
    return unattended.run(cups, **kwargs)


class TestTheSequence:
    def test_it_prints_a_complete_usable_set_in_order(self, monkeypatch):
        # Stand in for the manual so ordering can be checked without the
        # rendered asset.
        monkeypatch.setattr(unattended.jobs, "manual_available", lambda: True)
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        cups = Cups()
        assert run(cups) == 0
        # Status first so the countdown is readable before the wait; the MANUAL
        # ahead of the pads so that if paper runs out the instructions survive;
        # then the card, copy A, the tray break, copy B.
        assert cups.titles() == ["OTP status", "MANUAL", "TABULA RECTA",
                                 "OTP A", "REMOVE COPY A", "OTP B",
                                 "WHAT TO DO NOW"]

    def test_the_manual_can_be_turned_off(self, monkeypatch):
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        cups = Cups()
        run(cups, settings=settings(auto_manual=False))
        assert "MANUAL" not in cups.titles()

    def test_a_missing_manual_does_not_stop_the_pads(self, monkeypatch):
        # The rendered asset is absent on a hand-provisioned unit.
        import otpunit.jobs as jobs_mod

        real = jobs_mod.generate

        def flaky(spec, *args, **kwargs):
            if spec.kind is jobs_mod.JobKind.MANUAL:
                raise FileNotFoundError("/opt/otp-unit/assets/...")
            return real(spec, *args, **kwargs)

        monkeypatch.setattr(unattended.jobs, "generate", flaky)
        cups = Cups()
        assert run(cups) == 0
        assert "OTP A" in cups.titles() and "OTP B" in cups.titles()

    def test_the_manual_is_two_up_on_a4_to_halve_the_paper(self):
        assert unattended._manual_options(config.Settings(paper="A4")) == {
            "media": "A4", "number-up": "2"}

    def test_even_one_up_the_manual_still_names_its_media(self):
        # A6 cannot take two A5 pages, but the manual must still be submitted
        # with a size.
        assert unattended._manual_options(config.Settings(paper="A6")) == {
            "media": "A6"}


class TestThePaperEstimate:
    """The sheet count the status sheet quotes is right: running out of paper
    mid-pair loses the pair, not just the paper.
    """

    def test_it_counts_both_copies_and_the_imposition(self):
        # 100 pad pages, four to a sheet, twice, plus the fixed sheets.
        plain = unattended.sheets_needed(
            config.Settings(pages=100, auto_manual=False))
        assert plain == 25 * 2 + 4

    def test_the_manual_is_included_when_it_will_be_printed(self):
        with_manual = unattended.sheets_needed(config.Settings(pages=100))
        without = unattended.sheets_needed(
            config.Settings(pages=100, auto_manual=False))
        assert with_manual - without == (unattended.MANUAL_PAGES + 1) // 2

    def test_unimposed_paper_costs_four_times_as_much(self):
        a4 = unattended.sheets_needed(
            config.Settings(pages=100, paper="A4", auto_manual=False))
        a6 = unattended.sheets_needed(
            config.Settings(pages=100, paper="A6", auto_manual=False))
        assert a6 > a4 * 3

    def test_a7_counts_two_pad_pages_to_a_sheet(self):
        """A7 pages print two to an A6 sheet, so 100 pages cost 50 sheets a copy.
        An overestimate misleads someone checking they have enough paper.
        """
        a7 = config.Settings(pages=100, a7=True, auto_manual=False)
        assert a7.sheets == 50
        assert unattended.sheets_needed(a7) == 50 * 2 + 4

    def test_a7_quotes_the_media_that_comes_out_of_the_tray(self):
        # A7 lays out on A6 sheets whatever `paper` says.
        lines = " ".join(unattended._plan(
            config.Settings(pages=10, a7=True, paper="A4")))
        assert "sheets of A6" in lines

    def test_the_estimate_reaches_the_status_sheet(self):
        lines = " ".join(unattended._plan(config.Settings()))
        assert "sheets" in lines and "PAPER" in lines

    def test_the_two_copies_are_byte_identical(self):
        # This is what makes them a pair; generated once, submitted twice.
        cups = Cups()
        run(cups)
        jobs = {job["title"]: job["data"] for job in cups.submitted}
        assert jobs["OTP A"] == jobs["OTP B"]
        assert jobs["OTP A"].startswith(b"%PDF")

    def test_the_codeword_never_appears_in_a_job_title(self):
        cups = Cups()
        run(cups, settings=settings(auto_codeword="SILENT-OSPREY"))
        assert not any("OSPREY" in title for title in cups.titles())

    def test_a_configured_codeword_is_the_one_printed(self, monkeypatch):
        # Page streams are compressed, so assert on the spec the job was built
        # from.
        seen = {}
        real = unattended.jobs.PadPairJob

        class Spy(real):
            def __init__(self, spec, cups, queue="OTP"):
                seen["codeword"] = spec.codeword
                super().__init__(spec, cups, queue)

        monkeypatch.setattr(unattended.jobs, "PadPairJob", Spy)
        run(Cups(), settings=settings(auto_codeword="SILENT-OSPREY"))
        assert seen["codeword"] == "SILENT-OSPREY"

    def test_an_unset_codeword_is_rolled_not_left_blank(self, monkeypatch):
        seen = {}
        real = unattended.jobs.PadPairJob

        class Spy(real):
            def __init__(self, spec, cups, queue="OTP"):
                seen["codeword"] = spec.codeword
                super().__init__(spec, cups, queue)

        monkeypatch.setattr(unattended.jobs, "PadPairJob", Spy)
        run(Cups(), settings=settings())
        assert "-" in seen["codeword"], "expected a MODIFIER-NOUN codeword"

    def test_auto_print_off_stops_after_the_status_sheet(self):
        cups = Cups()
        assert run(cups, settings=settings(auto_print=False)) == 1
        assert cups.titles() == ["OTP status"]

    def test_a_tabula_failure_does_not_cost_the_pads(self, monkeypatch):
        # The card is useful, not essential: losing it must not lose the pads.
        import otpunit.jobs as jobs_mod

        real = jobs_mod.generate

        def flaky(spec, *args, **kwargs):
            if spec.kind is jobs_mod.JobKind.TABULA:
                raise RuntimeError("no tabula for you")
            return real(spec, *args, **kwargs)

        monkeypatch.setattr(unattended.jobs, "generate", flaky)
        cups = Cups()
        assert run(cups) == 0
        assert "OTP A" in cups.titles() and "OTP B" in cups.titles()
        assert "TABULA RECTA" not in cups.titles()


class TestUnpluggingIsTheAbort:
    """Pulling the printer cable stops the unit: it is the one control that needs
    no parts, and the only way to say no without a display.
    """

    def test_unplugging_during_the_wait_prints_no_pad(self):
        # Status sheet, then the printer goes away before the delay ends.
        cups = Cups(script=[[DEVICE]] + [[]] * 8)
        assert run(cups) == 1
        assert cups.titles() == ["OTP status"]

    def test_a_transient_lookup_failure_does_not_abort(self):
        class Flaky(Cups):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def devices(self):
                self.calls += 1
                if self.calls == 2:
                    raise OSError("cupsd busy")
                return [DEVICE]

        cups = Flaky()
        assert run(cups) == 0, "a hiccup must not throw away a pad"

    def test_countdown_reports_a_sustained_disconnect(self):
        cups = Cups(script=[[]] * unattended.GONE_AFTER)
        with pytest.raises(unattended.Aborted):
            unattended.countdown(cups, seconds=50, sleep=lambda s: None,
                                 log=lambda *_: None)

    def test_one_empty_poll_does_not_abandon_the_pad(self):
        # devices() returns [] for a busy cupsd too, so one empty poll is not a
        # disconnect.
        cups = Cups(script=[[DEVICE], [], [DEVICE], [], [DEVICE]])
        assert unattended.countdown(cups, seconds=5, sleep=lambda s: None,
                                    log=lambda *_: None) == "elapsed"


class TestAButtonMeansPrintNow:
    """A press means stop waiting. A button is the one part someone can improvise:
    a wire across pin 33 and ground.
    """

    def test_a_press_ends_the_wait_early(self):
        cups = Cups()
        buttons = Buttons(press_after=1)
        assert unattended.countdown(cups, seconds=10_000, buttons=buttons,
                                    sleep=lambda s: None,
                                    log=lambda *_: None) == "pressed"

    def test_no_button_still_prints_eventually(self):
        cups = Cups()
        assert unattended.countdown(cups, seconds=2, buttons=None,
                                    sleep=lambda s: None,
                                    log=lambda *_: None) == "elapsed"

    def test_a_broken_button_layer_is_not_fatal(self):
        # An unexportable pin or a missing lgpio degrades to "no button".
        class Broken:
            def wait(self, timeout=0):
                raise OSError(121, "Remote I/O error")

        cups = Cups()
        assert unattended.countdown(cups, seconds=1, buttons=Broken(),
                                    sleep=lambda s: None,
                                    log=lambda *_: None) == "elapsed"

    def test_opening_buttons_never_raises(self):
        """hmi.open_buttons answers "no button" on a unit with no gpiozero,
        gpiochip or permissions, so a missing button never stops the pads.
        """
        from otpunit import hmi

        assert not hasattr(unattended, "open_buttons")
        result, kind = hmi.open_buttons()
        assert isinstance(kind, str)
        if result is not None:
            result.close()

    def test_a_press_waiter_on_nothing_is_simply_false(self):
        assert unattended._press_waiter(None)() is False


class TestTheSheetsThatStandInForAPanel:
    def test_the_swap_sheet_says_to_take_copy_a_out(self):
        data = bytes(unattended.sheet(unattended.SWAP, codeword="X-Y",
                                      settings=settings(), seconds=90))
        assert data.startswith(b"%PDF")

    def test_no_sheet_over_key_material_claims_to_be_harmless(self, monkeypatch):
        """Only the status sheet carries the "no key material" heading; a sheet
        lying on secret pages must not, or live key material gets binned.

        Asserted on the heading each sheet asks for, since reportlab's page
        streams are not greppable.
        """
        seen = []
        real = unattended.diagnostics.render_bytes

        def spy(sections, page_size=None, **heading):
            seen.append(heading)
            return real(sections, page_size, **heading)

        monkeypatch.setattr(unattended.diagnostics, "render_bytes", spy)
        for kind in (unattended.SWAP, unattended.DONE):
            seen.clear()
            unattended.sheet(kind, codeword="X-Y", settings=settings())
            heading = seen[-1]
            blurb = f"{heading.get('title', '')} {heading.get('subtitle', '')}"
            assert "no key material" not in blurb, kind
            assert "STATUS SHEET" not in blurb, kind
            assert blurb.strip(), f"{kind} must set its own heading"

    def test_the_swap_sheet_heading_says_the_pages_are_secret(self, monkeypatch):
        seen = {}
        real = unattended.diagnostics.render_bytes
        monkeypatch.setattr(unattended.diagnostics, "render_bytes",
                            lambda sec, ps=None, **h: (seen.update(h),
                                                       real(sec, ps, **h))[1])
        unattended.sheet(unattended.SWAP, codeword="X-Y", settings=settings())
        assert "secret" in seen["subtitle"].lower()

    def test_the_status_sheet_still_says_it_is_harmless(self):
        # The converse: the reassurance belongs on the sheet that is safe to
        # photograph.
        from otpunit import diagnostics

        assert "no key material" in diagnostics.STATUS_SUBTITLE

    def test_the_plan_names_the_abort_and_the_shortcut(self):
        lines = " ".join(unattended._plan(settings()))
        assert "unplug" in lines.lower()
        assert "pin 33" in lines
        assert "otp-unit.conf" in lines

    def test_the_plan_says_so_when_auto_print_is_off(self):
        lines = " ".join(unattended._plan(settings(auto_print=False)))
        assert "auto_print" in lines


class TestEverySheetAsksForTheRightPaper:
    def test_the_instruction_sheets_carry_the_media_size(self):
        # A LETTER unit must ask the queue for Letter.
        class Recording(Cups):
            def __init__(self):
                super().__init__()
                self.options = []

            def submit(self, data, name="OTP", title="OTP", options=None):
                self.options.append((title, options))
                return super().submit(data, name, title, options)

        cups = Recording()
        run(cups, settings=settings(paper="LETTER"))
        for title, options in cups.options:
            assert options, f"{title} was submitted with no options"
            assert "media" in options, title

    def test_a6_sheets_are_rendered_a6_not_a4(self):
        from otpunit import diagnostics
        from reportlab.lib.pagesizes import A6

        assert diagnostics._page_size(config.Settings(paper="A6")) == A6


class TestCopyBActuallyReachesPaper:
    """The unattended path waits for the queue to drain before purging the spool.
    lp returns once a job is spooled, and `cancel -x -a` cancels the job that
    is printing, so purging straight after submit would cancel copy B.
    """

    class Spool(Cups):
        """Models lp returning on spool, with work still outstanding."""

        def __init__(self, drain_after=2, stuck=False):
            super().__init__()
            self.queued = 0
            self.polls = 0
            self.drain_after = drain_after
            self.stuck = stuck
            self.purged_with_work = None

        def submit(self, data, name="OTP", title="OTP", options=None):
            self.queued += 1
            return super().submit(data, name, title, options)

        def active_jobs(self, name="OTP"):
            self.polls += 1
            if self.stuck:
                return self.queued
            if self.polls % self.drain_after == 0:
                self.queued = 0
            return self.queued

        def purge(self, name="OTP"):
            if self.purged_with_work is None:
                self.purged_with_work = self.queued > 0

    def test_the_spool_is_not_purged_while_copy_b_is_live(self):
        cups = self.Spool()
        assert run(cups) == 0
        assert cups.purged_with_work is False, \
            "purging with work queued cancels the copy that is printing"

    def test_it_waits_for_the_queue_rather_than_a_fixed_timer(self):
        cups = self.Spool()
        run(cups)
        assert cups.polls > 0, "the queue must actually be asked"

    def test_a_queue_that_never_drains_is_not_purged(self):
        # Unknowable state is not an empty queue: leave the spool sooner than
        # cancel a job that may be printing.
        cups = self.Spool(stuck=True)
        run(cups, settings=settings())
        assert cups.purged_with_work in (False, None)


class TestTheTrayCanExplainItselfWithoutAPrinter:
    """The warning that copy B may be missing is printed before copy B is
    attempted, while there is still a printer: the commonest reason copy B
    fails is that printing broke.
    """

    def test_the_separator_says_what_a_missing_copy_b_means(self):
        rows = [str(value) for section in _sections(unattended.SWAP)
                for _, value in section.rows]
        text = " ".join(rows).upper()
        assert "IF THEY DO NOT" in text or "IF NOTHING FOLLOWS" in text
        assert "HALF A PAIR" in text
        assert "DESTROY" in text

    def test_a_broken_printer_cannot_erase_that_warning(self, monkeypatch):
        # The separator is on paper before copy B is attempted, so nothing
        # afterwards can take it back.
        class DiesAfterSeparator(Cups):
            """cupsd stops answering the moment the separator is on paper."""

            def submit(self, data, name="OTP", title="OTP", options=None):
                if "REMOVE COPY A" in self.titles():
                    raise printer.PrinterError("cupsd is not running")
                return super().submit(data, name, title, options)

        cups = DiesAfterSeparator()
        assert run(cups) == 1
        assert cups.titles()[-1] == "REMOVE COPY A", \
            "the separator must be the last thing that reached paper"
        assert "OTP B" not in cups.titles()
        assert "WHAT TO DO NOW" not in cups.titles(), \
            "this models the case where the final sheet cannot be printed"


def _sections(kind, **kwargs):
    """A sheet's sections, without going through the PDF."""
    captured = []
    real = unattended.diagnostics.render_bytes
    try:
        unattended.diagnostics.render_bytes = \
            lambda sections, *a, **k: captured.append(sections) or bytearray()
        unattended.sheet(kind, "X-Y", config.Settings(), **kwargs)
    finally:
        unattended.diagnostics.render_bytes = real
    return captured[0]


class TestTheQueueIsNeverAskedToHoldTwoJobs:
    """Each job waits for the queue to drain before the next is submitted. The
    unit ships MaxJobs 4 and cupsd refuses, not queues, past the limit (`lp:
    Too many active jobs.`).
    """

    class OneAtATime(Cups):
        """A queue that refuses a second job while one is outstanding."""

        def __init__(self, max_jobs=1):
            super().__init__()
            self.max_jobs = max_jobs
            self.outstanding = 0
            self.refusals = 0
            self.high_water = 0

        def submit(self, data, name="OTP", title="OTP", options=None):
            if self.outstanding >= self.max_jobs:
                self.refusals += 1
                raise printer.PrinterError("lp: Too many active jobs.")
            self.outstanding += 1
            self.high_water = max(self.high_water, self.outstanding)
            return super().submit(data, name, title, options)

        def active_jobs(self, name="OTP"):
            # One poll of the queue retires one job, as a printer does.
            if self.outstanding:
                self.outstanding -= 1
            return self.outstanding

    def test_the_whole_sequence_prints_against_a_maxjobs_of_one(self,
                                                                monkeypatch):
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        cups = self.OneAtATime()
        assert run(cups) == 0
        assert cups.refusals == 0
        assert cups.titles() == ["OTP status", "MANUAL", "TABULA RECTA",
                                 "OTP A", "REMOVE COPY A", "OTP B",
                                 "WHAT TO DO NOW"]

    def test_only_one_job_is_ever_outstanding(self, monkeypatch):
        # The spool holds one job's key material at a time.
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        cups = self.OneAtATime(max_jobs=4)
        run(cups)
        assert cups.high_water == 1


class TestADrainedQueueIsNotProofOfPrinting:
    """An empty queue is not read as success. ErrorPolicy is abort-job, so a
    failed job is discarded as promptly as a printed one; the unit must check
    for a printer fault before announcing the pair or zeroing the key.
    """

    class OutOfPaper(Cups):
        def __init__(self, fault="Out of paper", after=0):
            super().__init__()
            self.fault = fault
            self.after = after

        def printer_fault(self, name="OTP"):
            return self.fault if len(self.submitted) >= self.after else None

    def test_a_reported_fault_beats_an_empty_queue(self):
        cups = self.OutOfPaper()
        assert run(cups) == 1, "an empty tray is not a printed pair"

    def test_the_operator_is_told_to_destroy_the_stack(self):
        cups = self.OutOfPaper()
        run(cups)
        final = [job for job in cups.submitted
                 if job["title"] == "WHAT TO DO NOW"][-1]
        assert b"%PDF" == final["data"][:4]
        text = bytes(unattended.sheet(unattended.HALF, "X-Y",
                                      config.Settings(),
                                      trouble="Out of paper"))
        assert text[:4] == b"%PDF"

    def test_the_reason_reaches_the_half_sheet(self):
        # The sheet must say why the pad failed.
        rows = [value for section in _sections(unattended.HALF, trouble="Out of paper")
                for _, value in section.rows]
        assert any("Out of paper" in str(value) for value in rows)

    def test_it_stops_before_wasting_copy_b_on_a_dead_printer(self):
        # Copy A already failed, so copy B is not printed.
        cups = self.OutOfPaper()
        run(cups)
        assert "OTP B" not in cups.titles()

    def test_a_queue_that_cannot_be_asked_falls_back_to_the_drain(self):
        # Silence is not a fault: inventing one would have the operator burn a
        # good pair.
        class Mute(Cups):
            def printer_fault(self, name="OTP"):
                return None

        assert run(Mute()) == 0

    def test_a_cups_without_the_probe_at_all_still_works(self):
        # Older doubles, and any Cups that predates printer_fault.
        class Old(Cups):
            printer_fault = None

            def __getattr__(self, name):
                raise AttributeError(name)

        assert run(Old()) == 0


class TestThePlugWorksDuringTheDrainToo:
    """Unplugging the printer aborts during the drain as well. The drain is the
    longest wait in the sequence, and the cable is the only cancel this mode
    offers.
    """

    class Gone(Cups):
        """A queue with work outstanding and a printer that vanishes."""

        def __init__(self, disconnect_after=1):
            super().__init__()
            self.disconnect_after = disconnect_after
            self.device_polls = 0

        def devices(self):
            self.device_polls += 1
            return [] if self.device_polls > self.disconnect_after else [DEVICE]

        def active_jobs(self, name="OTP"):
            return 1                             # never drains

    def test_drain_aborts_when_the_printer_is_unplugged(self):
        cups = self.Gone()
        with pytest.raises(unattended.Aborted):
            unattended.drain(cups, "OTP", lambda s: None, lambda *_: None)

    def test_one_empty_poll_during_the_drain_is_not_a_disconnect(self):
        """A single empty devices() answer during the drain does not abort: a busy
        cupsd returns [] just as an unplugged cable does.
        """
        class Flaky(Cups):
            def __init__(self):
                super().__init__()
                self.polls = 0

            def devices(self):
                self.polls += 1
                return [] if self.polls % 2 == 0 else [DEVICE]

            def active_jobs(self, name="OTP"):
                return 0 if self.polls > 6 else 1

        cups = Flaky()
        assert unattended.drain(cups, "OTP", lambda s: None,
                                lambda *_: None) is True

    def test_the_drain_is_bounded_even_with_a_no_op_sleep(self):
        # The sleep in these tests does not advance a wall clock, so the loop
        # needs its own bound.
        cups = self.Gone(disconnect_after=10_000)
        assert unattended.drain(cups, "OTP", lambda s: None, lambda *_: None,
                                timeout=10.0, step=2.0) is False


class TestTheSpoolIsPurgedWhenNothingCanBePrinting:
    """The spool is purged on a disconnect abort. finish(purge=False) is right
    while a copy may still be feeding paper, but once the printer is gone what
    stays on tmpfs is a complete copy of the key.
    """

    class Vanishes(Cups):
        """Copy A spools, then the cable comes out while it is printing."""

        def __init__(self):
            super().__init__()
            self.purged = 0
            self.polls = 0

        def devices(self):
            self.polls += 1
            # Present through the status sheet and the countdown, gone by the
            # time copy A is draining.
            return [DEVICE] if self.polls <= 8 else []

        def active_jobs(self, name="OTP"):
            # Work outstanding, so the disconnect lands in the drain.
            return 1 if self.submitted else 0

        def purge(self, name="OTP"):
            self.purged += 1

    def test_an_abort_still_purges_the_spool(self):
        cups = self.Vanishes()
        assert run(cups) == 1
        assert cups.purged >= 1, \
            "the printer is gone; the spooled pad is all that is left"

    def test_an_unknown_failure_does_not_purge(self, monkeypatch):
        # Any abort other than a disconnect leaves the queue state unknown, and
        # cancelling a live job loses that copy.
        cups = Cups()
        purged = []
        monkeypatch.setattr(cups, "purge", lambda name="OTP": purged.append(1))

        def explode(*args, **kwargs):
            raise RuntimeError("the printer caught fire")

        monkeypatch.setattr(unattended.jobs.PadPairJob, "print_next_copy",
                            explode)
        assert run(cups) == 1
        assert purged == []


class TestAHalfPairIsNeverCalledComplete:
    def test_an_abort_after_copy_a_says_the_pad_is_unusable(self):
        class DiesOnB(Cups):
            def submit(self, data, name="OTP", title="OTP", options=None):
                if title == "OTP B":
                    raise printer.PrinterError("out of paper")
                return super().submit(data, name, title, options)

        cups = DiesOnB()
        assert run(cups) == 1, "an incomplete pair is not a success"
        assert "WHAT TO DO NOW" in cups.titles()

    def test_the_final_sheet_does_not_claim_two_identical_copies(self,
                                                                monkeypatch):
        seen = []
        real = unattended.diagnostics.render_bytes

        def spy(sections, page_size=None, **heading):
            seen.append((heading, sections))
            return real(sections, page_size, **heading)

        monkeypatch.setattr(unattended.diagnostics, "render_bytes", spy)
        unattended.sheet(unattended.HALF, codeword="X-Y", settings=settings())
        heading, sections = seen[-1]
        blurb = f"{heading.get('title','')} {heading.get('subtitle','')}"
        assert "NOT USABLE" in blurb
        body = " ".join(str(v) for _, rows in
                        ((s.title, s.rows) for s in sections)
                        for _, v in rows)
        assert "DESTROY" in body
        assert "identical" not in body

    def test_the_half_pair_sheet_is_one_page(self):
        import re
        data = bytes(unattended.sheet(unattended.HALF, codeword="X-Y",
                                      settings=settings()))
        assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 1

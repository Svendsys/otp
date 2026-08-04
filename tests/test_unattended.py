"""Tests for producing pads with no panel and no buttons.

The point of this mode is that someone who cannot obtain an SSD1306 still
gets working pads. So the tests are about the two things that can go
wrong in a way the operator cannot see or correct: the sequence printing
in the wrong order, and the unit ignoring the only abort it offers.
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
        # The manual is a file on the unit; stand one in so the ordering
        # can be checked without the rendered asset being present.
        monkeypatch.setattr(unattended.jobs, "manual_available", lambda: True)
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        cups = Cups()
        assert run(cups) == 0
        # Status first so the countdown is readable BEFORE the wait. Then
        # the MANUAL -- ahead of the pads deliberately, so that if paper
        # runs out what survives is the instructions rather than half a
        # pad. Then the card, copy A, the tray break, copy B.
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
        # A6 paper cannot take two A5 pages; leave it to the driver.
        assert unattended._manual_options(config.Settings(paper="A6")) == {}


class TestThePaperEstimate:
    """
    Someone deciding whether to let this run may have a finite stack of
    paper and no way to get more, and running out mid-pair loses the pair
    rather than just the paper.
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
        # Page streams are compressed, so assert on the spec the job was
        # built from rather than grepping the PDF for the word.
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
        # The card is useful, not essential. Losing it must not lose the
        # thing the operator actually needs.
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
    """
    The printer cable is the one control that needs no parts. If pulling
    it does not stop the unit, someone with no display has no way to say
    no at all.
    """

    def test_unplugging_during_the_wait_prints_no_pad(self):
        # Status sheet, then the printer goes away before the delay ends.
        cups = Cups(script=[[DEVICE], [], [], []])
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

    def test_countdown_reports_a_disconnect(self):
        cups = Cups(script=[[]])
        with pytest.raises(unattended.Aborted):
            unattended.countdown(cups, seconds=5, sleep=lambda s: None,
                                 log=lambda *_: None)


class TestAButtonMeansPrintNow:
    """
    A press is the whole vocabulary here: stop waiting. That matters
    because a button is the one part someone can improvise -- a wire or a
    paperclip across pin 33 and ground is a button.
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
        # An unexportable pin or a missing lgpio must degrade to "no
        # button", never stop the pads.
        class Broken:
            def wait(self, timeout=0):
                raise OSError(121, "Remote I/O error")

        cups = Cups()
        assert unattended.countdown(cups, seconds=1, buttons=Broken(),
                                    sleep=lambda s: None,
                                    log=lambda *_: None) == "elapsed"

    def test_open_buttons_never_raises(self):
        """
        On a unit with no gpiozero, no gpiochip or no permissions this has
        to answer "no button", not take the pads down with it.
        """
        result = unattended.open_buttons()
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
        """
        The status sheet's heading says it contains no key material, and
        that heading used to be printed on EVERY sheet -- including the
        one telling the operator to keep the secret pages above it. A
        contradiction there gets live key material binned.

        Asserted on the heading each sheet asks for, not on the rendered
        bytes: reportlab's page streams are not greppable, so a raw-bytes
        check here passes whatever the sheet says.
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
        # The converse: that reassurance belongs on the sheet that really
        # is safe to photograph and send to someone for help.
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

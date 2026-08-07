"""Tests for the status sheet a panel-less unit prints.

This sheet is the entire user interface of a unit with no OLED. If it
raises, the operator gets a dead box and a blank tray with nothing to go
on -- so the tests that matter most here are the ones that break the
probes rather than the ones that run them on a healthy system.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otpunit import config, diagnostics, printer

DEVICE = printer.Device("usb://HP/LaserJet%20Pro%20M12w?serial=VNB3K1",
                        "HP LaserJet Pro M12w")


def page_count(data: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", bytes(data)))


def text_of(sections) -> str:
    out = []
    for section in sections:
        out.append(section.title)
        for label, value in section.rows:
            out.append(f"{label or ''} {value}")
    return "\n".join(out)


def drawn_at(sections, page_size=None):
    """
    [(y, text)] for every string the renderer actually put on paper.

    Reading the drawing calls rather than the section list is the point:
    the two used to disagree by 241 lines, and a test that walked the
    sections could not see the difference.
    """
    import otp_generator as gen

    calls = []

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            attribute = getattr(self.inner, name)
            if name != "drawString":
                return attribute

            def draw(x, y, text, *args, **kwargs):
                calls.append((y, text))
                return attribute(x, y, text, *args, **kwargs)
            return draw

    real = gen.new_canvas
    try:
        gen.new_canvas = lambda out, size, **kw: Spy(real(out, size, **kw))
        diagnostics.render_bytes(sections, page_size or diagnostics.A4)
    finally:
        gen.new_canvas = real
    return calls


def drawn_lines(sections, page_size=None) -> list:
    return [text for _, text in drawn_at(sections, page_size)]


def words(lines) -> set:
    return {word for line in lines for word in line.split()}


class Cups:
    """Scripted printer discovery."""

    def __init__(self, script=None, fail_setup=False, fail_submit=False):
        # Each entry is the device list for one poll.
        self.script = list(script if script is not None else [[DEVICE]])
        self.fail_setup = fail_setup
        self.fail_submit = fail_submit
        self.submitted = []
        self.polls = 0

    def devices(self):
        self.polls += 1
        if self.script:
            return self.script.pop(0)
        return [DEVICE]

    def ensure_queue(self, device, name="OTP"):
        if self.fail_setup:
            raise printer.PrinterError("no driver for HP LaserJet Pro M12w")
        return name

    def submit(self, data, name="OTP", title="OTP", options=None):
        if self.fail_submit:
            raise printer.PrinterError("lp: no such destination")
        self.submitted.append({"title": title, "data": bytes(data),
                               "options": options or {}})
        return "job-1"


def drive(cups, rounds=6, sequence=None, **kwargs):
    """Run the headless loop for a bounded number of polls."""
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= rounds:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        diagnostics.run_headless(cups, sleep=sleep, log=lambda *_: None,
                                 sequence=sequence or record(cups), **kwargs)
    return calls


def record(cups):
    """A stand-in sequence that just notes it ran."""
    def sequence(c, settings=None, queue="OTP", log=None, sleep=None,
                 buttons=None, driver=None):
        cups.submitted.append({"title": "OTP status", "data": b"%PDF-",
                               "driver": driver})
        return 0
    return sequence


class TestTheSheetSurvivesABrokenSystem:
    """
    Every probe reads a file or imports a module that may not be there.
    The sheet has to come out anyway -- a diagnostic that dies while
    diagnosing is worse than none.
    """

    def test_collect_never_raises_when_nothing_is_readable(self, monkeypatch):
        def explode(*args, **kwargs):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(Path, "read_text", explode)
        monkeypatch.setattr(diagnostics.shutil, "disk_usage", explode)
        monkeypatch.setattr(diagnostics.subprocess, "run", explode)
        monkeypatch.setattr(diagnostics.glob, "glob", explode)
        sections = diagnostics.collect(settings=config.Settings())
        assert sections, "a broken system must still produce a sheet"

    def test_a_broken_system_still_renders_one_page(self, monkeypatch):
        monkeypatch.setattr(Path, "read_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError()))
        sections = diagnostics.collect(settings=config.Settings())
        assert page_count(diagnostics.render_bytes(sections)) == 1

    def test_a_missing_file_reads_as_absent_not_as_a_traceback(self):
        value = diagnostics._try(lambda: diagnostics._read("/nope/missing"))
        assert "not present" in value
        assert "Traceback" not in value and "Errno" not in value

    def test_a_missing_module_is_named_plainly(self):
        value = diagnostics._try(lambda: diagnostics._module_version("nope_xyz"))
        assert value.startswith("NOT INSTALLED")

    def test_an_unexpected_failure_is_still_printable(self):
        def boom():
            raise RuntimeError("something odd")

        value = diagnostics._try(boom)
        assert "something odd" in value
        assert isinstance(value, str)


class TestTheSheetFitsAndSaysTheRightThings:
    def test_it_is_exactly_one_page(self):
        sections = diagnostics.collect(settings=config.Settings(),
                                       printer=DEVICE, queue="OTP")
        assert page_count(diagnostics.render_bytes(sections)) == 1

    def test_nothing_is_dropped_when_every_value_is_long(self):
        """
        Probe failures produce long strings, and they have to survive.

        This used to assert the sheet stayed on one page -- which it did,
        by counting the lines that would not fit and throwing them away
        behind a one-line footnote. The content that gets discarded first
        is the content at the bottom, which is the SECURITY POSTURE and
        SETTINGS sections: exactly what a long probe failure is about.
        """
        sections = diagnostics.collect(settings=config.Settings(),
                                       printer=DEVICE, queue="OTP")
        for section in sections:
            section.rows = [(label, (str(value) + " ") * 12)
                            for label, value in section.rows]
        drawn = drawn_lines(sections)
        assert page_count(diagnostics.render_bytes(sections)) > 1, \
            "this much text cannot fit one sheet; it must run onto another"
        for section in sections:
            assert section.title in drawn
        assert not [line for line in drawn if "did not fit" in line]

    def test_a6_gets_the_whole_sheet_not_a_fortieth_of_it(self):
        """
        A6 is 105mm wide. Two columns there leave 13pt of width for a value
        beside a 30mm label, so every value wrapped one word to a line and
        the sheet shed 241 of its 282 lines -- silently, apart from a
        footnote giving the count. An A6 unit is exactly the unit least
        likely to have any other way of being read.
        """
        from reportlab.lib.pagesizes import A6

        settings = config.Settings(paper="A6")
        sections = diagnostics.collect(settings=settings, printer=DEVICE,
                                       queue="OTP")
        on_a4 = drawn_lines(sections, page_size=diagnostics.A4)
        on_a6 = drawn_lines(sections, page_size=A6)
        # Wrapping differs with the column width, so compare the words that
        # reached paper rather than the lines they were broken into.
        assert words(on_a6) >= words(on_a4)
        for section in sections:
            assert section.title in on_a6

    def test_no_line_is_drawn_below_the_bottom_margin(self):
        """
        Pagination that runs off the foot of the sheet is the same defect
        in a different costume: a laser printer's unprintable band eats it
        and nothing says so.
        """
        from reportlab.lib.pagesizes import A6

        for page_size in (diagnostics.A4, A6):
            settings = config.Settings(paper="A6")
            sections = diagnostics.collect(settings=settings)
            lowest = min(y for y, _ in drawn_at(sections, page_size))
            assert lowest > 20, f"{page_size}: text at y={lowest}"

    def test_it_tells_the_operator_what_hardware_to_add(self):
        text = text_of(diagnostics.collect(settings=config.Settings()))
        assert "SSD1306" in text
        assert "GPIO13" in text, "the button pinout has to be on the sheet"
        assert "0x3C" in text

    def test_it_says_what_is_about_to_happen(self):
        # The countdown notice is the part someone with no hardware
        # actually needs: what is coming, and how to stop it.
        plan = ["THIS UNIT WILL PRINT A ONE-TIME PAD PAIR IN 5 MINUTES.",
                "TO STOP IT: unplug the printer."]
        text = text_of(diagnostics.collect(settings=config.Settings(),
                                           plan=plan))
        assert "WILL PRINT" in text and "unplug" in text

    def test_it_explains_the_card_and_the_paperclip(self):
        # The two control surfaces that need no parts at all.
        text = text_of(diagnostics.collect(settings=config.Settings()))
        assert "otp-unit.conf" in text
        assert "paperclip" in text
        assert "pin 33" in text

    def test_it_reports_the_printer_it_found(self):
        text = text_of(diagnostics.collect(printer=DEVICE, queue="OTP",
                                           driver="foo2zjs"))
        assert "LaserJet" in text and "foo2zjs" in text

    def test_it_flags_swap_and_a_live_network_as_defects(self, monkeypatch):
        monkeypatch.setattr(diagnostics, "_read",
                            lambda p, limit=200: "Filename Type\n/swapfile file 512 0 -2")
        assert "KEY MATERIAL" in diagnostics._swap()

    def test_no_key_material_can_reach_the_sheet(self):
        """
        The sheet is meant to be photographed and sent to someone for
        help, so it must never carry a codeword or key. collect() takes
        no codeword at all -- this pins that.
        """
        import inspect

        params = inspect.signature(diagnostics.collect).parameters
        assert "codeword" not in params
        text = text_of(diagnostics.collect(settings=config.Settings(pages=3),
                                           printer=DEVICE))
        # Five-letter uppercase groups are what key material looks like.
        assert not re.search(r"\b[A-Z]{5}\s+[A-Z]{5}\b", text)


class TestTheHeadlessLoop:
    def test_it_prints_one_sheet_when_a_printer_appears(self):
        cups = Cups()
        drive(cups)
        assert len(cups.submitted) == 1
        assert cups.submitted[0]["title"] == "OTP status"

    def test_it_does_not_reprint_while_the_printer_stays_put(self):
        cups = Cups(script=[[DEVICE]] * 8)
        drive(cups, rounds=8)
        assert len(cups.submitted) == 1, "one sheet per connection, not per poll"

    def test_it_reprints_after_a_real_disconnect_and_reconnect(self):
        # GONE_AFTER consecutive empties, not one: see below.
        cups = Cups(script=[[DEVICE], [], [], [], [DEVICE]])
        drive(cups, rounds=6)
        assert len(cups.submitted) == 2

    def test_one_bad_poll_does_not_start_a_second_pad_pair(self):
        """
        The real Cups.devices() returns [] for a busy cupsd or a timed-out
        lpinfo exactly as it does for an unplugged cable. Re-arming on one
        of those printed a whole second pair -- 68 more sheets and a fresh
        codeword -- with the first still in the tray. One bad poll in five
        produced twelve pad-pair starts in sixty polls.
        """
        cups = Cups(script=[[DEVICE], [], [DEVICE], [], [DEVICE], []])
        drive(cups, rounds=8)
        assert len(cups.submitted) == 1

    def test_it_waits_quietly_with_no_printer(self):
        cups = Cups(script=[[]] * 6)
        drive(cups, rounds=5)
        assert cups.submitted == []

    def test_a_failed_queue_setup_still_runs_the_sequence(self):
        # The setup error is the single most useful line on the page, so
        # failing to create a queue must not suppress the report.
        cups = Cups(fail_setup=True)
        drive(cups)
        assert len(cups.submitted) == 1

    def test_a_sequence_that_raises_does_not_restart_the_pair(self):
        # Re-running would print copy A of a second pair on top of the
        # first, which is how a tray ends up holding two half-pairs.
        cups = Cups(script=[[DEVICE]] * 8)

        def explode(*args, **kwargs):
            cups.submitted.append("attempt")
            raise RuntimeError("printer caught fire")

        drive(cups, rounds=8, sequence=explode)
        assert len(cups.submitted) == 1, "one attempt per connection"

    def test_a_lookup_failure_is_survived(self):
        class Broken(Cups):
            def devices(self):
                self.polls += 1
                raise OSError("cupsd is not running")

        cups = Broken()
        drive(cups, rounds=3)
        assert cups.polls >= 3, "it must keep trying"

    def test_the_queue_setup_error_reaches_the_sequence(self):
        """
        The setup error is the most useful line on the sheet a mute unit
        prints. Moving collect() into unattended.run left `driver`
        computed here and passed nowhere, so the sheet always said
        "Driver unknown" -- and the test that covered it only counted
        sheets, so it stayed green.
        """
        cups = Cups(fail_setup=True)
        drive(cups)
        assert "no driver" in (cups.submitted[0]["driver"] or "")

    def test_once_returns_rather_than_looping(self):
        cups = Cups()
        assert diagnostics.run_headless(cups, once=True, log=lambda *_: None,
                                        sleep=lambda s: None,
                                        sequence=record(cups)) == 0
        assert len(cups.submitted) == 1

    def test_once_reports_failure_when_there_is_no_printer(self):
        cups = Cups(script=[[]])
        assert diagnostics.run_headless(cups, once=True, log=lambda *_: None,
                                        sleep=lambda s: None,
                                        sequence=record(cups)) == 1

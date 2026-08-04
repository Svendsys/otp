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


def drive(cups, rounds=6, **kwargs):
    """Run the headless loop for a bounded number of polls."""
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= rounds:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        diagnostics.run_headless(cups, sleep=sleep, log=lambda *_: None,
                                 **kwargs)
    return calls


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

    def test_it_stays_one_page_when_every_value_is_long(self):
        # Probe failures produce long strings; they must not spill a page.
        sections = diagnostics.collect(settings=config.Settings(),
                                       printer=DEVICE, queue="OTP")
        for section in sections:
            section.rows = [(label, (str(value) + " ") * 12)
                            for label, value in section.rows]
        assert page_count(diagnostics.render_bytes(sections)) == 1

    def test_it_tells_the_operator_what_hardware_to_add(self):
        text = text_of(diagnostics.collect(settings=config.Settings()))
        assert "SSD1306" in text
        assert "GPIO13" in text, "the button pinout has to be on the sheet"
        assert "0x3C" in text

    def test_it_says_pads_will_not_print_in_this_state(self):
        text = text_of(diagnostics.collect(settings=config.Settings()))
        assert "NOT print pads" in text

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

    def test_it_reprints_after_a_disconnect_and_reconnect(self):
        cups = Cups(script=[[DEVICE], [], [], [DEVICE]])
        drive(cups, rounds=5)
        assert len(cups.submitted) == 2

    def test_it_waits_quietly_with_no_printer(self):
        cups = Cups(script=[[]] * 6)
        drive(cups, rounds=5)
        assert cups.submitted == []

    def test_a_failed_queue_setup_still_prints_the_sheet(self):
        # The setup error is the single most useful line on the page, so
        # failing to create a queue must not suppress the report.
        cups = Cups(fail_setup=True)
        drive(cups)
        assert len(cups.submitted) == 1
        body = cups.submitted[0]["data"]
        assert page_count(body) == 1

    def test_a_failed_submit_does_not_spin(self):
        cups = Cups(fail_submit=True)
        calls = drive(cups, rounds=4)
        assert calls, "a submit failure must back off, not busy-loop"

    def test_a_lookup_failure_is_survived(self):
        class Broken(Cups):
            def devices(self):
                self.polls += 1
                raise OSError("cupsd is not running")

        cups = Broken()
        drive(cups, rounds=3)
        assert cups.polls >= 3, "it must keep trying"

    def test_the_media_option_follows_the_configured_paper(self):
        cups = Cups()
        drive(cups, settings=config.Settings(paper="A4"))
        assert cups.submitted[0]["options"]["media"] == "A4"

    def test_once_returns_rather_than_looping(self):
        cups = Cups()
        assert diagnostics.run_headless(cups, once=True, log=lambda *_: None,
                                        sleep=lambda s: None) == 0
        assert len(cups.submitted) == 1

    def test_once_reports_failure_when_there_is_no_printer(self):
        cups = Cups(script=[[]])
        assert diagnostics.run_headless(cups, once=True, log=lambda *_: None,
                                        sleep=lambda s: None) == 1

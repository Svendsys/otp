"""Tests for the print unit, driven through the real UI with fake hardware.

These push scripted button presses through a real App and assert on what the
panel would show, so the whole flow is covered without an OLED, a Pi, or a
printer.
"""
import io
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as gen
from otpunit import codewords as cw
from otpunit import config, jobs, printer, ui
from otpunit.hw.buttons import FakeButtons, Press
from otpunit.hw.display import FakeDisplay, Frame


class RecordingCups(printer.Cups):
    """A printer that records what it was asked to print."""

    def __init__(self, devices=(("usb://Fake/Laser?serial=1", "Fake Laser"),)):
        super().__init__(run=None)
        self._devices = [printer.Device(u, d) for u, d in devices]
        self.submitted = []
        self.purged = 0

    def devices(self):
        return list(self._devices)

    def ensure_queue(self, device, name="OTP"):
        return name

    def submit(self, data, name="OTP", title="OTP", options=None):
        self.submitted.append({"title": title, "data": bytes(data), "options": options})
        return f"job-{len(self.submitted)}"

    def purge(self, name="OTP"):
        self.purged += 1


def make_app(script, settings=None, cups=None, tmp_path=None):
    app = ui.App(
        display=FakeDisplay(),
        buttons=FakeButtons(script),
        cups=cups or RecordingCups(),
        settings=settings or config.Settings(pages=2),
        vocabulary=cw.Vocabulary(),
        config_path=str(tmp_path / "otp-unit.conf") if tmp_path else "/nonexistent",
        poll_seconds=0,
    )
    return app


def screens(app):
    return [f.rendered() for f in app.display.frames]


def flat(app):
    return "\n".join("\n".join(rows) for rows in screens(app))


class TestFrame:
    def test_fits_the_panel(self):
        frame = Frame(title="TITLE", lines=["one", "two"], selected=0,
                      progress=0.5, footer="FOOT")
        rows = frame.rendered()
        assert len(rows) == 8
        assert all(len(row) == 21 for row in rows)

    def test_selection_caret_and_progress_bar(self):
        rows = Frame(lines=["alpha", "beta"], selected=1, progress=1.0).rendered()
        assert ">beta" in rows[1]
        assert rows[-1] == "[" + "#" * 19 + "]"

    def test_long_lines_are_truncated_not_wrapped(self):
        rows = Frame(lines=["X" * 80]).rendered()
        assert len(rows[0]) == 21

    def test_overflow_is_detectable(self):
        assert Frame(lines=["X" * 80]).overflowing()
        assert Frame(lines=["short"]).overflowing() == []

    def test_selection_costs_a_column(self):
        exact = "X" * 21
        assert Frame(lines=[exact]).overflowing() == []
        assert Frame(lines=[exact], selected=0).overflowing() == [exact]


class TestEveryScreenFitsThePanel:
    """
    No screen may silently truncate. A 21-column panel turns an over-long
    string into misinformation, which on this device could mean an operator
    not reading "POWER-CYCLE THE PRINTER" to the end.
    """

    def _check(self, screen, app):
        overflow = screen.frame(app).overflowing()
        assert overflow == [], f"{type(screen).__name__}: {overflow}"

    def test_top_level_screens(self):
        app = make_app([])
        for screen in (ui.main_menu(), ui.settings_menu(), ui.WaitForPrinter()):
            self._check(screen, app)

    def test_menus_at_every_selection(self):
        app = make_app([])
        for menu in (ui.main_menu(), ui.settings_menu()):
            for index in range(len(menu.items)):
                menu.index = index
                self._check(menu, app)

    def test_codeword_screens(self):
        app = make_app([])
        self._check(ui.codeword_menu(lambda a, w: None), app)
        self._check(ui.CodewordRoll(lambda a, w: None), app)
        self._check(ui.TextEntry("MODIFIER", lambda a, v: None), app)

    def test_category_and_noun_choosers(self):
        app = make_app([])
        categories = ui.Chooser("CATEGORY", app.vocabulary.categories, None,
                                lambda a, v: None, render=str.upper)
        for index in range(len(categories.options)):
            categories.index = index
            self._check(categories, app)

        for category in app.vocabulary.categories:
            nouns = ui.Chooser(category.upper()[:21], app.vocabulary.nouns(category),
                               None, lambda a, v: None)
            for index in (0, len(nouns.options) - 1):
                nouns.index = index
                self._check(nouns, app)

    def test_settings_choosers(self):
        app = make_app([])
        for factory in [item[1] for item in ui.settings_menu().items[:-1]]:
            chooser = factory(app)
            for index in range(len(chooser.options)):
                chooser.index = index
                self._check(chooser, app)

    def test_every_run_job_stage(self):
        app = make_app([])
        cups = RecordingCups()
        app.cups = cups
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY",
                            config.Settings(pages=2))
        screen = ui.RunJob(spec)
        self._check(screen, app)

        screen.press(app, Press.OK)
        self._check(screen, app)
        screen.press(app, Press.OK)
        self._check(screen, app)
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        self._check(screen, app)

        screen.stage, screen.error = "error", "SOMETHING WENT WRONG BADLY"
        self._check(screen, app)
        screen.stage, screen.done_pages = "generating", 7
        self._check(screen, app)

    def test_longest_codeword_fits_the_confirm_screen(self):
        app = make_app([])
        longest = cw.join(max(app.vocabulary.modifiers, key=len),
                          max(app.vocabulary.all_nouns, key=len))
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, longest, config.Settings(pages=1000))
        self._check(ui.RunJob(spec), app)


class TestWaitForPrinter:
    def test_proceeds_when_a_printer_is_present(self):
        app = make_app([Press.QUIT])
        assert app.wait_for_printer() is True

    def test_waits_while_none_present(self):
        cups = RecordingCups(devices=())
        app = make_app([None, Press.QUIT], cups=cups)
        app.buttons._script = [Press.QUIT]
        assert app.wait_for_printer() is False
        assert "PLUG IN A USB PRINTER" in flat(app)


class TestMainMenu:
    def test_lists_every_print_job(self):
        app = make_app([])
        app.stack = [ui.main_menu()]
        app.render()
        text = flat(app)
        assert "PRINT PAD PAIR" in text
        assert "PRINT TABULA RECTA" in text
        assert "PRINT MANUAL" in text
        assert "PRINT TEST PAGE" in text

    def test_down_moves_the_selection(self):
        app = make_app([])
        menu = ui.main_menu()
        menu.press(app, Press.DOWN)
        assert menu.index == 1

    def test_up_wraps_to_the_end(self):
        app = make_app([])
        menu = ui.main_menu()
        menu.press(app, Press.UP)
        assert menu.index == len(menu.items) - 1


class TestCodewordSelection:
    def test_roll_offers_a_two_word_codeword(self):
        app = make_app([])
        chosen = []
        screen = ui.CodewordRoll(lambda a, word: chosen.append(word))
        app.display.show(screen.frame(app))
        modifier, noun = cw.split(screen.current)
        assert modifier in app.vocabulary.modifiers
        assert noun in app.vocabulary.all_nouns

    def test_reroll_changes_the_codeword(self):
        app = make_app([])
        screen = ui.CodewordRoll(lambda a, w: None)
        screen.frame(app)
        first = screen.current
        seen = set()
        for _ in range(20):
            screen.press(app, Press.DOWN)
            seen.add(screen.current)
        assert len(seen) > 1, "rerolling must actually reroll"

    def test_ok_accepts_the_rolled_codeword(self):
        app = make_app([])
        got = []
        screen = ui.CodewordRoll(lambda a, word: got.append(word) or "done")
        screen.frame(app)
        assert screen.press(app, Press.OK) == "done"
        assert got == [screen.current]

    def test_text_entry_builds_a_word(self):
        app = make_app([])
        got = []
        entry = ui.TextEntry("MODIFIER", lambda a, value: got.append(value))
        entry.press(app, Press.DOWN)      # A -> B
        entry.press(app, Press.OK)        # next character
        entry.press(app, Press.DOWN)
        entry.press(app, Press.DOWN)      # A -> C
        entry.press(app, Press.BACK)      # done
        assert got == ["BC"]

    def test_text_entry_respects_max_length(self):
        app = make_app([])
        entry = ui.TextEntry("NOUN", lambda a, v: v, maxlen=3)
        for _ in range(10):
            entry.press(app, Press.OK)
        assert len(entry.value) == 3


class TestCodewordValidation:
    def test_accepts_a_normal_codeword(self):
        assert cw.validate("RUSTED-BADGER", 9, a7=True, with_auth=True) is None

    def test_rejects_empty(self):
        assert cw.validate("", 9, False, True) == "EMPTY"

    def test_rejects_unsafe_filename_characters(self):
        assert cw.validate("BAD/WORD", 9, False, True) == "BAD CHARACTERS"

    def test_rejects_what_will_not_print(self):
        reason = cw.validate("X" * 40, 9, a7=True, with_auth=True)
        assert reason.startswith("TOO LONG")

    def test_every_vocabulary_codeword_validates(self):
        vocab = cw.Vocabulary()
        longest = cw.join(max(vocab.modifiers, key=len), max(vocab.all_nouns, key=len))
        assert cw.validate(longest, 9, a7=True, with_auth=True) is None


class TestPadPairJob:
    def _spec(self, **kwargs):
        settings = config.Settings(pages=2, **kwargs)
        return jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER", settings)

    def test_prints_two_identical_copies(self):
        cups = RecordingCups()
        job = jobs.PadPairJob(self._spec(), cups)
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        job.print_next_copy()

        assert len(cups.submitted) == 2
        first, second = cups.submitted
        assert first["data"] == second["data"], "a pad pair must be two identical copies"
        assert first["data"][:5] == b"%PDF-"
        assert first["title"].endswith("A")
        assert second["title"].endswith("B")

    def test_refuses_a_third_copy(self):
        job = jobs.PadPairJob(self._spec(), RecordingCups())
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        job.print_next_copy()
        with pytest.raises(RuntimeError):
            job.print_next_copy()

    def test_refuses_to_print_before_generating(self):
        job = jobs.PadPairJob(self._spec(), RecordingCups())
        with pytest.raises(RuntimeError):
            job.print_next_copy()

    def test_finish_zeroes_the_buffer_and_purges_the_spool(self):
        cups = RecordingCups()
        job = jobs.PadPairJob(self._spec(), cups)
        job.generate(progress=lambda d, t: None)
        buffer = job._buffer
        assert any(buffer), "buffer should hold a PDF"
        job.finish()
        assert not any(buffer), "key material must be zeroed"
        assert cups.purged == 1

    def test_finish_is_idempotent(self):
        job = jobs.PadPairJob(self._spec(), RecordingCups())
        job.generate(progress=lambda d, t: None)
        job.finish()
        job.finish()

    def test_context_manager_wipes_on_exit(self):
        cups = RecordingCups()
        with jobs.PadPairJob(self._spec(), cups) as job:
            job.generate(progress=lambda d, t: None)
            buffer = job._buffer
        assert not any(buffer)

    def test_a7_uses_the_a7_layout(self):
        cups = RecordingCups()
        job = jobs.PadPairJob(self._spec(a7=True), cups)
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        assert cups.submitted[0]["data"][:5] == b"%PDF-"

    def test_generation_writes_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        before = set(tmp_path.rglob("*"))
        job = jobs.PadPairJob(self._spec(), RecordingCups())
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        assert set(tmp_path.rglob("*")) == before


class TestOtherJobs:
    def test_non_pad_jobs_print_once(self):
        for kind in (jobs.JobKind.WORKSHEETS, jobs.JobKind.TABULA, jobs.JobKind.TEST_PAGE):
            spec = jobs.JobSpec(kind, "", config.Settings(), count=1)
            assert spec.copies == 1
            assert spec.carries_key_material is False

    def test_test_page_renders(self):
        spec = jobs.JobSpec(jobs.JobKind.TEST_PAGE, "", config.Settings())
        assert jobs.generate(spec)[:5] == bytearray(b"%PDF-")

    def test_tabula_and_worksheets_render(self):
        for kind in (jobs.JobKind.TABULA, jobs.JobKind.WORKSHEETS):
            spec = jobs.JobSpec(kind, "", config.Settings(), count=2)
            assert jobs.generate(spec)[:5] == bytearray(b"%PDF-")

    def test_non_pad_job_does_not_purge(self):
        cups = RecordingCups()
        spec = jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings())
        job = jobs.PadPairJob(spec, cups)
        job.generate()
        job.finish()
        assert cups.purged == 0

    def test_unknown_kind_is_rejected(self):
        spec = jobs.JobSpec("nonsense", "", config.Settings())
        with pytest.raises(ValueError):
            jobs.generate(spec)


class TestRunJobFlow:
    """The confirm -> generate -> copy A -> swap -> copy B -> wipe sequence."""

    def _run(self):
        cups = RecordingCups()
        app = make_app([])
        settings = config.Settings(pages=2)
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY", settings)
        return app, cups, ui.RunJob(spec)

    def test_confirm_screen_states_two_copies(self):
        app, _, screen = self._run()
        text = "\n".join(screen.frame(app).rendered())
        assert "2 COPIES: A AND B" in text
        assert "SILENT-OSPREY" in text

    def test_full_sequence_prints_a_then_b_then_wipes(self):
        app, cups, screen = self._run()
        app.cups = cups

        screen.press(app, Press.OK)             # confirm -> generate -> copy A
        assert screen.stage == "printing"
        assert len(cups.submitted) == 1

        screen.press(app, Press.OK)             # tray clear -> swap prompt
        assert screen.stage == "swap"
        assert "REMOVE THE STACK" in "\n".join(screen.frame(app).rendered())

        screen.press(app, Press.OK)             # -> copy B
        assert screen.stage == "printing"
        assert len(cups.submitted) == 2

        screen.press(app, Press.OK)             # -> done, wiped
        assert screen.stage == "done"
        assert cups.purged == 1
        text = "\n".join(screen.frame(app).rendered())
        # Not "KEY WIPED": the buffer is zeroed, but reportlab's
        # intermediates and the immutable bytes given to the subprocess are
        # not and cannot be. Power-off is the real wipe, so that is what the
        # panel must tell the operator to do.
        assert "KEY WIPED" not in text
        assert "POWER-CYCLE PRINTER" in text

    def test_cancelling_at_confirm_prints_nothing(self):
        app, cups, screen = self._run()
        app.cups = cups
        assert screen.press(app, Press.BACK) is None
        assert cups.submitted == []

    def test_abandoning_between_copies_wipes_and_warns(self):
        app, cups, screen = self._run()
        app.cups = cups
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)             # swap prompt
        screen.press(app, Press.BACK)           # abandon
        assert cups.purged == 1
        # Copy A is already on the tray and the key is now gone, so the pair
        # can never be completed. The operator must be told those sheets are
        # live key material rather than dropped back at the menu.
        text = "\n".join(screen.frame(app).rendered())
        assert "DESTROY" in text
        assert screen.press(app, Press.OK) is ui.HOME

    def test_printer_failure_surfaces_on_the_panel(self):
        class Failing(RecordingCups):
            def submit(self, *a, **k):
                raise printer.PrinterError("OUT OF PAPER")

        app, _, screen = self._run()
        app.cups = Failing()
        screen.press(app, Press.OK)
        assert screen.stage == "error"
        assert "OUT OF PAPER" in "\n".join(screen.frame(app).rendered())


class TestSettings:
    def test_defaults_are_valid(self):
        assert config.Settings().validate() == []

    def test_round_trip_through_a_file(self, tmp_path):
        path = tmp_path / "otp-unit.conf"
        original = config.Settings(pages=500, a7=True, auth_size=6,
                                   training=True, with_auth=True)
        assert config.save(original, str(path), remount=False)
        assert config.load(str(path)) == original

    def test_missing_file_gives_defaults(self, tmp_path):
        assert config.load(str(tmp_path / "nope.conf")) == config.Settings()

    def test_malformed_lines_do_not_stop_the_unit_booting(self, tmp_path):
        path = tmp_path / "c.conf"
        path.write_text("pages = not-a-number\nnonsense\na7 = yes\n")
        loaded = config.load(str(path))
        assert loaded.a7 is True
        assert loaded.pages == config.Settings().pages

    def test_comments_are_ignored(self, tmp_path):
        path = tmp_path / "c.conf"
        path.write_text("# a comment\npages = 50  # trailing\n")
        assert config.load(str(path)).pages == 50

    def test_codeword_is_never_persisted(self, tmp_path):
        path = tmp_path / "c.conf"
        config.save(config.Settings(), str(path), remount=False)
        assert "codeword" not in path.read_text().lower()

    def test_a4_is_the_default(self):
        # Almost nobody has A6 paper; almost everybody has A4 or Letter.
        assert config.Settings().paper == "A4"
        assert config.Settings().imposed is True

    def test_lp_options_only_set_media(self):
        # The imposition is baked into the PDF. If CUPS also tiled or scaled
        # it, the cut lines would no longer be where the crop marks say.
        assert config.Settings(paper="A4").lp_options == {"media": "A4"}
        assert config.Settings(paper="LETTER").lp_options == {"media": "Letter"}
        assert config.Settings(paper="A6").lp_options == {"media": "A6"}

    def test_a6_paper_is_not_imposed(self):
        assert config.Settings(paper="A6").imposed is False

    def test_a7_is_never_imposed(self):
        # A7 already lays two pad pages on an A6 sheet with its own cut line.
        assert config.Settings(paper="A4", a7=True).imposed is False

    def test_sheet_count_reflects_tiling(self):
        assert config.Settings(pages=100, paper="A6").sheets == 100
        assert config.Settings(pages=100, paper="A4").sheets == 25
        assert config.Settings(pages=99, paper="A4").sheets == 25
        assert config.Settings(pages=100, a7=True).sheets == 50

    def test_unknown_paper_is_rejected(self):
        assert config.Settings(paper="A3").validate()

    def test_pad_job_uses_the_paper_setting(self):
        cups = RecordingCups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=1, paper="LETTER"))
        job = jobs.PadPairJob(spec, cups)
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        assert cups.submitted[0]["options"] == {"media": "Letter"}

    def test_manual_job_leaves_paper_to_the_driver(self):
        cups = RecordingCups()
        spec = jobs.JobSpec(jobs.JobKind.WORKSHEETS, "", config.Settings(), count=1)
        job = jobs.PadPairJob(spec, cups)
        job.generate()
        job.print_next_copy()
        assert cups.submitted[0]["options"] == {}

    def test_chars_per_page_follows_the_format(self):
        assert config.Settings(a7=False).chars_per_page == 665
        assert config.Settings(a7=True).chars_per_page == 375

    def test_invalid_pages_are_reported(self):
        assert config.Settings(pages=0).validate()

    def test_settings_menu_changes_pages(self, tmp_path):
        app = make_app([], tmp_path=tmp_path)
        chooser = ui.Chooser("PAGES", config.PAGE_CHOICES, app.settings.pages,
                             lambda a, v: ui._apply(a, pages=v))
        chooser.index = config.PAGE_CHOICES.index(500)
        chooser.press(app, Press.OK)
        assert app.settings.pages == 500

    def test_save_reports_failure_without_raising(self):
        assert config.save(config.Settings(), "/proc/nope/x.conf", remount=False) is False


class TestPrinterDiscovery:
    def _cups(self, lpinfo_output):
        class Result:
            returncode = 0
            stdout = lpinfo_output.encode()
            stderr = b""

        return printer.Cups(run=lambda argv, stdin=None: Result())

    def test_finds_usb_devices(self):
        cups = self._cups(
            "direct usb://Brother/HL-2030?serial=X\n"
            "file cups-pdf:/\n"
            "serial serial:/dev/ttyS0\n"
        )
        devices = cups.devices()
        assert len(devices) == 1
        assert devices[0].uri.startswith("usb://Brother")

    def test_finds_the_ipp_usb_endpoint_too(self):
        # ipp-usb publishes a local IPP endpoint, and that is the ONLY kind
        # of URI `lpadmin -m everywhere` accepts. Filtering it out is what
        # made the driverless path unreachable.
        cups = self._cups(
            "direct usb://HP/LaserJet%20Pro%20M12w?serial=X\n"
            "network ipp://localhost:60000/ipp/print\n"
        )
        devices = cups.devices()
        assert len(devices) == 2
        # The driverless endpoint must be offered first.
        assert devices[0].is_ipp
        assert not devices[1].is_ipp

    def test_is_ipp_classification(self):
        assert printer.Device("ipp://localhost:60000/ipp/print", "").is_ipp
        assert printer.Device("ippusb://HP/x", "").is_ipp
        assert not printer.Device("usb://HP/LaserJet?serial=1", "").is_ipp

    def test_device_label_is_panel_sized(self):
        device = printer.Device("usb://Brother/HL-2030?serial=X", "Brother HL-2030")
        assert device.label == "Brother HL-2030"
        assert len(device.label) <= 20

    def test_label_falls_back_to_the_uri(self):
        assert printer.Device("usb://x/y", "").label

    def test_no_devices_when_none_connected(self):
        assert self._cups("").devices() == []

    def test_pretty_decodes_percent_escapes(self):
        assert printer._pretty("usb://HP/LaserJet%201020?serial=1") == "HP LaserJet 1020"

    def test_submit_pipes_bytes_on_stdin(self):
        seen = {}

        class Result:
            returncode = 0
            stdout = b"request id is OTP-7 (1 file)"
            stderr = b""

        def run(argv, stdin=None):
            seen["argv"] = argv
            seen["stdin"] = stdin
            return Result()

        cups = printer.Cups(run=run)
        job = cups.submit(bytearray(b"%PDF-fake"), title="RUSTED-BADGER A")

        assert job == "OTP-7"
        assert seen["stdin"] == b"%PDF-fake", "key material goes over stdin, not a file"
        assert "-d" in seen["argv"] and printer.QUEUE in seen["argv"]
        assert not any(str(a).endswith(".pdf") for a in seen["argv"])

    def test_submit_raises_on_failure_(self):
        class Result:
            returncode = 1
            stdout = b""
            stderr = b"lp: no destination"

        cups = printer.Cups(run=lambda argv, stdin=None: Result())
        with pytest.raises(printer.PrinterError):
            cups.submit(bytearray(b"x"))


# Real-shaped `lpinfo -m` output. The M12w is the interesting case: it is a
# host-based laser with no driverless support, so it needs a Foomatic PPD
# picked correctly out of a list where "HP" matches hundreds of lines.
LPINFO_M = """\
everywhere IPP Everywhere
drv:///sample.drv/generic.ppd Generic PostScript Printer
foomatic-ppds/HP/HP-LaserJet_1020-foo2zjs.ppd.gz HP LaserJet 1020 Foomatic/foo2zjs
foomatic-ppds/HP/HP-LaserJet_Pro_M12a-foo2zjs-z2.ppd.gz HP LaserJet Pro M12a Foomatic/foo2zjs-z2
foomatic-ppds/HP/HP-LaserJet_Pro_M12w-foo2zjs-z2.ppd.gz HP LaserJet Pro M12w Foomatic/foo2zjs-z2
foomatic-ppds/HP/HP-LaserJet_Pro_M1212nf_MFP-foo2xqx.ppd.gz HP LaserJet Pro M1212nf MFP Foomatic/foo2xqx
foomatic-ppds/Brother/Brother-HL-2030-brlaser.ppd.gz Brother HL-2030 Foomatic/brlaser
"""


class TestPpdMatching:
    """Picking the wrong PPD gives a queue that prints garbage, not an error."""

    def _cups(self, driverless_ok=False, listing=LPINFO_M):
        calls = []

        class Result:
            def __init__(self, rc=0, out=b""):
                self.returncode = rc
                self.stdout = out
                self.stderr = b""

        def run(argv, stdin=None):
            calls.append(argv)
            if argv[0] == printer.LPADMIN:
                if "everywhere" in argv:
                    return Result(0 if driverless_ok else 1)
                return Result(0)
            if argv[0] == printer.LPINFO and "-m" in argv:
                return Result(0, listing.encode())
            return Result(0)

        cups = printer.Cups(run=run)
        cups.calls = calls
        return cups

    def test_picks_the_exact_model(self):
        cups = self._cups()
        device = printer.Device("usb://HP/LaserJet%20Pro%20M12w?serial=X", "")
        assert cups._match_ppd(device) == \
            "foomatic-ppds/HP/HP-LaserJet_Pro_M12w-foo2zjs-z2.ppd.gz"

    def test_does_not_confuse_sibling_models(self):
        cups = self._cups()
        for model, expected in (("M12a", "M12a"), ("M12w", "M12w")):
            device = printer.Device(f"usb://HP/LaserJet%20Pro%20{model}?serial=X", "")
            assert expected in cups._match_ppd(device)

    def test_does_not_fall_back_to_another_manufacturer(self):
        cups = self._cups()
        device = printer.Device("usb://Canon/LBP6000?serial=X", "")
        assert cups._match_ppd(device) is None

    def test_returns_none_when_the_model_is_unknown(self):
        cups = self._cups()
        device = printer.Device("usb://HP/LaserJet%20Ultra%20M999?serial=X", "")
        assert cups._match_ppd(device) is None

    def test_tokeniser_splits_letters_from_digits(self):
        assert printer._tokens("LaserJet Pro M12w") == ["laserjet", "pro", "m", "12", "w"]
        assert printer._tokens("HP-LaserJet_Pro_M12w") == \
            ["hp", "laserjet", "pro", "m", "12", "w"]

    def test_driverless_is_used_for_an_ipp_endpoint(self):
        cups = self._cups(driverless_ok=True)
        device = printer.Device("ipp://localhost:60000/ipp/print", "")
        assert cups.ensure_queue(device) == printer.QUEUE
        used = [argv for argv in cups.calls if argv[0] == printer.LPADMIN][-1]
        assert used[used.index("-m") + 1] == "everywhere"

    def test_driverless_is_never_attempted_for_a_usb_uri(self):
        # CUPS rejects `-m everywhere` against usb:// with "IPP Everywhere
        # driver requires an IPP connection" AND deletes the half-made
        # queue, so trying it is worse than useless.
        cups = self._cups(driverless_ok=True)
        device = printer.Device("usb://HP/LaserJet%20Pro%20M12w?serial=X", "")
        cups.ensure_queue(device)
        models = [argv[argv.index("-m") + 1] for argv in cups.calls
                  if argv[0] == printer.LPADMIN and "-m" in argv]
        assert "everywhere" not in models

    def test_usb_printer_gets_its_ppd(self):
        cups = self._cups(driverless_ok=False)
        device = printer.Device("usb://HP/LaserJet%20Pro%20M12w?serial=X", "")
        assert cups.ensure_queue(device) == printer.QUEUE
        used = [argv for argv in cups.calls if argv[0] == printer.LPADMIN][-1]
        assert "M12w" in used[used.index("-m") + 1]

    def test_raises_when_nothing_can_drive_the_printer(self):
        cups = self._cups(driverless_ok=False, listing="everywhere IPP Everywhere\n")
        device = printer.Device("usb://Canon/LBP6000?serial=X", "")
        with pytest.raises(printer.PrinterError):
            cups.ensure_queue(device)

    def test_submit_raises_on_failure(self):
        class Result:
            returncode = 1
            stdout = b""
            stderr = b"lp: no destination"

        cups = printer.Cups(run=lambda argv, stdin=None: Result())
        with pytest.raises(printer.PrinterError):
            cups.submit(bytearray(b"x"))

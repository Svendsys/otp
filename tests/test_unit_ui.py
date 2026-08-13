"""Tests for the print unit, driven through the real UI with fake hardware.

These push scripted button presses through a real App and assert on what the
panel would show, so the whole flow is covered without an OLED, a Pi, or a
printer.
"""
import inspect
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

    def active_jobs(self, name="OTP"):
        # Must be stubbed. The real one shells out to lpstat, which fails
        # here and now correctly reports "cannot tell" -- which the UI treats
        # as busy, so an unstubbed fake would park every test at `waiting`.
        return 0

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

    def test_a_codeword_cannot_be_picked_off_the_list(self):
        """
        The unit rolls codewords or takes one typed in, and offers no third
        way. Browsing a category was removed deliberately: a hand-picked noun
        carries the operator's taste, and the category picked is a hint about
        who the twin belongs to. Both defeat the point of a codeword, which is
        to name the set without naming its holders.

        This guards the whole surface, not just the menu label -- the pool the
        unit can draw from has to stay the undivided one.
        """
        app = make_app([])
        labels = [label for label, _ in ui.codeword_menu(lambda a, w: None).items]
        assert labels == ["ROLL RANDOM", "TYPE IT IN"]

        for attr in ("categories", "nouns", "random_noun", "nouns_by_category"):
            assert not hasattr(app.vocabulary, attr), (
                f"Vocabulary.{attr} lets a caller narrow a draw to one category"
            )
        assert len(app.vocabulary.all_nouns) > 600

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
        assert "PAD PAIR: A AND B" in text
        assert "SILENT-OSPREY" in text
        # Live vs training must be stated positively, never by omission.
        assert "LIVE KEY MATERIAL" in text

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
        screen.press(app, Press.BACK)           # ask to abandon
        screen.press(app, Press.OK)             # confirm -- swap now asks
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


class TestADrainedQueueIsNotProofOfPrinting:
    """
    lp accepting a job is not paper, and neither is the queue emptying.

    The unit ships `ErrorPolicy abort-job` precisely so a failed job cannot
    wedge a MaxJobs-bounded queue -- which means a job that FAILED is
    discarded exactly as promptly as one that printed. unattended.run has
    consulted printer_fault since that combination handed an operator "YOUR
    PAD PAIR IS PRINTED" over an empty tray. RunJob never did: it advanced
    on `active_jobs == 0` alone, so the panel said COPY A DONE over the
    same empty tray, then PAIR COMPLETE, and wiped the key on the strength
    of it.

    Driven here with doubles so the fast suite owns the regression; the
    same sequence runs against a real cupsd and a real failing backend in
    tests/test_simulated_hardware.py.
    """

    class Reporting(RecordingCups):
        """A queue that drains and a printer that says it failed anyway.

        Reports BOTH channels, because the real one does and they mean
        different things: printer-state-message is the prose lpstat prints,
        printer-state-reasons are the IPP keywords that carry severity.
        A double that only answered the first let the panel's decision rest
        on prose, which is how "Toner low." came to destroy a pad pair.
        """

        def __init__(self, fault="out of paper",
                     reasons=("media-empty-error",), after=0):
            super().__init__()
            self._fault = fault
            self._reasons = list(reasons)
            self.after = after                   # copies to let through

        def _yet(self):
            return len(self.submitted) > self.after

        def printer_fault(self, name="OTP"):
            return self._fault if self._yet() else None

        def state_reasons(self, name="OTP"):
            return list(self._reasons) if self._yet() else []

    def _screen(self, cups):
        app = make_app([])
        app.cups = cups
        settings = config.Settings(pages=2)
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY", settings)
        return app, ui.RunJob(spec)

    def test_a_failed_copy_a_is_not_reported_as_copy_a_done(self):
        cups = self.Reporting()
        app, screen = self._screen(cups)
        screen.press(app, Press.OK)             # confirm -> copy A
        screen.press(app, Press.OK)             # tray clear?
        assert screen.stage == "error", \
            "the panel offered the swap prompt over a tray that got nothing"
        text = "\n".join(screen.frame(app).rendered())
        assert "REMOVE THE STACK" not in text
        # The printer's own words. The IPP keyword is the fallback: "out
        # of paper" is something an operator can act on, "MEDIA EMPTY" is
        # what it boils down to.
        assert "OUT OF PAPER" in text.upper()

    def test_a_failed_copy_b_is_not_reported_as_a_complete_pair(self):
        cups = self.Reporting(after=1)          # copy A prints, copy B does not
        app, screen = self._screen(cups)
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "swap"
        screen.press(app, Press.OK)             # -> copy B
        screen.press(app, Press.OK)             # tray clear?
        assert screen.stage == "error", \
            "the panel called a lost copy B a complete pair"
        text = "\n".join(screen.frame(app).rendered())
        assert "PAIR COMPLETE" not in text
        # Copy A is in their hand and can never be matched. Say so.
        assert "DESTROY PRINTED PAGES" in text

    def test_a_healthy_pair_is_still_reported_as_one(self):
        # The control. Everything above is satisfied by a panel that calls
        # every job a failure, and that panel would be useless.
        cups = RecordingCups()
        app, screen = self._screen(cups)
        for _ in range(4):
            screen.press(app, Press.OK)
        assert screen.stage == "done"
        assert "PAIR COMPLETE" in "\n".join(screen.frame(app).rendered())

    def test_a_cups_that_cannot_report_faults_still_finishes(self):
        """
        Silence is not trouble. A wedged cupsd, and any Cups predating
        printer_fault, must fall back to the queue result rather than send
        someone to burn a pair that printed perfectly.
        """
        class Old(RecordingCups):
            printer_fault = None                 # not even callable

        app, screen = self._screen(Old())
        for _ in range(4):
            screen.press(app, Press.OK)
        assert screen.stage == "done"

    def test_a_raising_printer_fault_does_not_take_the_panel_down(self):
        class Exploding(RecordingCups):
            def printer_fault(self, name="OTP"):
                raise RuntimeError("lpstat went away")

        app, screen = self._screen(Exploding())
        for _ in range(4):
            screen.press(app, Press.OK)
        assert screen.stage == "done"

    def test_a_banked_press_cannot_dismiss_the_fault_unread(self):
        """
        Getting to this screen costs two lpstat subprocesses, and the
        operator spends them looking at `printing`, whose footer is OK WHEN
        TRAY IS CLEAR. A second tap during that wait is banked by
        GpioButtons and replayed against whatever is drawn next -- and
        ERROR's handler wipes the key and returns HOME. The panel would
        flash the fault for one cycle and drop them at the menu having
        never read DESTROY PRINTED PAGES.

        _print_copy has drained on its error path since round three. This
        is the same hazard on the path added with the fault check.
        """
        cups = self.Reporting(after=1)
        app = make_app([])
        app.cups = cups
        settings = config.Settings(pages=2)
        screen = ui.RunJob(jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY",
                                        settings))
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)              # -> swap
        screen.press(app, Press.OK)              # -> copy B
        app.buttons.push(Press.OK, Press.OK)     # impatient taps
        screen.press(app, Press.OK)
        assert screen.stage == "error"
        assert app.buttons.wait(timeout=0) is None, \
            "a banked press survived and will dismiss the fault unread"

    def test_a_stopped_queue_is_not_reported_as_still_printing(self):
        """
        CUPS_BACKEND_STOP -- an open cover, or a printer that reports an
        empty tray properly -- stops the queue and KEEPS the job, so
        active_jobs never reaches zero. The panel said STILL PRINTING / OK
        TO CHECK AGAIN at a printer that would never print, while the
        printer had been saying why the whole time.
        """
        class Stopped(RecordingCups):
            def active_jobs(self, name="OTP"):
                return 1

            def printer_fault(self, name="OTP"):
                # What real lpstat returns for a disabled queue: the header
                # line, with the reason on the NEXT line, which printer.py
                # discards. Measured against the rig's cupsd. The panel must
                # not be reduced to showing this.
                return ("printer OTP disabled since Wed Aug 12 20:21:35 "
                        "2026 -")

            def state_reasons(self, name="OTP"):
                return ["media-empty-error", "paused"]

        app, screen = self._screen(Stopped())
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting"
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "STILL PRINTING" not in shown, shown
        # The REASON, not the header. "or DISABLED" would have been
        # satisfied by the timestamp row of a screen that never said what
        # was wrong -- which is what it was doing.
        assert "MEDIA EMPTY" in shown, shown
        assert "2026" not in shown, \
            f"the panel is showing lpstat's timestamp instead of a reason: {shown}"
        # And the non-destructive retry must survive: this screen is not a
        # trap, it is a report.
        assert "HOLD" in shown

    def test_ok_on_a_stopped_queue_re_enables_it(self):
        """
        OK RECHECK has to be able to succeed, or the only working exit is
        the one that destroys the pair.

        CUPS_BACKEND_STOP disables the queue and holds the job until
        something runs `lpadmin -E`. The unit did that once, in ensure_queue
        at startup, and never again -- so an operator who closed the cover
        and pressed OK got PRINTER STOPPED for ever.
        """
        class Stopped(RecordingCups):
            def __init__(self):
                super().__init__()
                self.resumed = 0
                self.stopped = True

            def active_jobs(self, name="OTP"):
                return 1 if self.stopped else 0

            def state_reasons(self, name="OTP"):
                return ["media-empty-error", "paused"] if self.stopped else []

            def resume(self, name="OTP"):
                self.resumed += 1
                self.stopped = False             # the cover was closed
                return True

        cups = Stopped()
        app, screen = self._screen(cups)
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting"
        assert cups.resumed == 0, "resumed before the operator asked"
        screen.press(app, Press.OK)              # OK RECHECK
        assert cups.resumed == 1, "OK RECHECK did not re-enable the queue"
        assert screen.stage == "swap", \
            "the queue recovered and the panel stayed on PRINTER STOPPED"

    def test_one_ok_press_costs_at_most_two_cups_subprocesses(self):
        """
        Every CUPS query is a subprocess bounded by Cups.TIMEOUT = 120s, and
        the panel is frozen with the key resident for the whole of it. The
        count is therefore a safety property, not a performance one.

        Two is the budget: one active_jobs to decide busy/idle, one
        state_reasons to decide what the printer says about it. The
        stopped-queue path briefly cost THREE -- asking for the reasons
        once to decide it was stopped and again to say why -- which put a
        wedged-but-answering cupsd at six minutes of dead panel per press.
        """
        for label, busy, reasons in [
            ("busy, healthy", True, []),
            ("busy, stopped", True, ["media-empty-error", "paused"]),
            ("idle, healthy", False, []),
            ("idle, real fault", False, ["media-empty-error"]),
        ]:
            calls = []

            class Counting(RecordingCups):
                def active_jobs(self, name="OTP"):
                    calls.append("active_jobs")
                    return 1 if busy else 0

                def printer_fault(self, name="OTP"):
                    calls.append("printer_fault")
                    return None

                def state_reasons(self, name="OTP"):
                    calls.append("state_reasons")
                    return list(reasons)

            app, screen = self._screen(Counting())
            screen.press(app, Press.OK)          # confirm -> copy A
            calls.clear()
            screen.press(app, Press.OK)          # the press under test
            assert len(calls) <= 2, (
                f"{label}: one OK press ran {len(calls)} CUPS subprocesses "
                f"({calls}); at Cups.TIMEOUT={printer.Cups.TIMEOUT}s each "
                f"that is up to {len(calls) * printer.Cups.TIMEOUT}s of "
                f"frozen panel holding key material")

    def test_a_backend_that_reports_no_reasons_is_still_believed(self):
        """
        The unit's OWN hardware path, which a reasons-based decision could
        not see at all.

            $ strings /usr/lib/cups/backend/usb | grep -c 'media-'
            0

        The usb backend -- and usb:// is what Cups.devices() hands back for
        a directly attached laser -- reports no media state ever; its only
        state keywords are connecting-to-device. An earlier version of this
        decided faults from printer-state-reasons alone, so on the shipped
        printer an empty tray produced no reasons, no fault, and PAIR
        COMPLETE over a tray holding nothing.
        """
        cups = self.Reporting(fault="out of paper", reasons=[])
        app, screen = self._screen(cups)
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "error", \
            "a printer that reports prose but no IPP reasons was believed " \
            "to have printed"
        assert "OUT OF PAPER" in "\n".join(screen.frame(app).rendered()).upper()

    def test_a_warning_severity_media_fault_still_stops_the_pair(self):
        """
        CUPS' SNMP supplies code emits the conditions that mean NOTHING
        PRINTED at "-warning" severity:

            $ strings /usr/lib/cups/backend/{socket,ipp,lpd} | grep media-
            media-empty-warning
            media-jam-warning

        Reading the suffix as the severity therefore discarded exactly the
        faults that matter, on every SNMP-capable printer. The stem is what
        carries the meaning.
        """
        for reason in ("media-empty-warning", "media-jam-warning",
                       "toner-empty-warning", "cover-open-warning"):
            cups = self.Reporting(fault="out of paper", reasons=[reason])
            app, screen = self._screen(cups)
            screen.press(app, Press.OK)
            screen.press(app, Press.OK)
            assert screen.stage == "error", \
                f"{reason} was treated as advisory; it means nothing printed"

    def test_a_sticky_reason_does_not_condemn_every_later_pair(self):
        """
        printer-state-reasons OUTLIVE the job that set them: after one
        empty-tray event, `lpstat -l -p` keeps reporting media-empty-error
        on a queue that is printing perfectly, and `lpadmin -E` clears only
        `paused`. A decision resting on reasons therefore sent every later
        pair to DESTROY PRINTED PAGES for the lifetime of the daemon.

        The prose does not stick -- measured against the rig, printer_fault
        returns to None once the tray is refilled -- which is the other
        reason it is the channel that decides.
        """
        cups = self.Reporting(fault=None, reasons=["media-empty-error"])
        app, screen = self._screen(cups)
        for _ in range(4):
            screen.press(app, Press.OK)
        assert screen.stage == "done", \
            "a stale state reason condemned a pair that printed"

    def test_a_resumed_job_warns_that_pages_may_repeat(self):
        """
        Re-enabling a stopped queue re-sends the WHOLE held job. Measured
        against the rig: after OK RECHECK the backend had received copy A
        twice. On real paper that is pages 1..N from before the cover
        opened followed by pages 1..M of the same key -- and the ordinary
        swap prompt says "REMOVE THE STACK AND KEEP IT TOGETHER", which is
        the wrong instruction over a stack with duplicate key material in
        it. The operator is the only one who can see the tray.
        """
        class Stopped(RecordingCups):
            def __init__(self):
                super().__init__()
                self.stopped = True

            def active_jobs(self, name="OTP"):
                return 1 if self.stopped else 0

            def state_reasons(self, name="OTP"):
                return ["media-empty-error", "paused"] if self.stopped else []

            def resume(self, name="OTP"):
                self.stopped = False
                return True

        app, screen = self._screen(Stopped())
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting"
        screen.press(app, Press.OK)              # OK RECHECK -> resume
        assert screen.stage == "swap"
        shown = "\n".join(screen.frame(app).rendered()).upper()
        assert "REPEATED PAGES" in shown, shown
        assert "KEEP IT TOGETHER" not in shown, \
            "the stack may hold the same key pages twice; do not tell the " \
            "operator to keep it together without checking"

    def test_the_panel_and_the_headless_run_agree(self):
        """
        One decision, not one helper. The two modes had a fault helper each;
        merging them shared the helper but left the DECISION split -- the
        panel weighed IPP reasons, unattended read raw prose -- so on the
        same printer they disagreed in both directions: a standing "Toner
        low." aborted the headless pair while the panel finished it, and an
        SNMP empty tray did the reverse.
        """
        from otpunit import unattended

        cases = [
            ("Toner low.", ["toner-low-report"], False),
            ("out of paper", [], True),
            ("out of paper", ["media-empty-warning"], True),
            ("DEBUG: cfFilterChain: universal exited with no errors.",
             [], False),
            (None, ["media-empty-error"], False),
        ]
        for prose, reasons, is_fault in cases:
            cups = self.Reporting(fault=prose, reasons=reasons)
            cups.submitted.append({"title": "x"})   # past `after`
            headless = bool(unattended._fault(cups, "OTP"))
            panel = bool(printer.blocking_in(
                printer.reported_fault(cups, "OTP"),
                printer.state_reasons(cups, "OTP")))
            assert headless == panel == is_fault, (
                f"{prose!r} / {reasons}: panel says {panel}, headless says "
                f"{headless}, expected {is_fault}")

    def test_an_advisory_reason_does_not_destroy_a_good_pair(self):
        """
        The severity suffix is the whole point. A laser reporting
        toner-low-report all day is working, and cupsd puts its own filter
        chatter in printer-state-message on a perfectly healthy queue --
        measured mid-print: "DEBUG: cfFilterChain: universal (PID 21339)
        exited with no errors."

        Both used to reach the panel as ERROR / DESTROY PRINTED PAGES on a
        pair that printed, and the next OK wiped the key. Deciding on any
        non-empty printer-state-message is what made that possible.
        """
        for prose, reasons in [
            ("Toner low.", ["toner-low-report"]),
            ("Toner is low.", ["marker-supply-low-report"]),
            ("DEBUG: cfFilterChain: universal (PID 21339) exited with "
             "no errors.", []),
        ]:
            cups = self.Reporting(fault=prose, reasons=reasons)
            app, screen = self._screen(cups)
            for _ in range(4):
                screen.press(app, Press.OK)
            assert screen.stage == "done", (
                f"{prose!r} / {reasons} sent a healthy pair to "
                f"{screen.stage!r}")

    def test_a_banked_press_cannot_spool_copy_b_over_copy_a(self):
        """
        The success path banks presses exactly as the error path does.

        Reaching `swap` costs two blocking lpstat subprocesses, spent with
        the operator looking at `printing` and its OK WHEN TRAY IS CLEAR
        footer. A second tap is queued by GpioButtons and replayed against
        SWAP the instant it is drawn -- and SWAP's OK handler spools copy B.
        Both halves of the pair then land in one tray, with COPY A DONE /
        REMOVE THE STACK never shown. The error path was drained from the
        start; this one was not.
        """
        cups = RecordingCups()
        app = make_app([])
        app.cups = cups
        settings = config.Settings(pages=2)
        screen = ui.RunJob(jobs.JobSpec(jobs.JobKind.PAD_PAIR,
                                        "SILENT-OSPREY", settings))
        screen.press(app, Press.OK)              # confirm -> copy A
        app.buttons.push(Press.OK, Press.OK)     # impatient taps
        screen.press(app, Press.OK)              # tray clear? -> swap
        assert screen.stage == "swap"
        assert app.buttons.wait(timeout=0) is None, \
            "a banked press survived and will spool copy B over copy A"

    def test_a_busy_queue_with_no_fault_still_says_still_printing(self):
        # The control for the screen above.
        class Busy(RecordingCups):
            def active_jobs(self, name="OTP"):
                return 1

        app, screen = self._screen(Busy())
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert "STILL PRINTING" in "\n".join(screen.frame(app).rendered())

    def test_the_simulator_never_shells_out_to_a_real_printer(self):
        """
        `--sim` runs as a real process outside pytest, so the conftest
        guard does not protect it. SimulatedCups must override every method
        that would otherwise reach the host -- and printer_fault was the
        one that got missed, so `--sim` on the unit itself, or on any dev
        box that has run install.sh, reported the REAL OTP queue's fault
        over a simulated pad that reached no printer at all.
        """
        from otpunit.__main__ import SimulatedCups
        from otpunit import printer as printer_mod

        # DERIVED from Cups, not a list written out here. The hand-written
        # version named six methods and missed _clear_temp(), which is
        # reachable from --sim through unattended.run's finally block and
        # unlinks every file in the host's live /run/cups/tmp as root. A
        # completeness test that has to be kept complete by hand is not one.
        reaches_host = {
            name for name, value in vars(printer_mod.Cups).items()
            if callable(value) and not name.startswith("__")
            and name not in ("_subprocess_run", "_text", "_run_text")
            # A staticmethod has no self, so it cannot reach self._run.
            # reason_stem is pure string work on an IPP keyword.
            and not isinstance(
                inspect.getattr_static(printer_mod.Cups, name), staticmethod)
        }
        covered = set(vars(SimulatedCups))
        # _clear_temp is neutralised by pointing temp_dir at a scratch
        # directory rather than by overriding the method, which covers
        # every caller instead of the one that was remembered.
        assert SimulatedCups().temp_dir != printer_mod.TEMP_DIR, \
            "SimulatedCups would empty the host cupsd's TempDir"
        by_temp_dir = {"_clear_temp"}
        # Private helpers of ensure_queue, which IS overridden, so nothing
        # in --sim can reach them. Named individually rather than excusing
        # everything underscored, because _clear_temp is underscored too and
        # is exactly the one that mattered.
        unreachable = {"_lpadmin", "_match_ppd", "_remove_queue"}
        for name in unreachable:
            assert name in reaches_host, \
                f"{name}() is gone from Cups; drop it from the excuse list"
        missing = reaches_host - covered - by_temp_dir - unreachable
        assert not missing, (
            f"SimulatedCups inherits {sorted(missing)} from Cups, which "
            f"shell out to the host's CUPS")

    def test_a_cups_double_cannot_fall_through_to_the_host(self):
        """
        The conftest guard, pinned. Every double here is built as
        `Cups(run=None)`, which means "use the real subprocess runner", so
        any method left unstubbed reaches the host's lp or lpstat. That was
        harmless only while nothing in the fast path called one -- and then
        printer_fault did, and eleven tests failed on a machine whose real
        OTP queue happened to be reporting a fault.
        """
        with pytest.raises(OSError, match="must not run"):
            printer.Cups._subprocess_run(["/usr/bin/lpstat", "-p", "OTP"])

    def test_the_unanswerable_queue_keeps_its_manual_override(self):
        """
        The override, pinned -- but honestly.

        `confirm_continue` is the operator's only non-destructive way on
        when the queue cannot be asked at all. It is NOT actually at risk
        from the fault check: active_jobs and printer_fault both go through
        Cups._run_text, so a daemon that cannot answer one cannot answer
        the other, and queue_unknown already implies no fault. The double
        below manufactures a combination the real Cups cannot produce, and
        is here to catch someone MOVING the check into _proceed -- not
        because the scenario occurs.
        """
        class Mute(RecordingCups):
            def active_jobs(self, name="OTP"):
                return None                      # "cannot tell"

            def printer_fault(self, name="OTP"):
                return "out of paper"            # must not be consulted here

        app, screen = self._screen(Mute())
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting" and screen.queue_unknown
        screen.press(app, Press.DOWN)
        assert screen.stage == "confirm_continue"
        screen.press(app, Press.OK)
        assert screen.stage == "swap", screen.stage


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

    def test_the_unit_never_writes_a_codeword_to_the_card(self, tmp_path):
        """
        A codeword is not key material, but it names a live pad, and the SD
        card is the part most likely to be captured along with the unit.
        So the unit must never record which pad it produced -- including
        when auto_codeword was set and used.
        """
        path = tmp_path / "c.conf"
        config.save(config.Settings(auto_codeword="RUSTED-BADGER"),
                    str(path), remount=False)
        written = path.read_text()
        assert "RUSTED-BADGER" not in written
        # The only mention may be the commented-out example.
        live = [line for line in written.splitlines()
                if "codeword" in line.lower() and not line.strip().startswith("#")]
        assert live == []

    def test_a_hand_written_codeword_is_still_honoured(self, tmp_path):
        # Read but never written: someone who agrees a codeword out of band
        # can still set one, they are just making that trade themselves.
        path = tmp_path / "c.conf"
        path.write_text("auto_codeword = SILENT-OSPREY\n")
        assert config.load(str(path)).auto_codeword == "SILENT-OSPREY"

    def test_a_malformed_codeword_falls_back_rather_than_printing_it(self, tmp_path):
        path = tmp_path / "c.conf"
        path.write_text("auto_codeword = has spaces/and slashes\n")
        assert config.load(str(path)).auto_codeword == ""

    def test_a4_is_the_default(self):
        # Almost nobody has A6 paper; almost everybody has A4 or Letter.
        assert config.Settings().paper == "A4"
        assert config.Settings().imposed is True

    def test_lp_options_pin_the_geometry(self):
        # The imposition is baked into the PDF. If CUPS also tiled or scaled
        # it, the cut lines would no longer be where the crop marks say.
        #
        # This test used to assert lp_options == {"media": ...} exactly --
        # naming that risk in a comment and then locking in the absence of
        # the options that prevent it. cups-filters defaults print-scaling
        # to `auto`, so "we send nothing" meant "the driver may shrink it to
        # fit", which is precisely the failure the comment describes.
        for paper, media in (("A4", "A4"), ("LETTER", "Letter"), ("A6", "A6")):
            options = config.Settings(paper=paper).lp_options
            assert options["media"] == media
            assert options["print-scaling"] == "none"
            assert options["sides"] == "one-sided"

    def test_a7_sheets_are_not_scaled_either(self):
        options = config.Settings(paper="A4", a7=True).lp_options
        assert options == {"media": "A6", "print-scaling": "none",
                           "sides": "one-sided"}

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
        assert cups.submitted[0]["options"]["media"] == "Letter"
        # The driver must not be left free to shrink an imposed sheet.
        assert cups.submitted[0]["options"]["print-scaling"] == "none"

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

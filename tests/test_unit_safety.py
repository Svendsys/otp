"""Regression tests for defects found by adversarial review.

Two themes run through these. First, this is a headless appliance: a screen
stack that empties, or a screen with no exit, leaves an operator holding a
dark panel and three dead buttons with nothing to do but pull the power.
Second, the pad pair is unrecoverable: there are exactly two copies and no
third, so anything that destroys the key mid-pair, or truncates one copy,
loses the pad for good.
"""
import itertools
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otpunit import codewords as cw
from otpunit import config, jobs, printer, ui
from otpunit.hw.buttons import FakeButtons, Press
from otpunit.hw.display import FakeDisplay

ALL_PRESSES = (Press.UP, Press.DOWN, Press.OK, Press.BACK)


class Cups(printer.Cups):
    def __init__(self, fail_setup=False, no_devices=False, busy=0):
        super().__init__(run=None)
        self.fail_setup = fail_setup
        self.no_devices = no_devices
        self.busy = busy
        self.submitted = []
        self.purged = 0

    def devices(self):
        return [] if self.no_devices else [printer.Device("usb://F/L?serial=1", "F L")]

    def ensure_queue(self, device, name="OTP"):
        if self.fail_setup:
            raise printer.PrinterError("no driver for a printer with a long name")
        return name

    def submit(self, data, name="OTP", title="OTP", options=None):
        self.submitted.append({"title": title, "data": bytes(data)})
        return f"job-{len(self.submitted)}"

    def active_jobs(self, name="OTP"):
        return self.busy

    def purge(self, name="OTP"):
        self.purged += 1


def make_app(script, cups=None, settings=None):
    return ui.App(
        display=FakeDisplay(),
        buttons=FakeButtons(script),
        cups=cups or Cups(),
        settings=settings or config.Settings(pages=2),
        vocabulary=cw.Vocabulary(),
        config_path="/nonexistent",
        poll_seconds=0,
    )


class TestTheUnitCannotBeBricked:
    def test_back_on_the_root_menu_does_not_exit(self):
        app = make_app([Press.BACK] * 5 + [Press.QUIT])
        app.run()
        assert app.stack, "the root menu must never be popped"
        # Every BACK had to be read and survived; only the QUIT ended it.
        assert app.buttons._script == []

    def test_printer_setup_failure_still_leaves_a_menu(self):
        app = make_app([Press.OK] + [Press.QUIT], cups=Cups(fail_setup=True))
        app.run()
        assert len(app.stack) >= 1
        assert isinstance(app.stack[0], ui.Menu)

    def test_the_printer_error_is_readable_not_truncated_mid_word(self):
        app = make_app([Press.QUIT], cups=Cups(fail_setup=True))
        app.run()
        shown = "\n".join(app.display.last.rendered())
        assert "no driver" in shown
        assert app.display.last.overflowing() == []

    def test_text_entry_can_always_be_escaped(self):
        """Deleting past the first letter leaves the screen."""
        got = []
        entry = ui.TextEntry("MODIFIER", lambda a, v: got.append(v))
        app = make_app([])
        # Wind to the delete symbol and press OK on an empty word.
        entry.press(app, Press.UP)
        assert entry.letters[-1] == ui.TextEntry.DELETE
        assert entry.press(app, Press.OK) is None
        assert got == []

    def test_no_screen_traps_the_operator(self):
        """
        From any reachable state, some button sequence returns to the root.

        This is the property the whole navigation model has to have: an
        appliance with three buttons and no keyboard cannot afford a screen
        that only ever pushes.
        """
        def escapes(prefix, depth=4):
            for combo in itertools.product(ALL_PRESSES, repeat=depth):
                app = make_app(list(prefix) + list(combo) + [Press.QUIT])
                app.run()
                if len(app.stack) == 1 and isinstance(app.stack[0], ui.Menu):
                    return True
            return False

        # Into PRINT PAD PAIR -> TYPE IT IN, the deepest reachable flow.
        assert escapes([Press.OK, Press.DOWN, Press.DOWN, Press.OK])

    def test_no_job_stage_traps_the_operator(self):
        """
        Every RunJob stage must have an exit, including the ones only a
        misbehaving printer can reach.

        The App-level search above cannot see these: generation drains the
        scripted presses, so a whole-app walk never gets past a job start.
        Drive RunJob directly instead.
        """
        stages = ("confirm", "printing", "waiting", "swap",
                  "cancelled", "abandoned", "error", "done")
        for stage in stages:
            cups = Cups(busy=1 if stage == "waiting" else 0)
            app = make_app([])
            app.cups = cups
            screen = ui.RunJob(jobs.JobSpec(jobs.JobKind.PAD_PAIR, "X-Y",
                                            config.Settings(pages=1)))
            if stage != "confirm":
                screen.press(app, Press.OK)      # generate + copy A
                screen.stage = stage
                if screen.job is None:
                    screen.job = jobs.PadPairJob(screen.spec, cups)

            # Not a trap if some press either leaves the screen or moves it
            # to a different stage. Standing still on every button is what
            # strands an operator with no keyboard.
            moved = False
            for press in (Press.BACK, Press.OK):
                before = screen.stage
                if screen.press(app, press) in (None, ui.HOME) or screen.stage != before:
                    moved = True
                    break
                screen.stage = stage
            assert moved, f"stage {stage!r} has no exit"

    def test_shutdown_can_be_declined(self):
        confirm = ui.Message("SHUT DOWN", ["POWER OFF?"],
                             on_ok=lambda a: a.request_shutdown())
        app = make_app([])
        assert confirm.press(app, Press.BACK) is None
        assert app.shutdown_requested is False, "a back press must not power off"

    def test_shutdown_still_works_on_ok(self):
        confirm = ui.Message("SHUT DOWN", ["POWER OFF?"],
                             on_ok=lambda a: a.request_shutdown())
        app = make_app([])
        confirm.press(app, Press.OK)
        assert app.shutdown_requested is True

    def test_can_shut_down_while_waiting_for_a_printer(self):
        # An unsupported printer would otherwise strand the operator here
        # with every button inert.
        app = make_app([Press.BACK], cups=Cups(no_devices=True))
        assert app.wait_for_printer() is False
        assert app.shutdown_requested is True


class TestThePairIsNeverSilentlyLost:
    def _job(self, cups, pages=2):
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY",
                            config.Settings(pages=pages))
        return ui.RunJob(spec)

    def test_neither_transition_proceeds_while_the_queue_is_busy(self):
        """
        lp returns once a job is spooled, so "the tray is clear" is the
        operator's guess about paper, not about the queue.

        Starting copy B early interleaves the two copies in one tray;
        purging early cancels the rest of copy B and destroys the key,
        leaving a truncated half-pair that cannot be regenerated.
        """
        cups = Cups(busy=3)
        app, screen = make_app([]), self._job(cups)
        app.cups = cups

        screen.press(app, Press.OK)        # generate + submit copy A
        screen.press(app, Press.OK)        # tray "clear" -- but A is queued
        assert screen.stage == "waiting"
        assert len(cups.submitted) == 1, "copy B must not start early"

        cups.busy = 0
        screen.press(app, Press.OK)
        assert screen.stage == "swap"
        screen.press(app, Press.OK)        # -> copy B
        assert len(cups.submitted) == 2

        cups.busy = 2
        screen.press(app, Press.OK)
        assert screen.stage == "waiting"
        assert cups.purged == 0, "must not purge with jobs still queued"

        cups.busy = 0
        screen.press(app, Press.OK)
        assert screen.stage == "done"
        assert cups.purged == 1

    def test_waiting_always_has_a_way_out(self):
        """
        A queue that never drains must not trap the operator.

        CUPS' default ErrorPolicy holds a job in the queue indefinitely
        after a jam, and three buttons cannot run cupsenable. Without an
        unconditional exit the operator has live key material on screen and
        no option but to pull the power.
        """
        cups = Cups(busy=1)                # never drains
        app, screen = make_app([]), self._job(cups)
        app.cups = cups
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting"

        screen.press(app, Press.BACK)
        assert screen.stage == "abandoned"
        assert cups.purged == 1, "giving up must still wipe"
        assert "DESTROY" in "\n".join(screen.frame(app).rendered())
        assert screen.press(app, Press.OK) is ui.HOME

    def test_a_failure_after_copy_a_tells_the_operator_to_destroy_it(self):
        class Failing(Cups):
            def submit(self, data, name="OTP", title="OTP", options=None):
                if self.submitted:
                    raise printer.PrinterError("printer went away")
                return super().submit(data, name, title, options)

        cups = Failing()
        app, screen = make_app([]), self._job(cups)
        app.cups = cups
        screen.press(app, Press.OK)        # copy A submitted
        screen.press(app, Press.OK)        # -> swap
        screen.press(app, Press.OK)        # copy B fails
        assert screen.stage == "error"
        shown = "\n".join(screen.frame(app).rendered())
        assert "DESTROY" in shown, "copy A is live key material on the tray"

    def test_a_wipe_failure_cannot_kill_the_ui(self):
        class Exploding(Cups):
            def purge(self, name="OTP"):
                raise OSError("cancel is missing")

        cups = Exploding()
        app, screen = make_app([]), self._job(cups)
        app.cups = cups
        for _ in range(4):
            screen.press(app, Press.OK)
        screen.press(app, Press.OK)        # dismiss -- must not raise

    def test_finishing_a_job_returns_all_the_way_to_the_menu(self):
        app = make_app([])
        screen = self._job(Cups())
        app.stack = [ui.main_menu(), ui.codeword_menu(lambda a, w: None), screen]
        for _ in range(4):
            screen.press(app, Press.OK)
        assert screen.press(app, Press.OK) is ui.HOME


class TestCancellationIsReal:
    def test_holding_ok_during_generation_cancels_and_prints_nothing(self):
        cups = Cups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=400))
        screen = ui.RunJob(spec)
        app = make_app([Press.BACK] * 3, cups=cups)
        screen.press(app, Press.OK)
        assert screen.stage == "cancelled"
        assert cups.submitted == [], "a cancelled job must print nothing"
        assert cups.purged == 1, "and must not leave key material behind"

    def test_presses_made_during_generation_do_not_replay_afterwards(self):
        """
        Generation blocks the event loop. Un-drained presses would arrive
        immediately afterwards and skip the swap prompt, spooling both
        copies of the pair back to back into one interleaved stack.
        """
        cups = Cups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=20))
        screen = ui.RunJob(spec)
        app = make_app([Press.UP, Press.DOWN, Press.UP], cups=cups)
        screen.press(app, Press.OK)
        assert screen.stage == "printing"
        assert len(cups.submitted) == 1, "copy B must wait for the swap prompt"
        assert app.buttons._script == [], "banked presses must be drained"


class TestConfigIsTrusted:
    def test_a_hand_edited_negative_auth_size_cannot_blank_the_pad(self):
        # auth_size < 0 shortens the key draw instead of lengthening it,
        # and at -chars_per_page produces entirely blank pad pages.
        assert config.Settings(auth_size=-700).validate()

    def test_bad_values_fall_back_instead_of_being_used(self, tmp_path):
        path = tmp_path / "otp-unit.conf"
        path.write_text("pages = 0\nauth_size = -700\npaper = A3\na7 = yes\n")
        loaded = config.load(str(path))
        assert loaded.validate() == [], "load() must never return a bad config"
        assert loaded.a7 is True, "the sane fields should survive"
        assert loaded.pages == config.Settings().pages

    def test_load_never_raises_on_any_hand_edited_value(self, tmp_path):
        # load() runs before the panel exists, so anything it raises is a
        # crash loop into systemd's start limit: dark panel, no message.
        # font_size = 0 used to divide by zero inside validate().
        path = tmp_path / "c.conf"
        for line in ("font_size = 0", "font_size = -1", "font_size = 999",
                     "pages = -1", "auth_size = -700", "paper = A3",
                     "a7 = maybe", "pages = ", "nonsense"):
            path.write_text(line + "\n")
            loaded = config.load(str(path))
            assert loaded.validate() == [], line

    def test_a_font_too_wide_for_the_page_is_rejected(self):
        # The panel cannot reach this, but the config file can.
        assert config.Settings(font_size=20).validate()
        assert config.Settings(font_size=9).validate() == []

    def test_letter_quarter_is_measured_against_letter(self):
        # A Letter quarter is shorter than A6; measuring against A6 let key
        # rows fall below the guillotine line.
        assert config.Settings(paper="LETTER", font_size=18).validate()

    def test_a_writable_path_needs_no_remount(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(config, "_remount",
                            lambda d, m: calls.append((d, m)) or True)
        assert config.save(config.Settings(), str(tmp_path / "c.conf"))
        assert calls == [], "no remount should happen when the write succeeds"

    def test_a_failed_write_does_not_leave_the_partition_remounted(self, tmp_path,
                                                                   monkeypatch):
        calls = []
        monkeypatch.setattr(config, "_remount",
                            lambda d, m: calls.append(m) or True)
        monkeypatch.setattr(config, "_write",
                            lambda s, p: (_ for _ in ()).throw(OSError("full")))
        assert config.save(config.Settings(), str(tmp_path / "c.conf")) is False
        # If it remounted rw it must put it back; never a stray one-way ro.
        assert calls in ([], ["rw", "ro"])


class TestQueueQueriesFailSafe:
    """
    An unanswerable queue query must never read as "nothing queued".

    Cups._text swallows a wedged cupsd, a missing lpstat and an unknown
    destination alike. Reporting all of those as an empty queue let both
    job transitions through at once: copy B spooled while A was still
    printing, then the spool purged, then PAIR COMPLETE on the panel over
    an interleaved and truncated pair.
    """

    def _wedged(self):
        def boom(argv, stdin=None):
            raise TimeoutError("cupsd wedged")
        return printer.Cups(run=boom)

    def test_active_jobs_reports_unknown_not_zero(self):
        assert self._wedged().active_jobs() is None

    def test_a_failed_query_counts_as_busy(self):
        app = make_app([])
        app.cups = self._wedged()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "X-Y", config.Settings(pages=1))
        assert ui.RunJob(spec).cups_busy(app) is True

    def test_an_empty_queue_counts_as_drained(self):
        app = make_app([])
        app.cups = Cups(busy=0)
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "X-Y", config.Settings(pages=1))
        assert ui.RunJob(spec).cups_busy(app) is False

    def test_a_wedged_queue_does_not_purge_or_claim_completion(self):
        app = make_app([])
        cups = self._wedged()
        submitted = []
        cups.submit = lambda d, n="OTP", t="OTP", o=None: submitted.append(t) or "j"
        purges = []
        cups.purge = lambda n="OTP": purges.append(n)
        app.cups = cups
        screen = ui.RunJob(jobs.JobSpec(jobs.JobKind.PAD_PAIR, "X-Y",
                                        config.Settings(pages=1)))
        for _ in range(4):
            screen.press(app, Press.OK)
        assert screen.stage == "waiting"
        assert purges == [], "must not purge a queue it cannot read"
        assert submitted == ["OTP A"], "copy B must not start"


class TestAbandonedScreenTellsTheTruth:
    def test_only_a_pad_is_described_as_key_material(self):
        app = make_app([])
        for kind, expect in ((jobs.JobKind.PAD_PAIR, "KEY DISCARDED"),
                             (jobs.JobKind.TABULA, "JOB ABANDONED"),
                             (jobs.JobKind.WORKSHEETS, "JOB ABANDONED")):
            screen = ui.RunJob(jobs.JobSpec(kind, "X-Y", config.Settings(pages=1)))
            screen.stage = "abandoned"
            text = "\n".join(screen.frame(app).rendered())
            assert expect in text, kind
        # And the destroy warning belongs only to the pad.
        card = ui.RunJob(jobs.JobSpec(jobs.JobKind.TABULA, "", config.Settings()))
        card.stage = "abandoned"
        assert "DESTROY" not in "\n".join(card.frame(app).rendered())


class TestManualIsHandledWhenAbsent:
    def test_menu_reports_a_missing_manual_plainly(self, monkeypatch):
        monkeypatch.setattr(jobs, "manual_available", lambda: False)
        app = make_app([])
        menu = ui.main_menu()
        menu.index = [label for label, _ in menu.items].index("PRINT MANUAL")
        screen = menu.press(app, Press.OK)
        assert isinstance(screen, ui.Message)
        assert "NOT INSTALLED" in "\n".join(screen.frame(app).rendered())

    def test_generate_raises_a_readable_error(self, monkeypatch):
        monkeypatch.setattr(jobs, "manual_available", lambda: False)
        spec = jobs.JobSpec(jobs.JobKind.MANUAL, "", config.Settings())
        with pytest.raises(FileNotFoundError) as excinfo:
            jobs.generate(spec)
        assert "NOT INSTALLED" in str(excinfo.value)


class TestCodewordNeverLeavesTheProcess:
    """
    The codeword must be absent from BOTH the job envelope and the document.

    Checking only the envelope is what let the codeword sit in the PDF's
    /Title for a whole review round: page content is Flate-compressed, but
    the document Info dictionary is not, so a `strings` pass over a spooled
    job or a printer's stored copy reads it straight out.
    """

    def _submit_a_pair(self, codeword="RUSTED-BADGER", **settings):
        cups = Cups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, codeword,
                            config.Settings(pages=1, **settings))
        job = jobs.PadPairJob(spec, cups)
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        job.print_next_copy()
        return cups

    def test_the_cups_job_title_is_not_the_codeword(self):
        cups = self._submit_a_pair()
        assert [s["title"] for s in cups.submitted] == ["OTP A", "OTP B"]

    def test_the_pdf_sent_to_the_printer_does_not_contain_the_codeword(self):
        cups = self._submit_a_pair()
        for submission in cups.submitted:
            assert b"RUSTED" not in submission["data"]
            assert b"BADGER" not in submission["data"]

    def test_the_pdf_is_not_dated(self):
        # A timestamp in the metadata dates the pad, and lets a captured
        # printer job be correlated with its twin.
        cups = self._submit_a_pair()
        data = cups.submitted[0]["data"]
        assert b"D:2000" in data, "reportlab's invariant date should be pinned"
        for year in (b"D:2024", b"D:2025", b"D:2026", b"D:2027"):
            assert year not in data

    def test_training_state_does_not_reach_the_printer(self):
        cups = self._submit_a_pair(training=True)
        # The watermark is drawn into the compressed page stream; what must
        # not leak is an uncompressed metadata marker.
        assert b"/Title (OTP)" in cups.submitted[0]["data"]

    def test_every_job_kind_scrubs_its_metadata(self):
        for kind in (jobs.JobKind.WORKSHEETS, jobs.JobKind.TABULA,
                     jobs.JobKind.TEST_PAGE):
            spec = jobs.JobSpec(kind, "", config.Settings(), count=1)
            data = bytes(jobs.generate(spec))
            assert b"D:2000" in data, kind

    def test_both_copies_remain_byte_identical(self):
        # Pinning the metadata must not have introduced per-copy variation.
        cups = self._submit_a_pair()
        assert cups.submitted[0]["data"] == cups.submitted[1]["data"]

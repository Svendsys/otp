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

        # Giving up asks first: this screen invites repeated OK, and a tap
        # held a beat too long would otherwise purge the queue and zero the
        # key with no confirmation.
        screen.press(app, Press.BACK)
        assert screen.stage == "confirm_abandon"
        assert cups.purged == 0, "nothing destroyed until confirmed"
        assert "CANNOT BE" in "\n".join(screen.frame(app).rendered())

        screen.press(app, Press.BACK)               # decline
        assert screen.stage == "waiting"
        assert cups.purged == 0

        screen.press(app, Press.BACK)
        screen.press(app, Press.OK)                 # confirm
        assert screen.stage == "abandoned"
        assert cups.purged == 1, "giving up must still wipe"
        assert "DESTROY" in "\n".join(screen.frame(app).rendered())
        assert screen.press(app, Press.OK) is ui.HOME

    def test_the_waiting_screen_says_which_copy_is_outstanding(self):
        cups = Cups(busy=1)
        app, screen = make_app([]), self._job(cups)
        app.cups = cups
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert "COPY A" in "\n".join(screen.frame(app).rendered())

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

    def test_the_panel_moves_during_an_imposed_job(self):
        """
        The imposed layouts report progress per SHEET, so a `done % 10`
        redraw test fired once -- at the very end, after the panel had sat
        at 0/10 for the whole job.
        """
        cups = Cups()
        app = make_app([], cups=cups)
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=10, paper="A4"))
        screen = ui.RunJob(spec)
        screen.press(app, Press.OK)
        generating = [f for f in app.display.frames if f.title == "GENERATING"]
        assert len(generating) >= 3, "the operator must see it move"
        shown = [f.lines[1] for f in generating]
        assert shown[0] != shown[-1]

    def test_presses_banked_during_spooling_are_discarded(self):
        # submit() blocks on a multi-megabyte PDF. Presses made at the
        # frozen panel were aimed at what was on screen, not at what comes
        # next; replaying them can abandon a pair. Driven at _print_copy
        # directly, because a scripted press cannot be timed to land
        # between generation finishing and spooling starting.
        cups = Cups()
        app = make_app([], cups=cups)
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "X-Y", config.Settings(pages=1))
        screen = ui.RunJob(spec)
        screen.press(app, Press.OK)               # generate + copy A

        screen.press(app, Press.OK)               # tray clear -> swap prompt
        assert screen.stage == "swap"

        app.buttons.push(Press.BACK, Press.OK, Press.UP)
        screen.press(app, Press.OK)               # -> spool copy B
        assert app.buttons._script == [], "banked presses must be discarded"
        assert screen.stage == "printing"
        assert len(cups.submitted) == 2

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


class TestSwapAsksBeforeDestroying:
    """
    The swap screen has the same consequence as the waiting screen -- copy
    A on the tray, the key zeroed, the pair impossible to finish -- and
    round four gave `waiting` a confirmation and left `swap` on a single
    unconfirmed press.
    """

    def _at_swap(self):
        cups = Cups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY",
                            config.Settings(pages=2))
        app, screen = make_app([]), ui.RunJob(spec)
        app.cups = cups
        screen.press(app, Press.OK)        # generate and submit copy A
        screen.press(app, Press.OK)        # queue idle, so copy A is done
        assert screen.stage == "swap"
        return app, screen, cups

    def test_back_at_swap_asks_first(self):
        app, screen, _ = self._at_swap()
        buffer = screen.job._buffer
        screen.press(app, Press.BACK)
        assert screen.stage == "confirm_abandon"
        assert screen.job is not None, "the job must survive an unconfirmed press"
        assert any(buffer), "the key must not be zeroed before confirmation"

    def test_declining_returns_to_swap_not_to_waiting(self):
        # Landing on `waiting` would tell an operator a job is printing
        # when the queue is idle and copy B has not been submitted.
        app, screen, _ = self._at_swap()
        screen.press(app, Press.BACK)
        screen.press(app, Press.BACK)
        assert screen.stage == "swap"

    def test_declining_still_lets_copy_b_print(self):
        app, screen, cups = self._at_swap()
        screen.press(app, Press.BACK)
        screen.press(app, Press.BACK)
        screen.press(app, Press.OK)
        assert len(cups.submitted) == 2

    def test_confirming_destroys_the_key(self):
        # Hold the buffer itself, so this checks that the bytes were
        # overwritten rather than that a reference was dropped.
        app, screen, _ = self._at_swap()
        buffer = screen.job._buffer
        assert any(buffer), "precondition: the key is live"
        screen.press(app, Press.BACK)
        screen.press(app, Press.OK)
        assert screen.stage == "abandoned"
        assert not any(buffer), "the key must be zeroed, not just released"

    def test_declining_from_waiting_still_returns_to_waiting(self):
        # The shared prompt must not have made the two paths converge.
        cups = Cups(busy=3)
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "SILENT-OSPREY",
                            config.Settings(pages=2))
        app, screen = make_app([]), ui.RunJob(spec)
        app.cups = cups
        screen.press(app, Press.OK)
        screen.press(app, Press.OK)
        assert screen.stage == "waiting"
        screen.press(app, Press.BACK)
        assert screen.stage == "confirm_abandon"
        screen.press(app, Press.BACK)
        assert screen.stage == "waiting"


class TestQueueSetupNeverEscapes:
    """
    ensure_queue runs on the boot path, next to a cupsd that is itself
    still starting. App.run catches only PrinterError, so anything else
    unwinds run() into Restart=on-failure and a dark panel.
    """

    def _cups(self, raiser):
        return printer.Cups(run=raiser)

    def test_a_timeout_becomes_a_printer_error(self):
        import subprocess

        def wedged(argv, stdin=None):
            raise subprocess.TimeoutExpired(argv, 120)

        cups = self._cups(wedged)
        device = printer.Device("ipp://localhost/ipp/print", "HP LaserJet")
        with pytest.raises(printer.PrinterError):
            cups.ensure_queue(device)

    def test_a_missing_lpadmin_becomes_a_printer_error(self):
        def absent(argv, stdin=None):
            raise FileNotFoundError(2, "No such file", argv[0])

        cups = self._cups(absent)
        device = printer.Device("usb://HP/LaserJet%20Pro%20M12w", "")
        with pytest.raises(printer.PrinterError):
            cups.ensure_queue(device)


class TestAFailedSetupLeavesNoQueue:
    """
    lpadmin exits non-zero on a bad -m but creates the queue anyway. An
    enabled queue with no driver accepts a whole pad and never prints it,
    and the caller goes on using the same queue name.
    """

    class Recorder:
        def __init__(self, fail_all=True):
            self.calls = []
            self.fail_all = fail_all

        def __call__(self, argv, stdin=None):
            self.calls.append(argv)
            import subprocess as sp
            rc = 1 if self.fail_all else 0
            return sp.CompletedProcess(argv, rc, b"", b"")

    def test_the_queue_is_deleted_when_no_driver_matches(self):
        recorder = self.Recorder()
        cups = printer.Cups(run=recorder)
        with pytest.raises(printer.PrinterError):
            cups.ensure_queue(printer.Device("ipp://localhost/ipp/print", "HP"))
        removals = [c for c in recorder.calls if "-x" in c]
        assert removals, "a failed setup must not leave a queue behind"
        assert removals[-1][-1] == "OTP"

    def test_a_successful_setup_deletes_nothing(self):
        recorder = self.Recorder(fail_all=False)
        cups = printer.Cups(run=recorder)
        assert cups.ensure_queue(
            printer.Device("ipp://localhost/ipp/print", "HP")) == "OTP"
        assert not [c for c in recorder.calls if "-x" in c]


class TestPurgeClearsTheFilterScratchFiles:
    """
    cancel -x empties RequestRoot but not TempDir, and CUPS SIGKILLs the
    filter chain, so Ghostscript leaves its scratch file behind holding
    plaintext key material.
    """

    def test_temp_files_are_removed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(printer, "TEMP_DIR", str(tmp_path))
        leftover = tmp_path / "gs_kLfdpl"
        leftover.write_bytes(b"(MIDPRINT-KILL) AUTH FUIUQ 0001")

        import subprocess as sp
        cups = printer.Cups(
            run=lambda argv, stdin=None: sp.CompletedProcess(argv, 0, b"", b""))
        cups.purge()
        assert not leftover.exists(), "key material left in TempDir"

    def test_a_missing_temp_dir_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(printer, "TEMP_DIR", str(tmp_path / "absent"))
        import subprocess as sp
        cups = printer.Cups(
            run=lambda argv, stdin=None: sp.CompletedProcess(argv, 0, b"", b""))
        cups.purge()        # must not raise

    def test_a_cancel_failure_still_clears_temp(self, tmp_path, monkeypatch):
        # The wipe path must not depend on cancel having worked.
        monkeypatch.setattr(printer, "TEMP_DIR", str(tmp_path))
        leftover = tmp_path / "gs_scratch"
        leftover.write_bytes(b"key")

        def wedged(argv, stdin=None):
            raise OSError("cupsd is gone")

        printer.Cups(run=wedged).purge()
        assert not leftover.exists()

    def test_subdirectories_are_left_alone(self, tmp_path, monkeypatch):
        # Only files. Recursing risks deleting something that is not ours.
        monkeypatch.setattr(printer, "TEMP_DIR", str(tmp_path))
        (tmp_path / "sub").mkdir()
        import subprocess as sp
        printer.Cups(
            run=lambda argv, stdin=None: sp.CompletedProcess(argv, 0, b"", b"")
        ).purge()
        assert (tmp_path / "sub").is_dir()


class TestTheUnitNeverBindsALanPrinter:
    """
    The device is meant to be offline. Binding a printer across the network
    sends every pad it prints to a machine in another room, and nothing on
    the panel names the bound device.

    "Accept dnssd whenever something is plugged into USB" failed in the
    worst direction: it never checked the dnssd entry WAS the plugged-in
    printer, and driverless endpoints sort first.
    """

    USB = "direct usb://Brother/HL-2030%20series?serial=A1B2C3"
    LAN = ("network dnssd://HP%20LaserJet%20MFP%20M428fdw%20%5BABCDEF%5D"
           "._ipp._tcp.local/?uuid=1")
    SAME = ("network dnssd://Brother%20HL-2030%20series%20%5B00AA11%5D"
            "._ipp._tcp.local/?uuid=2")
    LOOPBACK = "network ippusb://HP/LaserJet?serial=9"

    def _devices(self, *lines):
        text = "\n".join(lines)

        class Fake(printer.Cups):
            def _text(self, argv):
                return text

        return [d.uri for d in Fake(run=None).devices()]

    def test_a_lan_printer_is_not_offered_even_with_usb_attached(self):
        uris = self._devices(self.USB, self.LAN)
        assert not any(u.startswith("dnssd://") for u in uris), uris
        assert uris == [self.USB.split()[1]]

    def test_the_attached_printer_is_still_preferred_over_its_usb_entry(self):
        # Same printer on both: the driverless endpoint needs no driver.
        uris = self._devices(self.USB, self.SAME)
        assert uris[0].startswith("dnssd://")
        assert len(uris) == 2

    def test_a_lan_printer_alone_offers_nothing(self):
        assert self._devices(self.LAN) == []

    def test_the_loopback_endpoint_is_always_local(self):
        uris = self._devices(self.LOOPBACK, self.USB)
        assert uris[0].startswith("ippusb://")

    def test_bare_backend_names_are_not_devices(self):
        # lpinfo -v lists backends themselves, which are not URIs.
        assert self._devices("network lpd", "network ipp", "file cups-pdf:/") == []

    def test_an_unparseable_usb_uri_does_not_wave_the_lan_through(self):
        # _pretty returns "" for a URI it cannot read; an empty token list
        # must not match every printer on the network.
        assert self._devices("direct usb://", self.LAN) == ["usb://"]

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

    def test_the_spool_is_not_purged_while_copy_b_is_still_queued(self):
        """
        lp returns once a job is spooled, so "the tray is clear" is a guess.
        Purging then would cancel the rest of copy B and destroy the key,
        leaving a truncated half-pair that cannot be regenerated.
        """
        cups = Cups(busy=3)
        app, screen = make_app([]), self._job(Cups(busy=3))
        app.cups = cups
        screen.press(app, Press.OK)        # generate + copy A
        screen.press(app, Press.OK)        # -> swap
        screen.press(app, Press.OK)        # -> copy B
        screen.press(app, Press.OK)        # operator says tray is clear
        assert screen.stage == "waiting"
        assert cups.purged == 0, "must not purge with jobs still queued"

        cups.busy = 0
        screen.press(app, Press.OK)
        assert screen.stage == "done"
        assert cups.purged == 1

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
    def test_the_cups_job_title_is_not_the_codeword(self):
        cups = Cups()
        spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                            config.Settings(pages=1))
        job = jobs.PadPairJob(spec, cups)
        job.generate(progress=lambda d, t: None)
        job.print_next_copy()
        job.print_next_copy()
        titles = [s["title"] for s in cups.submitted]
        assert titles == ["OTP A", "OTP B"]
        for title in titles:
            assert "RUSTED" not in title and "BADGER" not in title

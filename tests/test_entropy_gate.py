"""What the unit says while the kernel CSPRNG is still being seeded.

`os.urandom` blocks in getrandom() until the CRNG is initialised, so an
unseeded unit cannot print a weak pad, but it can freeze: the panel stops
repainting, or a headless unit prints nothing. A freeze is indistinguishable
from a crash, and power-cycling restarts the wait. So every test asks whether
the unit says something, and keeps saying it, while it waits.

Not covered here: the CRNG in the test environment is always seeded, so the
unseeded kernel is injected at `otp_generator.crng_seeded`. That a real Pi
seeds early is asserted in harness/img-boot.sh.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as gen
from otpunit import config, diagnostics, printer, ui, unattended
from otpunit.hw.buttons import Press
from otpunit.hw.display import FakeDisplay


def seeded_after(polls: int):
    """A CRNG that reports unseeded for `polls` questions, then seeded."""
    asked = [0]

    def probe():
        asked[0] += 1
        return asked[0] > polls
    probe.asked = asked
    return probe


class QuietButtons:
    """Nobody is pressing anything, then the run ends. Not FakeButtons, whose
    exhausted script answers QUIT to every blocking wait and would end a wait
    loop on its first poll.
    """

    def __init__(self, quiet_polls: int, then=Press.QUIT):
        self.answers = [None] * quiet_polls + [then]
        self.polls = 0

    def wait(self, timeout=None):
        # timeout=0 asks "is anything pending?", and with nobody pressing the
        # answer is None, consuming nothing. A non-blocking drain loop runs
        # until it sees None, so falling through to the script or the trailing
        # QUIT would starve or hang it.
        if timeout == 0:
            return None
        self.polls += 1
        return self.answers.pop(0) if self.answers else Press.QUIT

    def close(self):
        pass


class Cups(printer.Cups):
    """A printer that is always there and records what it was sent."""

    def __init__(self):
        super().__init__(run=None)
        self.submitted = []

    def devices(self):
        return [printer.Device("usb://Fake/Laser?serial=1", "Fake Laser")]

    def ensure_queue(self, device, name="OTP"):
        return name

    def active_jobs(self, name="OTP"):
        return 0

    def purge(self, name="OTP"):
        pass

    def submit(self, data, name="OTP", title="OTP", options=None):
        self.submitted.append(title)
        return f"job-{len(self.submitted)}"


def make_app(buttons, cups=None):
    from otpunit import codewords as cw

    # poll_seconds must not be 0: timeout=0 is the drain probe, and the wait
    # loop's own poll would become indistinguishable from it.
    return ui.App(display=FakeDisplay(), buttons=buttons, cups=cups or Cups(),
                  settings=config.Settings(pages=2), vocabulary=cw.Vocabulary(),
                  config_path="/nonexistent", poll_seconds=0.01)


def panel(app) -> str:
    return "\n".join("\n".join(f.rendered()) for f in app.display.frames)


# --- the probe ----------------------------------------------------------


class TestTheProbeAsksTheKernel:
    """The probe is a non-blocking getrandom(). Comparing `entropy_avail` to a
    number tests a pre-5.6 kernel, and drawing a byte to see what happens is
    itself the hang.
    """

    def test_it_asks_getrandom_without_blocking(self, monkeypatch):
        # A probe without GRND_NONBLOCK blocks in the kernel just as the draw
        # it protects would.
        calls = []
        monkeypatch.setattr(gen.os, "getrandom",
                            lambda n, flags=0: calls.append((n, flags)) or b"\0" * n)
        assert gen.crng_seeded() is True
        assert calls, "the probe must ask the kernel, not infer from a file"
        for _, flags in calls:
            assert flags & gen.os.GRND_NONBLOCK, \
                f"getrandom called with flags={flags}: this probe can block"

    def test_eagain_is_the_answer_no(self, monkeypatch):
        def refuses(n, flags=0):
            raise BlockingIOError(11, "Resource temporarily unavailable")

        monkeypatch.setattr(gen.os, "getrandom", refuses)
        assert gen.crng_seeded() is False

    def test_a_running_kernel_reports_seeded(self):
        # Weak but kept: a probe stuck at False would park every unit on the
        # waiting screen, and every other test injects the answer.
        assert gen.crng_seeded() is True

    def test_a_kernel_that_cannot_be_asked_counts_as_seeded(self, monkeypatch):
        # Reading unsupported as seeded costs only the report; the other way
        # parks the operator at a screen that never clears, with the kernel's
        # own gate still underneath.
        def unsupported(n, flags=0):
            raise OSError(38, "Function not implemented")

        monkeypatch.setattr(gen.os, "getrandom", unsupported)
        assert gen.crng_seeded() is True
        # And platforms with no os.getrandom attribute at all.
        monkeypatch.delattr(gen.os, "getrandom")
        assert gen.crng_seeded() is True


class TestTheBitCountIsContextNotAGate:
    def test_it_reads_the_kernel_estimate(self, tmp_path, monkeypatch):
        avail = tmp_path / "entropy_avail"
        avail.write_text("137\n")
        monkeypatch.setattr(gen, "ENTROPY_AVAIL", str(avail))
        assert gen.entropy_bits() == 137

    def test_an_unreadable_estimate_is_none_rather_than_an_exception(
            self, tmp_path, monkeypatch):
        # The progress indicator must not raise when the count is unreadable.
        monkeypatch.setattr(gen, "ENTROPY_AVAIL", str(tmp_path / "absent"))
        assert gen.entropy_bits() is None
        (tmp_path / "garbage").write_text("not a number")
        monkeypatch.setattr(gen, "ENTROPY_AVAIL", str(tmp_path / "garbage"))
        assert gen.entropy_bits() is None

    def test_nothing_waits_on_the_count(self, tmp_path, monkeypatch):
        """Zero bits on a seeded kernel holds nothing up: on a post-5.6 kernel the
        count and readiness are different facts, and a threshold would stall a
        unit getrandom() will serve.
        """
        avail = tmp_path / "entropy_avail"
        avail.write_text("0\n")
        monkeypatch.setattr(gen, "ENTROPY_AVAIL", str(avail))
        assert gen.entropy_bits() == 0
        assert gen.crng_seeded() is True
        assert gen.wait_for_crng(sleep=lambda s: None) == 0.0


class TestTheWaiterSaysSoThenStops:
    def test_a_seeded_kernel_costs_nothing(self, monkeypatch):
        said, slept = [], []
        monkeypatch.setattr(gen, "crng_seeded", lambda: True)
        assert gen.wait_for_crng(on_wait=said.append,
                                 sleep=slept.append) == 0.0
        assert said == [] and slept == []

    def test_it_speaks_once_per_poll_until_the_crng_is_up(self, monkeypatch):
        said, slept = [], []
        monkeypatch.setattr(gen, "crng_seeded", seeded_after(3))
        waited = gen.wait_for_crng(on_wait=said.append, sleep=slept.append,
                                   poll=0.5)
        assert said == [0.0, 0.5, 1.0], "every poll has to be reported"
        assert slept == [0.5, 0.5, 0.5]
        assert waited == 1.5

    def test_it_does_not_return_before_the_crng_is_up(self, monkeypatch):
        # Returning early would hand the caller back to a draw that then blocks
        # anyway.
        probe = seeded_after(4)
        monkeypatch.setattr(gen, "crng_seeded", probe)
        gen.wait_for_crng(sleep=lambda s: None, poll=1.0)
        assert probe.asked[0] == 5
        assert gen.crng_seeded() is True


# --- the panel ----------------------------------------------------------


class TestThePanelKeepsTalkingWhileTheCrngSeeds:
    def test_it_says_waiting_for_entropy_rather_than_freezing(self, monkeypatch):
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(QuietButtons(2))
        assert app.wait_for_entropy() is False
        text = panel(app)
        assert "WAITING FOR ENTROPY" in text
        assert "NOT SEEDED" in text
        # The count, and a hint the operator can act on.
        assert "BITS" in text
        assert "PRESS THE BUTTONS" in text

    def test_it_repaints_on_every_poll(self, monkeypatch):
        """Successive frames differ while waiting. One frame and then a block is a
        dead panel; the spinner is what makes "alive" visible.
        """
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(QuietButtons(5))
        app.wait_for_entropy()
        assert len(app.display.frames) == 6
        footers = [f.footer for f in app.display.frames]
        assert len(set(footers)) > 1, f"the panel never moved: {footers}"

    def test_no_key_material_is_drawn_behind_the_waiting_screen(
            self, monkeypatch):
        # A gate that draws key material would block in its own loop.
        drawn = []
        real = gen.os.urandom
        monkeypatch.setattr(gen.os, "urandom",
                            lambda n: drawn.append(n) or real(n))
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(QuietButtons(4))
        app.wait_for_entropy()
        assert drawn == [], f"drew {drawn} while claiming to wait for entropy"

    def test_the_screen_fits_the_panel_in_every_state(self, monkeypatch):
        app = make_app(QuietButtons(0))
        screen = ui.WaitForEntropy()
        for bits in (None, 0, 256, 4096):
            monkeypatch.setattr(gen, "entropy_bits", lambda b=bits: b)
            for _ in range(4):                   # every spinner phase
                frame = screen.frame(app)
                assert frame.overflowing() == [], frame.overflowing()
                assert all(len(row) == 21 for row in frame.rendered())

    def test_the_operator_can_still_shut_down(self, monkeypatch):
        # The same exit wait_for_printer offers: a unit that never seeds still
        # needs a clean power-off.
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(QuietButtons(1, then=Press.BACK))
        assert app.wait_for_entropy() is False
        assert app.shutdown_requested is True

    def test_the_gate_clears_and_the_menu_appears(self, monkeypatch):
        monkeypatch.setattr(gen, "crng_seeded", seeded_after(2))
        app = make_app(QuietButtons(2))
        app.run()
        frames = [f.rendered() for f in app.display.frames]
        assert "WAITING FOR ENTROPY" in frames[0][0]
        assert "OTP PRINT UNIT" in frames[-1][0], \
            "the wait has to end at the menu, not park there"

    def test_a_seeded_unit_never_shows_the_screen(self, monkeypatch):
        # A gate that fires on a healthy boot would put a new screen before the
        # menu on every power-on.
        monkeypatch.setattr(gen, "crng_seeded", lambda: True)
        app = make_app(QuietButtons(0))
        app.run()
        assert "WAITING FOR ENTROPY" not in panel(app)
        assert app.display.frames, "the menu still has to be drawn"


# --- headless -----------------------------------------------------------


class TestHeadlessPrintsBeforeItWaits:
    """With no panel the printer is the interface, so the status sheet is
    submitted before the first draw, which is where an unseeded kernel stops
    the unit.
    """

    def _run(self, monkeypatch, seeded, events=None):
        events = [] if events is None else events

        class Vocabulary:
            def random(self):
                events.append("roll")
                return "RUSTED-BADGER"

        class Recording(Cups):
            def submit(self, data, name="OTP", title="OTP", options=None):
                events.append(f"submit:{title}")
                return super().submit(data, name, title, options)

        monkeypatch.setattr(gen, "crng_seeded", seeded)
        monkeypatch.setattr(unattended.jobs, "manual_available", lambda: True)
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        cups = Recording()
        result = unattended.run(
            cups, settings=config.Settings(pages=2, auto_delay=0,
                                           auto_swap_delay=0),
            vocabulary=Vocabulary(), sleep=lambda s: None,
            log=lambda line: events.append(f"log:{line}"))
        return cups, events, result

    def test_the_status_sheet_reaches_paper_before_the_codeword_is_rolled(
            self, monkeypatch):
        # Sheet before roll: rolling first means an unseeded unit prints
        # nothing at all.
        cups, events, result = self._run(monkeypatch, seeded_after(3))
        assert result == 0
        assert events.index("submit:OTP status") < events.index("roll"), events

    def test_it_waits_between_the_sheet_and_the_roll(self, monkeypatch):
        cups, events, _ = self._run(monkeypatch, seeded_after(3))
        waiting = [i for i, e in enumerate(events)
                   if e.startswith("log:waiting for the kernel CSPRNG")]
        assert waiting, f"the wait was silent: {events}"
        assert events.index("submit:OTP status") < waiting[0] < \
            events.index("roll")

    def test_the_wait_is_reported_with_the_time_spent(self, monkeypatch):
        _, events, _ = self._run(monkeypatch, seeded_after(3))
        assert any("seeded after" in e for e in events), events

    def test_the_waiting_is_not_logged_on_every_poll(self, monkeypatch):
        # The journal is volatile and lives in RAM, so an unbounded wait must
        # not log on every poll.
        _, events, _ = self._run(monkeypatch, seeded_after(3))
        waiting = [e for e in events
                   if e.startswith("log:waiting for the kernel CSPRNG")]
        assert len(waiting) == 1, f"three polls produced {len(waiting)} lines"

    def test_a_seeded_unit_says_nothing_about_entropy(self, monkeypatch):
        # A healthy run gets no extra line.
        _, events, result = self._run(monkeypatch, lambda: True)
        assert result == 0
        assert not [e for e in events if "CSPRNG" in e], events


# --- the status sheet ---------------------------------------------------


def posture(sections):
    for section in sections:
        if section.title == "SECURITY POSTURE":
            return dict(section.rows)
    raise AssertionError("no SECURITY POSTURE section on the sheet")


class TestTheSheetReportsTheCrngNotABitCount:
    def test_a_seeded_kernel_reads_as_correct(self, monkeypatch):
        monkeypatch.setattr(gen, "crng_seeded", lambda: True)
        row = posture(diagnostics.collect())["Kernel CSPRNG"]
        assert "seeded" in row and "correct" in row
        assert "NOT SEEDED" not in row

    def test_an_unseeded_kernel_is_a_finding_not_a_number(self, monkeypatch):
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        row = posture(diagnostics.collect())["Kernel CSPRNG"]
        assert "NOT SEEDED" in row
        assert "block" in row, "say what happens next, not just the state"

    def test_a_low_count_on_a_seeded_kernel_is_not_reported_as_trouble(
            self, monkeypatch):
        """A low bit count on a seeded kernel is not reported as a problem: it is
        not a readiness signal, and a warning would send an operator to wait
        for nothing.
        """
        monkeypatch.setattr(gen, "crng_seeded", lambda: True)
        monkeypatch.setattr(gen, "entropy_bits", lambda: 12)
        row = posture(diagnostics.collect())["Kernel CSPRNG"]
        assert "12 bits" in row
        assert "LOW" not in row and "NOT SEEDED" not in row

    def test_an_unreadable_estimate_still_prints_a_sheet(self, monkeypatch):
        monkeypatch.setattr(gen, "entropy_bits", lambda: None)
        row = posture(diagnostics.collect())["Kernel CSPRNG"]
        assert "unknown" in row


# --- the wait must not eat, park, or pre-empt -----------------------------


class BankedButtons:
    """A queue, like the real GpioButtons: presses persist until taken. The
    entropy screen asks to be pressed, so banked presses are the case to model.
    """

    def __init__(self, script=()):
        self.queue = list(script)

    def push(self, *presses):
        self.queue.extend(presses)

    def wait(self, timeout=None):
        # None when empty, for both forms: answering QUIT to a blocking poll
        # would end the loop on its first iteration. Termination comes from the
        # crng_seeded probe, and each probe caps its poll count so a broken
        # loop fails, not hangs.
        return self.queue.pop(0) if self.queue else None

    def close(self):
        pass


class TestTheEntropyWaitDoesNotLeakPressesForward:
    def test_presses_made_at_the_waiting_screen_do_not_reach_the_menu(
            self, monkeypatch):
        """Presses banked during the wait are drained before the next screen, so
        an OK aimed at the waiting screen cannot select PRINT PAD PAIR.
        """
        buttons = BankedButtons()
        polls = [0]

        def seeded():
            polls[0] += 1
            if polls[0] > 200:
                raise AssertionError("wait_for_entropy never returned")
            if polls[0] <= 2:
                return False
            # The presses land during the last blocking wait, after the loop's
            # own poll: the only window in which a press can survive to the
            # next screen.
            if not buttons.queue:
                buttons.push(Press.OK, Press.OK)
            return True

        monkeypatch.setattr(gen, "crng_seeded", seeded)
        app = make_app(buttons)
        assert app.wait_for_entropy() is True
        assert buttons.queue == [], \
            f"presses survived the entropy wait and will hit the next " \
            f"screen: {buttons.queue}"

    def test_the_drain_does_not_swallow_the_shutdown(self, monkeypatch):
        # BACK on the waiting screen must still power down: the drain runs only
        # on the way out, once seeded.
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(BankedButtons([Press.BACK]))
        assert app.wait_for_entropy() is False
        assert app.shutdown_requested is True


class TestTheHeadlessWaitKeepsWatchingThePlug:
    def test_unplugging_during_the_wait_aborts(self, monkeypatch):
        """Pulling the printer cable during the entropy wait aborts the run. The
        status sheet promises exactly that, and it is the only control a
        headless unit has.
        """
        events = []

        class Gone(Cups):
            def devices(self):
                events.append("devices")
                return []

        # Bounded, so removing the plug watch fails instead of hanging.
        asked = [0]

        def never(*_a, **_k):
            asked[0] += 1
            if asked[0] > 200:
                raise AssertionError(
                    "the entropy wait ran 200 polls without noticing the "
                    "printer had gone; nothing is watching the plug")
            return False

        monkeypatch.setattr(gen, "crng_seeded", never)
        monkeypatch.setattr(unattended.jobs, "manual_available", lambda: True)
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        result = unattended.run(
            Gone(), settings=config.Settings(pages=2, auto_delay=0,
                                             auto_swap_delay=0),
            vocabulary=type("V", (), {"random": lambda self: "X-Y"})(),
            sleep=lambda s: None,
            log=lambda line: events.append(f"log:{line}"))
        assert result == 1, events
        assert any("aborted" in e and "disconnect" in e for e in events), events

    def test_a_single_missed_answer_does_not_abort(self, monkeypatch):
        """One empty devices() answer does not abort the wait: a busy cupsd looks
        the same as an unplugged cable, so the GONE_AFTER rule applies here
        too.
        """
        waiting = [False]
        blinked = [False]

        class Blinks(Cups):
            def devices(self):
                # One empty answer, and only once the entropy wait is the one
                # asking, so an earlier lookup cannot absorb it.
                if waiting[0] and not blinked[0]:
                    blinked[0] = True
                    return []
                return super().devices()

        def seeded():
            waiting[0] = True
            return blinked[0]

        monkeypatch.setattr(gen, "crng_seeded", seeded)
        monkeypatch.setattr(unattended.jobs, "manual_available", lambda: True)
        monkeypatch.setattr(unattended.jobs, "generate",
                            lambda spec, *a, **k: bytearray(b"%PDF-fake"))
        result = unattended.run(
            Blinks(), settings=config.Settings(pages=2, auto_delay=0,
                                               auto_swap_delay=0),
            vocabulary=type("V", (), {"random": lambda self: "X-Y"})(),
            sleep=lambda s: None, log=lambda line: None)
        assert blinked[0], "the wait never saw the empty answer"
        assert result == 0, "one empty device list aborted a healthy pair"

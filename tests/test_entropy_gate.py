"""What the unit says while the kernel CSPRNG is still being seeded.

Phase 4 specified `rng-tools` plus a boot gate on `entropy_avail`: refuse
to generate until the pool is seeded. Built as specified that gate would
be theatre twice over. `os.urandom` goes through `getrandom()`, which
since Linux 5.6 BLOCKS until the CRNG is initialised rather than handing
back predictable bytes -- so "a pad drawn from an unseeded pool" is not a
state this program can reach. And `entropy_avail` is no longer a level
anybody can wait for: it is clamped to the pool size and sits pinned at
that maximum on any healthy machine (measured: 256, unmoving, on the idle
6.18 kernel these tests run under).

What the plan missed is worse in a different way, and it is what these
tests are for. Measured against this codebase before the gate, with
`os.urandom` made to block exactly as an unseeded kernel makes it block:

  interactive  two frames drawn, then the CODEWORD menu with the caret
               still on ROLL RANDOM, indefinitely. The first draw is
               `Vocabulary.random()`, called from inside
               `CodewordRoll.frame()`, so the block lands BEFORE the
               frame is computed and the panel never repaints again.
  headless     an empty submission list and an empty log. `run()` rolled
               its codeword before submitting the status sheet, so a unit
               whose only output device is a printer printed NOTHING --
               not even the sheet whose whole job is to explain a unit
               with no panel.

On three buttons and 128x64 pixels a freeze is indistinguishable from a
crash, and the documented remedy for a crash -- power-cycle -- discards
the entropy collected so far and restarts the wait. So every test below
asks one question: does the unit say something, and keep saying it, while
it waits?

WHAT IS NOT COVERED HERE. This container's CRNG is seeded and cannot be
unseeded, so the unseeded kernel is injected at the one seam that asks
the question -- `otp_generator.crng_seeded`. That the probe reads a real
kernel correctly is pinned separately against a fake `os.getrandom`; that
a real Pi seeds early enough for none of this to fire is a tier-3 console
assertion in harness/img-boot.sh, not something provable from here.
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
    """
    Nobody is pressing anything, then the run ends.

    Deliberately not FakeButtons: an exhausted script answers QUIT to
    every blocking wait, which ends the very first poll of a wait loop --
    the opposite of the standing-at-a-silent-box case being modelled.
    """

    def __init__(self, quiet_polls: int, then=Press.QUIT):
        self.answers = [None] * quiet_polls + [then]
        self.polls = 0

    def wait(self, timeout=None):
        # timeout=0 asks "is anything pending?", not "give me the next
        # press", and this double models NOBODY PRESSING ANYTHING -- so
        # the honest answer is None, every time, consuming nothing.
        #
        # It matters because a non-blocking drain loop runs until it sees
        # None. Falling through to the scripted answers would let a drain
        # eat the run's whole script; falling through to the trailing QUIT
        # would hand it an endless supply and hang forever, which is
        # exactly what happened when wait_for_entropy started draining.
        # The real GpioButtons honours this and FakeButtons carries the
        # same guard with the same reasoning written above it.
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

    # poll_seconds must NOT be 0. The doubles return immediately whatever
    # it is, so this costs nothing -- but timeout=0 is the wire signal for
    # "is anything pending?", which is what a non-blocking drain sends. At
    # poll_seconds=0 the wait loop's own poll is indistinguishable from a
    # drain probe, so a double that answers one correctly answers the
    # other wrongly, and wait_for_entropy either starves or hangs.
    return ui.App(display=FakeDisplay(), buttons=buttons, cups=cups or Cups(),
                  settings=config.Settings(pages=2), vocabulary=cw.Vocabulary(),
                  config_path="/nonexistent", poll_seconds=0.01)


def panel(app) -> str:
    return "\n".join("\n".join(f.rendered()) for f in app.display.frames)


# --- the probe ----------------------------------------------------------


class TestTheProbeAsksTheKernel:
    """
    The question has exactly one honest form on a modern kernel, and two
    tempting wrong ones: read `entropy_avail` and compare it to a number,
    or draw a byte and see what happens. The first tests a pre-5.6 kernel;
    the second IS the hang.
    """

    def test_it_asks_getrandom_without_blocking(self, monkeypatch):
        # The whole point. A probe that omits GRND_NONBLOCK blocks in the
        # kernel exactly as the draw it is meant to protect would, so the
        # gate becomes the freeze it was written to replace.
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
        # Weak on its own and kept anyway: a probe stuck at False would
        # park every unit on the waiting screen for ever, and no injected
        # test can catch that because they all inject the answer.
        assert gen.crng_seeded() is True

    def test_a_kernel_that_cannot_be_asked_counts_as_seeded(self, monkeypatch):
        # Being wrong here costs the report. Being wrong the other way
        # parks an operator in front of a screen that can never clear,
        # on a platform where the kernel's own gate is still underneath.
        def unsupported(n, flags=0):
            raise OSError(38, "Function not implemented")

        monkeypatch.setattr(gen.os, "getrandom", unsupported)
        assert gen.crng_seeded() is True
        # And the platform that has no getrandom at all -- macOS, the BSDs,
        # anything predating 3.17 -- where the attribute is simply absent.
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
        # It is drawn on a 21-column panel while the unit is already in
        # trouble. A traceback from the progress indicator is not a
        # diagnosis, it is a second fault on top of the first.
        monkeypatch.setattr(gen, "ENTROPY_AVAIL", str(tmp_path / "absent"))
        assert gen.entropy_bits() is None
        (tmp_path / "garbage").write_text("not a number")
        monkeypatch.setattr(gen, "ENTROPY_AVAIL", str(tmp_path / "garbage"))
        assert gen.entropy_bits() is None

    def test_nothing_waits_on_the_count(self, tmp_path, monkeypatch):
        """
        Zero bits on a seeded kernel must not hold anything up.

        This is the Phase-4 gate as specified, and the reason it was not
        built: on a post-5.6 kernel the count and the readiness are
        different facts, and a threshold on the count would stall a unit
        that getrandom() is perfectly willing to serve.
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
        # Returning early would hand the caller back to a draw that then
        # blocks anyway -- the same freeze, with a reassuring log line
        # already printed above it.
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
        # The count, and a hint the operator can act on -- both named in
        # the issue, both the difference between a report and a shrug.
        assert "BITS" in text
        assert "PRESS THE BUTTONS" in text

    def test_it_repaints_on_every_poll(self, monkeypatch):
        """
        A gate that blocks without repainting IS the bug being fixed.

        One frame and then a wait is the same dead panel as before, with
        better wording on it. The spinner is what makes "alive" visible
        from a metre away, so the frames have to differ.
        """
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(QuietButtons(5))
        app.wait_for_entropy()
        assert len(app.display.frames) == 6
        footers = [f.footer for f in app.display.frames]
        assert len(set(footers)) > 1, f"the panel never moved: {footers}"

    def test_no_key_material_is_drawn_behind_the_waiting_screen(
            self, monkeypatch):
        # The gate exists because the draw blocks. A gate that draws
        # anyway blocks in its own loop, and the operator is back where
        # they started -- staring at one frame.
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
        # The same exit wait_for_printer offers. Without it a unit that
        # never seeds has three inert buttons and no clean power-off,
        # which is how an SD card gets corrupted on top of everything.
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
        # A gate that fires on a healthy boot is a new screen between the
        # operator and the menu on every single power-on.
        monkeypatch.setattr(gen, "crng_seeded", lambda: True)
        app = make_app(QuietButtons(0))
        app.run()
        assert "WAITING FOR ENTROPY" not in panel(app)
        assert app.display.frames, "the menu still has to be drawn"


# --- headless -----------------------------------------------------------


class TestHeadlessPrintsBeforeItWaits:
    """
    With no panel the printer IS the interface, so "say something" means
    "put a sheet in the tray" -- and it has to happen before the first
    draw, because the first draw is where the kernel stops the unit.
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
        # The ordering IS the fix. Rolling first meant an unseeded unit
        # printed nothing at all: measured, an empty submission list and
        # an empty log, indefinitely.
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
        # This unit's journal is volatile and lives in RAM. A line every
        # half second for a wait with no upper bound is a slow leak into
        # the one resource a headless unit cannot spare.
        _, events, _ = self._run(monkeypatch, seeded_after(3))
        waiting = [e for e in events
                   if e.startswith("log:waiting for the kernel CSPRNG")]
        assert len(waiting) == 1, f"three polls produced {len(waiting)} lines"

    def test_a_seeded_unit_says_nothing_about_entropy(self, monkeypatch):
        # The whole sequence is printed and logged for someone with no
        # other channel. Adding a line to every healthy run costs that
        # channel its signal.
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
        """
        The row this replaced said "LOW" below 256 bits.

        That is the pre-5.6 model: on a modern kernel the estimate is not
        a readiness signal, and printing a warning about it sends an
        operator to wait for a number that never gates anything.
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


# --- what review found: the wait must not eat, park, or pre-empt --------


class BankedButtons:
    """A queue, like the real GpioButtons -- presses persist until taken.

    QuietButtons models nobody pressing anything, which is the wrong
    double for the banked-press question: the entropy screen is the one
    screen that ASKS to be pressed.
    """

    def __init__(self, script=()):
        self.queue = list(script)

    def push(self, *presses):
        self.queue.extend(presses)

    def wait(self, timeout=None):
        # None when empty, for BOTH forms. A queue that answers QUIT to a
        # blocking poll ends the wait loop on its very first iteration, so
        # the loop under test never runs. Termination comes from the
        # crng_seeded probe instead -- which is what the real loop waits
        # on -- and each probe below caps its own poll count, so a broken
        # loop fails rather than hangs.
        return self.queue.pop(0) if self.queue else None

    def close(self):
        pass


class TestTheEntropyWaitDoesNotLeakPressesForward:
    def test_presses_made_at_the_waiting_screen_do_not_reach_the_menu(
            self, monkeypatch):
        """
        The screen tells the operator that using the buttons helps, so it
        is the one screen guaranteed to leave a queue behind -- and
        GpioButtons is a queue, not a level. Handing those presses to
        whatever is drawn next means a banked OK selecting PRINT PAD PAIR
        from a press aimed at a waiting screen.
        """
        buttons = BankedButtons()
        polls = [0]

        def seeded():
            polls[0] += 1
            if polls[0] > 200:
                raise AssertionError("wait_for_entropy never returned")
            if polls[0] <= 2:
                return False
            # The presses land during the LAST blocking wait -- after the
            # loop's own poll has already been served, which is the only
            # window in which a press can survive to the next screen. A
            # test that queues them earlier has them eaten by the loop
            # itself and passes with the drain removed.
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
        # BACK on the waiting screen must still power down: the drain runs
        # on the way OUT, only once the kernel has seeded.
        monkeypatch.setattr(gen, "crng_seeded", lambda: False)
        app = make_app(BankedButtons([Press.BACK]))
        assert app.wait_for_entropy() is False
        assert app.shutdown_requested is True


class TestTheHeadlessWaitKeepsWatchingThePlug:
    def test_unplugging_during_the_wait_aborts(self, monkeypatch):
        """
        The status sheet is already in the tray telling the operator that
        pulling the cable stops the run -- that is the entire control
        surface this mode has. A wait that ignores it leaves the unit as
        dead as the silent hang this gate replaces, one step further on.
        """
        events = []

        class Gone(Cups):
            def devices(self):
                events.append("devices")
                return []

        # Bounded, so removing the plug watch FAILS instead of hanging: an
        # unbounded wait with nothing watching is precisely the defect, and
        # a test that hangs to report it is unusable in CI.
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
        """
        devices() swallows every error and returns [], so a busy cupsd is
        indistinguishable from an unplugged one. The same GONE_AFTER rule
        countdown() and drain() use, for the same reason: one empty answer
        is a hiccup, several in a row is a cable.
        """
        waiting = [False]
        blinked = [False]

        class Blinks(Cups):
            def devices(self):
                # One empty answer, and only once the entropy wait is the
                # thing asking -- an earlier lookup would absorb it and
                # leave the wait's own tolerance untested.
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

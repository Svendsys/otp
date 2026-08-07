"""Choosing an interface from whatever hardware turned up.

The rule this file protects: display and input are chosen independently,
so an OLED with a USB keyboard and a monitor with three buttons are both
perfectly good front panels. Anything that collapses that back into "the
intended panel or nothing" puts the unit back to being useless without
parts that may not exist.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otpunit import hmi


def force(monkeypatch, *, oled=False, screen=False, keyboard=False,
          gpio=False, console=True):
    """Pin every probe, so the ladder is tested and not the host."""
    def maybe(ok, value):
        def build(*args, **kwargs):
            if not ok:
                raise OSError("not present")
            return value
        return build

    monkeypatch.setattr(hmi, "Ssd1306Display", maybe(oled, "OLED"))
    monkeypatch.setattr(hmi, "GpioButtons", maybe(gpio, "BUTTONS"))
    monkeypatch.setattr(hmi, "KeyboardButtons", lambda *a, **k: "KEYBOARD")
    monkeypatch.setattr(hmi, "ConsoleDisplay", lambda *a, **k: "CONSOLE")
    monkeypatch.setattr(hmi, "screen_connected", lambda: screen)
    monkeypatch.setattr(hmi, "keyboard_connected", lambda: keyboard)
    monkeypatch.setattr(hmi, "console_usable", lambda: console)


class TestTheLadder:
    def test_the_oled_wins_when_it_is_there(self, monkeypatch):
        force(monkeypatch, oled=True, screen=True)
        assert hmi.detect().display == "OLED"

    def test_a_monitor_is_used_when_there_is_no_oled(self, monkeypatch):
        force(monkeypatch, screen=True)
        interface = hmi.detect()
        assert interface.display == "CONSOLE"
        assert interface.display_kind == "HDMI console"

    def test_gpio_buttons_win_when_they_are_there(self, monkeypatch):
        force(monkeypatch, gpio=True, keyboard=True)
        assert hmi.detect().buttons == "BUTTONS"

    def test_a_keyboard_is_used_when_there_are_no_buttons(self, monkeypatch):
        force(monkeypatch, keyboard=True)
        interface = hmi.detect()
        assert interface.buttons == "KEYBOARD"
        assert interface.input_kind == "USB keyboard"


class TestMixedHardwareStillGivesAMenu:
    """The point of choosing the two independently."""

    def test_oled_plus_keyboard_is_interactive(self, monkeypatch):
        force(monkeypatch, oled=True, keyboard=True)
        assert hmi.detect().interactive

    def test_monitor_plus_gpio_buttons_is_interactive(self, monkeypatch):
        force(monkeypatch, screen=True, gpio=True)
        assert hmi.detect().interactive

    def test_the_intended_panel_is_interactive(self, monkeypatch):
        force(monkeypatch, oled=True, gpio=True)
        assert hmi.detect().interactive


class TestFallingBackToPaper:
    def test_nothing_at_all_is_not_interactive(self, monkeypatch):
        force(monkeypatch)
        interface = hmi.detect()
        assert not interface.interactive
        assert interface.display is None and interface.buttons is None

    def test_a_display_with_no_input_is_not_a_menu(self, monkeypatch):
        # A screen nobody can press anything on cannot drive a pad pair.
        force(monkeypatch, oled=True)
        assert not hmi.detect().interactive

    def test_an_input_with_no_display_is_not_a_menu(self, monkeypatch):
        # Buttons with nothing to read are worse than none: every press
        # would be blind.
        force(monkeypatch, gpio=True)
        assert not hmi.detect().interactive

    def test_a_tty_alone_does_not_count_as_a_screen(self, monkeypatch):
        """
        The service binds to tty1, so stdin is a terminal whether or not
        anything is plugged into the HDMI socket. Trusting isatty() would
        make every unit think it had a monitor and sit at a menu nobody
        can see, instead of printing.
        """
        force(monkeypatch, screen=False, keyboard=True, console=True)
        assert hmi.detect().display is None

    def test_a_screen_with_no_usable_console_is_skipped(self, monkeypatch):
        force(monkeypatch, screen=True, console=False)
        assert hmi.detect().display is None


class TestProbesAreHonestAboutTheRealSystem:
    def test_screen_detection_reads_drm_status(self, tmp_path, monkeypatch):
        card = tmp_path / "card0-HDMI-A-1"
        card.mkdir()
        (card / "status").write_text("disconnected\n")
        monkeypatch.setattr(hmi.glob, "glob",
                            lambda p: [str(card / "status")])
        assert hmi.screen_connected() is False
        (card / "status").write_text("connected\n")
        assert hmi.screen_connected() is True

    def test_missing_drm_tree_is_not_fatal(self, monkeypatch):
        monkeypatch.setattr(hmi.glob, "glob", lambda p: ["/nonexistent/status"])
        assert hmi.screen_connected() is False

    def test_keyboard_detection_uses_udev_symlinks(self, monkeypatch):
        monkeypatch.setattr(hmi.glob, "glob",
                            lambda p: ["/dev/input/by-id/usb-Foo-event-kbd"]
                            if "by-id" in p else [])
        assert hmi.keyboard_connected() is True

    def test_no_keyboard_anywhere_reads_false(self, monkeypatch):
        monkeypatch.setattr(hmi.glob, "glob", lambda p: [])
        monkeypatch.setattr(hmi, "_read",
                            lambda p: (_ for _ in ()).throw(OSError()))
        assert hmi.keyboard_connected() is False

    def test_detection_never_raises(self, monkeypatch):
        """
        This runs before anything else on a cold unit. A probe that throws
        takes the whole device down with no panel to say why.
        """
        def explode(*args, **kwargs):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(hmi.glob, "glob", explode)
        monkeypatch.setattr(hmi, "Ssd1306Display", explode)
        monkeypatch.setattr(hmi, "GpioButtons", explode)
        interface = hmi.detect()
        assert interface.describe()


class TestClosingUp:
    def test_close_survives_a_part_that_throws(self):
        class Rude:
            def close(self):
                raise OSError("nope")

        hmi.Interface(display=Rude(), buttons=Rude()).close()

    def test_close_handles_missing_parts(self):
        hmi.Interface().close()


class TestAnInterfaceNobodyAnswersIsNotAnInterface:
    """
    Opening GpioButtons proves only that a pin could be reserved. gpiozero
    has no presence detection, so on any Pi with lgpio the constructor
    always succeeds -- tests/test_hardware.py builds one against a mock
    factory with nothing wired and asserts exactly that.

    Without a liveness check, attaching a monitor to a working headless
    unit made `interactive` True and sent it into a menu whose first
    wait() blocks forever. No pad, no log line, no fallback.
    """

    class Silent:
        def wait(self, timeout=None):
            return None

    class Answers:
        def __init__(self):
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            return "PRESS" if self.calls > 1 else None

    class Broken:
        def wait(self, timeout=None):
            raise OSError(121, "Remote I/O error")

    def _interface(self, buttons):
        return hmi.Interface(display=object(), buttons=buttons,
                             display_kind="HDMI console",
                             input_kind="GPIO buttons")

    def test_silence_means_not_drivable(self):
        clock = [0.0]

        def tick(_):
            clock[0] += 0.5
        assert self._interface(self.Silent()).prove(
            seconds=5, sleep=tick, clock=lambda: clock[0]) is False

    def test_a_press_proves_it(self):
        clock = [0.0]
        assert self._interface(self.Answers()).prove(
            seconds=5, sleep=lambda _: clock.__setitem__(0, clock[0] + 0.5),
            clock=lambda: clock[0]) is True

    def test_it_gives_up_rather_than_waiting_forever(self):
        # The bug was an unbounded wait. Pin that this returns.
        clock = [0.0]

        def tick(_):
            clock[0] += 1.0
        assert self._interface(self.Silent()).prove(
            seconds=3, sleep=tick, clock=lambda: clock[0]) is False
        assert clock[0] >= 3

    def test_a_broken_button_layer_does_not_hang(self):
        assert self._interface(self.Broken()).prove(seconds=5) is False

    def test_nothing_to_press_is_not_drivable(self):
        assert hmi.Interface(display=object()).prove(seconds=5) is False

"""The front-panel UI: a stack of screens driven by three buttons.

Every screen takes a Press and returns the next screen (or None to pop).
That keeps the whole flow synchronous and testable -- a test can drive a
scripted button sequence through a real App with fake hardware and assert on
what the panel would show.
"""
from __future__ import annotations

from dataclasses import replace

from . import codewords as cw
from . import jobs as jobs_mod
from . import printer as printer_mod
from .config import (AUTH_SIZE_CHOICES, PAGE_CHOICES, PAPER_CHOICES,
                     PAPER_LABELS, Settings, save)
from .hw.buttons import Press
from .hw.display import Frame

VERSION = "1.0"

# Returned by a screen to unwind the whole stack back to the main menu.
# Without this, any flow that pushes screens on the way to a job (picking a
# codeword, typing one in) has no route home: a screen can only pop itself,
# and the screens underneath it may not be reachable in any button sequence.
HOME = object()


def wrap(text: str, width: int = 21, lines: int = 4) -> list[str]:
    """
    Break a message across panel rows on word boundaries.

    Slicing text into fixed 21-character chunks drops whatever does not fit
    with no ellipsis, which turns a long driver error into a sentence that
    stops mid-word and reads as if that were the whole message.
    """
    words, out, row = text.split(), [], ""
    for word in words:
        candidate = f"{row} {word}".strip()
        if len(candidate) <= width:
            row = candidate
            continue
        if row:
            out.append(row)
        row = word if len(word) <= width else word[:width - 1] + "…"
        if len(out) == lines:
            break
    if row and len(out) < lines:
        out.append(row)
    if len(out) == lines and len("".join(words)) > len("".join(out).replace(" ", "")):
        out[-1] = out[-1][:width - 1] + "…"
    return out[:lines]


class Screen:
    """Base screen. Subclasses render a Frame and handle presses."""

    def frame(self, app) -> Frame:
        raise NotImplementedError

    def press(self, app, press: Press):
        return self


class Menu(Screen):
    """A scrolling list of labelled actions."""

    title = ""

    def __init__(self, items):
        self.items = list(items)
        self.index = 0

    def labels(self, app) -> list[str]:
        return [label for label, _ in self.items]

    def frame(self, app) -> Frame:
        labels = self.labels(app)
        window = 5
        top = max(0, min(self.index - window // 2, len(labels) - window))
        visible = labels[top:top + window]
        return Frame(
            title=self.title,
            lines=visible,
            selected=self.index - top,
            footer=f"{self.index + 1}/{len(labels)}" if len(labels) > window else "",
        )

    def press(self, app, press):
        if press is Press.UP:
            self.index = (self.index - 1) % len(self.items)
        elif press is Press.DOWN:
            self.index = (self.index + 1) % len(self.items)
        elif press is Press.OK:
            return self.items[self.index][1](app)
        elif press is Press.BACK:
            return None
        return self


class Message(Screen):
    """
    A static screen dismissed with OK.

    BACK always just dismisses. It must never trigger on_ok: the only
    consequential Message is the shutdown confirmation, and a screen that
    powers the unit off whichever button you press is not a confirmation.
    """

    def __init__(self, title, lines, on_ok=None, footer="OK TO CONTINUE"):
        self.title = title
        self.lines = lines
        self.on_ok = on_ok
        self.footer = footer

    def frame(self, app):
        return Frame(title=self.title, lines=self.lines, footer=self.footer)

    def press(self, app, press):
        if press is Press.OK:
            return self.on_ok(app) if self.on_ok else None
        if press is Press.BACK:
            return None
        return self


class Chooser(Screen):
    """Pick one value from a list; UP/DOWN cycle, OK commits."""

    def __init__(self, title, options, current, on_choose, render=str):
        self.title = title
        self.options = list(options)
        self.render = render
        self.on_choose = on_choose
        self.index = self.options.index(current) if current in self.options else 0

    def frame(self, app):
        window = 5
        top = max(0, min(self.index - window // 2, len(self.options) - window))
        visible = [self.render(o) for o in self.options[top:top + window]]
        return Frame(title=self.title, lines=visible, selected=self.index - top,
                     footer=f"{self.index + 1}/{len(self.options)}")

    def press(self, app, press):
        if press is Press.UP:
            self.index = (self.index - 1) % len(self.options)
        elif press is Press.DOWN:
            self.index = (self.index + 1) % len(self.options)
        elif press is Press.OK:
            return self.on_choose(app, self.options[self.index])
        elif press is Press.BACK:
            return None
        return self


class TextEntry(Screen):
    """
    A-Z entry on three buttons: UP/DOWN change the letter, OK commits it and
    moves on, BACK (long OK) finishes the word.

    The alphabet carries a trailing backspace symbol. Selecting it and
    pressing OK deletes the previous letter, and deleting past the start
    leaves the screen. Without that there is no way out at all: BACK means
    "done" here, so every press either stays put or pushes another screen,
    and the operator is left in the letter picker with no route back to the
    menu and no keyboard to escape with.
    """

    DELETE = "<"
    ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + DELETE

    def __init__(self, title, on_done, maxlen=8):
        self.title = title
        self.on_done = on_done
        self.maxlen = maxlen
        self.letters = ["A"]

    @property
    def value(self) -> str:
        return "".join(ch for ch in self.letters if ch != self.DELETE)

    def frame(self, app):
        shown = "".join(self.letters)
        cursor = " " * (len(self.letters) - 1) + "^"
        return Frame(
            title=self.title,
            lines=[shown, cursor, "", f"UP/DN PICK  '{self.DELETE}'=DEL"],
            footer="HOLD OK WHEN DONE",
        )

    def press(self, app, press):
        current = self.ALPHABET.index(self.letters[-1])
        size = len(self.ALPHABET)
        if press is Press.UP:
            self.letters[-1] = self.ALPHABET[(current - 1) % size]
        elif press is Press.DOWN:
            self.letters[-1] = self.ALPHABET[(current + 1) % size]
        elif press is Press.OK:
            if self.letters[-1] == self.DELETE:
                self.letters.pop()
                if not self.letters:
                    return None          # deleted past the start: leave
                return self
            if len(self.letters) < self.maxlen:
                self.letters.append("A")
        elif press is Press.BACK:
            if not self.value:
                return None
            return self.on_done(app, self.value)
        return self


# --- codeword selection -------------------------------------------------


class CodewordRoll(Screen):
    """The fast path: roll a codeword, OK to accept, DOWN to reroll."""

    def __init__(self, on_choose):
        self.on_choose = on_choose
        self.current = ""

    def frame(self, app):
        if not self.current:
            self.current = app.vocabulary.random()
        modifier, noun = cw.split(self.current)
        return Frame(
            title="CODEWORD",
            lines=[modifier, noun, "", "OK ACCEPT  DN REROLL"],
            footer=f"{app.vocabulary.combinations:,} POSSIBLE",
        )

    def press(self, app, press):
        if press is Press.DOWN or press is Press.UP:
            self.current = app.vocabulary.random()
        elif press is Press.OK:
            return self.on_choose(app, self.current)
        elif press is Press.BACK:
            return None
        return self


def codeword_menu(on_choose):
    """Roll / browse / type -- the three ways to land on a codeword."""

    def browse(app):
        # Browsing narrows the space: one category is ~18 nouns against 642,
        # and human choice within it is not uniform. Two pads from the same
        # cell both named after birds is an attribution signal. ROLL RANDOM
        # is the default for that reason.
        def pick_category(app, category):
            def pick_noun(app, noun):
                return on_choose(app, cw.join(app.vocabulary.random_modifier(), noun))

            return Chooser(category.upper()[:21], app.vocabulary.nouns(category),
                           None, pick_noun)

        return Chooser("CATEGORY", app.vocabulary.categories, None, pick_category,
                       render=str.upper)

    def type_in(app):
        def got_modifier(app, modifier):
            def got_noun(app, noun):
                return on_choose(app, cw.join(modifier, noun))

            return TextEntry("NOUN", got_noun)

        return TextEntry("MODIFIER", got_modifier)

    menu = Menu([
        ("ROLL RANDOM", lambda app: CodewordRoll(on_choose)),
        ("BROWSE CATEGORY", browse),
        ("TYPE IT IN", type_in),
    ])
    menu.title = "CODEWORD"
    return menu


# --- jobs ---------------------------------------------------------------


class RunJob(Screen):
    """
    Generate, then print each copy, prompting between them.

    Steps are explicit so the panel updates between them and the operator
    can lift copy A off the tray before copy B starts.
    """

    def __init__(self, spec):
        self.spec = spec
        self.job = None
        self.stage = "confirm"
        self.done_pages = 0
        self.error = ""

    def frame(self, app):
        spec = self.spec
        if self.stage == "confirm":
            if spec.kind is jobs_mod.JobKind.PAD_PAIR:
                settings = spec.settings
                lines = [
                    "PAD PAIR: A AND B",
                    # The codeword runs to 17 characters, so it gets a line
                    # of its own rather than sharing one with a label.
                    spec.codeword,
                    f"{settings.pages} PAGES  {settings.format_label}",
                    # Sheets, not pages: it is what tells you whether there
                    # is enough paper in the tray for both copies.
                    f"{settings.sheets * 2} SHEETS TOTAL",
                    f"AUTH {settings.auth_size if settings.with_auth else 'OFF'}",
                    # Always stated, never blank. The absence of a word is
                    # not a signal an operator can rely on at the last
                    # checkpoint before a thousand pages of key material.
                    "*** TRAINING ***" if settings.training else "LIVE KEY MATERIAL",
                ]
            else:
                lines = [spec.kind.value.upper(), f"{spec.count} COPIES"]
            return Frame(title="CONFIRM", lines=lines, footer="OK START  HOLD CANCEL")

        if self.stage == "generating":
            total = self.spec.settings.pages if self.spec.settings else 1
            return Frame(
                title="GENERATING",
                lines=[self.spec.codeword or self.spec.kind.value.upper(),
                       f"{self.done_pages}/{total} PAGES"],
                progress=self.done_pages / total if total else 0.0,
                footer="HOLD OK TO CANCEL",
            )

        if self.stage == "swap":
            return Frame(
                title="COPY A DONE",
                lines=["REMOVE THE STACK", "AND KEEP IT TOGETHER", "",
                       "OK TO PRINT COPY B"],
                footer="HOLD TO ABANDON",
            )

        if self.stage == "waiting":
            return Frame(
                title="STILL PRINTING",
                lines=["THE PRINTER IS STILL", "WORKING THROUGH THE",
                       "QUEUE. WAIT FOR IT.", "", "OK TO CHECK AGAIN"],
            )

        if self.stage == "abandoned":
            return Frame(
                title="ABANDONED",
                lines=["KEY DISCARDED", "", "DESTROY ANY PAGES",
                       "ALREADY PRINTED"],
                footer="OK TO RETURN",
            )

        if self.stage == "printing":
            letter = "AB"[max(0, self.job.copies_done - 1)] if self.spec.copies > 1 else ""
            return Frame(title="PRINTING",
                         lines=[f"COPY {letter}".strip() or "IN PROGRESS",
                                self.spec.codeword or ""],
                         footer="OK WHEN TRAY IS CLEAR")

        if self.stage == "cancelled":
            return Frame(title="CANCELLED", lines=["NOTHING WAS PRINTED",
                                                   "KEY DISCARDED"],
                         footer="OK TO RETURN")

        if self.stage == "error":
            lines = wrap(self.error, lines=3)
            # If copy A is already on the tray, the operator is holding live
            # key material for a pad that can never be completed. Say so --
            # silently discarding the key and returning to the menu leaves
            # them with a half-pair they may not realise is dangerous.
            if self.spec.carries_key_material and self.job and self.job.copies_done:
                lines = lines[:2] + ["DESTROY PRINTED PAGES"]
            return Frame(title="ERROR", lines=lines, footer="OK TO RETURN")

        # 21 columns is the whole panel: every string here has to fit.
        # Deliberately not "KEY WIPED FROM RAM". The buffer is zeroed, but
        # reportlab's intermediates and the immutable bytes handed to the
        # subprocess are not and cannot be, so copies remain resident until
        # the memory is reused. Power-off is the real wipe; say that.
        lines = ["PAIR COMPLETE", "", "POWER-CYCLE PRINTER", "THEN THIS UNIT"] \
            if self.spec.carries_key_material else ["DONE"]
        return Frame(title="FINISHED", lines=lines, footer="OK TO RETURN")

    def press(self, app, press):
        if self.stage == "confirm":
            if press is Press.BACK:
                return None
            if press is not Press.OK:
                return self
            return self._generate(app)

        if self.stage == "swap":
            if press is Press.BACK:
                self._wipe()
                self.stage = "abandoned"
                return self
            if press is Press.OK:
                return self._print_copy(app)
            return self

        if self.stage in ("printing", "waiting"):
            if press in (Press.OK, Press.BACK):
                return self._advance(app)
            return self

        if self.stage in ("done", "error", "cancelled", "abandoned"):
            if press in (Press.OK, Press.BACK):
                self._wipe()
                # HOME, not None: the stack below may include codeword
                # screens the operator cannot navigate out of, and dropping
                # one frame would leave them mid-flow with a finished job.
                return HOME
        return self

    def _wipe(self):
        """Wipe once, and never let a wipe failure escape and kill the UI."""
        if self.job is None:
            return
        try:
            self.job.finish()
        except Exception:
            pass
        self.job = None

    def _generate(self, app):
        self.stage = "generating"
        app.render(self)
        self.job = jobs_mod.PadPairJob(self.spec, app.cups, app.queue)
        cancelled = []

        def progress(done, _total):
            self.done_pages = done
            if done % 10 == 0:
                app.render(self)

        def should_cancel():
            # Generation blocks the event loop, so presses made during it
            # would otherwise queue up and replay afterwards -- skipping the
            # swap prompt and spooling both copies of a pair back to back.
            # Draining here both honours the advertised cancel and stops
            # those presses arriving late.
            while True:
                press = app.buttons.wait(timeout=0)
                if press is None:
                    return bool(cancelled)
                if press in (Press.BACK, Press.QUIT):
                    cancelled.append(press)

        try:
            self.job.generate(progress=progress, should_cancel=should_cancel)
        except gen_cancelled():
            self.job.finish()
            self.stage = "cancelled"
            return self
        except Exception as exc:
            self.job.finish()
            self.stage = "error"
            self.error = f"GENERATE FAILED: {exc}"
            return self
        return self._print_copy(app)

    def _print_copy(self, app):
        try:
            self.job.print_next_copy()
        except Exception as exc:
            self.stage = "error"
            self.error = str(exc)
            return self
        self.stage = "printing"
        return self

    def _advance(self, app):
        if not self.job.done:
            self.stage = "swap"
            return self
        # lp returns as soon as a job is spooled, so "the tray is clear" is
        # the operator's guess. Purging while copy B is still queued would
        # cancel the rest of it and destroy the key, leaving a truncated
        # half of a pair that can never be regenerated.
        try:
            if self.cups_busy(app):
                self.stage = "waiting"
                return self
        except Exception:
            pass
        self._wipe()
        self.stage = "done"
        return self

    def cups_busy(self, app) -> bool:
        return app.cups.active_jobs(app.queue) > 0


def gen_cancelled():
    import otp_generator

    return otp_generator.GenerationCancelled


# --- settings -----------------------------------------------------------


def settings_menu():
    def set_pages(app):
        return Chooser("PAGES", PAGE_CHOICES, app.settings.pages,
                       lambda a, v: _apply(a, pages=v))

    def set_format(app):
        return Chooser("FORMAT", [False, True], app.settings.a7,
                       lambda a, v: _apply(a, a7=v),
                       render=lambda v: "A7 2-UP (CUT)" if v else "A6 ONE PER SHEET")

    def set_auth(app):
        current = app.settings.auth_size if app.settings.with_auth else 0
        return Chooser("AUTH GROUP", AUTH_SIZE_CHOICES, current,
                       lambda a, v: _apply(a, with_auth=v > 0,
                                           auth_size=v or a.settings.auth_size),
                       render=lambda v: "OFF" if v == 0 else f"{v} LETTERS")

    def set_training(app):
        return Chooser("TRAINING", [False, True], app.settings.training,
                       lambda a, v: _apply(a, training=v),
                       render=lambda v: "TRAINING (MARKED)" if v else "LIVE MATERIAL")

    def set_paper(app):
        return Chooser("PAPER IN TRAY", PAPER_CHOICES, app.settings.paper,
                       lambda a, v: _apply(a, paper=v),
                       render=lambda v: PAPER_LABELS[v])

    menu = SettingsMenu([
        ("PAGES", set_pages),
        ("PAPER", set_paper),
        ("FORMAT", set_format),
        ("AUTH GROUP", set_auth),
        ("TRAINING", set_training),
        ("SAVE SETTINGS", _save),
    ])
    menu.title = "SETTINGS"
    return menu


class SettingsMenu(Menu):
    """
    A menu that says when a change has not been written to the card.

    This matters because the unit tells the operator to power-cycle after
    every pad job, and an unsaved TRAINING setting reverts to LIVE on the
    next boot -- so the next "training" batch would print unwatermarked and
    be indistinguishable from live key material.
    """

    def labels(self, app):
        out = [label for label, _ in self.items]
        if not getattr(app, "settings_saved", True):
            out[-1] = "SAVE SETTINGS  *"
        return out

    def frame(self, app):
        frame = super().frame(app)
        if not getattr(app, "settings_saved", True):
            frame.title = "SETTINGS  * UNSAVED"
        return frame


def _apply(app, **changes):
    candidate = replace(app.settings, **changes)
    problems = candidate.validate()
    if problems:
        # Do not commit a value the unit cannot use.
        return Message("INVALID", wrap(problems[0]))
    app.settings = candidate
    app.settings_saved = False
    return None


def _save(app):
    ok = save(app.settings, app.config_path)
    app.settings_saved = ok
    return Message("SETTINGS", ["SAVED"] if ok else
                   ["COULD NOT WRITE", "BOOT PART READ-ONLY"])


# --- top level ----------------------------------------------------------


def main_menu():
    def pad_pair(app):
        def chosen(app, codeword):
            spec = jobs_mod.JobSpec(jobs_mod.JobKind.PAD_PAIR, codeword, app.settings)
            return RunJob(spec)

        return codeword_menu(chosen)

    def simple(kind, count=1):
        return lambda app: RunJob(jobs_mod.JobSpec(kind, "", app.settings, count))

    def manual(app):
        # The manual is rendered at image-build time; a source install has
        # none. Say so plainly rather than failing with a truncated errno.
        if not jobs_mod.manual_available():
            return Message("MANUAL", ["NOT INSTALLED ON THIS", "UNIT"])
        return RunJob(jobs_mod.JobSpec(jobs_mod.JobKind.MANUAL, "", app.settings))

    menu = Menu([
        ("PRINT PAD PAIR", pad_pair),
        ("PRINT WORKSHEETS", simple(jobs_mod.JobKind.WORKSHEETS, 10)),
        ("PRINT TABULA RECTA", simple(jobs_mod.JobKind.TABULA, 2)),
        ("PRINT MANUAL", manual),
        ("PRINT TEST PAGE", simple(jobs_mod.JobKind.TEST_PAGE)),
        ("SETTINGS", lambda app: settings_menu()),
        ("SHUT DOWN", lambda app: Message(
            "SHUT DOWN", ["POWER OFF THE UNIT?", "", "WAIT FOR THE LED",
                          "BEFORE PULLING POWER"],
            on_ok=lambda a: a.request_shutdown(),
            footer="OK=YES  HOLD=NO")),
    ])
    menu.title = "OTP PRINT UNIT"
    return menu


class WaitForPrinter(Screen):
    """Shown until CUPS can see a USB printer."""

    def __init__(self):
        self.spinner = 0

    def frame(self, app):
        self.spinner = (self.spinner + 1) % 4
        return Frame(
            title="OTP PRINT UNIT",
            lines=["", "PLUG IN A USB PRINTER", "",
                   "|/-\\"[self.spinner] + " SEARCHING",
                   "HOLD OK TO SHUT DOWN"],
            footer=f"v{VERSION}",
        )

    def press(self, app, press):
        return self


class App:
    """Owns the screen stack and the hardware handles."""

    def __init__(self, display, buttons, cups=None, settings=None,
                 vocabulary=None, config_path=None, poll_seconds=2.0):
        from .config import CONFIG_PATH

        self.display = display
        self.buttons = buttons
        self.cups = cups or printer_mod.Cups()
        self.settings = settings or Settings()
        self.vocabulary = vocabulary or cw.Vocabulary()
        self.config_path = config_path or CONFIG_PATH
        self.queue = printer_mod.QUEUE
        self.poll_seconds = poll_seconds
        self.stack: list[Screen] = []
        self.running = True
        self.shutdown_requested = False
        # Settings start matching whatever was loaded from the card.
        self.settings_saved = True

    def render(self, screen=None) -> None:
        target = screen or (self.stack[-1] if self.stack else None)
        if target is not None:
            self.display.show(target.frame(self))

    def request_shutdown(self):
        self.shutdown_requested = True
        self.running = False
        return None

    def wait_for_printer(self) -> bool:
        """Block on the waiting screen until a printer appears."""
        screen = WaitForPrinter()
        while self.running:
            if self.cups.devices():
                return True
            self.render(screen)
            press = self.buttons.wait(timeout=self.poll_seconds)
            if press is Press.QUIT:
                self.running = False
            elif press is Press.BACK:
                # An unsupported or dead printer would otherwise leave the
                # operator on this screen with every button inert and no way
                # to power down cleanly except pulling the plug.
                self.request_shutdown()
        return False

    def run(self) -> None:
        if not self.wait_for_printer():
            return
        # The main menu is always the bottom of the stack, even when setup
        # failed: an operator who dismisses a printer error still needs a
        # route to SETTINGS and SHUT DOWN.
        self.stack = [main_menu()]
        try:
            device = self.cups.devices()[0]
            self.queue = self.cups.ensure_queue(device)
        except (IndexError, printer_mod.PrinterError) as exc:
            self.stack.append(Message("PRINTER", wrap(str(exc))))

        while self.running and self.stack:
            self.render()
            press = self.buttons.wait()
            if press is None:
                continue
            if press is Press.QUIT:
                break
            current = self.stack[-1]
            nxt = current.press(self, press)
            if nxt is HOME:
                self.stack = [main_menu()]
            elif nxt is None:
                # Never pop the last screen. This is a headless appliance
                # with three buttons: an empty stack ends run(), the panel
                # goes dark and every button stops responding, with nothing
                # short of a power cycle to recover. A back press at the
                # root menu must simply do nothing.
                if len(self.stack) > 1:
                    self.stack.pop()
            elif nxt is not current:
                self.stack.append(nxt)

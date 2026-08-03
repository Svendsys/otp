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
    """A static screen dismissed with OK."""

    def __init__(self, title, lines, on_ok=None):
        self.title = title
        self.lines = lines
        self.on_ok = on_ok

    def frame(self, app):
        return Frame(title=self.title, lines=self.lines, footer="OK TO CONTINUE")

    def press(self, app, press):
        if press in (Press.OK, Press.BACK):
            return self.on_ok(app) if self.on_ok else None
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
    A-Z entry on three buttons: UP/DOWN change the letter, OK advances,
    BACK (long OK) finishes. For reproducing a codeword agreed elsewhere.
    """

    ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def __init__(self, title, on_done, maxlen=8):
        self.title = title
        self.on_done = on_done
        self.maxlen = maxlen
        self.letters = ["A"]

    @property
    def value(self) -> str:
        return "".join(self.letters)

    def frame(self, app):
        cursor = " " * (len(self.letters) - 1) + "^"
        return Frame(
            title=self.title,
            lines=[self.value, cursor, "", "UP/DN LETTER  OK NEXT"],
            footer="HOLD OK WHEN DONE",
        )

    def press(self, app, press):
        current = self.ALPHABET.index(self.letters[-1])
        if press is Press.UP:
            self.letters[-1] = self.ALPHABET[(current - 1) % 26]
        elif press is Press.DOWN:
            self.letters[-1] = self.ALPHABET[(current + 1) % 26]
        elif press is Press.OK:
            if len(self.letters) < self.maxlen:
                self.letters.append("A")
        elif press is Press.BACK:
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
            lines = [spec.kind.value.upper()]
            # The codeword runs to 17 characters, so it gets a line of its own
            # rather than sharing one with a label.
            if spec.codeword:
                lines.append(spec.codeword)
            if spec.kind is jobs_mod.JobKind.PAD_PAIR:
                settings = spec.settings
                lines += [
                    f"{settings.pages} PAGES  {settings.format_label}",
                    # Sheets, not pages: it is what tells you whether there
                    # is enough paper in the tray for both copies.
                    f"{settings.sheets * 2} SHEETS TOTAL",
                    f"AUTH {settings.auth_size if settings.with_auth else 'OFF'}"
                    + ("  TRAINING" if settings.training else ""),
                    "2 COPIES: A AND B",
                ]
            else:
                lines.append(f"{spec.count} COPIES")
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

        if self.stage == "printing":
            letter = "AB"[max(0, self.job.copies_done - 1)] if self.spec.copies > 1 else ""
            return Frame(title="PRINTING",
                         lines=[f"COPY {letter}".strip() or "IN PROGRESS",
                                self.spec.codeword or ""],
                         footer="OK WHEN TRAY IS CLEAR")

        if self.stage == "error":
            return Frame(title="ERROR", lines=[self.error[:21], self.error[21:42]],
                         footer="OK TO RETURN")

        # 21 columns is the whole panel: every string here has to fit.
        lines = ["PAIR COMPLETE", "KEY WIPED FROM RAM", "", "POWER-CYCLE PRINTER"] \
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
                self.job.finish()
                return None
            if press is Press.OK:
                return self._print_copy(app)
            return self

        if self.stage == "printing":
            if press in (Press.OK, Press.BACK):
                return self._advance(app)
            return self

        if self.stage in ("done", "error"):
            if press in (Press.OK, Press.BACK):
                if self.job:
                    self.job.finish()
                return None
        return self

    def _generate(self, app):
        self.stage = "generating"
        app.render(self)
        self.job = jobs_mod.PadPairJob(self.spec, app.cups, app.queue)

        def progress(done, _total):
            self.done_pages = done
            if done % 10 == 0:
                app.render(self)

        try:
            self.job.generate(progress=progress)
        except gen_cancelled() as exc:  # pragma: no cover - needs a live button
            self.stage = "error"
            self.error = str(exc)
            return self
        except Exception as exc:
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
        if self.job.done:
            self.job.finish()
            self.stage = "done"
        else:
            self.stage = "swap"
        return self


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

    menu = Menu([
        ("PAGES", set_pages),
        ("PAPER", set_paper),
        ("FORMAT", set_format),
        ("AUTH GROUP", set_auth),
        ("TRAINING", set_training),
        ("SAVE SETTINGS", _save),
    ])
    menu.title = "SETTINGS"
    return menu


def _apply(app, **changes):
    app.settings = replace(app.settings, **changes)
    problems = app.settings.validate()
    if problems:
        return Message("INVALID", [problems[0][:21]])
    return None


def _save(app):
    ok = save(app.settings, app.config_path)
    return Message("SETTINGS", ["SAVED" if ok else "COULD NOT WRITE",
                                "" if ok else "BOOT PART READ-ONLY"])


# --- top level ----------------------------------------------------------


def main_menu():
    def pad_pair(app):
        def chosen(app, codeword):
            spec = jobs_mod.JobSpec(jobs_mod.JobKind.PAD_PAIR, codeword, app.settings)
            return RunJob(spec)

        return codeword_menu(chosen)

    def simple(kind, count=1):
        return lambda app: RunJob(jobs_mod.JobSpec(kind, "", app.settings, count))

    menu = Menu([
        ("PRINT PAD PAIR", pad_pair),
        ("PRINT WORKSHEETS", simple(jobs_mod.JobKind.WORKSHEETS, 10)),
        ("PRINT TABULA RECTA", simple(jobs_mod.JobKind.TABULA, 2)),
        ("PRINT MANUAL", simple(jobs_mod.JobKind.MANUAL)),
        ("PRINT TEST PAGE", simple(jobs_mod.JobKind.TEST_PAGE)),
        ("SETTINGS", lambda app: settings_menu()),
        ("SHUT DOWN", lambda app: Message(
            "SHUT DOWN", ["HOLD OK TO CONFIRM", "THEN WAIT FOR THE LED"],
            on_ok=lambda a: a.request_shutdown())),
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
            lines=["", "PLUG IN A USB PRINTER", "", "|/-\\"[self.spinner] + " SEARCHING"],
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
        return False

    def run(self) -> None:
        if not self.wait_for_printer():
            return
        try:
            device = self.cups.devices()[0]
            self.queue = self.cups.ensure_queue(device)
        except (IndexError, printer_mod.PrinterError) as exc:
            self.stack = [Message("PRINTER", [str(exc)[:21]])]

        if not self.stack:
            self.stack = [main_menu()]

        while self.running and self.stack:
            self.render()
            press = self.buttons.wait()
            if press is None:
                continue
            if press is Press.QUIT:
                break
            current = self.stack[-1]
            nxt = current.press(self, press)
            if nxt is None:
                self.stack.pop()
            elif nxt is not current:
                self.stack.append(nxt)

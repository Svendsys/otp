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
from .config import (AUTH_SIZE_CHOICES, AUTO_DELAY_CHOICES, PAGE_CHOICES,
                     PAPER_CHOICES, PAPER_LABELS, Settings, save)
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
    # ASCII only, INPUT INCLUDED. Pillow's default bitmap font is latin-1,
    # and drawing anything outside it raises inside the display driver.
    # The text here is often a CUPS error, which arrives via
    # stderr.decode(..., "replace") -- so a truncated or localised message
    # carries U+FFFD or accented characters straight to the panel.
    text = text.encode("ascii", "replace").decode("ascii")
    ellipsis = ">"
    words, out, row = text.split(), [], ""
    tail_dropped = False
    for index, word in enumerate(words):
        candidate = f"{row} {word}".strip()
        if len(candidate) <= width:
            row = candidate
            continue
        if row:
            out.append(row)
        if len(out) == lines:
            # This word and everything after it never made it onto the panel.
            tail_dropped = True
            break
        # An over-long word is marked where it is cut; that is not the same
        # as losing the tail, and must not also mark the final row.
        row = word if len(word) <= width else word[:width - 1] + ellipsis
    else:
        if row and len(out) < lines:
            out.append(row)
        elif row:
            tail_dropped = True

    if tail_dropped and out and not out[-1].endswith(ellipsis):
        out[-1] = out[-1][:width - 1].rstrip() + ellipsis
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
        # Browsing narrows the space: one category is a few dozen nouns
        # against ~700, and human choice within it is not uniform. Two pads
        # from the same cell both named after birds is an attribution signal.
        # ROLL RANDOM is the default for that reason.
        def pick_category(app, category):
            def pick_noun(app, noun):
                return on_choose(app, cw.join(app.vocabulary.random_modifier(), noun))

            return Chooser(category.upper()[:21], app.vocabulary.nouns(category),
                           None, pick_noun)

        return Chooser("CATEGORY", app.vocabulary.categories, None, pick_category,
                       render=str.upper)

    def type_in(app):
        # Each half reaches exactly as far as the bundled lists do, so a
        # codeword agreed elsewhere can always be typed back in.
        def got_modifier(app, modifier):
            def got_noun(app, noun):
                return on_choose(app, cw.join(modifier, noun))

            return TextEntry("NOUN", got_noun, maxlen=app.vocabulary.noun_maxlen)

        return TextEntry("MODIFIER", got_modifier,
                         maxlen=app.vocabulary.modifier_maxlen)

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
        # Which stage the abandon prompt was reached from, so declining it
        # returns where the operator was rather than to a fixed guess.
        self.abandon_from = "waiting"
        # Whether the last queue query came back unanswerable, so the
        # waiting screen can say which of the two situations it is.
        self.queue_unknown = False

    def _total_pages(self) -> int:
        """Units the progress callback counts in, for this job kind."""
        if self.spec.carries_key_material and self.spec.settings:
            return self.spec.settings.pages
        return max(1, self.spec.count)

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
            # settings.pages counts PAD pages. Worksheets and tabula recta
            # are driven by spec.count instead, so reading settings.pages
            # for them showed a ten-copy worksheet run as "0/100 PAGES"
            # with a progress bar that never moved off zero.
            total = self._total_pages()
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
                footer="OK COPY B  HOLD STOP",
            )

        if self.stage == "waiting":
            # Say WHICH copy is outstanding. The frame was previously
            # identical whether copy A or copy B was still queued, so an
            # operator could not tell what giving up would cost them.
            outstanding = "A" if self.job and self.job.copies_done < 2 else "B"
            subject = (f"COPY {outstanding}"
                       if self.spec.carries_key_material else "THE JOB")
            if self.queue_unknown:
                # "STILL PRINTING" would be a guess, not a report. An
                # unanswerable query is deliberately counted as busy, so
                # claiming the queue is still working sends the operator to
                # the only other exit -- which destroys the pair.
                return Frame(
                    title="NO ANSWER",
                    lines=["CANNOT ASK THE", "PRINT QUEUE.", "",
                           "DOWN IF IT IS DONE"],
                    footer="OK RETRY  HOLD STOP",
                )
            return Frame(
                title="STILL PRINTING",
                lines=[f"{subject} IS STILL", "IN THE QUEUE.", "",
                       "OK TO CHECK AGAIN"],
                footer="HOLD TO GIVE UP",
            )

        if self.stage == "confirm_continue":
            return Frame(
                title="IS IT DONE?",
                lines=["CHECK THE PRINTER", "AND THE TRAY.", "",
                       "IS THIS COPY DONE?"],
                footer="OK=YES   HOLD=NO",
            )

        if self.stage == "confirm_abandon":
            return Frame(
                title="GIVE UP?",
                lines=["THIS DESTROYS THE KEY", "AND CANCELS PRINTING.",
                       "THE PAIR CANNOT BE", "FINISHED OR REMADE."],
                footer="OK=YES   HOLD=NO",
            )

        if self.stage == "abandoned":
            # Every job kind can reach this now, and only a pad carries key
            # material -- telling an operator to destroy an abandoned tabula
            # recta card teaches them to ignore the warning that matters.
            lines = (["KEY DISCARDED", "", "DESTROY ANY PAGES",
                      "ALREADY PRINTED"] if self.spec.carries_key_material
                     else ["JOB ABANDONED", "", "THE PRINTER MAY STILL",
                           "HAVE WORK QUEUED"])
            return Frame(title="ABANDONED", lines=lines, footer="OK TO RETURN")

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
            # If copy A is already on the tray, the operator is holding live
            # key material for a pad that can never be completed. Say so --
            # silently discarding the key and returning to the menu leaves
            # them with a half-pair they may not realise is dangerous. Wrap
            # to the space that leaves, rather than wrapping to three lines
            # and then discarding the third.
            warn = bool(self.spec.carries_key_material
                        and self.job and self.job.copies_done)
            lines = wrap(self.error or "UNKNOWN ERROR", lines=2 if warn else 4)
            if warn:
                lines.append("DESTROY PRINTED PAGES")
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
            # Asks first, exactly as `waiting` does. The consequence is
            # identical -- copy A on the tray, the key gone, the pair
            # impossible to finish -- and this screen also invites a
            # confident OK, so a tap held a beat too long landed on an
            # unconfirmed destroy. Round four guarded `waiting` and left
            # its neighbour.
            if press is Press.BACK:
                self.abandon_from = "swap"
                self.stage = "confirm_abandon"
                return self
            if press is Press.OK:
                return self._print_copy(app)
            return self

        if self.stage == "printing":
            if press in (Press.OK, Press.BACK):
                return self._advance(app)
            return self

        if self.stage == "waiting":
            # BACK is an unconditional way out. Without one this stage is a
            # trap: lp returns as soon as a job is spooled, so the queue is
            # busy on every pad pair, and CUPS' default ErrorPolicy holds a
            # job in the queue indefinitely after a jam -- which three
            # buttons cannot clear. An operator stuck here has live key
            # material and no option but to pull the power.
            #
            # It asks first, though. This screen invites repeated OK, and a
            # tap held a beat too long would otherwise purge the queue and
            # zero the key with no confirmation at all.
            if press is Press.BACK:
                self.abandon_from = "waiting"
                self.stage = "confirm_abandon"
                return self
            # When the queue cannot be asked at all, retrying forever is not
            # a way out: the only other exit destroys the key. Offer the
            # operator -- who is standing at the printer and can see the
            # tray -- a non-destructive way to say the copy landed.
            #
            # On DOWN, deliberately, and not on OK. This screen invites
            # repeated OK, and an operator drumming on it must never be able
            # to walk through the override and spool copy B over copy A.
            # DOWN is otherwise unused here, and no amount of OK reaches it.
            if press is Press.DOWN and self.queue_unknown:
                self.stage = "confirm_continue"
                return self
            if press is Press.OK:
                return self._advance(app)
            return self

        if self.stage == "confirm_continue":
            if press is Press.OK:
                return self._proceed(app)
            if press is Press.BACK:
                # "No, keep waiting" re-queries rather than parking on a
                # stale answer, so a cupsd that comes back is picked up.
                return self._advance(app)
            return self

        if self.stage == "confirm_abandon":
            if press is Press.OK:
                self._wipe()
                self.stage = "abandoned"
            elif press is Press.BACK:
                self.stage = self.abandon_from
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
        last_drawn = [0]

        def progress(done, total):
            # Redraw on a fraction of the total, not on `done % 10`. The
            # imposed layouts report per SHEET, so a ten-page A4 job counts
            # 4, 8, 10 and a modulo-10 test fires exactly once -- at the very
            # end, after the panel has sat at 0/10 for the whole job.
            self.done_pages = done
            step = max(1, total // 50)
            if done == 1 or done >= total or done - last_drawn[0] >= step:
                last_drawn[0] = done
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
            self._wipe()
            self.stage = "cancelled"
            return self
        except Exception as exc:
            # _wipe(), never finish() directly: a purge failure here would
            # otherwise escape press() and take the whole UI down.
            self._wipe()
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
            _drain(app)
            return self
        self.stage = "printing"
        # Submitting a multi-megabyte PDF blocks the loop, so presses made
        # while it ran are still queued. Replayed against the next screen
        # they advance the flow the operator could not see -- at worst
        # abandoning a pair they were only being impatient about.
        _drain(app)
        return self

    def _advance(self, app):
        # lp returns as soon as a job is spooled, so "the tray is clear" is
        # the operator's guess about paper, not a statement about the queue.
        # Both transitions have to wait for it to drain: starting copy B
        # early interleaves the two copies in one tray, and purging early
        # cancels the rest of copy B and destroys the key, leaving a
        # truncated half-pair that can never be regenerated.
        state = self.queue_state(app)
        self.queue_unknown = state == "unknown"
        if state != "idle":
            self.stage = "waiting"
            return self
        return self._proceed(app)

    def _proceed(self, app):
        """Take the transition, having established the queue is clear."""
        if not self.job.done:
            self.stage = "swap"
            return self
        self._wipe()
        self.stage = "done"
        return self

    def queue_state(self, app) -> str:
        """
        "busy", "idle", or "unknown" -- and the third is not the second.

        active_jobs returns None for a wedged cupsd, a missing lpstat and an
        unknown destination alike. Folding that into "idle" would let both
        transitions through at once: copy B spooled while A is still
        printing, then the spool purged and the panel reporting PAIR
        COMPLETE over an interleaved, truncated pair.

        Folding it into "busy" and leaving it there is not safe either,
        which is what the separate value is for. If the query never
        recovers, an operator whose pair printed perfectly is parked on a
        screen whose only exit destroys it. The caller offers a manual way
        on instead; see the confirm_continue stage.
        """
        try:
            queued = app.cups.active_jobs(app.queue)
        except Exception:
            return "unknown"
        if queued is None:
            return "unknown"
        return "busy" if queued > 0 else "idle"

    def cups_busy(self, app) -> bool:
        """Whether the queue holds work, counting an unanswerable query."""
        return self.queue_state(app) != "idle"


def gen_cancelled():
    import otp_generator

    return otp_generator.GenerationCancelled


def _drain(app) -> None:
    """
    Discard presses banked while the loop was blocked.

    Anything long enough to bank presses -- generating, spooling -- leaves
    the operator pressing at a frozen panel. Those presses were aimed at
    what was on screen, not at whatever comes next, so replaying them is
    always wrong; on this device it can mean abandoning a job.
    """
    while app.buttons.wait(timeout=0) is not None:
        pass


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

        def label(v):
            # The manual's procedure says "the first group", and a group is
            # five. Anything else has to be agreed at both ends, and shorter
            # is genuinely weaker -- say so where the choice is made.
            if v == 0:
                return "OFF - NO AUTH"
            if v < 5:
                return f"{v} LETTERS  WEAKER"
            if v == 5:
                return "5 LETTERS (MANUAL)"
            return f"{v} LETTERS"

        return Chooser("AUTH GROUP", AUTH_SIZE_CHOICES, current,
                       lambda a, v: _apply(a, with_auth=v > 0,
                                           auth_size=v or a.settings.auth_size),
                       render=label)

    def set_training(app):
        return Chooser("TRAINING", [False, True], app.settings.training,
                       lambda a, v: _apply(a, training=v),
                       render=lambda v: "TRAINING (MARKED)" if v else "LIVE MATERIAL")

    def set_paper(app):
        return Chooser("PAPER IN TRAY", PAPER_CHOICES, app.settings.paper,
                       lambda a, v: _apply(a, paper=v),
                       render=lambda v: PAPER_LABELS[v])

    def set_auto_print(app):
        # What this unit does when nobody is at the panel. It is reachable
        # only FROM the panel, which is the point: the person setting a unit
        # up to be left headless is holding the buttons now, and otherwise
        # has to power down and edit the card in another computer.
        return Chooser("UNATTENDED", [False, True], app.settings.auto_print,
                       lambda a, v: _apply(a, auto_print=v),
                       render=lambda v: ("PRINTS A PAIR ALONE" if v
                                         else "STATUS SHEET ONLY"))

    def set_auto_delay(app):
        return Chooser("WAIT FIRST", AUTO_DELAY_CHOICES,
                       app.settings.auto_delay,
                       lambda a, v: _apply(a, auto_delay=v),
                       render=lambda v: ("NO WAIT - AT ONCE" if v == 0
                                         else f"{v // 60} MIN" if v >= 60
                                         else f"{v} SEC"))

    menu = SettingsMenu([
        ("PAGES", set_pages),
        ("PAPER", set_paper),
        ("FORMAT", set_format),
        ("AUTH GROUP", set_auth),
        ("TRAINING", set_training),
        ("UNATTENDED", set_auto_print),
        ("UNATTENDED WAIT", set_auto_delay),
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
        self.display_failures = 0

    def render(self, screen=None) -> None:
        """
        Draw the current screen. Never raises.

        An I2C error mid-job would otherwise unwind run() with the key
        buffer live and the spool unpurged -- a complete pad left in a tmpfs
        spool across systemd's restart. A blank panel is bad; a blank panel
        that also abandons a running job without wiping is worse.
        """
        target = screen or (self.stack[-1] if self.stack else None)
        if target is None:
            return
        try:
            self.display.show(target.frame(self))
        except Exception:
            self.display_failures += 1

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
        # Try every device, not just the first. Driverless endpoints sort
        # first because they need no driver, but when one fails to set up
        # the usb:// entry for the same printer is usually right behind it
        # -- stopping at [0] turned the preferred path into a single point
        # of failure.
        last_error = None
        for device in self.cups.devices():
            try:
                self.queue = self.cups.ensure_queue(device)
                break
            except printer_mod.PrinterError as exc:
                last_error = exc
        else:
            self.stack.append(Message(
                "PRINTER", wrap(str(last_error) if last_error else "no printer found")))

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

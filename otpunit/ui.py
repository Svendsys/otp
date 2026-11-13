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

# Consecutive failed draws before the display is reopened.
REOPEN_AFTER = 3

# Returned by a screen to unwind the whole stack back to the main menu. A
# screen can otherwise only pop itself, and the screens beneath it may not
# be navigable.
HOME = object()

# "No reasons supplied", distinct from None, which state_reasons() uses for
# "asked, could not tell".
_UNASKED = object()


def wrap(text: str, width: int = 21, lines: int = 4) -> list[str]:
    """
    Break a message across panel rows on word boundaries.

    Text that does not fit is marked with a trailing ">" rather than
    silently dropped.
    """
    # ASCII only: Pillow's default bitmap font is latin-1 and drawing outside
    # it raises in the display driver. CUPS errors can carry U+FFFD or
    # accented characters.
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
            tail_dropped = True
            break
        # An over-long word is marked where it is cut; that alone must not
        # mark the final row.
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

    BACK only dismisses and never triggers on_ok, so the shutdown
    confirmation cannot be confirmed by either button.
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

    The alphabet ends in a backspace symbol: OK on it deletes the previous
    letter, and deleting past the start leaves the screen. BACK means
    "done" here, so that is the only way out without entering a word.
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
    """Roll or type: the two ways to land on a codeword.

    There is deliberately no way to browse and pick. A hand-picked noun
    reflects the operator's taste and hints at who the pad is for; rolling
    is uniform, and the codeword must identify the set without identifying
    its holders. Typing stays because reproducing a codeword agreed
    earlier is transcription, not selection.
    """

    def type_in(app):
        # Each half is as long as the longest bundled word, so any rolled
        # codeword can be typed back in.
        def got_modifier(app, modifier):
            def got_noun(app, noun):
                return on_choose(app, cw.join(modifier, noun))

            return TextEntry("NOUN", got_noun, maxlen=app.vocabulary.noun_maxlen)

        return TextEntry("MODIFIER", got_modifier,
                         maxlen=app.vocabulary.modifier_maxlen)

    menu = Menu([
        ("ROLL RANDOM", lambda app: CodewordRoll(on_choose)),
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
        # Stage the abandon prompt was reached from, to return to if declined.
        self.abandon_from = "waiting"
        # Whether the last queue query was unanswerable.
        self.queue_unknown = False
        # What the printer said while the queue stayed busy, if anything.
        self.queue_fault = ""
        # Set when the operator resumes a stopped queue. cupsd re-sends the
        # held job in full, so the stack may hold the same pages twice.
        self.reprinted = False

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
                    # Up to 17 characters, so it gets a line of its own.
                    spec.codeword,
                    f"{settings.pages} PAGES  {settings.format_label}",
                    # Sheets, not pages: it is what tells the operator
                    # whether the tray holds enough paper for both copies.
                    f"{settings.sheets * 2} SHEETS TOTAL",
                    f"AUTH {settings.auth_size if settings.with_auth else 'OFF'}",
                    # Always stated, never blank: absence of a word is not
                    # a signal to rely on before printing key material.
                    "*** TRAINING ***" if settings.training else "LIVE KEY MATERIAL",
                ]
            else:
                lines = [spec.kind.value.upper(), f"{spec.count} COPIES"]
            return Frame(title="CONFIRM", lines=lines, footer="OK START  HOLD CANCEL")

        if self.stage == "generating":
            # Pads count pages; other job kinds count spec.count.
            total = self._total_pages()
            return Frame(
                title="GENERATING",
                lines=[self.spec.codeword or self.spec.kind.value.upper(),
                       f"{self.done_pages}/{total} PAGES"],
                progress=self.done_pages / total if total else 0.0,
                footer="HOLD OK TO CANCEL",
            )

        if self.stage == "swap":
            # After a resume CUPS re-sends the whole held job, so a
            # part-printed copy comes out again from page one. Only the
            # operator can see the duplicate key material.
            if self.reprinted:
                return Frame(
                    title="COPY A DONE",
                    lines=["THE PRINTER RESTARTED", "THIS COPY. CHECK FOR",
                           "REPEATED PAGES AND", "DESTROY THE SPARES."],
                    footer="OK COPY B  HOLD STOP",
                )
            return Frame(
                title="COPY A DONE",
                lines=["REMOVE THE STACK", "AND KEEP IT TOGETHER", "",
                       "OK TO PRINT COPY B"],
                footer="OK COPY B  HOLD STOP",
            )

        if self.stage == "waiting":
            # Say which copy is outstanding, so the operator knows what
            # giving up would cost.
            outstanding = "A" if self.job and self.job.copies_done < 2 else "B"
            subject = (f"COPY {outstanding}"
                       if self.spec.carries_key_material else "THE JOB")
            if self.queue_fault:
                # Busy with a stopped queue: the job will sit there until
                # someone deals with the printer.
                return Frame(
                    title="PRINTER STOPPED",
                    lines=wrap(self.queue_fault, lines=2)
                          + ["", f"{subject} IS STUCK."],
                    footer="OK RECHECK  HOLD STOP",
                )
            if self.queue_unknown:
                # An unanswerable query is counted as busy, but claiming
                # STILL PRINTING would be a guess that leaves the operator
                # only the exit that destroys the pair.
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
            # Only a pad carries key material; telling an operator to
            # destroy an abandoned tabula recta card devalues the warning.
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
            # With copy A already on the tray the operator holds live key
            # material for a pad that can never be completed, so say so,
            # and wrap the error to the two lines that leaves.
            warn = bool(self.spec.carries_key_material
                        and self.job and self.job.copies_done)
            lines = wrap(self.error or "UNKNOWN ERROR", lines=2 if warn else 4)
            if warn:
                lines.append("DESTROY PRINTED PAGES")
            return Frame(title="ERROR", lines=lines, footer="OK TO RETURN")

        # 21 columns is the whole panel: every string here has to fit.
        # Not "KEY WIPED FROM RAM": the buffer is zeroed, but reportlab's
        # intermediates and the bytes handed to the subprocess cannot be.
        # Power-off is the real wipe.
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
            # Asks first, as `waiting` does: abandoning here leaves copy A
            # on the tray with the key gone, and a tap held slightly too
            # long must not do that unconfirmed.
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
            # BACK is always a way out: CUPS can hold a job indefinitely
            # after a jam, which three buttons cannot clear. It asks first,
            # because this screen invites repeated OK and a tap held too
            # long would otherwise purge the queue and zero the key.
            if press is Press.BACK:
                self.abandon_from = "waiting"
                self.stage = "confirm_abandon"
                return self
            # When the queue cannot be asked at all, offer a non-destructive
            # way to say the copy landed. On DOWN, not OK: an operator
            # drumming OK must never walk through the override and spool
            # copy B over copy A.
            if press is Press.DOWN and self.queue_unknown:
                self.stage = "confirm_continue"
                return self
            if press is Press.OK:
                # A stopped queue holds its job until it is re-enabled, so
                # a recheck must resume it first or nothing can change.
                # Resuming keeps the held job.
                if self.queue_fault:
                    self._resume(app)
                return self._advance(app)
            return self

        if self.stage == "confirm_continue":
            if press is Press.OK:
                return self._proceed(app)
            if press is Press.BACK:
                # "No, keep waiting" re-queries, so a cupsd that comes back
                # is picked up.
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
                # HOME, not None: the stack below may hold codeword screens
                # that would leave the operator mid-flow with a finished job.
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
            # Redraw on a fraction of the total, not every Nth count: the
            # imposed layouts report per sheet, so counts skip values.
            self.done_pages = done
            step = max(1, total // 50)
            if done == 1 or done >= total or done - last_drawn[0] >= step:
                last_drawn[0] = done
                app.render(self)

        def should_cancel():
            # Generation blocks the event loop. Draining here honours the
            # advertised cancel and stops banked presses replaying
            # afterwards, which would skip the swap prompt.
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
            # _wipe(), never finish() directly: a purge failure must not
            # escape press() and take the UI down.
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
        # Submitting blocks the loop; presses banked meanwhile would
        # advance a flow the operator could not see.
        _drain(app)
        return self

    def _advance(self, app):
        # lp returns as soon as a job is spooled, so "the tray is clear" is
        # only the operator's guess. Both transitions wait for the queue to
        # drain: starting copy B early interleaves the copies, and purging
        # early truncates copy B and destroys the key.
        state = self.queue_state(app)
        self.queue_unknown = state == "unknown"
        if state != "idle":
            # A busy queue may be a stopped one, which keeps its job for
            # ever. Only a stopped queue counts as a fault: cupsd puts
            # transient filter chatter in the state message while a healthy
            # job runs. Decided on the IPP state reasons, which do not vary
            # with the locale.
            self.queue_fault = self._stopped_because(app) if state == "busy" else ""
            self.stage = "waiting"
            return self
        # A drained queue is necessary but not sufficient: under ErrorPolicy
        # abort-job a failed job leaves the queue as promptly as a printed
        # one. The printer's prose decides and the state reasons can only
        # excuse it (see Cups.ADVISORY_STEMS; the usb backend reports no
        # media reasons). Reasons are asked for only when there is prose,
        # so a healthy queue costs one query.
        prose = self._fault(app)
        blocking = (printer_mod.blocking_in(prose, self._reasons(app))
                    if prose.strip() else [])
        if blocking:
            self.stage = "error"
            # The printer's own words first: "out of paper" is what an
            # operator can act on.
            self.error = prose or _reasons_as_text(blocking)
            # Discard presses banked during the lpstat calls. Replayed
            # against the ERROR frame they would wipe and return HOME
            # before the operator read DESTROY PRINTED PAGES.
            _drain(app)
            return self
        # Drained on the success path too: a banked OK reaching `swap`
        # would spool copy B into a tray still holding copy A.
        _drain(app)
        return self._proceed(app)

    def _fault(self, app) -> str:
        """What the printer says is wrong, or "" if it says nothing."""
        return printer_mod.reported_fault(app.cups, app.queue)

    def _reasons(self, app) -> list[str] | None:
        """
        The queue's IPP state reasons. Ask once per _advance and reuse.

        Each call is an lpstat subprocess bounded by Cups.TIMEOUT, and the
        panel is frozen with the key resident while it runs.
        """
        return printer_mod.state_reasons(app.cups, app.queue)

    def _blocking(self, app, reasons=_UNASKED) -> list[str]:
        """Why the queue cannot print, or [] -- prose decides, reasons excuse."""
        if reasons is _UNASKED:
            reasons = self._reasons(app)
        return printer_mod.blocking_in(self._fault(app), reasons)

    def _resume(self, app) -> None:
        """Ask cupsd to re-enable the queue; never raise into the loop."""
        resume = getattr(app.cups, "resume", None)
        if resume is None:
            return
        try:
            if resume(app.queue) is not False:
                self.reprinted = True
        except Exception:                        # noqa: BLE001
            pass

    def _stopped_because(self, app) -> str:
        """
        Panel text for a stopped queue, or "" if it is not stopped.

        Built from the state reasons, not lpstat's header line, which is
        only the queue name and the date it was disabled.
        """
        reasons = self._reasons(app)
        if not printer_mod.stopped_in(reasons, app.cups, app.queue):
            return ""
        # From the reasons in hand, to avoid another lpstat. "paused" is
        # dropped because PRINTER STOPPED is already the title.
        why = [r for r in (reasons or []) if r != "paused"]
        return _reasons_as_text(why) or "QUEUE STOPPED"

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
        "busy", "idle", or "unknown".

        active_jobs returns None when the queue cannot be asked. Treating
        that as idle would spool copy B over A and then report PAIR
        COMPLETE; treating it as permanently busy would park the operator
        on a screen whose only exit destroys the pair. So it is its own
        value, and the caller offers confirm_continue.
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


def generator():
    """otp_generator, imported late so this module stays cheap to import."""
    import otp_generator

    return otp_generator


def gen_cancelled():
    return generator().GenerationCancelled


def _reasons_as_text(reasons: list[str]) -> str:
    """
    IPP state-reason keywords as text an operator can read.

    The severity suffix is dropped and the rest becomes words.
    """
    words = []
    for reason in reasons:
        for suffix in ("-error", "-warning", "-report"):
            if reason.endswith(suffix):
                reason = reason[:-len(suffix)]
                break
        words.append(reason.replace("-", " ").upper())
    return ", ".join(words)


def _drain(app) -> None:
    """
    Discard presses banked while the loop was blocked.

    They were aimed at what was on screen then, so replaying them is always
    wrong; on this device it can mean abandoning a job.
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
            # The manual's procedure assumes a group of five; shorter is
            # weaker, so say so where the choice is made.
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
        # Settable from the panel so that a unit can be made headless
        # without editing the card in another computer.
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

    The operator is told to power-cycle after every pad job, and an unsaved
    TRAINING setting reverts to LIVE on the next boot.
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
        # See otpunit.posture. Refused before a codeword is rolled, so the
        # operator is not walked through a flow that cannot finish.
        if app.refuse_live and not app.settings.training:
            return Message("NOT SAFE", wrap(
                f"{app.refuse_live}. LIVE PADS REFUSED. TRAINING PADS "
                f"STILL PRINT."), footer="OK TO RETURN")

        def chosen(app, codeword):
            spec = jobs_mod.JobSpec(jobs_mod.JobKind.PAD_PAIR, codeword, app.settings)
            return RunJob(spec)

        return codeword_menu(chosen)

    def simple(kind, count=1):
        return lambda app: RunJob(jobs_mod.JobSpec(kind, "", app.settings, count))

    def manual(app):
        # The manual is rendered at image-build time; a source install has
        # none.
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


class WaitForEntropy(Screen):
    """
    Shown while the kernel CSPRNG is still being seeded.

    The spinner and the bit count move so the operator can see the unit is
    alive and does not power-cycle it, which would discard the entropy
    collected so far. The button hint is literal: GPIO interrupt timings
    feed the pool.
    """

    def __init__(self):
        self.spinner = 0

    def frame(self, app):
        self.spinner = (self.spinner + 1) % 4
        bits = generator().entropy_bits()
        return Frame(
            title="WAITING FOR ENTROPY",
            lines=["THE KERNEL RANDOM",
                   "POOL IS NOT SEEDED.",
                   "NO KEY CAN BE MADE",
                   "UNTIL IT IS.",
                   "PRESS THE BUTTONS:",
                   "IT HELPS A LITTLE."],
            footer=f"{'?' if bits is None else bits} BITS   "
                   + "|/-\\"[self.spinner],
        )

    def press(self, app, press):
        return self


class App:
    """Owns the screen stack and the hardware handles."""

    def __init__(self, display, buttons, cups=None, settings=None,
                 vocabulary=None, config_path=None, poll_seconds=2.0,
                 refuse_live: str = ""):
        from .config import CONFIG_PATH

        self.display = display
        self.buttons = buttons
        self.cups = cups or printer_mod.Cups()
        self.settings = settings or Settings()
        self.vocabulary = vocabulary or cw.Vocabulary()
        self.config_path = config_path or CONFIG_PATH
        self.queue = printer_mod.QUEUE
        self.poll_seconds = poll_seconds
        self.refuse_live = refuse_live
        self.stack: list[Screen] = []
        self.running = True
        self.shutdown_requested = False
        # Settings start matching whatever was loaded from the card.
        self.settings_saved = True
        self.display_failures = 0

    def render(self, screen=None) -> None:
        """
        Draw the current screen. Never raises.

        A display error mid-job must not unwind run() with the key buffer
        live and the spool unpurged.
        """
        target = screen or (self.stack[-1] if self.stack else None)
        if target is None:
            return
        try:
            self.display.show(target.frame(self))
            self.display_failures = 0
        except Exception:
            self.display_failures += 1
            # A panel that browned out comes back with its display off;
            # reopening re-runs its init sequence. Only every few failures,
            # since reopening a bus with nothing on it is not free.
            if self.display_failures % REOPEN_AFTER == 0:
                reopen = getattr(self.display, "reopen", None)
                try:
                    if reopen is not None:
                        reopen()
                except Exception:                # noqa: BLE001
                    pass

    def request_shutdown(self):
        self.shutdown_requested = True
        self.running = False
        return None

    def wait_for_entropy(self) -> bool:
        """
        Hold the panel here until the kernel CSPRNG is seeded.

        Not a security gate (see otp_generator.crng_seeded): os.urandom
        already blocks until seeded. Without this the first random draw
        would block inside a frame() call and the panel would look frozen,
        inviting a power cycle that discards the entropy collected.

        Polls the buttons itself so the panel stays live. Returns False if
        the app stopped running first.
        """
        screen = WaitForEntropy()
        while self.running:
            if generator().crng_seeded():
                # This screen asks to be pressed, so discard the banked
                # presses rather than hand them to the main menu.
                _drain(self)
                return True
            self.render(screen)
            press = self.buttons.wait(timeout=self.poll_seconds)
            if press is Press.QUIT:
                self.running = False
            elif press is Press.BACK:
                # A unit that can never seed must still be able to power
                # down cleanly.
                self.request_shutdown()
        return False

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
                # Without this an unsupported or dead printer leaves no way
                # to power down cleanly.
                self.request_shutdown()
        return False

    def run(self) -> None:
        # Entropy first: it has to clear before the menu can roll a
        # codeword.
        if not self.wait_for_entropy():
            return
        if not self.wait_for_printer():
            return
        # The main menu is always the bottom of the stack, even when setup
        # failed, so SETTINGS and SHUT DOWN stay reachable.
        self.stack = [main_menu()]
        # Try every device, not just the first: when a driverless endpoint
        # fails to set up, the usb:// entry for the same printer may work.
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
                # Never pop the last screen: an empty stack ends run() and
                # leaves the panel dark until a power cycle.
                if len(self.stack) > 1:
                    self.stack.pop()
            elif nxt is not current:
                self.stack.append(nxt)

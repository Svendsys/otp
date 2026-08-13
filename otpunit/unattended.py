"""Producing pads with no panel, no buttons and no way to get either.

The status sheet on its own answers the wrong question. It tells someone
to go and find a 128x64 SSD1306 and three momentary switches, which
assumes there is somewhere to find them. If there is not -- and the case
this unit exists for is exactly the case where there is not -- then a
device that can only ask for parts is a brick that prints a shopping list.

So the panel is treated as an optimisation, not a requirement. The pads
are the point. Everything the panel ever did was choose a codeword and a
page count, and both have defaults that are fine.

What is left is a control surface built from what is certainly available:

  time        the unit waits, and says on paper exactly how long
  the plug    unplugging the printer aborts; it is the one control that
              needs no parts at all
  the SD card any computer that can read a FAT partition can edit
              otp-unit.conf, which is a complete configuration channel
  a wire      GPIO13 to ground is a button. A paperclip is a button. If
              anything at all is bridging those pins, a press means
              "stop waiting, print it now"
  paper       the sheets between the copies are the user interface

The sequence is deliberately one-shot. It prints a pair and stops, rather
than looping, because unattended key material accumulating in an output
tray is its own hazard.
"""
from __future__ import annotations

import time

from otpunit import diagnostics, jobs
from otpunit.codewords import Vocabulary
from otpunit.config import Settings


class Aborted(Exception):
    """The printer went away mid-sequence."""


# Consecutive empty device lists before believing the printer is really
# gone. One empty answer is a hiccup; several in a row is a cable. Shared
# with the headless loop rather than restated: two copies of a threshold
# whose whole job is to be the same number is a drift waiting to happen.
GONE_AFTER = diagnostics.GONE_AFTER


def _press_waiter(buttons):
    """
    A callable that reports whether anything is pressing a button.

    Returns a function that never raises: a missing gpiozero, an
    unexportable pin or a lgpio that will not load must degrade to "no
    button", not stop the pads.
    """
    if buttons is None:
        return lambda: False

    def pressed():
        try:
            return buttons.wait(timeout=0) is not None
        except Exception:                        # noqa: BLE001
            return False
    return pressed


def countdown(cups, seconds, buttons=None, sleep=None, log=print,
              step=1.0) -> str:
    """
    Wait, watching the printer and the buttons.

    Returns "elapsed", "pressed", or raises Aborted if the printer was
    unplugged -- which is how someone with no other controls says no.
    """
    sleep = sleep or time.sleep
    pressed = _press_waiter(buttons)
    # Discard edges banked BEFORE this window opened. GpioButtons is a
    # queue, and a stray edge -- a wire being connected, a test press at
    # boot -- otherwise satisfies the very next countdown instantly. Two
    # banked taps erased both the 5-minute abort window and the tray
    # break, spooling copy A and copy B back to back.
    for _ in range(64):
        if not pressed():
            break
    waited = 0.0
    misses = 0
    while waited < seconds:
        if pressed():
            log("button pressed; starting now")
            return "pressed"
        # A single empty answer is NOT a disconnect. The real
        # Cups.devices() swallows every error and returns [] -- a busy
        # cupsd, a timed-out lpinfo, a missing binary all look identical
        # to an unplugged cable. Reading one of those as "unplugged"
        # abandoned the pad mid-pair over a hiccup, and the except-clause
        # that was supposed to tolerate it was dead code, because
        # devices() cannot raise.
        try:
            present = bool(cups.devices())
        except Exception as exc:                 # noqa: BLE001
            log(f"printer lookup failed: {exc}")
            present = False
        misses = 0 if present else misses + 1
        if misses >= GONE_AFTER:
            raise Aborted("printer disconnected")
        sleep(step)
        waited += step
    return "elapsed"


def drain(cups, queue, sleep, log, timeout=900.0, step=2.0,
          clock=None) -> bool:
    """
    Block until the queue reports no work, or the timeout expires.

    True means the queue is genuinely empty and it is safe to purge.
    False means we could not establish that -- an unanswerable lpstat or a
    queue that never emptied -- in which case the caller must NOT purge,
    because cancelling a job that is still printing destroys the copy.

    Raises Aborted if the printer goes away while we wait. The plug is the
    only cancel this mode offers, and a 25-sheet copy A is fifteen minutes
    of drain: leaving that window deaf meant pulling the cable during the
    longest wait in the sequence did nothing at all until the wait ended.

    Measured against a WALL CLOCK, not by adding up the sleeps. Each poll
    runs lpstat through subprocess with Cups.TIMEOUT = 120s, and counting
    only the sleeps let a wedged cupsd stretch a 3600-second budget to
    sixty-one hours. Bounded by the poll count as well, because a test that
    injects a no-op sleep never advances a real clock.
    """
    clock = clock or time.monotonic
    deadline = clock() + timeout
    polls = misses = 0
    while clock() < deadline and polls <= timeout / step + 1:
        polls += 1
        try:
            queued = cups.active_jobs(queue)
        except Exception:                        # noqa: BLE001
            queued = None
        if queued == 0:
            return True
        # One empty answer is a hiccup, several in a row is a cable -- the
        # same rule the countdown uses, for the same reason.
        try:
            present = bool(cups.devices())
        except Exception:                        # noqa: BLE001
            present = False
        misses = 0 if present else misses + 1
        if misses >= GONE_AFTER:
            raise Aborted("printer disconnected while the queue was draining")
        sleep(step)
    log("timed out waiting for the print queue to drain")
    return False


def run(cups, settings: Settings = None, queue: str = "OTP", log=print,
        sleep=None, buttons=None, vocabulary=None, driver=None) -> int:
    """
    Status sheet, a pause, then a pad pair, with printed sheets between.

    Returns 0 if a pair was printed, 1 if not.
    """
    settings = settings or Settings()
    sleep = sleep or time.sleep
    vocabulary = vocabulary or Vocabulary()

    # Every sheet carries the media size. Dropping it left the status,
    # swap and done sheets submitted with no media at all, so a LETTER or
    # A6 unit rendered one size and asked the queue for another.
    paper = {"media": _media(settings)}

    def send(data, title, options=None):
        # Wait for the queue before adding to it. The unit ships with
        # MaxJobs 1, so cupsd REJECTS a second job while one is active --
        # `lp: Too many active jobs.` Measured against a real cupsd with
        # the shipped config: the manual spooled, then the tabula, copy A,
        # the separator and the final sheet were all refused in turn, and
        # the operator got a status sheet promising a pad followed by
        # nothing at all. Every gap in this sequence needs the wait, not
        # just the two that had it.
        drain(cups, queue, sleep, log, timeout=_drain_timeout(settings))
        cups.submit(data, name=queue, title=title,
                    options=paper if options is None else options)

    # 1. Say what is about to happen, before doing any of it. Guarded like
    #    everything else: send() now waits for the queue first, so it can
    #    raise Aborted, and this call sits outside every other try. A
    #    missing /usr/bin/lp reached here as a bare FileNotFoundError and
    #    left run() by the front door.
    try:
        send(diagnostics.render_bytes(
            diagnostics.collect(settings=settings, printer=_first(cups),
                                queue=queue, driver=driver,
                                plan=_plan(settings)),
            diagnostics._page_size(settings)), "OTP status")
        log("status sheet submitted")
    except Exception as exc:                     # noqa: BLE001
        # Nothing can be printed, so there is nothing to say and nowhere to
        # say it. Do not go on to generate a pad for a printer that just
        # refused a single sheet.
        log(f"could not print the status sheet: {exc}")
        return 1

    if not settings.auto_print:
        log("auto_print is off; stopping after the status sheet")
        return 1

    # 2. The codeword, and not one line earlier.
    #
    #    Rolling one draws from the CSPRNG, and os.urandom blocks inside
    #    getrandom() until the kernel has seeded it. This used to be the
    #    first statement in run(), which meant a unit at first boot with
    #    no network and no RTC printed NOTHING AT ALL -- not the status
    #    sheet, not the plan, not the row that would have explained it.
    #    Measured against this function before the move: an empty
    #    submission list and an empty log, indefinitely.
    #
    #    The status sheet needs no codeword -- it must never carry one --
    #    so it goes out first and reports the CRNG state itself, and the
    #    wait below happens with that page already in the tray. In this
    #    mode the printer IS the panel, so that page is the only place the
    #    unit can say anything at all.
    announced = [None]

    def waiting(seconds):
        # First, then every 30s. The unit's journal is volatile and lives
        # in RAM, so a line every half second for a wait with no upper
        # bound is a slow leak into the one resource it cannot spare.
        if announced[0] is not None and seconds - announced[0] < 30.0:
            return
        announced[0] = seconds
        log(f"waiting for the kernel CSPRNG to be seeded ({seconds:.0f}s so "
            f"far); no key material can be drawn until it is")

    waited = gen_module().wait_for_crng(on_wait=waiting, sleep=sleep)
    if waited:
        log(f"kernel CSPRNG seeded after {waited:.0f}s")
    codeword = settings.auto_codeword or vocabulary.random()

    # 3. Wait, so there is time to read the sheet and pull the plug.
    try:
        countdown(cups, settings.auto_delay, buttons=buttons, sleep=sleep,
                  log=log)
    except Aborted as exc:
        log(f"aborted: {exc}")
        return 1

    # 4. The manual, BEFORE the pads. A pad is useless to someone who
    #    does not know the rules -- one reused page undoes the whole
    #    thing -- and the person this mode exists for has no other way to
    #    find out. It goes first so that if the paper runs out, what
    #    survives is the instructions rather than half a pad.
    if settings.auto_manual:
        try:
            book = jobs.generate(jobs.JobSpec(jobs.JobKind.MANUAL, "", settings))
            send(book, "MANUAL", _manual_options(settings))
            log("manual submitted")
        except Exception as exc:                 # noqa: BLE001
            log(f"manual failed, continuing: {exc}")

    # 5. The tabula recta, which is what makes a pad usable by hand
    #    without doing arithmetic. No key material.
    try:
        card = jobs.generate(jobs.JobSpec(jobs.JobKind.TABULA, "", settings))
        send(card, "TABULA RECTA", settings.lp_options)
        log("tabula recta submitted")
    except Exception as exc:                     # noqa: BLE001
        # Useful, not essential. Losing it must not lose the pads.
        log(f"tabula recta failed, continuing: {exc}")

    # 6. The pair itself, generated once and submitted twice so the two
    #    copies are byte-identical -- which is what makes them a pair.
    spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, codeword, settings)
    job = jobs.PadPairJob(spec, cups, queue)
    # Reset before generating and read after, so the sheet reports what
    # THIS pad was made from. A probe at sheet-render time answers a
    # different question: the status sheet probes five minutes before
    # generation starts and the final sheet probes after it ends, and a
    # device that dies in between made every page after it CSPRNG-only.
    gen_module().TALLY.reset()
    try:
        job.generate()
    except Exception as exc:                     # noqa: BLE001
        log(f"generation failed: {exc}")
        return 1

    printed_b = False
    drained = purge = False
    trouble = ""
    try:
        # The pad copies go through PadPairJob rather than send(), so they
        # need the same wait for the queue that send() does. Copy B gets it
        # from the tray break; copy A follows the tabula card directly and
        # was the submission that MaxJobs refused.
        drain(cups, queue, sleep, log, timeout=_drain_timeout(settings))
        job.print_next_copy()
        log(f"copy A submitted ({settings.pages} pages)")

        # 7. The separator IS the prompt. With no buttons nothing can wait
        #    for a keypress, so the sheet names the deadline instead.
        send(sheet(SWAP, codeword=codeword, settings=settings,
                   seconds=settings.auto_swap_delay), "REMOVE COPY A")

        # Wait for copy A and the separator to physically COME OUT before
        # timing the tray break. lp returns once a job is spooled, so a
        # fixed timer started here runs while the printer is still working
        # -- at 12 ppm a 25-sheet copy A needs 125s against a 90s break, so
        # copy B was spooled before the separator sheet even landed.
        drain(cups, queue, sleep, log, timeout=_drain_timeout(settings))
        # If copy A did not reach paper, copy B will not either, and 25
        # more sheets of a pad nobody can use is the wrong answer. Stop and
        # say what the printer said.
        fault = _fault(cups, queue)
        if fault:
            raise Aborted(f"the printer reports: {fault}")
        countdown(cups, settings.auto_swap_delay, buttons=buttons,
                  sleep=sleep, log=log)

        job.print_next_copy()
        log("copy B submitted")

        # THE important wait. finish() below purges the spool with
        # `cancel -x -a`, which cancels every job on the queue INCLUDING
        # the one currently printing -- and copy B was handed to lp
        # microseconds ago. Without this the unit cancelled its own copy B
        # on every single run, then printed a sheet saying the pair was
        # complete. The interactive path has always waited for this; the
        # unattended path did not.
        drained = drain(cups, queue, sleep, log,
                        timeout=_drain_timeout(settings))
        if not drained:
            log("queue never drained; leaving the spool rather than "
                "cancelling a copy that may still be printing")
        # A drained queue is necessary but NOT sufficient. It says the
        # spool emptied; under ErrorPolicy abort-job that happens just as
        # fast when every job failed. Ask the printer as well.
        purge = drained
        fault = _fault(cups, queue)
        if fault:
            log(f"the queue drained but the printer reports: {fault}")
        # Setting this on the drain alone handed the operator "YOUR PAD
        # PAIR IS PRINTED / They are identical" over a tray that was empty
        # because the printer had run out of paper -- the exact failure the
        # HALF sheet exists to report, made unreachable.
        printed_b = drained and not fault
        trouble = fault or trouble
    except Aborted as exc:
        log(f"aborted mid-pair: {exc}")
        trouble = trouble or str(exc)
        # The printer is gone, so nothing can be mid-print and there is
        # nothing left to destroy by cancelling. What IS still there is a
        # spooled copy of the pad, which is the one case where purging is
        # both safe and the whole point.
        purge = True
    except Exception as exc:                     # noqa: BLE001
        # Anything else leaves the queue in an unknown state, and cancelling
        # a job that is still feeding paper loses that copy for good.
        log(f"printing failed: {exc}")
        trouble = trouble or str(exc)
    finally:
        try:
            # The key is wiped either way; only the spool purge is
            # conditional, because cancelling an in-flight job destroys
            # the copy that is printing.
            job.finish(purge=purge)
        except Exception as exc:                 # noqa: BLE001
            # Never let a wipe failure mask what happened to the pair.
            log(f"wipe failed: {exc}")

    # 8. What they are now holding -- which is NOT always a pair. Saying
    #    "two identical copies" over half a pair is worse than saying
    #    nothing, because the operator files it and finds out later.
    try:
        send(sheet(DONE if printed_b else HALF, codeword=codeword,
                   settings=settings, tally=gen_module().TALLY,
                   trouble=trouble),
             "WHAT TO DO NOW")
    except Exception as exc:                     # noqa: BLE001
        log(f"could not print the final sheet: {exc}")
    log("done" if printed_b else "pair incomplete")
    return 0 if printed_b else 1


def _first(cups):
    try:
        devices = cups.devices()
    except Exception:                            # noqa: BLE001
        return None
    return devices[0] if devices else None


def _fault(cups, queue) -> str:
    """
    What the printer says is wrong, or "" if it says nothing or cannot say.

    Silence is deliberately not read as trouble. A cupsd that cannot be
    asked, and an older Cups without this method at all, must fall back to
    the drain result rather than send someone to burn a pad that printed
    perfectly.
    """
    try:
        return cups.printer_fault(queue) or ""
    except Exception:                            # noqa: BLE001
        return ""


MANUAL_PAGES = 28          # the rendered A5 manual, for the paper estimate


def _media(settings: Settings) -> str:
    """What actually comes out of the tray, which is not always `paper`."""
    return settings.lp_options.get("media", settings.paper)


# Seconds of drain budget per physical sheet, plus a fixed floor. A fixed
# 900s bound is generous for the 25-sheet default and far too short for
# `pages = 1000`, which is 250 sheets and over twenty minutes of printing
# on a 12ppm laser -- the drain would time out, the purge be skipped and a
# perfectly good pair reported as half.
DRAIN_PER_SHEET = 12.0
DRAIN_FLOOR = 300.0


def _drain_timeout(settings: Settings) -> float:
    return DRAIN_FLOOR + DRAIN_PER_SHEET * max(settings.sheets, 1)


def _manual_options(settings: Settings) -> dict:
    """
    Two manual pages per sheet on A4 or Letter.

    The manual is laid out A5, which is exactly half of A4, so two to a
    sheet is a clean fit rather than a scaling compromise -- and it halves
    28 sheets to 14. Paper is a real constraint for anyone relying on this
    mode, so it is not a detail.

    Anything narrower gets one page a sheet -- but it still gets the media,
    because returning a bare {} left the manual as the only job in the
    sequence submitted with no size at all, to be rendered at whatever the
    queue happened to default to.
    """
    media = {"media": _media(settings)}
    if settings.paper in ("A4", "LETTER"):
        return {**media, "number-up": "2"}
    return media


def _manual_sheets(settings: Settings) -> int:
    if not settings.auto_manual:
        return 0
    if settings.paper in ("A4", "LETTER"):
        return (MANUAL_PAGES + 1) // 2
    return MANUAL_PAGES


def sheets_needed(settings: Settings) -> int:
    """
    Roughly how much paper a full run costs, for the status sheet.

    Someone deciding whether to let this run may have a finite stack and
    no way to get more. Better an estimate on the first sheet than a
    printer that stops halfway through copy B.

    `Settings.sheets` already knows the imposition, including that A7 is
    two pad pages to an A6 sheet. Restating the arithmetic here got A7
    wrong by a factor of two -- 100 pages quoted as 100 sheets a copy
    rather than 50 -- which is exactly the number this is printed for.
    """
    # status + tabula + swap + done
    return _manual_sheets(settings) + settings.sheets * 2 + 4


def _plan(settings: Settings) -> list[str]:
    """The countdown notice printed on the status sheet."""
    if not settings.auto_print:
        return ["auto_print is off in otp-unit.conf, so this unit will print "
                "nothing further. Set it to yes to have it produce pads on "
                "its own."]
    minutes = settings.auto_delay / 60
    when = ("immediately" if settings.auto_delay == 0
            else f"in {settings.auto_delay} seconds"
            if settings.auto_delay < 120 else f"in about {minutes:.0f} minutes")
    order = ["the manual"] if settings.auto_manual else []
    order += ["a tabula recta card", "copy A of the pad", "copy B"]
    return [
        f"THIS UNIT WILL PRINT A ONE-TIME PAD PAIR {when.upper()}.",
        f"In order: {', '.join(order)}. Two identical copies, "
        f"{settings.pages} pages each. You do not need to do anything.",
        f"PAPER: about {sheets_needed(settings)} sheets of "
        f"{_media(settings)} in total. Load more than that if you can; if it "
        "runs out mid-pair you lose the pair, not just the paper.",
        "TO STOP IT: unplug the printer, or power the unit off. Nothing is "
        "printed until the wait is over.",
        "TO START NOW: if anything is bridging GPIO13 (header pin 33) to "
        "ground (pin 34) -- a button, a wire, a paperclip -- press or touch "
        "it and printing begins at once.",
        "TO CHANGE IT: edit otp-unit.conf on the SD card's first partition "
        "in any computer. See CONFIGURATION.",
    ]


# --- the sheets that stand in for a panel ------------------------------

SWAP = "swap"
DONE = "done"
HALF = "half"


def _key_source(tally=None) -> str:
    """
    Said on the pad's own sheet, not just the status sheet.

    Someone may keep this page with the pad for years. If the unit had no
    hardware TRNG, what they are holding is stream-cipher output rather
    than a one-time pad -- still strong, but not unbreakable-in-principle,
    and that is a difference they are entitled to know about the specific
    pad in their hand.
    """
    try:
        if tally is not None and tally.total:
            return tally.summary()
        # No tally means nobody generated anything through this path; fall
        # back to a probe and be clear that is what it is.
        if gen_module().entropy_source() == "hwrng+urandom":
            return "hardware TRNG available (not measured for this pad)"
        return ("NO HARDWARE RNG WAS FOUND. This key came from the system "
                "CSPRNG alone, which makes it a very strong stream cipher "
                "rather than a true one-time pad.")
    except Exception:                            # noqa: BLE001
        return "unknown"


def gen_module():
    import otp_generator

    return otp_generator


def sheet(kind, codeword="", settings=None, seconds=0, tally=None,
          trouble=""):
    """One full-page instruction sheet, as PDF bytes."""
    settings = settings or Settings()
    if kind is SWAP:
        heading = dict(
            title="STOP -- TAKE COPY A OUT OF THE TRAY",
            subtitle="The pages above this sheet are one-time pad key "
                     "material. They are secret. Keep them.")
        sections = [
            diagnostics.Section("STOP -- TAKE THE STACK OUT", [
                (None, "Everything printed above this sheet is COPY A of a "
                       "one-time pad. Take it out of the tray now and put it "
                       "somewhere separate."),
                (None, f"COPY B starts printing in about {seconds} seconds. "
                       "If the two copies mix in the tray you will not be "
                       "able to tell them apart, and a pad pair whose halves "
                       "cannot be told apart is worthless."),
                (None, "If anything is bridging header pin 33 to pin 34, "
                       "press it to start copy B at once."),
            ]),
            diagnostics.Section("THIS IS KEY MATERIAL", [
                ("Codeword", codeword or "(unset)"),
                ("Pages", str(settings.pages)),
                (None, "Unlike the status sheet, these pages ARE secret. "
                       "Anyone who photographs them can read every message "
                       "the pad ever protects."),
            ]),
            # The sheet that reports a failed copy B has to be printed, and
            # if copy B failed because the printing stopped working then it
            # cannot be. Measured: with cupsd killed after the separator,
            # the last thing in the tray was this sheet promising a copy B
            # that never came, and nothing ever said otherwise. So the
            # warning goes here, in advance, while there is still a printer
            # to put it on paper.
            diagnostics.Section("IF NOTHING FOLLOWS THIS SHEET", [
                (None, "A COPY B AND A FINAL INSTRUCTION SHEET SHOULD COME "
                       "OUT AFTER THIS ONE. If they do not -- the printer "
                       "runs out of paper, jams, or is switched off -- then "
                       "what you are holding is HALF A PAIR."),
                (None, "Half a pair is worthless and dangerous: there is no "
                       "matching copy for the other end, and the key has "
                       "already been wiped from this unit, so a copy B can "
                       "never be made. DESTROY the stack. Burn it."),
                (None, "Then fix the printer, power the unit off and on with "
                       "it attached, and it will make a fresh pair under a "
                       "new codeword."),
            ]),
        ]
    elif kind is HALF:
        heading = dict(
            title="STOP -- THIS PAD IS NOT USABLE",
            subtitle="Printing did not finish. What is in the tray is half "
                     "a pair, and half a pair is worthless.")
        sections = [
            diagnostics.Section("WHAT WENT WRONG", [
                (None, "Copy A was printed but copy B was not, so there is "
                       "no second copy to give to the person you were going "
                       "to talk to. A one-time pad only works because both "
                       "ends hold the SAME key."),
                # The printer usually knows -- out of paper, cover open,
                # jammed -- and saying so is the difference between a sheet
                # that ends the problem and one that only reports it.
                ("Reported", trouble or "nothing; the printer gave no reason"),
                ("Codeword", codeword or "(unset)"),
            ]),
            diagnostics.Section("WHAT TO DO", [
                (None, "1. DESTROY every page in the tray. Burn it. Do not "
                       "keep it and do not try to use it -- the key has "
                       "already been wiped from this unit, so a matching "
                       "copy B can never be made."),
                (None, "2. Check the printer: paper, toner, a jam, or the "
                       "cable. The status sheet at the top of the stack "
                       "lists what this unit could see."),
                (None, "3. Power the unit off and on with the printer "
                       "attached to try again. It will roll a new codeword."),
            ]),
        ]
    else:
        heading = dict(
            title="YOUR PAD PAIR IS PRINTED",
            subtitle="This sheet names a live pad. Keep it with the pad or "
                     "destroy it -- do not leave it lying about.")
        sections = [
            diagnostics.Section("YOU NOW HAVE A PAD PAIR", [
                ("Codeword", codeword or "(unset)"),
                ("Pages per copy", str(settings.pages)),
                ("Format", "A7" if settings.a7 else "A6"),
                ("Key source", _key_source(tally)),
                (None, "Two copies were printed, A then B. They are "
                       "identical, and that is the point: one stays with "
                       "you, the other goes to the person you will be "
                       "talking to. Hand it over in person."),
            ]),
            diagnostics.Section("THE FOUR RULES", [
                (None, "1. NEVER use a page twice. Not for a second message, "
                       "not to correct a mistake. Two messages under one page "
                       "can be broken by hand."),
                (None, "2. DESTROY each page as soon as it is used, on both "
                       "ends. Burn it. A used page is evidence and a liability, "
                       "never an archive."),
                (None, "3. KEEP THE TWO COPIES APART. Both in one place means "
                       "one raid takes the channel and everything it carried."),
                (None, "4. NEVER send the same message on two channels. If "
                       "one is broken, the other is handed to them as a crib."),
            ]),
            diagnostics.Section("TO USE IT", [
                (None, "Both ends work from the same numbered page. Turn "
                       "letters into numbers (A=0..Z=25), ADD the key to your "
                       "message letter by letter, wrap past Z back to A. To "
                       "read one, SUBTRACT instead."),
                (None, "The tabula recta card does the arithmetic for you: "
                       "find the message letter along the top and the key "
                       "letter down the side."),
                (None, "Pad the message out with A to the end of the page, so "
                       "its length gives nothing away. Under the padding "
                       "convention the ciphertext there is just the key."),
            ]),
            diagnostics.Section("TO MAKE MORE", [
                (None, "Power the unit off and on with the printer attached. "
                       "It prints another pair with a new codeword."),
                (None, "To choose the codeword or the page count, edit "
                       "otp-unit.conf on the SD card in any computer."),
            ]),
        ]
    return diagnostics.render_bytes(sections, diagnostics._page_size(settings),
                                    **heading)

"""Producing pads with no panel and no buttons.

The panel is an optimisation, not a requirement: all it does is choose a
codeword and a page count, and both have usable defaults. Without it the
control surface is what is certainly available:

  time        the unit waits, and says on paper exactly how long
  the plug    unplugging the printer aborts
  the SD card otp-unit.conf on the FAT partition is the configuration
  a wire      anything bridging GPIO13 to ground is a button; a press
              means "stop waiting, print it now"
  paper       the sheets between the copies are the user interface

The sequence is one-shot: it prints a pair and stops, because unattended
key material accumulating in an output tray is its own hazard.
"""
from __future__ import annotations

import time

from otpunit import diagnostics, jobs, printer
from otpunit.codewords import Vocabulary
from otpunit.config import Settings


class Aborted(Exception):
    """The printer went away mid-sequence."""


# Consecutive empty device lists before believing the printer is really
# gone. Shared with the headless loop so the two cannot drift.
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
    # Discard edges banked before this window opened. GpioButtons is a
    # queue, and a stray edge would otherwise end the countdown instantly,
    # erasing the abort window or the tray break.
    for _ in range(64):
        if not pressed():
            break
    waited = 0.0
    misses = 0
    while waited < seconds:
        if pressed():
            log("button pressed; starting now")
            return "pressed"
        # A single empty answer is not a disconnect: Cups.devices() returns
        # [] for a busy cupsd or a timed-out lpinfo as well as for an
        # unplugged cable.
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

    True means the queue is empty and it is safe to purge. False means
    that could not be established (an unanswerable lpstat, or a queue that
    never emptied), and the caller must not purge: cancelling a job that
    is still printing destroys the copy.

    Raises Aborted if the printer goes away meanwhile; the plug is the
    only cancel this mode offers.

    The timeout is wall-clock, because each poll can itself block for up
    to Cups.TIMEOUT. It is also bounded by poll count, for tests whose
    injected sleep never advances a real clock.
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
        # Same rule as countdown(): several misses in a row is a cable.
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
        sleep=None, buttons=None, vocabulary=None, driver=None,
        refuse_live: str = "") -> int:
    """
    Status sheet, a pause, then a pad pair, with printed sheets between.

    Returns 0 if a pair was printed, 1 if not.
    """
    settings = settings or Settings()
    sleep = sleep or time.sleep
    vocabulary = vocabulary or Vocabulary()
    # See otpunit.posture. Training pads are not secret and still print.
    refused = refuse_live if not settings.training else ""

    # Every sheet carries the media size, so the queue is never asked for a
    # different size than was rendered.
    paper = {"media": _media(settings)}

    def send(data, title, options=None):
        # Wait for the queue before adding to it: past MaxJobs cupsd
        # rejects a job rather than queueing it. This wait is what makes
        # the sequence independent of the MaxJobs value.
        drain(cups, queue, sleep, log, timeout=_drain_timeout(settings))
        cups.submit(data, name=queue, title=title,
                    options=paper if options is None else options)

    # 1. Say what is about to happen, before doing any of it. Guarded
    #    because send() can raise.
    try:
        send(diagnostics.render_bytes(
            diagnostics.collect(settings=settings, printer=_first(cups),
                                queue=queue, driver=driver,
                                plan=_plan(settings, refused)),
            diagnostics._page_size(settings)), "OTP status")
        log("status sheet submitted")
    except Exception as exc:                     # noqa: BLE001
        # Do not go on to generate a pad for a printer that just refused a
        # single sheet.
        log(f"could not print the status sheet: {exc}")
        return 1

    if not settings.auto_print:
        log("auto_print is off; stopping after the status sheet")
        return 1
    if refused:
        log(f"not printing a live pad: {refused}")
        return 1

    # 2. The codeword, and not earlier. Rolling one draws from the CSPRNG,
    #    which blocks until the kernel has seeded it. The status sheet
    #    needs no codeword and must never carry one, so it goes out first
    #    and reports the CRNG state; in this mode it is the only place the
    #    unit can say anything.
    announced = [None]
    missing = [0]

    def waiting(seconds):
        # The plug stays live during this unbounded wait: the sheet in the
        # tray says unplugging aborts. Same rule as countdown(): one empty
        # answer is a hiccup, GONE_AFTER in a row is a cable.
        try:
            present = bool(cups.devices())
        except Exception:                        # noqa: BLE001
            present = False
        missing[0] = 0 if present else missing[0] + 1
        if missing[0] >= GONE_AFTER:
            raise Aborted("printer disconnected while waiting for entropy")

        # First, then every 30s: the journal lives in RAM and this wait
        # has no upper bound.
        if announced[0] is not None and seconds - announced[0] < 30.0:
            return
        announced[0] = seconds
        log(f"waiting for the kernel CSPRNG to be seeded ({seconds:.0f}s so "
            f"far); no key material can be drawn until it is")

    try:
        waited = gen_module().wait_for_crng(on_wait=waiting, sleep=sleep)
    except Aborted as exc:
        log(f"aborted: {exc}")
        return 1
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

    # 4. The manual, before the pads. A pad is useless to someone who does
    #    not know the rules, and if the paper runs out, what survives
    #    should be the instructions rather than half a pad.
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
    # Reset before generating and read after, so the final sheet reports
    # what this pad was made from, not what a probe predicts.
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
        # The pad copies go through PadPairJob, not send(), so copy A
        # needs the same wait for the queue. Copy B gets it from the tray
        # break.
        drain(cups, queue, sleep, log, timeout=_drain_timeout(settings))
        job.print_next_copy()
        log(f"copy A submitted ({settings.pages} pages)")

        # 7. The separator IS the prompt. With no buttons nothing can wait
        #    for a keypress, so the sheet names the deadline instead.
        send(sheet(SWAP, codeword=codeword, settings=settings,
                   seconds=settings.auto_swap_delay), "REMOVE COPY A")

        # Wait for copy A and the separator to come out before timing the
        # tray break: lp returns once a job is spooled, not printed.
        drain(cups, queue, sleep, log, timeout=_drain_timeout(settings))
        # If copy A did not reach paper, copy B will not either. Stop and
        # say what the printer said.
        fault = _fault(cups, queue)
        if fault:
            raise Aborted(f"the printer reports: {fault}")
        countdown(cups, settings.auto_swap_delay, buttons=buttons,
                  sleep=sleep, log=log)

        job.print_next_copy()
        log("copy B submitted")

        # finish() below purges the spool with `cancel -x -a`, which
        # cancels every job on the queue including one still printing.
        # Copy B was only just handed to lp, so wait for it to drain.
        drained = drain(cups, queue, sleep, log,
                        timeout=_drain_timeout(settings))
        if not drained:
            log("queue never drained; leaving the spool rather than "
                "cancelling a copy that may still be printing")
        # A drained queue is necessary but not sufficient: under
        # ErrorPolicy abort-job it empties just as fast when every job
        # failed. Ask the printer as well.
        purge = drained
        fault = _fault(cups, queue)
        if fault:
            log(f"the queue drained but the printer reports: {fault}")
        # A fault means the tray may be empty, so the HALF sheet is the
        # right one even though the queue drained.
        printed_b = drained and not fault
        trouble = fault or trouble
    except Aborted as exc:
        log(f"aborted mid-pair: {exc}")
        trouble = trouble or str(exc)
        # The printer is gone, so nothing can be mid-print. A spooled copy
        # of the pad may remain, so purging is both safe and necessary.
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

    # 8. What they are now holding, which is not always a pair. Claiming
    #    "two identical copies" over half a pair is worse than silence.
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


# The shared decision (see printer.fault_text), so the panel and the
# headless run cannot disagree about the same printer.
_fault = printer.fault_text


MANUAL_PAGES = 28          # the rendered A5 manual, for the paper estimate


def _media(settings: Settings) -> str:
    """What actually comes out of the tray, which is not always `paper`."""
    return settings.lp_options.get("media", settings.paper)


# Seconds of drain budget per physical sheet, plus a fixed floor. A fixed
# bound would time out on a large pad (pages = 1000 is 250 sheets), skip
# the purge and report a good pair as half.
DRAIN_PER_SHEET = 12.0
DRAIN_FLOOR = 300.0


def _drain_timeout(settings: Settings) -> float:
    return DRAIN_FLOOR + DRAIN_PER_SHEET * max(settings.sheets, 1)


def _manual_options(settings: Settings) -> dict:
    """
    Two manual pages per sheet on A4 or Letter.

    The manual is laid out A5, exactly half of A4, and paper is a real
    constraint in this mode. Narrower paper gets one page a sheet. The
    media size is always included so the job is never left to the queue's
    default.
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

    Uses `Settings.sheets`, which already accounts for the imposition (A7
    is two pad pages to an A6 sheet).
    """
    # status + tabula + swap + done
    return _manual_sheets(settings) + settings.sheets * 2 + 4


def _plan(settings: Settings, refused: str = "") -> list[str]:
    """The countdown notice printed on the status sheet."""
    if refused and settings.auto_print:
        return [f"THIS UNIT WILL NOT PRINT A PAD: {refused}.",
                "Key material made on this machine could be written to its "
                "storage, so live pads are refused. See SECURITY POSTURE "
                "below. Reflash the card with the provided image, or rerun "
                "the installer and reboot.",
                "Training pads are not secret and still print: set "
                "training = yes in otp-unit.conf."]
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
    The key source, for the pad's own sheet.

    Someone may keep this page with the pad for years; without a hardware
    TRNG what they hold is stream-cipher output, not a one-time pad.
    """
    try:
        if tally is not None and tally.total:
            return tally.summary()
        # No tally means nothing was generated through this path; fall
        # back to a probe and say that is what it is.
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
            # A failed copy B may mean the report of it cannot be printed
            # either, so the warning goes here in advance, while there is
            # still a working printer.
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
                # The printer usually knows why: out of paper, cover open,
                # jammed.
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
                (None, "THIS PAD IS ONE-WAY. Only one of you ever encrypts "
                       "with it; the other only decrypts. If you both sent "
                       "from it you could each use the same page, and that "
                       "breaks both messages. For replies, print a second "
                       "pair."),
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
                (None, "4. ONE DIRECTION PER PAD. The holder of one copy "
                       "sends, the holder of the other receives. Never both."),
            ]),
            diagnostics.Section("TO USE IT", [
                (None, "Both ends work from the same numbered page. Turn "
                       "letters into numbers (A=0..Z=25), ADD the key to your "
                       "message letter by letter, wrap past Z back to A. To "
                       "read one, SUBTRACT instead."),
                (None, "The tabula recta card does the arithmetic for you: "
                       "find the message letter along the top and the key "
                       "letter down the side."),
                # Only where the pages carry one.
                *([(None, "Begin every message with the AUTH group printed "
                          "in the page header, encrypted like the rest. The "
                          "receiver decrypts it first and REJECTS the "
                          "message if it does not match their own page.")]
                  if settings.with_auth else []),
                (None, "Pad the message out with A to the length you agreed "
                       "at the handover, so its real length gives nothing "
                       "away."),
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

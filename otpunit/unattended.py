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


def open_buttons():
    """GPIO buttons if they can be opened at all, else None."""
    try:
        from otpunit.hw.buttons import GpioButtons

        return GpioButtons()
    except Exception:                            # noqa: BLE001
        # No gpiozero, no pins, no permissions -- all the same answer.
        return None


def countdown(cups, seconds, buttons=None, sleep=None, log=print,
              step=1.0) -> str:
    """
    Wait, watching the printer and the buttons.

    Returns "elapsed", "pressed", or raises Aborted if the printer was
    unplugged -- which is how someone with no other controls says no.
    """
    sleep = sleep or time.sleep
    pressed = _press_waiter(buttons)
    waited = 0.0
    while waited < seconds:
        if pressed():
            log("button pressed; starting now")
            return "pressed"
        try:
            if not cups.devices():
                raise Aborted("printer disconnected")
        except Aborted:
            raise
        except Exception as exc:                 # noqa: BLE001
            # A failed lookup is not the same as an unplugged printer.
            # Carry on rather than throwing away a pad over a hiccup.
            log(f"printer lookup failed, continuing: {exc}")
        sleep(step)
        waited += step
    return "elapsed"


def run(cups, settings: Settings = None, queue: str = "OTP", log=print,
        sleep=None, buttons=None, vocabulary=None) -> int:
    """
    Status sheet, a pause, then a pad pair, with printed sheets between.

    Returns 0 if a pair was printed, 1 if not.
    """
    settings = settings or Settings()
    sleep = sleep or time.sleep
    vocabulary = vocabulary or Vocabulary()

    codeword = settings.auto_codeword or vocabulary.random()

    def send(data, title, options=None):
        cups.submit(data, name=queue, title=title, options=options or {})

    # 1. Say what is about to happen, before doing any of it.
    send(diagnostics.render_bytes(
        diagnostics.collect(settings=settings, printer=_first(cups),
                            queue=queue, plan=_plan(settings)),
        diagnostics._page_size(settings)), "OTP status")
    log("status sheet submitted")

    if not settings.auto_print:
        log("auto_print is off; stopping after the status sheet")
        return 1

    # 2. Wait, so there is time to read the sheet and pull the plug.
    try:
        countdown(cups, settings.auto_delay, buttons=buttons, sleep=sleep,
                  log=log)
    except Aborted as exc:
        log(f"aborted: {exc}")
        return 1

    # 3. The tabula recta, which is what makes a pad usable by hand
    #    without the manual. No key material, so it is printed first and
    #    unconditionally.
    try:
        card = jobs.generate(jobs.JobSpec(jobs.JobKind.TABULA, "", settings))
        send(card, "TABULA RECTA", settings.lp_options)
        log("tabula recta submitted")
    except Exception as exc:                     # noqa: BLE001
        # Useful, not essential. Losing it must not lose the pads.
        log(f"tabula recta failed, continuing: {exc}")

    # 4. The pair itself, generated once and submitted twice so the two
    #    copies are byte-identical -- which is what makes them a pair.
    spec = jobs.JobSpec(jobs.JobKind.PAD_PAIR, codeword, settings)
    job = jobs.PadPairJob(spec, cups, queue)
    try:
        job.generate()
    except Exception as exc:                     # noqa: BLE001
        log(f"generation failed: {exc}")
        return 1

    try:
        job.print_next_copy()
        log(f"copy A submitted ({settings.pages} pages)")

        # 5. The separator IS the prompt. With no buttons nothing can wait
        #    for a keypress, so the sheet names the deadline instead.
        send(sheet(SWAP, codeword=codeword, settings=settings,
                   seconds=settings.auto_swap_delay), "REMOVE COPY A")
        countdown(cups, settings.auto_swap_delay, buttons=buttons,
                  sleep=sleep, log=log)

        job.print_next_copy()
        log("copy B submitted")
    except Aborted as exc:
        log(f"aborted mid-pair: {exc}")
        return 1
    except Exception as exc:                     # noqa: BLE001
        log(f"printing failed: {exc}")
        return 1
    finally:
        job.finish()

    # 6. What they are now holding, and what it costs them to get wrong.
    send(sheet(DONE, codeword=codeword, settings=settings), "WHAT TO DO NOW")
    log("done")
    return 0


def _first(cups):
    try:
        devices = cups.devices()
    except Exception:                            # noqa: BLE001
        return None
    return devices[0] if devices else None


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
    return [
        f"THIS UNIT WILL PRINT A ONE-TIME PAD PAIR {when.upper()}.",
        f"Two identical copies, {settings.pages} pages each, plus a tabula "
        f"recta card. You do not need to do anything.",
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


def _key_source() -> str:
    """
    Said on the pad's own sheet, not just the status sheet.

    Someone may keep this page with the pad for years. If the unit had no
    hardware TRNG, what they are holding is stream-cipher output rather
    than a one-time pad -- still strong, but not unbreakable-in-principle,
    and that is a difference they are entitled to know about the specific
    pad in their hand.
    """
    import otp_generator as gen

    try:
        if gen.entropy_source() == "hwrng+urandom":
            return "hardware TRNG, mixed with the system CSPRNG"
        return ("NO HARDWARE RNG WAS FOUND. This key came from the system "
                "CSPRNG alone, which makes it a very strong stream cipher "
                "rather than a true one-time pad.")
    except Exception:                            # noqa: BLE001
        return "unknown"


def sheet(kind, codeword="", settings=None, seconds=0):
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
        ]
    else:
        heading = dict(
            title="YOUR PAD PAIR IS PRINTED",
            subtitle="Everything above this sheet, except the tabula recta "
                     "card, is secret. This sheet is not.")
        sections = [
            diagnostics.Section("YOU NOW HAVE A PAD PAIR", [
                ("Codeword", codeword or "(unset)"),
                ("Pages per copy", str(settings.pages)),
                ("Format", "A7" if settings.a7 else "A6"),
                ("Key source", _key_source()),
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

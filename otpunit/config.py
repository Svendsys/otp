"""Print unit settings.

With the overlay root enabled, anything written to / is discarded at
power-off. Settings that should survive a reboot therefore live on the boot
partition, which is outside the overlay.

Note that raspi-config's enable_overlayfs leaves /boot writable -- making it
read-only is a separate step most operators will not have taken -- so the
remount below is a fallback for those who have, not the normal path.

Deliberately not persisted: the last codeword used. It is the one field that
would link the unit to a pad it produced, and the envelope labels are the
real register.
"""
from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, asdict, fields, replace

import otp_generator as gen

CONFIG_PATH = "/boot/firmware/otp-unit.conf"

PAGE_CHOICES = (10, 20, 50, 100, 200, 500, 1000)
AUTH_SIZE_CHOICES = (0, 3, 4, 5, 6, 7, 8)  # 0 means no AUTH group

# Pad pages are A6. Most printers have A4 or Letter loaded, so the default
# imposes four pad pages per sheet for cutting down on a guillotine. The
# tiling is done in the PDF rather than with CUPS number-up: a guillotine
# cuts the whole stack at once, so every sheet needs identical geometry, and
# driver-side scaling would drift the cut line from sheet to sheet.
PAPER_CHOICES = ("A4", "LETTER", "A6")
# Kept equal to the CLI's ceiling in otp_generator.main.
MAX_PAGES = 9999

PAPER_LABELS = {
    "A4": "A4, 4-UP + CUT",
    "LETTER": "LETTER, 4-UP + CUT",
    "A6": "A6 SHEETS",
}

# How long a panel-less unit waits after the status sheet before printing
# a pair. Offered on the panel too, because the reason to change it is to
# set a unit up now and run it headless later -- and the alternative is
# powering down and editing a file on the SD card in another computer.
AUTO_DELAY_CHOICES = (0, 60, 300, 900, 3600)


def _codeword_ok(word: str) -> bool:
    """
    A hand-typed codeword has to survive being used as a filename.

    Imported lazily rather than at module scope because config is loaded
    before anything else and must not depend on the codewords module.
    """
    from otpunit import codewords

    return codewords.is_filename_safe(word)


@dataclass
class Settings:
    pages: int = 100
    a7: bool = False
    with_auth: bool = True
    auth_size: int = gen.GROUP_SIZE
    training: bool = False
    font_size: float = 9.0
    paper: str = "A4"
    # --- unattended operation ------------------------------------------
    # What the unit does when there is no panel and no buttons attached.
    # Defaulting this ON is deliberate. A unit that can only tell you to go
    # and find an SSD1306 is useless to someone who cannot get one, and the
    # pads are the point -- the panel only ever chose a codeword and a page
    # count, both of which have perfectly good defaults.
    auto_print: bool = True
    # Seconds between the status sheet and the pad. Long enough to read the
    # sheet and pull the plug, short enough not to abandon someone.
    auto_delay: int = 300
    # Seconds the tray break gets, since with no buttons nothing can wait
    # for a keypress before copy B starts.
    auto_swap_delay: int = 90
    # Empty means roll one. Set it to agree a codeword out of band.
    auto_codeword: str = ""
    # Print the manual before the pads. On by default and deliberately so:
    # a pad is useless to someone who does not know the rules, and one
    # reused page undoes the whole scheme. Costs paper, which is the right
    # trade for the situation this mode exists for.
    auto_manual: bool = True

    @property
    def imposed(self) -> bool:
        """True when pad pages are tiled four to a sheet for cutting."""
        return self.paper in ("A4", "LETTER") and not self.a7

    @property
    def lp_options(self) -> dict:
        """
        CUPS options: the media size, and the two defaults that would
        otherwise silently invalidate the cut geometry.

        `print-scaling=none` is the important one. The imposition is baked
        into the PDF at exact millimetre positions and the crop marks are a
        promise about where the blade goes -- but cups-filters defaults
        print-scaling to `auto`, which fits the page to the printable area.
        On a printer with a 4.23mm unprintable band that is roughly a 4%
        shrink, so every cut lands off the mark and the two halves of a pair
        no longer match. This code used to only send `media` and rely on the
        driver doing nothing; a comment even named the risk. Saying nothing
        is not the same as saying no.

        `sides=one-sided` guards a queue that defaults to duplex, which
        would put two pad pages on one slip of paper -- and "destroy the
        page after use" has no answer for a page with another page's key on
        its back.
        """
        fixed = {"print-scaling": "none", "sides": "one-sided"}
        if self.a7:
            # A7 emits landscape-A6 sheets with their own cut line; the
            # paper setting does not apply to it, and sending media=A4 would
            # ask the driver to scale a sheet that is already the right size.
            return {"media": "A6", **fixed}
        media = {"A6": "A6", "LETTER": "Letter"}.get(self.paper, "A4")
        return {"media": media, **fixed}

    @property
    def sheets(self) -> int:
        """Physical sheets a job of `pages` pad pages will use."""
        if self.a7:
            return -(-self.pages // 2)      # two pad pages per A6 sheet
        return -(-self.pages // 4) if self.imposed else self.pages

    @property
    def chars_per_page(self) -> int:
        return 375 if self.a7 else 665

    @property
    def format_label(self) -> str:
        return "A7 2-UP" if self.a7 else "A6"

    def validate(self) -> list[str]:
        """
        Return human-readable problems, empty if the settings are usable.

        Must never raise: this runs on a config file the unit's own header
        invites the operator to edit by hand, and load() calls it before the
        panel exists. Anything that would divide by or index into a bad
        value has to be gated behind the check that rejects it.
        """
        problems = []
        if self.pages < 1:
            problems.append("pages must be at least 1")
        # The same ceiling the CLI enforces. It landed there and not here,
        # but this is the side that reads a hand-edited file off the boot
        # partition, so it is the side that actually receives a silly
        # number. Above 9999 the page-number field runs to five digits while
        # the manual's message header stays four, and the two ends stop
        # being able to name the same page.
        if self.pages > MAX_PAGES:
            problems.append(f"pages cannot exceed {MAX_PAGES}")
        if self.with_auth and self.auth_size < 1:
            problems.append("auth size must be at least 1")
        # A negative auth_size shortens the draw in draw_pad_page instead of
        # lengthening it, which silently prints fewer key letters than the
        # page is laid out for -- and at -chars_per_page, blank pages.
        if self.auth_size < 0:
            problems.append("auth size cannot be negative")
        if self.paper not in PAPER_CHOICES:
            problems.append("unknown paper size")
        # Zero is allowed: it means "print the pad immediately", which is
        # what someone who already knows what this unit does will want.
        if self.auto_delay < 0:
            problems.append("auto delay cannot be negative")
        if self.auto_swap_delay < 0:
            problems.append("auto swap delay cannot be negative")
        if self.auto_codeword and not _codeword_ok(self.auto_codeword):
            problems.append("auto codeword must be A-Z, 0-9, - or _ only")
        # isfinite as well as > 0: nan compares False against BOTH bounds, so
        # a bare `<= 0` check lets it through to calc_max_chars, which then
        # raises out of load() before the panel exists.
        if not math.isfinite(self.font_size) or self.font_size <= 0:
            # Return here: calc_max_chars divides by a font-size-derived
            # spacing, so a bad size would raise instead of reporting.
            problems.append("font size must be a positive number")
            return problems
        if self.font_size > gen.max_body_font_size(self.a7, self._box_width()):
            problems.append("font size too large for the page width")
        if self.with_auth and self.auth_size > gen.max_auth_size(
                self.font_size, self.a7, self._box_width()):
            problems.append("auth group too wide for the header")
        if self.chars_per_page > gen.calc_max_chars(
                self.font_size, self.a7, self._box_height()):
            problems.append("chars per page exceed the format")
        return problems

    def _box_width(self):
        """Width of the box a pad page is drawn into, for fit checks."""
        if self.a7:
            return None                      # calc uses the A7 default
        if self.paper == "LETTER":
            return gen.LETTER[0] / 2
        if self.paper == "A4":
            return gen.A4[0] / 2
        return None

    def _box_height(self):
        """Height of that box. A Letter quarter is shorter than A6."""
        if self.a7 or not self.imposed:
            return None
        return (gen.LETTER if self.paper == "LETTER" else gen.A4)[1] / 2


def _parse(text: str) -> dict:
    types = {f.name: f.type for f in fields(Settings)}
    out = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key not in types:
            continue
        kind = types[key]
        try:
            if kind is bool or kind == "bool":
                out[key] = value.lower() in ("1", "true", "yes", "on")
            elif kind is int or kind == "int":
                out[key] = int(value)
            elif kind is float or kind == "float":
                out[key] = float(value)
            else:
                out[key] = value
        except ValueError:
            # A malformed line must not stop the unit booting; the default
            # for that field stands and the operator can fix it on-panel.
            continue
    return out


def load(path: str = CONFIG_PATH) -> Settings:
    """
    Read settings, falling back to a default for any field that is not
    usable. The file says "safe to edit by hand", so it will be -- and a
    hand-edited `auth_size = -700` must not silently produce blank pad
    pages. Never raises: a bad config must not stop the unit booting.
    """
    try:
        with open(path, "r") as handle:
            values = _parse(handle.read())
    except OSError:
        return Settings()

    try:
        settings = Settings(**values)
    except TypeError:
        return Settings()
    if not settings.validate():
        return settings

    # Something is out of range. Keep the fields that are individually sane
    # rather than discarding the whole file.
    settings = Settings()
    for key, value in values.items():
        candidate = replace(settings, **{key: value})
        if not candidate.validate():
            settings = candidate
    return settings


def render(settings: Settings) -> str:
    lines = [
        "# OTP print unit settings.",
        "# Written by the unit; safe to edit by hand.",
        "",
    ]
    for key, value in asdict(settings).items():
        # The unit reads auto_codeword but never writes one. A codeword is
        # not key material, but it names a live pad, and the SD card is the
        # part most likely to be captured together with the unit -- so the
        # card must never learn which pad this unit produced. A human who
        # deliberately puts one there is making their own trade; the unit
        # does not make it for them, and does not record it afterwards.
        if key == "auto_codeword":
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"{key} = {value}")
    lines.append("")
    lines.append("# auto_codeword = MODIFIER-NOUN   # optional; read but"
                 " never written by the unit")
    return "\n".join(lines) + "\n"


def save(settings: Settings, path: str = CONFIG_PATH, remount: bool = True) -> bool:
    """
    Persist settings to the boot partition. Returns False rather than raising
    if the partition is read-only and cannot be remounted -- losing a setting
    is not worth taking the unit down for.
    """
    directory = os.path.dirname(path) or "."
    try:
        _write(settings, path)
        return True
    except OSError:
        pass
    if not remount:
        return False

    # Only reached when the partition is genuinely read-only. Remount, write,
    # and put it back exactly as it was -- the previous version's `finally`
    # was attached to the wrong `try`, so a write that failed for an
    # unrelated reason (a full boot partition, say) left the filesystem
    # remounted read-only as a side effect.
    if not _remount(directory, "rw"):
        return False
    try:
        _write(settings, path)
        return True
    except OSError:
        return False
    finally:
        _remount(directory, "ro")


def _remount(directory: str, mode: str) -> bool:
    """subprocess, not os.system: the path comes from --config, and a shell
    has no business seeing it. Returns whether the remount succeeded."""
    result = subprocess.run(["mount", "-o", f"remount,{mode}", directory],
                            capture_output=True)
    return result.returncode == 0


def _write(settings: Settings, path: str) -> None:
    with open(path, "w") as handle:
        handle.write(render(settings))

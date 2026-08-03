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

PAPER_LABELS = {
    "A4": "A4, 4-UP + CUT",
    "LETTER": "LETTER, 4-UP + CUT",
    "A6": "A6 SHEETS",
}


@dataclass
class Settings:
    pages: int = 100
    a7: bool = False
    with_auth: bool = True
    auth_size: int = gen.GROUP_SIZE
    training: bool = False
    font_size: float = 9.0
    paper: str = "A4"
    printer: str = ""

    @property
    def imposed(self) -> bool:
        """True when pad pages are tiled four to a sheet for cutting."""
        return self.paper in ("A4", "LETTER") and not self.a7

    @property
    def lp_options(self) -> dict:
        """
        CUPS options. Only the media size -- the imposition is already in
        the PDF, so nothing here may scale or re-tile it.
        """
        return {"media": {"A6": "A6", "LETTER": "Letter"}.get(self.paper, "A4")}

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
        """Return human-readable problems, empty if the settings are usable."""
        problems = []
        if self.pages < 1:
            problems.append("pages must be at least 1")
        if self.with_auth and self.auth_size < 1:
            problems.append("auth size must be at least 1")
        # A negative auth_size shortens the draw in draw_pad_page instead of
        # lengthening it, which silently prints fewer key letters than the
        # page is laid out for -- and at -chars_per_page, blank pages.
        if self.auth_size < 0:
            problems.append("auth size cannot be negative")
        if self.paper not in PAPER_CHOICES:
            problems.append("unknown paper size")
        if self.font_size <= 0:
            problems.append("font size must be positive")
        if self.chars_per_page > gen.calc_max_chars(self.font_size, self.a7):
            problems.append("chars per page exceed the format")
        return problems


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
        if isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"{key} = {value}")
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

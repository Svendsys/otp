"""The status sheet a unit prints when it has no panel to talk through.

A print unit with no OLED is mute. It cannot report that the I2C bus is
empty, that a driver is missing, or that swap is somehow still on -- and it
has no buttons to be driven with either. The one output device it is
guaranteed to have is the printer, so on a headless unit the printer
becomes the console: plug one in and the unit prints everything it knows
about itself, plus what hardware to add next.

Two rules shape this module.

Nothing here may raise. A diagnostic that crashes while diagnosing is
worse than none at all, so every probe is individually guarded and a
failed one prints its error in place of its value. That is a finding too.

Nothing here touches key material. This sheet is not secret and may well
be photographed and emailed to someone for help, which is rather the
point, so it must never grow a field that carries a codeword or key.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

import otp_generator as gen

# Wiring, repeated here rather than referenced, because the sheet has to be
# useful to someone holding it next to a Pi with no other documentation.
PINOUT = [
    ("OLED SDA", "GPIO2", "pin 3"),
    ("OLED SCL", "GPIO3", "pin 5"),
    ("OLED VCC", "3V3", "pin 1"),
    ("OLED GND", "GND", "pin 6"),
    ("Button UP", "GPIO5", "pin 29, to GND"),
    ("Button DOWN", "GPIO6", "pin 31, to GND"),
    ("Button OK", "GPIO13", "pin 33, to GND"),
]


@dataclass
class Section:
    title: str
    rows: list = field(default_factory=list)


def _try(fn, default="unavailable"):
    """
    Run a probe, and turn any failure into a printable value.

    A missing file is reported as absent rather than as a traceback: on a
    sheet read by someone holding a Pi, "not present on this system" is
    the finding, and a truncated FileNotFoundError is just noise.
    """
    try:
        value = fn()
    except FileNotFoundError as exc:
        return f"not present ({exc.filename})"
    except ModuleNotFoundError as exc:
        return f"NOT INSTALLED ({exc.name})"
    except Exception as exc:                     # noqa: BLE001 - deliberate
        return f"! {type(exc).__name__}: {exc}"[:70]
    if value is None or value == "":
        return default
    return value


def _read(path, limit=200):
    return Path(path).read_text(errors="replace")[:limit].strip().strip("\x00")


def _first_line(path):
    return _read(path).splitlines()[0].strip()


def _cpuinfo_field(name):
    for line in _read("/proc/cpuinfo", 8000).splitlines():
        if line.startswith(name):
            return line.split(":", 1)[1].strip()
    return None


def _os_release(key="PRETTY_NAME"):
    for line in _read("/etc/os-release", 2000).splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _meminfo_total():
    for line in _read("/proc/meminfo", 2000).splitlines():
        if line.startswith("MemTotal:"):
            kb = int(line.split()[1])
            return f"{kb / 1024:.0f} MiB"
    return None


def _uptime():
    seconds = float(_read("/proc/uptime").split()[0])
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def _cpu_temp():
    milli = int(_read("/sys/class/thermal/thermal_zone0/temp"))
    return f"{milli / 1000:.1f} C"


def _swap():
    """Anything beyond the header line means swap is on. That is a defect."""
    lines = [ln for ln in _read("/proc/swaps", 2000).splitlines()[1:] if ln.strip()]
    if not lines:
        return "none  (correct)"
    return "ON: " + "; ".join(ln.split()[0] for ln in lines) + "  <- KEY MATERIAL CAN BE PAGED OUT"


def _root_filesystem():
    for line in _read("/proc/mounts", 20000).splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] == "/":
            overlay = "overlay" in parts[2]
            ro = "ro" in parts[3].split(",")
            state = "read-only overlay" if overlay and ro else parts[2]
            if not overlay:
                state += "  (overlay NOT enabled -- writes persist)"
            return state
    return None


def _networks():
    """Radios off is not the same as offline. Say which links are actually up."""
    up = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        if name == "lo":
            continue
        try:
            state = _read(os.path.join(path, "operstate"))
        except Exception:                        # noqa: BLE001
            state = "?"
        if state == "up":
            up.append(name)
    if not up:
        return "no links up  (correct)"
    return "UP: " + ", ".join(up) + "  <- this unit is not offline"


def _entropy():
    avail = int(_read("/proc/sys/kernel/random/entropy_avail"))
    verdict = "ok" if avail >= 256 else "LOW"
    return f"{avail} bits ({verdict})"


def _entropy_source():
    """
    Whether key material comes from the SoC's TRNG or only the CSPRNG.

    This is the difference between a one-time pad and a stream cipher. A
    pad expanded algorithmically from a seed is the latter by definition,
    however good the algorithm, so a unit falling back to os.urandom alone
    has to say so on paper rather than let it pass unnoticed.
    """
    if gen.entropy_source() == "hwrng+urandom":
        return "hardware TRNG + CSPRNG (correct)"
    return ("CSPRNG ONLY -- no /dev/hwrng. Pads are stream-cipher output, "
            "not true one-time pads.")


def _disk():
    usage = shutil.disk_usage("/")
    return (f"{usage.used / 2**30:.1f} / {usage.total / 2**30:.1f} GiB used, "
            f"{usage.free / 2**30:.1f} GiB free")


def _i2c_buses():
    buses = sorted(glob.glob("/dev/i2c-*"))
    if not buses:
        return "NO I2C DEVICE NODES -- enable with dtparam=i2c_arm=on"
    return ", ".join(os.path.basename(b) for b in buses)


def _i2c_scan(bus=1):
    """
    Which addresses answer on the bus.

    An SSD1306 is almost always 0x3C, occasionally 0x3D. Reporting the raw
    scan rather than just "no display" is the difference between "you have
    not wired it up" and "you wired it up and it is at the other address".
    """
    try:
        from smbus2 import SMBus
    except Exception:                            # noqa: BLE001
        return "smbus2 not installed; cannot scan"
    found = []
    with SMBus(bus) as smbus:
        for address in range(0x03, 0x78):
            try:
                smbus.write_quick(address)
            except Exception:                    # noqa: BLE001
                continue
            found.append(f"0x{address:02X}")
    if not found:
        return "nothing responds on the bus"
    return ", ".join(found)


def _module_version(import_name, dist_name=None):
    """
    Prove the module imports, then name its version.

    Importing is the part that matters -- luma and gpiozero are the two
    things whose absence stops the panel working -- so the import is done
    first and allowed to raise. `luma` on its own is a namespace package
    with no __version__, hence asking the installed-distribution metadata
    rather than the module object.
    """
    from importlib import metadata

    __import__(import_name)
    try:
        return metadata.version(dist_name or import_name)
    except Exception:                            # noqa: BLE001
        module = sys.modules.get(import_name)
        return getattr(module, "__version__", "installed")


def _command(argv, limit=60):
    if not Path(argv[0]).exists():
        return f"{argv[0]} not installed"
    result = subprocess.run(argv, capture_output=True, timeout=10)
    text = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    if not text:
        return f"exit {result.returncode}"
    return text.splitlines()[0][:limit]


def _image_build():
    """Written by the pi-gen stage; absent on a unit provisioned by hand."""
    return _read("/etc/otp-image-release", 400).replace("\n", "  ")


def collect(settings=None, printer=None, queue=None, driver=None,
            plan=None) -> list[Section]:
    """Everything worth knowing, as printable sections. Never raises."""
    sections = []

    sections.append(Section("WHAT THIS SHEET IS", [
        (None, "This unit booted with no display attached, so it printed its "
               "status here instead. The printer is the only output device a "
               "headless unit has."),
        (None, "NO KEY MATERIAL IS ON THIS SHEET -- no codeword, no key -- so "
               "sending it to someone for help cannot compromise a pad."),
        # It used to say "safe to show anyone", four sections above this
        # unit's board serial and the printer's serial number in its device
        # URI. Neither is key material and both are worth having when
        # diagnosing, but they identify a specific machine, and telling
        # someone a page is safe for anyone is a claim to get right.
        (None, "It does identify this hardware: the board serial below, the "
               "printer's serial inside its device URI, and any network link "
               "it can see. That names the unit, not the pad. Cross them out "
               "before you send it if that matters where you are."),
    ]))

    # The countdown notice, when the unit is about to print a pad on its
    # own. This is the part someone with no hardware actually needs.
    if plan:
        sections.append(Section("WHAT HAPPENS NEXT",
                                [(None, line) for line in plan]))

    sections.append(Section("CONFIGURATION", [
        (None, "Power the unit off, take the SD card to any computer, and "
               "edit otp-unit.conf on its first partition -- it is a FAT "
               "partition, so Windows, macOS and Linux can all read it. This "
               "works with no display, no buttons and no network."),
        ("auto_print", "yes/no -- print a pad pair on its own"),
        ("auto_delay", "seconds to wait first (0 = at once)"),
        ("auto_codeword", "leave empty to have one rolled"),
        ("pages", "pages per copy (default 100)"),
        ("paper", "A4, LETTER or A6"),
    ]))

    sections.append(Section("IF YOU HAVE NO PARTS AT ALL", [
        (None, "You do not need buttons. Briefly bridging header pin 33 to "
               "pin 34 with a wire, a paperclip or a screwdriver is a button "
               "press, and during the wait above that means START NOW."),
        (None, "You do not need the display to get pads. It only ever chose "
               "a codeword and a page count, and the defaults are fine. "
               "Everything below is how to make the unit nicer to use, not "
               "how to make it work."),
    ]))

    sections.append(Section("WIRING (OPTIONAL)", [(f"{sig}", f"{pin} / {hdr}")
                                                  for sig, pin, hdr in PINOUT]))

    sections.append(Section("PANEL AND BUTTONS", [
        ("I2C bus nodes", _try(_i2c_buses)),
        ("Addresses seen", _try(_i2c_scan)),
        ("Expected", "0x3C (SSD1306); some modules use 0x3D"),
        ("luma.oled", _try(lambda: _module_version("luma.oled"))),
        ("gpiozero", _try(lambda: _module_version("gpiozero"))),
        ("lgpio", _try(lambda: _module_version("lgpio"))),
    ]))

    printer_rows = [
        ("Device URI", printer.uri if printer else "none"),
        ("Description", (printer.description or "-") if printer else "-"),
        ("Queue", queue or "not created"),
        ("Driver", driver or "unknown"),
        # cupsd has no --version; ask the package manager instead, which is
        # also what tells us whether CUPS is installed at all.
        ("CUPS", _try(lambda: _command(
            ["/usr/bin/dpkg-query", "-W", "-f=${Version}", "cups"]))),
    ]
    sections.append(Section("PRINTER", printer_rows))

    sections.append(Section("UNIT", [
        ("Model", _try(lambda: _first_line("/proc/device-tree/model"))),
        ("Revision", _try(lambda: _cpuinfo_field("Revision"))),
        ("Serial", _try(lambda: _cpuinfo_field("Serial"))),
        ("Memory", _try(_meminfo_total)),
        ("CPU temp", _try(_cpu_temp)),
        ("Uptime", _try(_uptime)),
    ]))

    sections.append(Section("IMAGE", [
        ("OS", _try(_os_release)),
        ("Kernel", _try(lambda: f"{os.uname().release} {os.uname().machine}")),
        ("Python", sys.version.split()[0]),
        ("reportlab", _try(lambda: _module_version("reportlab"))),
        ("Build", _try(_image_build, "hand-provisioned (no image release file)")),
    ]))

    # The four claims the README makes about this device, each checked
    # rather than asserted. A unit that fails one should say so on paper.
    sections.append(Section("SECURITY POSTURE", [
        # First, because it is the one that decides whether what comes out
        # of this unit is a one-time pad at all.
        ("Key source", _try(_entropy_source)),
        ("Swap", _try(_swap)),
        ("Root filesystem", _try(_root_filesystem)),
        ("Network links", _try(_networks)),
        ("Entropy", _try(_entropy)),
        ("Disk", _try(_disk)),
    ]))

    if settings is not None:
        sections.append(Section("SETTINGS", [
            ("Pages per pad", str(settings.pages)),
            ("Page format", "A7" if settings.a7 else "A6"),
            ("Paper", settings.paper),
            ("Imposed 4-up", "yes" if settings.imposed else "no"),
            ("AUTH group", f"{settings.auth_size} letters"
                           if settings.with_auth else "off"),
            ("Body font", f"{settings.font_size} pt"),
            ("Training mode", "ON -- pads are NOT secure" if settings.training
                              else "off"),
        ]))

    return sections


# --- rendering ---------------------------------------------------------

TITLE_SIZE = 15
HEAD_SIZE = 8.5
BODY_SIZE = 7.5
LEADING = 9.6


def _wrap(text, font, size, width, canvas):
    """Greedy wrap against real string widths, not an average character."""
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if canvas.stringWidth(trial, font, size) <= width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


STATUS_TITLE = "OTP PRINT UNIT - STATUS SHEET"
STATUS_SUBTITLE = ("No display detected. This sheet is printed automatically; "
                   "it contains no key material.")


def render(sections, output, page_size=A4, title=STATUS_TITLE,
           subtitle=STATUS_SUBTITLE) -> None:
    """
    Draw the report onto `output` (a path or a binary stream).

    The heading is a parameter because these same two columns are used for
    the sheets that stand in for a panel, and the status sheet's "contains
    no key material" is a dangerous thing to print at the top of the sheet
    that tells someone to keep the secret pages above it.

    Content that does not fit runs onto another sheet. It used to be
    counted and thrown away, with a one-line footnote saying how much had
    gone: on A6 that was 241 of 282 lines, so the entire user interface of
    a mute unit came out as a title, a wiring table and an apology.
    """
    width, height = page_size
    canvas = gen.new_canvas(output, page_size, title="OTP unit")

    # A6 is 105mm wide. Two columns there leave 13pt of width for a value
    # beside a 30mm label, so every value wrapped to one word per line and
    # the sheet overflowed by an order of magnitude. Narrow paper gets one
    # column, a smaller margin and a label column scaled to fit.
    narrow = width < 400
    columns = 1 if narrow else 2
    margin = (9 if narrow else 14) * mm
    gutter = 8 * mm
    column_width = (width - 2 * margin - gutter * (columns - 1)) / columns
    label_width = min(30 * mm, column_width * 0.42)
    bottom = margin + 12

    def draw_heading(page_title):
        y = height - margin - TITLE_SIZE
        canvas.setFont("Helvetica-Bold", TITLE_SIZE)
        canvas.drawString(margin, y, page_title)
        y -= 11
        canvas.setFont("Helvetica", BODY_SIZE)
        for line in _wrap(subtitle, "Helvetica", BODY_SIZE,
                          width - 2 * margin, canvas):
            canvas.drawString(margin, y, line)
            y -= LEADING
        canvas.setLineWidth(0.6)
        canvas.line(margin, y + 3, width - margin, y + 3)
        return y - 7

    # Lay each section out before drawing any of it, as a list of rows that
    # are safe to break between. A label and its value must stay together --
    # they share a line -- so the row, not the item, is the unit of flow.
    def lay_out(section):
        """[(advance, [(kind, x_offset, text)])] for one section."""
        rows = [(LEADING + 2, [("head", 0, section.title)])]
        for label, value in section.rows:
            items, advance = [], 1.5
            if label is None:
                for line in _wrap(str(value), "Helvetica", BODY_SIZE,
                                  column_width, canvas):
                    items.append(("body", 0, line))
                    advance += LEADING
            else:
                lines = _wrap(str(value), "Helvetica", BODY_SIZE,
                              column_width - label_width, canvas) or [""]
                items.append(("label", 0, str(label)))
                for index, line in enumerate(lines):
                    items.append(("value" if index == 0 else "cont",
                                  label_width, line))
                    advance += LEADING
            rows.append((advance, items))
        rows.append((5, []))                     # space after the section
        return rows

    rows = [row for section in sections for row in lay_out(section)]
    column_top = draw_heading(title)
    available = column_top - bottom
    total = sum(advance for advance, _ in rows)

    # Balance the columns when the whole report fits on one sheet -- that is
    # the common case, and a full left column beside a stub looks broken.
    # Once it spills, fill each column to the floor instead: balancing a
    # multi-sheet report only makes every sheet of it ragged.
    target = total / columns if total <= available * columns else available
    target = min(max(target, LEADING * 4), available)

    column, x, y, used = 0, margin, column_top, 0.0

    def next_column():
        nonlocal column, x, y, used
        column += 1
        if column >= columns:
            canvas.showPage()
            column = 0
            draw_heading(f"{title} (CONTINUED)")
        x = margin + column * (column_width + gutter)
        y = column_top
        used = 0.0

    for index, (advance, items) in enumerate(rows):
        # Never strand a section title at the foot of a column.
        keep_with = (rows[index + 1][0] if items and items[0][0] == "head"
                     and index + 1 < len(rows) else 0)
        # The balance target applies to every column but the last one on
        # the sheet, which runs to the floor. Enforcing it everywhere sent
        # the final few rows onto a second, near-empty sheet.
        limit = target if column < columns - 1 else available
        if used and used + advance + keep_with > limit:
            next_column()
        for kind, offset, text in items:
            # A row taller than a whole column is the only case the row
            # bookkeeping cannot catch; break mid-row rather than draw off
            # the bottom edge.
            if y < bottom:
                next_column()
            if kind == "head":
                canvas.setFont("Helvetica-Bold", HEAD_SIZE)
                canvas.drawString(x, y, text)
                canvas.setLineWidth(0.3)
                canvas.line(x, y - 2, x + column_width, y - 2)
                y -= LEADING + 2
                continue
            canvas.setFont("Helvetica-Bold" if kind == "label"
                           else "Helvetica", BODY_SIZE)
            canvas.drawString(x + offset, y, text)
            # A label and its first value share a line.
            if kind != "label":
                y -= LEADING
        # Drive the cursor from the running total rather than from what was
        # drawn, so the measurement that decides the breaks and the drawing
        # that follows them cannot drift apart.
        used += advance
        y = column_top - used

    canvas.showPage()
    canvas.save()


def render_bytes(sections, page_size=A4, **heading) -> bytearray:
    """The sheet as bytes, for piping straight to lp."""
    import io

    buffer = io.BytesIO()
    render(sections, buffer, page_size, **heading)
    return bytearray(buffer.getvalue())


# --- headless mode -----------------------------------------------------

# Consecutive empty polls before believing the printer is really gone.
# Canonical: unattended.py takes its value from here.
GONE_AFTER = 3


def _page_size(settings):
    from reportlab.lib.pagesizes import LETTER

    from reportlab.lib.pagesizes import A6

    # A6 was missing, so an A6-only unit rendered A4 instruction sheets
    # and submitted them to a queue loaded with A6.
    return {"A4": A4, "LETTER": LETTER, "A6": A6}.get(
        getattr(settings, "paper", "A4"), A4)


def run_headless(cups, settings=None, poll_seconds=2.0, log=print,
                 sleep=None, once=False, sequence=None, buttons=None) -> int:
    """
    Wait for a printer, then run the unattended sequence against it.

    Runs once per connection, so a unit left plugged in overnight produces
    one pad pair and not a ream. Everything is injectable because this
    loop is the one part of headless mode a test can drive.
    """
    import time

    if sequence is None:
        from otpunit import unattended

        sequence = unattended.run

    sleep = sleep or time.sleep
    printed_for = None
    misses = 0

    while True:
        devices = []
        try:
            devices = cups.devices()
        except Exception as exc:                 # noqa: BLE001
            log(f"printer lookup failed: {exc}")

        if not devices:
            # Only re-arm after SEVERAL consecutive empties. devices()
            # returns [] for a busy cupsd or a timed-out lpinfo just as it
            # does for an unplugged cable, and re-arming on one of those
            # started a whole second pad pair -- 68 more sheets and a new
            # codeword -- with the first still in the tray. Measured: one
            # bad poll in five produced 12 pad-pair starts in 60 polls.
            misses += 1
            if printed_for is not None and misses >= GONE_AFTER:
                log("printer disconnected")
                printed_for = None
            if once:
                return 1
            sleep(poll_seconds)
            continue
        misses = 0

        device = devices[0]
        if device.uri == printed_for:
            if once:
                return 0
            sleep(poll_seconds)
            continue

        log(f"printer found: {device.uri}")
        queue = driver = None
        try:
            queue = cups.ensure_queue(device)
            driver = "driverless (IPP Everywhere)" if device.is_ipp else "PPD"
        except Exception as exc:                 # noqa: BLE001
            # Print the sheet anyway if a queue somehow exists -- a failed
            # setup is exactly the thing the operator needs told, and the
            # error itself is the most useful line on the page.
            log(f"queue setup failed: {exc}")
            driver = f"SETUP FAILED: {exc}"

        # Mark the connection handled BEFORE running the sequence. A
        # failure part-way through must not leave the loop thinking this
        # printer is still untouched and start over, printing copy A of a
        # second pair on top of the first.
        printed_for = device.uri
        try:
            result = sequence(cups, settings=settings, queue=queue or "OTP",
                              log=log, sleep=sleep, buttons=buttons,
                              driver=driver)
        except Exception as exc:                 # noqa: BLE001
            log(f"unattended sequence failed: {exc}")
            result = 1

        if once:
            return result
        sleep(poll_seconds)

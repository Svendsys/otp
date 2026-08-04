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


def collect(settings=None, printer=None, queue=None, driver=None) -> list[Section]:
    """Everything worth knowing, as printable sections. Never raises."""
    sections = []

    sections.append(Section("WHAT THIS SHEET IS", [
        (None, "This unit booted with no display attached, so it printed its "
               "status here instead. The printer is the only output device a "
               "headless unit has."),
        (None, "It will NOT print pads in this state: choosing a codeword and "
               "a page count needs the panel and buttons. Add the hardware "
               "below and reboot."),
    ]))

    sections.append(Section("WHAT TO DO NEXT", [
        (None, "1. Wire up a 128x64 SSD1306 OLED and three momentary buttons "
               "as listed under WIRING."),
        (None, "2. Reboot. The unit detects the panel on the I2C bus at "
               "start-up; nothing needs configuring."),
        (None, "3. If the panel still does not come up, compare the I2C scan "
               "below against 0x3C (or 0x3D)."),
        (None, "4. To print this sheet again, power-cycle with the printer "
               "attached, or run: python3 -m otpunit --diagnostic"),
    ]))

    sections.append(Section("WIRING", [(f"{sig}", f"{pin} / {hdr}")
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


def render(sections, output, page_size=A4) -> None:
    """Draw the report onto `output` (a path or a binary stream)."""
    width, height = page_size
    canvas = gen.new_canvas(output, page_size, title="OTP unit status")

    margin = 14 * mm
    gutter = 8 * mm
    column_width = (width - 2 * margin - gutter) / 2
    top = height - margin

    canvas.setFont("Helvetica-Bold", TITLE_SIZE)
    canvas.drawString(margin, top - TITLE_SIZE, "OTP PRINT UNIT - STATUS SHEET")
    canvas.setFont("Helvetica", BODY_SIZE)
    canvas.drawString(margin, top - TITLE_SIZE - 11,
                      "No display detected. This sheet is printed automatically; "
                      "it contains no key material.")
    canvas.setLineWidth(0.6)
    canvas.line(margin, top - TITLE_SIZE - 16, width - margin, top - TITLE_SIZE - 16)

    column_top = top - TITLE_SIZE - 26
    bottom = margin + 12
    label_width = 30 * mm

    # Lay each section out before drawing any of it. Measuring first is
    # what lets the two columns be balanced rather than filling the left
    # one to the floor and dropping a lone section on the right.
    def lay_out(section):
        """(height, [(kind, x_offset, text)]) for one section."""
        items, height = [], 0.0
        items.append(("head", 0, section.title))
        height += LEADING + 2
        for label, value in section.rows:
            if label is None:
                for line in _wrap(str(value), "Helvetica", BODY_SIZE,
                                  column_width, canvas):
                    items.append(("body", 0, line))
                    height += LEADING
            else:
                lines = _wrap(str(value), "Helvetica", BODY_SIZE,
                              column_width - label_width, canvas) or [""]
                items.append(("label", 0, str(label)))
                for index, line in enumerate(lines):
                    items.append(("value" if index == 0 else "cont",
                                  label_width, line))
                    height += LEADING
            height += 1.5
        return height + 5, items

    laid = [lay_out(section) for section in sections]
    total = sum(height for height, _ in laid)
    available = column_top - bottom

    # Break at the section that first crosses half the total, unless the
    # left column would overflow the page before then.
    split, running = len(laid), 0.0
    for index, (height, _) in enumerate(laid):
        if running > 0 and (running + height > available
                            or running >= total / 2):
            split = index
            break
        running += height

    overflow = []
    for column, group in enumerate((laid[:split], laid[split:])):
        x = margin + column * (column_width + gutter)
        y = column_top
        for height, items in group:
            for kind, offset, text in items:
                if y < bottom:
                    overflow.append(text)
                    continue
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
            y -= 5

    if overflow:
        canvas.setFont("Helvetica-Oblique", BODY_SIZE)
        canvas.drawString(margin, margin,
                          f"{len(overflow)} lines did not fit this sheet")

    canvas.showPage()
    canvas.save()


def render_bytes(sections, page_size=A4) -> bytearray:
    """The sheet as bytes, for piping straight to lp."""
    import io

    buffer = io.BytesIO()
    render(sections, buffer, page_size)
    return bytearray(buffer.getvalue())


# --- headless mode -----------------------------------------------------

PAGE_SIZES = {"A4": A4}


def _page_size(settings):
    from reportlab.lib.pagesizes import LETTER

    return {"A4": A4, "LETTER": LETTER}.get(
        getattr(settings, "paper", "A4"), A4)


def run_headless(cups, settings=None, poll_seconds=2.0, log=print,
                 sleep=None, once=False) -> int:
    """
    Wait for a printer, print the status sheet, then idle.

    Reprints only when the printer goes away and comes back, so a unit left
    plugged in overnight does not produce a ream. Everything is injectable
    because this loop is the one part of headless mode a test can drive.
    """
    import time

    sleep = sleep or time.sleep
    printed_for = None

    while True:
        devices = []
        try:
            devices = cups.devices()
        except Exception as exc:                 # noqa: BLE001
            log(f"printer lookup failed: {exc}")

        if not devices:
            # Unplugged: arm for a reprint when something comes back.
            if printed_for is not None:
                log("printer disconnected")
                printed_for = None
            if once:
                return 1
            sleep(poll_seconds)
            continue

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

        sections = collect(settings=settings, printer=device,
                           queue=queue, driver=driver)
        try:
            data = render_bytes(sections, _page_size(settings))
            cups.submit(data, name=queue or "OTP", title="OTP status",
                        options={"media": getattr(settings, "paper", "A4")})
            log("status sheet submitted")
            printed_for = device.uri
        except Exception as exc:                 # noqa: BLE001
            log(f"could not print the status sheet: {exc}")
            if once:
                return 1
            # Do not spin: back off before trying this printer again.
            sleep(poll_seconds)
            continue

        if once:
            return 0
        sleep(poll_seconds)

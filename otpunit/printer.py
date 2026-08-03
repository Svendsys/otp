"""CUPS glue for the print unit.

Two things matter here. First, jobs are submitted by piping bytes to `lp` on
stdin -- this process never writes key material to a filesystem. Second,
CUPS itself always spools a job to disk; that is unavoidable short of
writing raw to /dev/usb/lp0, which only PostScript and PCL printers accept.
The image forces the spool onto tmpfs and purge() empties it after each job,
so key material stays in RAM and never reaches the SD card. Do not describe
this as "nothing is written anywhere" -- describe it accurately.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

QUEUE = "OTP"
LP = "/usr/bin/lp"
LPSTAT = "/usr/bin/lpstat"
LPINFO = "/usr/sbin/lpinfo"
LPADMIN = "/usr/sbin/lpadmin"
CANCEL = "/usr/bin/cancel"


@dataclass(frozen=True)
class Device:
    uri: str
    description: str

    @property
    def label(self) -> str:
        """Something that fits a 21-column panel."""
        name = self.description or self.uri
        name = re.sub(r"\s+", " ", name).strip()
        return name[:20]

    @property
    def is_ipp(self) -> bool:
        """Driverless-capable: lpadmin -m everywhere needs an IPP URI."""
        return self.uri.startswith(("ipp://", "ipps://", "ippusb://", "dnssd://"))

    @property
    def usb_id(self) -> str:
        """
        The usb:// form of this device, if it has one.

        ipp-usb publishes a loopback endpoint whose URI names no make or
        model, so a device discovered that way cannot be matched to a PPD on
        its own -- the usb:// entry for the same printer carries the name.
        """
        return self.uri if self.uri.startswith("usb://") else ""


class PrinterError(RuntimeError):
    pass


class Cups:
    """Thin wrapper over the CUPS command-line tools."""

    def __init__(self, run=None):
        # Injectable so tests can drive this against recorded output.
        self._run = run or self._subprocess_run

    # A wedged cupsd must not block the UI forever holding key material
    # resident, with no way out but pulling the power.
    TIMEOUT = 120

    @staticmethod
    def _subprocess_run(argv, stdin: bytes | None = None):
        return subprocess.run(argv, input=stdin, capture_output=True,
                              timeout=Cups.TIMEOUT)

    def _text(self, argv) -> str:
        # A wedged cupsd hits Cups.TIMEOUT and raises. Queries are advisory,
        # so treat that as "nothing to report" -- letting it escape turns a
        # slow printer into a crash loop with no panel message.
        try:
            result = self._run(argv)
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", "replace")

    # ipp-usb publishes a local IPP endpoint for printers that speak
    # IPP-over-USB. That endpoint is the only URI `lpadmin -m everywhere`
    # accepts -- driverless setup requires an IPP connection and rejects a
    # raw usb:// URI outright -- so both kinds have to be collected, and
    # the IPP one preferred when the same printer offers both.
    # Only ipp-usb's own loopback endpoint, never the network. A printer
    # across the LAN must not be picked up by a unit whose whole point is
    # being offline -- disabling the radios does not cover wired or USB
    # ethernet, and install.sh converts Pis that may have either.
    LOCAL_IPP = ("ippusb://", "ipp://localhost", "ipp://127.0.0.1",
                 "ipps://localhost", "ipps://127.0.0.1")

    def devices(self) -> list[Device]:
        """Locally attached printers CUPS can currently see."""
        found = []
        for line in self._text([LPINFO, "-v"]).splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            _kind, uri = parts
            uri = uri.strip()
            if uri.startswith("usb://") or uri.startswith(self.LOCAL_IPP):
                found.append(Device(uri=uri, description=_pretty(uri)))
        # An IPP endpoint needs no driver at all, so try those first.
        found.sort(key=lambda d: not d.is_ipp)
        return found

    def queues(self) -> list[str]:
        names = []
        for line in self._text([LPSTAT, "-p"]).splitlines():
            match = re.match(r"printer (\S+)", line)
            if match:
                names.append(match.group(1))
        return names

    def ensure_queue(self, device: Device, name: str = QUEUE) -> str:
        """
        Create or repoint the print queue.

        Driverless setup is only attempted for IPP URIs: CUPS rejects
        `-m everywhere` against a usb:// device with "IPP Everywhere driver
        requires an IPP connection", and would leave no queue behind. Older
        host-based lasers get a PPD matched on their USB device ID instead.
        """
        if device.is_ipp:
            result = self._run([LPADMIN, "-p", name, "-E", "-v", device.uri,
                                "-m", "everywhere"])
            if result.returncode == 0:
                return name

        model = self._match_ppd(device)
        if model:
            result = self._run([LPADMIN, "-p", name, "-E", "-v", device.uri, "-m", model])
            if result.returncode == 0:
                return name
        raise PrinterError(f"no driver for {device.label}")

    def _match_ppd(self, device: Device) -> str | None:
        """
        Pick a PPD for a printer CUPS has no driverless queue for.

        Matching on manufacturer alone is not good enough: "HP" appears in
        hundreds of PPDs, and picking the wrong one produces a queue that
        accepts jobs and prints garbage. So score candidates on the model
        tokens too and require the model itself to match.
        """
        wanted = _tokens(_pretty(device.uri))
        if len(wanted) < 2:
            return None
        make, model = wanted[0], wanted[1:]

        best, best_score = None, 0
        for line in self._text([LPINFO, "-m"]).splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            name, description = parts
            candidate = _tokens(description)
            if make not in candidate:
                continue
            score = sum(1 for token in model if token in candidate)
            # Every model token has to appear, or it is a different printer.
            if score < len(model):
                continue
            # Among equals prefer the shortest description: "LaserJet Pro
            # M12w" over "LaserJet Pro M12w MFP Special Edition".
            if score > best_score or (score == best_score and best
                                      and len(description) < best[1]):
                best, best_score = (name, len(description)), score
        return best[0] if best else None

    def submit(self, data: bytes, name: str = QUEUE, title: str = "OTP",
               options: dict | None = None) -> str:
        """Pipe `data` to lp on stdin and return the job id."""
        argv = [LP, "-d", name, "-t", title]
        for key, value in (options or {}).items():
            argv += ["-o", f"{key}={value}"]
        result = self._run(argv, stdin=bytes(data))
        if result.returncode != 0:
            raise PrinterError(result.stderr.decode("utf-8", "replace").strip() or "lp failed")
        match = re.search(r"request id is (\S+)", result.stdout.decode("utf-8", "replace"))
        return match.group(1) if match else ""

    def active_jobs(self, name: str = QUEUE) -> int:
        return len([
            line for line in self._text([LPSTAT, "-o", name]).splitlines() if line.strip()
        ])

    def purge(self, name: str = QUEUE) -> None:
        """Empty the spool. Belt and braces: the spool is already on tmpfs."""
        try:
            self._run([CANCEL, "-x", "-a", name])
        except Exception:
            # Best effort. The spool is tmpfs and dies with the power, so a
            # failed purge is not worth propagating into the UI.
            pass


def _pretty(uri: str) -> str:
    """Turn usb://Brother/HL-2030?serial=... into 'Brother HL-2030'."""
    match = re.match(r"usb://([^/]+)/([^?]+)", uri)
    if not match:
        return ""
    make = match.group(1).replace("%20", " ")
    model = match.group(2).replace("%20", " ")
    return f"{make} {model}".strip()


def _tokens(text: str) -> list[str]:
    """
    Lowercase alphanumeric words, for comparing a device name to a PPD's.

    Splits letter/digit runs apart so "M12w" and "M 12 W" compare equal --
    manufacturers are not consistent about that and CUPS descriptions are
    not either.
    """
    words = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text.lower()):
        words.extend(part for part in re.findall(r"[a-z]+|[0-9]+", chunk) if part)
    return words

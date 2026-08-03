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

import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import unquote

QUEUE = "OTP"
# Kept equal to TempDir in device/install.sh's cups-files.conf.
TEMP_DIR = "/run/cups/tmp"
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
    # Unambiguously local IPP endpoints.
    LOCAL_IPP = ("ippusb://", "ipp://localhost", "ipp://127.0.0.1",
                 "ipps://localhost", "ipps://127.0.0.1")

    def devices(self) -> list[Device]:
        """
        Locally attached printers CUPS can currently see.

        dnssd:// is the awkward case. It is how CUPS often reports the
        endpoint ipp-usb publishes for a USB printer -- the driverless path
        -- but it is also how a printer across the LAN appears, which a unit
        whose whole point is being offline must not silently bind to.
        (Disabling the radios does not cover wired or USB ethernet.)

        "Accept dnssd whenever something is plugged into USB" was not good
        enough, and failed in the worst direction. It never checked that the
        dnssd entry WAS the plugged-in printer, and driverless endpoints
        sort first, so a Brother on USB next to an HP on the LAN returned
        the HP -- and every pad printed on a machine in another room, with
        nothing on the panel naming the device.

        So a dnssd endpoint has to identify itself as one of the USB
        devices: its service name must carry all of that device's make and
        model tokens. An unmatched dnssd entry is the network and is
        dropped, whether or not anything is plugged in.

        The cost is a printer that speaks IPP-over-USB and exposes no
        printer-class interface, so it has no usb:// entry to match
        against. Those are reachable through ipp-usb's loopback endpoint,
        which LOCAL_IPP accepts unconditionally, but if CUPS reports only a
        dnssd URI for one it will not be offered and the panel will keep
        asking for a printer. That is the right way round: a visible,
        diagnosable refusal beats silently printing key material onto the
        LAN. docs/PRINTERS.md carries the workaround.
        """
        usb, local_ipp, discovered = [], [], []
        for line in self._text([LPINFO, "-v"]).splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            uri = parts[1].strip()
            device = Device(uri=uri, description=_pretty(uri))
            if uri.startswith("usb://"):
                usb.append(device)
            elif uri.startswith(self.LOCAL_IPP):
                local_ipp.append(device)
            elif uri.startswith("dnssd://"):
                discovered.append(device)

        attached = [_tokens(_pretty(device.uri)) for device in usb]
        discovered = [device for device in discovered
                      if _identifies_as_attached(device.uri, attached)]
        # Driverless endpoints first: they need no driver at all.
        return local_ipp + discovered + usb

    def ensure_queue(self, device: Device, name: str = QUEUE) -> str:
        """
        Create or repoint the print queue.

        Driverless setup is only attempted for IPP URIs: CUPS rejects
        `-m everywhere` against a usb:// device with "IPP Everywhere driver
        requires an IPP connection", and would leave no queue behind. Older
        host-based lasers get a PPD matched on their USB device ID instead.
        """
        if device.is_ipp:
            if self._lpadmin(name, device.uri, "everywhere"):
                return name

        model = self._match_ppd(device)
        if model and self._lpadmin(name, device.uri, model):
            return name

        # Every attempt failed, so leave nothing behind that can accept a
        # job. lpadmin exits non-zero on a bad -m but CREATES THE QUEUE
        # ANYWAY: verified against a real cupsd, a failed `-m everywhere`
        # against an unreachable IPP URI left OTP enabled and idle with no
        # PPD, absent from printers.conf, and happy to accept a complete
        # pad that it would never print. The caller keeps using the name
        # `OTP` after reporting the error, so the operator could walk
        # straight into spooling key material into a black hole and then
        # wait on a queue that never drains. The tell was a zero StateTime.
        self._remove_queue(name)
        raise PrinterError(f"no driver for {device.label}")

    def _remove_queue(self, name: str) -> None:
        """Delete a queue, best effort. Absent is the desired end state."""
        try:
            self._run([LPADMIN, "-x", name])
        except Exception:
            pass

    def _lpadmin(self, name: str, uri: str, model: str) -> bool:
        """
        Run lpadmin, reporting failure rather than raising.

        Every other call into CUPS already contains its own failures, but
        this one used to let them out: _subprocess_run raises
        TimeoutExpired after TIMEOUT seconds, and a missing lpadmin raises
        FileNotFoundError, neither of which is a PrinterError. Queue setup
        runs on the boot path, alongside a cupsd that is itself still
        starting -- precisely when a slow answer is most likely -- and the
        caller only catches PrinterError. So the escape route was an
        uncaught traceback out of run(), Restart=on-failure, and a dark
        panel with nothing on it to explain why.
        """
        try:
            result = self._run([LPADMIN, "-p", name, "-E", "-v", uri, "-m", model])
        except Exception:
            return False
        return result.returncode == 0

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

    def active_jobs(self, name: str = QUEUE) -> int | None:
        """
        Jobs still queued, or None if the queue could not be asked.

        None and 0 must not be conflated. `_text` swallows a wedged cupsd,
        a missing lpstat and an unknown destination alike, and reporting all
        of those as "nothing queued" would let the caller purge a spool that
        is still printing.
        """
        result = self._run_text([LPSTAT, "-o", name])
        if result is None:
            return None
        return len([line for line in result.splitlines() if line.strip()])

    def _run_text(self, argv) -> str | None:
        """Like _text, but None on failure rather than an empty string."""
        try:
            result = self._run(argv)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", "replace")

    def purge(self, name: str = QUEUE) -> None:
        """Empty the spool. Belt and braces: the spool is already on tmpfs."""
        try:
            self._run([CANCEL, "-x", "-a", name])
        except Exception:
            # Best effort. The spool is tmpfs and dies with the power, so a
            # failed purge is not worth propagating into the UI.
            pass
        self._clear_temp()

    def _clear_temp(self) -> None:
        """
        Delete what the filters left in TempDir.

        `cancel -x` empties RequestRoot but not TempDir, and cancelling is
        exactly when TempDir matters: CUPS SIGKILLs the filter chain, so
        Ghostscript never unlinks its own scratch files. Verified against a
        real cupsd -- cancelling a 400-page pad mid-print left a 1MB
        gs_XXXXXX holding 384 pages of plaintext PostScript with the
        codeword and the key groups in it, and a cupsd restart did not
        clear it. Normal completion is clean; this is the cancel path, and
        the cancel path is what abandoning a pair invokes.

        Still RAM-only on the device -- TempDir is under /run -- so this is
        about honouring purge()'s contract, not about the SD card.
        """
        try:
            for entry in os.scandir(TEMP_DIR):
                try:
                    if entry.is_file(follow_symlinks=False):
                        os.unlink(entry.path)
                except OSError:
                    # A file another job still holds. Skip it; this runs
                    # after cancel, so nothing of ours should be live.
                    pass
        except OSError:
            # No TempDir, or not ours to read. Nothing to do and nothing
            # worth taking the UI down for.
            pass


def _pretty(uri: str) -> str:
    """Turn usb://Brother/HL-2030?serial=... into 'Brother HL-2030'."""
    match = re.match(r"usb://([^/]+)/([^?]+)", uri)
    if not match:
        return ""
    make = match.group(1).replace("%20", " ")
    model = match.group(2).replace("%20", " ")
    return f"{make} {model}".strip()


def _dnssd_name(uri: str) -> str:
    """
    The service name out of a dnssd:// URI.

    `dnssd://HP%20LaserJet%20M428fdw%20%5BABCDEF%5D._ipp._tcp.local/?uuid=1`
    becomes `HP LaserJet M428fdw [ABCDEF]`. The trailing service type and
    the query string carry nothing that identifies the printer.
    """
    rest = uri[len("dnssd://"):].split("/", 1)[0]
    rest = rest.split("._", 1)[0]
    return unquote(rest)


def _identifies_as_attached(uri: str, attached: list[list[str]]) -> bool:
    """
    True when a dnssd service name names one of the USB devices.

    Requires every make and model token of some attached printer, so
    "Brother HL-2030 series" matches a usb://Brother/HL-2030%20series and
    an unrelated "HP LaserJet MFP M428fdw" on the LAN matches nothing. An
    empty token list cannot match: a usb:// URI _pretty could not parse
    would otherwise wave through every printer on the network.
    """
    name = _tokens(_dnssd_name(uri))
    if not name:
        return False
    return any(wanted and all(token in name for token in wanted)
               for wanted in attached)


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

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

    def __init__(self, run=None, temp_dir=None):
        # Injectable so tests can drive this against recorded output.
        self._run = run or self._subprocess_run
        # Which TempDir _clear_temp() empties. Per instance so a test rig
        # with its own cupsd never touches the host's /run/cups/tmp.
        self.temp_dir = temp_dir or TEMP_DIR

    # A wedged cupsd must not block the UI forever holding key material
    # resident, with no way out but pulling the power.
    TIMEOUT = 120

    @staticmethod
    def _subprocess_run(argv, stdin: bytes | None = None):
        # LC_ALL=C, because much of what lpstat prints is translated and
        # the English matches below would silently stop matching.
        return subprocess.run(argv, input=stdin, capture_output=True,
                              timeout=Cups.TIMEOUT,
                              env=dict(os.environ, LC_ALL="C"))

    def _text(self, argv) -> str:
        # A wedged cupsd hits Cups.TIMEOUT and raises. Queries are advisory,
        # so treat that as "nothing to report" instead of crashing the UI.
        try:
            result = self._run(argv)
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", "replace")

    # ipp-usb publishes a local IPP endpoint for printers that speak
    # IPP-over-USB, and `lpadmin -m everywhere` accepts only IPP URIs. So
    # both kinds are collected and the IPP one preferred. These prefixes
    # are unambiguously local.
    LOCAL_IPP = ("ippusb://", "ipp://localhost", "ipp://127.0.0.1",
                 "ipps://localhost", "ipps://127.0.0.1")

    def devices(self) -> list[Device]:
        """
        Locally attached printers CUPS can currently see.

        dnssd:// is how CUPS may report the endpoint ipp-usb publishes for
        a USB printer, but also how a printer across the LAN appears, and
        an offline unit must never print key material to the network. So a
        dnssd endpoint is kept only if its service name carries all the
        make and model tokens of an attached usb:// device.

        The cost: an IPP-over-USB printer that CUPS reports only as dnssd,
        with no usb:// entry to match, is not offered. A visible refusal
        beats printing onto the LAN; docs/PRINTERS.md has the workaround.
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
        # job. lpadmin exits non-zero on a bad -m but creates the queue
        # anyway, and such a queue would accept a pad it can never print.
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
        Run lpadmin. Returns False on any failure and never raises.

        A timeout or a missing binary is not a PrinterError, which is all
        the caller catches, and queue setup runs on the boot path while
        cupsd may still be starting.
        """
        try:
            result = self._run([LPADMIN, "-p", name, "-E", "-v", uri, "-m", model])
        except Exception:
            return False
        return result.returncode == 0

    def resume(self, name: str = QUEUE) -> bool:
        """
        Re-enable a queue cupsd stopped, keeping the held job.

        A stopped queue keeps its job and nothing clears that by itself.
        `lpadmin -p NAME -E`, not cupsenable: -E after -p both enables and
        accepts jobs. Returns False on failure; never raises.
        """
        try:
            result = self._run([LPADMIN, "-p", name, "-E"])
        except Exception:                        # noqa: BLE001
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
        """
        Pipe `data` to lp on stdin and return the job id.

        Every failure leaves as PrinterError, including a missing or
        timed-out lp, because that is what callers catch.
        """
        argv = [LP, "-d", name, "-t", title]
        for key, value in (options or {}).items():
            argv += ["-o", f"{key}={value}"]
        try:
            result = self._run(argv, stdin=bytes(data))
        except PrinterError:
            raise
        except Exception as exc:                 # noqa: BLE001
            raise PrinterError(f"could not run {LP}: {exc}") from exc
        if result.returncode != 0:
            raise PrinterError(result.stderr.decode("utf-8", "replace").strip() or "lp failed")
        match = re.search(r"request id is (\S+)", result.stdout.decode("utf-8", "replace"))
        return match.group(1) if match else ""

    # State reasons cupsd reports when nothing is actually wrong. Anything
    # else on those lines is a fault worth telling the operator about.
    BENIGN_REASONS = ("ready to print.", "ready to print", "none")

    def printer_fault(self, name: str = QUEUE) -> str | None:
        """
        The fault the queue is reporting, or None if it reports none.

        An empty `lpstat -o` is not proof that anything printed: under
        ErrorPolicy abort-job a failed job is discarded as promptly as a
        successful one. The queue's own state distinguishes the two.

        None also means "cannot tell". Draining stays the primary signal;
        this only overrides it when the printer says it failed, since
        inventing a fault would send someone to burn a pad that is fine.
        """
        text = self._run_text([LPSTAT, "-p", name])
        if text is None:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        if "disabled" in lines[0]:
            return lines[0]
        # cupsd prints state reasons on the lines after the header, and only
        # when it has something to say about them.
        for line in lines[1:]:
            if line.lower().rstrip(".") not in [r.rstrip(".") for r
                                                in self.BENIGN_REASONS]:
                return line
        return None

    # IPP printer-state-reasons are keyword constants, unlike the free,
    # translated text of printer-state-message. But the suffix is not the
    # severity: CUPS' network backends report out-of-paper and jams as
    # "-warning", so reasons are matched on the stem. And the usb backend
    # this unit runs reports no media reasons at all, while the reasons
    # other backends report are sticky and outlive the job that set them.
    #
    # Hence reasons never create a fault, they only excuse one; the prose
    # in printer-state-message is the decision channel. These stems are
    # the advisory ones.
    ADVISORY_STEMS = (
        "toner-low", "marker-supply-low", "marker-waste-almost-full",
        "media-low", "cups-waiting-for-job-completed",
    )
    NOT_A_REASON = ("none",)

    @staticmethod
    def reason_stem(reason: str) -> str:
        """An IPP state reason without its severity suffix."""
        for suffix in ("-error", "-warning", "-report"):
            if reason.endswith(suffix):
                return reason[:-len(suffix)]
        return reason

    def state_reasons(self, name: str = QUEUE) -> list[str] | None:
        """
        The queue's IPP state reasons, or None if it could not be asked.

        None is "cannot tell" and must not be read as "nothing wrong"; see
        active_jobs for why that distinction is load-bearing here.
        """
        text = self._run_text([LPSTAT, "-l", "-p", name])
        if text is None:
            return None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("Alerts:"):
                return [word for word in stripped[len("Alerts:"):].split()
                        if word not in self.NOT_A_REASON]
        # No Alerts line at all: an lpstat too old to print one. Unknown,
        # not clean.
        return None

    def queue_stopped(self, name: str = QUEUE) -> bool | None:
        """
        Whether cupsd has disabled the queue, or None if it cannot be asked.

        Decided on "paused", an IPP keyword that is not translated.
        """
        reasons = self.state_reasons(name)
        return None if reasons is None else "paused" in reasons

    def active_jobs(self, name: str = QUEUE) -> int | None:
        """
        Jobs still queued, or None if the queue could not be asked.

        None and 0 must not be conflated: None covers a wedged cupsd, a
        missing lpstat and an unknown destination, and reporting those as
        "nothing queued" would let the caller purge a spool that is still
        printing.
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

        `cancel -x` empties RequestRoot but not TempDir. On cancel CUPS
        kills the filter chain, so Ghostscript leaves scratch files there
        holding plaintext pages with the codeword and key groups.

        TempDir is under /run, so this is still RAM-only on the device.
        """
        try:
            for entry in os.scandir(self.temp_dir):
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


def reported_fault(cups, queue: str = QUEUE) -> str:
    """
    What the printer says is wrong, or "" if it says nothing or cannot say.

    Silence is not read as trouble: a cupsd that cannot be asked, or a
    Cups without this method, falls back to the drain result.
    """
    try:
        return cups.printer_fault(queue) or ""
    except Exception:                            # noqa: BLE001
        return ""


def state_reasons(cups, queue: str = QUEUE):
    """
    The queue's IPP state reasons, or None if it could not be asked.

    Fetched separately from the questions asked of it, so a caller that
    wants several answers pays for one lpstat. None means "could not tell"
    and must not be read as "nothing wrong".
    """
    query = getattr(cups, "state_reasons", None)
    if query is None:
        return None
    try:
        return query(queue)
    except Exception:                            # noqa: BLE001
        return None


# Prose that means the printer is chatting, not failing: cupsd puts filter
# output in printer-state-message on a healthy queue. Matched with LC_ALL=C
# in force, so this is stable English.
_CHATTER = ("debug:", "info:")
_ADVISORY_PROSE = ("toner low", "toner is low", "supply low", "media low",
                   "paper low", "ready to print")


def blocking_in(prose: str, reasons) -> list[str]:
    """
    Why this queue cannot print; empty if nothing says it cannot.

    The prose decides. `reasons` (whatever state_reasons() returned, None
    included) can only excuse it, never create it; see
    Cups.ADVISORY_STEMS. Both are passed in so the caller can ask for each
    at most once.

    A printer nobody can classify therefore errs towards "destroy a pair
    that was probably fine", never towards "the pair printed" over an
    empty tray.
    """
    prose = (prose or "").strip()
    if not prose:
        return []
    lowered = prose.lower()
    if lowered.startswith(_CHATTER):
        return []
    if any(phrase in lowered for phrase in _ADVISORY_PROSE):
        return []
    # An excuse needs reasons that are all advisory. Silence excuses
    # nothing: on the usb backend silence is the normal state.
    if reasons:
        stems = {Cups.reason_stem(reason) for reason in reasons}
        if stems and stems <= set(Cups.ADVISORY_STEMS):
            return []
        blocking = [r for r in reasons
                    if Cups.reason_stem(r) not in Cups.ADVISORY_STEMS]
        if blocking:
            return blocking
    return ["print-error"]


def fault_text(cups, queue: str = QUEUE) -> str:
    """
    Why this printer did not print, in its own words, or "" if it did.

    The one decision the panel and the unattended path share, so they
    cannot disagree about the same printer. Costs up to two lpstat
    subprocesses; callers needing the parts compose reported_fault,
    state_reasons and blocking_in themselves.
    """
    prose = reported_fault(cups, queue)
    if not prose.strip():
        return ""
    return prose if blocking_in(prose, state_reasons(cups, queue)) else ""


def stopped_in(reasons, cups, queue: str = QUEUE) -> bool:
    """
    Whether cupsd has disabled the queue, given its state reasons.

    Decided on "paused", an untranslated IPP keyword. For a Cups that
    cannot report reasons, falls back to "disabled" in lpstat's header.
    """
    if reasons is None:
        return "disabled" in reported_fault(cups, queue).lower()
    return "paused" in reasons

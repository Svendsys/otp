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
        # Which TempDir _clear_temp() empties. A module constant here meant
        # the harness -- which runs its own cupsd in a temp directory --
        # deleted the HOST's live /run/cups/tmp as root instead, on any
        # machine with CUPS running, and never once exercised its own.
        self.temp_dir = temp_dir or TEMP_DIR

    # A wedged cupsd must not block the UI forever holding key material
    # resident, with no way out but pulling the power.
    TIMEOUT = 120

    @staticmethod
    def _subprocess_run(argv, stdin: bytes | None = None):
        # LC_ALL=C, because half of what lpstat prints is translated. The
        # header comes from _cupsLangPrintf, so on a unit installed in a
        # non-English locale "disabled since ..." arrives as "desactivee
        # depuis ..." and any English match against it silently stops
        # matching. otp-unit.service sets no locale, so the unit gets
        # whatever the image was built with.
        return subprocess.run(argv, input=stdin, capture_output=True,
                              timeout=Cups.TIMEOUT,
                              env=dict(os.environ, LC_ALL="C"))

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

    def resume(self, name: str = QUEUE) -> bool:
        """
        Re-enable a queue cupsd stopped, keeping the held job.

        CUPS_BACKEND_STOP disables the queue and KEEPS the job, and nothing
        clears that by itself -- not time, not the fault going away. The
        only place the unit ran `lpadmin -E` was ensure_queue, once, at
        startup, so an operator who closed the cover and pressed OK RECHECK
        got PRINTER STOPPED again for ever and the sole working exit was the
        one that wipes the key. A recoverable jam cost a pad pair.

        `lpadmin -p NAME -E` rather than cupsenable: -E after -p is enable
        AND accept-jobs, and lpadmin is already on the unit's path.
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

        Every failure leaves as PrinterError, including the ones that are
        not lp's fault. A missing /usr/bin/lp raised FileNotFoundError
        straight out of subprocess, past every caller that catches
        PrinterError -- and the first submit in the unattended sequence is
        the status sheet, which is outside any try. _lpadmin has carried a
        docstring about exactly this since it was written; submit did not.
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

        An empty `lpstat -o` is NOT proof that anything printed. The unit
        runs ErrorPolicy abort-job, deliberately, so a job that fails is
        discarded just as promptly as one that succeeds -- measured against
        a real cupsd with the tray empty, all seven jobs aborted, the queue
        drained in four seconds, and the unit printed "YOUR PAD PAIR IS
        PRINTED / They are identical" over a tray holding nothing at all.
        The queue's own state is what distinguishes the two.

        None also means "cannot tell": a wedged cupsd answers nothing, and
        inventing a fault would send someone to burn a pad that is fine.
        Draining stays the primary signal; this only overrides it when the
        printer says out loud that it failed.
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

    # IPP printer-state-reasons. Unlike printer-state-message -- which is
    # free text, translated, and where cupsd also puts filter chatter like
    # "DEBUG: cfFilterChain: universal (PID 21339) exited with no errors."
    # -- these are IPP keyword constants and carry their own severity as a
    # suffix. Measured against the rig's cupsd:
    #
    #   healthy          Alerts: none
    #   tray empty       Alerts: media-empty-error
    #   backend STOP     Alerts: media-empty-error paused
    #
    # The SUFFIX IS NOT THE SEVERITY, whatever it looks like. That was the
    # first design here and it was wrong in the dangerous direction:
    #
    #   $ strings /usr/lib/cups/backend/{socket,ipp,lpd} | grep media-
    #   media-empty-warning
    #   media-jam-warning
    #   toner-empty-warning
    #
    # CUPS' SNMP supplies code emits out-of-paper and paper-jam as
    # "-warning", so treating "-warning" as advisory discarded exactly the
    # conditions that mean nothing printed, and the panel said PAIR
    # COMPLETE over an empty tray.
    #
    #   $ strings /usr/lib/cups/backend/usb | grep -c media-
    #   0
    #
    # Worse, the usb backend -- which is what THIS unit runs, on usb://
    # device URIs -- reports no media state at all, ever. Its only state
    # keywords are connecting-to-device. So on the unit's own hardware
    # path, reasons are empty whatever is wrong with the printer, and a
    # fault decision resting on them cannot see anything.
    #
    # Hence: reasons never CREATE a fault, they only EXCUSE one. The prose
    # in printer-state-message stays the decision channel, because it is
    # the only one the usb backend populates -- and because reasons are
    # STICKY, surviving the job that set them and every healthy job after
    # it, so a single empty tray would otherwise condemn every later pair
    # for the lifetime of the daemon.
    #
    # Matched on the STEM, with the suffix stripped, since the same
    # condition arrives as -error from one backend and -warning from
    # another.
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
        # not clean -- claiming clean here is the false success this whole
        # mechanism exists to prevent.
        return None

    def queue_stopped(self, name: str = QUEUE) -> bool | None:
        """
        Whether cupsd has disabled the queue, or None if it cannot be asked.

        "paused" is the IPP keyword and is not translated. The UI used to
        decide this by looking for the substring "disabled" in the header
        line, which is _cupsLangPrintf output and therefore only English.
        """
        reasons = self.state_reasons(name)
        return None if reasons is None else "paused" in reasons

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

    Silence is deliberately not read as trouble. A cupsd that cannot be
    asked, and an older Cups without this method at all, must fall back to
    the drain result rather than send someone to burn a pad that printed
    perfectly.

    One copy, called from both the UI and the unattended path. They had a
    verbatim duplicate of this each, docstring reasoning included, so a
    change to what counts as "cannot tell" had to be made twice or the panel
    and the headless run would disagree about the only question either asks.
    """
    try:
        return cups.printer_fault(queue) or ""
    except Exception:                            # noqa: BLE001
        return ""


def state_reasons(cups, queue: str = QUEUE):
    """
    The queue's IPP state reasons, or None if it could not be asked.

    Split from the two questions asked OF it -- "can this queue print" and
    "has cupsd stopped it" -- because each is an lpstat subprocess bounded
    by Cups.TIMEOUT, and the caller usually wants both answers about the
    same moment. Asking twice put three blocking subprocesses on a single
    OK press.

    None means "could not tell" and must not be read as "nothing wrong".
    """
    query = getattr(cups, "state_reasons", None)
    if query is None:
        return None
    try:
        return query(queue)
    except Exception:                            # noqa: BLE001
        return None


# Prose that means the printer is chatting, not failing. cupsd puts filter
# output in printer-state-message on a perfectly healthy queue -- measured
# mid-print: "DEBUG: cfFilterChain: universal (PID 21339) exited with no
# errors." Matched with LC_ALL=C in force, so this is stable English.
_CHATTER = ("debug:", "info:")
_ADVISORY_PROSE = ("toner low", "toner is low", "supply low", "media low",
                   "paper low", "ready to print")


def blocking_in(prose: str, reasons) -> list[str]:
    """
    Why this queue cannot print; empty if nothing says it cannot.

    The PROSE decides. `reasons` -- whatever state_reasons() returned, None
    included -- can only excuse it, never create it. Both are passed in
    rather than fetched, because each is an lpstat subprocess bounded by
    Cups.TIMEOUT and the caller must be able to ask for each at most once. See Cups.ADVISORY_STEMS
    for why that asymmetry is not squeamishness: the usb backend this unit
    runs reports no reasons at all, and the ones other backends do report
    are sticky and outlive the job that caused them.

    So the failure mode of a printer nobody can classify is "the operator
    is told to destroy a pair that was probably fine", not "the operator is
    told a pair printed when the tray was empty". Only the second leaves
    someone relying on key material they do not have.
    """
    prose = (prose or "").strip()
    if not prose:
        return []
    lowered = prose.lower()
    if lowered.startswith(_CHATTER):
        return []
    if any(phrase in lowered for phrase in _ADVISORY_PROSE):
        return []
    # An excuse needs reasons that are ALL advisory. Silence excuses
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

    THE decision, and the only one either caller should make. The panel and
    the unattended path had a fault helper each: after they were merged the
    helper was shared but the DECISION was not, so the panel excused a
    laser's standing "Toner low." while the headless run aborted a pair on
    it -- and on an SNMP printer with an empty tray they swapped sides.
    Two answers to the one question either of them asks.

    Costs up to two lpstat subprocesses, and only reaches the second when
    the first says something; callers that need the parts separately (the
    panel, which is also asking whether the queue is stopped) compose
    reported_fault, state_reasons and blocking_in themselves.
    """
    prose = reported_fault(cups, queue)
    if not prose.strip():
        return ""
    return prose if blocking_in(prose, state_reasons(cups, queue)) else ""


def stopped_in(reasons, cups, queue: str = QUEUE) -> bool:
    """
    Whether cupsd has disabled the queue, given its state reasons.

    "paused" is the IPP keyword and is not translated. The UI used to
    decide this by looking for the substring "disabled" in lpstat's header,
    which is _cupsLangPrintf output and therefore English only; that
    fallback survives here for a Cups that cannot report reasons at all.
    """
    if reasons is None:
        return "disabled" in reported_fault(cups, queue).lower()
    return "paused" in reasons

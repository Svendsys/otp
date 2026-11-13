"""A real cupsd, in a temp directory, with a printer that writes to a file.

It never touches the system CUPS: own ServerRoot, ServerBin, spool and domain
socket, with CUPS_SERVER pointed at it.

Its configuration is parsed out of `device/install.sh` at run time (MaxJobs,
ErrorPolicy, PreserveJobHistory, PreserveJobFiles), so a change to the shipped
value is what gets tested.

`set_failure()` injects the ways a real printer fails (see
`CupsRig.FAILURE_MODES`). They differ in shape: abort-job drains the queue,
STOP disables it and keeps the job, RETRY leaves it pending. An empty queue is
therefore not proof of printing.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "device" / "install.sh"

CUPSD = "/usr/sbin/cupsd"
LPINFO = "/usr/sbin/lpinfo"
SYSTEM_SERVERBIN = Path("/usr/lib/cups")


def _apparmor_state() -> str:
    """Whether cupsd is confined, which decides if a rig in /tmp can work.

    Ubuntu's profile permits backends only under /usr/lib/cups/backend, so on a
    confined host it must be put in complain mode first:

        sudo aa-complain /usr/sbin/cupsd
    """
    profiles = Path("/sys/kernel/security/apparmor/profiles")
    try:
        for line in profiles.read_text().splitlines():
            if "cupsd" in line:
                return line.strip()
    except OSError:
        return "not available (no apparmor, or securityfs not mounted)"
    return "no cupsd profile loaded"

# What the fake backend advertises. The URI must start with usb:// or
# Cups.devices() drops it; the scheme must match the backend's filename or
# cupsd will not route jobs to it; and the make and model must be ones
# _match_ppd can find a PPD for.
#
# "Generic PDF Printer" passes the PDF through its filter chain unaltered, so
# the backend receives byte-for-byte what the unit submitted and copies A and B
# can be compared.
DEVICE_URI = "usb://Generic/PDF%20Printer?serial=RIG001"
DEVICE_MAKE = "Generic PDF Printer"

BACKEND = r"""#!/bin/sh
# A stand-in for the usb backend.
#
# With no arguments cupsd is asking what is attached, so answer with one
# device. With arguments it is a job: copy it somewhere the test can read
# and assert on the bytes that actually reached the printer.
#
# It can also be told to FAIL, and that half is the point. A backend that
# can only ever succeed leaves every real-world printing failure
# unexercised, and the asymmetry is what makes that dangerous: a false
# failure wastes paper, a false success destroys key material the operator
# believes is on a page in their hand.
#
# The mode is read FRESH on every invocation, so a test can change it
# mid-sequence -- which is how "copy A reached paper and copy B did not"
# gets modelled at all.
MODE=$(cat "{outdir}/.mode" 2>/dev/null || echo "")
AFTER=$(cat "{outdir}/.after" 2>/dev/null || echo 0)
UNTIL=$(cat "{outdir}/.until" 2>/dev/null || echo 0)

if [ $# -eq 0 ]; then
    # A printer that has been unplugged advertises nothing. This is what
    # makes cups.devices() go empty mid-sequence, which is the only signal
    # the unattended path has that the cable came out.
    if [ -f "{outdir}/.unplugged" ]; then
        exit 0
    fi
    echo 'direct {uri} "{make}" "{make}" "MFG:OTP;MDL:Simulated Laser;"'
    exit 0
fi

# INVOCATIONS, not jobs: a RETRY re-runs the backend against the same job,
# and both windows below have to be expressible in the same counter.
# cupsd runs one backend at a time per printer, so no locking is needed --
# and if that ever stops being true the count going wrong is visible in
# backend_invocations() rather than silent.
COUNT=$(cat "{outdir}/.count" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
printf '%s\n' "$COUNT" > "{outdir}/.count"

OUT="{outdir}/job-$1-$(date +%s%N)"
if [ -n "$6" ] && [ -f "$6" ]; then
    cp "$6" "$OUT.data"
else
    cat > "$OUT.data"
fi
# The job's title is argument 3. Recorded separately so a test can assert
# on the ORDER sheets reached paper without parsing PDFs.
printf '%s\n' "$3" > "$OUT.title"

# Outside the window the printer is well. `.after` lets the first N
# invocations through; `.until` ends the fault, which is what lets a RETRY
# mode terminate instead of spinning for as long as a test will wait.
if [ "$COUNT" -le "$AFTER" ]; then
    MODE=""
fi
if [ "$UNTIL" -gt 0 ] && [ "$COUNT" -gt "$UNTIL" ]; then
    MODE=""
fi

case "$MODE" in
"")
    ;;
media-empty)
    # CUPS_BACKEND_FAILED. What happens next is the queue's ErrorPolicy,
    # and the unit ships abort-job -- so the job is DISCARDED and the queue
    # drains exactly as fast as a successful one. That is the bug class
    # this rig exists for, and the one that already nearly wiped a key.
    echo "STATE: +media-empty-error" >&2
    echo "ERROR: out of paper" >&2
    exit 1
    ;;
stop)
    # CUPS_BACKEND_STOP: cupsd stops the QUEUE and keeps the job for
    # reprinting rather than consulting ErrorPolicy. This is what a
    # printer that reports an open cover or an empty tray properly looks
    # like, and its shape is the opposite of abort-job's -- the queue does
    # not drain and lpstat says the printer is disabled.
    echo "STATE: +media-empty-error" >&2
    echo "ERROR: tray open" >&2
    exit 4
    ;;
retry)
    # CUPS_BACKEND_RETRY: transient. cupsd holds the job for a later
    # attempt, so the queue never empties. The danger here is the mirror
    # image of abort-job's: not a false success but an unbounded wait.
    echo "ERROR: printer busy" >&2
    exit 6
    ;;
retry-current)
    # CUPS_BACKEND_RETRY_CURRENT: retried immediately, in a loop, for as
    # long as the fault persists. Only safe to inject with `until` set,
    # which set_failure() enforces rather than trusting.
    echo "ERROR: printer busy, retrying now" >&2
    exit 7
    ;;
disconnect)
    # The bytes were accepted and THEN the device vanished. Everything
    # after this point sees no usb:// device at all.
    touch "{outdir}/.unplugged"
    echo "ERROR: USB device disappeared" >&2
    exit 1
    ;;
truncate)
    # Some of it reached paper and then it stopped. Recorded as a SHORT
    # file, deliberately: "some pages printed" is the state an operator
    # most needs the truth about, and a test that reads only the exit code
    # cannot tell it from "nothing printed".
    SIZE=$(wc -c < "$OUT.data")
    head -c $((SIZE / 2)) "$OUT.data" > "$OUT.data.part" \
        && mv "$OUT.data.part" "$OUT.data"
    echo "STATE: +media-jam-error" >&2
    echo "ERROR: jammed after partial delivery" >&2
    exit 1
    ;;
*)
    # A typo in a mode name must not read as a healthy printer.
    echo "ERROR: rig given an unknown failure mode: $MODE" >&2
    exit 1
    ;;
esac

# Optional dwell, so a test can model lp returning before the paper does.
if [ -f "{outdir}/.slow" ]; then
    sleep "$(cat "{outdir}/.slow")"
fi
exit 0
"""


def shipped_directives() -> dict:
    """The cupsd.conf settings device/install.sh actually applies."""
    found = {}
    try:
        text = INSTALL_SH.read_text()
    except OSError:
        return found
    for key, value in re.findall(r"^\s*set_cupsd\s+(\S+)\s+(\S+)\s*$",
                                 text, re.M):
        found[key] = value
    return found


def shipped_modes() -> dict:
    """The spool/tmp/cache modes device/install.sh sets, keyed by basename.

    cupsd runs its filters as `lp`; a TempDir they cannot write to fails every
    job with "universal filter failed".
    """
    modes = {}
    try:
        text = INSTALL_SH.read_text()
    except OSError:
        return modes
    for path, mode, owner, group in re.findall(
            r"^d\s+(/run/cups\S*)\s+(\d+)\s+(\S+)\s+(\S+)", text, re.M):
        modes[Path(path).name] = (int(mode, 8), owner, group)
    return modes


class CupsRig:
    """A private cupsd. Use as a context manager."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.jobs = self.root / "jobs"
        self.socket = self.root / "cups.sock"
        self.proc = None
        self.directives = shipped_directives()

    # --- lifecycle -----------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def _serverbin(self) -> Path:
        """A ServerBin that is the system's, with our own usb backend."""
        binary = self.root / "bin"
        binary.mkdir(parents=True, exist_ok=True)
        for entry in SYSTEM_SERVERBIN.iterdir():
            if entry.name == "backend":
                continue
            target = binary / entry.name
            if not target.exists():
                target.symlink_to(entry)
        backends = binary / "backend"
        backends.mkdir(exist_ok=True)
        for entry in (SYSTEM_SERVERBIN / "backend").iterdir():
            if entry.name == "usb":
                continue
            target = backends / entry.name
            if not target.exists():
                target.symlink_to(entry)
        usb = backends / "usb"
        usb.write_text(BACKEND.format(uri=DEVICE_URI, make=DEVICE_MAKE,
                                      outdir=self.jobs))
        # cupsd refuses to run a backend that is group- or world-writable,
        # and silently skips one that is not executable.
        usb.chmod(0o700)
        return binary

    def _make_dirs(self) -> None:
        """Create the rig's directories with the modes device/install.sh ships.

        `cups` in shipped_modes() is the parent, applied to the rig root so
        cupsd can traverse it. `jobs` is the rig's own; the backend runs as lp
        and must be able to write there.
        """
        modes = shipped_modes()
        import grp

        try:
            lp_gid = grp.getgrnam("lp").gr_gid
        except KeyError:
            lp_gid = -1

        for name in ("spool", "tmp", "cache", "jobs", "log", "etc/ppd"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

        default = (0o770, "root", "lp")
        for name in ("spool", "tmp", "cache"):
            mode, _owner, _group = modes.get(name, default)
            os.chmod(self.root / name, mode)
            if lp_gid >= 0:
                os.chown(self.root / name, 0, lp_gid)
        # The rig root itself must be traversable by lp, and `jobs` and
        # `log` writable by it.
        os.chmod(self.root, 0o711)
        for name in ("jobs", "log"):
            os.chmod(self.root / name, 0o770)
            if lp_gid >= 0:
                os.chown(self.root / name, 0, lp_gid)

        # And so must every directory above it: cupsd runs its helpers as `lp`,
        # and under a 0700 parent such as pytest's tmp_path `lpinfo -m` returns
        # nothing and the unit reports "no driver".
        self._widened = []
        for parent in self.root.parents:
            if parent == Path(parent.root):
                break
            mode = parent.stat().st_mode & 0o777
            if mode & 0o001:
                break                            # already traversable
            os.chmod(parent, mode | 0o011)
            # Remembered so stop() can restore them: pytest keeps old tmp
            # roots, which hold complete pad PDFs.
            self._widened.append((parent, mode))

    def _write_config(self) -> None:
        self._make_dirs()

        shipped = self.directives
        extra = "\n".join(f"{key} {value}" for key, value in shipped.items())
        (self.root / "etc" / "cupsd.conf").write_text(f"""\
# Generated by tests/cupsrig.py. The block at the bottom is lifted from
# device/install.sh so the rig cannot drift from what the unit ships.
LogLevel warn
MaxLogSize 0
Listen {self.socket}
Browsing Off
DefaultAuthType None
WebInterface No
<Location />
  Order allow,deny
  Allow all
</Location>
<Location /admin>
  Order allow,deny
  Allow all
</Location>
<Policy default>
  <Limit All>
    Order deny,allow
  </Limit>
</Policy>

{extra}
""")
        (self.root / "etc" / "cups-files.conf").write_text(f"""\
ServerRoot {self.root}/etc
ServerBin {self._serverbin()}
DataDir /usr/share/cups
DocumentRoot /usr/share/cups/doc-root
RequestRoot {self.root}/spool
TempDir {self.root}/tmp
CacheDir {self.root}/cache
StateDir {self.root}
AccessLog {self.root}/log/access_log
ErrorLog {self.root}/log/error_log
PageLog {self.root}/log/page_log
FileDevice Yes
""")

    def start(self) -> None:
        self._write_config()
        # cupsd -t is the gate install.sh runs. Failing here means the rig is
        # wrong, not the unit.
        check = subprocess.run(
            [CUPSD, "-t", "-c", str(self.root / "etc" / "cupsd.conf"),
             "-s", str(self.root / "etc" / "cups-files.conf")],
            capture_output=True)
        if check.returncode != 0:
            raise RuntimeError("the rig's CUPS config is invalid:\n"
                               + check.stderr.decode("utf-8", "replace"))
        self.proc = subprocess.Popen(
            [CUPSD, "-f", "-c", str(self.root / "etc" / "cupsd.conf"),
             "-s", str(self.root / "etc" / "cups-files.conf")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            if self.socket.exists() and self._ok(["/usr/bin/lpstat", "-r"]):
                ready = True
                break
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"cupsd exited immediately ({self.proc.returncode}); "
                    f"error_log:\n{self.error_log()}")
            time.sleep(0.2)
        if not ready:
            raise RuntimeError("cupsd never accepted a connection")
        self._verify_backend()

    def _verify_backend(self) -> None:
        """Prove cupsd can run the backend before any test relies on it. A daemon
        that answers `lpstat -r` but cannot execute a backend just reports no
        devices.
        """
        listed = self.run([LPINFO, "-v"]).stdout.decode("utf-8", "replace")
        if any(line.startswith("direct usb://") for line in listed.splitlines()):
            return

        usb = Path(self._serverbin()) / "backend" / "usb"
        clues = [
            "cupsd is running but reported no usb:// device, so it could not "
            "run the rig's backend.",
            f"lpinfo -v said: {listed.strip() or '(nothing)'}",
            f"backend: {usb} mode={oct(usb.stat().st_mode & 0o7777)} "
            f"exists={usb.exists()}",
            f"backend runs standalone: {self._backend_runs(usb)}",
        ]
        # The likeliest cause: cupsd is confined by AppArmor and cannot execute
        # anything under /tmp.
        clues.append(f"apparmor: {_apparmor_state()}")
        clues.append(f"error_log tail:\n{self.error_log()[-1500:]}")
        raise RuntimeError("\n  ".join(clues))

    @staticmethod
    def _backend_runs(path: Path) -> str:
        """Whether the backend works when we run it ourselves."""
        try:
            result = subprocess.run([str(path)], capture_output=True, timeout=10)
            return (f"rc={result.returncode} "
                    f"out={result.stdout.decode('utf-8', 'replace').strip()[:120]!r}")
        except Exception as exc:                 # noqa: BLE001
            return f"could not run it: {exc}"

    def stop(self) -> None:
        for parent, mode in reversed(getattr(self, "_widened", [])):
            try:
                os.chmod(parent, mode)
            except OSError:
                pass
        self._widened = []
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None

    # --- talking to it -------------------------------------------------

    @property
    def env(self) -> dict:
        environment = dict(os.environ)
        environment["CUPS_SERVER"] = str(self.socket)
        return environment

    def run(self, argv, stdin=None):
        """A Cups._run replacement that points every tool at this rig."""
        return subprocess.run(argv, input=stdin, capture_output=True,
                              timeout=120, env=self.env)

    def _ok(self, argv) -> bool:
        try:
            return self.run(argv).returncode == 0
        except Exception:                        # noqa: BLE001
            return False

    def cups(self):
        """An otpunit.printer.Cups wired to this rig, otherwise untouched."""
        from otpunit import printer

        # temp_dir keeps _clear_temp() on the rig's scratch and off the host's
        # /run/cups/tmp.
        return printer.Cups(run=self.run, temp_dir=str(self.root / "tmp"))

    # --- what reached the printer --------------------------------------

    def printed(self) -> list:
        """[(title, bytes)] in the order the backend received them."""
        out = []
        for title in sorted(self.jobs.glob("*.title")):
            data = title.with_suffix(".data")
            out.append((title.read_text().strip(),
                        data.read_bytes() if data.exists() else b""))
        return out

    def titles(self) -> list:
        return [title for title, _ in self.printed()]

    # What the backend understands and what each mode models. A table, so a
    # typo is a ValueError and not a test passing against a printer that never
    # failed.
    FAILURE_MODES = {
        "media-empty": "CUPS_BACKEND_FAILED; ErrorPolicy decides, and "
                       "abort-job discards the job",
        "stop": "CUPS_BACKEND_STOP; the queue is stopped and the job kept",
        "retry": "CUPS_BACKEND_RETRY; the job waits for a later attempt",
        "retry-current": "CUPS_BACKEND_RETRY_CURRENT; retried immediately",
        "disconnect": "the bytes are accepted, then the usb device vanishes",
        "truncate": "half the bytes are accepted, then a jam",
    }

    def set_failure(self, mode: str = "", *, after: int = 0,
                    until: int = 0) -> None:
        """Make the printer fail the way `mode` describes. "" refills the tray.

        `after=N` lets the first N backend invocations succeed before the fault
        begins, which models "copy A reached paper and copy B did not".

        `until=N` ends the fault after invocation N. Required for
        retry-current, which cupsd re-runs immediately.

        Both count backend invocations, not jobs (a retry re-runs the backend
        on the same job), and both count from this call: the invocation counter
        is reset here.
        """
        if mode and mode not in self.FAILURE_MODES:
            raise ValueError(
                f"unknown failure mode {mode!r}; known modes: "
                + ", ".join(sorted(self.FAILURE_MODES)))
        if mode == "retry-current" and until <= 0:
            raise ValueError(
                "retry-current is retried immediately and forever; pass "
                "until=N so the fault clears and the test can terminate")
        if after < 0 or until < 0:
            raise ValueError("after and until count invocations from 1")
        if until and after >= until:
            raise ValueError(
                f"after={after} until={until} leaves no invocation to fail")
        self.jobs.mkdir(parents=True, exist_ok=True)
        (self.jobs / ".mode").write_text(mode)
        (self.jobs / ".after").write_text(str(after))
        (self.jobs / ".until").write_text(str(until))
        # after/until are relative to this call, so the counter rebases.
        (self.jobs / ".count").write_text("0")
        # Any reconfiguration plugs the printer back in, or a stale .unplugged
        # would turn the next mode into a disconnect.
        unplugged = self.jobs / ".unplugged"
        if unplugged.exists():
            unplugged.unlink()

    def fail_jobs(self, failing: bool = True) -> None:
        """Make the tray run out of paper, or refill it."""
        self.set_failure("media-empty" if failing else "")

    def backend_invocations(self) -> int:
        """How many times cupsd ran the backend since the last set_failure. Tells
        a job retried three times from three submitted jobs.
        """
        try:
            return int((self.jobs / ".count").read_text().strip())
        except (OSError, ValueError):
            return 0

    def plugged_in(self) -> bool:
        """Whether the backend is still advertising a device."""
        return not (self.jobs / ".unplugged").exists()

    def slow_jobs(self, seconds: float) -> None:
        """Make the backend dwell, so lp returns well before the paper does."""
        (self.jobs / ".slow").write_text(str(seconds))

    def error_log(self) -> str:
        try:
            return (self.root / "log" / "error_log").read_text()[-4000:]
        except OSError:
            return "(no error log)"

    def spool_files(self) -> list:
        return [p for p in (self.root / "spool").rglob("*") if p.is_file()]


def available() -> str:
    """Why the rig cannot run here, or "" if it can."""
    if not Path(CUPSD).exists():
        return f"{CUPSD} is not installed"
    if not SYSTEM_SERVERBIN.exists():
        return f"{SYSTEM_SERVERBIN} is missing"
    for tool in ("/usr/bin/lp", "/usr/bin/lpstat", "/usr/sbin/lpadmin",
                 "/usr/sbin/lpinfo", "/usr/bin/cancel"):
        if not shutil.which(tool) and not Path(tool).exists():
            return f"{tool} is not installed"
    if os.geteuid() != 0:
        return "cupsd needs root here (it binds a socket and runs backends)"
    if "(enforce)" in _apparmor_state():
        # AppArmor denies a confined cupsd exec of anything under /tmp: the
        # daemon starts, answers lpstat and finds no printers.
        return ("cupsd is confined by AppArmor and cannot run a backend "
                "from a temp directory. Run: sudo apparmor_parser -R "
                "/etc/apparmor.d/usr.sbin.cupsd")
    return ""

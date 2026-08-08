"""A real cupsd, in a temp directory, with a printer that writes to a file.

The single most valuable thing this repository can test without hardware.
Six rounds of reading the code found real bugs; the first run against an
actual cupsd found that the shipped unit could not print a pad at all,
because `MaxJobs 1` makes CUPS *refuse* a second job rather than queue it.
Nothing short of a real daemon was ever going to say so.

Two rules shape this.

**It never touches the system CUPS.** Own ServerRoot, own ServerBin, own
spool, own port-less domain socket, and CUPS_SERVER pointed at it. Killing
the rig leaves the host exactly as it was.

**Its configuration is taken from `device/install.sh`, not restated here.**
The directives that matter -- MaxJobs, ErrorPolicy, PreserveJobHistory,
PreserveJobFiles -- are parsed out of the provisioning script at run time.
A rig with its own copy of those numbers would have gone on passing after
somebody changed the shipped value, which is exactly the class of bug this
exists to catch.
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
    """
    Whether cupsd is confined, which decides if a rig in /tmp can work.

    Ubuntu ships /etc/apparmor.d/usr.sbin.cupsd, and it permits backends
    under /usr/lib/cups/backend and nowhere else. A hermetic rig in a temp
    directory is precisely what that forbids -- so on a confined host the
    profile has to be put in complain mode first:

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

# What the fake backend advertises. Three constraints, all learned the
# hard way against a real cupsd:
#
#   - the URI must start with usb:// or Cups.devices() drops it;
#   - the scheme must match the backend's filename or cupsd will not route
#     jobs to it;
#   - the make and model must be ones _match_ppd can actually find a PPD
#     for, because the point is to exercise that matching rather than to
#     hand the rig a queue it did not have to earn. A made-up name failed
#     with "no driver", which is the CORRECT answer for an unknown printer
#     and a useless one for a fixture.
#
# "Generic PDF Printer" is chosen deliberately over a PCL or PostScript
# driver: its filter chain passes the PDF through unaltered, so what the
# backend receives is byte-for-byte what the unit submitted. Copy A and
# copy B being identical is the property that makes them a pair, and
# asserting it through Ghostscript -- which stamps a creation date into its
# output -- would be flaky for reasons that have nothing to do with the pad.
DEVICE_URI = "usb://Generic/PDF%20Printer?serial=RIG001"
DEVICE_MAKE = "Generic PDF Printer"

BACKEND = r"""#!/bin/sh
# A stand-in for the usb backend.
#
# With no arguments cupsd is asking what is attached, so answer with one
# device. With arguments it is a job: copy it somewhere the test can read
# and assert on the bytes that actually reached the printer.
if [ $# -eq 0 ]; then
    echo 'direct {uri} "{make}" "{make}" "MFG:OTP;MDL:Simulated Laser;"'
    exit 0
fi

OUT="{outdir}/job-$1-$(date +%s%N)"
if [ -n "$6" ] && [ -f "$6" ]; then
    cp "$6" "$OUT.data"
else
    cat > "$OUT.data"
fi
# The job's title is argument 3. Recorded separately so a test can assert
# on the ORDER sheets reached paper without parsing PDFs.
printf '%s\n' "$3" > "$OUT.title"

# A tray that can be made to fail on demand, which is the interesting case:
# under ErrorPolicy abort-job a failed job leaves the queue as empty as a
# successful one.
if [ -f "{outdir}/.fail" ]; then
    echo "STATE: +media-empty-error" >&2
    echo "ERROR: out of paper" >&2
    exit 1
fi
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
    """
    The spool/tmp/cache modes device/install.sh sets, keyed by basename.

    Taken from the shipped tmpfiles.d block rather than restated, for the
    same reason as the directives -- and because these modes are load
    bearing in a way that is easy to miss. cupsd runs its filters as `lp`,
    so a TempDir the filters cannot write to fails every job with
    "universal filter failed", which reads like a driver problem and is
    really a permissions problem. That is how this rig first behaved.
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
        """
        The rig's directories, with the modes device/install.sh ships.

        `cups` in shipped_modes() is the parent, applied to the rig root so
        cupsd can traverse it. `jobs` is ours -- the backend runs as lp and
        has to be able to write what it received.
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

        # And so must every directory above it. cupsd runs its helpers as
        # `lp`, so a rig under a 0700 parent -- which is exactly what
        # pytest's tmp_path is -- cannot execute its own cups-driverd. The
        # symptom is not a permission error: `lpinfo -m` simply returns
        # nothing, _match_ppd finds no candidate, and the unit reports "no
        # driver" for a printer whose PPD is sitting right there.
        self._widened = []
        for parent in self.root.parents:
            if parent == Path(parent.root):
                break
            mode = parent.stat().st_mode & 0o777
            if mode & 0o001:
                break                            # already traversable
            os.chmod(parent, mode | 0o011)
            # Remembered so stop() can put them back. Leaving a 0700 tree
            # at 0711 is permanent, and pytest keeps the last three tmp
            # roots -- which hold complete pad PDFs.
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
        # cupsd -t is the same gate install.sh runs. Failing here means the
        # rig is wrong, not the unit -- say so rather than timing out later.
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
        """
        Prove cupsd can actually run our backend before any test relies on it.

        A daemon that answers `lpstat -r` but cannot execute a backend
        reports no devices, and the failure surfaces several frames away as
        `IndexError: list index out of range` on `devices()[0]`. That is a
        rig whose failure mode is a riddle. Diagnose it here instead.
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
        # The likeliest cause on a distro that ships one: cupsd is confined
        # and cannot execute anything under /tmp. The profile permits
        # /usr/lib/cups/backend/* and nothing else, so a hermetic rig in a
        # temp directory is exactly what it forbids.
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

        # temp_dir matters: _clear_temp() used to empty the module-level
        # /run/cups/tmp, so the harness deleted the HOST's live CUPS
        # scratch as root and never exercised its own. Proven by dropping
        # a sentinel in the host's /run/cups/tmp and running one test.
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

    def fail_jobs(self, failing: bool = True) -> None:
        """Make the tray run out of paper, or refill it."""
        flag = self.jobs / ".fail"
        if failing:
            flag.touch()
        elif flag.exists():
            flag.unlink()

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
        # Measured on an Ubuntu runner: AppArmor denies cupsd exec of
        # anything under /tmp, so a hermetic rig gets a daemon that starts,
        # answers lpstat and finds no printers. Skip with the fix rather
        # than fail with a riddle.
        return ("cupsd is confined by AppArmor and cannot run a backend "
                "from a temp directory. Run: sudo apparmor_parser -R "
                "/etc/apparmor.d/usr.sbin.cupsd")
    return ""

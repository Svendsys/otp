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


class PrinterError(RuntimeError):
    pass


class Cups:
    """Thin wrapper over the CUPS command-line tools."""

    def __init__(self, run=None):
        # Injectable so tests can drive this against recorded output.
        self._run = run or self._subprocess_run

    @staticmethod
    def _subprocess_run(argv, stdin: bytes | None = None):
        return subprocess.run(argv, input=stdin, capture_output=True)

    def _text(self, argv) -> str:
        result = self._run(argv)
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", "replace")

    def devices(self) -> list[Device]:
        """USB printers CUPS can currently see."""
        found = []
        for line in self._text([LPINFO, "-v"]).splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            kind, uri = parts
            if kind != "direct" and not uri.startswith("usb://"):
                continue
            if not uri.startswith("usb://") and "usb" not in uri:
                continue
            found.append(Device(uri=uri.strip(), description=_pretty(uri)))
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

        Tries driverless first, which covers anything made since roughly
        2017 and everything reachable through ipp-usb. Older host-based
        lasers fall back to a PPD matched on the IEEE-1284 device ID.
        """
        result = self._run([LPADMIN, "-p", name, "-E", "-v", device.uri, "-m", "everywhere"])
        if result.returncode == 0:
            return name
        model = self._match_ppd(device)
        if model:
            result = self._run([LPADMIN, "-p", name, "-E", "-v", device.uri, "-m", model])
            if result.returncode == 0:
                return name
        raise PrinterError(f"no driver for {device.label}")

    def _match_ppd(self, device: Device) -> str | None:
        make = _pretty(device.uri).split()[0].lower() if _pretty(device.uri) else ""
        if not make:
            return None
        for line in self._text([LPINFO, "-m"]).splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and make in parts[1].lower():
                return parts[0]
        return None

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
        self._run([CANCEL, "-x", "-a", name])


def _pretty(uri: str) -> str:
    """Turn usb://Brother/HL-2030?serial=... into 'Brother HL-2030'."""
    match = re.match(r"usb://([^/]+)/([^?]+)", uri)
    if not match:
        return ""
    make = match.group(1).replace("%20", " ")
    model = match.group(2).replace("%20", " ")
    return f"{make} {model}".strip()

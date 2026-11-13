"""A Raspberry Pi board identity, faked in a private mount namespace.

gpiozero refuses to build a local pin factory unless it finds a Pi board
revision, so on an ordinary machine it will not talk to a gpio-sim chip. This
module lends the process a revision without patching gpiozero, so the shipped
`GpioButtons` runs through the real gpiozero -> lgpio -> kernel path.

Where the revision comes from: gpiozero's `get_pi_revision()` reads
`/proc/device-tree/system/linux,revision` (big-endian u32), then the
`Revision:` line of `/proc/cpuinfo` only if the first raised ENOENT. procfs
will not create entries under a missing /proc/device-tree, but an existing
procfs file can be bind-mounted over, so /proc/cpuinfo is forged always and the
device-tree file only where the kernel provides it.

Why a private namespace: the revision must be visible to this process alone. A
new mount namespace inherits propagation, and on a systemd host `/` is shared,
so the namespace is made MS_REC|MS_PRIVATE before anything is bound.

Teardown contract: leaving unmounts what was forged but does not setns back.
The kernel refuses a mount-namespace setns from a multi-threaded process, and
lgpio starts a thread at import. So after exit:

* nothing this module mounted is still mounted (test_teardown_leaves_nothing_mounted
  compares the whole mount table);
* every path reads what the host has, /proc/cpuinfo included;
* the process stays in a private copy of the host's mount namespace, so mounts made
  elsewhere afterwards are not seen here.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

# Pi Zero 2 W rev 1.0, the board this unit ships on. The revision decides the
# pin header: on a board without GPIO5/6/13 on its header, `Button(5)` fails
# with PinInvalidPin.
REVISION = 0x902120
MODEL = "Zero2W"

CPUINFO_PATH = Path("/proc/cpuinfo")
DEVICE_TREE_REVISION = Path("/proc/device-tree/system/linux,revision")

# gpiozero reads only the Revision line; diagnostics.py reads Model and Serial
# from the same file, so the fake stays self-consistent.
_CORE = """\
processor\t: {n}
BogoMIPS\t: 38.40
Features\t: fp asimd evtstrm crc32 cpuid
CPU implementer\t: 0x41
CPU architecture: 8
CPU variant\t: 0x0
CPU part\t: 0xd03
CPU revision\t: 4
"""
CPUINFO = "\n".join(_CORE.format(n=n) for n in range(4)) + """
Hardware\t: BCM2835
Revision\t: {revision:06x}
Serial\t\t: 00000000f1e2d3c4
Model\t\t: Raspberry Pi Zero 2 W Rev 1.0
""".format(revision=REVISION)

CLONE_NEWNS = 0x00020000
MS_BIND = 0x1000
MS_REC = 0x4000
MS_PRIVATE = 0x40000

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                    use_errno=True)


def _checked(result: int, what: str) -> None:
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")


def _mount(source, target, flags, what):
    _checked(_libc.mount(source.encode() if source else None,
                         str(target).encode(), None, flags, None), what)


def available() -> str:
    """Why the identity cannot be faked here, or "" if it can."""
    if os.geteuid() != 0:
        return ("faking the board identity needs root: it is a mount "
                "namespace with a bind mount over /proc/cpuinfo")
    if not CPUINFO_PATH.exists():
        return "there is no /proc/cpuinfo here to bind a fake over"
    if shutil.which("nsenter") is None:
        # Only the leak check needs it, but an uncheckable forgery is not one
        # to go ahead with.
        return "nsenter (util-linux) is not installed"
    return ""


class PiIdentity:
    """A mount namespace in which this process, alone, is a Pi Zero 2 W.

    Entering unshares the mount namespace, makes it private and binds the
    forged files over the real ones. Leaving unmounts each of them and stays in
    the namespace (see the teardown contract in the module docstring).
    """

    # No `revision` parameter: /proc/cpuinfo is formatted from the module-level
    # REVISION, so a per-instance value would make the two sources disagree.
    # Change REVISION and MODEL together.
    def __init__(self):
        self.forged: list[str] = []
        self._home = None
        self._temporary: list[str] = []

    def _bind(self, target: Path, content: bytes) -> None:
        handle = tempfile.NamedTemporaryFile(
            prefix="otp-pirig-", suffix=f"-{target.name}", delete=False)
        handle.write(content)
        handle.close()
        self._temporary.append(handle.name)
        _mount(handle.name, target, MS_BIND, f"bind {target}")
        self.forged.append(str(target))

    def __enter__(self) -> "PiIdentity":
        # Opened before the unshare: host_sees_a_pi() uses this descriptor to
        # look at the host from outside.
        self._home = os.open("/proc/self/ns/mnt", os.O_RDONLY)
        # All inside one guard, the check included: an exception after the
        # unshare must not leave the forgery mounted for the rest of the
        # process.
        try:
            _checked(_libc.unshare(CLONE_NEWNS), "unshare(CLONE_NEWNS)")
            _mount("none", "/", MS_REC | MS_PRIVATE, "make / private")
            self._bind(CPUINFO_PATH, CPUINFO.encode())
            if DEVICE_TREE_REVISION.exists():
                self._bind(DEVICE_TREE_REVISION,
                           struct.pack(">L", REVISION))

            from gpiozero.pins.local import get_pi_revision

            found = get_pi_revision()
            if found != REVISION:
                # Something is answering ahead of the forgery. Refuse: the pin
                # header would be wrong.
                raise AssertionError(
                    f"gpiozero read revision {found:#x} with {self.forged} "
                    f"forged; expected {REVISION:#x}. Some source "
                    f"ahead of these is answering first.")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def host_sees_a_pi(self) -> bool:
        """Whether the namespace we came from has been contaminated too.

        Only meaningful while the forgery is still mounted. Uses nsenter, not
        fork(): forking a process that already has lgpio's thread risks a
        deadlock in the child.
        """
        probe = subprocess.run(
            ["nsenter", f"--mount=/proc/self/fd/{self._home}",
             "grep", "-c", "^Revision", str(CPUINFO_PATH)],
            pass_fds=(self._home,), capture_output=True, text=True,
            check=False)
        count = probe.stdout.strip()
        if not count.isdigit():
            # grep exits 1 for "no matches" and nsenter exits 1 for "could not
            # get in", so anything that is not a count means the question was
            # never asked.
            raise AssertionError(
                f"could not read the host's {CPUINFO_PATH} from outside "
                f"the namespace, so nothing was checked: "
                f"{probe.stderr.strip() or probe.returncode}")
        return int(count) > 0

    def __exit__(self, *_exc) -> None:
        # Unmounts and stays in the namespace (see the module docstring). A
        # failed umount propagates, since a forged /proc/cpuinfo left mounted
        # would poison everything after; the finally still closes the namespace
        # descriptor and removes the temp files.
        try:
            while self.forged:
                # Umount before popping, so a failed umount keeps the path for
                # the error and a retry.
                _checked(_libc.umount(self.forged[-1].encode()), "umount")
                self.forged.pop()
        finally:
            if self._home is not None:
                os.close(self._home)
                self._home = None
            for path in self._temporary:
                try:
                    # Safe even if the bind is still up: the mount holds the
                    # inode, and a survivor still shows in the mount table.
                    os.unlink(path)
                except OSError:
                    pass
            self._temporary.clear()


def mount_points() -> tuple:
    """Every mount point this process can see: field 5 of /proc/self/mountinfo,
    sorted. A tuple, not a set, so a second mount stacked on the same path
    still shows as a difference.
    """
    seen = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 4:
            seen.append(fields[4])
    return tuple(sorted(seen))


def open_gpiochips() -> set:
    """Every /dev/gpiochip* this process currently has open."""
    found = set()
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = str(entry.resolve())
        except OSError:                          # closed under us
            continue
        if target.startswith("/dev/gpiochip"):
            found.add(target)
    return found


def bind_gpiozero_to(chip: int):
    """Point gpiozero at one specific gpiochip and return the factory.

    lgpio addresses chips by number and gpiozero defaults to chip 0, while
    gpio-sim lands wherever the kernel puts it.
    """
    from gpiozero import Device
    from gpiozero.pins.lgpio import LGPIOFactory

    Device.pin_factory = LGPIOFactory(chip=chip)
    return Device.pin_factory


def stale_pins() -> list:
    """Names of cached pins that belong to a factory other than the current one.
    Empty is the only healthy answer: a stale pin receives no edges.
    """
    from gpiozero import Device
    from gpiozero.pins.local import LocalPiFactory

    live = Device.pin_factory
    return sorted(
        info.name for info, pin in LocalPiFactory.pins.items()
        if pin.factory is not live
    )


def notify_thread():
    """lgpio's notification thread, or None if lgpio is not imported."""
    try:
        import lgpio
    except ImportError:                          # pragma: no cover
        return None
    return lgpio._notify_thread


def edge_callbacks() -> int:
    """How many edge callbacks lgpio is holding, process-wide. Zero between
    panels: the list is a module-level singleton's, pruned only by an explicit
    cancel (see silence()).
    """
    thread = notify_thread()
    return 0 if thread is None else len(thread.callbacks)


def silence(factory) -> None:
    """Cancel every edge callback belonging to `factory`, before it closes.

    lgpio's notify thread dispatches callbacks with no guard, and it is a
    module-level singleton: one exception kills edge delivery for the whole
    process. A gpiozero pin whose device has been collected tries to stop
    listening on its next edge, and raises if its factory's handle is already
    closed. Cancelling here, while the handle is open, prevents that.

    Raises RuntimeError if any pin could not be silenced.
    """
    from gpiozero.pins.local import LocalPiFactory

    trouble = []
    for info, pin in list(LocalPiFactory.pins.items()):
        if pin.factory is not factory:
            continue
        try:
            # Cancels the lgpio callback and drops it from the notify thread's
            # list.
            pin.when_changed = None
        except Exception as exc:                 # noqa: BLE001
            trouble.append(f"{info.name}: {exc!r}")
    if trouble:
        # Not swallowed: a callback still armed on a handle about to close is
        # what kills the notification thread.
        raise RuntimeError(
            "could not cancel edge callbacks before closing the factory, "
            "so the next edge may kill lgpio's notification thread and "
            "take every button with it: " + "; ".join(trouble))


def release_gpiozero() -> None:
    """Put gpiozero back the way an untouched process would find it.

    Collect before closing, while the chip handle is open: `Device.__del__`
    calls `close()`, which dereferences the factory handle, so a late finalizer
    raises in some later test.

    Then empty the pin cache by hand. `LocalPiFactory.pins` is a class
    attribute shared across factories and `close()` clears it only if every
    `pin.close()` returned; a stale entry hands the next factory a pin on a
    dead handle, which receives no edges.

    Close the factory even when silence() raised, so a failed cancel does not
    also leak the gpiochip handle. Exceptions from either propagate.
    """
    from gpiozero import Device
    from gpiozero.pins.local import LocalPiFactory

    factory, Device.pin_factory = Device.pin_factory, None
    gc.collect()
    try:
        if factory is not None:
            try:
                silence(factory)
            finally:
                # close() runs even if silence() raised. Otherwise the cache
                # clear below would orphan lines still claimed by an
                # unreachable handle, and the next Button(5) fails "GPIO busy".
                # silence() can also raise after the callback is already
                # cancelled.
                factory.close()
    finally:
        # close() clears these on the happy path; a stale pin costs the next
        # panel its edges, so do not depend on it.
        LocalPiFactory.pins.clear()
        LocalPiFactory._reservations.clear()
    # Exceptions from silence() or factory.close() propagate so the teardown
    # fails visibly; where both raise, close()'s carries silence()'s as
    # __context__. The cache is emptied either way, so the next test is
    # unaffected.

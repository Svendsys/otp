"""A Raspberry Pi board identity, faked in a private mount namespace.

`gpio-sim` hands us a real gpiochip whose lines are driven from sysfs, and
that is everything the button code needs -- except that gpiozero will not
talk to it. gpiozero decides it is on a Pi by looking for a board revision,
finds none on an ordinary machine, and refuses to build any local pin
factory at all. Measured on the CI runner, with gpio-sim up and lgpio
installed:

    PinFactoryFallback: Falling back from lgpio: unable to locate Pi
    revision in /proc/device-tree or /proc/cpuinfo
    ...
    gpiozero could not open a gpiochip here: Unable to load any default
    pin factory!

So the whole input surface of a device with no keyboard -- three buttons
and a long press -- skipped, in every run this repository has ever done.

This module lends the process a revision, and only the process. What it
does NOT do is patch gpiozero: the point is to exercise the shipped
`GpioButtons` through the real gpiozero -> lgpio -> kernel path, and a
monkeypatched board detector would leave that path exactly as untested as
it was.

## Where the revision comes from, measured rather than assumed

gpiozero 2.0.1's `get_pi_revision()` reads, in this order:

  1. `/proc/device-tree/system/linux,revision`, as a big-endian u32;
  2. the `Revision:` line in `/proc/cpuinfo`, but ONLY if (1) raised
     ENOENT.

The `/proc/device-tree/model` file named in the issue is never read for
this at all -- it appears in gpiozero only in `native.py`'s device-tree
alias lookup. And it cannot be faked anyway on a machine that has no
device tree: procfs will not let you create entries under it.

    # unshare -m
    # mkdir /proc/device-tree
    mkdir: cannot create directory '/proc/device-tree': No such file
    # mount -t tmpfs none /proc/device-tree
    mount: /proc/device-tree: mount point does not exist.

Bind-mounting over a procfs file that already exists works fine, which is
why `/proc/cpuinfo` is the one that can be forged. Source (1) is forged
too, but only where the kernel already provides it -- a real Pi running
this harness would otherwise report its own revision and open its own
chip instead of the simulated one.

## Why a namespace, and why it is made private first

The revision has to be visible to this process and to nothing else: a
machine whose /proc/cpuinfo claims to be a Pi is a machine where anything
that asks gets lied to. `unshare(CLONE_NEWNS)` alone does not achieve
that. A new mount namespace inherits the propagation of the one it was
copied from, and on any systemd host -- every GitHub runner included --
`/` is *shared*, so a bind mount made inside propagates straight back out.
Measured, in a namespace set up to look like such a host:

    without MS_REC|MS_PRIVATE:  host sees Revision lines: ['Revision: 902120']
    with    MS_REC|MS_PRIVATE:  host sees Revision lines: []

## The teardown contract, stated because half of it is unusual

Leaving unmounts what was forged. It does NOT go back to the namespace it
came from, which would be the obvious way to do this -- one `setns` and
the whole borrowed view drops at once, with no way to half-succeed. The
kernel will not have it: `mntns_install()` requires `fs->users == 1` and
pthreads share `fs`, so a process with a single thread anywhere in it
cannot re-enter a mount namespace. lgpio has one from the moment it is
imported, and never stops it: `_notify_thread = _callback_thread()` is
module level (lgpio.py:562) and that constructor ends in `self.start()`.
Measured in a fresh interpreter, with no panel and no gpiochip anywhere:

    threads before import: ['MainThread']
    threads after  import: ['MainThread', 'Thread-1']

Which is earlier than "the first panel" would suggest. diagnostics.py
asks `_module_version("lgpio")` for a line on the status sheet, and the
unattended sequence prints that sheet, so the thread is running by the
end of the CUPS tests -- before anything here has built a panel at all.
Measured, in the teardown of the first button test:

    OSError: [Errno 22] setns back: Invalid argument

Doing it by hand where possible and by setns where not would give two
paths, one of which only runs before anything spawns a thread -- which is
to say, one that is never exercised where it matters. So there is one
path, and this is what it promises:

  * nothing this module mounted is still mounted -- checked, per exit, by
    test_teardown_leaves_nothing_mounted, which compares the whole mount
    table either side;
  * every path reads exactly what the host has, /proc/cpuinfo included;
  * the process stays in a mount namespace of its own for the rest of its
    life. It is a private copy of the host's, so the only difference that
    survives is that mounts made ELSEWHERE afterwards are not seen here.
    Nothing in this harness mounts anything once pytest is running --
    kernel-sim.sh does its configfs mount before the run starts -- and a
    test that needed to see one would have to say so.
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

# Pi Zero 2 W rev 1.0, the board this unit ships on. Not any Pi: the
# revision decides the pin header, and getting it wrong fails somewhere
# far away and unhelpful. Measured, decoding each with gpiozero itself:
#
#   0x902120 -> Zero2W, BCM2837, header J8, GPIO5/6/13 at J8 pins 29/31/33
#   0x2      -> B,      BCM2835, headers P1/P2/P3, GPIO5/6/13 nowhere
#
# and on that second board `Button(5)` dies with "PinInvalidPin: GPIO5 is
# not a valid pin name" -- a message that says nothing about the real
# mistake, which was three digits in a fake cpuinfo.
REVISION = 0x902120
MODEL = "Zero2W"

CPUINFO_PATH = Path("/proc/cpuinfo")
DEVICE_TREE_REVISION = Path("/proc/device-tree/system/linux,revision")

# Only the Revision line is read by gpiozero. The rest is here because
# diagnostics.py reads Model and Serial out of the same file, and a fake
# that answers one question and contradicts itself on the next two is a
# trap for whoever writes the next test.
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
        # Only the leak check needs it, but a forgery whose containment
        # cannot be checked is not one to go ahead with.
        return "nsenter (util-linux) is not installed"
    return ""


class PiIdentity:
    """
    A mount namespace in which this process, alone, is a Pi Zero 2 W.

    Entering unshares the mount namespace, makes it private so nothing
    escapes, and binds the forged files over the real ones. Leaving
    unmounts each of them by hand and STAYS in the namespace -- see the
    teardown contract at the top of this file for why setns back is not
    available once lgpio has a thread running, and
    test_teardown_leaves_nothing_mounted for the part that is checked.
    """

    def __init__(self, revision: int = REVISION):
        self.revision = revision
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
        # Opened BEFORE the unshare: this descriptor is the only way back,
        # and it is also how host_sees_a_pi() gets to look at the world
        # from outside while the forgery is still mounted.
        self._home = os.open("/proc/self/ns/mnt", os.O_RDONLY)
        # Everything below has to be inside the same guard, the check
        # included. An exception between the unshare and the return would
        # otherwise leave the caller running in a namespace nothing holds
        # a reference to any more -- for the rest of the process, not for
        # the rest of the test.
        try:
            _checked(_libc.unshare(CLONE_NEWNS), "unshare(CLONE_NEWNS)")
            _mount("none", "/", MS_REC | MS_PRIVATE, "make / private")
            self._bind(CPUINFO_PATH, CPUINFO.encode())
            if DEVICE_TREE_REVISION.exists():
                self._bind(DEVICE_TREE_REVISION,
                           struct.pack(">L", self.revision))

            from gpiozero.pins.local import get_pi_revision

            found = get_pi_revision()
            if found != self.revision:
                # Something is answering ahead of what we forged. Refuse,
                # rather than let the tests run against whatever board
                # that turns out to be: the pin header would be wrong and
                # the failure would land somewhere unrelated to the cause.
                raise AssertionError(
                    f"gpiozero read revision {found:#x} with {self.forged} "
                    f"forged; expected {self.revision:#x}. Some source "
                    f"ahead of these is answering first.")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def host_sees_a_pi(self) -> bool:
        """
        Whether the namespace we came from has been contaminated too.

        Asked while the forgery is still mounted, because that is the only
        time the question means anything: checking after teardown would
        pass just as happily for a bind mount that leaked and was then
        cleaned up behind it.

        Through nsenter rather than fork(), even though a forked child
        could setns itself. By the time the panel exists this process has
        lgpio's alert thread in it, and forking a multi-threaded process
        to then allocate in the child is the recipe for a deadlock that
        would hang the harness rather than fail it.
        """
        probe = subprocess.run(
            ["nsenter", f"--mount=/proc/self/fd/{self._home}",
             "grep", "-c", "^Revision", str(CPUINFO_PATH)],
            pass_fds=(self._home,), capture_output=True, text=True,
            check=False)
        count = probe.stdout.strip()
        if not count.isdigit():
            # grep says 1 for "no matches" and nsenter says 1 for "could
            # not get in", which are opposite answers. Anything that is
            # not a count means the question was never asked.
            raise AssertionError(
                f"could not read the host's {CPUINFO_PATH} from outside "
                f"the namespace, so nothing was checked: "
                f"{probe.stderr.strip() or probe.returncode}")
        return int(count) > 0

    def __exit__(self, *_exc) -> None:
        # Unmounts, and stays in the namespace. See the teardown contract
        # at the top of this file for why coming back out is not on the
        # table, and test_teardown_leaves_nothing_mounted for the part
        # that has to be true regardless.
        #
        # No try/except around the umounts. A forged /proc/cpuinfo this
        # process could not get rid of is not something to log and move
        # on from: everything after it would read a board revision it did
        # not ask for, and the failures would land far from the cause.
        while self.forged:
            _checked(_libc.umount(self.forged.pop().encode()), "umount")
        if self._home is not None:
            os.close(self._home)
            self._home = None
        for path in self._temporary:
            try:
                os.unlink(path)                  # gone from /tmp either way
            except OSError:
                pass
        self._temporary.clear()


def mount_points() -> tuple:
    """
    Every mount point this process can see, from the kernel's own table.

    Field 5 of /proc/self/mountinfo, sorted. Used to hold the teardown to
    its promise: a bind mount over a file that already exists is
    invisible in every other way -- same path, same stat, plausible
    contents -- and the mount table is the one place it cannot hide.

    A tuple rather than a set, so that a second mount stacked on a path
    that is already a mount point still shows up as a difference.
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


def default_chip() -> int:
    """
    The chip number gpiozero's own factory selection would open here.

    Observed rather than predicted from gpiozero's source, and safe to
    observe: opening a gpiochip claims no lines, so this does not touch a
    real controller's pins even where /dev/gpiochip0 is real hardware.
    Requires a faked identity -- with no revision the factory refuses to
    construct at all.
    """
    from gpiozero.pins.lgpio import LGPIOFactory

    factory = LGPIOFactory()
    try:
        return factory.chip
    finally:
        factory.close()


def bind_gpiozero_to(chip: int):
    """
    Point gpiozero at one specific gpiochip and return the factory.

    lgpio addresses chips by NUMBER, not by name or by device path, and
    gpiozero's default is chip 0 for every Pi but the 5. Where gpio-sim
    lands at some other number -- it takes whatever the kernel gives it --
    nothing about the default would find it, so the factory is built
    against the recorded chip explicitly.
    """
    from gpiozero import Device
    from gpiozero.pins.lgpio import LGPIOFactory

    Device.pin_factory = LGPIOFactory(chip=chip)
    return Device.pin_factory


def stale_pins() -> list:
    """
    Cached pins that belong to a factory other than the current one.

    Names only, so an assertion message stays readable. Empty is the
    healthy answer and the only one a panel should ever be built on: a
    stale pin is indistinguishable from a working one right up until a
    press fails to arrive, and "no press arrived" is what a broken panel
    and an untested panel both look like.
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
    """
    How many edge callbacks lgpio is currently holding, process-wide.

    Zero between panels. It is a module-level singleton's list, appended
    to by every `Button` and pruned by nothing except an explicit cancel,
    so a panel that does not clean up leaves its three behind for the
    life of the process -- see silence().
    """
    thread = notify_thread()
    return 0 if thread is None else len(thread.callbacks)


def silence(factory) -> None:
    """
    Cancel every edge callback belonging to `factory`, before it closes.

    This is the fix for the defect that made the panel work exactly
    once. lgpio's `_callback_thread.run()` dispatches with no guard:

        for cb in self.callbacks:
            if cb.chip == chip and cb.gpio == gpio:
                cb.func(chip, gpio, level, tick)

    An exception out of `cb.func` therefore kills the thread -- and it
    is a module-level singleton created at import, so once it dies no
    edge reaches any callback anywhere in the process, for the rest of
    the process. Measured, run 31703316190, on the tests after the
    first:

        lgpio: notify thread alive=False go=True callbacks=5

    with the line itself moving perfectly (rest=1, grounded=0,
    released=1, settled=1). Nothing was wrong with the press.

    What raises is gpiozero doing the right thing at the wrong moment.
    `PiPin._call_when_changed` holds its device by weak reference; once
    an earlier test's `Button` is collected that reference is dead, so
    on the next edge the pin tries to stop listening -- `when_changed =
    None` -> `_disable_event_detect()` -> `gpio_get_mode(_handle)` --
    and that pin's factory is closed, so `_handle` is None:

        TypeError: unsupported operand type(s) for &: 'NoneType' and 'int'

    raised inside the thread, reported only as a warning, and fatal to
    every button in the process.

    So the cancel happens HERE: on our terms, while the handle is still
    open, rather than on an edge that arrives after it is not.
    """
    from gpiozero.pins.local import LocalPiFactory

    trouble = []
    for info, pin in list(LocalPiFactory.pins.items()):
        if pin.factory is not factory:
            continue
        try:
            # Cancels the lgpio callback and drops it from the notify
            # thread's list -- the whole point. Through the property
            # rather than the private method because that is the way in
            # gpiozero itself uses.
            pin.when_changed = None
        except Exception as exc:                 # noqa: BLE001
            trouble.append(f"{info.name}: {exc!r}")
    if trouble:
        # Not swallowed. A pin that could not be silenced is a callback
        # still armed on a handle about to close, which is exactly what
        # kills the notification thread later on.
        raise RuntimeError(
            "could not cancel edge callbacks before closing the factory, "
            "so the next edge may kill lgpio's notification thread and "
            "take every button with it: " + "; ".join(trouble))


def release_gpiozero() -> None:
    """
    Put gpiozero back the way an untouched process would find it.

    Three steps beyond dropping the factory, the first two of them
    learned from run
    31699840801, where the first panel worked and every panel after it
    received no edges at all: UP passed, DOWN/OK/BACK each sat out their
    timeout with an empty queue, and the debounce test "passed" only
    because all it asserts is that nothing arrives.

    **Collect before closing, while the chip handle is still open.**
    `gpiozero.Device.__del__` calls `close()`, which reaches
    `LGPIOPin._disable_event_detect` and dereferences `factory._handle`.
    A Device that is already unreachable but not yet collected runs that
    finalizer whenever the collector next gets to it -- in that run, in
    the middle of the FOLLOWING test, by which point the handle is None:

        TypeError: unsupported operand type(s) for &: 'NoneType' and 'int'

    and it surfaced as a PytestUnhandledThreadExceptionWarning, which is
    to say nowhere the failing test would show it.

    **Then empty the pin cache by hand.** `LocalPiFactory.pins` is a
    CLASS attribute -- gpiozero deliberately shares one dict across every
    instance so that mixing back-ends cannot drive one pin two ways --
    and it is keyed by `PinInfo`, which compares equal across factories
    for the same pin of the same board. `PiFactory.close()` empties it
    only after every `pin.close()` has returned, so a single raising
    finalizer leaves it populated. The next factory's `Button(6)` is then
    handed back a pin belonging to the previous, closed factory; it
    claims its alert on a dead handle, and no edge ever arrives.
    Silently -- which is the one outcome this harness exists to refuse.

    **And close the factory even when silencing it failed.** silence()
    raising means a callback may still be armed; skipping close() over
    that adds a leaked gpiochip handle to the same problem rather than
    containing it. The cancel is attempted first and the close happens
    regardless, so the worst case is one loud teardown rather than a
    process whose next Button(5) cannot claim the line.

    TestTheFactoryTeardownBetweenPanels holds every clause of this to
    account without needing a gpiochip.
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
                # close() runs whatever silence() did, and the try/finally
                # is the whole of the reason. `silence(factory)` then
                # `factory.close()` in sequence meant a RuntimeError out
                # of silence skipped every pin.close() and the
                # gpiochip_close behind them, while the clear() below
                # still emptied the shared cache -- so the lines stayed
                # claimed by a handle no object in the process could
                # reach any more, and the next Button(5) died "GPIO
                # busy". Measured, with one pin refusing to be silenced:
                # pins closed [], chip handle closed False.
                #
                # Worse, silence() can raise on a failure that is not
                # dangerous at all: LGPIOPin._disable_event_detect
                # cancels the lgpio callback FIRST and only then
                # re-claims the line as an input, so a throw from the
                # re-claim means the callback is already gone. Losing
                # the handle over that would manufacture exactly the
                # leak the RuntimeError is raised to prevent.
                factory.close()
    finally:
        # Belt and braces: close() clears these itself on the happy path.
        # Leaving a stale pin behind costs the next panel its edges, so
        # they do not get to depend on close() having reached the end.
        LocalPiFactory.pins.clear()
        LocalPiFactory._reservations.clear()
    # Note what is deliberately NOT caught: if silence() or
    # factory.close() raises, it comes out of here and pytest reports the
    # teardown as an error -- and where both raise, close()'s exception
    # is the one that propagates with silence()'s attached to it as
    # __context__, so the traceback carries both.
    # That is the point. The collect above is what should stop it happening,
    # and if it happens anyway the run must say so in the open rather
    # than in a warning nobody reads. The cache is emptied either way, so
    # a raise here cannot spread to the next test -- it only ends this
    # one loudly, which is the trade this repository always takes.

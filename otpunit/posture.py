"""Whether this machine is fit to hold live key material right now.

The design rests on two properties of the running system: nothing in RAM
can be paged to storage, and nothing written to the root filesystem
survives power-off. Both are set up at provisioning, and both can be
silently absent -- an initramfs the firmware did not load leaves the root
writable -- so they are checked on the machine itself before a live pad is
made, rather than assumed.

Training pads are exempt: they are not secret.
"""
from __future__ import annotations

MOUNTS = "/proc/mounts"
SWAPS = "/proc/swaps"


def root_is_volatile(mounts: str = MOUNTS) -> bool:
    """True when / is an overlay, i.e. writes go to RAM."""
    try:
        with open(mounts) as handle:
            root = [line.split() for line in handle
                    if len(line.split()) >= 3 and line.split()[1] == "/"]
    except OSError:
        return False
    # The last entry for a mount point is the one in force.
    return bool(root) and root[-1][2] == "overlay"


def swap_is_off(swaps: str = SWAPS) -> bool:
    try:
        with open(swaps) as handle:
            return not [line for line in handle.readlines()[1:] if line.strip()]
    except OSError:
        return False


def problems(allow_writable_root: bool = False, mounts: str = MOUNTS,
             swaps: str = SWAPS) -> list[str]:
    """Reasons not to make a live pad, each short enough for the panel."""
    found = []
    if not swap_is_off(swaps):
        found.append("SWAP IS ON")
    if not allow_writable_root and not root_is_volatile(mounts):
        found.append("ROOT DISK IS WRITABLE")
    return found


def refusal(settings, **paths) -> str:
    """Why live pads are refused under these settings, or ""."""
    return ", ".join(problems(
        getattr(settings, "allow_writable_root", False), **paths))

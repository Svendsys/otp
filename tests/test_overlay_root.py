"""The read-only root overlay: what install.sh does, and what proves it.

Issue #9. Nothing enabled the overlay -- install.sh printed advice, the
pi-gen stage printed the same advice, and the image booted read-write -- so
the property the whole design rests on, that pulling the plug erases the
session, was true only of units whose owner had run a command they were told
about in a paragraph of output.

The advice was also broken on trixie, which is why install.sh does not call
raspi-config. On bookworm and later `enable_overlayfs` installs Debian's
overlayroot package, whose initramfs script moves the root aside with
`mount --move`; the mount(8) an initramfs-tools initrd actually contains is
klibc's, which prints `mount: invalid option --` and takes init down with
it. That is the panic recorded in issue #9's first comment, and it is a
property of the package rather than of the guest it was first seen in.

These tests RUN the shipped blocks rather than pattern-matching them, for
the reason test_service_unit.py gives: the first version of the group guard
matched the text of a `for` header and never executed the body. Where a
block writes to absolute paths that a test has no business touching, it is
sliced at the boundary where its behaviour is still parameterised -- the
boot directory and the command-line file are variables, and the verification
that decides whether provisioning fails is entirely inside that boundary.
"""
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "device" / "install.sh"
IMG_BOOT = REPO / "harness" / "img-boot.sh"
GUEST_CHECK = REPO / "harness" / "img-guest-check.sh"
IMGCHECK_UNIT = REPO / "device" / "systemd" / "otp-unit-imgcheck.service"
IDENTITY_SH = REPO / "device" / "persist-identity.sh"
IDENTITY_UNIT = REPO / "device" / "systemd" / "otp-unit-identity.service"

# What a machine-id looks like once systemd has generated one: 32 lower-case
# hex characters. Both ends of the persistence -- the initramfs restore and
# the guest check that reads it back -- are written around that shape.
LIVE_ID = "0f9c2b4d6e8a1c3f5b7d9e0a2c4e6f81"


def slice_between(text: str, first: str, last: str) -> str:
    """The shipped lines from `first` up to and including `last`."""
    start = text.index(first)
    end = text.index(last, start) + len(last)
    return text[start:end]


def run_block(block: str, tmp_path, *, env=None, preamble=""):
    """Run a slice of install.sh with a stub PATH and a fake boot directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # systemctl is stubbed rather than allowed through: the block disables a
    # first-boot service and masks another, and a test must not touch the
    # machine it runs on. It RECORDS what it was asked to do, because
    # "install.sh masks systemd-growfs-root.service" is a claim about a
    # command that was issued, and a stub that only exits 0 cannot tell a
    # block that issued it from one that never did.
    (bin_dir / "systemctl").write_text(
        '#!/bin/sh\n'
        'if [ -n "${SYSTEMCTL_LOG:-}" ]; then printf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"; fi\n'
        'exit 0\n')
    (bin_dir / "systemctl").chmod(0o755)
    runner = tmp_path / "run.sh"
    runner.write_text(
        "set -euo pipefail\n"
        "log() { printf 'LOG %s\\n' \"$*\"; }\n"
        + preamble + "\n" + block + "\n"
    )
    return subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {}),
             "SYSTEMCTL_LOG": str(tmp_path / "systemctl-calls.log"),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )


def systemctl_calls(tmp_path) -> list:
    """Every systemctl argument list the block under test issued."""
    log = tmp_path / "systemctl-calls.log"
    return log.read_text().splitlines() if log.exists() else []


# --- the kernel command line ---------------------------------------------

CMDLINE_BLOCK = ('    if ! grep -q "boot=overlay" "$CMDLINE_TXT"; then',
                 '    systemctl disable rpi-resize.service 2>/dev/null || true')

# pi-gen's own stage1 cmdline.txt, with export-image's PARTUUID substituted,
# which is what install.sh actually finds in the chroot.
PI_GEN_CMDLINE = ("console=serial0,115200 console=tty1 "
                  "root=PARTUUID=1a2b3c4d-02 rootfstype=ext4 fsck.repair=yes "
                  "rootwait resize\n")


def cmdline_after(tmp_path, before: str) -> str:
    boot = tmp_path / "boot"
    boot.mkdir(exist_ok=True)
    (boot / "cmdline.txt").write_text(before)
    block = slice_between(INSTALL.read_text(), *CMDLINE_BLOCK)
    proc = run_block(block, tmp_path,
                     preamble=f'CMDLINE_TXT="{boot}/cmdline.txt"')
    assert proc.returncode == 0, proc.stderr
    return (boot / "cmdline.txt").read_text()


def test_the_overlay_token_is_added_to_the_command_line(tmp_path):
    after = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    assert after.startswith("boot=overlay "), after
    # Everything else survives. Losing root= here would be an unbootable
    # card, and the token is prepended precisely so nothing else moves.
    assert "root=PARTUUID=1a2b3c4d-02" in after
    assert "rootfstype=ext4" in after


def test_adding_the_token_twice_does_not_add_it_twice(tmp_path):
    # install.sh says "safe to rerun" at the top of the file, and the
    # documented iteration loop is edit, rerun, reboot.
    once = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    twice = cmdline_after(tmp_path, once)
    assert twice.count("boot=overlay") == 1, twice


def test_the_first_boot_resize_token_is_removed(tmp_path):
    """
    An online resize cannot grow a filesystem mounted read-only as the
    overlay's lower layer, and a first-boot service that fails and reboots
    is a loop rather than a message. The appliance has nothing to grow.
    """
    after = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    assert " resize" not in after and not after.endswith("resize\n"), after
    # Not a blanket deletion of the substring: fsck.repair and rootwait are
    # neighbours here and a sloppier sed would have taken part of one.
    assert "fsck.repair=yes" in after
    assert "rootwait" in after


def test_systemds_own_grower_is_masked_as_well_as_pi_gens(tmp_path):
    """
    Two units grow the root filesystem on a Pi OS image, and disabling one
    of them is what shipped.

    rpi-resize.service is the pi-gen one and it is disabled above.
    systemd-growfs-root.service is systemd's, and nothing enables it: it is
    hooked onto the root mount by systemd-fstab-generator on every boot, so
    there is no symlink for `disable` to remove and it ran on every boot of
    every image this project has built. On an overlay root it cannot
    succeed -- `/` is an overlayfs, systemd-growfs wants a block device --
    and run 72's tier-3 console carries the result in both boots:

        systemd-growfs[293]: File system "/" not backed by block device.
        systemd[1]: systemd-growfs-root.service: Failed with result 'exit-code'.

    It was invisible until issue #21 put the journal on the console. The
    verb has to be `mask`: `disable` is a no-op against a generator.
    """
    cmdline_after(tmp_path, PI_GEN_CMDLINE)
    calls = systemctl_calls(tmp_path)
    assert "mask systemd-growfs-root.service" in calls, calls
    # The pi-gen half is still done -- this is an addition to that decision,
    # not a replacement for it.
    assert any(c.startswith("disable rpi-resize.service") for c in calls), calls


# --- building the initramfs the overlay lives in --------------------------

def kernels_guard_block() -> str:
    """The empty-kernel-list guard, ending at its own `fi`.

    The end is computed rather than quoted for the same reason the build
    loop's is: an edit inside the block has to surface as a behavioural
    failure, not as a slice that stopped matching.
    """
    text = INSTALL.read_text()
    start = text.index('    if [ -z "$KERNELS" ]; then')
    return text[start:text.index("\n    fi", start) + len("\n    fi")]


def initramfs_build_block() -> str:
    """The per-kernel build loop, sliced without its trailing flag.

    The end anchor deliberately stops at `done` rather than quoting the
    `update-initramfs` line, so a change to that line shows up as a failed
    assertion about what was RUN rather than as a slice that no longer
    matches.
    """
    text = INSTALL.read_text()
    start = text.index("    for kern in $KERNELS; do")
    return text[start:text.index("\n    done", start) + len("\n    done")]


def run_initramfs_build(tmp_path, kernels):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "argv.txt"
    (bin_dir / "update-initramfs").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {record}\n')
    (bin_dir / "update-initramfs").chmod(0o755)
    proc = run_block(initramfs_build_block(), tmp_path,
                     preamble=f'KERNELS="{kernels}"')
    return proc, (record.read_text() if record.exists() else "")


def test_the_initramfs_is_created_and_not_merely_updated(tmp_path):
    """
    `-c`, and the shipped comment names the exact defect: pi-gen sets
    update_initramfs=no in update-initramfs.conf so kernel installs build
    nothing, and update-initramfs honours that setting on its UPDATE path
    only. `-u` therefore prints "Not updating initramfs." and exits 0,
    leaving the overlay script out of an initramfs that already exists, has
    the right name and is the right size -- the firmware loads it,
    boot=overlay is read by nothing, and the unit comes up on a writable
    root. Nothing was red for that edit until this test.
    """
    kernels = "6.12.96+rpt-rpi-v8 6.12.96+rpt-rpi-2712"
    proc, argv = run_initramfs_build(tmp_path, kernels)
    assert proc.returncode == 0, proc.stderr
    # Every installed kernel, not just one: the image carries a second one
    # for the Pi 5, and an initramfs missing for the kernel that boots is
    # the same read-write root by another route.
    assert argv.split() and argv.splitlines() == [
        f"-c -k {kern}" for kern in kernels.split()], argv


def test_no_kernel_under_lib_modules_stops_provisioning(tmp_path):
    """
    In pi-gen's chroot `uname -r` is the BUILD HOST's kernel, for which
    there are no modules and no initramfs can be built -- so the kernel list
    is read out of /lib/modules, and an empty list means the overlay cannot
    be enabled at all. Building nothing and carrying on leaves the later
    verification to catch it, which is one guard rather than two.

    KERNELS is set here rather than discovered: the discovery loop reads
    /lib/modules on the machine running the test, and what it finds there
    is not this repository's business.
    """
    proc = run_block(kernels_guard_block(), tmp_path, preamble='KERNELS=""')
    assert proc.returncode != 0, proc.stdout
    assert "no kernel found" in proc.stderr, proc.stderr


def test_a_kernel_list_passes_the_guard(tmp_path):
    # The positive control: a guard that stopped everything would satisfy
    # the test above.
    proc = run_block(kernels_guard_block(), tmp_path,
                     preamble='KERNELS=" 6.12.96+rpt-rpi-v8"')
    assert proc.returncode == 0, proc.stderr


# --- the verification that decides whether provisioning fails -------------

VERIFY_BLOCK = ('    OVERLAY_INITRAMFS=""',
                '    log "  overlay initramfs: $OVERLAY_INITRAMFS"')


# A REAL-SIZED listing, with scripts/overlay early in it and a long tail
# after. Both details are load-bearing. install.sh runs under pipefail, and
# the first version of the check piped lsinitramfs into `grep -q`: grep
# closes the pipe on its first match, the producer dies of SIGPIPE, and the
# pipeline returns 141 -- so the check failed on exactly the initramfs that
# should pass. A three-line listing never reproduces it, because the producer
# is finished before grep can exit. Measured with the piped form and this
# fixture: NO MATCH, rc 141.
GOOD_LISTING = ("scripts/local\nscripts/overlay\n"
                + "".join(f"usr/lib/modules/6.12.96/kernel/drivers/x{i}.ko\n"
                          for i in range(20000)))
NO_OVERLAY_LISTING = GOOD_LISTING.replace("scripts/overlay\n", "")


def run_verification(tmp_path, *, initramfs_names=("initramfs8",),
                     listing=GOOD_LISTING,
                     cmdline="boot=overlay console=tty1\n"):
    boot = tmp_path / "boot"
    boot.mkdir(exist_ok=True)
    for name in initramfs_names:
        (boot / name).write_bytes(b"\x1f\x8b not really a cpio archive")
    (boot / "cmdline.txt").write_text(cmdline)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # The real lsinitramfs unpacks a compressed cpio; what matters here is
    # what install.sh does with the listing, so the listing is the stub.
    (bin_dir / "lsinitramfs").write_text(
        "#!/bin/sh\ncat %s\n" % (tmp_path / "listing.txt"))
    (bin_dir / "lsinitramfs").chmod(0o755)
    (tmp_path / "listing.txt").write_text(listing)
    block = slice_between(INSTALL.read_text(), *VERIFY_BLOCK)
    return run_block(block, tmp_path,
                     preamble=f'BOOT_DIR="{boot}"\n'
                              f'CMDLINE_TXT="{boot}/cmdline.txt"')


def test_a_good_initramfs_passes_the_verification(tmp_path):
    # The positive control. Everything below asserts that provisioning
    # STOPS, and "it stopped" is also what a broken rig produces.
    proc = run_verification(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "initramfs8" in proc.stdout, proc.stdout


def test_no_initramfs_at_all_fails_the_install(tmp_path):
    proc = run_verification(tmp_path, initramfs_names=())
    assert proc.returncode != 0
    assert "scripts/overlay" in proc.stderr, proc.stderr


def test_an_initramfs_without_the_overlay_script_fails_the_install(tmp_path):
    """
    The silent case, and the reason this check reads the archive rather than
    stat()ing the file. An initramfs built before the overlay script existed
    is the right size, has the right name, and is loaded by the firmware --
    and boot=overlay is then read by nothing, so the unit comes up on a
    writable root and says nothing about it.
    """
    proc = run_verification(tmp_path, listing=NO_OVERLAY_LISTING)
    assert proc.returncode != 0
    assert "would come up on a writable root" in proc.stderr, proc.stderr


def test_the_command_line_token_going_missing_fails_the_install(tmp_path):
    proc = run_verification(tmp_path, cmdline="console=tty1\n")
    assert proc.returncode != 0
    assert "boot=overlay is not in" in proc.stderr, proc.stderr


# --- what the initramfs script itself does -------------------------------

def overlay_script() -> str:
    text = INSTALL.read_text()
    return slice_between(text, "# Mount the root filesystem read-only under",
                         "OVERLAY\n")


def test_the_lower_layer_is_mounted_read_only():
    # The line that makes the card read-only for the life of the boot. An
    # overlay whose lower layer is writable is a slower way of writing to
    # the SD card, not a reset-on-power-cycle appliance.
    lower = [line for line in overlay_script().splitlines()
             if "/lower" in line and "mount " in line]
    assert lower, "nothing in the overlay script mounts a lower layer"
    assert all(" -r " in line for line in lower), lower


def test_a_failed_overlay_panics_rather_than_booting_writable():
    """
    The direction of the failure is a safety decision, so it is asserted.

    A unit that quietly came up without the overlay would keep on the card
    everything the session wrote -- the one outcome this mechanism exists to
    prevent -- and would look identical from the outside. Panicking is loud;
    tier 3 catches it before a release.
    """
    script = overlay_script()
    assert "panic \"Failed to assemble the root overlay.\"" in script
    assert "panic \"Failed to mount the overlay's upper layer in RAM.\"" in script


def test_install_sh_no_longer_tells_the_operator_to_run_raspi_config():
    """
    Not tidiness. On trixie that command installs overlayroot, whose
    initramfs script calls `mount --move` -- an option klibc's mount, the
    one in the initrd, does not have. Following the old advice is how a
    provisioned unit stops booting.
    """
    text = INSTALL.read_text()
    instructions = [line for line in text.splitlines()
                    if "enable_overlayfs" in line and not line.lstrip().startswith("#")]
    assert not instructions, instructions


# --- the one file the overlay lets through --------------------------------

MACHINE_ID_FN = ("otp_restore_machine_id()\n{", "\treturn 0\n}")

# The initrd has klibc's mount, which really mounts. Here it does not: the
# test lays the fake card's contents at the mountpoint itself and this stands
# in for the syscall, so what is exercised is the function's own logic --
# which candidate it tries, what it accepts, and where it writes.
MOUNT_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$MOUNT_LOG"
[ -z "${MOUNT_REFUSES-}" ] || exit 1
exit 0
"""
UMOUNT_STUB = """#!/bin/sh
printf 'umount %s\\n' "$*" >> "$MOUNT_LOG"
exit 0
"""


def run_machine_id_restore(tmp_path, *, stored=LIVE_ID, mountable=True,
                           make_store=True):
    """Run the shipped initramfs function against a tree this test builds."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    mnt = tmp_path / "mnt"
    (mnt / "otp-identity").mkdir(parents=True, exist_ok=True)
    if make_store and stored is not None:
        (mnt / "otp-identity" / "machine-id").write_text(stored + "\n")
    rootmnt = tmp_path / "root"
    (rootmnt / "etc").mkdir(parents=True, exist_ok=True)
    # The device the first candidate resolves to. `${ROOT%p[0-9]}p1` turns
    # .../mmcblk0p2 into .../mmcblk0p1, which is the derivation the shipped
    # line makes on a Pi.
    (tmp_path / "mmcblk0p1").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("mount", MOUNT_STUB), ("umount", UMOUNT_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    runner = tmp_path / "restore.sh"
    runner.write_text(
        "set -u\n"
        f'ROOT="{tmp_path}/mmcblk0p2"\n'
        f'rootmnt="{rootmnt}"\n'
        f'OTP_IDENTITY_MNT="{mnt}"\n'
        + slice_between(INSTALL.read_text(), *MACHINE_ID_FN) + "\n"
        "otp_restore_machine_id\n"
        'printf "rc=%s\\n" "$?"\n')
    env = {**os.environ, "MOUNT_LOG": str(tmp_path / "mount.log"),
           "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    if not mountable:
        env["MOUNT_REFUSES"] = "1"
    proc = subprocess.run(["bash", str(runner)], capture_output=True,
                          text=True, timeout=60, env=env)
    restored = rootmnt / "etc" / "machine-id"
    return proc, (restored.read_text().strip() if restored.exists() else None)


def test_the_initramfs_puts_the_stored_machine_id_where_pid_1_will_read_it(tmp_path):
    """
    The positive control. PID 1 reads /etc/machine-id before it looks at a
    single unit, so this is the last moment anything can put it there -- and
    the file has to land inside the assembled overlay, not beside it.
    """
    proc, restored = run_machine_id_restore(tmp_path)
    assert "rc=0" in proc.stdout, proc.stderr
    assert restored == LIVE_ID, proc.stdout


def test_the_identity_partition_is_mounted_read_only(tmp_path):
    """
    The whole point of the mechanism this rides in is that the boot never
    holds a writable handle on storage it is not writing to. Reading a
    machine-id needs none.
    """
    run_machine_id_restore(tmp_path)
    calls = (tmp_path / "mount.log").read_text().splitlines()
    mounts = [c for c in calls if not c.startswith("umount")]
    assert mounts, "the function never tried to mount anything"
    assert all(" -r " in f" {c} " for c in mounts), mounts


def test_a_machine_id_that_is_not_32_hex_characters_is_not_written(tmp_path):
    """
    systemd rejects a malformed id and calls the boot a first boot -- which
    is the state this exists to leave, arrived at by a longer route. Anything
    that is not exactly the shape systemd accepts must be dropped here, where
    the image's own file is still standing.
    """
    for bad in ("uninitialized", "", "deadbeef", LIVE_ID + "0", LIVE_ID[:-1] + "Z"):
        proc, restored = run_machine_id_restore(tmp_path / f"bad-{len(bad)}-{bad[:4]}",
                                                stored=bad)
        assert "rc=0" in proc.stdout, proc.stderr
        assert restored is None, f"{bad!r} was written as a machine-id"


def test_a_card_with_no_stored_identity_leaves_the_boot_alone(tmp_path):
    """Never fatal. The overlay panics when it fails because a unit that
    booted writable is the one thing it exists to prevent; an identity is
    not that, and a first boot is what this machine did before."""
    proc, restored = run_machine_id_restore(tmp_path, make_store=False)
    assert "rc=0" in proc.stdout, proc.stderr
    assert restored is None


def test_a_partition_that_will_not_mount_leaves_the_boot_alone(tmp_path):
    proc, restored = run_machine_id_restore(tmp_path, mountable=False)
    assert "rc=0" in proc.stdout, proc.stderr
    assert restored is None


def test_the_overlay_restores_the_machine_id_after_it_is_assembled(tmp_path):
    """
    Ordering, and it is not cosmetic: the write goes THROUGH the overlay into
    its tmpfs upper layer, so a call placed before the mount would write to
    the initrd's own root and vanish at pivot -- silently, with the boot
    looking exactly the same.
    """
    script = overlay_script()
    assert "otp_restore_machine_id" in script, \
        "the overlay script no longer restores the machine-id, so every " \
        "boot is a first boot again"
    called = script.index("\n\totp_restore_machine_id")
    assembled = script.index('panic "Failed to assemble the root overlay."')
    assert called > assembled, \
        "otp_restore_machine_id runs before the overlay is mounted, so it " \
        "writes to the initrd's root and the write is lost at pivot"


# --- the userspace half: recording the machine-id -------------------------

# persist-identity.sh asks findmnt the same question the guest probe does:
# which filesystem contains the store. The stub answers out of the
# environment, so a test can put the store inside the overlay without needing
# two real filesystems -- which no CI runner reliably has under /tmp.
IDENTITY_FINDMNT_STUB = """#!/bin/sh
case "$*" in
    *--target*) printf '%s\\n' "${FAKE_STORE_SRC-/dev/mmcblk0p1}" ;;
    *" /") printf '%s\\n' "${FAKE_ROOT_SRC-overlay}" ;;
    *) echo "stub: unexpected findmnt $*" >&2; exit 64 ;;
esac
"""


# chpasswd, which is the one command the script runs that a test must not let
# through: the real one edits the machine's own /etc/shadow, and --root would
# make it chroot into a tmp_path. The stub does what chpasswd does to the tree
# it is pointed at -- rewrites field 2 of the named account -- so the script's
# read-back after the apply is a read-back of a file that really changed. It
# also records its argv, because "--root reached it" is a claim about an
# argument list and a stub that only exits 0 cannot support one.
#
# APPLY=no turns it into the failure this repository keeps finding: a command
# that exits 0 having done nothing at all.
CHPASSWD_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$CHPASSWD_ARGV"
line=$(cat)
[ "${CHPASSWD_APPLY-yes}" = yes ] || exit "${CHPASSWD_RC-0}"
user=${line%%:*}
hash=${line#*:}
awk -F: -v u="$user" -v h="$hash" 'BEGIN{OFS=":"} $1==u{$2=h} {print}' \
    "$CHPASSWD_SHADOW" > "$CHPASSWD_SHADOW.new"
mv "$CHPASSWD_SHADOW.new" "$CHPASSWD_SHADOW"
exit "${CHPASSWD_RC-0}"
"""

# What pi-gen leaves in /etc/shadow before an operator ever seeds anything:
# FIRST_USER_PASS, hashed with a random salt, and image/build.sh makes the
# password itself random when OTP_USER_PASS_HASH is unset. Nobody has it.
BUILD_TIME_HASH = "$6$aVJ7yQ2mKp0xRt4d$" + "B" * 43
# The hash an operator's own userconf.txt carried, applied by the wizard.
SEEDED_HASH = "$6$otpimgcheck$" + "A" * 86


def run_persist_identity(tmp_path, *, machine_id=LIVE_ID, stored_id=None,
                         etc_keys=("ed25519",), store_src="/dev/mmcblk0p1",
                         credential=None, live_hash=BUILD_TIME_HASH,
                         first_user="otp", mode=None, chpasswd_env=None):
    """Run the shipped script against a fake boot partition and a fake /etc.

    `etc_keys` puts real-looking host keys in the fake /etc/ssh. They are not
    there to be persisted -- they are there so that "nothing copies them" is a
    statement about a tree that HAD them.

    `credential` is what the card already holds; `live_hash` is what
    /etc/shadow holds for the UID-1000 account this boot. The default pair is
    the interesting one: a card with nothing on it and an account carrying the
    random build-time password.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    boot = tmp_path / "boot"
    (boot / "otp-identity").mkdir(parents=True, exist_ok=True)
    root = tmp_path / "root"
    (root / "etc" / "ssh").mkdir(parents=True, exist_ok=True)
    if machine_id is not None:
        (root / "etc" / "machine-id").write_text(machine_id + "\n")
    if stored_id is not None:
        (boot / "otp-identity" / "machine-id").write_text(stored_id + "\n")
    if credential is not None:
        (boot / "otp-identity" / "credential").write_text(credential)
    for name in etc_keys:
        (root / "etc" / "ssh" / f"ssh_host_{name}_key").write_text(f"live {name}\n")
        (root / "etc" / "ssh" / f"ssh_host_{name}_key.pub").write_text(f"pub {name}\n")

    # A passwd/shadow pair with a root account in it as well, so that "the
    # store may only name the UID-1000 user" is a statement about a tree that
    # HAS another account to name.
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
        + (f"{first_user}:x:1000:1000:,,,:/home/{first_user}:/bin/bash\n"
           if first_user else ""))
    (root / "etc" / "shadow").write_text(
        "root:!:20088:0:99999:7:::\n"
        "daemon:*:20088:0:99999:7:::\n"
        + (f"{first_user}:{live_hash}:20088:0:99999:7:::\n"
           if first_user else ""))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "findmnt").write_text(IDENTITY_FINDMNT_STUB)
    (bin_dir / "findmnt").chmod(0o755)
    (bin_dir / "chpasswd").write_text(CHPASSWD_STUB)
    (bin_dir / "chpasswd").chmod(0o755)
    proc = invoke_persist_identity(tmp_path, mode=mode, store_src=store_src,
                                   chpasswd_env=chpasswd_env)
    return proc, boot, root


def invoke_persist_identity(tmp_path, *, mode=None, store_src="/dev/mmcblk0p1",
                            chpasswd_env=None):
    """Run the script AGAINST A TREE THAT ALREADY EXISTS.

    Split out of the builder above so that a test can boot the same fake unit
    more than once. The ordering this change is about -- a restore at sysinit,
    a fresh seed applied over it at multi-user, and the store replaced after
    that -- is a sequence, and a fixture that rebuilt /etc between the steps
    could not express it.
    """
    boot, root = tmp_path / "boot", tmp_path / "root"
    return subprocess.run(
        ["bash", str(IDENTITY_SH), "--boot-dir", str(boot), "--root", str(root)]
        + ([mode] if mode else []),
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "FAKE_STORE_SRC": store_src,
             "CHPASSWD_SHADOW": str(root / "etc" / "shadow"),
             "CHPASSWD_ARGV": str(tmp_path / "chpasswd-argv.log"),
             **(chpasswd_env or {}),
             "PATH": f"{tmp_path}/bin:{os.environ['PATH']}"})


def set_shadow_hash(root, new_hash, user="otp"):
    """What userconf-service's own `chpasswd -e` does, done by hand."""
    out = []
    for line in (root / "etc" / "shadow").read_text().splitlines():
        fields = line.split(":")
        if fields[0] == user:
            fields[1] = new_hash
        out.append(":".join(fields))
    (root / "etc" / "shadow").write_text("\n".join(out) + "\n")


def shadow_hash(root, user="otp") -> str:
    """Field 2 of the account's line, as the machine would read it."""
    for line in (root / "etc" / "shadow").read_text().splitlines():
        fields = line.split(":")
        if fields[0] == user:
            return fields[1]
    return ""


def chpasswd_argv(tmp_path) -> list:
    log = tmp_path / "chpasswd-argv.log"
    return log.read_text().splitlines() if log.exists() else []


def test_a_first_boot_records_what_it_has(tmp_path):
    # The positive control: the store starts empty and comes out holding this
    # machine's identity.
    proc, boot, _ = run_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "machine-id").read_text().strip() == LIVE_ID


def test_no_private_key_is_written_to_the_boot_partition(tmp_path):
    """
    THE OWNER'S DECISION, held against the shipped script rather than against
    the comment that announces it.

    An earlier version of this script copied /etc/ssh/ssh_host_*_key onto the
    FAT partition so the fingerprint of a key printer stopped changing on
    every power cycle. image/build.sh sets ENABLE_SSH=0, so pi-gen leaves
    ssh.service disabled and this appliance never runs sshd -- the only thing
    that ever started it was the first-boot `preset-all` that persisting the
    machine-id ends, and run 32020772161's boot2 console does not mention
    ssh.service, ssh.socket or OpenBSD once while still reaching
    multi-user.target. So the fingerprint was one nobody could ever be shown,
    and the private keys were on a partition every local account can read.

    The fake /etc/ssh here HAS keys. Nothing may end up on the card, and
    nothing may create a place to put them either -- an empty `ssh` directory
    on the boot partition is the next person's invitation.
    """
    proc, boot, _ = run_persist_identity(
        tmp_path, etc_keys=("ed25519", "rsa", "ecdsa"))
    assert proc.returncode == 0, proc.stderr
    landed = [p for p in boot.rglob("*") if p.is_file()]
    assert [p.name for p in landed] == ["machine-id"], landed
    assert not (boot / "otp-identity" / "ssh").exists(), \
        "the store still has somewhere to put private keys"
    # And the script must not be reading them either, which is the half a
    # listing of the card cannot see. Comments are stripped first: the header
    # names regenerate_ssh_host_keys.service precisely to explain why none of
    # this is here any more.
    code = "\n".join(line for line in IDENTITY_SH.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    for gone in ("ssh_host", "ssh-keygen", "SSH_STORE"):
        assert gone not in code, \
            f"persist-identity.sh still handles SSH host keys: {gone}"


def test_the_unit_that_runs_it_no_longer_orders_itself_around_sshd():
    """
    The orderings that existed only for the host-key half, deleted with it.

    `After=regenerate_ssh_host_keys.service` was there because that unit
    opens with `rm -f /etc/ssh/ssh_host_*_key*` and would have deleted a
    restore placed before it; `Before=ssh.service ssh.socket` named the
    consumers; `ConditionPathIsReadWrite=/etc` covered the restore's writes.
    Nothing here writes to /etc any more and no sshd starts on this image, so
    all three are edges nobody can check -- and an unreachable ordering is
    the shape of thing this repository keeps finding green.
    """
    unit = IDENTITY_UNIT.read_text()
    body = "\n".join(line for line in unit.splitlines()
                     if not line.startswith("#"))
    for gone in ("After=regenerate_ssh_host_keys.service",
                 "Before=ssh.service", "ConditionPathIsReadWrite="):
        assert gone not in body, f"{gone} outlived the half it existed for"


def test_the_unit_still_runs_before_anything_that_reads_the_identity():
    """
    The positive control for the test above: a unit stripped of every
    ordering would satisfy it perfectly and record the machine-id after the
    boot had finished using it.
    """
    unit = IDENTITY_UNIT.read_text()
    for required in ("After=local-fs.target",
                     "Before=sysinit.target",
                     "ExecStart=/opt/otp-unit/persist-identity.sh",
                     "Type=oneshot",
                     "[Install]",
                     "WantedBy=sysinit.target"):
        assert required in unit, (
            f"otp-unit-identity.service no longer says {required!r}, so the "
            f"machine-id is recorded by nothing, late, or never")


def test_a_store_inside_the_overlay_is_refused_rather_than_written(tmp_path):
    """
    The guard that stops the whole script being a no-op nobody notices.
    If /boot/firmware never mounted, $BOOT_DIR is a directory on the
    overlay's tmpfs: every copy agrees with its original, on every boot, and
    nothing survives any of them.
    """
    proc, boot, _ = run_persist_identity(tmp_path, store_src="overlay")
    assert proc.returncode != 0, proc.stdout
    assert not (boot / "otp-identity" / "machine-id").exists(), \
        "it wrote into the overlay anyway"


def test_a_kept_machine_id_is_not_overwritten_by_a_boot_that_lost_it(tmp_path):
    """
    A stored id that does not match the running one means the initramfs
    restore did not happen. The stored value is the one that still has a
    chance of being restored, so replacing it with this boot's random id
    would make the store chase a value that changes every boot.
    """
    other = "ffffffffffffffffffffffffffffffff"
    proc, boot, _ = run_persist_identity(tmp_path, stored_id=other)
    assert proc.returncode != 0, proc.stdout
    assert (boot / "otp-identity" / "machine-id").read_text().strip() == other


# --- the operator's login, which used to last one boot --------------------
#
# WHAT WAS BROKEN. userconf-service applies /boot/firmware/userconf.txt with
# `chpasswd -e` into /etc/shadow and then deletes the seed. /etc is inside
# the overlay and the FAT partition is not, so the credential died with the
# power while the only file that could reapply it was destroyed by the boot
# that consumed it -- and the account reverted to the random FIRST_USER_PASS
# image/build.sh:101 generates when OTP_USER_PASS_HASH is unset, which nobody
# has. The owner's decision out of {refuse the seed loudly, persist it, keep
# the seed file} is "persist it".
#
# WHAT THESE TESTS ARE FOR, and it is not only "the password comes back".
# Every guard below exists because the failure it catches is silent: a store
# that is a tmpfs file agreeing with itself, a truncated line that would make
# the account PASSWORDLESS, a `chpasswd` that exits 0 having written nothing,
# and a hash landing on the card of a unit whose operator never asked for
# one. None of those produces a message on a running machine.


def test_a_seeded_password_is_kept_and_comes_back_after_the_power_cycle(tmp_path):
    """
    The whole loop, and the positive control every negative below leans on.

    Boot 1: the account carries the random build-time hash, the operator's
    seed is applied by the wizard, and the wizard's ExecStartPost records it.
    Boot 2 is a fresh /etc -- which is what the overlay gives you -- and the
    restore has to put the seeded hash back before any login is possible.
    """
    proc, boot, root = run_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    # Nothing is kept until the wizard applies something.
    assert not (boot / "otp-identity" / "credential").exists()

    set_shadow_hash(root, SEEDED_HASH)                     # the wizard applies
    proc = invoke_persist_identity(tmp_path, mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{SEEDED_HASH}\n"

    set_shadow_hash(root, BUILD_TIME_HASH)                 # the power cycle
    proc = invoke_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH, \
        "the operator's password did not survive the power cycle"
    # Through chpasswd, and with the tree the script was pointed at. A restore
    # that edited /etc/shadow by hand would pass the line above and corrupt a
    # real one: chpasswd is what takes the lock and keeps the file consistent.
    assert any(f"-e --root {tmp_path}/root" in line
               for line in chpasswd_argv(tmp_path)), chpasswd_argv(tmp_path)


def test_an_unseeded_boot_puts_no_password_hash_on_the_boot_partition(tmp_path):
    """
    THE EXPOSURE GUARD, and the reason persisting costs no more than the seed
    file it replaces.

    The store is written by --record-credential and by nothing else, and that
    phase runs only as an ExecStartPost on the wizard -- so the only hash that
    ever reaches the card is one the operator's own userconf.txt had already
    put on that same card. A unit whose operator never seeded a credential
    must never have a password hash on its boot partition, and the hash it
    would leak is the worst possible one to leak by accident: pi-gen's random
    FIRST_USER_PASS is the same on no two units, so its appearance on a card
    would be pure loss with nothing gained.

    THE POSITIVE CONTROL IS IN THIS TEST AND ON THIS FIXTURE, because "no hash
    on the card" is satisfied perfectly by a script that writes nothing ever,
    by a --boot-dir that does not exist, and by a chpasswd stub that is never
    reached. The second half runs the recording phase over the identical tree
    and requires the identical hash to land.
    """
    proc, boot, root = run_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    on_the_card = sorted(p.name for p in (boot / "otp-identity").iterdir())
    assert on_the_card == ["machine-id"], on_the_card
    assert BUILD_TIME_HASH not in "".join(
        p.read_text() for p in boot.rglob("*") if p.is_file())

    proc = invoke_persist_identity(tmp_path, mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{BUILD_TIME_HASH}\n", \
        "the recording phase cannot write a hash either, so the absence " \
        "above says nothing about the default phase"


def test_a_truncated_store_does_not_leave_the_account_passwordless(tmp_path):
    """
    `chpasswd -e` writes its second field into /etc/shadow verbatim, and an
    EMPTY second field is an account that logs in with NO PASSWORD -- on an
    appliance that boots to a console with a keyboard and a screen attached.
    A card pulled mid-write is all it takes to produce one.

    The positive control is the same fixture with the line complete: the
    refusal has to be about the truncation and not about the tree.
    """
    proc, _, root = run_persist_identity(tmp_path, credential="otp:\n")
    assert proc.returncode != 0, proc.stdout
    assert shadow_hash(root) == BUILD_TIME_HASH, \
        "the account's password was replaced with the empty string"
    assert chpasswd_argv(tmp_path) == [], \
        "chpasswd was run at all, so only the stub's behaviour saved this"

    whole = f"otp:{SEEDED_HASH}\n"
    proc, _, root = run_persist_identity(tmp_path / "control",
                                         credential=whole)
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH


@pytest.mark.parametrize("kept", ["!", "*", "!!", "$6$short$x", ""])
def test_a_store_that_is_not_a_crypt_string_is_refused(tmp_path, kept):
    """
    The locked forms and the runts, all refused by one clause.

    `!` and `*` are what /etc/shadow holds for an account that cannot log in,
    and a store carrying one is a store that can TAKE AWAY the recovery path
    this whole change exists to provide -- the mirror of the empty field
    above, and just as quiet.
    """
    proc, _, root = run_persist_identity(tmp_path, credential=f"otp:{kept}\n")
    assert proc.returncode != 0, proc.stdout
    assert shadow_hash(root) == BUILD_TIME_HASH
    # And it never says what it refused. This script's stderr is the journal,
    # the journal is forwarded to the tier-3 serial console, and that console
    # is uploaded as a CI artifact.
    assert kept not in proc.stderr or kept == "", proc.stderr


def test_the_store_may_only_name_the_uid_1000_account(tmp_path):
    """
    Whoever can write this store can already write userconf.txt, and this must
    hand them no account the documented path would not. userconf-pi refuses
    `root` outright and RENAMES the UID-1000 user to whatever a seed names, so
    the only account /boot/firmware/userconf.txt can ever set a password on is
    the UID-1000 one. Keying on the uid says exactly that, and says it without
    re-transcribing three validation rules that could drift.
    """
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"root:{SEEDED_HASH}\n")
    assert proc.returncode != 0, proc.stdout
    assert shadow_hash(root, "root") == "!", "root's password was set"
    assert chpasswd_argv(tmp_path) == [], chpasswd_argv(tmp_path)

    # AND THE NAME IT QUOTES BACK IS STRIPPED FIRST. That field comes off a
    # partition anyone with a card reader can write, and this script's stderr
    # is the journal -- forwarded to the serial console under the tier-3
    # harness, which uploads it. An escape sequence there is one `tr` to
    # refuse.
    proc, _, _ = run_persist_identity(
        tmp_path / "escape",
        credential="ro\x1b[2Jot:" + SEEDED_HASH + "\n")
    assert proc.returncode != 0, proc.stdout
    for gone in ("\x1b", "["):
        assert gone not in proc.stderr, repr(proc.stderr)
    # The positive control for the stripping: what is left of the name IS
    # reported, so "no escape in the output" is not satisfied by a note that
    # says nothing about which account the card named. `2J` survives because
    # digits and letters are not escapes -- the ESC byte and the bracket that
    # made them a clear-screen sequence are what had to go.
    assert "'ro2Jot'" in proc.stderr, proc.stderr

    # The positive control, same fixture: the UID-1000 account is `otp` here,
    # and a store naming it is applied. Without this the test above passes on
    # a script that refuses every store there is.
    proc, _, root = run_persist_identity(tmp_path / "control",
                                         credential=f"otp:{SEEDED_HASH}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH


def test_the_uid_1000_account_is_found_by_uid_and_not_by_name(tmp_path):
    """
    The other half of the rule above. `otp` is image/build.sh's
    FIRST_USER_NAME today, and a script that matched that string would do the
    right thing on this image and the wrong thing on a renamed one -- silently
    refusing to restore anything, on the machine whose operator had customised
    it most.
    """
    proc, boot, root = run_persist_identity(
        tmp_path, first_user="keyprinter",
        credential=f"keyprinter:{SEEDED_HASH}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root, "keyprinter") == SEEDED_HASH
    proc = invoke_persist_identity(tmp_path, mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"keyprinter:{SEEDED_HASH}\n"


def test_a_second_line_in_the_store_is_refused_rather_than_applied(tmp_path):
    """
    `chpasswd` reads EVERY line it is handed. A two-line store is a store that
    sets two accounts' passwords, on a partition anyone with a card reader can
    write -- and the first line being perfectly valid is what would make the
    second one arrive unnoticed.
    """
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\nroot:{SEEDED_HASH}\n")
    assert proc.returncode != 0, proc.stdout
    assert chpasswd_argv(tmp_path) == [], chpasswd_argv(tmp_path)
    assert shadow_hash(root) == BUILD_TIME_HASH
    assert shadow_hash(root, "root") == "!"


def test_a_chpasswd_that_exits_zero_without_writing_is_caught(tmp_path):
    """
    The read-back, which is the difference between "the command ran" and "the
    password is back".

    chpasswd's exit status says the former. A chpasswd that is missing from
    the image, that is a stub, or that was pointed at another tree gives a
    boot where the restore reported success and the operator still cannot log
    in -- and on this appliance nobody finds out until they are standing in
    front of it with a keyboard.
    """
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\n",
        chpasswd_env={"CHPASSWD_APPLY": "no"})
    assert proc.returncode != 0, proc.stdout
    assert "does not hold the" in proc.stderr, proc.stderr
    assert shadow_hash(root) == BUILD_TIME_HASH
    # The positive control: the same store, the same fixture, a chpasswd that
    # does its job. Without it this passes on a script that always fails.
    proc, _, root = run_persist_identity(tmp_path / "control",
                                         credential=f"otp:{SEEDED_HASH}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH


def test_a_chpasswd_that_fails_is_not_reported_as_a_restore(tmp_path):
    """The other exit: a chpasswd that says so. `set -e` is not in force in
    this script, so a pipeline whose status nobody read would sail past."""
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\n",
        chpasswd_env={"CHPASSWD_APPLY": "no", "CHPASSWD_RC": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "could not put" in proc.stderr, proc.stderr
    assert shadow_hash(root) == BUILD_TIME_HASH


def test_neither_phase_writes_a_credential_into_the_overlay(tmp_path):
    """
    #35's guard, asked of the phase that did not exist when it was written.

    If /boot/firmware never mounted, $BOOT_DIR falls back to a directory on
    the overlay's tmpfs: the record would write a hash into RAM, the restore
    would read it back happily, and both would agree with each other on every
    boot while nothing survived any of them. The recording phase is a separate
    entry point into this script and has to honour the refusal on its own.
    """
    proc, boot, _ = run_persist_identity(tmp_path, store_src="overlay",
                                         mode="--record-credential")
    assert proc.returncode != 0, proc.stdout
    assert not (boot / "otp-identity" / "credential").exists()
    # The positive control, same fixture and same phase, with the store on the
    # card: two absences are not a refusal.
    proc, boot, _ = run_persist_identity(tmp_path / "control",
                                         mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").exists()


def test_a_locked_account_is_not_recorded_over_a_good_credential(tmp_path):
    """
    The recording phase's own refusal. An account whose /etc/shadow field is
    `!` has no password to keep, and writing that to the store would replace a
    working credential with one the restore must refuse -- costing the
    operator the login on the next power cycle rather than this one.
    """
    kept = f"otp:{SEEDED_HASH}\n"
    proc, boot, _ = run_persist_identity(tmp_path, credential=kept,
                                         live_hash="!",
                                         mode="--record-credential")
    assert proc.returncode != 0, proc.stdout
    assert (boot / "otp-identity" / "credential").read_text() == kept


def test_a_fresh_seed_this_boot_beats_the_credential_that_was_restored(tmp_path):
    """
    THE ORDERING DECISION, run as the sequence it is.

    A boot where the store is restored AND a fresh userconf.txt is on the card
    is a boot where the operator is CHANGING their password. The restore
    happens at sysinit and the wizard applies the new seed over it at
    multi-user, so the new password is in force by the end of that boot -- and
    the wizard's own ExecStartPost then replaces the store, so it is also the
    one the NEXT boot puts back. Precedence: fresh seed, then store, then the
    image's random build-time password.

    Get the last step wrong and the failure is the nastiest kind: the operator
    sets a new password, it works all session, and the next power cycle
    silently reverts them to the old one.
    """
    old = "$6$oldoldold$" + "C" * 86
    proc, boot, root = run_persist_identity(tmp_path, credential=f"otp:{old}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == old, "sysinit did not restore the kept one"

    set_shadow_hash(root, SEEDED_HASH)          # the wizard, at multi-user
    proc = invoke_persist_identity(tmp_path, mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{SEEDED_HASH}\n", "the store still holds the old password"

    set_shadow_hash(root, BUILD_TIME_HASH)      # the power cycle
    proc = invoke_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH, \
        "the next boot reverted the operator to the password they replaced"


def test_the_restore_runs_before_any_login_is_possible():
    """
    The systemd half of the ordering above, which no fake tree can show.

    The restore rides otp-unit-identity.service, which is Before=sysinit.target
    -- and every getty on this machine is pulled in at multi-user, so there is
    no window in which a login prompt exists and the password is still the
    build-time one. Unlike the machine-id, this needs no initramfs: nothing
    reads /etc/shadow before PID 1 has started units, which is the entire
    reason the credential is not a second exception in the overlay script.
    """
    unit = IDENTITY_UNIT.read_text()
    assert "Before=sysinit.target" in unit, unit
    assert "ExecStart=/opt/otp-unit/persist-identity.sh" in unit
    # And the RESTORE is the default phase, so the shipped ExecStart gets it
    # without an argument. A unit that passed --record-credential here would
    # write the build-time hash to the card on every unseeded boot and restore
    # nothing, ever.
    assert "--record-credential" not in unit, unit
    code = IDENTITY_SH.read_text()
    assert "/scripts/overlay" not in code
    assert "shadow" in code, "the script no longer touches the credential"


# --- and the four lines that put it on the machine ------------------------
#
# THE HALF NOTHING WATCHED. Everything above drives persist-identity.sh
# directly, so the script could be perfect while install.sh never shipped it:
# deleting the install block outright left the whole fast suite green, and so
# did gutting the unit -- no [Install] section, no ExecStart worth running.
# An image that installs and enables nothing boots, prints, and loses its
# identity on every power cycle, and the only thing that would have said so
# is a sixteen-minute tier-3 build. Same shape as the probe install above,
# which is watched for the same reason.

IDENTITY_INSTALL_BLOCK = (
    '    install -m 0755 "$REPO_DIR/device/persist-identity.sh" \\',
    "    systemctl enable otp-unit-identity.service")


def run_identity_install(tmp_path, *, repo_dir=None):
    """Run install.sh's identity-install block against a substitute REPO_DIR.

    `install` is shadowed by a shell function rather than stubbed on PATH,
    for the reason run_probe_install shadows it: the block writes into
    /etc/systemd/system and /opt/otp-unit, and a test has no business
    touching either.
    """
    block = slice_between(INSTALL.read_text(), *IDENTITY_INSTALL_BLOCK)
    return run_block(block, tmp_path,
                     preamble=("install() { printf 'INSTALL %s\\n' \"$*\"; }\n"
                               f'REPO_DIR="{repo_dir or REPO}"\n'
                               f'PREFIX="{tmp_path}/opt"'))


def test_the_identity_script_and_its_unit_are_both_installed(tmp_path):
    """
    Both files, and to the paths the unit and the harness name. The unit's
    ExecStart is an absolute /opt/otp-unit/persist-identity.sh, so a script
    installed anywhere else is a unit that fails to start.
    """
    proc = run_identity_install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    installed = [ln for ln in proc.stdout.splitlines() if ln.startswith("INSTALL")]
    assert any("device/persist-identity.sh" in ln
               and f"{tmp_path}/opt/persist-identity.sh" in ln
               for ln in installed), installed
    assert any("otp-unit-identity.service" in ln
               and "/etc/systemd/system/otp-unit-identity.service" in ln
               for ln in installed), installed


def test_the_identity_unit_is_enabled_and_not_merely_dropped_in_place(tmp_path):
    """
    A unit file in /etc/systemd/system with no symlink pointing at it is a
    unit systemd never runs. Nothing else on this image enables it -- and the
    accident that used to, first-boot `preset-all`, is precisely what this
    unit exists to end, so it cannot be relied on to enable its own cure.
    """
    proc = run_identity_install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = systemctl_calls(tmp_path)
    assert "enable otp-unit-identity.service" in calls, calls
    # And after the unit file lands, or systemd enables a file it has not
    # read: `enable` resolves [Install] out of the unit on disk.
    assert "daemon-reload" in calls, calls
    assert calls.index("daemon-reload") < calls.index(
        "enable otp-unit-identity.service"), calls


# --- and the one line that makes the credential ever get recorded ---------

CREDENTIAL_DROPIN_BLOCK = (
    "    install -d /etc/systemd/system/userconfig.service.d",
    "--record-credential\nCRED")
CREDENTIAL_DROPIN_DIR = "/etc/systemd/system/userconfig.service.d"


def run_credential_dropin(tmp_path):
    """Write the shipped credential drop-in into a substituted unit directory.

    Rewritten rather than stubbed, for the reason run_dropin gives: a test has
    no business writing into the machine's own /etc/systemd/system, and
    everything that decides what the file SAYS stays the shipped bytes.
    """
    etc = tmp_path / "etc"
    block = slice_between(INSTALL.read_text(), *CREDENTIAL_DROPIN_BLOCK)
    assert block.count(CREDENTIAL_DROPIN_DIR) == 2, block
    block = block.replace(CREDENTIAL_DROPIN_DIR, f"{etc}/userconfig.service.d")
    proc = run_block(block, tmp_path)
    assert proc.returncode == 0, proc.stderr
    return (etc / "userconfig.service.d" / "otp-credential.conf").read_text()


def test_the_wizard_records_the_credential_it_just_applied(tmp_path):
    """
    The other half of "persist it", and the half with no other trigger.

    Without this line the restore has nothing to restore, forever: the store
    is written by --record-credential and by nothing else. The image would
    then boot, apply an operator's seed, delete it, and lose the password at
    the power cycle exactly as before -- with a persistence mechanism
    installed, enabled, and reporting success every boot.
    """
    text = run_credential_dropin(tmp_path)
    assert "ExecStartPost=" in text, text
    assert "/opt/otp-unit/persist-identity.sh --record-credential" in text, text
    # The path the identity install above really writes the script to. An
    # ExecStartPost naming anything else is a line systemd cannot run -- and
    # with the `-` below, cannot complain about either.
    assert "ExecStartPost=-/opt/otp-unit/" in text, text


def test_a_record_that_fails_does_not_cost_the_operator_the_apply(tmp_path):
    """
    The leading `-`, stated as a property rather than left to be noticed.

    Stock userconfig.service carries Restart=on-failure. Without the `-` a
    full boot partition -- or any other reason the record fails -- turns into
    a restart loop printing `Failed with result` and `Scheduled restart job`,
    two of the strings harness/img-boot.sh fails a release on, on the one boot
    that was applying the operator's password successfully.
    """
    text = run_credential_dropin(tmp_path)
    assert "ExecStartPost=-" in text, text


def test_the_credential_is_recorded_only_on_an_overlay_machine():
    """
    The drop-in is inside install.sh's `if` on cmdline.txt, next to the unit
    and the script it names -- not beside the wizard drop-in written on every
    machine this script provisions.

    A writable root keeps /etc/shadow by itself and has nothing to persist,
    and persist-identity.sh is not installed there at all: an ExecStartPost
    pointing at a script that does not exist is what the tier-2 Debian guest
    would get, on a unit whose userconfig.service would then be carrying a
    command it can never run.
    """
    text = INSTALL.read_text()
    dropin = text.index("otp-credential.conf")
    identity = text.index('install -m 0755 "$REPO_DIR/device/persist-identity.sh"')
    overlay_branch = text.index('log "Enabling the read-only root overlay"')
    # After the branch opens, and after the script it names is installed.
    assert overlay_branch < identity < dropin, (overlay_branch, identity, dropin)
    # And before the branch closes. The `fi` that ends it is the one directly
    # ahead of the summary this script prints for the operator.
    assert dropin < text.index("# WHAT THIS SCRIPT DID TO SOMEONE ELSE'S MACHINE")


# --- ssh.socket, which turned a documented reload into a dead sshd --------

SSH_SOCKET_BLOCK = ("# --- AND THE RELOAD AT THE END OF THAT SAME SCRIPT",
                    "systemctl mask ssh.socket")


def test_the_socket_that_kept_sshd_from_rebinding_is_masked(tmp_path):
    """
    cancel-rename ends a seeded first boot with `systemctl --quiet reload
    ssh`. With ssh.socket holding :22, sshd runs on an inherited descriptor,
    its SIGHUP re-exec has nothing left to adopt, and RestartPreventExitStatus
    =255 keeps it down for the rest of the boot -- run 72, boot 1.

    MASKED rather than disabled, and that is the half worth asserting: the
    enable symlinks live in /etc/systemd/system, which is inside the overlay,
    so a disable lasts one boot and the next first-boot preset-all puts the
    socket straight back.
    """
    block = slice_between(INSTALL.read_text(), *SSH_SOCKET_BLOCK)
    proc = run_block(block, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "mask ssh.socket" in systemctl_calls(tmp_path), \
        systemctl_calls(tmp_path)


def test_ssh_service_itself_is_left_alone(tmp_path):
    """
    Masking the SERVICE would be the other way to stop the reload -- the
    guard in cancel-rename is `systemctl --quiet is-active ssh` -- and it is
    refused. It leaves ssh.socket listening on :22 for a service that can
    never start, and takes SSH off a machine as a side effect of a bug fix.
    """
    block = slice_between(INSTALL.read_text(), *SSH_SOCKET_BLOCK)
    run_block(block, tmp_path)
    for call in systemctl_calls(tmp_path):
        assert call != "mask ssh.service", call
        assert call != "mask ssh", call


# --- the harness end ------------------------------------------------------

def test_the_harness_copies_the_overlay_token_and_never_invents_one(tmp_path):
    """
    Adding boot=overlay to -append when the image's own cmdline.txt does not
    carry it would make tier 3 prove its own command line rather than the
    artifact. An image built without the overlay has to boot without it here
    too, and fail.
    """
    block = slice_between(IMG_BOOT.read_text(), 'OVERLAY_TOKENS=""', "esac")
    for cmdline, expected in (
        ("boot=overlay console=tty1 root=PARTUUID=1a2b-02", "boot=overlay"),
        ("console=tty1 root=PARTUUID=1a2b-02", ""),
        # Not a substring match: a token that merely contains the word is
        # not the token.
        ("console=tty1 noboot=overlayfs", ""),
    ):
        runner = tmp_path / "tokens.sh"
        runner.write_text(f'IMG_CMDLINE="{cmdline}"\n{block}\nprintf "%s" "$OVERLAY_TOKENS"\n')
        proc = subprocess.run(["bash", str(runner)], capture_output=True,
                              text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == expected, f"{cmdline!r} -> {proc.stdout!r}"


REVISION_BLOCK = ("# --- the board revision the firmware would have supplied",
                  'log "board revision $BOARD_REVISION synthesised into $DTB '
                  '(the firmware\'s job)"')

# fdtput/fdtget come from device-tree-compiler, which the image workflow
# installs next to qemu and mtools and the fast suite does not have. Stubbed
# so this runs anywhere: the "blob" is a text file and the property is a line
# in it, which is enough for every branch under test.
FDTPUT_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$FDT_LOG"
[ -z "${FDTPUT_DEAF-}" ] || exit 0
# fdtput -c <blob> <node>, and fdtput -t x <blob> <node> <prop> <value>.
case "$1" in
    -c) exit 0 ;;
    -t) printf '%s=%s\\n' "$5" "$6" >> "$3" ; exit 0 ;;
esac
exit 1
"""
# Shell builtins only: PATH below is cut down to the tools the block itself
# uses, and reaching for sed here would make the stub depend on something the
# code under test does not.
FDTGET_STUB = """#!/bin/sh
# fdtget <blob> <node> <prop>, printing the value as a decimal integer.
value=""
while IFS= read -r line; do
    case "$line" in "$3="*) value="${line#*=}" ;; esac
done < "$1"
[ -n "$value" ] || exit 1
printf '%d\\n' "$((value))"
"""


def run_revision_block(tmp_path, *, have_fdtput=True, fdtput_deaf=False):
    """Run the shipped board-revision block over a stand-in for the DTB."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    work = tmp_path / "work"
    (work / "boot").mkdir(parents=True, exist_ok=True)
    dtb = work / "boot" / "bcm2710-rpi-3-b.dtb"
    dtb.write_text("")
    # A PATH with nothing on it but the tools the block uses, so
    # `have_fdtput=False` really means absent -- on a developer's machine the
    # real device-tree-compiler is usually installed, and inheriting PATH
    # would make that test pass for the wrong reason there and fail in CI.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("cp", "basename"):
        real = shutil.which(tool)
        assert real, f"{tool} is not on PATH, so this test cannot run"
        (bin_dir / tool).symlink_to(real)
    if have_fdtput:
        for name, body in (("fdtput", FDTPUT_STUB), ("fdtget", FDTGET_STUB)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
    runner = tmp_path / "revision.sh"
    runner.write_text(
        "set -euo pipefail\n"
        "log() { printf 'LOG %s\\n' \"$*\"; }\n"
        f'WORK="{work}"\nDTB="{dtb}"\n'
        + slice_between(IMG_BOOT.read_text(), *REVISION_BLOCK) + "\n"
        'printf "DTB=%s\\n" "$DTB"\n')
    # PATH without the real device-tree-compiler even when the host has one,
    # so `have_fdtput=False` really means absent.
    proc = subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(runner)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "FDT_LOG": str(tmp_path / "fdt.log"),
             **({"FDTPUT_DEAF": "1"} if fdtput_deaf else {}),
             "PATH": str(bin_dir)})
    return proc, tmp_path / "fdt.log"


def test_the_harness_supplies_the_board_revision_the_firmware_would_have(tmp_path):
    """
    QEMU is not the Pi firmware, so no /system/linux,revision reaches the
    guest and rpi-eeprom-update.service dies on `(0x >> 23) & 1`. Measured on
    a stock card under -M raspi3b: absent without this, 00a02082 with it, and
    the service goes rc=2 to rc=0.
    """
    proc, log = run_revision_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert "linux,revision 0xa02082" in calls, calls
    # The image's own DTB is left alone, so the evidence can still show what
    # a device would have booted.
    assert "otp-harness-" in proc.stdout, proc.stdout


def test_a_revision_that_did_not_reach_the_blob_stops_the_boot(tmp_path):
    """
    A write that did nothing looks exactly like one that worked from here,
    and the cost of not noticing is a red release gate blamed on the image
    rather than on the emulator. So the property is read back.
    """
    proc, _ = run_revision_block(tmp_path, fdtput_deaf=True)
    assert proc.returncode != 0
    assert "linux,revision" in proc.stderr, proc.stderr


def test_no_device_tree_compiler_stops_the_boot_rather_than_booting_without_it(tmp_path):
    """
    Booting without the property is booting the configuration this exists to
    fix. A hard requirement, like mcopy: the failure has to name the missing
    package rather than arrive half an hour later as a failed unit.
    """
    proc, _ = run_revision_block(tmp_path, have_fdtput=False)
    assert proc.returncode != 0
    assert "device-tree-compiler" in proc.stderr, proc.stderr


def test_the_synthesised_revision_is_the_board_the_emulator_models():
    """
    A code for hardware `-M raspi3b` does not model is the run-1 DTB mistake
    in a smaller font -- and gpiozero builds its whole board description from
    these bits. Decoded against the scheme rpi-eeprom-update itself reads.
    """
    text = IMG_BOOT.read_text()
    code = int(shell_assignment(text, "BOARD_REVISION"), 16)
    assert (code >> 23) & 1, "new-style flag clear: rpi-eeprom-update would " \
                             "call this a pre-2016 board and skip"
    assert (code >> 12) & 0xF == 2, "processor is not BCM2837"
    assert (code >> 4) & 0xFF == 0x08, "board type is not 3 Model B"
    # 1GB, in the same units `-m 1024` gives the guest. A revision claiming
    # more memory than the emulator provides is a description of a machine
    # that is not there.
    assert 256 * (2 ** ((code >> 20) & 7)) == 1024
    assert "-M raspi3b -m 1024" in text, \
        "the emulated machine moved; the revision above describes the old one"


def shell_assignment(text: str, name: str) -> str:
    """A plain NAME=value assignment, read out of a shipped script."""
    match = re.search(rf"^{name}=(\S+)$", text, re.M)
    assert match, f"{name} is gone"
    return match.group(1)


def test_the_harness_hands_the_initramfs_to_qemu():
    # Without -initrd the kernel boots with no initramfs, boot=overlay is
    # read by nothing, and every overlay check would fail for a reason that
    # is the harness's fault rather than the image's.
    assert '${INITRD:+-initrd "$INITRD"}' in IMG_BOOT.read_text()


def test_the_guest_probe_cannot_run_on_a_flashed_unit():
    """
    The probe ships in the image, so what keeps it off a real appliance is
    this one condition. systemd matches a bare word against both the word
    and the left hand side of an assignment, so otp.imgcheck=boot1 starts
    it and a device command line that says nothing about it does not.
    """
    unit = IMGCHECK_UNIT.read_text()
    assert "ConditionKernelCommandLine=otp.imgcheck" in unit
    # And the harness is the only thing that supplies it.
    assert "otp.imgcheck=$phase" in IMG_BOOT.read_text()


# --- the build refuses to ship an image it cannot probe ------------------

PROBE_INSTALL_BLOCK = ('if [ -f "$REPO_DIR/harness/img-guest-check.sh" ]; then',
                       'would have nothing to report with."\nfi')

STAGE_RUN = REPO / "image" / "stage-otpunit" / "01-otpunit" / "00-run.sh"
STAGE_COPY_BLOCK = ("for item in otpunit codewords device harness",
                    'Run image/render-manual.sh before building." >&2\nfi')


def run_probe_install(tmp_path, *, repo_dir, image_build):
    """Run install.sh's probe-install block against a substitute REPO_DIR.

    `install` is shadowed by a shell function rather than stubbed on PATH:
    the block installs into /etc/systemd/system and /opt/otp-unit, and a
    test has no business writing to either.
    """
    block = slice_between(INSTALL.read_text(), *PROBE_INSTALL_BLOCK)
    return run_block(block, tmp_path,
                     preamble=("install() { printf 'INSTALL %s\\n' \"$*\"; }\n"
                               f'REPO_DIR="{repo_dir}"\n'
                               f'PREFIX="{tmp_path}/opt"\n'
                               f"IMAGE_BUILD={image_build}"))


def test_an_image_build_without_the_probe_fails(tmp_path):
    """
    Every other overlay precondition exits 1 under --image-build; this one
    printed a NOTE and carried on, so a build could ship an image whose
    overlay nothing can observe. Deleting the install line survived the
    whole fast suite, and the miss only appeared after a pi-gen build and
    two emulated boots -- as a guest that never reported, which reads as a
    boot failure rather than a packaging one.
    """
    empty = tmp_path / "repo"
    (empty / "harness").mkdir(parents=True)
    proc = run_probe_install(tmp_path, repo_dir=empty, image_build=1)
    assert proc.returncode != 0, proc.stdout
    assert "nothing to report on the" in proc.stderr, proc.stderr


def test_a_hand_provisioned_machine_without_the_probe_only_says_so(tmp_path):
    # Not an image build: someone running install.sh from a partial copy on
    # a Pi gets a working unit and a note, which is what they had before.
    empty = tmp_path / "repo"
    (empty / "harness").mkdir(parents=True)
    proc = run_probe_install(tmp_path, repo_dir=empty, image_build=0)
    assert proc.returncode == 0, proc.stderr
    assert "NOTE" in proc.stdout, proc.stdout


def test_the_probe_is_installed_when_it_is_there(tmp_path):
    # The positive control, against the real repository.
    proc = run_probe_install(tmp_path, repo_dir=REPO, image_build=1)
    assert proc.returncode == 0, proc.stderr
    assert "img-guest-check.sh" in proc.stdout, proc.stdout
    assert "otp-unit-imgcheck.service" in proc.stdout, proc.stdout


def run_stage_copy(tmp_path, *, present):
    """Run the pi-gen stage's copy loop over a synthetic repository."""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for item in present:
        if item.endswith(".py") or item.endswith(".md"):
            (src / item).write_text("x")
        else:
            (src / item).mkdir(exist_ok=True)
    rootfs = tmp_path / "rootfs"
    (rootfs / "tmp" / "otp-src").mkdir(parents=True, exist_ok=True)
    block = slice_between(STAGE_RUN.read_text(), *STAGE_COPY_BLOCK)
    runner = tmp_path / "stage.sh"
    runner.write_text(
        f'set -e\nREPO_SRC="{src}"\nROOTFS_DIR="{rootfs}"\n'
        'STAGING=/tmp/otp-src\n' + block)
    return subprocess.run(["bash", str(runner)], capture_output=True,
                          text=True, timeout=60)


ALL_ITEMS = ("otpunit", "codewords", "device", "harness",
             "otp_generator.py", "otp.md", "assets")


def test_the_stage_copies_everything_install_sh_reads(tmp_path):
    proc = run_stage_copy(tmp_path, present=ALL_ITEMS)
    assert proc.returncode == 0, proc.stderr
    for item in ALL_ITEMS:
        assert (tmp_path / "rootfs" / "tmp" / "otp-src" / item).exists(), item


def test_a_missing_item_stops_the_stage_instead_of_being_skipped(tmp_path):
    """
    The copy was `if [ -e ]`, so dropping a name from the list -- or from
    the repository -- was a silent no-op. `harness` is the one that did not
    even fail in the chroot: install.sh printed a NOTE and built an image
    whose overlay nothing could probe.
    """
    for missing in ALL_ITEMS:
        if missing == "assets":
            continue
        present = [i for i in ALL_ITEMS if i != missing]
        proc = run_stage_copy(tmp_path / missing, present=present)
        assert proc.returncode != 0, f"{missing} missing was accepted"
        assert missing in proc.stderr, proc.stderr


def test_the_assets_directory_stays_optional(tmp_path):
    # install.sh guards its own use of the manual PDFs, and an image
    # without them boots and prints pads. The warning is the whole penalty.
    present = [i for i in ALL_ITEMS if i != "assets"]
    proc = run_stage_copy(tmp_path, present=present)
    assert proc.returncode == 0, proc.stderr
    assert "manual PDFs missing" in proc.stderr, proc.stderr


# --- the probe refuses to run on anything but a harness boot -------------

PHASE_GUARD_BLOCK = ('PHASE=""', "\nesac")

# What a provisioned unit actually boots with: install.sh prepends
# boot=overlay and strips `resize`, and nothing anywhere adds otp.imgcheck.
DEVICE_CMDLINE = ("boot=overlay console=serial0,115200 console=tty1 "
                  "root=PARTUUID=1a2b3c4d-02 rootfstype=ext4 fsck.repair=yes "
                  "rootwait\n")


def run_phase_guard(tmp_path, cmdline, *, cwd=None):
    """Run the shipped phase parse and guard against a substituted cmdline.

    The path is rewritten rather than the file stubbed, so the test still
    describes the shipped bytes: everything else in the block -- the `set
    -f`, the token match, the case -- is the code that ships.
    """
    block = slice_between(GUEST_CHECK.read_text(), *PHASE_GUARD_BLOCK)
    assert block.count("/proc/cmdline") == 1, block
    fake = tmp_path / "cmdline"
    fake.write_text(cmdline)
    runner = tmp_path / "phase.sh"
    runner.write_text(
        "set -uo pipefail\n"
        "BOOTDIR=/boot/firmware\n"
        + block.replace("/proc/cmdline", str(fake))
        + '\nprintf "REACHED-THE-CHECKS phase=%s\\n" "$PHASE"\n')
    return subprocess.run(["bash", str(runner)], capture_output=True,
                          text=True, timeout=30, cwd=str(cwd or tmp_path))


def test_the_probe_is_inert_on_a_command_line_without_the_token(tmp_path):
    """
    The HIGH one. This script ships 0755 in /opt/otp-unit on every unit and
    its service is enabled unconditionally, and what it does is WRITE: a
    sentinel to /, and pages=137 through config.save() into
    /boot/firmware/otp-unit.conf, which is outside the overlay and therefore
    permanent. The version before this guard read no token, set
    PHASE=unknown and ran every check anyway -- run against a realistic
    device command line it took an operator's page count from 500 to 137.
    """
    proc = run_phase_guard(tmp_path, DEVICE_CMDLINE)
    assert proc.returncode != 0, proc.stdout
    assert "REACHED-THE-CHECKS" not in proc.stdout, proc.stdout
    assert "refusing to run" in proc.stderr, proc.stderr


def test_the_probe_runs_for_the_two_phases_the_harness_supplies(tmp_path):
    # The positive control: everything else here asserts a refusal, and a
    # script that refused everything would satisfy all of them.
    for phase in ("boot1", "boot2"):
        proc = run_phase_guard(
            tmp_path, f"{DEVICE_CMDLINE.strip()} otp.imgcheck={phase}\n")
        assert proc.returncode == 0, proc.stderr
        assert f"REACHED-THE-CHECKS phase={phase}" in proc.stdout, proc.stdout


def test_a_phase_that_is_not_one_of_the_two_is_refused(tmp_path):
    # `otp.imgcheck` bare is the interesting one: systemd's
    # ConditionKernelCommandLine matches a bare word as well as an
    # assignment, so that token starts the unit and names no phase.
    for token in ("otp.imgcheck", "otp.imgcheck=", "otp.imgcheck=boot3",
                  "otp.imgcheck=unknown"):
        proc = run_phase_guard(tmp_path, f"{DEVICE_CMDLINE.strip()} {token}\n")
        assert proc.returncode != 0, token
        assert "REACHED-THE-CHECKS" not in proc.stdout, token


def test_the_command_line_is_split_but_not_glob_expanded(tmp_path):
    """
    The unit runs as root with a working directory of /, and the token loop
    reads an unquoted command substitution. With pathname expansion left on,
    a bare `*` in the command line is replaced by the listing of the current
    directory before the case sees it -- so a FILE whose name begins
    otp.imgcheck= chooses the phase, and the probe starts writing.
    """
    root = tmp_path / "fake-root"
    root.mkdir()
    (root / "otp.imgcheck=boot2").write_text("")
    (root / "vmlinuz").write_text("")
    proc = run_phase_guard(tmp_path, "console=tty1 * rootwait\n", cwd=root)
    assert proc.returncode != 0, proc.stdout
    assert "REACHED-THE-CHECKS" not in proc.stdout, proc.stdout
    assert "vmlinuz" not in proc.stderr, proc.stderr


def test_the_refusal_comes_before_anything_the_probe_writes():
    # Position, not behaviour: a guard that refuses AFTER the first write
    # has already rewritten the operator's settings.
    text = GUEST_CHECK.read_text()
    guard = text.index('case "$PHASE" in')
    for later, what in (("check root-is-overlay", "the first check"),
                        ('> "$SENTINEL"', "the sentinel write"),
                        ("config.save(saved)", "the config.save() into /boot")):
        assert guard < text.index(later), what


# --- the drop-in that keeps the first-boot wizard usable ------------------

# The end anchor is the heredoc's LAST line, not the delimiter's first
# appearance: `slice_between` takes the first match, and "DROPIN\n" first
# appears on the `cat <<DROPIN` line itself -- which cut the body off and
# wrote an empty drop-in that these tests then passed judgement on.
DROPIN_BLOCK = ("systemctl unmask userconfig.service",
                "systemctl enable userconfig.service 2>/dev/null || true")
DROPIN_DIR = "/etc/systemd/system/userconfig.service.d"


def run_dropin(tmp_path, *, boot_dir):
    """Write the shipped drop-in into a substituted /etc/systemd/system.

    The unit directory is rewritten rather than stubbed: a test has no
    business writing into the machine's real /etc/systemd/system, and
    everything that decides what the file SAYS -- the two conditions, the
    directory they name, StandardInput -- stays the shipped bytes.
    """
    etc = tmp_path / "etc"
    block = slice_between(INSTALL.read_text(), *DROPIN_BLOCK)
    assert block.count(DROPIN_DIR) == 2, block
    block = block.replace(DROPIN_DIR, f"{etc}/userconfig.service.d")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nBOOT_DIR="{boot_dir}"\n{block}'],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    written = etc / "userconfig.service.d" / "otp-appliance.conf"
    return written.read_text(), (tmp_path / "systemctl.log").read_text()


def test_the_wizard_runs_only_when_an_operator_seeded_it(tmp_path):
    """
    Both spellings, both as `|=`, or the unit is worse than before.

    A single ConditionPathExists is an AND with itself and would demand BOTH
    files; the pair with `|` is an OR group, which is what makes "either
    spelling of the seed" work. And the file has to be one an operator
    actually writes -- userconf-pi reads `userconf` first and `userconf.txt`
    second, and Raspberry Pi Imager writes the latter.
    """
    text, log = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "ConditionPathExists=|/boot/firmware/userconf\n" in text, text
    assert "ConditionPathExists=|/boot/firmware/userconf.txt\n" in text, text
    # Not masked. Masking is the fix that was tried and reverted: it stops the
    # prompt and silently discards every credential file too.
    assert "unmask userconfig.service" in log, log
    assert "mask userconfig.service" not in log.replace("unmask", ""), log


def test_the_wizard_is_enabled_in_the_image_and_not_by_accident(tmp_path):
    """
    Run 32020772161, boot 2, the first boot pair with the machine-id
    persisted:

      OTP-CHECK boot2 userconf-unseeded-boot-skips-the-wizard FAIL
        condition=no checked-at='never' is-active=inactive is-enabled=disabled

    `is-enabled=disabled` on a boot where nothing disabled anything -- /etc is
    the overlay, so boot1's `systemctl disable userconfig` died with the
    power. That is the IMAGE's state, and it always was: what had been hiding
    it is that every boot used to be a first boot, so `preset-all` re-enabled
    the unit every time. The same run shows the change directly, in the
    targets it reached: boot1 has first-boot-complete.target, boot2 does not.

    userconfig.service is the ONLY consumer of the documented headless
    credential file, so leaving it disabled means a userconf.txt an operator
    writes to the card is ignored in silence on every boot after the first --
    which is exactly the trade the drop-in above replaced a `mask` to avoid.
    Enabling it is what that drop-in was written for: the condition pair costs
    an unseeded boot two ConditionPathExists and a skip.
    """
    _, log = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "enable userconfig.service" in log, log


def test_the_wizard_cannot_reach_a_terminal(tmp_path):
    # Without this a malformed seed falls back to the interactive prompt and
    # holds multi-user.target open on a machine with no visible tty.
    text, _ = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "StandardInput=null" in text, text


def test_the_condition_follows_the_boot_directory_that_exists(tmp_path):
    """
    install.sh falls back to BOOT_DIR=/boot when /boot/firmware is not a
    directory, and userconf-service's own get_fw_loc makes the same choice.
    Hardcoded, the pair named a path that cannot exist on that layout: the
    condition is false on every boot, the unit never runs, and the operator's
    credentials are ignored in silence -- the exact outcome masking the unit
    produced, arriving by a different route.
    """
    text, _ = run_dropin(tmp_path, boot_dir="/boot")
    assert "ConditionPathExists=|/boot/userconf\n" in text, text
    assert "ConditionPathExists=|/boot/userconf.txt\n" in text, text
    assert "/boot/firmware" not in text, text


# --- the two things that kept the wizard from ever running ----------------

# Run 31968966879 is the first run that seeded a real userconf.txt, and it
# found that the drop-in above had never been the deciding factor: the unit's
# start job never came up for execution at all. It was ordered behind
# cloud-init's config stage, which waits on network-online.target, which waits
# on systemd-networkd-wait-online.service -- TimeoutStartSec=infinity on a box
# whose links NetworkManager owns. Both boots ended with that job still
# running and multi-user.target never reached.

BOOT_FINISHES_BLOCK = ("systemctl mask systemd-networkd-wait-online.service",
                       ": > /etc/cloud/cloud-init.disabled")


def run_boot_finishes(tmp_path):
    """Run the shipped block that clears the two blockers, into a fake /etc.

    /etc/cloud is rewritten to a temporary directory; nothing else is, so
    the unit name masked and the kill switch's own filename stay the bytes
    that ship.
    """
    cloud = tmp_path / "cloud"
    block = slice_between(INSTALL.read_text(), *BOOT_FINISHES_BLOCK)
    assert block.count("/etc/cloud") == 2, block
    block = block.replace("/etc/cloud", str(cloud))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{block}"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    return (tmp_path / "systemctl.log").read_text(), cloud


def test_the_unbounded_network_wait_is_masked(tmp_path):
    """
    The job both boots of run 31968966879 ended on, in the console's own
    words: `systemd-networkd-wait-online.service/start running (2min 37s /
    no limit)`. "No limit" is the unit's TimeoutStartSec=infinity, and this
    appliance has no networkd configuration for it to ever be satisfied by.
    Everything ordered after network-online.target -- cloud-init's config
    stage, and through it the credential wizard -- waits for the life of
    the boot.
    """
    log, _ = run_boot_finishes(tmp_path)
    assert "mask systemd-networkd-wait-online.service" in log, log


def test_cloud_init_is_switched_off_on_an_air_gapped_printer(tmp_path):
    """
    The other end of the same chain, and worth doing on its own account.

    cloud-init is what pulls network-online.target into the transaction
    here; it spent 57 seconds of every boot finding no datasource; and it
    is a provisioning agent that takes user-data off the boot partition --
    the one partition an operator is told to write files on -- on a device
    that prints one-time pads and has no network by design. Its generator
    reads this file before it links cloud-init.target into
    multi-user.target, so cloud-config.service is not in the transaction at
    all and userconfig.service's `After=` on it becomes void.
    """
    _, cloud = run_boot_finishes(tmp_path)
    assert (cloud / "cloud-init.disabled").exists(), sorted(
        p.name for p in cloud.iterdir()) if cloud.exists() else "no /etc/cloud"


def test_the_closing_summary_names_what_was_done_to_the_whole_machine():
    """
    The documented path is "run this on a Pi you already have", and four of
    the steps above are not confined to the unit: three shared systemd units
    are MASKED and cloud-init is switched off permanently. All four outlive
    this script, all four change how software installed later behaves, and
    none of them was in the summary the operator reads at the end -- which
    listed the overlay and stopped, reading as though nothing else had been
    touched.

    EVERY MASK THIS SCRIPT ISSUES, and that is what the list is derived from
    rather than restated. The summary said "three machine-wide changes" while
    the script made four: systemd-growfs-root.service was masked in the same
    round that added the ssh.socket mask and never reached the paragraph an
    operator reads, so the one persistent change with no undo instruction was
    the newest one. A hand-written list is a list that goes stale exactly
    that way.

    The summary is what someone gets instead of a diff. Undo instructions
    are required with them: a machine-wide change nobody can find again is
    the part that hurts, six months later, when networkd's wait-online is
    the thing that mysteriously never blocks.
    """
    install = INSTALL.read_text()
    summary = install[install.index('log "Done. Reboot to start the unit."'):]
    body = install[:install.index('log "Done. Reboot to start the unit."')]
    # Indentation allowed: the growfs mask is inside the overlay branch, and
    # a pattern anchored at column one is exactly how it went unlisted.
    masked = sorted(set(re.findall(r"^[ \t]*systemctl mask (\S+)$", body, re.M)))
    assert len(masked) == 3, masked
    for unit in masked:
        assert unit in summary, (
            f"install.sh masks {unit} on any machine it touches and the "
            f"closing summary does not mention it")
        assert f"unmask {unit}" in summary, (
            f"the summary names a permanent change without telling the "
            f"operator how to reverse it: unmask {unit}")
    assert "cloud-init" in summary, (
        "install.sh switches cloud-init off permanently and the closing "
        "summary does not mention it")
    assert "/etc/cloud/cloud-init.disabled" in summary, (
        "the summary names a permanent change without telling the operator "
        "how to reverse it: /etc/cloud/cloud-init.disabled")
    # And the count in the prose, which is the half a list of names cannot
    # keep honest -- it said "Three" while naming three of four.
    assert "Four machine-wide changes" in summary, summary


GETTY_BLOCK = ("install -d /etc/systemd/system/getty@tty1.service.d",
               "systemctl stop getty@tty1.service 2>/dev/null || true")
GETTY_DIR = "/etc/systemd/system/getty@tty1.service.d"


def run_getty_dropin(tmp_path):
    """Write the shipped getty drop-in into a substituted unit directory."""
    etc = tmp_path / "etc"
    block = slice_between(INSTALL.read_text(), *GETTY_BLOCK)
    assert block.count(GETTY_DIR) == 2, block
    block = block.replace(GETTY_DIR, f"{etc}/getty@tty1.service.d")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{block}"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    written = etc / "getty@tty1.service.d" / "otp-appliance.conf"
    log = tmp_path / "systemctl.log"
    return written.read_text(), (log.read_text() if log.exists() else "")


def test_no_login_prompt_can_start_on_the_front_panels_tty(tmp_path):
    """
    tty1 is the front panel, and the thing that starts a getty on it is the
    credential path itself: /usr/lib/userconf-pi/userconf ends in
    cancel-rename, which ends in `systemctl --no-block start getty@tty1` on
    every console-boot machine. A condition, evaluated at start time, is
    what keeps that from ever producing a prompt.

    THE SPELLING IS THE PROPERTY here, which is why this reads the exact
    directives rather than "a condition mentioning otp-unit". The form that
    shipped first --
    `ConditionPathExists=!/etc/systemd/system/otp-unit.service` -- keys on
    the unit FILE, and `systemctl mask` writes a symlink to /dev/null at
    exactly that path while `systemctl disable` leaves the file alone. Both
    therefore left the condition false and tty1 with no getty at all, on a
    machine whose panel was not going to run either. The truth table is in
    the test below; this is the always-runs half of it.
    """
    text, _ = run_getty_dropin(tmp_path)
    assert ("ConditionPathExists=|!/etc/systemd/system/multi-user.target"
            ".wants/otp-unit.service" in text), text
    assert ("ConditionFileNotEmpty=|!/etc/systemd/system/otp-unit.service"
            in text), text
    # The `|` on both is what makes them an OR. Un-prefixed, systemd ANDs
    # them and the prompt comes back only on a panel that is disabled AND
    # masked -- neither of the two states this exists for.
    assert "\nConditionPathExists=!" not in text, (
        "an un-prefixed condition is ANDed with the other one: " + text)
    assert text.lstrip().startswith("[Unit]"), text


GETTY_STATES = ("healthy", "masked", "disabled", "absent")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="no systemd-analyze here to evaluate the "
                           "conditions with; the text assertions above "
                           "still run")
@pytest.mark.parametrize("state", GETTY_STATES)
def test_masking_or_disabling_the_panel_gives_tty1_its_login_back(
        tmp_path, state):
    """
    The four states a machine can be in, evaluated by real systemd.

    Only the healthy one may skip the getty. On the other three the panel is
    not going to take tty1, and a tty with neither a panel nor a login is a
    device an operator cannot reach -- the exact half-provisioned case the
    drop-in's own comment says the prompt exists for. The shipped form got
    two of the three wrong: `systemctl mask` REPLACES the unit file with a
    symlink to /dev/null and ConditionPathExists follows symlinks, and
    `systemctl disable` does not touch the unit file at all.

    The conditions are lifted out of the drop-in install.sh writes and only
    the /etc prefix is rewritten, so what systemd is handed here is the
    shipped directive with a different root.
    """
    text, _ = run_getty_dropin(tmp_path)
    etc = tmp_path / "sysroot" / "etc" / "systemd" / "system"
    (etc / "multi-user.target.wants").mkdir(parents=True)
    unit = etc / "otp-unit.service"
    wants = etc / "multi-user.target.wants" / "otp-unit.service"
    if state != "absent":
        unit.write_text("[Unit]\nDescription=OTP pad print unit\n")
        wants.symlink_to(unit)
    if state == "masked":
        unit.unlink()
        unit.symlink_to("/dev/null")
    if state == "disabled":
        wants.unlink()

    conditions = [ln.replace("/etc/systemd/system", str(etc))
                  for ln in text.splitlines() if ln.startswith("Condition")]
    assert len(conditions) == 2, text
    proc = subprocess.run(["systemd-analyze", "condition", *conditions],
                          capture_output=True, text=True, timeout=30)
    starts = proc.returncode == 0
    assert starts == (state != "healthy"), (
        f"with the panel {state}, systemd says the tty1 getty "
        f"{'starts' if starts else 'is skipped'}: {proc.stdout}{proc.stderr}")


def test_a_getty_already_running_on_tty1_is_stopped(tmp_path):
    """
    The half a condition cannot do, and tier 2 is where it was measured.

    `sudo ./device/install.sh` on a Pi that is already up meets a getty that
    started at boot, before any of this existed. otp-unit.service's
    Conflicts= used to stop it as a side effect of the unit starting; with
    the conflict gone and nothing in its place, the run went 11/15 with
    `getty1-stopped FAIL getty@tty1=active` and the unit `Started` then
    `Deactivated successfully` a second later -- the getty's Restart=always
    and TTYVHangup take /dev/tty1 back off the panel, whose Python exits.

    After the reload, not before it: the condition has to be in force before
    the getty is stopped, or anything that pokes it in between starts a new
    one.
    """
    _, log = run_getty_dropin(tmp_path)
    assert "stop getty@tty1.service" in log, log
    assert log.index("daemon-reload") < log.index("stop getty@tty1.service"), log


def test_the_getty_is_conditioned_rather_than_masked(tmp_path):
    """
    Masking looks equivalent and is not. `systemctl start` on a masked unit
    FAILS, cancel-rename's exit status is that command's, `userconf` hands
    it up, and userconf-service runs under `sh -e` -- so it dies before
    `rm`-ing the applied seed. Stock userconfig.service's Restart=on-failure
    then loops, printing "Failed with result" and "Scheduled restart job",
    two of the strings img-boot.sh fails a release on. A condition-skipped
    start returns success and does nothing.
    """
    _, log = run_getty_dropin(tmp_path)
    assert "mask getty@tty1" not in log, log


# --- the seeded userconf.txt path, as the guest sees it -------------------

# The stock unit is `StandardInput=tty`, `TTYPath=/dev/tty8` and
# `Restart=on-failure` (RPi-Distro/userconf-pi, debian/*.userconfig.service),
# and userconf-service goes INTERACTIVE whenever raspi-config's get_boot_cli
# says the machine boots to a console -- which this image does. That is the
# whiptail run 12's boot parked on. device/install.sh's drop-in is what stops
# it: the ConditionPathExists pair keeps the unit out of an unseeded boot
# entirely, and StandardInput=null turns a malformed seed into a fast failure
# instead of a prompt nobody can answer. These run the shipped block that
# checks all of that, against a systemd that says whatever the case needs.

HELPERS = ('check() {',
           'yesno() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }')

SYSTEMCTL_STUB = """#!/bin/sh
# Answers only what the probe asks, and says so loudly otherwise: a stub that
# silently returned "" for an unexpected question would make a check pass by
# accident, which is the failure this file exists to prevent.
case "$1 $2" in
    "show userconfig.service")
        case "$4" in
            ConditionTimestamp) printf '%s' "${UC_TS-}" ;;
            ConditionResult)    printf '%s' "${UC_COND-}" ;;
            Result)             printf '%s' "${UC_RESULT-}" ;;
            StandardInput)      printf '%s' "${UC_STDIN-}" ;;
            *) echo "stub: unexpected property $4" >&2; exit 64 ;;
        esac
        echo ;;
    "is-active userconfig.service")  echo "${UC_ACTIVE-inactive}" ;;
    "is-enabled userconfig.service") echo "${UC_ENABLED-enabled}" ;;
    "is-active otp-unit.service")    echo "${PANEL_ACTIVE-active}" ;;
    "is-active getty@tty1.service")  echo "${TTY1_ACTIVE-inactive}" ;;
    "is-enabled systemd-networkd-wait-online.service")
        echo "${NETWAIT_ENABLED-masked}" ;;
    # ONE queue for every question asked of it, because that is what the
    # machine has. A stub that answered the wizard's job count out of one
    # variable and the getty's out of another would let a test describe a
    # boot systemd cannot produce.
    "list-jobs --no-legend")         printf '%s' "${JOBS-}" ;;
    *) echo "stub: unexpected systemctl $*" >&2; exit 64 ;;
esac
"""

# The wizard's own log, which is the only account of it that survives the
# apply: cancel-rename disables the unit, systemd collects it, and every
# `systemctl show` after that describes a freshly loaded default. The
# default text is what run 31972140190's console recorded, verbatim.
#
# A JOURNAL THAT REMEMBERS, because the real one is one config line away from
# doing so. `-b` scopes the read to this boot; without it journalctl answers
# out of every boot it still holds, and device/install.sh's Storage=volatile
# is the only reason that is harmless today. UC_JOURNAL_LAST_BOOT is what a
# persistent journal would add, and the shipped `-b` is what keeps it out --
# so a test can set it and watch the check stay red.
JOURNALCTL_STUB = """#!/bin/sh
case "$*" in
    *userconfig.service*) ;;
    *) echo "stub: unexpected journalctl $*" >&2; exit 64 ;;
esac
case "$*" in
    *-b*) ;;
    *) [ -z "${UC_JOURNAL_LAST_BOOT-}" ] || printf '%s\\n' "$UC_JOURNAL_LAST_BOOT" ;;
esac
printf '%s\\n' "${UC_JOURNAL-\
Aug 16 22:05:01 otp-unit systemd[1]: Starting userconfig.service - User configuration dialog...
Aug 16 22:06:11 otp-unit systemd[1]: Finished userconfig.service - User configuration dialog.}"
"""

# What upstream does to a seed it will not accept: append the reason, rename
# it out of the way, and -- with no tty for the whiptail that comes next --
# die rather than prompt. The probe measures three things about this: that it
# ENDED, that userconf.txt is gone, and that the evidence is there.
USERCONF_SERVICE_STUB = """#!/bin/sh
cat "$BOOTDIR/userconf.txt" >> "$BOOTDIR/failed_userconf.txt"
rm -f "$BOOTDIR/userconf.txt"
echo "Entered username is invalid:"
exit 1
"""


# --- the journal on the console, and the panel that is not there ----------

# systemd-cat writes its stdin to the journal and NOTHING to stdout. Here the
# journal is a file, so a test can also model the machine where the write
# never lands -- which is the case that separates "forwarding is off" from
# "the marker was never made" when the host finds nothing on the console.
SYSTEMD_CAT_STUB = """#!/bin/sh
if [ -n "${SYSTEMD_CAT_DEAF-}" ]; then cat > /dev/null; exit 0; fi
cat >> "$FAKE_TAGGED_JOURNAL"
"""

# Answers the probe's two reads and refuses everything else, for the reason
# SYSTEMCTL_STUB does: a stub that returned "" for an unexpected question
# would let a check pass by accident.
#
# A JOURNAL THAT REMEMBERS PREVIOUS BOOTS, because the real one is one config
# line away from doing so. `-b` scopes both reads to this boot;
# UNIT_JOURNAL_LAST_BOOT is what a persistent journal would add, and the
# shipped `-b` is what keeps it out.
JOURNAL_READ_STUB = """#!/bin/sh
case "$*" in
    *"-t otp-imgcheck"*|*"-u otp-unit.service"*) ;;
    *) echo "stub: unexpected journalctl $*" >&2; exit 64 ;;
esac
case "$*" in
    *-b*) ;;
    *) [ -z "${UNIT_JOURNAL_LAST_BOOT-}" ] || printf '%s\\n' "$UNIT_JOURNAL_LAST_BOOT" ;;
esac
case "$*" in
    *"-t otp-imgcheck"*) cat "$FAKE_TAGGED_JOURNAL" 2>/dev/null ;;
    *"-u otp-unit.service"*) printf '%s\\n' "${UNIT_JOURNAL-}" ;;
esac
"""

# What a unit with no panel logs on the way to printing unattended, in the
# order otpunit/hmi.detect and otpunit/__main__ produce it.
#
# THE EMULATED MACHINE AS IT IS, quoted from the console rather than reasoned
# about. Run 32020772161, boot1 console-text.log:713-714:
#
#   no GPIO buttons ([Errno 22] Invalid argument)
#   interface -- display: none, input: none
#
# harness/img-boot.sh writes /system/linux,revision into the DTB so
# rpi-eeprom-update.service stops failing on an empty BOARD_INFO, and
# gpiozero reads the same property -- so the pin factory LOADS here, which is
# why the exception in the brackets is an OSError and not BadPinFactory. It
# does not follow that a Button constructs: claiming GPIO5 on QEMU's gpiochip
# fails EINVAL, hmi.open_buttons() falls through to (None, "none"), and this
# machine has neither half of an interface. An earlier version of this
# fixture said `input: GPIO buttons` and called itself the machine as it is
# now; it was the machine nothing has ever booted.
HEADLESS_JOURNAL = (
    "Aug 17 00:04:02 otp-unit systemd[1]: Started otp-unit.service.\n"
    "Aug 17 00:04:09 otp-unit python3[412]: no OLED (FileNotFoundError: "
    "[Errno 2] No such file or directory: '/dev/i2c-1')\n"
    "Aug 17 00:04:09 otp-unit python3[412]: no GPIO buttons ([Errno 22] "
    "Invalid argument)\n"
    "Aug 17 00:04:09 otp-unit python3[412]: interface -- display: none, "
    "input: none\n"
    "Aug 17 00:04:09 otp-unit python3[412]: no usable interface; printing "
    "unattended\n")

# A REAL Pi with lgpio, which is every unit this project targets. gpiozero
# only reserves and configures a pin -- there is no presence detection at all
# -- so `Button(5)` succeeds on a board with nothing wired to it and the unit
# reports `input: GPIO buttons`. That is the normal case on hardware, not a
# fault, and the check must go on passing for it: if it did not, the first
# unit anyone assembled would fail a check about its screen.
BUTTONS_JOURNAL = (HEADLESS_JOURNAL
                   .replace("Aug 17 00:04:09 otp-unit python3[412]: no GPIO "
                            "buttons ([Errno 22] Invalid argument)\n", "")
                   .replace("interface -- display: none, input: none",
                            "interface -- display: none, "
                            "input: GPIO buttons"))

# And a machine where gpiozero cannot build a pin factory at all -- a Pi with
# lgpio uninstalled, or an emulator that stops providing a gpiochip. A third
# shape of "no buttons", reported differently, and the check must pass for it
# too.
NO_FACTORY_JOURNAL = HEADLESS_JOURNAL.replace(
    "no GPIO buttons ([Errno 22] Invalid argument)",
    "no GPIO buttons (BadPinFactory: Unable to load any default pin factory!)")

# And a machine that DID find something to draw on. An HDMI monitor with no
# buttons: hmi.open_display still logs "no OLED (" because the SSD1306 probe
# raised, and __main__ still logs the headless line because
# Interface.interactive needs both halves -- so the two strings this check
# used to be made of are both there on a unit that has a screen.
SCREEN_JOURNAL = HEADLESS_JOURNAL.replace(
    "interface -- display: none, input: none",
    "interface -- display: HDMI console, input: none")


def journal_block() -> str:
    """The marker and the panel checks, sliced out of the shipped probe."""
    text = GUEST_CHECK.read_text()
    start = text.index("# --- the journal, now that the console carries it")
    return text[start:text.index("\n# --- CUPS,", start)]


def run_journal(tmp_path, *, phase="boot1", unit_journal=HEADLESS_JOURNAL,
                env=None):
    """Run the shipped journal/panel block with a journal made of files."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    tagged = tmp_path / "tagged-journal"
    tagged.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("systemd-cat", SYSTEMD_CAT_STUB),
                       ("journalctl", JOURNAL_READ_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    block = journal_block()
    # Only the two waits are shortened, and only the waits: every condition
    # and every string the block prints is the code that ships. Both are real
    # time in a real boot, and
    # test_the_probe_is_given_longer_than_its_own_bounded_wait is what holds
    # them against the unit's TimeoutStartSec.
    for interval, why in (("sleep 1", "the marker poll"),
                          ("sleep 2", "the panel poll")):
        assert block.count(interval) == 1, f"{why}'s interval moved"
        block = block.replace(interval, "sleep 0")

    runner = tmp_path / "journal.sh"
    runner.write_text(
        "set -uo pipefail\n"
        f'PHASE="{phase}"\nPASS=0\nTOTAL=0\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + block + '\nprintf "TOTALS %s/%s\\n" "$PASS" "$TOTAL"\n')
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {}),
             "FAKE_TAGGED_JOURNAL": str(tagged),
             "UNIT_JOURNAL": unit_journal,
             "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return proc, tagged


def test_a_headless_boot_reports_the_marker_and_the_missing_panel(tmp_path):
    # The positive control for everything below: every other test here
    # asserts a FAIL, and a block that failed unconditionally would satisfy
    # all of them.
    proc, _ = run_journal(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {"journal-marker-accepted": "PASS",
                             "unit-detects-no-panel": "PASS"}, proc.stdout


def test_the_marker_is_never_written_where_the_console_would_get_it_anyway(tmp_path):
    """
    The one thing that makes the host's gate mean anything.

    This probe's stdout IS the console: otp-unit-imgcheck.service carries
    StandardOutput=journal+console, so anything printed here reaches the
    serial port whether or not journald is forwarding. If the marker text
    ever appeared in a check's detail -- quoted back out of the journal, or
    echoed for debugging -- the host would find it on the console with
    forwarding switched off and pass a boot that proved nothing.

    So the block may say how MANY journal lines carry it and never what they
    say.
    """
    proc, tagged = run_journal(tmp_path)
    assert "OTP-JOURNAL-FORWARDED" in tagged.read_text(), \
        "the marker never reached the fake journal, so this proves nothing"
    assert "OTP-JOURNAL-FORWARDED" not in proc.stdout, proc.stdout
    assert "OTP-JOURNAL-FORWARDED" not in proc.stderr, proc.stderr


def test_a_journal_that_never_took_the_marker_fails(tmp_path):
    """
    The positive control the host cannot supply for itself.

    From outside the guest, a console with no marker on it is equally
    "forwarding is off" and "systemd-cat is missing, or wrote nothing". This
    check is what tells those apart, so it has to be able to say no.
    """
    proc, _ = run_journal(tmp_path, env={"SYSTEMD_CAT_DEAF": "1"})
    assert results(proc)["journal-marker-accepted"] == "FAIL", proc.stdout


def test_a_unit_that_never_looked_for_a_panel_fails(tmp_path):
    """
    Both halves of the headless decision, because either alone is a
    different machine.

    Without "no OLED (" the unit never probed the display at all -- the log
    below is what a unit that walked straight into the menu would leave, and
    it still says it is printing unattended.
    """
    journal = "\n".join(line for line in HEADLESS_JOURNAL.splitlines()
                        if "no OLED" not in line) + "\n"
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_unit_that_saw_no_panel_and_went_on_anyway_fails(tmp_path):
    """
    The other half. otpunit/hmi.Interface.prove exists because opening GPIO
    buttons proves nothing about whether buttons exist: a unit that noticed
    the missing OLED and still entered the menu blocks on an empty button
    queue forever, with no pad, no log line and no timeout. That machine
    logs the first string and never the second.
    """
    journal = "\n".join(line for line in HEADLESS_JOURNAL.splitlines()
                        if "no usable interface" not in line) + "\n"
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_unit_that_found_a_screen_is_not_a_unit_with_no_panel(tmp_path):
    """
    THE HOLE the third clause closes, and it belongs to a machine no tier of
    this harness boots -- which is the reason it had to be written down.

    An HDMI monitor with no buttons logs BOTH of the strings this check used
    to be made of: hmi.open_display reports "no OLED (" because the SSD1306
    probe raised, and __main__ reports "printing unattended" because
    Interface.interactive wants a display AND buttons. So a machine with a
    screen passed a check called unit-detects-no-panel.

    Not a hypothetical: a unit with a monitor plugged into it and nothing
    wired to the GPIO header is exactly this, and docs/HARDWARE.md describes
    that as a supported way to run one. The check has to key on the display
    side, said out loud, whatever the emulator does or does not provide.
    """
    proc, _ = run_journal(tmp_path, unit_journal=SCREEN_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_unit_whose_buttons_constructed_still_reports_no_panel(tmp_path):
    """
    The other direction, and the proof the third clause did not narrow what
    this check accepts.

    On every real Pi with lgpio installed `Button(5)` succeeds -- gpiozero
    reserves a pin and has no presence detection at all -- so a unit
    reporting `input: GPIO buttons` with nothing wired to those pins is the
    normal case on hardware, not a fault. The check must still pass for it,
    or the first unit anyone assembles fails a check about its screen.
    """
    assert "input: GPIO buttons" in BUTTONS_JOURNAL, \
        "this fixture is supposed to be the machine whose buttons constructed"
    assert "no GPIO buttons" not in BUTTONS_JOURNAL, BUTTONS_JOURNAL
    proc, _ = run_journal(tmp_path, unit_journal=BUTTONS_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_the_machine_tier_3_boots_has_neither_and_still_reports_no_panel(tmp_path):
    """
    The emulated unit, quoted from run 32020772161's boot1 console: the pin
    factory loads because the harness supplies a board revision, and the pin
    claim then fails EINVAL, so BOTH halves are none.

    This is the default fixture for everything in this section, and it is
    asserted here rather than assumed: for a while it said
    `input: GPIO buttons` and described itself as the machine as it is now,
    which meant six tests were driving a machine that has never booted.
    """
    assert "no GPIO buttons ([Errno 22] Invalid argument)" in HEADLESS_JOURNAL
    assert "interface -- display: none, input: none" in HEADLESS_JOURNAL
    proc, _ = run_journal(tmp_path, unit_journal=HEADLESS_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_a_unit_whose_pin_factory_never_loaded_still_reports_no_panel(tmp_path):
    """
    A third shape of "no buttons": gpiozero could not build a pin factory at
    all -- a Pi with lgpio uninstalled reports exactly this. No one of the
    three may be the only shape the check accepts.
    """
    proc, _ = run_journal(tmp_path, unit_journal=NO_FACTORY_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_a_unit_that_never_said_what_it_settled_on_fails(tmp_path):
    """
    The interface line is the one that carries the display kind, so a unit
    that never printed it leaves the new clause with nothing to read. An
    absence must be a FAIL rather than a shrug: the alternative is a check
    that goes green on a boot where the unit died between probing the display
    and deciding what to do about it.
    """
    journal = "\n".join(line for line in HEADLESS_JOURNAL.splitlines()
                        if "interface --" not in line) + "\n"
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def journal_from_the_real_hmi(monkeypatch, *, oled_present, buttons_present=False):
    """
    The lines otpunit's own detection writes, on the machine tier 3 boots.

    Not a fixture of what it is believed to log -- the real
    hmi.detect/open_display/open_buttons, with only the two pieces of
    hardware stubbed, so the strings come from the code that ships.

    The conditions are the measured ones, from run 32020772161's boot1
    console: no /dev/i2c-* so the SSD1306 probe raises FileNotFoundError,
    /sys/class/drm empty, and a Button whose pin claim raises OSError EINVAL
    -- `no GPIO buttons ([Errno 22] Invalid argument)` is the line that
    console carries. `buttons_present=True` is the real-Pi case instead,
    where gpiozero reserves the pin and succeeds.
    """
    import sys
    sys.path.insert(0, str(REPO))
    from otpunit import hmi

    class Dummy:
        def close(self):
            pass

    def oled(**_kwargs):
        if oled_present:
            return Dummy()
        raise FileNotFoundError(
            2, "No such file or directory", "/dev/i2c-1")

    def gpio(*_args, **_kwargs):
        if buttons_present:
            return Dummy()
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(hmi, "Ssd1306Display", oled)
    monkeypatch.setattr(hmi, "GpioButtons", gpio)
    monkeypatch.setattr(hmi, "screen_connected", lambda: False)
    monkeypatch.setattr(hmi, "keyboard_connected", lambda: False)

    lines = []
    interface = hmi.detect(log=lines.append)
    # The one line __main__ adds around describe(). Held against __main__'s
    # own source by tests/test_img_verdict.py.
    lines.append(f"interface -- {interface.describe()}")
    # And the decision. Given to the mutant as well, deliberately: a check
    # that only failed because the headless line went missing would be
    # proving something weaker than "the detection is what it reads".
    lines.append("no usable interface; printing unattended")
    return "".join(
        f"Aug 17 00:04:09 otp-unit python3[412]: {line}\n" for line in lines)


def test_the_check_reads_what_the_shipped_detection_actually_logs(tmp_path,
                                                                  monkeypatch):
    """
    End to end, with no fixture in the middle: the real hmi produces the
    lines, the shipped probe judges them -- and what it produces has to be
    the console this harness recorded, character for character in the parts
    the check reads.
    """
    journal = journal_from_the_real_hmi(monkeypatch, oled_present=False)
    assert "no GPIO buttons ([Errno 22] Invalid argument)" in journal, journal
    assert "interface -- display: none, input: none" in journal, journal
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_the_fixture_is_the_journal_the_shipped_detection_produces(monkeypatch):
    """
    HEADLESS_JOURNAL is the default for six tests in this section, so it is
    the fixture most worth holding against the code rather than against
    somebody's reading of a console. The timestamps and the OLED exception's
    wording are this file's; the two lines the check keys on are hmi's.
    """
    journal = journal_from_the_real_hmi(monkeypatch, oled_present=False)
    for line in ("no GPIO buttons ([Errno 22] Invalid argument)",
                 "interface -- display: none, input: none",
                 "no usable interface; printing unattended"):
        assert line in journal, (line, journal)
        assert line in HEADLESS_JOURNAL, (line, HEADLESS_JOURNAL)


def test_the_buttons_fixture_is_the_journal_a_real_pi_produces(monkeypatch):
    """The same, for the shape that must go on passing on hardware."""
    journal = journal_from_the_real_hmi(monkeypatch, oled_present=False,
                                        buttons_present=True)
    assert "interface -- display: none, input: GPIO buttons" in journal, journal
    assert "no GPIO buttons" not in journal, journal
    assert "interface -- display: none, input: GPIO buttons" in BUTTONS_JOURNAL
    assert "no GPIO buttons" not in BUTTONS_JOURNAL, BUTTONS_JOURNAL


def test_breaking_the_panel_absence_detection_still_turns_the_check_red(
        tmp_path, monkeypatch):
    """
    THE PROOF the emulator fix is allowed to ship. `unit-detects-no-panel`
    had to stop being able to pass for the wrong reason, and the way to show
    that is to break the thing it watches and see it go red.

    The break is the panel-absence detection itself: the SSD1306 probe
    succeeds, so the unit has a display where the emulated machine has none.
    Everything else is identical to the test above -- same function, same
    stubs, same headless line handed over for free -- and the only difference
    in the journal is what the shipped code said about the panel.
    """
    healthy = journal_from_the_real_hmi(monkeypatch, oled_present=False)
    broken = journal_from_the_real_hmi(monkeypatch, oled_present=True)
    assert healthy != broken, "the mutation changed nothing the unit logs"
    proc, _ = run_journal(tmp_path, unit_journal=broken)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_previous_boots_headless_decision_does_not_answer_for_this_one(tmp_path):
    """
    `-b`, and why it is load bearing three hundred lines from the file that
    makes it safe.

    device/install.sh sets Storage=volatile, so today an unscoped
    `journalctl -u` sees only this boot anyway. Delete that one line and a
    PREVIOUS boot's "no usable interface" satisfies a boot on which the unit
    never got that far -- which is precisely the reading this check exists to
    make impossible.
    """
    proc, _ = run_journal(
        tmp_path, unit_journal="",
        env={"UNIT_JOURNAL_LAST_BOOT": HEADLESS_JOURNAL})
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


# --- the identity that has to outlive the power cycle ---------------------

IDENTITY_BLOCK = (
    "# --- the identity, which is the one thing allowed to survive",
    # Through the closing `fi` of the boot2 branch, or the slice is a shell
    # fragment that will not parse -- which is a syntax error rather than a
    # test, and would have been one either way.
    '"boot1: $(short_id "$MACHINE_ID_BOOT1") | '
    'boot2: $(short_id "$LIVE_MACHINE_ID")"\nfi')

# The probe asks findmnt exactly one thing here: which filesystem contains the
# identity store. Anything else is a stub being asked a question nobody wrote
# an answer for, and it says so rather than returning "".
FINDMNT_STUB = """#!/bin/sh
case "$*" in
    *--target*) printf '%s\\n' "${FAKE_STORE_SRC-/dev/mmcblk0p1}" ;;
    *) echo "stub: unexpected findmnt $*" >&2; exit 64 ;;
esac
"""

# A different machine's id, and the one every "this is not the same box"
# fixture below uses. 32 hex characters, like the real thing.
OTHER_ID = "ffffffffffffffffffffffffffffffff"


def run_identity(tmp_path, *, phase="boot1", live_id=LIVE_ID, stored_id=LIVE_ID,
                 recorded=None, store_src="/dev/mmcblk0p1", boot_dir="boot",
                 make_boot=True, root_source="overlay"):
    """Run the shipped identity checks against a tree this test builds.

    Everything the block reads is a path it takes from a variable -- $BOOTDIR,
    $ETC_MACHINE_ID -- so the SHIPPED lines run here rather than a copy of
    them.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    boot = tmp_path / boot_dir
    machine_id = tmp_path / "machine-id"
    machine_id.write_text(live_id + "\n") if live_id is not None else None
    if make_boot:
        boot.mkdir(exist_ok=True)
        (boot / "otp-identity").mkdir(exist_ok=True)
        if stored_id is not None:
            (boot / "otp-identity" / "machine-id").write_text(stored_id + "\n")
        if recorded is not None:
            (boot / "otp-imgcheck-machine-id").write_text(recorded + "\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("findmnt", FINDMNT_STUB),):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)

    runner = tmp_path / "identity.sh"
    runner.write_text(
        "set -uo pipefail\n"
        f'PHASE="{phase}"\nPASS=0\nTOTAL=0\n'
        f'BOOTDIR="{boot}"\n'
        f'ETC_MACHINE_ID="{machine_id}"\n'
        f'ROOT_SOURCE="{root_source}"\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + slice_between(GUEST_CHECK.read_text(), *IDENTITY_BLOCK) + "\n")
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, "FAKE_STORE_SRC": store_src,
             "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return proc, boot


def test_a_boot_that_kept_its_identity_says_so(tmp_path):
    # The positive control for everything below: every other test here
    # asserts a FAIL, and a block that failed unconditionally would satisfy
    # all of them.
    proc, _ = run_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "machine-id-persisted-outside-the-overlay": "PASS",
        "machine-id-recorded-for-the-next-boot": "PASS"}, proc.stdout


def test_the_first_boot_writes_its_id_where_the_second_can_read_it(tmp_path):
    """The record is the whole mechanism boot2's check rests on, so boot1 has
    to leave it on the boot partition rather than merely read it."""
    proc, boot = run_identity(tmp_path)
    written = (boot / "otp-imgcheck-machine-id").read_text().strip()
    assert written == LIVE_ID, written
    assert results(proc)["machine-id-recorded-for-the-next-boot"] == "PASS"


def test_the_record_is_not_the_store_the_shipped_script_writes(tmp_path):
    """
    THE FINDING this pair was added for, stated as a property of the file
    names rather than of a comment.

    `machine-id-persisted-outside-the-overlay` compares the live id with
    /boot/firmware/otp-identity/machine-id -- and otp-unit-identity.service
    has already filled that in from the live id by the time the probe looks,
    so on a card whose store was wiped the two agree because one was copied
    from the other seconds earlier. The record has to be a DIFFERENT file,
    written by the probe, or boot2 is held against a value boot2 produced.
    """
    _, boot = run_identity(tmp_path)
    record = boot / "otp-imgcheck-machine-id"
    store = boot / "otp-identity" / "machine-id"
    assert record.exists() and store.exists()
    assert record != store, "the probe records into the store it is auditing"


def test_a_machine_with_no_id_at_all_records_nothing_and_says_so(tmp_path):
    """
    The positive control has to be able to fail, or boot2's "identical"
    means nothing. A machine with no readable /etc/machine-id writes an empty
    record and reads it back -- empty equals empty, which is exactly the shape
    of agreement this check exists to refuse.
    """
    proc, _ = run_identity(tmp_path, live_id=None)
    assert results(proc)["machine-id-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout


def test_an_id_that_never_reached_the_card_fails(tmp_path):
    """
    Reading it is not recording it. With no writable boot directory the
    record never lands, and boot2 would have nothing to compare against --
    which must be boot1's failure rather than boot2's mystery.
    """
    proc, _ = run_identity(tmp_path, make_boot=False)
    assert results(proc)["machine-id-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout


def test_the_second_boot_accepts_the_same_machine(tmp_path):
    proc, _ = run_identity(tmp_path, phase="boot2", recorded=LIVE_ID)
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "PASS", proc.stdout


def test_an_id_regenerated_by_the_power_cycle_fails_the_second_boot(tmp_path):
    """
    THE FAILURE THE OLD CHECK COULD NOT SEE, and the reason this pair exists.

    The FAT store was truncated or deleted between the boots, so systemd
    generated a fresh id and otp-unit-identity.service wrote THAT into the
    store: the live id and the store agree perfectly, and
    machine-id-persisted-outside-the-overlay is green on a unit that is a
    different machine than it was an hour ago. The record boot1 left is the
    only thing that disagrees -- so both checks are asked of the same
    fixture here, and exactly one of them is allowed to be satisfied.
    """
    proc, _ = run_identity(tmp_path, phase="boot2", live_id=OTHER_ID,
                           stored_id=OTHER_ID, recorded=LIVE_ID)
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "PASS", "the fixture no longer reproduces the hole: " + proc.stdout
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "FAIL", proc.stdout


def test_two_absences_are_not_an_identical_machine_id(tmp_path):
    """
    THE reading this check has to be unable to make. No id in boot2 and
    nothing recorded by boot1 compare equal as strings, and a machine that
    lost its identity entirely would then certify that it kept it.
    """
    proc, _ = run_identity(tmp_path, phase="boot2", live_id=None, recorded=None)
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "FAIL", proc.stdout


def test_a_second_boot_with_an_id_but_no_record_fails(tmp_path):
    """Half of that absence on its own: boot1 never recorded anything, so
    there is nothing this boot's id can be identical TO."""
    proc, _ = run_identity(tmp_path, phase="boot2", recorded=None)
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "FAIL", proc.stdout


def test_a_machine_id_that_was_never_kept_fails(tmp_path):
    proc, _ = run_identity(tmp_path, stored_id=None)
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_kept_machine_id_that_is_not_the_one_in_use_fails(tmp_path):
    """
    Which is what a failed initramfs restore looks like from userspace: the
    store holds an id, systemd generated a different one because it never saw
    it, and the boot was a first boot all over again.
    """
    proc, _ = run_identity(tmp_path, stored_id="ffffffffffffffffffffffffffffffff")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_machine_with_no_machine_id_at_all_fails(tmp_path):
    """
    Two absences again, in the other half. An unreadable /etc/machine-id and
    an empty store compare equal, and "the identity persisted" would be
    certified of a machine that has no identity.
    """
    proc, _ = run_identity(tmp_path, live_id=None, stored_id="")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_store_inside_the_overlay_is_not_persistence(tmp_path):
    """
    The clause that stops the whole check being self-satisfying. If
    /boot/firmware never mounted, the store is a directory on the overlay's
    tmpfs: the copy and the original agree on every boot and nothing survives
    any of them. findmnt then reports the ROOT's source for the store, which
    is the state named here.
    """
    proc, _ = run_identity(tmp_path, store_src="overlay")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_root_nothing_could_describe_does_not_make_the_store_separate(tmp_path):
    """
    The other end of the same comparison. If findmnt could not answer for
    `/`, $ROOT_SOURCE is empty and "the store is not on the root's
    filesystem" is true of every store there is. `root-is-overlay` would be
    red on that machine too, but a check that is only correct because a
    different check failed is a coincidence with a name.
    """
    proc, _ = run_identity(tmp_path, root_source="")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


# --- the diagnostic sheet -------------------------------------------------

def sheet_block() -> str:
    """The boot-1 print-path experiment, sliced out of the shipped probe."""
    text = GUEST_CHECK.read_text()
    start = text.index('if [ "$PHASE" = "boot1" ]; then\n'
                       '    # --- the diagnostic sheet')
    return text[start:text.index("\n# --- the seeded userconf.txt path", start)]


# What the shipped heredoc prints when the image can draw the sheet and cupsd
# takes it. test_the_sheet_program_prints_what_the_probe_reads runs the real
# program and holds these shapes against it.
SHEET_HEALTHY = "RENDER ok bytes=4632 magic=%PDF-\nSUBMIT ok job=otpimgcheck-1\n"


def run_sheet(tmp_path, *, says=SHEET_HEALTHY, lpadmin_rc=0):
    """Run the shipped sheet block with the Python and CUPS tools stubbed.

    The interpreter and the two CUPS binaries are rewritten rather than put
    on PATH, because the block names them by absolute path -- on purpose, the
    way otpunit/printer.py does. Everything else, including both `check`
    calls' conditions and the `case` patterns that read the program's output,
    is the code that ships.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    calls = tmp_path / "lpadmin-calls"
    lpadmin = tmp_path / "lpadmin"
    lpadmin.write_text("#!/bin/sh\n"
                       f'printf "%s\\n" "$*" >> "{calls}"\n'
                       'case "$1" in -x) exit 0 ;; esac\n'
                       f'echo "lpadmin said something"\nexit {lpadmin_rc}\n')
    lpadmin.chmod(0o755)
    lpstat = tmp_path / "lpstat"
    lpstat.write_text("#!/bin/sh\necho 'device for otpimgcheck: usb://OTP/imgcheck'\n")
    lpstat.chmod(0o755)
    program = tmp_path / "python3"
    # Reads the heredoc off stdin and throws it away, exactly as a python3
    # that ran it and printed nothing else would.
    program.write_text("#!/bin/sh\ncat > /dev/null\n"
                       f'printf "%s" "$SHEET_SAYS"\n')
    program.chmod(0o755)

    block = sheet_block()
    for original, replacement, why in (
        ("/usr/sbin/lpadmin", str(lpadmin), "lpadmin"),
        ("/usr/bin/lpstat", str(lpstat), "lpstat"),
        ("timeout -k 5 90 python3 -", f"{program} -", "the interpreter"),
    ):
        assert block.count(original) >= 1, f"{why}: {original!r} in the block"
        block = block.replace(original, replacement)

    runner = tmp_path / "sheet.sh"
    runner.write_text(
        "set -uo pipefail\n"
        'PHASE="boot1"\nPASS=0\nTOTAL=0\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + block + '\nprintf "TOTALS %s/%s\\n" "$PASS" "$TOTAL"\n')
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, "SHEET_SAYS": says})
    return proc, calls


def test_a_sheet_that_rendered_and_was_accepted_passes(tmp_path):
    # The positive control, and the only test here that does not assert a
    # FAIL.
    proc, calls = run_sheet(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {"diagnostic-sheet-renders": "PASS",
                             "diagnostic-sheet-reaches-cups": "PASS"}, proc.stdout
    # And the machine is left as a unit rather than as a rig.
    assert "-x otpimgcheck" in calls.read_text(), calls.read_text()


def test_an_image_that_cannot_draw_the_sheet_fails(tmp_path):
    """
    The half nothing else in this repository can ask. tier 1 renders this
    sheet against the host's reportlab; only a booted image can say whether
    the thing that gets flashed has one.
    """
    proc, _ = run_sheet(tmp_path, says="RENDER failed ModuleNotFoundError: reportlab\n")
    assert results(proc) == {"diagnostic-sheet-renders": "FAIL",
                             "diagnostic-sheet-reaches-cups": "FAIL"}, proc.stdout


def test_a_sheet_that_is_not_a_pdf_fails(tmp_path):
    """
    Bytes are not a page. reportlab writing an empty or truncated file
    raises nothing, and "no exception" is the whole of what a bare rc=0
    would prove -- so the magic number is checked, which is the cheapest
    thing that separates a sheet from an artefact.
    """
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=0 magic=\n"
                                       "SUBMIT ok job=otpimgcheck-1\n")
    assert results(proc)["diagnostic-sheet-renders"] == "FAIL", proc.stdout


def test_a_sheet_cupsd_would_not_take_fails_with_the_render_still_green(tmp_path):
    """
    Two checks, because they fail for entirely different reasons and a
    single one would report "the print path is broken" for either.
    """
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=4632 magic=%PDF-\n"
                                       "SUBMIT failed PrinterError: lp: Bad destination\n")
    assert results(proc) == {"diagnostic-sheet-renders": "PASS",
                             "diagnostic-sheet-reaches-cups": "FAIL"}, proc.stdout


def test_an_lp_that_returned_no_job_id_is_not_an_enqueued_sheet(tmp_path):
    """
    ENQUEUED means cupsd gave it an id, not that lp exited 0.

    Cups.submit() pulls "request id is <id>" out of lp's stdout and returns
    "" when it is not there -- an lp that succeeded and said nothing useful
    is a job nobody can point at, and the queue this ran against has no
    printer behind it to notice either way.
    """
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=4632 magic=%PDF-\n"
                                       "SUBMIT ok job=<no id>\n")
    assert results(proc)["diagnostic-sheet-reaches-cups"] == "FAIL", proc.stdout


def test_a_job_on_somebody_elses_queue_is_not_this_experiment(tmp_path):
    """
    The id has to name the queue the probe made. `lp` reports the id as
    <queue>-<n>, so a job id from anywhere else -- the unit's own OTP queue,
    say, if one ever existed on this machine -- would otherwise read as this
    experiment having worked.
    """
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=4632 magic=%PDF-\n"
                                       "SUBMIT ok job=OTP-7\n")
    assert results(proc)["diagnostic-sheet-reaches-cups"] == "FAIL", proc.stdout


def test_the_sheet_program_prints_what_the_probe_reads(tmp_path):
    """
    The shipped Python, run for real, against the shipped otpunit.

    Everything above stubs the interpreter, so the `case` patterns the block
    matches on are only as good as the guess about what the program prints.
    This runs the actual heredoc: diagnostics.collect() over this machine and
    render_bytes() through reportlab, which is also the only place in the
    fast suite where the sheet the headless design rests on is drawn at all.

    The submit half is expected to FAIL here -- there is no cupsd on a test
    runner -- and that it fails in the shape the block reads is the point.
    """
    block = sheet_block()
    opener = "<<'PY' 2>&1\n"
    assert block.count(opener) == 1, "the sheet program's heredoc moved"
    start = block.index(opener) + len(opener)
    program = block[start:block.index("\nPY\n", start)]
    assert "diagnostics.render_bytes" in program, program[:200]
    # /opt/otp-unit does not exist here; the repository root is on sys.path
    # already, so the shipped `sys.path.insert` is simply inert.
    #
    # CUPS_SERVER at a socket that cannot exist, so the submit half is
    # hermetic: `lp` fails against nothing rather than reaching whatever
    # daemon happens to be running on the machine the fast suite is on. The
    # rig in tests/cupsrig.py is the hardware tier's job and carries a marker
    # for it; this test must not touch a real queue by accident.
    proc = subprocess.run(
        ["python3", "-c", program], capture_output=True, text=True, timeout=180,
        cwd=REPO, env={**os.environ, "SHEET_QUEUE": "otpimgcheck",
                       "CUPS_SERVER": "/nonexistent/otp-no-such-cupsd.sock"})
    assert proc.returncode == 0, proc.stderr
    said = proc.stdout
    assert re.search(r"^RENDER ok bytes=\d+ magic=%PDF-", said, re.M), said
    assert re.search(r"^SUBMIT (ok job=|failed )", said, re.M), said
    # And what it printed is what the shipped block reads as a rendered
    # sheet. The glob is taken out of the probe rather than restated here:
    # its quoted literals have to appear, in order, in what the program said.
    glob = re.search(r"(\*\".*magic=%PDF\"\*)\) SHEET_RENDERED=yes",
                     sheet_block())
    assert glob, "the render pattern is gone from the probe"
    rest = said
    for literal in re.findall(r'"([^"]*)"', glob.group(1)):
        assert literal in rest, (glob.group(1), literal, said)
        rest = rest.split(literal, 1)[1]


def userconf_block() -> str:
    text = GUEST_CHECK.read_text()
    start = text.index("# --- the seeded userconf.txt path")
    return text[start:text.index("\nsync 2>/dev/null || true", start)]


def shadow_field(shadow_text: str, user: str) -> str:
    """Field 2 of an /etc/shadow line, as the probe's own awk reads it."""
    for line in shadow_text.splitlines():
        fields = line.split(":")
        if fields[0] == user:
            return fields[1]
    return ""


def sha256_of(text: str) -> str:
    """What the probe's `sha256sum` answers for the same bytes.

    The probe digests the hash rather than carrying it between the boots
    because its console is uploaded as a CI artifact and a crypt string is
    offline-crackable at leisure. Recomputed here rather than pasted, so the
    fixture cannot drift away from the thing under test.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def run_userconf(tmp_path, *, phase, service=USERCONF_SERVICE_STUB,
                 executable=True, shadow=None, env=None,
                 credential="otp:$6$otpimgcheck$hashbytes",
                 boot1_digest="auto", store_src="/dev/mmcblk0p1",
                 root_source="overlay", sha256sum=True):
    """Run the shipped userconf block with a substituted systemd and shadow.

    Three absolute paths are rewritten rather than stubbed on the filesystem
    -- /etc/shadow, the userconf-service the experiment runs, and the boot
    directory -- for the reason run_phase_guard gives: everything else in the
    block, including both `check` calls' conditions and the wording they
    print, is the code that ships. The two waits are shortened, and only
    those: the 30s condition poll and the 60s bound on the experiment are
    real time in a real boot, and test_the_probe_is_given_longer_than_its_own
    _bounded_wait is what holds them against the unit's TimeoutStartSec.
    """
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    shadow_file = tmp_path / "shadow"
    shadow_file.write_text(
        "root:!:20000:0:99999:7:::\n"
        + (shadow if shadow is not None
           else "otp:$6$otpimgcheck$hashbytes:20000:0:99999:7:::\n"))
    svc = tmp_path / "userconf-service"
    if service is not None:
        svc.write_text(service)
        svc.chmod(0o755 if executable else 0o644)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(SYSTEMCTL_STUB)
    (bin_dir / "systemctl").chmod(0o755)
    (bin_dir / "journalctl").write_text(JOURNALCTL_STUB)
    (bin_dir / "journalctl").chmod(0o755)
    if not sha256sum:
        # THE MISSING COMMAND, which is the question every guard here has to
        # answer: what exit code, empty output or absent binary makes this
        # pass silently? `cred_digest` pipes into sha256sum and takes what
        # comes back, so one that is absent or broken answers the empty
        # string -- in BOTH boots, which is how "the password survived the
        # power cycle" turns into "there was never a password". Exits 0, like
        # a command that is not there behind a `2>/dev/null`.
        (bin_dir / "sha256sum").write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
        (bin_dir / "sha256sum").chmod(0o755)

    # The credential store the probe audits, and the record boot1 leaves for
    # boot2. Supplied here rather than left to the block, because both are
    # written by the IMAGE -- persist-identity.sh puts the store there from the
    # wizard's ExecStartPost -- and this rig has no image.
    store = bootdir / "otp-identity"
    store.mkdir(parents=True, exist_ok=True)
    if credential is not None:
        (store / "credential").write_text(credential + "\n")
    # "auto" is the healthy path: the digest boot1 would have written of the
    # hash this fixture's own /etc/shadow holds. Computed from the fixture
    # rather than pasted, so a test that changes the hash cannot leave a
    # record describing the old one and call the mismatch a finding.
    if boot1_digest == "auto":
        boot1_digest = sha256_of(shadow_field(shadow_file.read_text(), "otp"))
    if boot1_digest is not None:
        (bootdir / "otp-imgcheck-credential").write_text(boot1_digest + "\n")

    block = userconf_block()
    for original, replacement, why, count in (
        # THE READ, not the path: the comments in the block name /etc/shadow
        # eight times over and rewriting those would prove nothing. TWICE,
        # not once, because boot1 reads the account's hash to say the seed
        # was applied and boot2 reads it again to say the hash outlived the
        # power cycle -- a rewrite that patched one would leave the other
        # reading the machine this test runs on.
        ("{print $2}' /etc/shadow", "{print $2}' " + str(shadow_file),
         "the shadow read", 2),
        ("/usr/lib/userconf-pi/userconf-service", str(svc), "the service", 1),
        ("timeout -k 5 60", "timeout -k 1 2", "the experiment's bound", 1),
        ("sleep 2", "sleep 0", "the condition poll", 1),
    ):
        assert block.count(original) == count, \
            f"{why}: {original!r} appears {block.count(original)}x, want {count}"
        block = block.replace(original, replacement)

    runner = tmp_path / "userconf.sh"
    runner.write_text(
        "set -uo pipefail\n"
        f'PHASE="{phase}"\nBOOTDIR="{bootdir}"\nPASS=0\nTOTAL=0\n'
        # What the identity section above the block would have set. Named
        # here for the same reason $BOOTDIR is: the credential checks compare
        # the store's filesystem against the root's, and that comparison has
        # to be steerable or the clause that catches an unmounted
        # /boot/firmware can never be shown to work.
        f'IDENTITY_STORE="{store}"\nSTORE_SRC="{store_src}"\n'
        f'ROOT_SOURCE="{root_source}"\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + block + '\nprintf "TOTALS %s/%s\\n" "$PASS" "$TOTAL"\n')
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {}), "BOOTDIR": str(bootdir),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return proc, bootdir


HEALTHY_BOOT1 = {"UC_TS": "Sat 2026-08-15 10:00:00 UTC", "UC_COND": "yes",
                 "UC_RESULT": "success", "UC_ACTIVE": "inactive",
                 "UC_ENABLED": "enabled", "UC_STDIN": "null", "JOBS": "",
                 "NETWAIT_ENABLED": "masked", "PANEL_ACTIVE": "active",
                 "TTY1_ACTIVE": "inactive"}
HEALTHY_BOOT2 = {**HEALTHY_BOOT1, "UC_COND": "no", "UC_RESULT": ""}


def results(proc) -> dict:
    """The check lines the block printed, name -> PASS/FAIL."""
    return {name: state for name, state in
            re.findall(r"^OTP-CHECK \S+ (\S+) (PASS|FAIL)", proc.stdout, re.M)}


def test_a_seeded_first_boot_reports_applied_credentials_and_no_wizard(tmp_path):
    # The positive control for everything below: every other test here
    # asserts a FAIL, and a block that failed unconditionally would satisfy
    # all of them.
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "network-wait-cannot-hold-the-boot-open": "PASS",
        "userconf-seed-applied": "PASS",
        "userconf-seeded-boot-ran-no-wizard": "PASS",
        "front-panel-survives-the-credential-apply": "PASS",
        "credential-recorded-outside-the-overlay": "PASS",
        "credential-recorded-for-the-next-boot": "PASS"}, proc.stdout


def test_a_boot_still_waiting_on_the_network_fails(tmp_path):
    """
    Run 31968966879's shape, stated as a check.

    The wizard's job was queued behind network-online.target, which was
    queued behind a wait with no timeout, and from the unit's own fields
    that is indistinguishable from a skip. The queue is asked directly, in
    both boots, because the boot that suffers it is any boot.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "JOBS": "1 systemd-networkd-wait-online.service start running\n"})
    assert results(proc)["network-wait-cannot-hold-the-boot-open"] == "FAIL", \
        proc.stdout


def test_an_unmasked_network_wait_fails_even_with_an_empty_queue(tmp_path):
    """
    Both halves, because either alone is satisfied by the broken image.

    An empty queue at the moment the probe looks says nothing about the next
    boot: whatever pulls network-online.target in decides that, and on the
    image run 31968966879 booted it was cloud-init. The mask is what makes
    the answer the same every time, so it is read back off the machine.
    """
    for state in ("enabled", "disabled", "static"):
        proc, _ = run_userconf(tmp_path / state, phase="boot2",
                               env={**HEALTHY_BOOT2, "NETWAIT_ENABLED": state})
        assert results(proc)["network-wait-cannot-hold-the-boot-open"] == "FAIL", \
            proc.stdout


def test_a_credential_apply_that_took_the_front_panel_down_fails(tmp_path):
    """
    The defect this check exists for, with the credentials applied perfectly.

    cancel-rename starts a getty on tty1 at the end of every successful
    apply. While otp-unit.service carried Conflicts=getty@tty1.service that
    start stopped the panel, so a unit whose operator used the documented
    credential file printed nothing until it was power-cycled -- with every
    other check in this phase green, because the seed WAS applied.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "PANEL_ACTIVE": "inactive"})
    assert results(proc)["userconf-seed-applied"] == "PASS", proc.stdout
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout


def test_a_login_prompt_sharing_the_panels_tty_fails(tmp_path):
    """
    The other outcome of the same start, and the reason the check does not
    stop at "otp-unit is active". Without the Conflicts= that used to stop
    it, a getty that DOES start shares /dev/tty1 with a unit holding it
    through StandardInput=tty-force: output interleaves and keystrokes go to
    whichever grabbed them. A getty merely queued is the same thing a moment
    later, so the queue counts too.
    """
    proc, _ = run_userconf(tmp_path / "running", phase="boot1",
                           env={**HEALTHY_BOOT1, "TTY1_ACTIVE": "active"})
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout
    proc, _ = run_userconf(tmp_path / "queued", phase="boot1", env={
        **HEALTHY_BOOT1, "JOBS": "7 getty@tty1.service start waiting\n"})
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout


def test_a_wizard_still_holding_a_job_fails_the_first_boot(tmp_path):
    """
    Run 12's shape, stated as a check, with everything else looking healthy.

    A queued job for a unit that has not started yet reads as `inactive` in
    every other field -- and a job is the thing that actually held run 12's
    boot open, with multi-user.target waiting on a whiptail nobody could
    see. So the queue is asked directly rather than inferred from is-active.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "JOBS": "42 userconfig.service start waiting\n"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_parked_mid_start_fails_the_first_boot(tmp_path):
    # The other half of the same shape: the prompt is up, the unit has been
    # "activating" for as long as the boot has been waiting.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_ACTIVE": "activating", "UC_RESULT": "",
        "JOBS": "42 userconfig.service start running\n"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_seeded_boot_whose_wizard_was_skipped_fails(tmp_path):
    # The condition came out false with a seed sitting on the card: the
    # drop-in's paths and the file the operator wrote disagree, or the unit
    # was masked. The credentials are silently ignored either way, and the
    # log says so in systemd's words rather than the unit's fields.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_COND": "no",
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service"
                      " - User configuration dialog was skipped because of an"
                      " unmet condition check (ConditionPathExists=|/boot/"
                      "firmware/userconf)."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_seeded_boot_whose_wizard_never_ran_at_all_fails(tmp_path):
    """
    Nothing in the log, nothing in the queue, nothing wrong with the unit --
    which is what a masked or never-pulled-in wizard looks like from every
    other field, and what run 31968966879's boots looked like once their
    queued job is taken out of the picture. The operator's credentials are
    ignored in silence.

    An empty journal is the one thing a garbage-collected unit and a
    never-started one do NOT share: the collected one leaves its "Finished"
    line behind, which is the whole reason this check reads the log.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "UC_JOURNAL": ""})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_previous_boots_success_line_does_not_answer_for_this_boot(tmp_path):
    """
    `journalctl -u` with no boot filter answers out of every boot the journal
    still holds. This machine's journal is volatile -- device/install.sh
    writes Storage=volatile into journald.conf.d -- so today there is only
    ever one boot in it, and the read was correct by accident three hundred
    lines away in another file. Delete that one setting, or ship on a machine
    someone has made persistent, and the FIRST boot's `Finished
    userconfig.service` satisfies a later boot on which the unit never ran at
    all: exactly the reading this block exists to make impossible.

    `-b` on the read is what stops it, and this is the fixture that can tell.
    The stub keeps a previous boot's entries and hands them over only to a
    caller that did not ask for this one.
    """
    persistent = {**HEALTHY_BOOT1, "UC_JOURNAL": "", "UC_JOURNAL_LAST_BOOT":
                  "Aug 15 09:00:00 otp-unit systemd[1]: Finished "
                  "userconfig.service - User configuration dialog."}
    proc, _ = run_userconf(tmp_path, phase="boot1", env=persistent)
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout
    # The positive control on the fixture itself: the same journal, offered
    # as THIS boot's, is the healthy case. Without this the test above would
    # also pass against a stub that simply printed nothing.
    proc, _ = run_userconf(
        tmp_path / "this-boot", phase="boot1",
        env={**HEALTHY_BOOT1, "UC_JOURNAL": persistent["UC_JOURNAL_LAST_BOOT"]})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "PASS", proc.stdout


def test_the_wizards_own_stdout_cannot_report_its_success(tmp_path):
    """
    `journalctl -u` returns the unit's STDOUT as well as PID 1's status lines
    about it -- that is what StandardOutput=journal means -- so a
    userconf-service that merely PRINTED the phrase satisfied the check.
    Nothing upstream prints it today, which is the only reason this was not
    already a false green; a phrase whose truth depends on nobody happening
    to say it is not a measurement.

    `Finished` is systemd's own success line. Matching the speaker with it
    costs nothing and means only PID 1 can make the claim.
    """
    impostor = ("Aug 16 22:05:01 otp-unit systemd[1]: Starting "
                "userconfig.service - User configuration dialog...\n"
                "Aug 16 22:06:10 otp-unit userconf-service[512]: Finished "
                "userconfig.service - User configuration dialog.")
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "UC_JOURNAL": impostor})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout
    assert "journal-finished=no" in proc.stdout, proc.stdout


def test_the_success_line_is_matched_literally_not_as_a_glob(tmp_path):
    """
    `systemd[1]:` is a glob pattern as well as a string, and `[1]` is a
    character class matching the single character 1. Unquoted inside the
    ${var#pattern} that does the matching, `systemd1: Finished` would
    therefore pass -- which is not a line systemd writes, but it is a line
    the unit's own stdout could.
    """
    globbed = ("Aug 16 22:06:10 otp-unit userconf-service[512]: systemd1: "
               "Finished userconfig.service - User configuration dialog.")
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "UC_JOURNAL": globbed})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_that_failed_on_the_seed_fails_the_first_boot(tmp_path):
    # A malformed seed met at BOOT: userconf-service dies on the whiptail it
    # cannot draw, and Restart=on-failure prints the two strings img-boot.sh
    # fails a release on. The log carries both, so the check does not have to
    # ask a unit that may already have been collected.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_RESULT": "exit-code", "UC_ACTIVE": "failed",
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service:"
                      " Failed with result 'exit-code'. Aug 16 22:05:02 otp-unit"
                      " systemd[1]: userconfig.service: Scheduled restart job,"
                      " restart counter is at 1."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_that_only_finished_after_a_restart_fails(tmp_path):
    """
    "Finished" on its own is not the whole answer, because stock
    userconfig.service carries Restart=on-failure: a boot that met a bad seed
    can fail, be restarted, and finish. That boot printed "Failed with result"
    and "Scheduled restart job" -- two of the strings img-boot.sh hard-fails a
    release on -- so the guest must not call it clean either.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service:"
                      " Failed with result 'exit-code'. Aug 16 22:05:31 otp-unit"
                      " systemd[1]: userconfig.service: Scheduled restart job,"
                      " restart counter is at 1. Aug 16 22:06:11 otp-unit"
                      " systemd[1]: Finished userconfig.service - User"
                      " configuration dialog."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_finished_line_from_a_collected_unit_is_still_a_pass(tmp_path):
    """
    The measurement this check was rewritten around, stated as a test.

    A seeded apply ends in cancel-rename, which runs `systemctl disable
    userconfig` and a daemon-reload; the oneshot is inactive and unreferenced
    by then, so systemd garbage-collects it and every property a later
    `systemctl show` returns is a pristine default. Run 31972140190 measured
    exactly that -- `condition=no result=success is-active=inactive jobs=0`
    on a boot whose console says `Finished userconfig.service`, whose shadow
    entry carries the seeded salt and whose card came back with the seed
    consumed. Failing that boot would be the harness calling a correct
    machine broken.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_TS": "", "UC_COND": "no", "UC_RESULT": "success"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "PASS", proc.stdout


def test_a_seed_deleted_without_being_applied_fails(tmp_path):
    """
    The gap the shadow check exists to close.

    From the host the file is gone, which is exactly what success looks like.
    A userconf-service that unlinked the seed without running chpasswd would
    leave the operator with an account whose password is pi-gen's random
    FIRST_USER_PASS -- a value nobody has -- and a green tier 3.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           shadow="otp:$6$rEaLsAlT$otherbytes:20000:0:99999:7:::\n")
    assert results(proc)["userconf-seed-applied"] == "FAIL", proc.stdout


def test_no_shadow_entry_at_all_fails(tmp_path):
    # The account renamed out from under the seed, or never created.
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           shadow="pi:$6$otpimgcheck$hashbytes:20000::::::\n")
    assert results(proc)["userconf-seed-applied"] == "FAIL", proc.stdout


def test_an_unseeded_second_boot_is_quiet_and_leaves_evidence(tmp_path):
    # The positive control for the boot2 half, and the healthy path: skipped
    # but enabled, no prompt possible, and the malformed seed quarantined.
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "network-wait-cannot-hold-the-boot-open": "PASS",
        "userconf-unseeded-boot-skips-the-wizard": "PASS",
        "userconf-wizard-cannot-prompt": "PASS",
        "credential-survives-the-power-cycle": "PASS",
        "userconf-malformed-seed-fails-fast": "PASS"}, proc.stdout
    assert (bootdir / "failed_userconf.txt").exists()
    assert not (bootdir / "userconf.txt").exists()


# --- and the credential the seed used to lose ------------------------------
#
# These drive the SAME block through the same rig. The image the checks
# describe is the one the owner's decision produced: the applied hash is kept
# in /boot/firmware/otp-identity/credential, restored at sysinit, and still in
# force after the power cycle. Every test below is a way for that to look
# right while being false.


def test_a_boot_that_applied_the_seed_but_kept_nothing_fails(tmp_path):
    """
    THE OLD BEHAVIOUR, stated as a failure.

    Everything else in boot1 was equally true of the image that lost the
    password: the hash reaches /etc/shadow, the wizard finishes, the panel
    survives. /etc is inside the overlay, so all of it died with the power.
    An empty store is that image, and it has to be red.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           credential=None)
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout
    # The other one still passes: boot1 watched the apply happen and wrote its
    # own record. That is the point of splitting them -- the positive control
    # must not go red for the thing it is controlling for.
    assert results(proc)["credential-recorded-for-the-next-boot"] == "PASS"


def test_a_store_holding_something_other_than_the_applied_hash_fails(tmp_path):
    """
    A store that exists is not a store that is right. This is the shape a
    stale credential has: the operator seeded a NEW password, the wizard
    applied it, and the recording phase never ran -- so the next power cycle
    puts the OLD one back and the operator is locked out of a password they
    believe they changed.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           credential="otp:$6$otpimgcheck$somethingelse")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_store_naming_another_account_fails(tmp_path):
    """`chpasswd` takes `user:hash`, so a store naming somebody else restores
    nothing -- and the digest of its hash field can still match perfectly."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           credential="pi:$6$otpimgcheck$hashbytes")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_credential_store_inside_the_overlay_is_not_persistence(tmp_path):
    """
    #35's hole, on the credential side. An unmounted /boot/firmware leaves the
    store as a directory on the overlay's tmpfs: it agrees with /etc/shadow
    perfectly, on every boot, while nothing survives any of them.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           store_src="overlay", root_source="overlay")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_root_nothing_could_describe_does_not_make_the_credential_kept(tmp_path):
    """
    The same clause from the other end. If findmnt could not describe `/`,
    $ROOT_SOURCE is empty and "the store is not on the root's filesystem"
    becomes true of every store there is -- a check that is only correct
    because a different one failed.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           root_source="")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_first_boot_with_no_credential_at_all_records_nothing_and_says_so(tmp_path):
    """
    The positive control's own negative. A machine whose /etc/shadow has no
    entry for the account has no credential to record, and a record file
    written from an empty string would be a digest of nothing that boot2's
    equally-empty read would match.
    """
    proc, bootdir = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                                 shadow="pi:$6$x$y:20000:0:99999:7:::\n")
    assert results(proc)["credential-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout
    written = (bootdir / "otp-imgcheck-credential").read_text().strip()
    assert written == "", written


def test_a_record_that_never_reached_the_card_fails_boot1(tmp_path):
    """
    The read-back in the positive control, which is what makes it a control.

    The record is the ONE thing boot2's claim rests on that the image did not
    write, so boot1 has to say it landed rather than that it tried. A boot
    partition that is full, read-only, or has something else at that path
    leaves boot2 comparing against nothing -- and two nothings are equal.
    """
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    # Something at the path the record wants, which no `printf >` can write
    # over. Cheaper than a full partition and the same failure from the
    # probe's side.
    (bootdir / "otp-imgcheck-credential").mkdir()
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           boot1_digest=None)
    assert results(proc)["credential-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout
    # The apply itself is untouched: this is about the probe's own bookkeeping.
    assert results(proc)["userconf-seed-applied"] == "PASS", proc.stdout


def test_the_second_boot_accepts_the_password_that_outlived_the_power(tmp_path):
    """The healthy boot2 path, spelled out: the hash carries the seed's salt
    and digests to what boot1 recorded, with no seed on the card."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert results(proc)["credential-survives-the-power-cycle"] == "PASS", \
        proc.stdout


def test_a_second_boot_back_on_the_build_time_password_fails(tmp_path):
    """
    THE DEFECT, measured before this change and now a red line: boot2 came up
    with pi-gen's random FIRST_USER_PASS, which nobody has, on a device whose
    only other way in is the card.
    """
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        shadow="otp:$6$RaNdOmSaLt$buildtimebytes:20000:0:99999:7:::\n",
        boot1_digest=sha256_of("$6$otpimgcheck$hashbytes"))
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_two_absences_are_not_a_credential_that_survived(tmp_path):
    """
    The two-absence hole the review caught on the machine-id side, closed on
    this one with the same shape of clause. No hash in /etc/shadow and no
    record from boot1 compare equal, and both are empty.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           shadow="root:!:20000:0:99999:7:::\n",
                           boot1_digest=None)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_a_digest_nothing_could_compute_is_not_a_match(tmp_path):
    """
    THE MISSING COMMAND, which is what the length clauses are actually for and
    what the mutation gate caught this test's absence with.

    Every other clause survives a broken `sha256sum`: the salt is still in
    /etc/shadow, so attribution passes, and both digests come back as the
    empty string, so the equality passes too -- a boot2 certifying that the
    password survived on the strength of two things nothing computed. It is
    not a hypothetical shape either: boot1 writes whatever `cred_digest`
    returned into the record, so ONE broken sha256sum makes both sides empty
    on the same machine.

    The positive control is the same fixture with sha256sum working.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           boot1_digest="", sha256sum=False)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout
    proc, _ = run_userconf(tmp_path / "control", phase="boot2",
                           env=HEALTHY_BOOT2)
    assert results(proc)["credential-survives-the-power-cycle"] == "PASS", \
        proc.stdout


def test_the_same_password_in_both_boots_is_not_the_seeded_one(tmp_path):
    """
    ATTRIBUTION, which the digest comparison cannot supply.

    A boot2 whose hash digests to exactly what boot1 recorded has proved that
    the password did not CHANGE -- and pi-gen's build-time password does not
    change either. It is the same on every boot of a unit whose credential was
    never persisted at all, which is the image this check exists to fail. Only
    the salt says the hash in force is the one the operator seeded.
    """
    build_time = "$6$RaNdOmSaLt$buildtimebytes"
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        shadow=f"otp:{build_time}:20000:0:99999:7:::\n",
        boot1_digest=sha256_of(build_time))
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout
    # The positive control on the same fixture: swap in the seeded hash and
    # its own digest, and the check passes. Without it this is satisfied by a
    # clause that refuses everything.
    seeded = "$6$otpimgcheck$hashbytes"
    proc, _ = run_userconf(
        tmp_path / "control", phase="boot2", env=HEALTHY_BOOT2,
        shadow=f"otp:{seeded}:20000:0:99999:7:::\n",
        boot1_digest=sha256_of(seeded))
    assert results(proc)["credential-survives-the-power-cycle"] == "PASS", \
        proc.stdout


def test_a_second_boot_with_a_password_but_no_record_fails(tmp_path):
    """Half of the pair above, on its own: boot1 never wrote a record, so
    there is nothing this boot's hash can be held against."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           boot1_digest=None)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_a_password_that_could_have_come_from_a_seed_this_boot_fails(tmp_path):
    """
    The clause that makes this a statement about a POWER CYCLE.

    A userconf.txt still on the card is a boot where the wizard is armed --
    the delete failed, or the operator left the file there -- so the hash in
    /etc/shadow may have been applied a moment ago rather than restored. That
    is also the rejected "keep the seed file" option arriving by accident,
    with the operator's credential line readable in any card reader forever.
    """
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    (bootdir / "userconf.txt").write_text("otp:$6$otpimgcheck$hashbytes\n")
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_a_restored_hash_that_was_mangled_on_the_way_back_fails(tmp_path):
    """
    Attribution is not integrity. A truncated restore still begins with the
    seed's salt, so the salt clause alone would pass it -- and a truncated
    crypt string is a password nobody can type.
    """
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        shadow="otp:$6$otpimgcheck$hashbyt:20000:0:99999:7:::\n",
        boot1_digest=sha256_of("$6$otpimgcheck$hashbytes"))
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_the_probe_never_writes_the_hash_where_the_console_can_see_it(tmp_path):
    """
    This console is uploaded as a CI artifact, and a crypt string on it is a
    crypt string anyone who downloads the run can attack at leisure -- for as
    long as they like, against a hash the operator may have reused elsewhere.
    What travels between the boots is a sha256 of it, and what is printed is
    twelve characters of that.
    """
    secret = "$6$otpimgcheck$hashbytes"
    for phase, env in (("boot1", HEALTHY_BOOT1), ("boot2", HEALTHY_BOOT2)):
        proc, bootdir = run_userconf(tmp_path / phase, phase=phase, env=env)
        assert secret not in proc.stdout, proc.stdout
        assert "hashbytes" not in proc.stdout, proc.stdout
        # And the record the probe itself leaves on the card is the digest,
        # not the hash. The store beside it holds the hash in full -- that is
        # the thing under test -- so this file must add nothing to it.
        record = (bootdir / "otp-imgcheck-credential").read_text().strip()
        assert record == sha256_of(secret), record
    # The positive control: the digest IS in the output, so "the hash is
    # absent" is not satisfied by a probe that printed nothing at all.
    assert sha256_of(secret)[:12] in proc.stdout, proc.stdout


def test_a_masked_wizard_is_quiet_for_the_wrong_reason(tmp_path):
    """
    Quiet is not the property. Masking userconfig.service also stops the
    prompt -- an earlier fix did exactly that -- and it silently discards
    every credential an operator ever writes to userconf.txt, because that
    unit is the file's only consumer. With no SSH and no network, the tty2
    recovery getty is then a login nobody can pass.
    """
    for state in ("masked", "disabled"):
        proc, _ = run_userconf(tmp_path / state, phase="boot2",
                               env={**HEALTHY_BOOT2, "UC_ENABLED": state})
        assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", \
            proc.stdout


def test_a_condition_systemd_never_evaluated_is_not_a_skip(tmp_path):
    # An empty ConditionTimestamp is "systemd has not looked at this unit",
    # which is what a probe that ran too early sees -- and it is
    # indistinguishable from a skip in every other field.
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_TS": ""})
    assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", proc.stdout


def test_a_second_boot_whose_condition_is_still_true_fails(tmp_path):
    """
    The property this check is NAMED for, which nothing was testing.

    Every other boot2 fixture leaves the condition false, so the
    `condition = no` clause could be DELETED and the whole suite stayed
    green -- the check went on being read as "the wizard was skipped" while
    only ever measuring "the unit is enabled, evaluated, and not running".
    Those do not imply it: a unit whose condition came out TRUE is one
    systemd let start, and on a oneshot that has already finished it is
    `inactive` with an evaluation timestamp and `enabled`, exactly like a
    skip.

    On the device a true condition here means the seed OUTLIVED the boot
    that was supposed to consume it -- the delete failed, or the operator
    wrote the file again -- so the wizard is armed on this boot and on every
    boot after it, and a credential line is still sitting on a FAT partition
    anybody can read in any card reader.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_COND": "yes"})
    assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", proc.stdout
    # And the reader is told which half went wrong.
    assert "condition=yes" in proc.stdout, proc.stdout


def test_a_wizard_that_can_still_reach_a_tty_fails(tmp_path):
    # The drop-in gone, or overridden by another one: the stock unit's
    # StandardInput=tty is back and a malformed seed is a hang again.
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_STDIN": "tty"})
    assert results(proc)["userconf-wizard-cannot-prompt"] == "FAIL", proc.stdout


def hang_stub(*, ignore_term: bool) -> str:
    """A userconf-service that quarantines the seed and then never returns.

    The quarantine comes FIRST, which is the real order: upstream renames the
    bad seed and only then reaches the whiptail. Every other clause of the
    check therefore passes, so what these fixtures measure is the stopwatch
    and nothing else.

    `exec >/dev/null` before the block, so that the `sleep` this process
    leaves behind when it is killed is not still holding the pipe the probe's
    command substitution is reading -- without it the ORPHAN sets the test's
    runtime, not the timeout, and the experiment measures the harness.
    """
    return ('#!/bin/sh\n'
            + ('trap "" TERM\n' if ignore_term else '')
            + 'cat "$BOOTDIR/userconf.txt" >> "$BOOTDIR/failed_userconf.txt"\n'
              'rm -f "$BOOTDIR/userconf.txt"\n'
              'exec >/dev/null 2>&1\n'
              'sleep 30\n')


def test_a_malformed_seed_that_hangs_fails_instead_of_timing_the_boot_out(tmp_path):
    """
    The failure this experiment exists for. A userconf-service that blocks --
    a whiptail that found a terminal after all, a read on a stdin that is not
    really closed -- holds multi-user.target open on a device forever. Here it
    is bounded and reported: the probe kills it and says rc=124.
    """
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                                 service=hang_stub(ignore_term=False))
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "rc=124" in proc.stdout, proc.stdout
    # And the probe carried on to report, rather than being the hang itself.
    assert "TOTALS" in proc.stdout, proc.stdout


def test_a_hang_that_ignores_the_term_signal_also_fails(tmp_path):
    """
    The hang the check was written for, and the one it used to PASS.

    124 is what `timeout` answers when the child took its SIGTERM and died.
    A child that IGNORES SIGTERM -- a whiptail with a handler, a shell with
    `trap "" TERM`, anything wedged in uninterruptible state -- survives it
    and has to be SIGKILLed by the `-k` escalation, and then timeout answers
    128+9 = 137 instead. Against the `!= 124` form of the gate that scored

        userconf-malformed-seed-fails-fast PASS rc=137

    on a service that never returned, with the detail line printing the
    "124 = still running" gloss beside a 137. The test above passed only
    because a bare `sleep` is reaped by the first signal.

    So the gate reads `-lt 124` now, and this is the fixture that tells the
    two apart. Both fixtures are kept: this one alone would be satisfied by a
    gate that only knew about 137.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service=hang_stub(ignore_term=True))
    assert "rc=137" in proc.stdout, (
        "the fixture did not reach the -k escalation, so this test is not "
        "measuring what it says: " + proc.stdout)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "TOTALS" in proc.stdout, proc.stdout


def test_the_detail_line_does_not_gloss_a_kill_as_a_timeout(tmp_path):
    """
    What the operator reads has to survive the same correction the gate did.
    The old line said "(124 = still running at the 60s bound)" unconditionally
    -- printed next to rc=137 it names the wrong number and the wrong signal,
    and the next person debugging a red run is told the child was politely
    asked when it was actually killed.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service=hang_stub(ignore_term=True))
    detail = next(ln for ln in proc.stdout.splitlines()
                  if "userconf-malformed-seed-fails-fast" in ln)
    assert "137" in detail, detail
    assert "124 = still running" not in detail, detail


def test_the_experiment_supplies_the_conditions_the_unit_would(tmp_path):
    """
    Stdin closed and TERM removed, which is what the drop-in plus a unit
    with no tty add up to. Run it with the probe's own stdin and whiptail
    may find a terminal after all -- and then the experiment measures a
    machine nobody ships instead of the appliance.
    """
    block = userconf_block()
    line = next(ln for ln in block.splitlines() if "USERCONF_SERVICE" in ln
                and "timeout" in ln)
    assert "< /dev/null" in line, line
    assert "env -u TERM" in line, line


def test_a_malformed_seed_that_leaves_no_evidence_fails(tmp_path):
    # It ended, and it took the operator's file with it. failed_userconf.txt
    # is the only thing that tells them why they have no login.
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service='#!/bin/sh\nrm -f "$BOOTDIR/userconf.txt"\nexit 1\n')
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_a_seed_left_in_place_by_the_service_fails(tmp_path):
    # Quarantine means moved: a card that still holds the bad line prompts on
    # every boot from here on.
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        service='#!/bin/sh\ncp "$BOOTDIR/userconf.txt" "$BOOTDIR/failed_userconf.txt"\nexit 1\n')
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_an_image_without_userconf_pi_fails_rather_than_passing_silently(tmp_path):
    """
    An absent userconf-service is not a pass.

    Nothing would have been written, nothing would have failed, and both
    "userconf.txt is gone" and "the boot did not hang" would be true -- of an
    image on which the documented credential mechanism does not exist at all.
    """
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                                 service=None)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "rc=none" in proc.stdout, proc.stdout
    # And nothing was planted on a card whose machine cannot consume it.
    assert not (bootdir / "userconf.txt").exists()

    # The other way it goes missing: present, and not runnable.
    proc, _ = run_userconf(tmp_path / "not-executable", phase="boot2",
                           env=HEALTHY_BOOT2, executable=False)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_the_probe_is_given_longer_than_its_own_bounded_wait(tmp_path):
    """
    systemd's default TimeoutStartSec is 90s and the probe's own poll for
    otp-unit.service is 45 iterations of `sleep 2` -- 90s exactly, before
    cupsd -t and two Python starts under TCG. On the default, a slow but
    healthy boot loses its probe mid-poll and the phase reports nothing,
    which the harness fails for having no OTP-GUEST-DONE line. It fails red
    rather than green, so this is a flake risk and not a false pass, but the
    number is worth stating rather than inheriting.

    EVERY bounded wait, summed, not the first one. The probe grew a second
    poll and a `timeout` when the credential checks landed, and a test that
    kept reading only the unit poll would have gone on approving a budget
    against 90s of a worst case several times that. Measured as this stands:
    90s polling for otp-unit, 15s waiting for journald to index the
    forwarding marker, 60s waiting for the unit to decide it has no panel,
    120s polling for systemd's verdict on the wizard, 90s bounding the
    diagnostic sheet and 60s bounding the malformed seed -- 435s against a
    TimeoutStartSec of 480 inside a 600s per-boot backstop.

    That sum is deliberately pessimistic: the two experiments are in
    different phases, so no single boot can pay both (boot1's worst case is
    375s, boot2's 345s). Summing them anyway keeps the assertion below
    arithmetic rather than bookkeeping about which phase runs what.

    Those three numbers are prose and the assertions below are not: the sums
    are re-derived from the shipped files on every run, so a wait that
    changes size fails the arithmetic rather than the sentence. The wizard
    poll was 30s when this paragraph was first written and is 120s now.

    Both halves are read out of the shipped files: the bounds out of the
    probe, the backstop out of the workflow.
    """
    probe = GUEST_CHECK.read_text()
    # Each `for _ in $(seq 1 N) ... done` with the `sleep S` inside it.
    poll_bound = 0
    for loop in re.finditer(r"for _ in \$\(seq 1 (\d+)\); do(.*?)\n *done",
                            probe, re.S):
        interval = re.search(r"^\s*sleep (\d+)\s*$", loop.group(2), re.M)
        assert interval, f"a poll with no sleep in it: {loop.group(0)[:80]}"
        poll_bound += int(loop.group(1)) * int(interval.group(1))
    # Plus anything the probe runs under a stopwatch of its own.
    for bound in re.finditer(r"timeout -k \d+ (\d+)", probe):
        poll_bound += int(bound.group(1))
    assert poll_bound >= 90, "the probe's bounded waits went missing"

    unit = IMGCHECK_UNIT.read_text()
    match = re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M)
    assert match, "otp-unit-imgcheck.service has no explicit TimeoutStartSec"
    timeout = int(match.group(1))
    assert timeout > poll_bound, (
        f"TimeoutStartSec={timeout}s does not clear the probe's own "
        f"{poll_bound}s poll, so a slow boot loses its report")

    workflow = (REPO / ".github" / "workflows" / "image.yml").read_text()
    backstop = int(re.search(r"OTP_IMG_TIMEOUT:\s*(\d+)", workflow).group(1))
    assert timeout < backstop, (
        f"TimeoutStartSec={timeout}s is not inside the {backstop}s per-boot "
        f"backstop, so a wedged probe eats the run instead of being killed")


def test_the_probe_checks_the_sentinel_before_it_writes_one():
    """
    Ordering is the whole test in boot2: reading the sentinel after writing
    it would report a survival every time, on a card that discards
    perfectly. The read is at the top of the script for that reason.
    """
    text = GUEST_CHECK.read_text()
    read_at = text.index('if [ -e "$SENTINEL" ]; then')
    write_at = text.index("> \"$SENTINEL\" 2>/dev/null")
    assert read_at < write_at, (
        "the sentinel is written before it is read back, so "
        "root-writes-discarded-by-the-power-cycle can never fail")


def test_the_probe_states_two_presences_before_it_asserts_an_absence():
    """
    "The file is gone" is satisfied by a boot1 that never ran, by a path
    that does not exist and by a rig that cannot write to / at all. Both
    controls are reported before it, with the same fixture: a sentinel
    written this boot and read back, and the setting boot1 persisted read
    back through config.load().
    """
    text = GUEST_CHECK.read_text()
    absence = text.index("check root-writes-discarded-by-the-power-cycle")
    for control in ("check root-write-lands-and-is-readable",
                    "check settings-survive-the-power-cycle"):
        assert text.index(control) < absence, control

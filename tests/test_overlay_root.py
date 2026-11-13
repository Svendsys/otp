"""The read-only root overlay: what install.sh does, and what proves it.

install.sh ships its own initramfs overlay script (issue #9) rather than
calling raspi-config, whose overlayroot package needs a `mount --move` that
klibc's mount lacks. These tests run the shipped blocks rather than
pattern-matching them, sliced where their paths are still variables.
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

# Shape of a generated machine-id: 32 lower-case hex characters.
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
    # Stubbed so a test never touches the host; it records its arguments so a
    # test can tell that a command was issued.
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
    # Everything else survives: losing root= would be an unbootable card.
    assert "root=PARTUUID=1a2b3c4d-02" in after
    assert "rootfstype=ext4" in after


def test_adding_the_token_twice_does_not_add_it_twice(tmp_path):
    # install.sh is documented as safe to rerun.
    once = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    twice = cmdline_after(tmp_path, once)
    assert twice.count("boot=overlay") == 1, twice


def test_the_first_boot_resize_token_is_removed(tmp_path):
    """The `resize` token is removed: an online resize cannot grow a read-only
    lower layer, and a failing first-boot service is a reboot loop."""
    after = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    assert " resize" not in after and not after.endswith("resize\n"), after
    # Not a blanket substring deletion: the neighbouring tokens survive.
    assert "fsck.repair=yes" in after
    assert "rootwait" in after


def test_systemds_own_grower_is_masked_as_well_as_pi_gens(tmp_path):
    """systemd-growfs-root.service is masked, not disabled: a generator hooks
    it onto the root mount, so there is no symlink for `disable` to remove, and
    it cannot succeed on an overlay root."""
    cmdline_after(tmp_path, PI_GEN_CMDLINE)
    calls = systemctl_calls(tmp_path)
    assert "mask systemd-growfs-root.service" in calls, calls
    # The pi-gen grower is still disabled as well.
    assert any(c.startswith("disable rpi-resize.service") for c in calls), calls


# --- building the initramfs the overlay lives in --------------------------

def kernels_guard_block() -> str:
    """The empty-kernel-list guard, ending at its own `fi`.

    The end is computed so an edit inside the block fails behaviourally rather
    than breaking the slice.
    """
    text = INSTALL.read_text()
    start = text.index('    if [ -z "$KERNELS" ]; then')
    return text[start:text.index("\n    fi", start) + len("\n    fi")]


def initramfs_build_block() -> str:
    """The per-kernel build loop, ending at its `done`.

    The update-initramfs line is not quoted, so a change to it fails an
    assertion about what was run.
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
    """`-c`, not `-u`: pi-gen sets update_initramfs=no, which update-initramfs
    honours on its update path only, so `-u` exits 0 and leaves the overlay
    script out of the initramfs."""
    kernels = "6.12.96+rpt-rpi-v8 6.12.96+rpt-rpi-2712"
    proc, argv = run_initramfs_build(tmp_path, kernels)
    assert proc.returncode == 0, proc.stderr
    # Every installed kernel: the image carries a second one for the Pi 5.
    assert argv.split() and argv.splitlines() == [
        f"-c -k {kern}" for kern in kernels.split()], argv


def test_no_kernel_under_lib_modules_stops_provisioning(tmp_path):
    """An empty kernel list stops provisioning: with no initramfs the overlay
    cannot be enabled.

    KERNELS is set rather than discovered, since discovery reads the test
    machine's /lib/modules.
    """
    proc = run_block(kernels_guard_block(), tmp_path, preamble='KERNELS=""')
    assert proc.returncode != 0, proc.stdout
    assert "no kernel found" in proc.stderr, proc.stderr


def test_a_kernel_list_passes_the_guard(tmp_path):
    # Positive control: a guard that stopped everything would pass the test
    # above.
    proc = run_block(kernels_guard_block(), tmp_path,
                     preamble='KERNELS=" 6.12.96+rpt-rpi-v8"')
    assert proc.returncode == 0, proc.stderr


# --- the verification that decides whether provisioning fails -------------

VERIFY_BLOCK = ('    OVERLAY_INITRAMFS=""',
                '    log "  overlay initramfs: $OVERLAY_INITRAMFS"')


# A real-sized listing with scripts/overlay early and a long tail: under
# pipefail, a check that pipes lsinitramfs into `grep -q` dies of SIGPIPE (rc
# 141) only while the producer is still writing.
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
    # The listing is the stub: what matters is what install.sh does with it.
    (bin_dir / "lsinitramfs").write_text(
        "#!/bin/sh\ncat %s\n" % (tmp_path / "listing.txt"))
    (bin_dir / "lsinitramfs").chmod(0o755)
    (tmp_path / "listing.txt").write_text(listing)
    block = slice_between(INSTALL.read_text(), *VERIFY_BLOCK)
    return run_block(block, tmp_path,
                     preamble=f'BOOT_DIR="{boot}"\n'
                              f'CMDLINE_TXT="{boot}/cmdline.txt"')


def test_a_good_initramfs_passes_the_verification(tmp_path):
    # Positive control: everything below asserts that provisioning stops.
    proc = run_verification(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "initramfs8" in proc.stdout, proc.stdout


def test_no_initramfs_at_all_fails_the_install(tmp_path):
    proc = run_verification(tmp_path, initramfs_names=())
    assert proc.returncode != 0
    assert "scripts/overlay" in proc.stderr, proc.stderr


def test_an_initramfs_without_the_overlay_script_fails_the_install(tmp_path):
    """The check reads the archive rather than stat()ing the file: an initramfs
    without the overlay script has the right name and size, and the unit would
    silently come up on a writable root."""
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
    # An overlay whose lower layer is writable still writes to the SD card.
    lower = [line for line in overlay_script().splitlines()
             if "/lower" in line and "mount " in line]
    assert lower, "nothing in the overlay script mounts a lower layer"
    assert all(" -r " in line for line in lower), lower


def test_a_failed_overlay_panics_rather_than_booting_writable():
    """A failed overlay panics: a unit that quietly booted without it would
    keep the session on the card and look identical from outside."""
    script = overlay_script()
    assert "panic \"Failed to assemble the root overlay.\"" in script
    assert "panic \"Failed to mount the overlay's upper layer in RAM.\"" in script


def test_install_sh_no_longer_tells_the_operator_to_run_raspi_config():
    """install.sh gives no raspi-config advice: on trixie enable_overlayfs
    installs overlayroot, whose `mount --move` klibc's mount lacks, so the unit
    stops booting."""
    text = INSTALL.read_text()
    instructions = [line for line in text.splitlines()
                    if "enable_overlayfs" in line and not line.lstrip().startswith("#")]
    assert not instructions, instructions


# --- the one file the overlay lets through --------------------------------

MACHINE_ID_FN = ("otp_restore_machine_id()\n{", "\treturn 0\n}")

# Stands in for the initrd's mount: the test lays the fake card's contents at
# the mountpoint, so only the function's own logic is exercised.
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
    # The device the first candidate resolves to: `${ROOT%p[0-9]}p1` turns
    # .../mmcblk0p2 into .../mmcblk0p1.
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
    """Positive control: the stored id lands inside the assembled overlay,
    where PID 1 reads /etc/machine-id before any unit runs."""
    proc, restored = run_machine_id_restore(tmp_path)
    assert "rc=0" in proc.stdout, proc.stderr
    assert restored == LIVE_ID, proc.stdout


def test_the_identity_partition_is_mounted_read_only(tmp_path):
    """Reading a machine-id needs no writable handle on the card, so every
    mount is read-only."""
    run_machine_id_restore(tmp_path)
    calls = (tmp_path / "mount.log").read_text().splitlines()
    mounts = [c for c in calls if not c.startswith("umount")]
    assert mounts, "the function never tried to mount anything"
    assert all(" -r " in f" {c} " for c in mounts), mounts


def test_a_machine_id_that_is_not_32_hex_characters_is_not_written(tmp_path):
    """Anything not exactly 32 hex characters is dropped: systemd rejects a
    malformed id and treats the boot as a first boot."""
    for bad in ("uninitialized", "", "deadbeef", LIVE_ID + "0", LIVE_ID[:-1] + "Z"):
        proc, restored = run_machine_id_restore(tmp_path / f"bad-{len(bad)}-{bad[:4]}",
                                                stored=bad)
        assert "rc=0" in proc.stdout, proc.stderr
        assert restored is None, f"{bad!r} was written as a machine-id"


def test_a_card_with_no_stored_identity_leaves_the_boot_alone(tmp_path):
    """A missing identity is never fatal: unlike a writable root, a first boot
    is harmless."""
    proc, restored = run_machine_id_restore(tmp_path, make_store=False)
    assert "rc=0" in proc.stdout, proc.stderr
    assert restored is None


def test_a_partition_that_will_not_mount_leaves_the_boot_alone(tmp_path):
    proc, restored = run_machine_id_restore(tmp_path, mountable=False)
    assert "rc=0" in proc.stdout, proc.stderr
    assert restored is None


def test_the_overlay_restores_the_machine_id_after_it_is_assembled(tmp_path):
    """The restore runs after the overlay is mounted: the write goes through
    the overlay into its upper layer, and an earlier call would write to the
    initrd's root and vanish at pivot."""
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

# The stub answers findmnt out of the environment, so a test can put the store
# inside the overlay without two real filesystems.
IDENTITY_FINDMNT_STUB = """#!/bin/sh
case "$*" in
    *--target*) printf '%s\\n' "${FAKE_STORE_SRC-/dev/mmcblk0p1}" ;;
    *" /") printf '%s\\n' "${FAKE_ROOT_SRC-overlay}" ;;
    *) echo "stub: unexpected findmnt $*" >&2; exit 64 ;;
esac
"""


# Stands in for chpasswd, which would edit the host's /etc/shadow: it rewrites
# field 2 of the named account in the fake tree and records its argv.
# CHPASSWD_APPLY=no makes it exit 0 having done nothing.
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

# What pi-gen leaves in /etc/shadow before any seed: a random password nobody
# has.
BUILD_TIME_HASH = "$6$aVJ7yQ2mKp0xRt4d$" + "B" * 43
# The hash an operator's own userconf.txt carried, applied by the wizard.
SEEDED_HASH = "$6$otpimgcheck$" + "A" * 86


def run_persist_identity(tmp_path, *, machine_id=LIVE_ID, stored_id=None,
                         etc_keys=("ed25519",), store_src="/dev/mmcblk0p1",
                         credential=None, partial=None,
                         live_hash=BUILD_TIME_HASH,
                         first_user="otp", mode=None, chpasswd_env=None):
    """Run the shipped script against a fake boot partition and a fake /etc.

    `etc_keys` puts host keys in the fake /etc/ssh, so "nothing copies them" is
    tested against a tree that has them. `credential` is what the card holds,
    `live_hash` what /etc/shadow holds for the UID-1000 account, and `partial`
    a `credential.new` fragment left by an interrupted record.
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
    if partial is not None:
        (boot / "otp-identity" / "credential.new").write_text(partial)
    for name in etc_keys:
        (root / "etc" / "ssh" / f"ssh_host_{name}_key").write_text(f"live {name}\n")
        (root / "etc" / "ssh" / f"ssh_host_{name}_key.pub").write_text(f"pub {name}\n")

    # A root account is present so "only the UID-1000 user" has another account
    # to name.
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
    """Run the script against a tree that already exists, so a test can boot
    the same fake unit more than once."""
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


def power_cycle(root, *, user="otp", live_hash=BUILD_TIME_HASH):
    """What the overlay hands the next boot: /etc back out of the image.

    A power cycle discards all of /etc, including any rename userconf-pi
    performed; only the FAT partition survives.
    """
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
        f"{user}:x:1000:1000:,,,:/home/{user}:/bin/bash\n")
    (root / "etc" / "shadow").write_text(
        "root:!:20088:0:99999:7:::\n"
        "daemon:*:20088:0:99999:7:::\n"
        f"{user}:{live_hash}:20088:0:99999:7:::\n")


def card_note(boot) -> str:
    """The refusal persist-identity.sh leaves where the card can carry it."""
    note = boot / "otp-identity" / "credential-not-restored.txt"
    return note.read_text() if note.exists() else ""


def test_a_first_boot_records_what_it_has(tmp_path):
    # Positive control: an empty store comes out holding this machine's
    # identity.
    proc, boot, _ = run_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "machine-id").read_text().strip() == LIVE_ID


def test_no_private_key_is_written_to_the_boot_partition(tmp_path):
    """No SSH host key reaches the boot partition, nor a directory to hold one:
    the appliance never runs sshd, and the partition is readable by every local
    account."""
    proc, boot, _ = run_persist_identity(
        tmp_path, etc_keys=("ed25519", "rsa", "ecdsa"))
    assert proc.returncode == 0, proc.stderr
    landed = [p for p in boot.rglob("*") if p.is_file()]
    assert [p.name for p in landed] == ["machine-id"], landed
    assert not (boot / "otp-identity" / "ssh").exists(), \
        "the store still has somewhere to put private keys"
    # Nor may the script read them. Comments are stripped first, since the
    # header mentions the host-key service.
    code = "\n".join(line for line in IDENTITY_SH.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    for gone in ("ssh_host", "ssh-keygen", "SSH_STORE"):
        assert gone not in code, \
            f"persist-identity.sh still handles SSH host keys: {gone}"


def test_the_unit_that_runs_it_no_longer_orders_itself_around_sshd():
    """The unit carries no ordering around sshd or the host-key service:
    nothing here writes /etc and no sshd starts, so those edges would be
    uncheckable."""
    unit = IDENTITY_UNIT.read_text()
    body = "\n".join(line for line in unit.splitlines()
                     if not line.startswith("#"))
    for gone in ("After=regenerate_ssh_host_keys.service",
                 "Before=ssh.service", "ConditionPathIsReadWrite="):
        assert gone not in body, f"{gone} outlived the half it existed for"


def test_the_unit_still_runs_before_anything_that_reads_the_identity():
    """Positive control for the test above: the unit still runs at sysinit,
    before anything reads the identity."""
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
    """A store on the overlay's tmpfs is refused: if /boot/firmware never
    mounted, every copy would agree with its original and nothing would
    survive."""
    proc, boot, _ = run_persist_identity(tmp_path, store_src="overlay")
    assert proc.returncode != 0, proc.stdout
    assert not (boot / "otp-identity" / "machine-id").exists(), \
        "it wrote into the overlay anyway"


def test_a_kept_machine_id_is_not_overwritten_by_a_boot_that_lost_it(tmp_path):
    """A stored id that differs from the running one is kept: the mismatch
    means the initramfs restore did not happen, and overwriting would make the
    store chase a value that changes every boot."""
    other = "ffffffffffffffffffffffffffffffff"
    proc, boot, _ = run_persist_identity(tmp_path, stored_id=other)
    assert proc.returncode != 0, proc.stdout
    assert (boot / "otp-identity" / "machine-id").read_text().strip() == other


# --- the operator's login, kept across power cycles -----------------------
#
# Each guard below catches a silent failure: a store on tmpfs, a truncated line
# that would leave the account passwordless, a chpasswd that exits 0 having
# written nothing, a hash on the card of a unit nobody seeded.


def test_a_seeded_password_is_kept_and_comes_back_after_the_power_cycle(tmp_path):
    """The whole loop, and the positive control for the negatives below: the
    wizard's hash is recorded on boot 1 and restored over a fresh /etc on boot
    2."""
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
    # Through chpasswd, which takes the lock, and against the tree the script
    # was pointed at.
    assert any(f"-e --root {tmp_path}/root" in line
               for line in chpasswd_argv(tmp_path)), chpasswd_argv(tmp_path)


def test_an_unseeded_boot_puts_no_password_hash_on_the_boot_partition(tmp_path):
    """A unit nobody seeded never has a password hash on its boot partition;
    only --record-credential writes one.

    The second half is the positive control: the recording phase on the same
    tree must land the hash.
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
    """A truncated store is refused: `chpasswd -e` would write an empty field
    verbatim, leaving an account that logs in with no password. The complete
    line on the same fixture is the positive control."""
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
    """Locked forms (`!`, `*`) and runts are refused: a store carrying one
    would take away the operator's login."""
    proc, _, root = run_persist_identity(tmp_path, credential=f"otp:{kept}\n")
    assert proc.returncode != 0, proc.stdout
    assert shadow_hash(root) == BUILD_TIME_HASH
    # It never echoes what it refused: stderr reaches the journal and the
    # uploaded tier-3 console.
    assert kept not in proc.stderr or kept == "", proc.stderr


def test_a_zero_length_store_is_not_read_as_a_card_that_was_never_seeded(tmp_path):
    """A zero-length `credential` fails the unit instead of reading as a fresh
    card: vfat has no atomic rename, so an interrupted record can leave one
    where a working credential was."""
    proc, boot, root = run_persist_identity(tmp_path, credential="")
    assert proc.returncode != 0, proc.stdout
    assert shadow_hash(root) == BUILD_TIME_HASH
    assert chpasswd_argv(tmp_path) == [], chpasswd_argv(tmp_path)
    # It must not claim the card was never seeded.
    assert "no credential kept" not in proc.stderr, proc.stderr

    # Positive control: a card that really was never seeded stays quiet and
    # green.
    proc, boot, _ = run_persist_identity(tmp_path / "fresh")
    assert proc.returncode == 0, proc.stderr
    assert "no credential kept" in proc.stderr, proc.stderr
    assert card_note(boot) == ""


def test_a_record_fragment_with_no_store_beside_it_is_not_a_fresh_card(tmp_path):
    """A `credential.new` with no `credential` beside it is an interrupted
    first record, not a fresh card, and fails the unit."""
    proc, boot, root = run_persist_identity(
        tmp_path, partial=f"otp:{SEEDED_HASH}\n")
    assert proc.returncode != 0, proc.stdout
    assert "no credential kept" not in proc.stderr, proc.stderr
    # The fragment is never applied: its contents may be half a line.
    assert chpasswd_argv(tmp_path) == [], chpasswd_argv(tmp_path)
    assert shadow_hash(root) == BUILD_TIME_HASH


def test_a_record_fragment_beside_a_good_store_is_cleared_and_the_store_wins(tmp_path):
    """A fragment beside an intact store is cleared and the store is restored
    normally, so later boots do not keep reporting the interruption."""
    proc, boot, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\n",
        partial="otp:$6$halfwritten$")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH, \
        "the fragment cost the operator the credential that was committed"
    assert not (boot / "otp-identity" / "credential.new").exists()
    assert "credential.new" in proc.stderr, proc.stderr


def test_every_refusal_is_left_on_the_card_a_locked_out_operator_can_read(tmp_path):
    """Each refusal is also written to the card, without the hash: the journal
    is volatile, and a locked-out operator can only read the card in another
    machine. A restore that works clears the note."""
    proc, boot, _ = run_persist_identity(tmp_path, credential="otp:!\n")
    assert proc.returncode != 0, proc.stdout
    note = card_note(boot)
    assert "userconf.txt" in note, note
    assert "crypt(3) string" in note, note
    # The reason is the journal's own lines under a three-line header, so the
    # two channels cannot drift apart.
    header, _, reason = note.partition("\n\n")
    assert len(header.splitlines()) == 3, note
    assert reason.strip(), note
    for line in reason.splitlines():
        assert f"otp-identity: {line}" in proc.stderr, (line, proc.stderr)
    # Never the hash: the partition is readable by every local account.
    assert SEEDED_HASH not in note and BUILD_TIME_HASH not in note

    # A restore that works clears the note; the appliance has no clock to date
    # a stale one.
    (boot / "otp-identity" / "credential").write_text(f"otp:{SEEDED_HASH}\n")
    proc = invoke_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert card_note(boot) == "", "the refusal outlived the fault"


def test_the_operator_documentation_says_which_account_the_login_comes_back_on():
    """docs/IMAGE.md names the account the kept login comes back on, qualifies
    the precedence row by UID 1000, and names the note file the script writes.
    """
    doc = (REPO / "docs" / "IMAGE.md").read_text()
    first_user = re.search(r"^FIRST_USER_NAME='([^']+)'",
                           (REPO / "image" / "build.sh").read_text(), re.M)
    assert first_user, "image/build.sh no longer sets FIRST_USER_NAME"
    assert f"log in as `{first_user.group(1)}`" in doc, \
        "docs/IMAGE.md does not say which account the kept password lands on"
    row = next((line for line in doc.splitlines()
                if line.startswith("| a kept credential only")), "")
    assert "UID-1000" in row, \
        f"the precedence table still promises the kept credential " \
        f"unconditionally: {row!r}"
    # The note is named by the filename the script writes.
    note = re.search(r'^CRED_NOTE="\$STORE/(\S+)"', IDENTITY_SH.read_text(), re.M)
    assert note, "persist-identity.sh no longer leaves a refusal on the card"
    assert note.group(1) in doc, \
        f"docs/IMAGE.md does not tell an operator to read {note.group(1)}"


def test_the_identity_units_notes_reach_the_console_and_not_only_the_journal():
    """The identity unit's stderr goes to journal+console: the journal is
    volatile and not forwarded on a shipped unit, so the notes would otherwise
    be unreadable."""
    unit = IDENTITY_UNIT.read_text()
    assert "StandardError=journal+console" in unit, unit
    # Scoped to this unit: machine-wide forwarding would put every unit's
    # output on the console of a device that prints key material.
    for shipped in (REPO / "device" / "install.sh", REPO / "image" / "build.sh"):
        assert "forward_to_console" not in shipped.read_text(), shipped


def test_the_store_may_only_ever_set_the_uid_1000_accounts_password(tmp_path):
    """The store can only set the UID-1000 account's password, the one account
    userconf.txt can reach.

    The name in the store is a label: a card saying `root:` still applies its
    hash to UID 1000 and leaves root untouched.
    """
    proc, boot, root = run_persist_identity(
        tmp_path, credential=f"root:{SEEDED_HASH}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root, "root") == "!", "root's password was set"
    assert shadow_hash(root) == SEEDED_HASH, \
        "the kept hash did not reach the UID-1000 account"
    # chpasswd ran, and the store is relabelled for the live account.
    assert chpasswd_argv(tmp_path), "chpasswd never ran"
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{SEEDED_HASH}\n", "the store kept the label it could not use"

    # The name quoted back is stripped first: it comes off a partition anyone
    # can write and ends up on a console.
    proc, _, _ = run_persist_identity(
        tmp_path / "escape",
        credential="ro\x1b[2Jot:" + SEEDED_HASH + "\n")
    assert proc.returncode == 0, proc.stderr
    for gone in ("\x1b", "["):
        assert gone not in proc.stderr, repr(proc.stderr)
    # Positive control: what is left of the name is still reported.
    assert "'ro2Jot'" in proc.stderr, proc.stderr

    # With no UID-1000 account the store is refused and chpasswd never runs,
    # even with `root` there to pick.
    proc, _, root = run_persist_identity(tmp_path / "nobody", first_user=None,
                                         credential=f"root:{SEEDED_HASH}\n")
    assert proc.returncode != 0, proc.stdout
    assert shadow_hash(root, "root") == "!", "root's password was set"
    assert chpasswd_argv(tmp_path / "nobody") == [], \
        chpasswd_argv(tmp_path / "nobody")


def test_a_store_naming_the_pre_rename_account_still_logs_the_operator_in(tmp_path):
    """A store naming a pre-rename account still restores the login:
    userconf-pi's rename of the UID-1000 user dies with the overlay while the
    store survives, so the hash goes to whoever holds UID 1000 and the store is
    relabelled."""
    # Boot 1: the seed names `pi`, userconf-pi renames the account, and the
    # record keeps what is live.
    proc, boot, root = run_persist_identity(tmp_path, first_user="pi",
                                            live_hash=SEEDED_HASH)
    assert proc.returncode == 0, proc.stderr
    proc = invoke_persist_identity(tmp_path, mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"pi:{SEEDED_HASH}\n", "the record did not keep the renamed account"

    # The power cycle: UID 1000 is `otp` again; the card still holds
    # `pi:<hash>`.
    power_cycle(root)
    assert "pi:" not in (root / "etc" / "passwd").read_text()

    proc = invoke_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    # The operator's hash is on the account they can actually type at the
    # prompt.
    assert shadow_hash(root) == SEEDED_HASH, \
        "the operator is locked out: the kept password did not reach otp"
    # Through chpasswd, for the live account.
    assert any(f"-e --root {tmp_path}/root" in line
               for line in chpasswd_argv(tmp_path)), chpasswd_argv(tmp_path)
    # It says so, so the re-pointing can be audited.
    assert "'pi'" in proc.stderr, proc.stderr

    # The store is rewritten, or every later boot repeats the mismatch.
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{SEEDED_HASH}\n"
    assert not (boot / "otp-identity" / "credential.new").exists()
    assert card_note(boot) == "", "a working restore left a refusal on the card"

    # The next boot is ordinary: nothing to say, nothing to rewrite.
    power_cycle(root)
    proc = invoke_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH
    assert "'otp'" not in proc.stderr, proc.stderr


def test_a_stale_label_is_rewritten_even_when_the_password_already_matches(tmp_path):
    """A stale label is rewritten even when the hash already matches and
    chpasswd has nothing to do; otherwise a unit that once failed to relabel
    never gets another chance."""
    proc, boot, root = run_persist_identity(
        tmp_path, credential=f"pi:{SEEDED_HASH}\n", live_hash=SEEDED_HASH)
    assert proc.returncode == 0, proc.stderr
    assert chpasswd_argv(tmp_path) == [], "chpasswd ran for a password that was already right"
    assert shadow_hash(root) == SEEDED_HASH
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{SEEDED_HASH}\n", "the stale label survived the boot"


def test_a_rewrite_the_card_will_not_take_does_not_cost_the_login(tmp_path):
    """A card that refuses the store rewrite does not cost the login: the
    failure is reported and the exit status still describes the restore.

    A directory in place of `credential.new` simulates the full or read-only
    card, since the suite runs as root and chmod stops nothing.
    """
    proc, boot, root = run_persist_identity(
        tmp_path, credential=f"pi:{SEEDED_HASH}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"otp:{SEEDED_HASH}\n"

    # Same tree, same mismatched store, with nothing able to write the store.
    (boot / "otp-identity" / "credential").write_text(f"pi:{SEEDED_HASH}\n")
    (boot / "otp-identity" / "credential.new").mkdir()
    power_cycle(root)
    proc = invoke_persist_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH, \
        "a store that could not be rewritten cost the operator the login"
    assert "could not rewrite" in proc.stderr, proc.stderr
    # The store keeps what it held: no half-finished rewrite.
    assert (boot / "otp-identity" / "credential").read_text() \
        == f"pi:{SEEDED_HASH}\n"


def test_the_uid_1000_account_is_found_by_uid_and_not_by_name(tmp_path):
    """The account is found by UID 1000, not by the name `otp`: matching the
    string would silently restore nothing on a renamed image."""
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
    """A two-line store is refused: chpasswd applies every line it is handed,
    and the partition is writable by anyone with a card reader."""
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\nroot:{SEEDED_HASH}\n")
    assert proc.returncode != 0, proc.stdout
    assert chpasswd_argv(tmp_path) == [], chpasswd_argv(tmp_path)
    assert shadow_hash(root) == BUILD_TIME_HASH
    assert shadow_hash(root, "root") == "!"


def test_a_chpasswd_that_exits_zero_without_writing_is_caught(tmp_path):
    """The restore reads /etc/shadow back: chpasswd's exit status says the
    command ran, not that the password is in place."""
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\n",
        chpasswd_env={"CHPASSWD_APPLY": "no"})
    assert proc.returncode != 0, proc.stdout
    assert "does not hold the" in proc.stderr, proc.stderr
    assert shadow_hash(root) == BUILD_TIME_HASH
    # Positive control: the same store with a chpasswd that does its job.
    proc, _, root = run_persist_identity(tmp_path / "control",
                                         credential=f"otp:{SEEDED_HASH}\n")
    assert proc.returncode == 0, proc.stderr
    assert shadow_hash(root) == SEEDED_HASH


def test_a_chpasswd_that_fails_is_not_reported_as_a_restore(tmp_path):
    """A failing chpasswd fails the restore; the script has no `set -e`, so its
    status must be read explicitly."""
    proc, _, root = run_persist_identity(
        tmp_path, credential=f"otp:{SEEDED_HASH}\n",
        chpasswd_env={"CHPASSWD_APPLY": "no", "CHPASSWD_RC": "1"})
    assert proc.returncode != 0, proc.stdout
    assert "could not put" in proc.stderr, proc.stderr
    assert shadow_hash(root) == BUILD_TIME_HASH


def test_neither_phase_writes_a_credential_into_the_overlay(tmp_path):
    """The recording phase also refuses a store on the overlay's tmpfs, where
    record and restore would agree on every boot while nothing survived."""
    proc, boot, _ = run_persist_identity(tmp_path, store_src="overlay",
                                         mode="--record-credential")
    assert proc.returncode != 0, proc.stdout
    assert not (boot / "otp-identity" / "credential").exists()
    # Positive control: the same phase with the store on the card.
    proc, boot, _ = run_persist_identity(tmp_path / "control",
                                         mode="--record-credential")
    assert proc.returncode == 0, proc.stderr
    assert (boot / "otp-identity" / "credential").exists()


def test_a_locked_account_is_not_recorded_over_a_good_credential(tmp_path):
    """A locked account (`!`) is not recorded: it would replace a working
    credential with one the restore must refuse."""
    kept = f"otp:{SEEDED_HASH}\n"
    proc, boot, _ = run_persist_identity(tmp_path, credential=kept,
                                         live_hash="!",
                                         mode="--record-credential")
    assert proc.returncode != 0, proc.stdout
    assert (boot / "otp-identity" / "credential").read_text() == kept


def test_a_fresh_seed_this_boot_beats_the_credential_that_was_restored(tmp_path):
    """Precedence is fresh seed, then store, then the build-time password: the
    wizard applies a new seed over the sysinit restore and its ExecStartPost
    replaces the store, so the next boot restores the new password rather than
    the old."""
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
    """The restore runs Before=sysinit.target, before any getty exists, and
    needs no initramfs hook because nothing reads /etc/shadow before PID 1
    starts units."""
    unit = IDENTITY_UNIT.read_text()
    assert "Before=sysinit.target" in unit, unit
    assert "ExecStart=/opt/otp-unit/persist-identity.sh" in unit
    # The restore is the default phase: a unit passing --record-credential
    # would write the build-time hash to the card and restore nothing.
    assert "--record-credential" not in unit, unit
    code = IDENTITY_SH.read_text()
    assert "/scripts/overlay" not in code
    assert "shadow" in code, "the script no longer touches the credential"


# --- and the lines that put it on the machine ------------------------------
#
# The tests above drive persist-identity.sh directly; these check that
# install.sh ships and enables it.

IDENTITY_INSTALL_BLOCK = (
    '    install -m 0755 "$REPO_DIR/device/persist-identity.sh" \\',
    "    systemctl enable otp-unit-identity.service")


def run_identity_install(tmp_path, *, repo_dir=None):
    """Run install.sh's identity-install block against a substitute REPO_DIR.

    `install` is shadowed by a shell function so nothing is written to
    /etc/systemd/system or /opt/otp-unit.
    """
    block = slice_between(INSTALL.read_text(), *IDENTITY_INSTALL_BLOCK)
    return run_block(block, tmp_path,
                     preamble=("install() { printf 'INSTALL %s\\n' \"$*\"; }\n"
                               f'REPO_DIR="{repo_dir or REPO}"\n'
                               f'PREFIX="{tmp_path}/opt"'))


def test_the_identity_script_and_its_unit_are_both_installed(tmp_path):
    """Script and unit are installed to the paths the unit names: ExecStart is
    an absolute /opt/otp-unit/persist-identity.sh."""
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
    """The unit is enabled, after a daemon-reload: a unit file with no symlink
    never runs, and nothing else on the image enables it."""
    proc = run_identity_install(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = systemctl_calls(tmp_path)
    assert "enable otp-unit-identity.service" in calls, calls
    # After the unit file lands: `enable` resolves [Install] from the unit on
    # disk.
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

    Only the directory is rewritten; the file's content stays the shipped
    bytes.
    """
    etc = tmp_path / "etc"
    block = slice_between(INSTALL.read_text(), *CREDENTIAL_DROPIN_BLOCK)
    assert block.count(CREDENTIAL_DROPIN_DIR) == 2, block
    block = block.replace(CREDENTIAL_DROPIN_DIR, f"{etc}/userconfig.service.d")
    proc = run_block(block, tmp_path)
    assert proc.returncode == 0, proc.stderr
    return (etc / "userconfig.service.d" / "otp-credential.conf").read_text()


def test_the_wizard_records_the_credential_it_just_applied(tmp_path):
    """The wizard's ExecStartPost records the credential: nothing else writes
    the store, so without it the restore has nothing to restore."""
    text = run_credential_dropin(tmp_path)
    assert "ExecStartPost=" in text, text
    assert "/opt/otp-unit/persist-identity.sh --record-credential" in text, text
    # The path the identity install writes the script to.
    assert "ExecStartPost=-/opt/otp-unit/" in text, text


def test_a_record_that_fails_does_not_cost_the_operator_the_apply(tmp_path):
    """The ExecStartPost has a leading `-`: stock userconfig.service is
    Restart=on-failure, so a failed record would otherwise restart-loop the
    boot that applied the password."""
    text = run_credential_dropin(tmp_path)
    assert "ExecStartPost=-" in text, text


def test_the_credential_is_recorded_only_on_an_overlay_machine():
    """The drop-in is written only inside the overlay branch, after the script
    it names: a writable root has nothing to persist and no persist-identity.sh
    installed."""
    text = INSTALL.read_text()
    dropin = text.index("otp-credential.conf")
    identity = text.index('install -m 0755 "$REPO_DIR/device/persist-identity.sh"')
    overlay_branch = text.index('log "Enabling the read-only root overlay"')
    assert overlay_branch < identity < dropin, (overlay_branch, identity, dropin)
    # Before the branch closes, just ahead of the operator summary.
    assert dropin < text.index("# WHAT THIS SCRIPT DID TO SOMEONE ELSE'S MACHINE")


# --- ssh.socket, which breaks sshd's reload ---------------------------------

SSH_SOCKET_BLOCK = ("# --- AND THE RELOAD AT THE END OF THAT SAME SCRIPT",
                    "systemctl mask ssh.socket")


def test_the_socket_that_kept_sshd_from_rebinding_is_masked(tmp_path):
    """ssh.socket is masked: with the socket holding :22, the `reload ssh` that
    ends a seeded first boot leaves sshd dead. Masked rather than disabled
    because enable symlinks live in the overlay and a disable lasts one boot.
    """
    block = slice_between(INSTALL.read_text(), *SSH_SOCKET_BLOCK)
    proc = run_block(block, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "mask ssh.socket" in systemctl_calls(tmp_path), \
        systemctl_calls(tmp_path)


def test_ssh_service_itself_is_left_alone(tmp_path):
    """ssh.service itself is not masked: that would leave the socket listening
    for a service that can never start."""
    block = slice_between(INSTALL.read_text(), *SSH_SOCKET_BLOCK)
    run_block(block, tmp_path)
    for call in systemctl_calls(tmp_path):
        assert call != "mask ssh.service", call
        assert call != "mask ssh", call


# --- the harness end ------------------------------------------------------

def test_the_harness_copies_the_overlay_token_and_never_invents_one(tmp_path):
    """The harness copies boot=overlay from the image's own cmdline.txt and
    never adds it, so tier 3 proves the artifact rather than its own command
    line."""
    block = slice_between(IMG_BOOT.read_text(), 'OVERLAY_TOKENS=""', "esac")
    for cmdline, expected in (
        ("boot=overlay console=tty1 root=PARTUUID=1a2b-02", "boot=overlay"),
        ("console=tty1 root=PARTUUID=1a2b-02", ""),
        # Whole-token match, not substring.
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

# fdtput/fdtget come from device-tree-compiler, which the fast suite does not
# have. The stub "blob" is a text file with one line per property.
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
# Shell builtins only: the test PATH holds just the tools the block uses.
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
    # A PATH holding only the tools the block uses, so `have_fdtput=False`
    # means absent even where device-tree-compiler is installed.
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
    proc = subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(runner)],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "FDT_LOG": str(tmp_path / "fdt.log"),
             **({"FDTPUT_DEAF": "1"} if fdtput_deaf else {}),
             "PATH": str(bin_dir)})
    return proc, tmp_path / "fdt.log"


def test_the_harness_supplies_the_board_revision_the_firmware_would_have(tmp_path):
    """The harness writes /system/linux,revision into a copy of the DTB: QEMU
    is not the Pi firmware, and without it rpi-eeprom-update.service fails."""
    proc, log = run_revision_block(tmp_path)
    assert proc.returncode == 0, proc.stderr
    calls = log.read_text()
    assert "linux,revision 0xa02082" in calls, calls
    # The image's own DTB is left alone.
    assert "otp-harness-" in proc.stdout, proc.stdout


def test_a_revision_that_did_not_reach_the_blob_stops_the_boot(tmp_path):
    """The property is read back: a write that did nothing would surface as a
    failed release gate blamed on the image."""
    proc, _ = run_revision_block(tmp_path, fdtput_deaf=True)
    assert proc.returncode != 0
    assert "linux,revision" in proc.stderr, proc.stderr


def test_no_device_tree_compiler_stops_the_boot_rather_than_booting_without_it(tmp_path):
    """A missing device-tree-compiler stops the boot and names the package,
    rather than booting without the property."""
    proc, _ = run_revision_block(tmp_path, have_fdtput=False)
    assert proc.returncode != 0
    assert "device-tree-compiler" in proc.stderr, proc.stderr


def test_the_synthesised_revision_is_the_board_the_emulator_models():
    """The synthesised revision decodes to the board `-M raspi3b -m 1024`
    models; gpiozero builds its board description from these bits."""
    text = IMG_BOOT.read_text()
    code = int(shell_assignment(text, "BOARD_REVISION"), 16)
    assert (code >> 23) & 1, "new-style flag clear: rpi-eeprom-update would " \
                             "call this a pre-2016 board and skip"
    assert (code >> 12) & 0xF == 2, "processor is not BCM2837"
    assert (code >> 4) & 0xFF == 0x08, "board type is not 3 Model B"
    # 1GB, matching `-m 1024`.
    assert 256 * (2 ** ((code >> 20) & 7)) == 1024
    assert "-M raspi3b -m 1024" in text, \
        "the emulated machine moved; the revision above describes the old one"


def shell_assignment(text: str, name: str) -> str:
    """A plain NAME=value assignment, read out of a shipped script."""
    match = re.search(rf"^{name}=(\S+)$", text, re.M)
    assert match, f"{name} is gone"
    return match.group(1)


def test_the_harness_hands_the_initramfs_to_qemu():
    # Without -initrd nothing reads boot=overlay and every overlay check fails
    # through the harness's fault.
    assert '${INITRD:+-initrd "$INITRD"}' in IMG_BOOT.read_text()


def test_the_guest_probe_cannot_run_on_a_flashed_unit():
    """The probe's unit is conditioned on the kernel command-line word the
    harness builds its token from, and the release phase gets no token.

    A spelling check only: tier 3's `release` phase is what shows a flashed
    unit does not run the probe. The word is read from the unit file, so
    renaming the condition cannot leave the harness supplying a token nothing
    matches.
    """
    unit = IMGCHECK_UNIT.read_text()
    condition = re.search(r"^ConditionKernelCommandLine=(\S+)$", unit, re.M)
    assert condition, "otp-unit-imgcheck.service no longer has the condition"
    word = condition.group(1)
    assert word == "otp.imgcheck", word

    harness = IMG_BOOT.read_text()
    # The probe phases get the condition's word plus the phase; -append carries
    # only the variable, so no phase has the token forced on it.
    assert f'imgcheck_token="{word}=$phase"' in harness, harness
    append = next(line for line in harness.splitlines()
                  if line.strip().startswith("-append "))
    assert "$imgcheck_token" in append and word not in append, append
    assert re.search(r"case \"\$phase\" in\n\s*release\) ;;\n", harness), \
        "no phase gets an empty imgcheck token, so nothing boots this image " \
        "the way a flashed unit boots it"


# --- the build refuses to ship an image it cannot probe ------------------

PROBE_INSTALL_BLOCK = ('if [ -f "$REPO_DIR/harness/img-guest-check.sh" ]; then',
                       'would have nothing to report with."\nfi')

STAGE_RUN = REPO / "image" / "stage-otpunit" / "01-otpunit" / "00-run.sh"
STAGE_COPY_BLOCK = ("for item in otpunit codewords device harness",
                    'Run image/render-manual.sh before building." >&2\nfi')


def run_probe_install(tmp_path, *, repo_dir, image_build):
    """Run install.sh's probe-install block against a substitute REPO_DIR.

    `install` is shadowed by a shell function so nothing is written to
    /etc/systemd/system or /opt/otp-unit.
    """
    block = slice_between(INSTALL.read_text(), *PROBE_INSTALL_BLOCK)
    return run_block(block, tmp_path,
                     preamble=("install() { printf 'INSTALL %s\\n' \"$*\"; }\n"
                               f'REPO_DIR="{repo_dir}"\n'
                               f'PREFIX="{tmp_path}/opt"\n'
                               f"IMAGE_BUILD={image_build}"))


def test_an_image_build_without_the_probe_fails(tmp_path):
    """An image build without the probe fails rather than printing a NOTE: the
    image's overlay would be unobservable, and the miss would read as a boot
    failure in tier 3."""
    empty = tmp_path / "repo"
    (empty / "harness").mkdir(parents=True)
    proc = run_probe_install(tmp_path, repo_dir=empty, image_build=1)
    assert proc.returncode != 0, proc.stdout
    assert "nothing to report on the" in proc.stderr, proc.stderr


def test_a_hand_provisioned_machine_without_the_probe_only_says_so(tmp_path):
    # Outside an image build, a partial copy gets a working unit and a note.
    empty = tmp_path / "repo"
    (empty / "harness").mkdir(parents=True)
    proc = run_probe_install(tmp_path, repo_dir=empty, image_build=0)
    assert proc.returncode == 0, proc.stderr
    assert "NOTE" in proc.stdout, proc.stdout


def test_the_probe_is_installed_when_it_is_there(tmp_path):
    # Positive control, against the real repository.
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
    """Every listed item except `assets` must exist: a silently skipped
    `harness` yields an image whose overlay nothing can probe."""
    for missing in ALL_ITEMS:
        if missing == "assets":
            continue
        present = [i for i in ALL_ITEMS if i != missing]
        proc = run_stage_copy(tmp_path / missing, present=present)
        assert proc.returncode != 0, f"{missing} missing was accepted"
        assert missing in proc.stderr, proc.stderr


def test_the_assets_directory_stays_optional(tmp_path):
    # install.sh guards its own use of the manual PDFs; the warning is the
    # whole penalty.
    present = [i for i in ALL_ITEMS if i != "assets"]
    proc = run_stage_copy(tmp_path, present=present)
    assert proc.returncode == 0, proc.stderr
    assert "manual PDFs missing" in proc.stderr, proc.stderr

# --- and the build refuses to purge more than it meant to ----------------

# The end marker is the heredoc terminator, not the purge command, so a
# mutation of the last line fails a test rather than breaking the slice.
STAGE_PURGE_BLOCK = ("# --- cloud-init, removed rather than left switched off",
                     "\nEOF\n")

# What `apt-get -s purge` prints for the two packages alone. Any other line is
# a package that would go with them.
PURGE_SIM_CLEAN = ("Reading package lists...\n"
                   "Building dependency tree...\n"
                   "Purg cloud-init [25.2-1~bpo13+1+rpt20]\n"
                   "Purg rpi-cloud-init-mods [1:20260119]\n")

# Records its arguments, so a run that never purged can be told from one that
# did. Either call can be made to fail: a simulation that exits non-zero with
# no plan must not read as clean.
APT_GET_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$APT_LOG"
case " $* " in
    *" -s "*)
        printf '%s' "$APT_SIM"
        if [ -n "$APT_SIM_ERR" ]; then printf '%s\\n' "$APT_SIM_ERR" >&2; fi
        exit "${APT_SIM_RC:-0}"
        ;;
esac
exit "${APT_PURGE_RC:-0}"
"""


def run_stage_purge(tmp_path, *, simulation=PURGE_SIM_CLEAN, sim_rc=0,
                    sim_stderr="", purge_rc=0):
    """Run the pi-gen stage's cloud-init purge against a simulated apt.

    `on_chroot` is a shell function so the heredoc reaches the interpreter
    untouched. pi-gen's on_chroot runs bash; `sh` is used here because the
    block is POSIX and dash would catch a bashism.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "apt-get").write_text(APT_GET_STUB)
    (bin_dir / "apt-get").chmod(0o755)
    log = tmp_path / "apt.log"
    log.write_text("")
    block = slice_between(STAGE_RUN.read_text(), *STAGE_PURGE_BLOCK)
    runner = tmp_path / "purge.sh"
    runner.write_text("set -e\non_chroot() { sh; }\n" + block)
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, "APT_LOG": str(log), "APT_SIM": simulation,
             "APT_SIM_RC": str(sim_rc), "APT_SIM_ERR": sim_stderr,
             "APT_PURGE_RC": str(purge_rc),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return proc, log.read_text()


def assert_nothing_was_removed(log):
    """Every apt call in this log carried -s, so the rootfs is untouched.

    A refusal must fire before the real purge, not after the packages are gone.
    """
    assert "-s" in log, log
    assert [ln for ln in log.splitlines() if "-s" not in ln.split()] == [], log


def test_the_stage_purges_both_cloud_init_packages(tmp_path):
    """Positive control for the refusals below: cloud-init and
    rpi-cloud-init-mods are simulated, then purged. ENABLE_CLOUD_INIT=0 only
    skips pi-gen's seed files; the packages are installed regardless (issue
    #34)."""
    proc, log = run_stage_purge(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "purge -y cloud-init rpi-cloud-init-mods" in log, log
    # Simulation first, then the real removal.
    lines = [line for line in log.splitlines() if "purge" in line]
    assert len(lines) == 2, log
    assert "-s" in lines[0].split(), log
    assert "-s" not in lines[1].split(), log


def test_a_purge_that_would_take_something_else_stops_the_build(tmp_path):
    """`apt-get purge -y` removes reverse dependencies without a prompt, so a
    plan naming any other package stops the build before anything is removed.
    """
    proc, log = run_stage_purge(tmp_path, simulation=(
        PURGE_SIM_CLEAN + "Remv raspberrypi-sys-mods [20260101]\n"))
    assert proc.returncode != 0, proc.stdout
    assert "raspberrypi-sys-mods" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)


def test_the_filter_reads_a_remv_apt_would_not_print_here(tmp_path):
    """A `Remv` line also stops the build. apt re-marks every deletion as
    `Purg` under `purge`, so this arm is defence against a later apt or an edit
    to `remove`."""
    proc, log = run_stage_purge(tmp_path, simulation=(
        "Remv netplan.io [1.1.2-7]\n" + PURGE_SIM_CLEAN))
    assert proc.returncode != 0, proc.stdout
    assert "netplan.io" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)


def test_a_simulation_that_names_nothing_stops_the_build(tmp_path):
    """An empty plan stops the build: silence about unexpected packages must
    not be read as a clean plan."""
    proc, log = run_stage_purge(tmp_path, simulation="")
    assert proc.returncode != 0, proc.stdout
    assert "did not schedule" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)


def test_a_simulation_that_fails_stops_the_build(tmp_path):
    """A simulation that exits non-zero stops the build, which is what a
    package leaving the archive looks like (`E: Unable to locate package`, no
    plan)."""
    proc, log = run_stage_purge(
        tmp_path / "no-plan", simulation="Reading package lists...\n",
        sim_rc=100, sim_stderr="E: Unable to locate package rpi-cloud-init-mods")
    assert proc.returncode != 0, proc.stdout
    assert "failed in the chroot" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)

    # A clean plan with a failing status: only the status check catches this.
    proc, log = run_stage_purge(
        tmp_path / "clean-plan", sim_rc=100,
        sim_stderr="E: Sub-process returned an error code")
    assert proc.returncode != 0, proc.stdout
    assert "failed in the chroot" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)


def test_a_simulation_that_names_only_one_of_the_two_stops_the_build(tmp_path):
    """A plan naming only one of the two packages stops the build and names the
    missing one, so a partial plan cannot be mistaken for a clean one."""
    proc, log = run_stage_purge(tmp_path, simulation=(
        "Reading package lists...\n"
        "Building dependency tree...\n"
        "Purg cloud-init [25.2-1~bpo13+1+rpt20]\n"))
    assert proc.returncode != 0, proc.stdout
    assert "rpi-cloud-init-mods" in proc.stderr, proc.stderr
    assert "did not schedule" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)


def test_a_removal_whose_name_merely_contains_cloud_init_stops_the_build(tmp_path):
    """The filter is a whole-line match (`-x`): a package whose name merely
    contains cloud-init, such as cloud-initramfs-growroot, is not passed as
    expected."""
    proc, log = run_stage_purge(tmp_path, simulation=(
        PURGE_SIM_CLEAN + "Purg cloud-initramfs-growroot [0.18.debian8]\n"))
    assert proc.returncode != 0, proc.stdout
    assert "cloud-initramfs-growroot" in proc.stderr, proc.stderr
    assert_nothing_was_removed(log)


def test_a_purge_that_fails_fails_the_build(tmp_path):
    """A purge that fails must fail the build. Today that holds only because
    the purge is the heredoc's last command, so it is pinned here."""
    proc, log = run_stage_purge(tmp_path, purge_rc=1)
    assert proc.returncode != 0, proc.stdout
    # The real purge was attempted: this is not a refusal before it.
    assert [ln for ln in log.splitlines() if "-s" not in ln.split()] != [], log


def test_the_purge_runs_before_install_sh_writes_the_kill_switch():
    """The purge runs before install.sh writes its kill switch into /etc/cloud,
    so the marker does not depend on dpkg sparing unowned files."""
    text = STAGE_RUN.read_text()
    assert text.index("apt-get purge -y ${CLOUD_PACKAGES}") \
        < text.index("./device/install.sh --image-build"), \
        "the purge must run before install.sh writes /etc/cloud/cloud-init.disabled"


# --- the probe refuses to run on anything but a harness boot -------------

PHASE_GUARD_BLOCK = ('PHASE=""', "\nesac")

# What a provisioned unit boots with: boot=overlay, no `resize`, no
# otp.imgcheck.
DEVICE_CMDLINE = ("boot=overlay console=serial0,115200 console=tty1 "
                  "root=PARTUUID=1a2b3c4d-02 rootfstype=ext4 fsck.repair=yes "
                  "rootwait\n")


def run_phase_guard(tmp_path, cmdline, *, cwd=None):
    """Run the shipped phase parse and guard against a substituted cmdline.

    Only the /proc/cmdline path is rewritten; the rest is the shipped code.
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
    """Without the otp.imgcheck token the probe refuses to run: it ships
    enabled on every unit and writes a sentinel to / and a page count into
    /boot/firmware/otp-unit.conf, which is outside the overlay and permanent.
    """
    proc = run_phase_guard(tmp_path, DEVICE_CMDLINE)
    assert proc.returncode != 0, proc.stdout
    assert "REACHED-THE-CHECKS" not in proc.stdout, proc.stdout
    assert "refusing to run" in proc.stderr, proc.stderr


def probe_phases() -> list:
    """The phases the shipped guard accepts, read out of its own case arm."""
    block = slice_between(GUEST_CHECK.read_text(), *PHASE_GUARD_BLOCK)
    match = re.search(r"^\s*([a-z0-9|]+)\) ;;$", block, re.M)
    assert match, block
    return match.group(1).split("|")


def test_the_probe_runs_for_the_phases_the_harness_supplies(tmp_path):
    # Positive control: the refusals alone would pass on a script that refused
    # everything. Phase names are read from the guard's own case arm.
    accepted = probe_phases()
    assert accepted == ["boot1", "boot2"], accepted
    for phase in accepted:
        proc = run_phase_guard(
            tmp_path, f"{DEVICE_CMDLINE.strip()} otp.imgcheck={phase}\n")
        assert proc.returncode == 0, proc.stderr
        assert f"REACHED-THE-CHECKS phase={phase}" in proc.stdout, proc.stdout


def test_a_phase_that_is_not_one_of_the_two_is_refused(tmp_path):
    # A bare `otp.imgcheck` satisfies systemd's condition but names no phase.
    # `release` is the phase the harness gives no token; the guard must refuse
    # it if one ever arrives.
    for token in ("otp.imgcheck", "otp.imgcheck=", "otp.imgcheck=boot3",
                  "otp.imgcheck=release", "otp.imgcheck=unknown"):
        proc = run_phase_guard(tmp_path, f"{DEVICE_CMDLINE.strip()} {token}\n")
        assert proc.returncode != 0, token
        assert "REACHED-THE-CHECKS" not in proc.stdout, token


def test_the_command_line_is_split_but_not_glob_expanded(tmp_path):
    """The command line is split with globbing off: the unit runs as root in /,
    and an expanded `*` would let a file named otp.imgcheck=... choose the
    phase."""
    root = tmp_path / "fake-root"
    root.mkdir()
    (root / "otp.imgcheck=boot2").write_text("")
    (root / "vmlinuz").write_text("")
    proc = run_phase_guard(tmp_path, "console=tty1 * rootwait\n", cwd=root)
    assert proc.returncode != 0, proc.stdout
    assert "REACHED-THE-CHECKS" not in proc.stdout, proc.stdout
    assert "vmlinuz" not in proc.stderr, proc.stderr


def test_the_refusal_comes_before_anything_the_probe_writes():
    # Position: a guard that refuses after the first write has already changed
    # the operator's settings.
    text = GUEST_CHECK.read_text()
    guard = text.index('case "$PHASE" in')
    for later, what in (("check root-is-overlay", "the first check"),
                        ('> "$SENTINEL"', "the sentinel write"),
                        ("config.save(saved)", "the config.save() into /boot")):
        assert guard < text.index(later), what


# --- the drop-in that keeps the first-boot wizard usable ------------------

# The end anchor is the heredoc's last line: "DROPIN\n" first appears on the
# `cat <<DROPIN` line itself.
DROPIN_BLOCK = ("systemctl unmask userconfig.service",
                "systemctl enable userconfig.service 2>/dev/null || true")
DROPIN_DIR = "/etc/systemd/system/userconfig.service.d"


def run_dropin(tmp_path, *, boot_dir):
    """Write the shipped drop-in into a substituted /etc/systemd/system.

    Only the unit directory is rewritten; the file's content stays the shipped
    bytes.
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
    """Both seed spellings are `ConditionPathExists=|`, an OR group; without
    the `|` both files would be required. userconf-pi reads `userconf` then
    `userconf.txt`, and Raspberry Pi Imager writes the latter."""
    text, log = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "ConditionPathExists=|/boot/firmware/userconf\n" in text, text
    assert "ConditionPathExists=|/boot/firmware/userconf.txt\n" in text, text
    # Not masked: masking stops the prompt but discards every credential file.
    assert "unmask userconfig.service" in log, log
    assert "mask userconfig.service" not in log.replace("unmask", ""), log


def test_the_wizard_is_enabled_in_the_image_and_not_by_accident(tmp_path):
    """userconfig.service is enabled by install.sh: it is the only consumer of
    the headless credential file, and on a persisted machine-id no first-boot
    `preset-all` re-enables it."""
    _, log = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "enable userconfig.service" in log, log


def test_the_wizard_cannot_reach_a_terminal(tmp_path):
    # Otherwise a malformed seed falls back to the interactive prompt and holds
    # multi-user.target open.
    text, _ = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "StandardInput=null" in text, text


def test_the_condition_follows_the_boot_directory_that_exists(tmp_path):
    """The conditions follow BOOT_DIR: on a layout without /boot/firmware a
    hardcoded path never exists and the seed is silently ignored."""
    text, _ = run_dropin(tmp_path, boot_dir="/boot")
    assert "ConditionPathExists=|/boot/userconf\n" in text, text
    assert "ConditionPathExists=|/boot/userconf.txt\n" in text, text
    assert "/boot/firmware" not in text, text


# --- the two things that kept the wizard from ever running ----------------

# The wizard is ordered behind cloud-init's config stage, which waits on
# network-online.target and so on systemd-networkd-wait-online.service:
# TimeoutStartSec=infinity on a machine whose links NetworkManager owns.

BOOT_FINISHES_BLOCK = ("systemctl mask systemd-networkd-wait-online.service",
                       ": > /etc/cloud/cloud-init.disabled")


def run_boot_finishes(tmp_path):
    """Run the shipped block that clears the two blockers, into a fake /etc.

    Only /etc/cloud is rewritten to a temporary directory.
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
    """systemd-networkd-wait-online.service is masked: it has no start timeout
    and nothing here can satisfy it, so everything after network-online.target
    would wait for the life of the boot."""
    log, _ = run_boot_finishes(tmp_path)
    assert "mask systemd-networkd-wait-online.service" in log, log


def test_cloud_init_is_switched_off_on_an_air_gapped_printer(tmp_path):
    """install.sh writes /etc/cloud/cloud-init.disabled: cloud-init pulls
    network-online.target into the boot and reads user-data off the boot
    partition of an air-gapped device.

    The image build also purges the packages (issue #34); the kill switch
    covers hand-provisioned machines and a later reinstall. ds-identify reads
    the file, and the generator then leaves cloud-init.target out of
    multi-user.target's wants.
    """
    _, cloud = run_boot_finishes(tmp_path)
    assert (cloud / "cloud-init.disabled").exists(), sorted(
        p.name for p in cloud.iterdir()) if cloud.exists() else "no /etc/cloud"


def test_the_closing_summary_names_what_was_done_to_the_whole_machine():
    """The closing summary names every unit install.sh masks, the cloud-init
    kill switch, and how to undo each; these changes are machine-wide and
    outlive the script.

    The mask list is derived from the script, so a new mask cannot go unlisted.
    """
    install = INSTALL.read_text()
    summary = install[install.index('log "Done. Reboot to start the unit."'):]
    body = install[:install.index('log "Done. Reboot to start the unit."')]
    # Indentation allowed: the growfs mask is inside the overlay branch.
    masked = sorted(set(re.findall(r"^[ \t]*systemctl mask (\S+)$", body, re.M)))
    assert len(masked) == 4, masked
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
    # The count in the prose must match as well.
    assert "Five machine-wide changes" in summary, summary


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
    """tty1 is the front panel, so the getty there is conditioned on the panel
    not being healthy: cancel-rename runs `systemctl --no-block start
    getty@tty1` at the end of the credential path.

    The exact directives are asserted. A condition on the unit file alone is
    wrong for both `mask` (a symlink to /dev/null at that path) and `disable`
    (file untouched).
    """
    text, _ = run_getty_dropin(tmp_path)
    assert ("ConditionPathExists=|!/etc/systemd/system/multi-user.target"
            ".wants/otp-unit.service" in text), text
    assert ("ConditionFileNotEmpty=|!/etc/systemd/system/otp-unit.service"
            in text), text
    # The `|` on both makes them an OR; un-prefixed, systemd ANDs them.
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
    """The four panel states, evaluated by real systemd: only a healthy panel
    may skip the getty, since a tty with neither panel nor login is
    unreachable.

    The conditions come from the drop-in install.sh writes, with only the /etc
    prefix rewritten.
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
    """A getty already running on tty1 is stopped, because a condition only
    acts at start time; left running, it takes /dev/tty1 back off the panel.

    The stop comes after the daemon-reload, so the condition is in force before
    anything can restart it.
    """
    _, log = run_getty_dropin(tmp_path)
    assert "stop getty@tty1.service" in log, log
    assert log.index("daemon-reload") < log.index("stop getty@tty1.service"), log


def test_the_getty_is_conditioned_rather_than_masked(tmp_path):
    """The getty is conditioned, not masked: starting a masked unit fails,
    which fails cancel-rename and userconf-service before the seed is removed,
    and userconfig.service then restart-loops. A condition-skipped start
    returns success."""
    _, log = run_getty_dropin(tmp_path)
    assert "mask getty@tty1" not in log, log


# --- the seeded userconf.txt path, as the guest sees it -------------------

# Stock userconfig.service is StandardInput=tty with Restart=on-failure, and
# userconf-service goes interactive on a console-boot machine. install.sh's
# drop-in keeps the unit out of an unseeded boot and turns a malformed seed
# into a fast failure. These run the shipped block that checks that, against a
# stubbed systemd.

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

# The wizard's own log is the only account of it that survives the apply:
# cancel-rename disables the unit and `systemctl show` then describes a freshly
# loaded default.
#
# UC_JOURNAL_LAST_BOOT is what a persistent journal would add to a read not
# scoped with `-b`.
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

# What upstream does to a seed it will not accept: append the reason, rename it
# out of the way, and exit non-zero rather than prompt.
USERCONF_SERVICE_STUB = """#!/bin/sh
cat "$BOOTDIR/userconf.txt" >> "$BOOTDIR/failed_userconf.txt"
rm -f "$BOOTDIR/userconf.txt"
echo "Entered username is invalid:"
exit 1
"""


# --- the journal on the console, and the panel that is not there ----------

# systemd-cat writes stdin to the journal and nothing to stdout. Here the
# journal is a file, and SYSTEMD_CAT_DEAF models a write that never lands.
SYSTEMD_CAT_STUB = """#!/bin/sh
if [ -n "${SYSTEMD_CAT_DEAF-}" ]; then cat > /dev/null; exit 0; fi
cat >> "$FAKE_TAGGED_JOURNAL"
"""

# Answers the probe's two reads and refuses everything else.
# UNIT_JOURNAL_LAST_BOOT is what a persistent journal would add to a read not
# scoped with `-b`.
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

# What a unit with no panel logs before printing unattended, as the emulated
# machine produces it: the harness-supplied board revision lets gpiozero's pin
# factory load, but claiming a pin on QEMU's gpiochip fails EINVAL, so both
# display and input are none.
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

# A real Pi with lgpio: gpiozero has no presence detection, so `Button(5)`
# succeeds with nothing wired and the unit reports `input: GPIO buttons`. The
# check must pass for it.
BUTTONS_JOURNAL = (HEADLESS_JOURNAL
                   .replace("Aug 17 00:04:09 otp-unit python3[412]: no GPIO "
                            "buttons ([Errno 22] Invalid argument)\n", "")
                   .replace("interface -- display: none, input: none",
                            "interface -- display: none, "
                            "input: GPIO buttons"))

# A machine where gpiozero cannot build a pin factory at all; the check must
# pass for it too.
NO_FACTORY_JOURNAL = HEADLESS_JOURNAL.replace(
    "no GPIO buttons ([Errno 22] Invalid argument)",
    "no GPIO buttons (BadPinFactory: Unable to load any default pin factory!)")

# A machine with a screen but no buttons: it still logs "no OLED (" and the
# headless line, because Interface.interactive needs both halves.
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
    # Only the two waits are shortened;
    # test_the_probe_is_given_longer_than_its_own_bounded_wait holds them
    # against the unit's TimeoutStartSec.
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
    # Positive control: every other test here asserts a FAIL.
    proc, _ = run_journal(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {"journal-marker-accepted": "PASS",
                             "unit-detects-no-panel": "PASS"}, proc.stdout


def test_the_marker_is_never_written_where_the_console_would_get_it_anyway(tmp_path):
    """The marker text never appears in the probe's own output: its stdout goes
    to the console directly, so echoing the marker would let the host find it
    there with journal forwarding off."""
    proc, tagged = run_journal(tmp_path)
    assert "OTP-JOURNAL-FORWARDED" in tagged.read_text(), \
        "the marker never reached the fake journal, so this proves nothing"
    assert "OTP-JOURNAL-FORWARDED" not in proc.stdout, proc.stdout
    assert "OTP-JOURNAL-FORWARDED" not in proc.stderr, proc.stderr


def test_a_journal_that_never_took_the_marker_fails(tmp_path):
    """A journal that never took the marker fails the check, which lets the
    host tell "forwarding is off" from "the marker was never written"."""
    proc, _ = run_journal(tmp_path, env={"SYSTEMD_CAT_DEAF": "1"})
    assert results(proc)["journal-marker-accepted"] == "FAIL", proc.stdout


def test_a_unit_that_never_looked_for_a_panel_fails(tmp_path):
    """Without "no OLED (" the unit never probed the display, so the check
    fails even though the log says it is printing unattended."""
    journal = "\n".join(line for line in HEADLESS_JOURNAL.splitlines()
                        if "no OLED" not in line) + "\n"
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_unit_that_saw_no_panel_and_went_on_anyway_fails(tmp_path):
    """A unit that saw no OLED but never logged the headless decision fails: it
    would block in the menu on an empty button queue."""
    journal = "\n".join(line for line in HEADLESS_JOURNAL.splitlines()
                        if "no usable interface" not in line) + "\n"
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_unit_that_found_a_screen_is_not_a_unit_with_no_panel(tmp_path):
    """A unit with an HDMI screen and no buttons logs both "no OLED (" and
    "printing unattended", so the check must key on the display kind and fail.
    """
    proc, _ = run_journal(tmp_path, unit_journal=SCREEN_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_unit_whose_buttons_constructed_still_reports_no_panel(tmp_path):
    """A real Pi reports `input: GPIO buttons` with nothing wired, since
    gpiozero has no presence detection; the check still passes."""
    assert "input: GPIO buttons" in BUTTONS_JOURNAL, \
        "this fixture is supposed to be the machine whose buttons constructed"
    assert "no GPIO buttons" not in BUTTONS_JOURNAL, BUTTONS_JOURNAL
    proc, _ = run_journal(tmp_path, unit_journal=BUTTONS_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_the_machine_tier_3_boots_has_neither_and_still_reports_no_panel(tmp_path):
    """The emulated unit has neither display nor input and passes. The
    fixture's two key lines are asserted, since it is the default for this
    section."""
    assert "no GPIO buttons ([Errno 22] Invalid argument)" in HEADLESS_JOURNAL
    assert "interface -- display: none, input: none" in HEADLESS_JOURNAL
    proc, _ = run_journal(tmp_path, unit_journal=HEADLESS_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_a_unit_whose_pin_factory_never_loaded_still_reports_no_panel(tmp_path):
    """A pin factory that never loaded is a third shape of "no buttons", and it
    passes too."""
    proc, _ = run_journal(tmp_path, unit_journal=NO_FACTORY_JOURNAL)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_a_unit_that_never_said_what_it_settled_on_fails(tmp_path):
    """A missing interface line is a FAIL: it carries the display kind, and its
    absence means the unit died between probing and deciding."""
    journal = "\n".join(line for line in HEADLESS_JOURNAL.splitlines()
                        if "interface --" not in line) + "\n"
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def journal_from_the_real_hmi(monkeypatch, *, oled_present, buttons_present=False):
    """The lines otpunit's own detection writes on the machine tier 3 boots.

    Runs the real hmi.detect with only the hardware stubbed: no OLED
    (FileNotFoundError), no screen or keyboard, and a Button that raises
    OSError EINVAL. `buttons_present=True` is the real-Pi case, where the pin
    claim succeeds.
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
    # The line __main__ adds around describe(); tests/test_img_verdict.py holds
    # it against __main__'s source.
    lines.append(f"interface -- {interface.describe()}")
    # The headless decision is appended even for the mutant, so the check must
    # fail on the detection lines themselves.
    lines.append("no usable interface; printing unattended")
    return "".join(
        f"Aug 17 00:04:09 otp-unit python3[412]: {line}\n" for line in lines)


def test_the_check_reads_what_the_shipped_detection_actually_logs(tmp_path,
                                                                  monkeypatch):
    """End to end: the real hmi produces the lines and the shipped probe judges
    them."""
    journal = journal_from_the_real_hmi(monkeypatch, oled_present=False)
    assert "no GPIO buttons ([Errno 22] Invalid argument)" in journal, journal
    assert "interface -- display: none, input: none" in journal, journal
    proc, _ = run_journal(tmp_path, unit_journal=journal)
    assert results(proc)["unit-detects-no-panel"] == "PASS", proc.stdout


def test_the_fixture_is_the_journal_the_shipped_detection_produces(monkeypatch):
    """HEADLESS_JOURNAL's three key lines are the ones the shipped detection
    produces."""
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
    """With the SSD1306 probe succeeding, the same journal otherwise must turn
    `unit-detects-no-panel` red, so the check cannot pass for the wrong reason.
    """
    healthy = journal_from_the_real_hmi(monkeypatch, oled_present=False)
    broken = journal_from_the_real_hmi(monkeypatch, oled_present=True)
    assert healthy != broken, "the mutation changed nothing the unit logs"
    proc, _ = run_journal(tmp_path, unit_journal=broken)
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


def test_a_previous_boots_headless_decision_does_not_answer_for_this_one(tmp_path):
    """The read is scoped with `-b`: without Storage=volatile, a previous
    boot's headless decision would otherwise answer for this one."""
    proc, _ = run_journal(
        tmp_path, unit_journal="",
        env={"UNIT_JOURNAL_LAST_BOOT": HEADLESS_JOURNAL})
    assert results(proc)["unit-detects-no-panel"] == "FAIL", proc.stdout


# --- the identity that has to outlive the power cycle ---------------------

IDENTITY_BLOCK = (
    "# --- the identity, which is the one thing allowed to survive",
    # Through the closing `fi` of the boot2 branch, so the slice parses.
    '"boot1: $(short_id "$MACHINE_ID_BOOT1") | '
    'boot2: $(short_id "$LIVE_MACHINE_ID")"\nfi')

# The probe asks findmnt only which filesystem contains the identity store;
# anything else is refused loudly.
FINDMNT_STUB = """#!/bin/sh
case "$*" in
    *--target*) printf '%s\\n' "${FAKE_STORE_SRC-/dev/mmcblk0p1}" ;;
    *) echo "stub: unexpected findmnt $*" >&2; exit 64 ;;
esac
"""

# A different machine's id.
OTHER_ID = "ffffffffffffffffffffffffffffffff"


def run_identity(tmp_path, *, phase="boot1", live_id=LIVE_ID, stored_id=LIVE_ID,
                 recorded=None, store_src="/dev/mmcblk0p1", boot_dir="boot",
                 make_boot=True, root_source="overlay"):
    """Run the shipped identity checks against a tree this test builds.

    Every path the block reads comes from a variable, so the shipped lines run.
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
    # Positive control: every other test here asserts a FAIL.
    proc, _ = run_identity(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "machine-id-persisted-outside-the-overlay": "PASS",
        "machine-id-recorded-for-the-next-boot": "PASS"}, proc.stdout


def test_the_first_boot_writes_its_id_where_the_second_can_read_it(tmp_path):
    """Boot1 writes its id to the boot partition; boot2's check rests on that
    record."""
    proc, boot = run_identity(tmp_path)
    written = (boot / "otp-imgcheck-machine-id").read_text().strip()
    assert written == LIVE_ID, written
    assert results(proc)["machine-id-recorded-for-the-next-boot"] == "PASS"


def test_the_record_is_not_the_store_the_shipped_script_writes(tmp_path):
    """The probe's record is a different file from the store:
    otp-unit-identity.service fills the store from the live id, so comparing
    against it would hold boot2 to a value boot2 produced."""
    _, boot = run_identity(tmp_path)
    record = boot / "otp-imgcheck-machine-id"
    store = boot / "otp-identity" / "machine-id"
    assert record.exists() and store.exists()
    assert record != store, "the probe records into the store it is auditing"


def test_a_machine_with_no_id_at_all_records_nothing_and_says_so(tmp_path):
    """A machine with no readable id fails the record check: an empty record
    read back equals itself."""
    proc, _ = run_identity(tmp_path, live_id=None)
    assert results(proc)["machine-id-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout


def test_an_id_that_never_reached_the_card_fails(tmp_path):
    """With no writable boot directory the record never lands, and that is
    boot1's failure."""
    proc, _ = run_identity(tmp_path, make_boot=False)
    assert results(proc)["machine-id-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout


def test_the_second_boot_accepts_the_same_machine(tmp_path):
    proc, _ = run_identity(tmp_path, phase="boot2", recorded=LIVE_ID)
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "PASS", proc.stdout


def test_an_id_regenerated_by_the_power_cycle_fails_the_second_boot(tmp_path):
    """If the store is lost between boots, systemd generates a new id and the
    store is refilled from it, so the store check passes; only boot1's record
    disagrees, and the cross-boot check must fail."""
    proc, _ = run_identity(tmp_path, phase="boot2", live_id=OTHER_ID,
                           stored_id=OTHER_ID, recorded=LIVE_ID)
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "PASS", "the fixture no longer reproduces the hole: " + proc.stdout
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "FAIL", proc.stdout


def test_two_absences_are_not_an_identical_machine_id(tmp_path):
    """No id in boot2 and no record from boot1 compare equal as strings; that
    must fail."""
    proc, _ = run_identity(tmp_path, phase="boot2", live_id=None, recorded=None)
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "FAIL", proc.stdout


def test_a_second_boot_with_an_id_but_no_record_fails(tmp_path):
    """With no boot1 record there is nothing for this boot's id to be identical
    to."""
    proc, _ = run_identity(tmp_path, phase="boot2", recorded=None)
    assert results(proc)["machine-id-identical-across-the-power-cycle"] \
        == "FAIL", proc.stdout


def test_a_machine_id_that_was_never_kept_fails(tmp_path):
    proc, _ = run_identity(tmp_path, stored_id=None)
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_kept_machine_id_that_is_not_the_one_in_use_fails(tmp_path):
    """A stored id that differs from the live one is what a failed initramfs
    restore looks like from userspace."""
    proc, _ = run_identity(tmp_path, stored_id="ffffffffffffffffffffffffffffffff")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_machine_with_no_machine_id_at_all_fails(tmp_path):
    """An unreadable /etc/machine-id and an empty store compare equal; that
    must fail."""
    proc, _ = run_identity(tmp_path, live_id=None, stored_id="")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_store_inside_the_overlay_is_not_persistence(tmp_path):
    """A store on the overlay's tmpfs is not persistence: findmnt then reports
    the root's source for the store."""
    proc, _ = run_identity(tmp_path, store_src="overlay")
    assert results(proc)["machine-id-persisted-outside-the-overlay"] \
        == "FAIL", proc.stdout


def test_a_root_nothing_could_describe_does_not_make_the_store_separate(tmp_path):
    """An empty $ROOT_SOURCE must not make every store look separate from the
    root."""
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


# What the shipped heredoc prints when the sheet renders and cupsd takes it;
# test_the_sheet_program_prints_what_the_probe_reads holds these shapes against
# the real program.
SHEET_HEALTHY = "RENDER ok bytes=4632 magic=%PDF-\nSUBMIT ok job=otpimgcheck-1\n"


def run_sheet(tmp_path, *, says=SHEET_HEALTHY, lpadmin_rc=0):
    """Run the shipped sheet block with the Python and CUPS tools stubbed.

    The interpreter and the two CUPS binaries are rewritten in the block, which
    names them by absolute path; everything else is the shipped code.
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
    # Discards the heredoc on stdin and prints the canned output.
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
    # Positive control: the only test here that does not assert a FAIL.
    proc, calls = run_sheet(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {"diagnostic-sheet-renders": "PASS",
                             "diagnostic-sheet-reaches-cups": "PASS"}, proc.stdout
    # The temporary queue is removed afterwards.
    assert "-x otpimgcheck" in calls.read_text(), calls.read_text()


def test_an_image_that_cannot_draw_the_sheet_fails(tmp_path):
    """Only a booted image can say whether the flashed system has reportlab;
    tier 1 renders against the host's."""
    proc, _ = run_sheet(tmp_path, says="RENDER failed ModuleNotFoundError: reportlab\n")
    assert results(proc) == {"diagnostic-sheet-renders": "FAIL",
                             "diagnostic-sheet-reaches-cups": "FAIL"}, proc.stdout


def test_a_sheet_that_is_not_a_pdf_fails(tmp_path):
    """The PDF magic number is checked: reportlab writing an empty or truncated
    file raises nothing."""
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=0 magic=\n"
                                       "SUBMIT ok job=otpimgcheck-1\n")
    assert results(proc)["diagnostic-sheet-renders"] == "FAIL", proc.stdout


def test_a_sheet_cupsd_would_not_take_fails_with_the_render_still_green(tmp_path):
    """Render and submit are separate checks because they fail for different
    reasons."""
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=4632 magic=%PDF-\n"
                                       "SUBMIT failed PrinterError: lp: Bad destination\n")
    assert results(proc) == {"diagnostic-sheet-renders": "PASS",
                             "diagnostic-sheet-reaches-cups": "FAIL"}, proc.stdout


def test_an_lp_that_returned_no_job_id_is_not_an_enqueued_sheet(tmp_path):
    """Enqueued means cupsd returned a job id, not that lp exited 0:
    Cups.submit() returns "" when lp's output carries no id."""
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=4632 magic=%PDF-\n"
                                       "SUBMIT ok job=<no id>\n")
    assert results(proc)["diagnostic-sheet-reaches-cups"] == "FAIL", proc.stdout


def test_a_job_on_somebody_elses_queue_is_not_this_experiment(tmp_path):
    """The job id must name the probe's own queue; a job on any other queue is
    not this experiment."""
    proc, _ = run_sheet(tmp_path, says="RENDER ok bytes=4632 magic=%PDF-\n"
                                       "SUBMIT ok job=OTP-7\n")
    assert results(proc)["diagnostic-sheet-reaches-cups"] == "FAIL", proc.stdout


def test_the_sheet_program_prints_what_the_probe_reads(tmp_path):
    """Runs the shipped heredoc program against the shipped otpunit, so the
    `case` patterns in the block are held to what the program really prints.

    There is no cupsd here, so the submit half is expected to fail, in the
    shape the block reads.
    """
    block = sheet_block()
    opener = "<<'PY' 2>&1\n"
    assert block.count(opener) == 1, "the sheet program's heredoc moved"
    start = block.index(opener) + len(opener)
    program = block[start:block.index("\nPY\n", start)]
    assert "diagnostics.render_bytes" in program, program[:200]
    # /opt/otp-unit does not exist here, so the shipped `sys.path.insert` is
    # inert. CUPS_SERVER names a socket that cannot exist, so `lp` never
    # reaches a real daemon on the test machine.
    proc = subprocess.run(
        ["python3", "-c", program], capture_output=True, text=True, timeout=180,
        cwd=REPO, env={**os.environ, "SHEET_QUEUE": "otpimgcheck",
                       "CUPS_SERVER": "/nonexistent/otp-no-such-cupsd.sock"})
    assert proc.returncode == 0, proc.stderr
    said = proc.stdout
    assert re.search(r"^RENDER ok bytes=\d+ magic=%PDF-", said, re.M), said
    assert re.search(r"^SUBMIT (ok job=|failed )", said, re.M), said
    # The render glob is taken from the probe: its quoted literals must appear,
    # in order, in what the program printed.
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

    The probe carries a digest between boots because its console is uploaded
    and a crypt string is crackable offline.
    """
    return hashlib.sha256(text.encode()).hexdigest()


def run_userconf(tmp_path, *, phase, service=USERCONF_SERVICE_STUB,
                 executable=True, shadow=None, passwd=None, env=None,
                 credential="otp:$6$otpimgcheck$hashbytes",
                 boot1_digest="auto", store_src="/dev/mmcblk0p1",
                 root_source="overlay", sha256sum=True):
    """Run the shipped userconf block with a substituted systemd and shadow.

    /etc/shadow, the userconf-service path and the boot directory are rewritten
    in the block; everything else is the shipped code. The two waits are
    shortened; test_the_probe_is_given_longer_than_its_own_bounded_wait holds
    them against the unit's TimeoutStartSec.
    """
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    # A whole fake /etc: boot2's relabel experiment runs the shipped
    # persist-identity.sh, which reads passwd, shadow and machine-id from one
    # tree.
    etc = tmp_path / "root" / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    shadow_file = etc / "shadow"
    shadow_file.write_text(
        "root:!:20000:0:99999:7:::\n"
        + (shadow if shadow is not None
           else "otp:$6$otpimgcheck$hashbytes:20000:0:99999:7:::\n"))
    # `pi` is absent on purpose: it is the name boot2's experiment plants in
    # the store. `otp` holds UID 1000, as image/build.sh builds it.
    (etc / "passwd").write_text(passwd if passwd is not None else (
        "root:x:0:0:root:/root:/bin/bash\n"
        "otp:x:1000:1000:,,,:/home/otp:/bin/bash\n"))
    (etc / "machine-id").write_text(LIVE_ID + "\n")
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
        # A sha256sum that prints nothing and exits 0: `cred_digest` would then
        # be empty in both boots, and two empty digests must not compare as a
        # kept password.
        (bin_dir / "sha256sum").write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n")
        (bin_dir / "sha256sum").chmod(0o755)

    # The credential store the probe audits and the record boot1 leaves for
    # boot2; on a real unit the image writes both.
    store = bootdir / "otp-identity"
    store.mkdir(parents=True, exist_ok=True)
    if credential is not None:
        (store / "credential").write_text(credential + "\n")
    # The machine-id is already kept, so the script's exit status speaks only
    # for the credential.
    (store / "machine-id").write_text(LIVE_ID + "\n")

    # Stubs for what the relabel experiment would otherwise run on the host:
    # chpasswd, findmnt, and the installed persist-identity.sh path.
    (bin_dir / "chpasswd").write_text(CHPASSWD_STUB)
    (bin_dir / "chpasswd").chmod(0o755)
    (bin_dir / "findmnt").write_text(IDENTITY_FINDMNT_STUB)
    (bin_dir / "findmnt").chmod(0o755)
    # The real script, pointed at this fixture's boot partition and /etc.
    shipped = tmp_path / "persist-identity.sh"
    shipped.write_text(
        "#!/bin/sh\n"
        f'exec bash {IDENTITY_SH} --boot-dir "{bootdir}" '
        f'--root "{tmp_path / "root"}" "$@"\n')
    shipped.chmod(0o755)
    # "auto" is the healthy path: the digest of the hash this fixture's own
    # /etc/shadow holds, computed so it cannot drift.
    if boot1_digest == "auto":
        boot1_digest = sha256_of(shadow_field(shadow_file.read_text(), "otp"))
    if boot1_digest is not None:
        (bootdir / "otp-imgcheck-credential").write_text(boot1_digest + "\n")

    block = userconf_block()
    for original, replacement, why, count in (
        # The read expression, not the bare path, since the block's comments
        # also name /etc/shadow. Four reads: boot1's apply check, boot2's
        # survival check, and either side of boot2's relabel experiment.
        ("{print $2}' /etc/shadow", "{print $2}' " + str(shadow_file),
         "the shadow read", 4),
        # The awk, not the bare path, for the same reason.
        ("exit }' /etc/passwd", "exit }' " + str(etc / "passwd"),
         "the passwd read", 2),
        ("/opt/otp-unit/persist-identity.sh", str(shipped),
         "the shipped identity script", 1),
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
        # What the identity section above the block would have set; steerable
        # so the unmounted-/boot/firmware clause can be exercised.
        f'IDENTITY_STORE="{store}"\nSTORE_SRC="{store_src}"\n'
        f'ROOT_SOURCE="{root_source}"\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + block + '\nprintf "TOTALS %s/%s\\n" "$PASS" "$TOTAL"\n')
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {}), "BOOTDIR": str(bootdir),
             "CHPASSWD_SHADOW": str(shadow_file),
             "CHPASSWD_ARGV": str(tmp_path / "chpasswd-argv.log"),
             "FAKE_STORE_SRC": store_src, "FAKE_ROOT_SRC": root_source,
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
    # Positive control: every other test here asserts a FAIL.
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
    """A wizard job queued behind an unbounded network wait looks like a skip
    in the unit's own fields, so the job queue is asked directly."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "JOBS": "1 systemd-networkd-wait-online.service start running\n"})
    assert results(proc)["network-wait-cannot-hold-the-boot-open"] == "FAIL", \
        proc.stdout


def test_an_unmasked_network_wait_fails_even_with_an_empty_queue(tmp_path):
    """An unmasked network wait fails even with an empty queue: the queue says
    nothing about the next boot, the mask does."""
    for state in ("enabled", "disabled", "static"):
        proc, _ = run_userconf(tmp_path / state, phase="boot2",
                               env={**HEALTHY_BOOT2, "NETWAIT_ENABLED": state})
        assert results(proc)["network-wait-cannot-hold-the-boot-open"] == "FAIL", \
            proc.stdout


def test_a_credential_apply_that_took_the_front_panel_down_fails(tmp_path):
    """A credential apply that takes the panel down fails its own check while
    the seed check stays green: cancel-rename starts a getty on tty1 after
    every successful apply."""
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "PANEL_ACTIVE": "inactive"})
    assert results(proc)["userconf-seed-applied"] == "PASS", proc.stdout
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout


def test_a_login_prompt_sharing_the_panels_tty_fails(tmp_path):
    """A getty running on tty1, or queued to start, fails the check: it would
    share the tty with the panel, interleaving output and stealing keystrokes.
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
    """A queued wizard job fails the first boot even though every other field
    reads `inactive`: a queued job is what holds multi-user.target open."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "JOBS": "42 userconfig.service start waiting\n"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_parked_mid_start_fails_the_first_boot(tmp_path):
    # The prompt is up: the unit is "activating" with a running job.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_ACTIVE": "activating", "UC_RESULT": "",
        "JOBS": "42 userconfig.service start running\n"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_seeded_boot_whose_wizard_was_skipped_fails(tmp_path):
    # The condition came out false with a seed on the card, so the credentials
    # are silently ignored; the log says so in systemd's words.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_COND": "no",
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service"
                      " - User configuration dialog was skipped because of an"
                      " unmet condition check (ConditionPathExists=|/boot/"
                      "firmware/userconf)."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_seeded_boot_whose_wizard_never_ran_at_all_fails(tmp_path):
    """An empty journal fails: a masked or never-pulled-in wizard looks healthy
    in every other field, whereas a garbage-collected unit that ran leaves its
    "Finished" line."""
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "UC_JOURNAL": ""})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_previous_boots_success_line_does_not_answer_for_this_boot(tmp_path):
    """The journal read is scoped with `-b`: on a persistent journal, a
    previous boot's `Finished userconfig.service` would otherwise satisfy a
    boot on which the unit never ran."""
    persistent = {**HEALTHY_BOOT1, "UC_JOURNAL": "", "UC_JOURNAL_LAST_BOOT":
                  "Aug 15 09:00:00 otp-unit systemd[1]: Finished "
                  "userconfig.service - User configuration dialog."}
    proc, _ = run_userconf(tmp_path, phase="boot1", env=persistent)
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout
    # Positive control: the same journal offered as this boot's passes.
    proc, _ = run_userconf(
        tmp_path / "this-boot", phase="boot1",
        env={**HEALTHY_BOOT1, "UC_JOURNAL": persistent["UC_JOURNAL_LAST_BOOT"]})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "PASS", proc.stdout


def test_the_wizards_own_stdout_cannot_report_its_success(tmp_path):
    """Only PID 1's `Finished` line counts: `journalctl -u` also returns the
    unit's own stdout, so the phrase printed by userconf-service must not pass.
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
    """`systemd[1]:` is matched literally: as an unquoted glob, `[1]` is a
    character class and `systemd1: Finished` would pass."""
    globbed = ("Aug 16 22:06:10 otp-unit userconf-service[512]: systemd1: "
               "Finished userconfig.service - User configuration dialog.")
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "UC_JOURNAL": globbed})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_that_failed_on_the_seed_fails_the_first_boot(tmp_path):
    # A malformed seed met at boot: the unit fails and Restart=on-failure logs
    # the two strings img-boot.sh fails a release on.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_RESULT": "exit-code", "UC_ACTIVE": "failed",
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service:"
                      " Failed with result 'exit-code'. Aug 16 22:05:02 otp-unit"
                      " systemd[1]: userconfig.service: Scheduled restart job,"
                      " restart counter is at 1."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_that_only_finished_after_a_restart_fails(tmp_path):
    """A "Finished" after a failure and restart is still a FAIL: that boot
    printed the strings img-boot.sh hard-fails a release on."""
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
    """A healthy apply still passes after systemd has garbage-collected the
    unit: cancel-rename disables it, so `systemctl show` returns defaults and
    the journal's Finished line is the evidence."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_TS": "", "UC_COND": "no", "UC_RESULT": "success"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "PASS", proc.stdout


def test_a_seed_deleted_without_being_applied_fails(tmp_path):
    """A seed removed without chpasswd running fails: from the host the file is
    gone either way, and the account would keep the build-time password nobody
    has."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           shadow="otp:$6$rEaLsAlT$otherbytes:20000:0:99999:7:::\n")
    assert results(proc)["userconf-seed-applied"] == "FAIL", proc.stdout


def test_no_shadow_entry_at_all_fails(tmp_path):
    # The account renamed out from under the seed, or never created.
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           shadow="pi:$6$otpimgcheck$hashbytes:20000::::::\n")
    assert results(proc)["userconf-seed-applied"] == "FAIL", proc.stdout


def test_an_unseeded_second_boot_is_quiet_and_leaves_evidence(tmp_path):
    # Positive control for boot2: skipped but enabled, no prompt possible, and
    # the malformed seed quarantined.
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "network-wait-cannot-hold-the-boot-open": "PASS",
        "userconf-unseeded-boot-skips-the-wizard": "PASS",
        "userconf-wizard-cannot-prompt": "PASS",
        "credential-survives-the-power-cycle": "PASS",
        "credential-recovers-a-store-naming-another-account": "PASS",
        "userconf-malformed-seed-fails-fast": "PASS"}, proc.stdout
    assert (bootdir / "failed_userconf.txt").exists()
    assert not (bootdir / "userconf.txt").exists()
    # The experiment restored the card through the shipped script: only the
    # name in the store was changed, so a working restore rewrites it.
    assert (bootdir / "otp-identity" / "credential").read_text() \
        == "otp:$6$otpimgcheck$hashbytes\n"
    assert shadow_field((tmp_path / "root" / "etc" / "shadow").read_text(),
                        "otp") == "$6$otpimgcheck$hashbytes"


def test_the_relabel_experiment_is_refused_a_name_this_machine_really_has(tmp_path):
    """The relabel experiment refuses to conclude anything if the planted name
    is a real account: its subject is a store naming an account that does not
    exist."""
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        passwd="root:x:0:0:root:/root:/bin/bash\n"
               "pi:x:1001:1001:,,,:/home/pi:/bin/bash\n"
               "otp:x:1000:1000:,,,:/home/otp:/bin/bash\n")
    assert results(proc)["credential-recovers-a-store-naming-another-account"] \
        == "FAIL", proc.stdout
    # It names the clause that failed.
    assert "an account here: yes" in proc.stdout, proc.stdout


def test_a_relabel_experiment_that_never_changed_the_password_proves_nothing(tmp_path):
    """If the experiment's mangle never changed the live hash, "the hash is the
    kept one afterwards" proves nothing, so the check fails. Driven with a
    chpasswd that exits 0 having written nothing."""
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "CHPASSWD_APPLY": "no"})
    assert results(proc)["credential-recovers-a-store-naming-another-account"] \
        == "FAIL", proc.stdout
    # Nothing moved: before and after agree.
    assert re.search(r"before: (\S+)\.\.\. \| after: \1\.\.\.", proc.stdout), \
        proc.stdout


# --- and the credential kept across the power cycle ------------------------
#
# The same block through the same rig. Each test below is a way for the kept
# credential to look right while being false.


def test_a_boot_that_applied_the_seed_but_kept_nothing_fails(tmp_path):
    """A boot that applied the seed but left the store empty fails: /etc is in
    the overlay, so the password would die with the power."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           credential=None)
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout
    # Boot1's own record still passes: the positive control must not go red for
    # what it controls for.
    assert results(proc)["credential-recorded-for-the-next-boot"] == "PASS"


def test_a_store_holding_something_other_than_the_applied_hash_fails(tmp_path):
    """A store holding a different hash from the applied one fails: the next
    power cycle would restore a stale password."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           credential="otp:$6$otpimgcheck$somethingelse")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_store_naming_another_account_fails(tmp_path):
    """A store naming another account fails even though the digest of its hash
    field matches."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           credential="pi:$6$otpimgcheck$hashbytes")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_credential_store_inside_the_overlay_is_not_persistence(tmp_path):
    """A credential store on the overlay's tmpfs agrees with /etc/shadow on
    every boot while nothing survives, so it fails."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           store_src="overlay", root_source="overlay")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_root_nothing_could_describe_does_not_make_the_credential_kept(tmp_path):
    """An empty $ROOT_SOURCE must not make every store look separate from the
    root."""
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           root_source="")
    assert results(proc)["credential-recorded-outside-the-overlay"] == "FAIL", \
        proc.stdout


def test_a_first_boot_with_no_credential_at_all_records_nothing_and_says_so(tmp_path):
    """With no shadow entry for the account, boot1 records nothing and fails,
    rather than writing a digest of nothing that boot2 would match."""
    proc, bootdir = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                                 shadow="pi:$6$x$y:20000:0:99999:7:::\n")
    assert results(proc)["credential-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout
    written = (bootdir / "otp-imgcheck-credential").read_text().strip()
    assert written == "", written


def test_a_record_that_never_reached_the_card_fails_boot1(tmp_path):
    """Boot1 reads its record back: on a full or read-only partition boot2
    would otherwise compare against nothing."""
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    # A directory at the record's path, which no `printf >` can overwrite.
    (bootdir / "otp-imgcheck-credential").mkdir()
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           boot1_digest=None)
    assert results(proc)["credential-recorded-for-the-next-boot"] == "FAIL", \
        proc.stdout
    # The apply itself is unaffected.
    assert results(proc)["userconf-seed-applied"] == "PASS", proc.stdout


def test_the_second_boot_accepts_the_password_that_outlived_the_power(tmp_path):
    """The healthy boot2 path: the hash carries the seed's salt and digests to
    what boot1 recorded, with no seed on the card."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert results(proc)["credential-survives-the-power-cycle"] == "PASS", \
        proc.stdout


def test_a_second_boot_back_on_the_build_time_password_fails(tmp_path):
    """A boot2 back on pi-gen's random build-time password, which nobody has,
    fails."""
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        shadow="otp:$6$RaNdOmSaLt$buildtimebytes:20000:0:99999:7:::\n",
        boot1_digest=sha256_of("$6$otpimgcheck$hashbytes"))
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_two_absences_are_not_a_credential_that_survived(tmp_path):
    """No hash in /etc/shadow and no record from boot1 compare equal; that must
    fail."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           shadow="root:!:20000:0:99999:7:::\n",
                           boot1_digest=None)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_a_digest_nothing_could_compute_is_not_a_match(tmp_path):
    """Two empty digests are not a match: a missing or broken sha256sum leaves
    both sides empty on the same machine. The same fixture with sha256sum
    working is the positive control."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           boot1_digest="", sha256sum=False)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout
    proc, _ = run_userconf(tmp_path / "control", phase="boot2",
                           env=HEALTHY_BOOT2)
    assert results(proc)["credential-survives-the-power-cycle"] == "PASS", \
        proc.stdout


def test_the_same_password_in_both_boots_is_not_the_seeded_one(tmp_path):
    """The salt attributes the hash to the seed: an unchanged digest alone is
    also true of the build-time password on a unit that persisted nothing."""
    build_time = "$6$RaNdOmSaLt$buildtimebytes"
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        shadow=f"otp:{build_time}:20000:0:99999:7:::\n",
        boot1_digest=sha256_of(build_time))
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout
    # Positive control: the seeded hash and its own digest pass.
    seeded = "$6$otpimgcheck$hashbytes"
    proc, _ = run_userconf(
        tmp_path / "control", phase="boot2", env=HEALTHY_BOOT2,
        shadow=f"otp:{seeded}:20000:0:99999:7:::\n",
        boot1_digest=sha256_of(seeded))
    assert results(proc)["credential-survives-the-power-cycle"] == "PASS", \
        proc.stdout


def test_a_second_boot_with_a_password_but_no_record_fails(tmp_path):
    """With no boot1 record there is nothing to hold this boot's hash against.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           boot1_digest=None)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_a_password_that_could_have_come_from_a_seed_this_boot_fails(tmp_path):
    """A userconf.txt still on the card fails the check: the hash may have been
    applied this boot rather than restored, and the seed stays readable in any
    card reader."""
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    (bootdir / "userconf.txt").write_text("otp:$6$otpimgcheck$hashbytes\n")
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_a_restored_hash_that_was_mangled_on_the_way_back_fails(tmp_path):
    """A truncated restore still begins with the seed's salt, so the digest
    must match as well; a truncated crypt string is a password nobody can type.
    """
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        shadow="otp:$6$otpimgcheck$hashbyt:20000:0:99999:7:::\n",
        boot1_digest=sha256_of("$6$otpimgcheck$hashbytes"))
    assert results(proc)["credential-survives-the-power-cycle"] == "FAIL", \
        proc.stdout


def test_the_probe_never_writes_the_hash_where_the_console_can_see_it(tmp_path):
    """The hash never reaches the console, which is uploaded as a CI artifact:
    boot1's record is a sha256 of it and only twelve characters of that are
    printed."""
    secret = "$6$otpimgcheck$hashbytes"
    for phase, env in (("boot1", HEALTHY_BOOT1), ("boot2", HEALTHY_BOOT2)):
        proc, bootdir = run_userconf(tmp_path / phase, phase=phase, env=env)
        assert secret not in proc.stdout, proc.stdout
        assert "hashbytes" not in proc.stdout, proc.stdout
        # The probe's own record on the card is the digest, not the hash.
        record = (bootdir / "otp-imgcheck-credential").read_text().strip()
        assert record == sha256_of(secret), record
    # Positive control: the digest prefix is in the output.
    assert sha256_of(secret)[:12] in proc.stdout, proc.stdout


def test_a_masked_wizard_is_quiet_for_the_wrong_reason(tmp_path):
    """A masked or disabled wizard is quiet for the wrong reason: the unit is
    userconf.txt's only consumer, so every seeded credential would be
    discarded."""
    for state in ("masked", "disabled"):
        proc, _ = run_userconf(tmp_path / state, phase="boot2",
                               env={**HEALTHY_BOOT2, "UC_ENABLED": state})
        assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", \
            proc.stdout


def test_a_condition_systemd_never_evaluated_is_not_a_skip(tmp_path):
    # An empty ConditionTimestamp means systemd has not evaluated the unit,
    # which looks like a skip in every other field.
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_TS": ""})
    assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", proc.stdout


def test_a_second_boot_whose_condition_is_still_true_fails(tmp_path):
    """A boot2 whose condition is true fails: the seed outlived the boot that
    should have consumed it, so the wizard stays armed and a credential line
    stays on the FAT partition. A finished oneshot otherwise looks exactly like
    a skip."""
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_COND": "yes"})
    assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", proc.stdout
    # The detail names which clause failed.
    assert "condition=yes" in proc.stdout, proc.stdout


def test_a_wizard_that_can_still_reach_a_tty_fails(tmp_path):
    # The drop-in gone, or overridden by another one: the stock unit's
    # StandardInput=tty is back and a malformed seed is a hang again.
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_STDIN": "tty"})
    assert results(proc)["userconf-wizard-cannot-prompt"] == "FAIL", proc.stdout


def hang_stub(*, ignore_term: bool) -> str:
    """A userconf-service that quarantines the seed and then never returns.

    The quarantine comes first, as upstream does, so only the time bound is
    under test. `exec >/dev/null` stops the orphaned `sleep` holding the pipe
    the probe's command substitution reads.
    """
    return ('#!/bin/sh\n'
            + ('trap "" TERM\n' if ignore_term else '')
            + 'cat "$BOOTDIR/userconf.txt" >> "$BOOTDIR/failed_userconf.txt"\n'
              'rm -f "$BOOTDIR/userconf.txt"\n'
              'exec >/dev/null 2>&1\n'
              'sleep 30\n')


def test_a_malformed_seed_that_hangs_fails_instead_of_timing_the_boot_out(tmp_path):
    """A userconf-service that blocks is killed at the bound and reported as
    rc=124, instead of holding the boot open."""
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                                 service=hang_stub(ignore_term=False))
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "rc=124" in proc.stdout, proc.stdout
    # The probe carried on to report.
    assert "TOTALS" in proc.stdout, proc.stdout


def test_a_hang_that_ignores_the_term_signal_also_fails(tmp_path):
    """A child that ignores SIGTERM is SIGKILLed by `-k` and reports rc=137, so
    the gate must fail every rc of 124 or above, not only 124. Both hang
    fixtures are kept so neither number is special-cased."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service=hang_stub(ignore_term=True))
    assert "rc=137" in proc.stdout, (
        "the fixture did not reach the -k escalation, so this test is not "
        "measuring what it says: " + proc.stdout)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "TOTALS" in proc.stdout, proc.stdout


def test_the_detail_line_does_not_gloss_a_kill_as_a_timeout(tmp_path):
    """The detail line must not gloss rc=137 as "124 = still running": that
    names the wrong signal to whoever debugs the run."""
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service=hang_stub(ignore_term=True))
    detail = next(ln for ln in proc.stdout.splitlines()
                  if "userconf-malformed-seed-fails-fast" in ln)
    assert "137" in detail, detail
    assert "124 = still running" not in detail, detail


def test_the_experiment_supplies_the_conditions_the_unit_would(tmp_path):
    """The experiment runs with stdin closed and TERM unset, as the unit would;
    otherwise whiptail may find a terminal."""
    block = userconf_block()
    line = next(ln for ln in block.splitlines() if "USERCONF_SERVICE" in ln
                and "timeout" in ln)
    assert "< /dev/null" in line, line
    assert "env -u TERM" in line, line


def test_a_malformed_seed_that_leaves_no_evidence_fails(tmp_path):
    # The seed is gone with no failed_userconf.txt, the only thing that tells
    # the operator why they have no login.
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service='#!/bin/sh\nrm -f "$BOOTDIR/userconf.txt"\nexit 1\n')
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_a_seed_left_in_place_by_the_service_fails(tmp_path):
    # Quarantine means moved: a card still holding the bad line prompts on
    # every boot.
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        service='#!/bin/sh\ncp "$BOOTDIR/userconf.txt" "$BOOTDIR/failed_userconf.txt"\nexit 1\n')
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_an_image_without_userconf_pi_fails_rather_than_passing_silently(tmp_path):
    """An absent or non-executable userconf-service is a FAIL: "the seed is
    gone" and "nothing hung" would otherwise both hold on an image without the
    credential mechanism."""
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                                 service=None)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "rc=none" in proc.stdout, proc.stdout
    # Nothing was planted on a card whose machine cannot consume it.
    assert not (bootdir / "userconf.txt").exists()

    # Present but not executable.
    proc, _ = run_userconf(tmp_path / "not-executable", phase="boot2",
                           env=HEALTHY_BOOT2, executable=False)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_the_probe_is_given_longer_than_its_own_bounded_wait(tmp_path):
    """The unit's TimeoutStartSec exceeds the sum of every bounded wait in the
    probe and is below the per-boot backstop, so a slow boot keeps its report
    and a wedged probe is killed.

    All waits are summed although the two experiments run in different phases;
    that is pessimistic but keeps the check simple arithmetic. Bounds are read
    from the probe, the backstop from the workflow.
    """
    probe = GUEST_CHECK.read_text()
    # Each `for _ in $(seq 1 N)` loop with the `sleep S` inside it.
    poll_bound = 0
    for loop in re.finditer(r"for _ in \$\(seq 1 (\d+)\); do(.*?)\n *done",
                            probe, re.S):
        interval = re.search(r"^\s*sleep (\d+)\s*$", loop.group(2), re.M)
        assert interval, f"a poll with no sleep in it: {loop.group(0)[:80]}"
        poll_bound += int(loop.group(1)) * int(interval.group(1))
    # Plus anything run under `timeout`.
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
    """The sentinel is read before it is written: the other order would report
    a survival every time."""
    text = GUEST_CHECK.read_text()
    read_at = text.index('if [ -e "$SENTINEL" ]; then')
    write_at = text.index("> \"$SENTINEL\" 2>/dev/null")
    assert read_at < write_at, (
        "the sentinel is written before it is read back, so "
        "root-writes-discarded-by-the-power-cycle can never fail")


def test_the_probe_states_two_presences_before_it_asserts_an_absence():
    """Two presences are reported before the absence: "the file is gone" is
    also true of a boot1 that never ran or a rig that cannot write to /."""
    text = GUEST_CHECK.read_text()
    absence = text.index("check root-writes-discarded-by-the-power-cycle")
    for control in ("check root-write-lands-and-is-readable",
                    "check settings-survive-the-power-cycle"):
        assert text.index(control) < absence, control


# --- the provisioning agent that is not on the image ----------------------
#
# The stage purges cloud-init and rpi-cloud-init-mods and install.sh writes the
# kill switch (issue #34). Tier 3 checks the two separately, because after a
# purge most cloud-init absences say nothing about the kill switch. These run
# the shipped block against a stubbed dpkg and systemd.

CLOUD_INIT_BLOCK = ("# --- the provisioning agent this image no longer carries",
                    'dir=$(yesno test -d "${CI_DISABLED%/*}")"')

# Answers the probe's two questions and refuses everything else, so an absence
# check cannot pass by accident. `-W -f <fmt> <pkg>` puts the package name in
# $4; an unknown package is an empty answer and rc 1, as with dpkg-query.
DPKG_QUERY_STUB = """#!/bin/sh
case "$1 $2" in
    "-W -f") ;;
    *) echo "stub: unexpected dpkg-query $*" >&2; exit 64 ;;
esac
case "$4" in
    cloud-init)           ANSWER="${PKG_CLOUD_INIT-}" ;;
    rpi-cloud-init-mods)  ANSWER="${PKG_RPI_MODS-}" ;;
    systemd)              ANSWER="${PKG_SYSTEMD-installed}" ;;
    *) echo "stub: unexpected package $4" >&2; exit 64 ;;
esac
printf '%s' "$ANSWER"
# Nothing on stdout and rc 1 is what dpkg-query answers for a package it has
# never heard of, which is what a purged one is. The block's `|| true` is
# written for that, and modelling it here is what makes the exit status
# irrelevant on purpose rather than by luck.
[ -n "$ANSWER" ] || exit 1
"""

# The same for systemd: `not-found` is the LoadState of a unit it has never
# heard of.
CLOUD_SYSTEMCTL_STUB = """#!/bin/sh
case "$1 $2 $3 $4" in
    "show -p LoadState --value")
        case "$5" in
            cloud-init.target)        printf '%s\\n' "${LOAD_TARGET-not-found}" ;;
            cloud-init-local.service) printf '%s\\n' "${LOAD_LOCAL-not-found}" ;;
            cloud-config.service)     printf '%s\\n' "${LOAD_CONFIG-not-found}" ;;
            otp-unit.service)         printf '%s\\n' "${LOAD_UNIT-loaded}" ;;
            *) echo "stub: unexpected unit $5" >&2; exit 64 ;;
        esac ;;
    *)
        case "$1 $2" in
            "is-active NetworkManager.service") printf '%s\\n' "${NM_ACTIVE-active}" ;;
            *) echo "stub: unexpected systemctl $*" >&2; exit 64 ;;
        esac ;;
esac
"""

# A purged machine with the kill switch in place: all stub defaults.
HEALTHY_PURGE = {}


def run_cloud_init(tmp_path, *, env=None, marker="file", generator=False,
                   renderer=False,
                   siblings=("systemd-fstab-generator", "systemd-gpt-auto-generator")):
    """Run the shipped cloud-init block against a substituted /usr and /etc.

    Three paths are rewritten at their assignments, which appear once each;
    everything else is the shipped code.
    """
    root = tmp_path / "root"
    gen_dir = root / "usr" / "lib" / "systemd" / "system-generators"
    gen_dir.mkdir(parents=True, exist_ok=True)
    for name in siblings:
        (gen_dir / name).write_text("#!/bin/sh\n")
    if generator:
        (gen_dir / "cloud-init-generator").write_text("#!/bin/sh\n")
    netplan = root / "usr" / "lib" / "netplan" / "00-network-manager-all.yaml"
    netplan.parent.mkdir(parents=True, exist_ok=True)
    if renderer:
        netplan.write_text("network:\n  version: 2\n  renderer: NetworkManager\n")
    disabled = root / "etc" / "cloud" / "cloud-init.disabled"
    disabled.parent.mkdir(parents=True, exist_ok=True)
    if marker == "file":
        disabled.write_text("")
    elif marker == "directory":
        disabled.mkdir()
    elif marker == "dangling-symlink":
        disabled.symlink_to(tmp_path / "no-such-file")
    elif marker == "no-etc-cloud":
        disabled.parent.rmdir()
    elif marker != "missing":
        raise AssertionError(f"unknown marker case {marker!r}")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "dpkg-query").write_text(DPKG_QUERY_STUB)
    (bin_dir / "dpkg-query").chmod(0o755)
    (bin_dir / "systemctl").write_text(CLOUD_SYSTEMCTL_STUB)
    (bin_dir / "systemctl").chmod(0o755)

    block = slice_between(GUEST_CHECK.read_text(), *CLOUD_INIT_BLOCK)
    for original, replacement, why in (
        ("CI_GENERATOR_DIR=/usr/lib/systemd/system-generators",
         f"CI_GENERATOR_DIR={gen_dir}", "the generator directory"),
        ("CI_NETPLAN_RENDERER=/usr/lib/netplan/00-network-manager-all.yaml",
         f"CI_NETPLAN_RENDERER={netplan}", "netplan's renderer default"),
        ("CI_DISABLED=/etc/cloud/cloud-init.disabled",
         f"CI_DISABLED={disabled}", "the kill switch"),
    ):
        assert block.count(original) == 1, \
            f"{why}: {original!r} appears {block.count(original)}x, want 1"
        block = block.replace(original, replacement)

    runner = tmp_path / "cloud-init.sh"
    runner.write_text(
        "set -uo pipefail\n"
        'PHASE="boot1"\nPASS=0\nTOTAL=0\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + block + '\nprintf "TOTALS %s/%s\\n" "$PASS" "$TOTAL"\n')
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {}),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return proc


def test_a_purged_machine_with_its_kill_switch_reports_both(tmp_path):
    # Positive control: every other test here asserts a FAIL.
    proc = run_cloud_init(tmp_path, env=HEALTHY_PURGE)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "cloud-init-is-not-installed": "PASS",
        "cloud-init-kill-switch-survives-the-purge": "PASS"}, proc.stdout


def test_a_cloud_init_still_installed_fails(tmp_path):
    """An installed cloud-init or rpi-cloud-init-mods fails the check;
    ENABLE_CLOUD_INIT=0 does not remove the packages."""
    for var in ("PKG_CLOUD_INIT", "PKG_RPI_MODS"):
        proc = run_cloud_init(tmp_path / var, env={var: "installed"})
        assert results(proc)["cloud-init-is-not-installed"] == "FAIL", proc.stdout


def test_a_removed_but_not_purged_cloud_init_fails(tmp_path):
    """`config-files` is not purged: rpi-cloud-init-mods' cloud.cfg.d file
    stays and points the NoCloud datasource at /boot/firmware for any
    reinstall."""
    proc = run_cloud_init(tmp_path, env={"PKG_CLOUD_INIT": "config-files"})
    assert results(proc)["cloud-init-is-not-installed"] == "FAIL", proc.stdout


def test_a_cloud_init_unit_systemd_still_knows_fails(tmp_path):
    """A cloud-init unit systemd still knows fails, whatever dpkg says;
    `masked` counts, since the unit file is on the machine."""
    for var, state in (("LOAD_TARGET", "loaded"), ("LOAD_LOCAL", "masked"),
                       ("LOAD_CONFIG", "loaded")):
        proc = run_cloud_init(tmp_path / var, env={var: state})
        assert results(proc)["cloud-init-is-not-installed"] == "FAIL", proc.stdout


def test_a_generator_left_behind_fails(tmp_path):
    """A leftover generator fails: it puts cloud-init.target into the boot
    transaction whether or not any unit is enabled."""
    proc = run_cloud_init(tmp_path, generator=True)
    assert results(proc)["cloud-init-is-not-installed"] == "FAIL", proc.stdout


def test_the_absences_need_their_positive_controls(tmp_path):
    """Each mechanism behind the absence clauses must answer one positive
    question: a silent dpkg-query, a silent systemctl or an empty generator
    directory would otherwise report a clean purge."""
    for case, kwargs in (
            ("dpkg-query says nothing", {"env": {"PKG_SYSTEMD": ""}}),
            ("systemctl says nothing", {"env": {"LOAD_UNIT": ""}}),
            ("the generator directory is empty", {"siblings": ()})):
        proc = run_cloud_init(tmp_path / case.split()[0], **kwargs)
        assert results(proc)["cloud-init-is-not-installed"] == "FAIL", \
            f"{case}: {proc.stdout}"


def test_a_missing_kill_switch_fails_with_the_purge_still_green(tmp_path):
    """A missing kill switch fails its own check while the purge check stays
    green: the kill switch is what disables a cloud-init a later dependency
    brings back."""
    proc = run_cloud_init(tmp_path, marker="missing")
    assert results(proc)["cloud-init-is-not-installed"] == "PASS", proc.stdout
    assert results(proc)["cloud-init-kill-switch-survives-the-purge"] == "FAIL", \
        proc.stdout


def test_a_kill_switch_that_is_not_a_regular_file_fails(tmp_path):
    """The kill switch must be a regular file: ds-identify tests `[ -f ... ]`,
    so a directory or a dangling symlink of that name does nothing."""
    for case in ("directory", "dangling-symlink"):
        proc = run_cloud_init(tmp_path / case, marker=case)
        assert results(proc)["cloud-init-kill-switch-survives-the-purge"] == "FAIL", \
            f"{case}: {proc.stdout}"


def test_a_purge_that_took_etc_cloud_with_it_fails(tmp_path):
    """A purge that removed /etc/cloud took the marker with it; this is why the
    stage purges before install.sh runs."""
    proc = run_cloud_init(tmp_path, marker="no-etc-cloud")
    assert results(proc)["cloud-init-kill-switch-survives-the-purge"] == "FAIL", \
        proc.stdout
    assert "dir=no" in proc.stdout, proc.stdout


def netplan_report(proc):
    """The netplan half of the first check's detail line."""
    line = [ln for ln in proc.stdout.splitlines()
            if "cloud-init-is-not-installed" in ln][0]
    assert "netplan-renderer=" in line, \
        f"the netplan report is no longer on the check line: {line}"
    return line[line.index("netplan-renderer="):]


def test_the_probe_reports_what_the_purge_did_to_netplan(tmp_path):
    """The check line reports whether netplan's NetworkManager renderer file
    exists and whether NetworkManager is active, read off the machine: the
    purge removes that file. Both branches are exercised."""
    purged = run_cloud_init(tmp_path / "purged", env={"NM_ACTIVE": "active"})
    assert netplan_report(purged) == \
        "netplan-renderer=no NetworkManager=active", purged.stdout

    # With the file kept the check still passes; only the report differs.
    kept = run_cloud_init(tmp_path / "kept", renderer=True,
                          env={"NM_ACTIVE": "inactive"})
    assert netplan_report(kept) == \
        "netplan-renderer=yes NetworkManager=inactive", kept.stdout
    assert results(kept)["cloud-init-is-not-installed"] == "PASS", kept.stdout

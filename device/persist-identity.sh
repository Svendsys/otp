#!/usr/bin/env bash
#
# Keep this unit's machine-id across the power-cycle.
#
# Run once per boot by otp-unit-identity.service, before anything that reads
# it. Installed to /opt/otp-unit by device/install.sh, and only on a machine
# whose root is the read-only overlay -- on a writable root /etc keeps this
# by itself.
#
# WHAT IS PERSISTED, and nothing else:
#
#   machine-id      because /etc/machine-id inside the overlay reverts to
#                   pi-gen's `uninitialized` on every power cycle, and
#                   systemd reads that word as "first boot" -- preset-all
#                   on every boot, ssh.socket re-enabled on every boot,
#                   regenerate_ssh_host_keys.service on every boot.
#
# WHAT IS DELIBERATELY NOT PERSISTED, because an earlier version of this
# script did it and the reason it did was wrong. The SSH HOST KEYS. They were
# copied onto the FAT partition so that the fingerprint of a machine that
# prints one-time pads stopped changing on every power cycle -- true, but it
# was a fingerprint nobody could ever be shown. image/build.sh sets
# ENABLE_SSH=0, so pi-gen leaves ssh.service DISABLED in the image and this
# appliance does not run sshd at all. The one thing that ever started it was
# the same first-boot `preset-all` the machine-id above ends, and run
# 32020772161's consoles measure exactly that: boot1 starts ssh.service at
# 130s, boot2 mentions ssh.service, ssh.socket and OpenBSD not once while
# still reaching multi-user.target.
#
# So the choice was between re-enabling sshd on an air-gapped key printer to
# give the persistence something to be for, and dropping the persistence. The
# owner's decision is the second: no sshd, no host keys on the boot
# partition, and one fewer secret outside the overlay on a machine whose
# whole design is that a power cycle is a full reset.
#
# WHERE THE MACHINE-ID GOES, AND WHAT THAT COSTS. The FAT boot partition,
# which is the only writable storage outside the overlay and is already where
# otpunit/config.py keeps the operator's settings. It is mounted with
# `defaults`, so every file on it is 0755 root:root and readable by EVERY
# local account on this machine -- the `otp` user the unit runs as included
# -- as well as by anyone who can take the card out and mount it. A
# machine-id is an identifier, not key material, and no pad byte or password
# is written here; but "only someone holding the card" would be the wrong
# thing to believe about it.
#
# EXITS NON-ZERO WHEN IT CANNOT DO ITS JOB, deliberately. A unit whose
# identity silently reverts every power cycle is the fault this exists to
# fix, and a script that shrugged at it would leave the fault in place with
# nothing to read. The unit is WantedBy= rather than RequiredBy= sysinit,
# so a failure here is loud without holding the boot open.
#
# TWO OPTIONS, AND THEY EXIST TO BE TESTED. otp-unit-identity.service passes
# neither, so the shipped behaviour is the defaults; tests/test_overlay_root.py
# passes both, because every path here is absolute and a script whose only
# exercise is an emulated boot is one whose failure modes are found by an
# emulated boot. Same shape as device/install.sh's own option loop.
#
#   --boot-dir DIR   where the persistent store lives
#   --root DIR       a prefix for /etc, so the read of machine-id can be
#                    pointed at a tree a test builds
set -uo pipefail

BOOT_DIR=""
ROOT_DIR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --boot-dir) BOOT_DIR="${2:-}"; shift ;;
        --root) ROOT_DIR="${2:-}"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
ETC_DIR="$ROOT_DIR/etc"
if [ -z "$BOOT_DIR" ]; then
    BOOT_DIR=/boot/firmware
    [ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
fi
STORE="$BOOT_DIR/otp-identity"

note() { printf 'otp-identity: %s\n' "$*" >&2; }

rc=0

# OUTSIDE THE OVERLAY, PROVEN RATHER THAN ASSUMED, and this is the check
# that stops the whole script being a no-op nobody notices. If
# /boot/firmware failed to mount, $BOOT_DIR falls back to a plain directory
# on the root filesystem -- so every copy below would go into the overlay's
# tmpfs, be discarded by the power cycle, and be written again next boot
# from the value it had just lost. Every read-back would agree with itself
# and nothing would ever look wrong.
#
# findmnt --target answers for the CONTAINING mount, so a store that is just
# a directory inside the root reports the root's source rather than nothing.
# Same call and same reasoning as harness/img-guest-check.sh's
# boot-partition-separate, which is the check that reads this from outside.
STORE_SRC=$(findmnt -no SOURCE --target "$BOOT_DIR" 2>/dev/null || true)
ROOT_SRC=$(findmnt -no SOURCE / 2>/dev/null || true)
if [ -z "$STORE_SRC" ] || [ "$STORE_SRC" = "$ROOT_SRC" ]; then
    note "$BOOT_DIR is on the same filesystem as / (${STORE_SRC:-unknown}), so"
    note "  it is INSIDE the read-only overlay and nothing written there"
    note "  survives a power cycle. Refusing to pretend this unit has a"
    note "  stable identity."
    exit 1
fi

if ! mkdir -p "$STORE" 2>/dev/null; then
    note "cannot create $STORE, so this unit's identity cannot outlive"
    note "  the power cycle"
    exit 1
fi

# --- the machine id -------------------------------------------------------

# What PID 1 is actually using this boot. On the first boot of a fresh card
# this is the ID systemd generated a moment ago; on every boot after it, it
# is the one the initramfs restored (see the overlay script in
# device/install.sh).
live=$(cat "$ETC_DIR/machine-id" 2>/dev/null)
case "$live" in
    ""|*[!0-9a-f]*) live="" ;;
esac
[ "${#live}" = 32 ] || live=""

if [ -z "$live" ]; then
    note "$ETC_DIR/machine-id is not 32 hex characters, so there is no identity"
    note "  to keep. systemd will call every boot a first boot."
    rc=1
elif [ ! -s "$STORE/machine-id" ]; then
    if printf '%s\n' "$live" > "$STORE/machine-id" 2>/dev/null; then
        note "recorded this unit's machine-id in $STORE/machine-id; from the"
        note "  next boot on, the initramfs will put it back before PID 1"
        note "  reads it"
    else
        note "could not write $STORE/machine-id"
        rc=1
    fi
else
    kept=$(cat "$STORE/machine-id" 2>/dev/null)
    kept=${kept%%[![:xdigit:]]*}
    if [ "$kept" != "$live" ]; then
        # NOT overwritten. A stored id that does not match the running one
        # means the initramfs restore did not happen -- and the stored value
        # is the one that has a chance of being restored next time, so
        # replacing it with this boot's random id would make the store chase
        # a value that changes every boot and never converge.
        note "the stored machine-id is not the one this boot is using, so the"
        note "  initramfs did not restore it. Left alone; see the overlay"
        note "  script's otp_restore_machine_id."
        rc=1
    fi
fi

sync 2>/dev/null || true
exit "$rc"

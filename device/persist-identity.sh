#!/usr/bin/env bash
#
# Keep this unit's identity across the power-cycle.
#
# Run once per boot by otp-unit-identity.service, before anything that
# reads either thing it looks after. Installed to /opt/otp-unit by
# device/install.sh, and only on a machine whose root is the read-only
# overlay -- on a writable root /etc keeps both of these by itself and
# copying SSH private keys onto a FAT partition would buy nothing.
#
# WHAT IS PERSISTED, and nothing else:
#
#   machine-id      because /etc/machine-id inside the overlay reverts to
#                   pi-gen's `uninitialized` on every power cycle, and
#                   systemd reads that word as "first boot" -- preset-all
#                   on every boot, ssh.socket re-enabled on every boot,
#                   regenerate_ssh_host_keys.service on every boot.
#   ssh host keys   because that last one deletes and regenerates them
#                   (ExecStartPre=/usr/bin/rm -f /etc/ssh/ssh_host_*_key*),
#                   so the fingerprint of a machine that prints one-time
#                   pads changed every time somebody switched it off.
#
# WHERE, AND WHAT THAT COSTS. The FAT boot partition, which is the only
# writable storage outside the overlay and is already where
# otpunit/config.py keeps the operator's settings. FAT has no permission
# bits, so the private keys sit there readable by anyone who can mount the
# card -- which is the same set of people who can already read them off the
# ext4 root, because neither is encrypted and the card IS the device. It is
# not a new exposure, but it is worth saying rather than implying: this
# machine's SSH host keys are on the partition an operator is told to write
# files on.
#
# The MODE is restored on the way back in, because FAT cannot carry it:
# sshd refuses a private key it can read from a group or from the world.
#
# EXITS NON-ZERO WHEN IT CANNOT DO ITS JOB, deliberately. A unit whose
# identity silently reverts every power cycle is the fault this exists to
# fix, and a script that shrugged at it would leave the fault in place with
# nothing to read. The unit is WantedBy= rather than RequiredBy= sysinit,
# so a failure here is loud without holding the boot open.
set -uo pipefail

BOOT_DIR=/boot/firmware
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
STORE="$BOOT_DIR/otp-identity"
SSH_STORE="$STORE/ssh"

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
# st_dev, because that is the question: is the store on the same filesystem
# as /? An overlayfs root and a vfat partition have different device
# numbers; a directory inside the root has the same one.
if [ "$(stat -c %d "$BOOT_DIR" 2>/dev/null)" = "$(stat -c %d / 2>/dev/null)" ]; then
    note "$BOOT_DIR is on the same filesystem as /, so it is INSIDE the"
    note "  read-only overlay and nothing written there survives a power"
    note "  cycle. Refusing to pretend this unit has a stable identity."
    exit 1
fi

if ! mkdir -p "$SSH_STORE" 2>/dev/null; then
    note "cannot create $SSH_STORE, so this unit's identity cannot outlive"
    note "  the power cycle"
    exit 1
fi

# --- the machine id -------------------------------------------------------

# What PID 1 is actually using this boot. On the first boot of a fresh card
# this is the ID systemd generated a moment ago; on every boot after it, it
# is the one the initramfs restored (see the overlay script in
# device/install.sh).
live=$(cat /etc/machine-id 2>/dev/null)
case "$live" in
    ""|*[!0-9a-f]*) live="" ;;
esac
[ "${#live}" = 32 ] || live=""

if [ -z "$live" ]; then
    note "/etc/machine-id is not 32 hex characters, so there is no identity"
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

# --- the SSH host keys ----------------------------------------------------

if ! command -v ssh-keygen >/dev/null 2>&1; then
    note "no ssh-keygen here, so there are no host keys to look after"
    exit "$rc"
fi
install -d -m 0755 /etc/ssh

shopt -s nullglob
stored=("$SSH_STORE"/ssh_host_*_key)

if [ "${#stored[@]}" -gt 0 ]; then
    # RESTORE, with the modes FAT could not keep. 0600 on the private key
    # because sshd refuses to load one that is group- or world-readable and
    # says so only in its own log.
    for key in "${stored[@]}"; do
        install -m 0600 "$key" /etc/ssh/ || rc=1
        [ -f "$key.pub" ] && { install -m 0644 "$key.pub" /etc/ssh/ || rc=1; }
    done
    note "restored ${#stored[@]} host key(s) from $SSH_STORE"
else
    # ADOPT WHAT IS THERE, and only generate if there is nothing. On the
    # first boot of an image this runs after regenerate_ssh_host_keys.service
    # (ConditionFirstBoot=yes) has already made a fresh set, so what is
    # adopted is unique to this card. On the documented "run install.sh on a
    # Pi you already have" path there is no first boot, that unit does not
    # run, and adopting means the machine KEEPS the host key people already
    # know rather than having it silently replaced by a provisioning script.
    live_keys=(/etc/ssh/ssh_host_*_key)
    if [ "${#live_keys[@]}" -eq 0 ]; then
        note "no host keys anywhere yet; generating this unit's own"
        ssh-keygen -A >/dev/null 2>&1 || rc=1
        live_keys=(/etc/ssh/ssh_host_*_key)
    fi
    if [ "${#live_keys[@]}" -eq 0 ]; then
        note "still no host keys after ssh-keygen -A"
        rc=1
    fi
    for key in "${live_keys[@]}"; do
        # `cp`, NOT `install -m`. A chmod to a mode vfat cannot represent is
        # refused by the filesystem, so `install -m 0600` onto the store
        # fails outright -- and the mode there is worth nothing anyway,
        # which is why it is re-applied on the way back in above.
        cp "$key" "$SSH_STORE/" 2>/dev/null || rc=1
        [ -f "$key.pub" ] && { cp "$key.pub" "$SSH_STORE/" 2>/dev/null || rc=1; }
    done
    note "recorded ${#live_keys[@]} host key(s) in $SSH_STORE"
fi

sync 2>/dev/null || true
exit "$rc"

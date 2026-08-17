#!/usr/bin/env bash
#
# Keep this unit's machine-id, and the operator's login, across the
# power-cycle.
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
#   the UID-1000 account's password hash, and ONLY once an operator has
#                   seeded one themselves. userconf-service applies
#                   /boot/firmware/userconf.txt with `chpasswd -e` into
#                   /etc/shadow and then DELETES the seed. /etc is inside the
#                   overlay and the FAT partition is not, so the credential
#                   died with the power while the one file that could reapply
#                   it was destroyed by the boot that consumed it -- the
#                   operator's password worked for exactly one boot and the
#                   account reverted to the random FIRST_USER_PASS
#                   image/build.sh generates at build time, which nobody has.
#                   The device can be connected to a keyboard and a screen,
#                   so that login is a real recovery path rather than a
#                   nicety, which is why the repository owner's decision on
#                   the three candidates in device/install.sh was "persist
#                   it".
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
# WHERE THE STORE IS, AND WHAT THAT COSTS. The FAT boot partition, which is
# the only writable storage outside the overlay and is already where
# otpunit/config.py keeps the operator's settings. It is mounted with
# `defaults`, so every file on it is 0755 root:root and readable by EVERY
# local account on this machine -- the `otp` user itself, and the `lp` uid
# CUPS runs filters as -- as well as by anyone who can take the card out and
# mount it. A machine-id is an identifier and no pad byte is written here.
#
# A PASSWORD HASH IS DIFFERENT, and this is the paragraph to read before
# trusting the paragraph above. It is offline-crackable at leisure by anyone
# in that set. Measured with python's crypt on the build container:
# sha512crypt at the 5000 rounds `openssl passwd -6` defaults to -- which is
# the hash the Raspberry Pi documentation tells an operator to generate --
# verifies in 2.111 ms, so 474 guesses per second per core, before any GPU.
# yescrypt at $y$j9T$ took 16.146 ms, 62 per second per core, and is
# memory-hard as well. A low-entropy password on that partition is a
# recovered password.
#
# WHAT IT IS NOT is a new exposure, and that is the whole reason this was
# safe to do. The bytes written here are the bytes of the operator's own
# /boot/firmware/userconf.txt -- same crypt string, same partition, same
# 0755 -- and this store is only ever written by the userconfig.service
# drop-in that fires when that file was applied (see device/install.sh). A
# unit whose operator never seeded a credential never gets a hash on its
# card: pi-gen's random FIRST_USER_PASS is never copied out of /etc.
# "Persist the credential" and the rejected "keep the seed file" are the
# same exposure; what this buys over keeping the seed is that userconf-pi's
# apply path -- which ends in cancel-rename starting a getty on the front
# panel's tty -- does not run on every boot, and that the store gets the
# validation and the outside-the-overlay refusal below.
#
# WHAT IT COSTS AN OPERATOR TO UNDO, said here because it is the one way
# this is worse than the option it replaced: deleting userconf.txt from the
# card no longer takes the hash off the card. `rm -rf
# /boot/firmware/otp-identity/credential` is what does, and docs/IMAGE.md
# says so where an operator will find it.
#
# EXITS NON-ZERO WHEN IT CANNOT DO ITS JOB, deliberately. A unit whose
# identity silently reverts every power cycle is the fault this exists to
# fix, and a script that shrugged at it would leave the fault in place with
# nothing to read. The unit is WantedBy= rather than RequiredBy= sysinit,
# so a failure here is loud without holding the boot open.
#
# TWO PHASES, AND THE SPLIT BETWEEN THEM IS ABOUT WHEN.
#
#   the default          record the machine-id, RESTORE the credential.
#                        otp-unit-identity.service runs this at sysinit,
#                        which is before any getty exists and therefore
#                        before a login can be attempted.
#   --record-credential  RECORD the credential, and nothing else. Run as an
#                        ExecStartPost on userconfig.service, so it fires
#                        exactly when the wizard applied an operator's seed
#                        and on no other boot.
#
# THE ORDER OF THOSE TWO IS THE ANSWER TO "what happens on a boot where the
# credential is restored AND a fresh userconf.txt is present", and it is
# deliberate rather than incidental. The restore runs at sysinit; the wizard
# runs at multi-user and applies the new seed OVER the restored hash, so the
# operator's new password is the one in force by the end of that boot. Its
# ExecStartPost then replaces the store, so the new password is also the one
# the NEXT boot restores. Precedence, stated once: a fresh seed beats the
# store, and the store beats the image's random build-time password. A
# malformed seed reaches neither -- userconf-service fails before
# ExecStartPost runs, the store is untouched, and the password the restore
# put back at sysinit is still the one that works.
#
# EXITS NON-ZERO WHEN IT CANNOT DO ITS JOB, in both phases, and see
# device/install.sh for why the ExecStartPost that runs the second one is
# nevertheless prefixed with `-`.
#
# THREE OPTIONS, AND THEY EXIST TO BE TESTED. otp-unit-identity.service
# passes none of them, so the shipped behaviour is the defaults;
# tests/test_overlay_root.py passes them, because every path here is absolute
# and a script whose only exercise is an emulated boot is one whose failure
# modes are found by an emulated boot. Same shape as device/install.sh's own
# option loop.
#
#   --boot-dir DIR   where the persistent store lives
#   --root DIR       a prefix for /etc, so the reads of machine-id, passwd
#                    and shadow can be pointed at a tree a test builds
#   --record-credential   the second phase above
set -uo pipefail

BOOT_DIR=""
ROOT_DIR=""
MODE=persist
while [ $# -gt 0 ]; do
    case "$1" in
        --boot-dir) BOOT_DIR="${2:-}"; shift ;;
        --root) ROOT_DIR="${2:-}"; shift ;;
        --record-credential) MODE=record-credential ;;
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
CRED_STORE="$STORE/credential"

# --root is a test affordance, so it has to reach the one command here that
# does not take a path. Unset on a real unit, where a bare `chpasswd` is
# wanted: `chpasswd --root /` chroots, and asking a boot-critical account
# change to do that for the sake of symmetry is not a trade worth making.
CHPASSWD=(chpasswd -e)
if [ -n "$ROOT_DIR" ]; then
    CHPASSWD=(chpasswd -e --root "$ROOT_DIR")
fi

note() { printf 'otp-identity: %s\n' "$*" >&2; }

# --- the credential, and the three things that read or write it -----------

# THE ONLY ACCOUNT EITHER PHASE MAY TOUCH: the UID-1000 user.
#
# This is one rule instead of four, and it is tighter than the four it
# replaces. userconf-pi validates a seed's user field against `[a-z][a-z0-9-]*`,
# a 32-character bound and `!= root`, and then RENAMES the UID-1000 user to
# whatever it names -- so the only account /boot/firmware/userconf.txt can
# ever set a password on is the UID-1000 one. Keying off the uid instead of
# re-transcribing the regex says exactly that: whoever can write this store
# can already write userconf.txt, and this must hand them no account the
# documented path would not.
first_user() {
    awk -F: '$3 == 1000 { print $1; exit }' "$ETC_DIR/passwd" 2>/dev/null
}

live_hash() {
    awk -F: -v u="$1" '$1 == u { print $2; exit }' "$ETC_DIR/shadow" 2>/dev/null
}

# A crypt(3) string and nothing else.
#
# THIS IS THE GUARD THAT STOPS A TRUNCATED STORE BECOMING A PASSWORDLESS
# ACCOUNT. `chpasswd -e` writes its second field into /etc/shadow verbatim,
# and an EMPTY second field is an account that logs in with no password at
# all -- on a machine that boots to a console, with a keyboard and a screen
# attached, and whose whole point is printing key material. A card pulled
# mid-write, a full partition, or anyone with a card reader is all it takes
# to produce one. `!` and `*` are refused by the same clause and for the
# mirror reason: a store that can LOCK the only account is a store that can
# take away the recovery path this change exists to provide.
#
# Three `$` separators, because every modern crypt string has at least
# `$id$salt$hash`; twenty characters, because `$6$x$y` satisfies the first
# clause; no colon, because a colon would forge a second chpasswd field; no
# whitespace, because nothing legitimate has any.
is_crypt_hash() {
    case "$1" in
        '$'*'$'*'$'*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        *[:[:space:]]*) return 1 ;;
    esac
    [ "${#1}" -ge 20 ]
}

# NEITHER PHASE EVER PRINTS THE HASH. This script's stderr is the journal,
# the journal is forwarded to the serial console under the tier-3 harness,
# and that console is uploaded as a CI artifact. A length is enough to tell
# "empty" from "locked" from "a real hash" while reading a boot log.
credential_restore() {
    local stored user hash want live

    if [ ! -s "$CRED_STORE" ]; then
        note "no credential kept in $CRED_STORE, so this unit's login is the"
        note "  random one image/build.sh generated. Write a userconf.txt to"
        note "  the boot partition to set one; it will be kept from then on."
        return 0
    fi

    # ONE LINE, and that is a validation rather than tidiness: `chpasswd`
    # reads EVERY line it is handed, so a two-line store is a store that sets
    # two accounts' passwords. $() strips trailing newlines, so a well-formed
    # single-line file has no newline left in it at all.
    stored=$(cat "$CRED_STORE" 2>/dev/null)
    case "$stored" in
        *$'\n'*)
            note "$CRED_STORE has more than one line, which chpasswd would"
            note "  read as more than one account. Refusing all of it."
            return 1
            ;;
    esac

    user=${stored%%:*}
    hash=${stored#*:}
    if [ "$user" = "$stored" ]; then
        note "$CRED_STORE is not user:hash, so there is no credential in it"
        return 1
    fi

    want=$(first_user)
    if [ -z "$want" ] || [ "$user" != "$want" ]; then
        note "the kept credential names '$user', which is not this unit's"
        note "  UID-1000 account (${want:-none}). Refusing: userconf.txt can"
        note "  only ever set that account's password and this store must not"
        note "  be able to set any other."
        return 1
    fi

    if ! is_crypt_hash "$hash"; then
        note "the kept credential's hash (${#hash} characters) is not a"
        note "  crypt(3) string. Refusing: applying it would leave $user"
        note "  locked out or, if it is empty, with no password at all."
        return 1
    fi

    live=$(live_hash "$user")
    if [ "$live" = "$hash" ]; then
        note "$user's password is already the kept one"
        return 0
    fi

    if ! printf '%s:%s\n' "$user" "$hash" | "${CHPASSWD[@]}"; then
        note "chpasswd could not put $user's kept password back, so the"
        note "  operator's login does not work on this boot"
        return 1
    fi

    # READ IT BACK, because chpasswd's exit status says the command ran and
    # not that /etc/shadow changed. A `chpasswd` that is missing, that is a
    # stub, or that wrote to a different tree answers 0 in at least one of
    # those cases, and a restore nobody verified is exactly the shape of
    # defect this repository keeps finding green.
    live=$(live_hash "$user")
    if [ "$live" != "$hash" ]; then
        note "chpasswd exited 0 but $ETC_DIR/shadow still does not hold the"
        note "  kept hash for $user, so the operator's password is NOT back"
        return 1
    fi

    note "put $user's password back from $CRED_STORE (${#hash}-character"
    note "  hash). A fresh userconf.txt this boot will override it and be"
    note "  kept in its place."
    return 0
}

credential_record() {
    local user hash

    user=$(first_user)
    if [ -z "$user" ]; then
        note "no UID-1000 account in $ETC_DIR/passwd, so there is no login"
        note "  to keep"
        return 1
    fi

    hash=$(live_hash "$user")
    if ! is_crypt_hash "$hash"; then
        note "$user has no usable password hash (${#hash} characters), so"
        note "  there is nothing worth keeping. Left $CRED_STORE alone rather"
        note "  than replacing a good credential with a locked or empty one."
        return 1
    fi

    # Written beside and renamed, so that losing power mid-write leaves the
    # previous credential rather than half of this one. vfat has no atomic
    # anything, but a rename within one directory is the closest it offers.
    if printf '%s:%s\n' "$user" "$hash" > "$CRED_STORE.new" 2>/dev/null \
       && mv -f "$CRED_STORE.new" "$CRED_STORE" 2>/dev/null; then
        note "kept $user's password (${#hash}-character hash) in $CRED_STORE,"
        note "  so it survives the power cycle. It is the same hash the"
        note "  operator's own userconf.txt carried, on the same partition:"
        note "  0755, readable by every account here and by anyone who can"
        note "  mount the card."
        return 0
    fi
    rm -f "$CRED_STORE.new" 2>/dev/null || true
    note "could not write $CRED_STORE, so the password applied this boot"
    note "  will be gone at the next power cycle"
    return 1
}

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

if [ "$MODE" = record-credential ]; then
    # THE SECOND PHASE, AND NOTHING ELSE. It runs from the wizard's own
    # ExecStartPost, at multi-user, on the one boot in a unit's life where an
    # operator's seed was applied. The machine-id below is a sysinit job and
    # doing it again from here would only duplicate its complaints.
    credential_record || rc=1
else

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

# --- and the operator's login, put back before a login is possible --------
#
# RESTORE ONLY. Nothing in this phase can create a credential on the card:
# the store is written by --record-credential and by nothing else, so a unit
# whose operator never seeded a userconf.txt never has a password hash on its
# boot partition at all. That is the whole of the difference between this and
# copying /etc/shadow out every boot, and it is the reason the exposure this
# adds is bounded by the exposure the operator already chose.
credential_restore || rc=1

fi

sync 2>/dev/null || true
exit "$rc"

#!/usr/bin/env bash
#
# Keep this unit's machine-id, and the operator's login, across the
# power-cycle. Installed by device/install.sh only on a machine whose root is
# the read-only overlay.
#
# Persisted, on the FAT boot partition (the only writable storage outside the
# overlay):
#
#   machine-id   /etc/machine-id otherwise reverts to `uninitialized` every
#                power cycle, which systemd reads as a first boot.
#   credential   the UID-1000 account's password hash, only once an operator
#                has seeded one with userconf.txt: userconf-service applies
#                it to /etc/shadow (inside the overlay) and deletes the seed.
#
# SSH host keys are not persisted: the appliance does not run sshd.
#
# The partition is vfat mounted `defaults`, so the store is readable by every
# local account and by anyone who can mount the card, and a hash can be
# attacked offline. It is the same hash, on the same partition, as the
# operator's own userconf.txt -- but deleting userconf.txt no longer removes
# it; `rm /boot/firmware/otp-identity/credential` does (docs/IMAGE.md).
#
# Two phases:
#
#   the default          record the machine-id and restore the credential;
#                        run by otp-unit-identity.service at sysinit, before
#                        any getty exists.
#   --record-credential  record the credential only; run as an ExecStartPost
#                        on userconfig.service, so it fires only when the
#                        wizard applied an operator's seed.
#
# Precedence: a fresh seed beats the store (the wizard runs after the
# restore, and its ExecStartPost then replaces the store), and the store
# beats the image's random build-time password. The stored hash is always
# applied to the UID-1000 account, whatever name the store carries.
#
# Exits non-zero when it cannot do its job, in both phases.
#
# Options (the unit passes none; tests use them):
#
#   --boot-dir DIR   where the persistent store lives
#   --root DIR       a prefix for /etc
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

# A note left on the card whenever a restore is refused: the journal is
# volatile, and taking the card out is the one thing a locked-out operator
# can still do. It never contains a hash, and is removed when a restore
# works.
CRED_NOTE="$STORE/credential-not-restored.txt"

# --root has to reach chpasswd too. Unset on a real unit, where a bare
# `chpasswd` is wanted: `chpasswd --root /` chroots.
CHPASSWD=(chpasswd -e)
if [ -n "$ROOT_DIR" ]; then
    CHPASSWD=(chpasswd -e --root "$ROOT_DIR")
fi

note() { printf 'otp-identity: %s\n' "$*" >&2; }

# A reason credential_restore declined: same wording in the journal and on
# the card, under a header telling the operator what to do.
refuse() {
    local line
    for line in "$@"; do note "$line"; done
    {
        printf 'This unit refused to restore the kept login at its last boot.\n'
        printf 'The account is back on the random password the image was built\n'
        printf 'with, which nobody has. Write a fresh userconf.txt here.\n\n'
        printf '%s\n' "$@"
    } > "$CRED_NOTE" 2>/dev/null || true
}

# --- the credential, and the three things that read or write it -----------

# The only account either phase may touch: the UID-1000 user. userconf-pi
# renames UID 1000 to whatever a seed names, so that is the only account
# userconf.txt can set a password on, and this store must reach no other.
# The store's user field is a label, not the authorisation; see
# credential_restore.
first_user() {
    awk -F: '$3 == 1000 { print $1; exit }' "$ETC_DIR/passwd" 2>/dev/null
}

live_hash() {
    awk -F: -v u="$1" '$1 == u { print $2; exit }' "$ETC_DIR/shadow" 2>/dev/null
}

# A crypt(3) string and nothing else. `chpasswd -e` writes its second field
# verbatim, so an empty one is a passwordless account and `!` or `*` locks
# the only account. Requires `$id$salt$hash` (three `$`), at least twenty
# characters, and no colon (a forged field) or whitespace.
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

# The store's user field comes off a partition anyone can write and is quoted
# in notes that reach a console, so it is stripped to safe characters and cut
# to 32, the longest a user name may be.
safe_name() {
    printf '%s' "$1" | tr -cd 'a-zA-Z0-9._-' | cut -c1-32
}

# Neither phase prints the hash, only its length: stderr is the journal,
# which tier 3 forwards to a console uploaded as a CI artifact.
credential_restore() {
    local rc
    credential_restore_inner
    rc=$?
    # The note on the card exists exactly when the last restore refused; kept
    # here so no return path below can forget it.
    [ "$rc" = 0 ] && rm -f "$CRED_NOTE" 2>/dev/null
    return "$rc"
}

credential_restore_inner() {
    local stored user hash want live have_store=no have_fragment=no relabel=no

    [ -e "$CRED_STORE" ] && have_store=yes
    [ -e "$CRED_STORE.new" ] && have_fragment=yes

    if [ "$have_store" = no ] && [ "$have_fragment" = no ]; then
        note "no credential kept in $CRED_STORE, so this unit's login is the"
        note "  random one image/build.sh generated. Write a userconf.txt to"
        note "  the boot partition to set one; it will be kept from then on."
        return 0
    fi

    # A zero-length store, or a `.new` with no store, is not a card that was
    # never seeded: credential_record writes `.new` and renames it, vfat has
    # no atomic rename, and a power cut or full partition leaves this. Fail
    # loudly; an unseeded card has neither name on it.
    if [ ! -s "$CRED_STORE" ]; then
        refuse "there is no usable credential in $CRED_STORE. This is NOT a" \
               "  card that was never seeded -- one of the two names a record" \
               "  uses is on it, and whichever it is holds nothing usable:" \
               "  credential present=$have_store (zero length)," \
               "  credential.new present=$have_fragment (never committed). A" \
               "  record writes .new and renames it into place, and vfat has" \
               "  no atomic rename, so a power cut mid-record or a full" \
               "  partition leaves exactly this. The login that was kept here" \
               "  is GONE; write a fresh userconf.txt to the boot partition."
        return 1
    fi

    # A fragment beside an intact store: the previous credential survived.
    # Remove the fragment so later boots do not report it again.
    if [ "$have_fragment" = yes ]; then
        note "$CRED_STORE.new is beside the store, so a record lost the power"
        note "  or the room mid-write. The credential below is the PREVIOUS"
        note "  one, which is what writing beside and renaming is for."
        note "  Removing the fragment."
        rm -f "$CRED_STORE.new" 2>/dev/null || true
    fi

    # One line only: chpasswd reads every line it is handed, so a second line
    # would set a second account's password. $() strips the trailing newline
    # of a well-formed file.
    stored=$(cat "$CRED_STORE" 2>/dev/null)
    case "$stored" in
        *$'\n'*)
            refuse "$CRED_STORE has more than one line, which chpasswd would" \
                   "  read as more than one account. Refusing all of it."
            return 1
            ;;
    esac

    user=${stored%%:*}
    hash=${stored#*:}
    if [ "$user" = "$stored" ]; then
        refuse "$CRED_STORE is not user:hash, so there is no credential in it"
        return 1
    fi

    # A store is only ever applied to whoever holds UID 1000 this boot.
    want=$(first_user)
    if [ -z "$want" ]; then
        refuse "there is no UID-1000 account in $ETC_DIR/passwd, so there is" \
               "  no account this store may be applied to. Refusing: the one" \
               "  rule here is that only the UID-1000 user is ever touched," \
               "  and on this boot there is no such user."
        return 1
    fi

    # $want, not $user: this note is about the account the hash would land on,
    # and $user comes off a partition anyone with a card reader can write.
    if ! is_crypt_hash "$hash"; then
        refuse "the kept credential's hash (${#hash} characters) is not a" \
               "  crypt(3) string. Refusing: applying it would leave $want" \
               "  locked out or, if it is empty, with no password at all."
        return 1
    fi

    # The name in the store is a label, not an authorisation. userconf-pi
    # renames UID 1000 to the seed's name, but the rename lives in /etc and
    # /home, inside the overlay, and dies with the power while the store does
    # not; refusing a mismatch would lock the operator out. $user is never
    # handed to chpasswd, so a store claiming `root:` cannot set root's
    # password.
    if [ "$user" != "$want" ]; then
        relabel=yes
        note "the kept credential names '$(safe_name "$user")'; this unit's"
        note "  UID-1000 account is $want. userconf-pi renames UID 1000 to"
        note "  whatever a seed names, and that rename lives in /etc and"
        note "  /home -- inside the overlay -- so it does not survive the"
        note "  power cycle while this store does. Applying the kept hash to"
        note "  $want and rewriting the store to match."
    fi

    live=$(live_hash "$want")
    if [ "$live" = "$hash" ]; then
        note "$want's password is already the kept one"
        [ "$relabel" = no ] || credential_relabel "$want" "$hash"
        return 0
    fi

    if ! printf '%s:%s\n' "$want" "$hash" | "${CHPASSWD[@]}"; then
        refuse "chpasswd could not put $want's kept password back, so the" \
               "  operator's login does not work on this boot"
        return 1
    fi

    # Read it back: chpasswd's exit status does not prove /etc/shadow changed.
    live=$(live_hash "$want")
    if [ "$live" != "$hash" ]; then
        refuse "chpasswd exited 0 but $ETC_DIR/shadow still does not hold the" \
               "  kept hash for $want, so the operator's password is NOT back"
        return 1
    fi

    # Only now, after the verify: a store rewritten before the
    # apply was read back would be a store relabelled for a login that never
    # came back.
    [ "$relabel" = no ] || credential_relabel "$want" "$hash"

    note "put $want's password back from $CRED_STORE (${#hash}-character"
    note "  hash). A fresh userconf.txt this boot will override it and be"
    note "  kept in its place."
    return 0
}

# Best effort: the login is already back, so a failed rewrite must not fail
# the restore. The next boot finds the same mismatch and applies it again.
credential_relabel() {
    if credential_write "$1" "$2"; then
        note "rewrote $CRED_STORE to name $1"
        return 0
    fi
    note "could not rewrite $CRED_STORE to name $1. The login IS back for"
    note "  this boot; the store still carries the stale name, so the next"
    note "  boot will say all of this again and apply it again."
    return 1
}

# Written beside and renamed, so losing power mid-write leaves the previous
# credential; credential_restore reads a leftover `.new` as the tell.
credential_write() {
    if printf '%s:%s\n' "$1" "$2" > "$CRED_STORE.new" 2>/dev/null \
       && mv -f "$CRED_STORE.new" "$CRED_STORE" 2>/dev/null; then
        return 0
    fi
    rm -f "$CRED_STORE.new" 2>/dev/null || true
    return 1
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

    if credential_write "$user" "$hash"; then
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

# Prove the store is outside the overlay. If /boot/firmware failed to mount,
# $BOOT_DIR is a plain directory on the root, and everything written there
# would be discarded by the power cycle while every read-back agreed.
# findmnt --target answers for the containing mount, so such a directory
# reports the root's source.
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
    # The machine-id is a sysinit job; this phase only records the credential.
    credential_record || rc=1
else

# --- the machine id -------------------------------------------------------

# What PID 1 is using this boot: generated on a fresh card's first boot,
# restored by the initramfs afterwards (see the overlay script in
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
        # Not overwritten: a mismatch means the initramfs restore did not
        # happen, and replacing the stored id with this boot's random one would
        # never converge.
        note "the stored machine-id is not the one this boot is using, so the"
        note "  initramfs did not restore it. Left alone; see the overlay"
        note "  script's otp_restore_machine_id."
        rc=1
    fi
fi

# --- and the operator's login, put back before a login is possible --------
# Restore only: the store is written by --record-credential alone, so a unit
# whose operator never seeded a userconf.txt never has a hash on its card.
credential_restore || rc=1

fi

sync 2>/dev/null || true
exit "$rc"

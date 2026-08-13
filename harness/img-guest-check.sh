#!/usr/bin/env bash
#
# Runs INSIDE the tier-3 image boot, as root, once per boot.
#
#   img-guest-check.sh
#
# Started by otp-unit-imgcheck.service, which exists only while the kernel
# command line carries otp.imgcheck=<phase> -- something nothing but
# harness/img-boot.sh puts there. The phase is read from /proc/cmdline rather
# than passed as an argument so that the unit file needs no per-boot edit.
#
# WHY THIS EXISTS. The read-only overlay was the largest completely
# unobserved surface in the project (issue #9). Tier 2 cannot see it: two
# mechanisms were tried in that Debian guest and both failed, one of them by
# panicking the kernel. Tier 3 can, because tier 3 boots the artifact that is
# actually flashed, with the overlay the image was built with -- and the
# overlay is a property of a booted machine, so a booted machine has to be
# the one asked.
#
# TWO BOOTS, and the second is the whole point:
#
#   boot1  the overlay is engaged. Writes a sentinel to / and a setting to
#          /boot/firmware through config.save().
#   boot2  the same card, powered off and on. The sentinel must be GONE --
#          that is the overlay discarding -- and the setting must still be
#          there -- that is the boot partition being outside it.
#
# An absence proves nothing on its own: a boot1 that never ran, a sentinel
# written to the wrong path, or a check looking somewhere else all produce
# the same missing file. So boot2 states a presence with the same fixture
# first. The setting written in boot1 has to be readable in boot2 before the
# sentinel's absence is allowed to mean anything, and the sentinel is written
# again afterwards and confirmed visible, which proves the path and the test
# would have found it had it survived.
#
# Prints one line per check in the format the host greps for:
#
#   OTP-CHECK <phase> <name> PASS|FAIL <detail>
#   OTP-GUEST-DONE <phase>
#   OTP-RESULT <phase> <passed>/<total>
#
# The phase is in every marker because the host requires each expected phase
# to have reported exactly once. A boot that never happened produces no
# output, which is indistinguishable from a boot with nothing to say -- the
# lesson tier 2's three-phase gate was built out of.
#
# Never exits non-zero on a failed check. It reports them all and lets the
# host decide; stopping at the first would hide the rest behind it.
set -uo pipefail

PHASE=unknown
for token in $(tr -d '\n' < /proc/cmdline); do
    case "$token" in
        otp.imgcheck=*) PHASE="${token#otp.imgcheck=}" ;;
    esac
done

PASS=0
TOTAL=0

BOOTDIR=/boot/firmware
# Written to / in every phase and looked for at the start of the next one.
# At the root of the filesystem on purpose: /tmp and /run are tmpfs whatever
# the root is, so a sentinel there would vanish across a power-cycle on a
# perfectly writable card and prove nothing.
SENTINEL=/otp-imgcheck-sentinel
# A page count no default and no panel choice produces, so its presence in
# boot2 can only be the write boot1 made. Same value and same reasoning as
# tier 2's MARK_PAGES.
MARK_PAGES=137

check() {
    local name="$1" ok="$2" detail="${3:-}"
    TOTAL=$((TOTAL + 1))
    if [ "$ok" = "yes" ]; then
        PASS=$((PASS + 1))
        printf 'OTP-CHECK %s %s PASS %s\n' "$PHASE" "$name" "$detail"
    else
        printf 'OTP-CHECK %s %s FAIL %s\n' "$PHASE" "$name" "$detail"
    fi
}

yesno() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }

echo "OTP-GUEST starting phase=$PHASE on $(uname -srm)"
# The command line, every phase. The tier-2 arc turned on being able to tell
# "the flag arrived and did nothing" from "the flag never arrived", and that
# distinction only existed because the guest echoed its own cmdline.
echo "OTP-CMDLINE $PHASE $(tr -d '\n' < /proc/cmdline)"

# --- was the sentinel from the previous boot discarded? -------------------

# FIRST, before anything else writes to /. In boot2 this is the answer to
# issue #9's last bullet; in boot1 it is the statement that the card came up
# clean, which is what makes boot2's reading of it mean something.
SENTINEL_SURVIVED=no
SENTINEL_WAS=""
if [ -e "$SENTINEL" ]; then
    SENTINEL_SURVIVED=yes
    SENTINEL_WAS=$(head -c 120 "$SENTINEL" 2>/dev/null | tr -d '\n')
fi

# --- the overlay itself ---------------------------------------------------

ROOT_FSTYPE=$(findmnt -no FSTYPE / 2>/dev/null | head -1)
ROOT_SOURCE=$(findmnt -no SOURCE / 2>/dev/null | head -1)
# The super options, which name the three layers. Read from mountinfo rather
# than from findmnt's OPTIONS, which reports the per-mount flags and would
# say rw,relatime for an overlay and for a plain ext4 alike.
ROOT_SUPER=$(awk '$5 == "/" {for (i = 7; i <= NF; i++) if ($i == "-") {print $(i + 3); exit}}' \
             /proc/self/mountinfo 2>/dev/null | head -1)
check root-is-overlay \
      "$(if [ "$ROOT_FSTYPE" = "overlay" ]; then echo yes; else echo no; fi)" \
      "fstype=${ROOT_FSTYPE:-?} source=${ROOT_SOURCE:-?} super=${ROOT_SUPER:-?}"

# The positive control for the sentinel checks, and it runs in both phases.
# "the file is gone" is satisfied by a rig that cannot write to / at all, by
# a path that does not exist, and by a boot that never got this far. Writing
# it and reading it back in the same phase removes all three.
WROTE=no
if printf 'phase=%s stamp=%s\n' "$PHASE" "$(date -u +%s 2>/dev/null)" \
        > "$SENTINEL" 2>/dev/null && [ -s "$SENTINEL" ]; then
    WROTE=yes
fi
check root-write-lands-and-is-readable "$WROTE" "$SENTINEL"

if [ "$PHASE" = "boot2" ]; then
    # The overlay is really discarding rather than merely mounted. Ordered
    # after the write above so that a failure here cannot be read as "the
    # harness could not write to / anyway".
    check root-writes-discarded-by-the-power-cycle \
          "$(if [ "$SENTINEL_SURVIVED" = "no" ]; then echo yes; else echo no; fi)" \
          "$SENTINEL before this boot: ${SENTINEL_WAS:-absent}"
fi

# --- the unit, on a read-only root ----------------------------------------

# Polled rather than slept through. otp-unit-imgcheck.service is only
# After= the unit, and Type=simple means "started" the instant the process is
# spawned, so without this the check would race a Python interpreter that has
# not imported anything yet. Bounded, because a unit that is never going to
# come up must still produce a report.
STATE=inactive
for _ in $(seq 1 45); do
    STATE=$(systemctl is-active otp-unit.service 2>&1 || true)
    [ "$STATE" = "active" ] && break
    sleep 2
done
check unit-active \
      "$(if [ "$STATE" = "active" ]; then echo yes; else echo no; fi)" \
      "is-active=$STATE"

RESTARTS=$(systemctl show otp-unit.service -p NRestarts --value 2>/dev/null || echo "?")
check unit-not-restart-looping \
      "$(if [ "${RESTARTS:-9}" -le 1 ] 2>/dev/null; then echo yes; else echo no; fi)" \
      "NRestarts=$RESTARTS"

# --- CUPS, which is the half of the design the overlay could break --------

# /etc/cups has to be writable for cupsd to start at all, and on a read-only
# root the answer is a tmpfs laid over it and repopulated from a template in
# /opt/otp-unit/cups-etc. If that is wrong the unit cannot print, and nothing
# short of a booted machine with the overlay engaged would say so.
ETC_CUPS_FS=$(findmnt -no FSTYPE /etc/cups 2>/dev/null | head -1)
check etc-cups-is-its-own-tmpfs \
      "$(if [ "$ETC_CUPS_FS" = "tmpfs" ]; then echo yes; else echo no; fi)" \
      "fstype=${ETC_CUPS_FS:-not a mount point}"

check cups-running "$(yesno systemctl is-active cups.service)" \
      "is-active=$(systemctl is-active cups.service 2>&1 || true)"

# install.sh gates on this at provisioning time, against the configuration as
# it stood in a chroot. This asks the daemon's own parser on the machine that
# has to run it, after the tmpfs was laid down and repopulated.
check cupsd-config-valid \
      "$(yesno cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf)" \
      ""

# --- settings, which must outlive the power-cycle -------------------------

# The boot partition has to be a separate mount, or "the setting survived"
# would be a statement about the overlay's tmpfs and would be false on the
# next power-cycle.
BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
BOOT_FS=$(findmnt -no FSTYPE --target "$BOOTDIR" 2>/dev/null || echo "?")
check boot-partition-separate \
      "$(if [ "$BOOT_SRC" != "?" ] && [ "$BOOT_SRC" != "$ROOT_SOURCE" ]; then echo yes; else echo no; fi)" \
      "$BOOTDIR src=$BOOT_SRC fstype=$BOOT_FS"

if [ "$PHASE" = "boot2" ]; then
    # BEFORE the write below, and read back through config.load() rather
    # than grepped for: a setting that persisted as bytes but no longer
    # parses is not a setting that persisted, because load() falls back to
    # the default for any field it cannot use, silently and by design.
    KEPT=$(python3 - <<'PY' 2>&1 || true
import sys
sys.path.insert(0, "/opt/otp-unit")
from otpunit import config
print(config.load().pages)
PY
)
    KEPT=${KEPT//[$'\n\r']/}
    check settings-survive-the-power-cycle \
          "$(if [ "$KEPT" = "$MARK_PAGES" ]; then echo yes; else echo no; fi)" \
          "pages=$KEPT want=$MARK_PAGES from ${BOOTDIR}/otp-unit.conf"
fi

# Through config.save() at its real CONFIG_PATH, in both phases. That code is
# the only writer on the device that has to work with the root read-only, and
# it is the thing under test rather than a shell redirect to somewhere
# convenient. Writing in boot2 as well keeps the check honest about which
# boot's write boot2 read: a save that only ever worked once would still pass
# a two-boot run that wrote only in boot1.
SAVED=$(MARK="$MARK_PAGES" python3 - <<'PY' 2>&1 || true
import os, sys
sys.path.insert(0, "/opt/otp-unit")
from dataclasses import replace
from otpunit import config
saved = replace(config.load(), pages=int(os.environ["MARK"]))
print(f"save={config.save(saved)} path={config.CONFIG_PATH} pages={saved.pages}")
PY
)
SAVED=${SAVED//$'\n'/ }
check setting-saved-to-the-boot-partition \
      "$(if [ "${SAVED#*save=True}" != "$SAVED" ]; then echo yes; else echo no; fi)" \
      "$SAVED"

sync 2>/dev/null || true

echo "--- otp-unit journal ($PHASE) ---"
journalctl -u otp-unit.service --no-pager -n 30 2>/dev/null || true
echo "--- end journal ---"

# DONE before RESULT, and both before the host is allowed to believe either.
# Results with no DONE line is a guest that died part way through a phase,
# and its counts agree with themselves right up to the axe.
printf 'OTP-GUEST-DONE %s\n' "$PHASE"
printf 'OTP-RESULT %s %s/%s\n' "$PHASE" "$PASS" "$TOTAL"

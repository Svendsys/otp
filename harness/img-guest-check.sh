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
# The script refuses to do anything at all without that token; see the phase
# guard below for why the unit's condition is not enough on its own.
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
# the same missing file. So boot2 states two presences with the same fixture
# first, and the discard check is reported last, after both. The sentinel is
# rewritten and read back, which proves the path is writable and that this
# check would have found the file had it survived; and the setting boot1
# wrote is read back through config.load(), which proves boot1 ran and got
# far enough to write things down.
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
# host decide; stopping at the first would hide the rest behind it. Refusing
# to run at all is a different thing and does exit non-zero -- see below.
set -uo pipefail

BOOTDIR=/boot/firmware

# THE PHASE, and every write in this file hangs off it.
#
# `set -f` around the loop because the substitution is deliberately word
# split and must NOT also be pathname expanded. This runs as root from a
# systemd unit, whose working directory is /, so with globbing left on a
# cmdline token of `*` is replaced by the listing of the root directory
# before the case below ever sees it -- and a file in / whose name happens
# to start otp.imgcheck= would then choose the phase.
PHASE=""
set -f
for token in $(tr -d '\n' < /proc/cmdline 2>/dev/null); do
    case "$token" in
        otp.imgcheck=*) PHASE="${token#otp.imgcheck=}" ;;
    esac
done
set +f

# REFUSE anything that is not one of the two phases the harness supplies,
# and refuse it here, before a single check has run.
#
# This script WRITES. It puts a sentinel in / and it puts a marker page
# count through config.save() into $BOOTDIR/otp-unit.conf -- which is
# OUTSIDE the overlay, on the boot partition, and therefore permanent on a
# real unit. install.sh ships it 0755 in /opt/otp-unit and enables its unit
# on every appliance, so until this guard existed the only thing between an
# operator's settings and that write was the ConditionKernelCommandLine=
# line in otp-unit-imgcheck.service: one line, in one file, that a drop-in,
# a hand-run `systemctl start`, or anyone resolving the ExecStart path
# steps straight past.
#
# Measured rather than argued. Given a realistic device command line with
# no otp.imgcheck token, the previous version set PHASE=unknown and ran
# every check regardless: the unit's page count went 500 -> 137 on the boot
# partition and a sentinel was left in /. The unit condition stays where it
# is -- this is a second lock on the same door, not a replacement.
case "$PHASE" in
    boot1|boot2) ;;
    *)
        echo "OTP-GUEST refusing to run: the kernel command line carries no" >&2
        echo "  otp.imgcheck=boot1 or otp.imgcheck=boot2 token (phase read:" >&2
        echo "  '${PHASE:-none}'). This probe writes a sentinel to / and a" >&2
        echo "  marker page count into $BOOTDIR/otp-unit.conf, which is" >&2
        echo "  outside the read-only overlay and would overwrite the" >&2
        echo "  operator's settings for good. It runs only inside the" >&2
        echo "  emulated boot harness/img-boot.sh sets up." >&2
        exit 1
        ;;
esac

PASS=0
TOTAL=0
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

    # LAST, and that ordering is the point. This is the only check in the
    # run whose evidence is a file NOT being there, and an absence is
    # equally satisfied by a boot1 that never ran, by a sentinel written
    # somewhere else, and by a rig that cannot write to / at all. The two
    # checks above it rule all three out with the same fixture: the setting
    # boot1 persisted is readable, and a sentinel written this boot is
    # readable back from the path this check looks at. The value read here
    # was taken before anything in this script wrote anything.
    check root-writes-discarded-by-the-power-cycle \
          "$(if [ "$SENTINEL_SURVIVED" = "no" ]; then echo yes; else echo no; fi)" \
          "$SENTINEL before this boot: ${SENTINEL_WAS:-absent}"
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

# --- the seeded userconf.txt path -----------------------------------------

# WHY THIS IS HERE. Issue #18 replaced a mask on userconfig.service with a
# ConditionPathExists drop-in so that the documented headless credential file
# keeps working, and until issue #20 only the branch where nobody ever wrote
# one had been exercised: no seed, no wizard, boot green. The harness plants a
# real seed in the boot partition before boot1; what follows is the machine's
# side of it.
#
# The account the seed names and the salt its hash carries. The hash itself
# stays in harness/img-boot.sh, which is not installed on a unit; only this
# marker travels here, and a salt is a comparison string rather than a
# credential. tests/test_img_verdict.py holds the two files against each other
# and against image/build.sh's FIRST_USER_NAME.
USERCONF_USER=otp
# shellcheck disable=SC2016  # the $ signs are crypt(3) field separators
USERCONF_SALT='$6$otpimgcheck$'
USERCONF_SERVICE=/usr/lib/userconf-pi/userconf-service

# POLLED, and briefly. This probe is ordered after otp-unit.service and
# cups.service and the poll above has already spent up to 90 seconds, so
# userconfig.service -- a oneshot wanted by multi-user.target -- has had every
# chance to be dealt with. It is polled anyway because "systemd has not looked
# at it yet" and "systemd looked and skipped it" are the same two empty fields
# from here, and reading the first as the second passes on exactly the boot run
# 12 died on.
UC_COND_TS=""
for _ in $(seq 1 15); do
    UC_COND_TS=$(systemctl show userconfig.service -p ConditionTimestamp --value 2>/dev/null)
    [ -n "$UC_COND_TS" ] && break
    sleep 2
done
UC_COND=$(systemctl show userconfig.service -p ConditionResult --value 2>/dev/null)
UC_RESULT=$(systemctl show userconfig.service -p Result --value 2>/dev/null)
UC_STDIN=$(systemctl show userconfig.service -p StandardInput --value 2>/dev/null)
UC_ACTIVE=$(systemctl is-active userconfig.service 2>&1 || true)
UC_ENABLED=$(systemctl is-enabled userconfig.service 2>&1 || true)
# A JOB, because a job is what actually held run 12's boot open: the wizard sat
# on a whiptail nobody could see and multi-user.target waited on the job. That
# state is not `is-active`'s business -- a unit whose ExecStart is blocked is
# "activating", and so is a healthy unit that started two seconds ago.
UC_JOBS=$(systemctl list-jobs --no-legend 2>/dev/null | grep -c "userconfig\.service" || true)

if [ "$PHASE" = "boot1" ]; then
    # APPLIED, not merely deleted. From outside, the harness can watch the
    # file vanish from the FAT partition; only the machine can say the
    # credentials in it were put anywhere, and a userconf-service that
    # deleted the seed without applying it would look identical from there.
    # pi-gen hashes FIRST_USER_PASS with a random salt, so this marker in the
    # shadow entry can only have come from the seed. The entry itself is
    # never printed: this console is uploaded as a CI artifact.
    UC_SHADOW=$(awk -F: -v u="$USERCONF_USER" '$1 == u {print $2}' /etc/shadow 2>/dev/null)
    UC_APPLIED=no
    if [ -n "$UC_SHADOW" ] && [ "${UC_SHADOW#"$USERCONF_SALT"}" != "$UC_SHADOW" ]; then
        UC_APPLIED=yes
    fi
    check userconf-seed-applied "$UC_APPLIED" \
          "${USERCONF_USER}'s shadow entry (${#UC_SHADOW} chars) begins $USERCONF_SALT: $UC_APPLIED"

    # NO WIZARD, which is the clause issue #20 is written around. A seeded
    # boot must take the non-interactive path: the condition passed (the
    # drop-in let it run), the oneshot finished successfully, and it has left
    # no job of its own in the queue holding multi-user.target open.
    check userconf-seeded-boot-ran-no-wizard \
          "$(if [ "$UC_COND" = yes ] && [ "$UC_RESULT" = success ] \
                && [ "$UC_ACTIVE" = inactive ] && [ "${UC_JOBS:-1}" = 0 ]; \
             then echo yes; else echo no; fi)" \
          "condition=${UC_COND:-?} result=${UC_RESULT:-?} is-active=$UC_ACTIVE jobs=${UC_JOBS:-?}"
fi

if [ "$PHASE" = "boot2" ]; then
    # THE UNSEEDED BRANCH, which is every boot a unit does after its first.
    # The delete boot1's wizard made stuck, because the FAT partition is
    # outside the overlay, so the condition is false here and the unit is
    # skipped -- instantly, with no prompt.
    #
    # `enabled` is asserted in the same breath, and it is the half that is
    # easy to lose. Masking or disabling the unit would also make it quiet,
    # and it is the ONLY consumer of userconf.txt: a quiet-by-masking image
    # ignores an operator's credentials in silence and leaves the tty2
    # recovery getty with no knowable login. That is the trade
    # device/install.sh's comment refuses, stated as a check.
    check userconf-unseeded-boot-skips-the-wizard \
          "$(if [ "$UC_COND" = no ] && [ -n "$UC_COND_TS" ] \
                && [ "$UC_ACTIVE" = inactive ] && [ "$UC_ENABLED" = enabled ]; \
             then echo yes; else echo no; fi)" \
          "condition=${UC_COND:-?} checked-at='${UC_COND_TS:-never}' is-active=$UC_ACTIVE is-enabled=$UC_ENABLED"

    # StandardInput=null, read back off the running machine rather than out
    # of the file install.sh wrote. It is the whole of the fail-fast: with a
    # tty a malformed seed becomes a whiptail nobody can answer, which is how
    # run 12's boot was held open. This says the drop-in reached the image,
    # parsed, and is in effect on the unit.
    check userconf-wizard-cannot-prompt \
          "$(if [ "$UC_STDIN" = null ]; then echo yes; else echo no; fi)" \
          "StandardInput=${UC_STDIN:-unset}"

    # THE MALFORMED SEED, run by hand and not left on the card for the unit
    # to find at boot. Stock userconfig.service carries Restart=on-failure,
    # so a boot that met one prints "Failed with result" and "Scheduled
    # restart job" -- two of the strings harness/img-boot.sh fails a release
    # on -- and relaxing a release gate to accommodate a fault the harness
    # injected itself is the wrong way round. The conditions the unit would
    # supply are supplied here instead: stdin closed, and no TERM, which is
    # what a unit with no tty is given. The drop-in's own half is the check
    # directly above.
    #
    # WHAT IS GATED is what matters on a device: the thing TERMINATED, and it
    # left the operator the evidence under the failed_ name. The exit status
    # is reported and NOT gated -- the failure mode that hurts is a script
    # that never returns, and whether the return is 0 or 1 is upstream's
    # business.
    UC_BAD='NOT A VALID NAME:'
    UC_SAID="the probe never ran it: $USERCONF_SERVICE is not executable here"
    UC_RC=none
    if [ -x "$USERCONF_SERVICE" ]; then
        printf '%s\n' "$UC_BAD" > "$BOOTDIR/userconf.txt"
        UC_SAID=$(timeout -k 5 60 env -u TERM "$USERCONF_SERVICE" < /dev/null 2>&1)
        UC_RC=$?
        UC_SAID=${UC_SAID//[$'\n\r']/ }
    fi
    check userconf-malformed-seed-fails-fast \
          "$(if [ "$UC_RC" != none ] && [ "$UC_RC" != 124 ] \
                && [ -e "$BOOTDIR/failed_userconf.txt" ] \
                && [ ! -e "$BOOTDIR/userconf.txt" ]; then echo yes; else echo no; fi)" \
          "rc=$UC_RC (124 = still running at the 60s bound) quarantined=$(yesno test -e "$BOOTDIR/failed_userconf.txt") leftover-seed=$(yesno test -e "$BOOTDIR/userconf.txt") said: ${UC_SAID:0:160}"
fi

sync 2>/dev/null || true

echo "--- otp-unit journal ($PHASE) ---"
journalctl -u otp-unit.service --no-pager -n 30 2>/dev/null || true
echo "--- end journal ---"

# DONE before RESULT, and both before the host is allowed to believe either.
# Results with no DONE line is a guest that died part way through a phase,
# and its counts agree with themselves right up to the axe.
printf 'OTP-GUEST-DONE %s\n' "$PHASE"
printf 'OTP-RESULT %s %s/%s\n' "$PHASE" "$PASS" "$TOTAL"

#!/usr/bin/env bash
#
# Runs inside the tier-3 image boot, as root, once per boot.
#
#   img-guest-check.sh
#
# Started by otp-unit-imgcheck.service, which exists only while the kernel
# command line carries otp.imgcheck=<phase>; only harness/img-boot.sh puts
# it there. The phase is read from /proc/cmdline so the unit file needs no
# per-boot edit, and the script refuses to run without it.
#
# Two boots of the artifact that is flashed, overlay engaged (issue #9):
#
#   boot1  writes a sentinel to / and a setting to /boot/firmware through
#          config.save().
#   boot2  the same card, powered off and on. The sentinel must be gone
#          (the overlay discards) and the setting still there (the boot
#          partition is outside it).
#
# An absence proves nothing alone, so boot2 first states two presences with
# the same fixtures and reports the discard check last.
#
# Prints one line per check in the format the host greps for:
#
#   OTP-CHECK <phase> <name> PASS|FAIL <detail>
#   OTP-GUEST-DONE <phase>
#   OTP-RESULT <phase> <passed>/<total>
#
# The phase is in every marker because the host requires each expected
# phase to have reported exactly once. Never exits non-zero on a failed
# check; refusing to run at all does.
set -uo pipefail

BOOTDIR=/boot/firmware
# A variable, like $BOOTDIR, because tests/test_overlay_root.py slices
# these checks out and runs them against a tree it builds.
ETC_MACHINE_ID=/etc/machine-id

# The phase. `set -f` because the substitution is word split and must not
# also be pathname expanded: this runs with cwd /, where a cmdline token of
# `*` would expand to the root listing.
PHASE=""
set -f
for token in $(tr -d '\n' < /proc/cmdline 2>/dev/null); do
    case "$token" in
        otp.imgcheck=*) PHASE="${token#otp.imgcheck=}" ;;
    esac
done
set +f

# Refuse anything but the two phases the harness supplies, before any
# check runs. This script writes a sentinel to / and a marker page count
# into $BOOTDIR/otp-unit.conf, outside the overlay and so permanent on a
# real unit. The unit's ConditionKernelCommandLine= is bypassed by a
# drop-in or a hand-run start; this is the second lock.
case "$PHASE" in
    boot1|boot2) ;;
    *)
        echo "OTP-GUEST refusing to run: the kernel command line carries no" >&2
        echo "  otp.imgcheck=boot1 or otp.imgcheck=boot2 token (phase read:" >&2
        echo "  '${PHASE:-none}'). This probe writes a sentinel to /, a" >&2
        echo "  marker page count into $BOOTDIR/otp-unit.conf and this" >&2
        echo "  machine's id into $BOOTDIR/otp-imgcheck-machine-id --" >&2
        echo "  the last two outside the read-only overlay, where the first" >&2
        echo "  would overwrite the operator's settings for good. It runs" >&2
        echo "  only inside the emulated boot harness/img-boot.sh sets up." >&2
        exit 1
        ;;
esac

PASS=0
TOTAL=0
# Written to / in every phase and looked for at the start of the next.
# Not /tmp or /run: those are tmpfs whatever the root is.
SENTINEL=/otp-imgcheck-sentinel
# A page count no default and no panel choice produces, so its presence in
# boot2 can only be boot1's write.
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
# Echoed so "the flag arrived and did nothing" can be told from "the flag
# never arrived".
echo "OTP-CMDLINE $PHASE $(tr -d '\n' < /proc/cmdline)"

# --- was the sentinel from the previous boot discarded? -------------------

# First, before anything else writes to /.
SENTINEL_SURVIVED=no
SENTINEL_WAS=""
if [ -e "$SENTINEL" ]; then
    SENTINEL_SURVIVED=yes
    SENTINEL_WAS=$(head -c 120 "$SENTINEL" 2>/dev/null | tr -d '\n')
fi

# --- the overlay itself ---------------------------------------------------

ROOT_FSTYPE=$(findmnt -no FSTYPE / 2>/dev/null | head -1)
ROOT_SOURCE=$(findmnt -no SOURCE / 2>/dev/null | head -1)
# The super options name the three layers. From mountinfo, because
# findmnt's OPTIONS reports per-mount flags that read the same for an
# overlay and a plain ext4.
ROOT_SUPER=$(awk '$5 == "/" {for (i = 7; i <= NF; i++) if ($i == "-") {print $(i + 3); exit}}' \
             /proc/self/mountinfo 2>/dev/null | head -1)
check root-is-overlay \
      "$(if [ "$ROOT_FSTYPE" = "overlay" ]; then echo yes; else echo no; fi)" \
      "fstype=${ROOT_FSTYPE:-?} source=${ROOT_SOURCE:-?} super=${ROOT_SUPER:-?}"

# The positive control for the sentinel checks, in both phases: writing
# and reading back rules out a root that cannot be written, a wrong path
# and a boot that never got this far.
WROTE=no
if printf 'phase=%s stamp=%s\n' "$PHASE" "$(date -u +%s 2>/dev/null)" \
        > "$SENTINEL" 2>/dev/null && [ -s "$SENTINEL" ]; then
    WROTE=yes
fi
check root-write-lands-and-is-readable "$WROTE" "$SENTINEL"

# --- the unit, on a read-only root ----------------------------------------

# Polled: Type=simple means "started" the instant the process is spawned.
# Bounded, because a unit that never comes up must still produce a report.
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

# Read through the unit's own mount namespace, since that is the view its
# refusal is decided on. A unit that refuses live pads on the shipped image
# is an appliance that prints nothing.
UNIT_PID=$(systemctl show otp-unit.service -p MainPID --value 2>/dev/null || echo 0)
POSTURE=$(python3 - "$UNIT_PID" <<'PY' 2>&1
import sys
sys.path.insert(0, "/opt/otp-unit")
from otpunit import posture
print(",".join(posture.problems(mounts=f"/proc/{sys.argv[1]}/mounts")) or "none")
PY
)
check unit-will-make-live-pads \
      "$(if [ "$POSTURE" = "none" ]; then echo yes; else echo no; fi)" \
      "problems=$POSTURE pid=$UNIT_PID swaps=$(tail -n +2 /proc/swaps | wc -l)"

# --- the journal, now that the console carries it -------------------------

# The kernel command line carries systemd.journald.forward_to_console=1
# (issue #21). To prove it works, one marker goes into the journal through
# systemd-cat and by no other route: never through this script's stdout or
# a check's detail, which reach the console without forwarding. The host
# looks for it on the console; this check is the positive control that the
# journal accepted it.

# -p notice clears journald's default MaxLevelConsole of info.
JOURNAL_TAG=otp-imgcheck
JOURNAL_MARK="OTP-JOURNAL-FORWARDED $PHASE"
printf '%s\n' "$JOURNAL_MARK" | systemd-cat -t "$JOURNAL_TAG" -p notice 2>/dev/null || true
# Polled: journald indexes the entry on its own schedule.
MARKED=0
for _ in $(seq 1 15); do
    MARKED=$(journalctl -b -t "$JOURNAL_TAG" --no-pager 2>/dev/null \
             | grep -cF "$JOURNAL_MARK" || true)
    [ "${MARKED:-0}" != 0 ] && break
    sleep 1
done
check journal-marker-accepted \
      "$(if [ "${MARKED:-0}" != 0 ]; then echo yes; else echo no; fi)" \
      "journalctl -b -t $JOURNAL_TAG holds ${MARKED:-0} line(s) for this phase"

# --- the panel this machine does not have ---------------------------------

# The headless route. QEMU's raspi3b has no OLED and no I2C device node,
# which is the case otpunit/diagnostics.py exists for (issue #21). Read
# from the unit's journal for this boot (-b), not the console.

# Three clauses, each a different claim:
#   "no OLED ("                     hmi.open_display's probe raised: the
#                                   unit looked.
#   "interface -- display: none,"   Interface.describe: nothing to draw on.
#                                   Without it a unit with an HDMI console
#                                   and no buttons logs the other two.
#   "no usable interface; printing  __main__ chose the headless route.
#    unattended"

# tests/test_img_verdict.py holds each string against the module that
# prints it. The input half is not read: on the emulator claiming GPIO5
# fails EINVAL, on a real Pi it says "GPIO buttons" wired or not.

# Polled: Type=simple reports active before the interpreter has imported
# anything, and under TCG that import takes seconds.
NO_OLED=no
NO_DISPLAY=no
HEADLESS=no
UNIT_LOG=""
for _ in $(seq 1 30); do
    UNIT_LOG=$(journalctl -b -u otp-unit.service --no-pager 2>/dev/null | tr '\n' ' ')
    case "$UNIT_LOG" in *"no OLED ("*) NO_OLED=yes ;; esac
    case "$UNIT_LOG" in *"interface -- display: none,"*) NO_DISPLAY=yes ;; esac
    case "$UNIT_LOG" in *"no usable interface; printing unattended"*) HEADLESS=yes ;; esac
    [ "$NO_OLED" = yes ] && [ "$NO_DISPLAY" = yes ] && [ "$HEADLESS" = yes ] && break
    sleep 2
done
# What the unit decided it had, quoted back in the detail.
UNIT_IFACE=$(printf '%s' "$UNIT_LOG" | grep -m1 -oE 'interface -- .{0,60}' || true)
check unit-detects-no-panel \
      "$(if [ "$NO_OLED" = yes ] && [ "$NO_DISPLAY" = yes ] && [ "$HEADLESS" = yes ]; then echo yes; else echo no; fi)" \
      "no-oled=$NO_OLED no-display=$NO_DISPLAY headless=$HEADLESS ${UNIT_IFACE:-no 'interface --' line in the unit journal}"

# --- CUPS, which is the half of the design the overlay could break --------

# /etc/cups must be writable for cupsd to start; on a read-only root that
# is a tmpfs repopulated from /opt/otp-unit/cups-etc.
ETC_CUPS_FS=$(findmnt -no FSTYPE /etc/cups 2>/dev/null | head -1)
check etc-cups-is-its-own-tmpfs \
      "$(if [ "$ETC_CUPS_FS" = "tmpfs" ]; then echo yes; else echo no; fi)" \
      "fstype=${ETC_CUPS_FS:-not a mount point}"

check cups-running "$(yesno systemctl is-active cups.service)" \
      "is-active=$(systemctl is-active cups.service 2>&1 || true)"

# install.sh gates on this in a chroot; this asks the daemon's parser on
# the booted machine, after the tmpfs was repopulated.
check cupsd-config-valid \
      "$(yesno cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf)" \
      ""

# --- settings, which must outlive the power-cycle -------------------------

# The boot partition has to be a separate mount, or "the setting survived"
# is a statement about the overlay's tmpfs.
BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
BOOT_FS=$(findmnt -no FSTYPE --target "$BOOTDIR" 2>/dev/null || echo "?")
check boot-partition-separate \
      "$(if [ "$BOOT_SRC" != "?" ] && [ "$BOOT_SRC" != "$ROOT_SOURCE" ]; then echo yes; else echo no; fi)" \
      "$BOOTDIR src=$BOOT_SRC fstype=$BOOT_FS"

# --- the identity, which is the one thing allowed to survive --------------

# /etc is inside the overlay, so without persistence /etc/machine-id
# reverts to `uninitialized` on every power cycle and systemd treats each
# boot as a first boot. device/install.sh restores it from the initramfs
# and otp-unit-identity.service records it on the boot partition.
IDENTITY_STORE="$BOOTDIR/otp-identity"
LIVE_MACHINE_ID=$(tr -d '\n\r' < "$ETC_MACHINE_ID" 2>/dev/null)
KEPT_MACHINE_ID=$(tr -d '\n\r' < "$IDENTITY_STORE/machine-id" 2>/dev/null)
# findmnt --target answers for the containing mount, so a store that is a
# plain directory on the overlay (an unmounted /boot/firmware) reports the
# root's source and fails the check below.
STORE_SRC=$(findmnt -no SOURCE --target "$IDENTITY_STORE" 2>/dev/null || echo "?")
MACHINE_ID_KEPT=no
if [ -n "$LIVE_MACHINE_ID" ] && [ "$LIVE_MACHINE_ID" = "$KEPT_MACHINE_ID" ]; then
    MACHINE_ID_KEPT=yes
fi
# $ROOT_SOURCE must be non-empty, or "not on the root's filesystem" is true
# of every store. The detail carries eight characters of each id: this
# console is uploaded as a CI artifact.

# This compares the live id with the store, which
# otp-unit-identity.service may have filled this very boot; the
# probe-owned record below is what tells "restored" from "recorded fresh".
check machine-id-persisted-outside-the-overlay \
      "$(if [ "$MACHINE_ID_KEPT" = yes ] && [ "$STORE_SRC" != "?" ] \
            && [ -n "$ROOT_SOURCE" ] \
            && [ "$STORE_SRC" != "$ROOT_SOURCE" ]; then echo yes; else echo no; fi)" \
      "$ETC_MACHINE_ID=${LIVE_MACHINE_ID:0:8}... stored=${KEPT_MACHINE_ID:0:8}... match=$MACHINE_ID_KEPT store=$IDENTITY_STORE src=$STORE_SRC root-src=${ROOT_SOURCE:-?}"

# A second file, written by this probe once in boot1 and compared in boot2,
# so the value boot2 is held against predates boot2's own identity.
MACHINE_ID_RECORD="$BOOTDIR/otp-imgcheck-machine-id"

# Eight characters or the word `nothing`, so an absent value cannot read as
# a present one.
short_id() {
    if [ -n "$1" ]; then printf '%s...' "${1:0:8}"; else printf 'nothing'; fi
}

if [ "$PHASE" = "boot1" ]; then
    # THE POSITIVE CONTROL, and it is the reason boot2's check can mean
    # anything: there was an id, and it reads back off the boot partition.
    printf '%s\n' "$LIVE_MACHINE_ID" > "$MACHINE_ID_RECORD" 2>/dev/null || true
    MACHINE_ID_READBACK=$(tr -d '\n\r' < "$MACHINE_ID_RECORD" 2>/dev/null)
    check machine-id-recorded-for-the-next-boot \
          "$(if [ -n "$LIVE_MACHINE_ID" ] \
                && [ "$MACHINE_ID_READBACK" = "$LIVE_MACHINE_ID" ]; \
             then echo yes; else echo no; fi)" \
          "this boot: $(short_id "$LIVE_MACHINE_ID") | read back from $MACHINE_ID_RECORD: $(short_id "$MACHINE_ID_READBACK")"
fi

if [ "$PHASE" = "boot2" ]; then
    # The second boot is the same machine as the first. Both sides must be
    # non-empty: two absences are equal.
    MACHINE_ID_BOOT1=$(tr -d '\n\r' < "$MACHINE_ID_RECORD" 2>/dev/null)
    check machine-id-identical-across-the-power-cycle \
          "$(if [ -n "$LIVE_MACHINE_ID" ] && [ -n "$MACHINE_ID_BOOT1" ] \
                && [ "$LIVE_MACHINE_ID" = "$MACHINE_ID_BOOT1" ]; \
             then echo yes; else echo no; fi)" \
          "boot1: $(short_id "$MACHINE_ID_BOOT1") | boot2: $(short_id "$LIVE_MACHINE_ID")"
fi

if [ "$PHASE" = "boot2" ]; then
    # Before the write below, and through config.load(), not grep: load()
    # silently defaults any field it cannot parse.
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

    # Last: the only check whose evidence is an absence. The two above rule
    # out a boot1 that never ran and an unwritable or wrong path; the value
    # was read before this script wrote anything.
    check root-writes-discarded-by-the-power-cycle \
          "$(if [ "$SENTINEL_SURVIVED" = "no" ]; then echo yes; else echo no; fi)" \
          "$SENTINEL before this boot: ${SENTINEL_WAS:-absent}"
fi

# Through config.save() at its real CONFIG_PATH, in both phases: it is the
# only writer on the device that must work with the root read-only.
# Writing in boot2 too catches a save that only works once.
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

# --- the provisioning agent this image no longer carries ------------------

# Two checks. image/stage-otpunit/01-otpunit/00-run.sh purges cloud-init
# and rpi-cloud-init-mods, whose datasource seeds from /boot/firmware
# (issue #34); device/install.sh also writes cloud-init's kill switch.
# After a purge nearly all cloud-init evidence is absent whether or not
# the kill switch exists, so the absences are one check and the marker
# file another.

# Neither runs cloud-init's code. That ds-identify honours the file is
# upstream's property; these check the packages are gone and the file has
# the shape ds-identify requires.

# Every absence has a positive control: each mechanism (dpkg-query,
# systemctl, the generator directory) must also answer one question in
# the affirmative.
CI_PACKAGES="cloud-init rpi-cloud-init-mods"
# cloud-init.target is the one the generator links; the other two run out
# of it.
CI_UNITS="cloud-init.target cloud-init-local.service cloud-config.service"
CI_GENERATOR_DIR=/usr/lib/systemd/system-generators
# The one file rpi-cloud-init-mods ships that is not cloud-init's own.
# Reported, not gated.
CI_NETPLAN_RENDERER=/usr/lib/netplan/00-network-manager-all.yaml
CI_DISABLED=/etc/cloud/cloud-init.disabled

CI_INSTALLED=""
for pkg in $CI_PACKAGES; do
    # shellcheck disable=SC2016  # a dpkg-query field name, not a shell expansion
    CI_ST=$(dpkg-query -W -f '${db:Status-Status}' "$pkg" 2>/dev/null || true)
    # `config-files` is a remove, not a purge: the datasource config
    # pointing at the boot partition would still be there. Only gone passes.
    case "$CI_ST" in
        ""|not-installed) ;;
        *) CI_INSTALLED="$CI_INSTALLED $pkg=$CI_ST" ;;
    esac
done
# The control: a package that must come back `installed`.
CI_CONTROL_PKG=systemd
# shellcheck disable=SC2016  # a dpkg-query field name, not a shell expansion
CI_CONTROL_STATUS=$(dpkg-query -W -f '${db:Status-Status}' "$CI_CONTROL_PKG" 2>/dev/null || true)

CI_LOADED=""
for unit in $CI_UNITS; do
    CI_LS=$(systemctl show -p LoadState --value "$unit" 2>/dev/null || true)
    [ "$CI_LS" = not-found ] || CI_LOADED="$CI_LOADED $unit=${CI_LS:-?}"
done
# The control: a systemctl that stopped answering would report this unit
# not-found too.
CI_CONTROL_UNIT=otp-unit.service
CI_CONTROL_LOAD=$(systemctl show -p LoadState --value "$CI_CONTROL_UNIT" 2>/dev/null || true)

CI_GENERATOR="$CI_GENERATOR_DIR/cloud-init-generator"
# systemd's own generators live here, so an empty listing is a wrong path
# or an unreadable /usr, not a purge.
CI_GENERATOR_SIBLINGS=$(find "$CI_GENERATOR_DIR" -mindepth 1 -maxdepth 1 \
                        2>/dev/null | wc -l)

# The netplan-renderer and NetworkManager fields in the detail are a
# report, not evidence: on a purged unit they are constants. Removing the
# renderer default is harmless because netplan then has no configuration
# and writes no backend config, and install.sh masks
# systemd-networkd-wait-online.service regardless (checked below).
check cloud-init-is-not-installed \
      "$(if [ -z "$CI_INSTALLED" ] && [ "$CI_CONTROL_STATUS" = installed ] \
            && [ -z "$CI_LOADED" ] && [ "$CI_CONTROL_LOAD" = loaded ] \
            && [ ! -e "$CI_GENERATOR" ] \
            && [ "${CI_GENERATOR_SIBLINGS:-0}" -gt 0 ] 2>/dev/null; \
         then echo yes; else echo no; fi)" \
      "dpkg:${CI_INSTALLED:- none} (control $CI_CONTROL_PKG=${CI_CONTROL_STATUS:-?}) | units:${CI_LOADED:- none} (control $CI_CONTROL_UNIT=${CI_CONTROL_LOAD:-?}) | generator=$(yesno test -e "$CI_GENERATOR") siblings=${CI_GENERATOR_SIBLINGS:-?} | netplan-renderer=$(yesno test -e "$CI_NETPLAN_RENDERER") NetworkManager=$(systemctl is-active NetworkManager.service 2>&1 || true)"

# The kill switch, read back off the machine. `-f`, not `-e`: ds-identify
# tests `[ -f /etc/cloud/cloud-init.disabled ]`, so a directory or dangling
# symlink of that name is no kill switch. In boot2 the overlay has
# discarded boot1's writes, so a marker still there shipped in the image.
CI_DISABLED_IS_FILE=$(yesno test -f "$CI_DISABLED")
check cloud-init-kill-switch-survives-the-purge \
      "$CI_DISABLED_IS_FILE" \
      "$CI_DISABLED regular-file=$CI_DISABLED_IS_FILE exists=$(yesno test -e "$CI_DISABLED") dir=$(yesno test -d "${CI_DISABLED%/*}")"

if [ "$PHASE" = "boot1" ]; then
    # --- the diagnostic sheet, and how far the print path really gets -----

    # The unit's own headless path cannot fire here: run_headless() waits for
    # a printer and the emulator has none. This drives the rest by hand
    # through the shipped code and cupsd: can the image render the sheet, and
    # does cupsd accept it.

    # The queue is the probe's own name, pointing at a usb:// URI with nothing
    # behind it: the job being accepted is the claim. /etc/cups is a tmpfs, so
    # none of this reaches the card. `lpadmin -p NAME -E -v usb://...` with no
    # -m creates a queue `lp` accepts a job into.
    SHEET_QUEUE=otpimgcheck
    SHEET_URI="usb://OTP/imgcheck"
    SHEET_LPADMIN=$(/usr/sbin/lpadmin -p "$SHEET_QUEUE" -E -v "$SHEET_URI" 2>&1)
    SHEET_LPADMIN_RC=$?
    # Bounded: a probe killed by otp-unit-imgcheck.service's TimeoutStartSec
    # reports nothing at all. The exit code is reported, not gated; at or
    # above 124 it is timeout(1) saying the program never finished.
    SHEET_SAID=$(SHEET_QUEUE="$SHEET_QUEUE" timeout -k 5 90 python3 - <<'PY' 2>&1
import os, sys
sys.path.insert(0, "/opt/otp-unit")

queue = os.environ["SHEET_QUEUE"]
try:
    from otpunit import config, diagnostics
    settings = config.load()
    sheet = diagnostics.render_bytes(
        diagnostics.collect(settings=settings, printer=None, queue=queue,
                            driver="none: the harness supplied this queue"),
        diagnostics._page_size(settings))
    print("RENDER ok bytes=%d magic=%s"
          % (len(sheet), bytes(sheet[:5]).decode("ascii", "replace")))
except Exception as exc:                         # noqa: BLE001
    print("RENDER failed %s: %s" % (type(exc).__name__, exc))
    raise SystemExit(0)

# A separate try, so "cannot draw the sheet" and "cupsd would not take it"
# are reported apart.
try:
    from otpunit.printer import Cups
    print("SUBMIT ok job=%s" % (Cups().submit(bytes(sheet), name=queue,
                                              title="OTP status") or "<no id>"))
except Exception as exc:                         # noqa: BLE001
    print("SUBMIT failed %s: %s" % (type(exc).__name__, exc))
PY
)
    SHEET_RC=$?
    SHEET_SAID=${SHEET_SAID//[$'\n\r']/ }
    # Rendered means a PDF, not merely no exception.
    SHEET_RENDERED=no
    case "$SHEET_SAID" in *"RENDER ok bytes="*"magic=%PDF"*) SHEET_RENDERED=yes ;; esac
    check diagnostic-sheet-renders "$SHEET_RENDERED" \
          "rc=$SHEET_RC (>=124 = no clean status at the 90s bound) $SHEET_SAID"
    # Enqueued means cupsd gave it an id: Cups.submit() returns "" when lp's
    # "request id is <id>" is absent.
    SHEET_ENQUEUED=no
    case "$SHEET_SAID" in
        *"SUBMIT ok job=$SHEET_QUEUE-"*) SHEET_ENQUEUED=yes ;;
    esac
    check diagnostic-sheet-reaches-cups "$SHEET_ENQUEUED" \
          "lpadmin rc=$SHEET_LPADMIN_RC ${SHEET_LPADMIN:-(silent)} | queue=$(/usr/bin/lpstat -v "$SHEET_QUEUE" 2>&1 | head -1) | $SHEET_SAID"
    # Removed whatever happened, so boot2 reads a machine that looks like a
    # unit.
    /usr/sbin/lpadmin -x "$SHEET_QUEUE" 2>/dev/null || true
fi

# --- the seeded userconf.txt path -----------------------------------------

# The harness plants a real userconf.txt seed in the boot partition before
# boot1 (issues #18, #20); this is the machine's side of it.

# The account the seed names and the salt its hash carries. The hash stays
# in harness/img-boot.sh; a salt is a comparison string, not a credential.
# tests/test_img_verdict.py holds the two files against each other and
# against image/build.sh's FIRST_USER_NAME.
USERCONF_USER=otp
# shellcheck disable=SC2016  # the $ signs are crypt(3) field separators
USERCONF_SALT='$6$otpimgcheck$'
USERCONF_SERVICE=/usr/lib/userconf-pi/userconf-service

# --- and where that credential is kept, now that it is kept ---------------

# `chpasswd -e` writes /etc/shadow, inside the overlay, and
# userconf-service deletes the seed. device/persist-identity.sh records
# the applied hash here and restores it at sysinit.
CRED_STORE="$IDENTITY_STORE/credential"

# A probe-owned record, as $MACHINE_ID_RECORD is: written once in boot1, so
# boot2 is not comparing against a store it could have refilled itself.
CRED_RECORD="$BOOTDIR/otp-imgcheck-credential"

# The hash is never printed or copied: this console is a CI artifact, so
# only a sha256 of it travels. Every check requires 64 characters as well
# as a match, because two empty digests compare equal.
cred_digest() {
    [ -n "${1:-}" ] || return 0
    printf '%s' "$1" | sha256sum 2>/dev/null | cut -d' ' -f1
}

cred_short() {
    if [ "${#1}" = 64 ]; then printf '%s...' "${1:0:12}"; else printf 'nothing'; fi
}

# Polled until systemd has dealt with the unit: nothing orders this probe
# after userconfig.service. Settled means no queued job, plus either a
# condition timestamp or systemd's own "Finished" line in the journal. The
# journal is needed because a seeded apply ends in cancel-rename, which
# disables the unit and reloads, after which systemd garbage-collects it
# and every property reads as a default.

# -b restricts the read to this boot, so correctness does not depend on
# install.sh's Storage=volatile. The needle carries the speaker,
# systemd[1], because `journalctl -u` also returns the unit's own stdout.
# It is quoted in the ${#} patterns below: unquoted, `[1]` is a glob class.
UC_DONE='systemd[1]: Finished userconfig.service'
UC_COND_TS=""
UC_JOBS=1
UC_LOG=""
for _ in $(seq 1 60); do
    UC_COND_TS=$(systemctl show userconfig.service -p ConditionTimestamp --value 2>/dev/null)
    UC_JOBS=$(systemctl list-jobs --no-legend 2>/dev/null | grep -c "userconfig\.service" || true)
    UC_LOG=$(journalctl -b -u userconfig.service --no-pager 2>/dev/null | tr '\n' ' ')
    if [ "${UC_JOBS:-1}" = 0 ] \
       && { [ -n "$UC_COND_TS" ] || [ "${UC_LOG#*"$UC_DONE"}" != "$UC_LOG" ]; }
    then break; fi
    sleep 2
done
UC_COND=$(systemctl show userconfig.service -p ConditionResult --value 2>/dev/null)
UC_RESULT=$(systemctl show userconfig.service -p Result --value 2>/dev/null)
UC_STDIN=$(systemctl show userconfig.service -p StandardInput --value 2>/dev/null)
UC_ACTIVE=$(systemctl is-active userconfig.service 2>&1 || true)
UC_ENABLED=$(systemctl is-enabled userconfig.service 2>&1 || true)

# --- what the wizard's job was waiting for --------------------------------

# Stock systemd-networkd-wait-online.service has TimeoutStartSec=infinity
# and never returns here, where NetworkManager owns the link; it would
# hold userconfig.service queued behind it. install.sh masks it. A mask
# that reached the image and a boot not waiting on it are separate claims,
# so both are read.
NETWAIT=systemd-networkd-wait-online.service
NETWAIT_ENABLED=$(systemctl is-enabled "$NETWAIT" 2>&1 || true)
NETWAIT_JOBS=$(systemctl list-jobs --no-legend 2>/dev/null | grep -cF "$NETWAIT" || true)
check network-wait-cannot-hold-the-boot-open \
      "$(if [ "$NETWAIT_ENABLED" = masked ] && [ "${NETWAIT_JOBS:-1}" = 0 ]; \
         then echo yes; else echo no; fi)" \
      "$NETWAIT is-enabled=$NETWAIT_ENABLED jobs=${NETWAIT_JOBS:-?}"

if [ "$PHASE" = "boot1" ]; then
    # Applied, not merely deleted: only the machine can say the seed's
    # credentials went anywhere. pi-gen hashes FIRST_USER_PASS with a random
    # salt, so this salt in the shadow entry can only come from the seed.
    # The entry itself is never printed.
    UC_SHADOW=$(awk -F: -v u="$USERCONF_USER" '$1 == u {print $2}' /etc/shadow 2>/dev/null)
    UC_APPLIED=no
    if [ -n "$UC_SHADOW" ] && [ "${UC_SHADOW#"$USERCONF_SALT"}" != "$UC_SHADOW" ]; then
        UC_APPLIED=yes
    fi
    check userconf-seed-applied "$UC_APPLIED" \
          "${USERCONF_USER}'s shadow entry (${#UC_SHADOW} chars) begins $USERCONF_SALT: $UC_APPLIED"

    # A seeded boot must take the non-interactive path (issue #20): the
    # oneshot finished, nothing skipped or failed it, and no job of its own
    # is left holding multi-user.target open. Read off the log, because the
    # unit object does not survive the apply (see the poll above);
    # ConditionResult is reported only.
    UC_TROUBLE=no
    case "$UC_LOG" in
        *"Failed with result"*|*"Failed to start"*|*"Scheduled restart job"*|\
        *"was skipped"*|*"skipped, unmet condition"*) UC_TROUBLE=yes ;;
    esac
    # The trouble strings are not speaker-scoped: failing on the unit's own
    # stdout is the safe direction.
    UC_FINISHED=no
    if [ "${UC_LOG#*"$UC_DONE"}" != "$UC_LOG" ]; then
        UC_FINISHED=yes
    fi
    check userconf-seeded-boot-ran-no-wizard \
          "$(if [ "$UC_FINISHED" = yes ] && [ "$UC_TROUBLE" = no ] \
                && [ "$UC_ACTIVE" = inactive ] && [ "${UC_JOBS:-1}" = 0 ]; \
             then echo yes; else echo no; fi)" \
          "journal-finished=$UC_FINISHED trouble=$UC_TROUBLE is-active=$UC_ACTIVE jobs=${UC_JOBS:-?} condition=${UC_COND:-?} result=${UC_RESULT:-?}"

    # The apply ends in cancel-rename, which starts a getty on tty1, the
    # tty otp-unit.service holds as its front panel. device/install.sh
    # conditions that getty off. `unit-active` above runs before the apply
    # and cannot stand in for this.
    PANEL=$(systemctl is-active otp-unit.service 2>&1 || true)
    TTY1=$(systemctl is-active getty@tty1.service 2>&1 || true)
    TTY1_JOBS=$(systemctl list-jobs --no-legend 2>/dev/null | grep -cF "getty@tty1.service" || true)
    check front-panel-survives-the-credential-apply \
          "$(if [ "$PANEL" = active ] && [ "$TTY1" != active ] \
                && [ "${TTY1_JOBS:-1}" = 0 ]; then echo yes; else echo no; fi)" \
          "otp-unit=$PANEL getty@tty1=$TTY1 getty-jobs=${TTY1_JOBS:-?}"

    # /etc/shadow is inside the overlay, so the hash must also be recorded
    # outside it. Four clauses:
    #   - $UC_APPLIED: the hash is the seeded one, not the build-time
    #     password.
    #   - the store names the seeded account; `chpasswd` takes `user:hash`.
    #   - the digests match and are 64 characters.
    #   - the store is not on the root's filesystem ($STORE_SRC, as for
    #     machine-id-persisted-outside-the-overlay).
    CRED_KEPT=$(tr -d '\n\r' < "$CRED_STORE" 2>/dev/null)
    CRED_KEPT_USER=${CRED_KEPT%%:*}
    CRED_KEPT_HASH=${CRED_KEPT#*:}
    # No colon at all means no hash, rather than the whole line being one.
    [ "$CRED_KEPT_USER" != "$CRED_KEPT" ] || CRED_KEPT_HASH=""
    CRED_LIVE_DIGEST=$(cred_digest "$UC_SHADOW")
    CRED_KEPT_DIGEST=$(cred_digest "$CRED_KEPT_HASH")
    check credential-recorded-outside-the-overlay \
          "$(if [ "$UC_APPLIED" = yes ] \
                && [ "$CRED_KEPT_USER" = "$USERCONF_USER" ] \
                && [ "${#CRED_LIVE_DIGEST}" = 64 ] \
                && [ "$CRED_KEPT_DIGEST" = "$CRED_LIVE_DIGEST" ] \
                && [ "$STORE_SRC" != "?" ] && [ -n "$ROOT_SOURCE" ] \
                && [ "$STORE_SRC" != "$ROOT_SOURCE" ]; \
             then echo yes; else echo no; fi)" \
          "$CRED_STORE user=${CRED_KEPT_USER:-none} kept=$(cred_short "$CRED_KEPT_DIGEST") live=$(cred_short "$CRED_LIVE_DIGEST") applied=$UC_APPLIED src=$STORE_SRC root-src=${ROOT_SOURCE:-?}"

    # The positive control for boot2: there was a credential, it was the
    # seeded one, and the record holds a digest that reads back.
    printf '%s\n' "$CRED_LIVE_DIGEST" > "$CRED_RECORD" 2>/dev/null || true
    CRED_READBACK=$(tr -d '\n\r' < "$CRED_RECORD" 2>/dev/null)
    check credential-recorded-for-the-next-boot \
          "$(if [ "$UC_APPLIED" = yes ] && [ "${#CRED_LIVE_DIGEST}" = 64 ] \
                && [ "$CRED_READBACK" = "$CRED_LIVE_DIGEST" ]; \
             then echo yes; else echo no; fi)" \
          "this boot: $(cred_short "$CRED_LIVE_DIGEST") | read back from $CRED_RECORD: $(cred_short "$CRED_READBACK")"
fi

if [ "$PHASE" = "boot2" ]; then
    # Every boot after the first is unseeded: boot1's wizard deleted the
    # seed from the FAT partition, so the condition is false and the unit
    # is skipped. `enabled` is asserted too, because masking would also be
    # quiet and the unit is the only consumer of userconf.txt. A true
    # condition here means the seed outlived the boot that consumed it.
    # The three clauses do not imply each other.
    check userconf-unseeded-boot-skips-the-wizard \
          "$(if [ "$UC_COND" = no ] && [ -n "$UC_COND_TS" ] \
                && [ "$UC_ACTIVE" = inactive ] && [ "$UC_ENABLED" = enabled ]; \
             then echo yes; else echo no; fi)" \
          "condition=${UC_COND:-?} checked-at='${UC_COND_TS:-never}' is-active=$UC_ACTIVE is-enabled=$UC_ENABLED"

    # StandardInput=null, read off the running machine: with a tty a
    # malformed seed becomes a whiptail nobody can answer.
    check userconf-wizard-cannot-prompt \
          "$(if [ "$UC_STDIN" = null ]; then echo yes; else echo no; fi)" \
          "StandardInput=${UC_STDIN:-unset}"

    # --- and the operator can still log in ---------------------------------
    #
    # Must run before the malformed-seed experiment below, which writes its
    # own userconf.txt. Four clauses:
    #   - the salt marker: the hash is the one boot1's seed applied.
    #   - the digest matches boot1's record: the restore did not mangle it.
    #   - both digests are 64 characters, so two absences cannot agree.
    #   - no seed is on the card, so this boot was not handed the
    #     credential.
    UC_SHADOW=$(awk -F: -v u="$USERCONF_USER" '$1 == u {print $2}' /etc/shadow 2>/dev/null)
    CRED_FROM_SEED=no
    if [ -n "$UC_SHADOW" ] && [ "${UC_SHADOW#"$USERCONF_SALT"}" != "$UC_SHADOW" ]; then
        CRED_FROM_SEED=yes
    fi
    CRED_LIVE_DIGEST=$(cred_digest "$UC_SHADOW")
    CRED_BOOT1=$(tr -d '\n\r' < "$CRED_RECORD" 2>/dev/null)
    check credential-survives-the-power-cycle \
          "$(if [ "$CRED_FROM_SEED" = yes ] \
                && [ "${#CRED_LIVE_DIGEST}" = 64 ] \
                && [ "${#CRED_BOOT1}" = 64 ] \
                && [ "$CRED_LIVE_DIGEST" = "$CRED_BOOT1" ] \
                && [ ! -e "$BOOTDIR/userconf.txt" ] \
                && [ ! -e "$BOOTDIR/userconf" ]; \
             then echo yes; else echo no; fi)" \
          "${USERCONF_USER}'s hash (${#UC_SHADOW} chars) begins $USERCONF_SALT: $CRED_FROM_SEED | boot1: $(cred_short "$CRED_BOOT1") | boot2: $(cred_short "$CRED_LIVE_DIGEST") | seed on the card: $(yesno test -e "$BOOTDIR/userconf.txt")"

    # --- and a store that names the account the seed RENAMED ---------------
    #
    # userconf-pi renames the UID-1000 account when a seed names another
    # user. The rename lives in the overlay and dies with the power; the
    # store on the FAT partition does not, so the next boot meets a store
    # naming an account that no longer exists. That state is synthesised
    # here by relabelling the store; the shipped script, chpasswd, shadow
    # file and store it is handed to are real.

    # The "before" password is this unit's own hash with its last character
    # changed, never printed. Only the name in the store changes, so a
    # working restore leaves both files as they were found.

    # /etc/passwd by UID: the account in question holds UID 1000 whatever
    # it is called, and there is no other NSS source.
    CRED_LIVE_USER=$(awk -F: '$3 == 1000 { print $1; exit }' /etc/passwd 2>/dev/null)
    # Must not be an account on this image (FIRST_USER_NAME='otp');
    # asserted below.
    CRED_OTHER_USER=pi
    CRED_OTHER_EXISTS=$(awk -F: -v u="$CRED_OTHER_USER" \
                        '$1 == u { print "yes"; exit }' /etc/passwd 2>/dev/null)
    case "$UC_SHADOW" in
        *Z) CRED_MANGLED="${UC_SHADOW%?}Y" ;;
        *)  CRED_MANGLED="${UC_SHADOW%?}Z" ;;
    esac
    printf '%s:%s\n' "$CRED_LIVE_USER" "$CRED_MANGLED" | chpasswd -e 2>/dev/null || true
    CRED_BEFORE=$(awk -F: -v u="$CRED_LIVE_USER" '$1 == u {print $2}' /etc/shadow 2>/dev/null)
    printf '%s:%s\n' "$CRED_OTHER_USER" "$UC_SHADOW" > "$CRED_STORE" 2>/dev/null || true
    /opt/otp-unit/persist-identity.sh; CRED_RELABEL_RC=$?
    CRED_AFTER=$(awk -F: -v u="$CRED_LIVE_USER" '$1 == u {print $2}' /etc/shadow 2>/dev/null)
    CRED_STORE_USER=$(cut -d: -f1 < "$CRED_STORE" 2>/dev/null | tr -d '\n\r')
    # The first three clauses are about the fixture:
    #   - the planted name is not an account here, nor the UID-1000 one;
    #   - the password differed before the script ran;
    #   - the script exited 0;
    #   - the live UID-1000 account carries the kept hash, 64 digest
    #     characters of it;
    #   - the store now names the live account.
    check credential-recovers-a-store-naming-another-account \
          "$(if [ -n "$CRED_LIVE_USER" ] \
                && [ -z "$CRED_OTHER_EXISTS" ] \
                && [ "$CRED_OTHER_USER" != "$CRED_LIVE_USER" ] \
                && [ -n "$CRED_BEFORE" ] && [ "$CRED_BEFORE" != "$UC_SHADOW" ] \
                && [ "$CRED_RELABEL_RC" = 0 ] \
                && [ "${#CRED_LIVE_DIGEST}" = 64 ] \
                && [ "$(cred_digest "$CRED_AFTER")" = "$CRED_LIVE_DIGEST" ] \
                && [ "$CRED_STORE_USER" = "$CRED_LIVE_USER" ]; \
             then echo yes; else echo no; fi)" \
          "store named $CRED_OTHER_USER (an account here: ${CRED_OTHER_EXISTS:-no}), uid-1000 is ${CRED_LIVE_USER:-none} | before: $(cred_short "$(cred_digest "$CRED_BEFORE")") | after: $(cred_short "$(cred_digest "$CRED_AFTER")") | kept: $(cred_short "$CRED_LIVE_DIGEST") | rc=$CRED_RELABEL_RC | store now names ${CRED_STORE_USER:-none}"

    # The malformed seed, run by hand rather than left for the unit: stock
    # userconfig.service has Restart=on-failure, and its failure lines are
    # ones harness/img-boot.sh fails a release on. Stdin is closed and TERM
    # unset, as for a unit with no tty.

    # Gated: the script terminated and quarantined the seed under the
    # failed_ name. The exit status is reported only. Terminated is
    # rc < 124, not rc != 124: a child that ignores SIGTERM is SIGKILLed by
    # -k and timeout answers 137. The `2>/dev/null` covers rc=none.
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
          "$(if [ "$UC_RC" != none ] && [ "$UC_RC" -lt 124 ] 2>/dev/null \
                && [ -e "$BOOTDIR/failed_userconf.txt" ] \
                && [ ! -e "$BOOTDIR/userconf.txt" ]; then echo yes; else echo no; fi)" \
          "rc=$UC_RC (>=124 = no clean status at the 60s bound: 124 killed by TERM, 137 ignored it and needed KILL) quarantined=$(yesno test -e "$BOOTDIR/failed_userconf.txt") leftover-seed=$(yesno test -e "$BOOTDIR/userconf.txt") said: ${UC_SAID:0:160}"
fi

sync 2>/dev/null || true

echo "--- otp-unit journal ($PHASE) ---"
journalctl -u otp-unit.service --no-pager -n 30 2>/dev/null || true
echo "--- end journal ---"

# DONE before RESULT: results with no DONE line are from a guest that died
# part way through a phase.
printf 'OTP-GUEST-DONE %s\n' "$PHASE"
printf 'OTP-RESULT %s %s/%s\n' "$PHASE" "$PASS" "$TOTAL"

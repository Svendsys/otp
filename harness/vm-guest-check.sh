#!/usr/bin/env bash
#
# Runs INSIDE the tier-2 virtual machine, as root.
#
#   vm-guest-check.sh <phase>
#
# Everything here is a claim device/install.sh or otp-unit.service makes
# that no other test can check, because checking it needs a booted systemd
# with real virtual terminals. pi-gen never boots the image it builds; a
# container has no VTs; the unit tests substitute systemd entirely. This is
# the only place these run.
#
# THREE PHASES, THREE BOOTS. Until this script grew phases, every check
# described a machine seconds after provisioning, with the service
# hand-started by the harness rather than by systemd at boot. The device's
# actual life is flash -> boot -> run -> power-cycle, and nothing observed
# the second boot at all.
#
#   provision  first boot, right after install.sh ran. The service is
#              hand-started by the harness.
#   reboot     second boot. SYSTEMD started the unit this time. Writes a
#              setting through config.save() for the next phase to find.
#   persist    third boot. The setting must still be there, read back
#              through config.load() rather than grepped for.
#
# A READ-ONLY OVERLAY ROOT IS NOT TESTED HERE -- see issue #9 and the note
# further down. Two mechanisms were tried and neither works in this guest.
#
# The core checks run in every phase, deliberately. A unit that starts when
# hand-started but not when systemd starts it at boot is precisely the class
# of bug a single-boot harness cannot see.
#
# Prints one line per check in a fixed format so the host can grep it:
#
#   OTP-CHECK <phase> <name> PASS|FAIL <detail>
#   OTP-RESULT <phase> <passed>/<total>
#
# The phase is in both markers because the host requires EVERY expected
# phase to have reported. Without that, a boot that never happened looks
# exactly like a boot with nothing to say -- and `tail -1` on the result
# line would have read the previous phase's success as the whole answer.
#
# Never exits non-zero on a failed check -- it reports them all and lets the
# host decide. A script that stopped at the first failure would hide the
# other nine behind it.
set -uo pipefail

PHASE="${1:-provision}"
PASS=0
TOTAL=0

# The boot partition, on its own disk -- the same geometry a Pi has, where
# /boot/firmware is a separate FAT partition that raspi-config's overlay
# leaves alone. That is what lets settings survive a device whose root
# filesystem discards every write.
BOOTDIR=/boot/firmware
# A page count no default produces, written through config.save() in the
# reboot phase and looked for again in persist. Deliberately not one of
# PAGE_CHOICES: a value the panel can produce would still be there if the
# unit rewrote the file itself, and would not prove the write survived.
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
# The kernel command line, every phase. Boots 2 and 3 get theirs from GRUB
# rather than from cloud-init's seed, and a botched edit there is invisible
# in every other way -- an earlier version dropped console=ttyS0 while
# adding the overlay flag, so two whole phases ran and reported into a
# console that was not connected to anything.
echo "OTP-CMDLINE $PHASE $(tr -d '\n' < /proc/cmdline)"

# --- the service itself -------------------------------------------------

# Give it a moment to settle. RestartSec is 15s, so a unit that is going to
# restart-loop will have done it at least once inside 45 seconds -- which
# is the failure this whole tier exists to catch.
sleep 45

STATE=$(systemctl is-active otp-unit.service 2>&1 || true)
check service-active \
      "$(if [ "$STATE" = "active" ]; then echo yes; else echo no; fi)" \
      "is-active=$STATE"

RESTARTS=$(systemctl show otp-unit.service -p NRestarts --value 2>/dev/null || echo "?")
# A unit that cannot start restarts every RestartSec. Anything above one is
# a loop, not a hiccup.
check no-restart-loop \
      "$(if [ "${RESTARTS:-9}" -le 1 ] 2>/dev/null; then echo yes; else echo no; fi)" \
      "NRestarts=$RESTARTS"

# --- tty1, the riskiest untested change in the repository ---------------

# No login prompt on tty1. Both processes holding /dev/tty1 means
# interleaved output and keystrokes going to whichever grabbed them.
#
# WHAT KEEPS IT OFF changed with issue #20 and this check did not need to.
# It used to be otp-unit.service's Conflicts=getty@tty1.service, which
# stopped a getty that started; it is now a condition install.sh puts on the
# getty so none starts, plus one explicit `systemctl stop` for the getty a
# live machine already has running. The conflict had to go because it is
# symmetric -- userconf-pi's cancel-rename starts that getty at the end of
# every credential apply, and the unit went down with it -- and THIS CHECK
# IS WHY THE STOP EXISTS: with only the condition, the provision phase read
# `getty@tty1=active` with the unit deactivating a second after it started.
# It asks about the outcome rather than the mechanism, so it reads the same
# on both.
GETTY1=$(systemctl is-active getty@tty1.service 2>&1 || true)
check getty1-stopped \
      "$(if [ "$GETTY1" != "active" ]; then echo yes; else echo no; fi)" \
      "getty@tty1=$GETTY1"

# And the unit should be the one holding it.
MAIN_PID=$(systemctl show otp-unit.service -p MainPID --value 2>/dev/null || echo 0)
HOLDS_TTY1=no
if [ "${MAIN_PID:-0}" -gt 0 ] 2>/dev/null; then
    for fd in /proc/"$MAIN_PID"/fd/*; do
        target=$(readlink "$fd" 2>/dev/null || true)
        if [ "$target" = "/dev/tty1" ]; then HOLDS_TTY1=yes; break; fi
    done
fi
check unit-holds-tty1 "$HOLDS_TTY1" "MainPID=$MAIN_PID"

# A login prompt has to remain reachable, or a unit with a monitor attached
# and no working panel is a machine nobody can get into.
check getty2-enabled "$(yesno systemctl is-enabled getty@tty2.service)" \
      "is-enabled=$(systemctl is-enabled getty@tty2.service 2>&1 || true)"

# --- what install.sh claims about the system ----------------------------

SWAP=$(swapon --show --noheadings 2>/dev/null || true)
check swap-off "$(if [ -z "$SWAP" ]; then echo yes; else echo no; fi)" \
      "swapon=${SWAP:-none}"

check cups-running "$(yesno systemctl is-active cups.service)" \
      "is-active=$(systemctl is-active cups.service 2>&1 || true)"

# The generated cupsd.conf has to be one cupsd will actually load. install.sh
# gates on this, but only against the config as it stood at install time.
check cupsd-config-valid \
      "$(yesno cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf)" \
      ""

JOURNAL=$(grep -rhs '^Storage=' /etc/systemd/journald.conf.d/ /etc/systemd/journald.conf 2>/dev/null | tail -1)
check journal-volatile \
      "$(if [ "$JOURNAL" = "Storage=volatile" ]; then echo yes; else echo no; fi)" \
      "${JOURNAL:-unset}"

# LimitCORE=0 on the unit, not kernel.core_pattern. Measured here: the
# pattern was plain `core`, meaning systemd-coredump is not the registered
# handler and install.sh's Storage=none would never be consulted -- a dump
# would land in the working directory instead. The unit-level rlimit is
# what actually stops this process producing one, whatever the handler.
CORELIMIT=$(systemctl show otp-unit.service -p LimitCORE --value 2>/dev/null || echo "?")
check coredumps-off-for-the-unit \
      "$(if [ "$CORELIMIT" = "0" ]; then echo yes; else echo no; fi)" \
      "LimitCORE=$CORELIMIT core_pattern=$(sysctl -n kernel.core_pattern 2>/dev/null)"

# --- the spool is where the hardening says it is ------------------------

# Read the CONFIGURED RequestRoot and check where that lands, rather than
# asking about /run/cups. `findmnt --target` reports the fstype of the
# mount CONTAINING a path, /run is a tmpfs on every systemd system by
# construction, and install.sh creates /run/cups unconditionally -- so the
# old form answered "tmpfs" whether or not install.sh had redirected
# anything. Reverting the three set_cups_file lines left it green.
REQUEST_ROOT=$(awk '/^RequestRoot/{print $2}' /etc/cups/cups-files.conf 2>/dev/null)
SPOOL_FS=$(findmnt -no FSTYPE --target "${REQUEST_ROOT:-/var/spool/cups}" 2>/dev/null || echo "?")
check spool-redirected-to-tmpfs \
      "$(if [ -n "$REQUEST_ROOT" ] && [ "$SPOOL_FS" = "tmpfs" ] \
            && [ "${REQUEST_ROOT#/var}" = "$REQUEST_ROOT" ]; then echo yes; else echo no; fi)" \
      "RequestRoot=${REQUEST_ROOT:-unset} fstype=$SPOOL_FS"

# --- provision only -----------------------------------------------------

if [ "$PHASE" = "provision" ]; then
    # The boot partition has to be a separate mount before install.sh writes
    # a setting to it, or the settings tested two boots from now were never
    # on the partition whose survival is the thing in question.
    BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
    check boot-partition-mounted \
          "$(if [ "$BOOT_SRC" != "?" ] && [ "$BOOT_SRC" != "$(findmnt -no SOURCE --target / 2>/dev/null)" ]; then echo yes; else echo no; fi)" \
          "$BOOTDIR src=$BOOT_SRC"

    # The repo really did unpack. `tar -xf` against the wrong disk exits 0
    # having produced nothing, and the next thing to notice was install.sh
    # exiting 127 for want of existing -- 128 seconds later, with the cause
    # long scrolled away.
    check repo-unpacked "$(yesno test -x /repo/device/install.sh)" \
          "/repo/device/install.sh"

    # install.sh says "safe to rerun" at the top of the file. Rerunning it
    # on a provisioned system is the only way to find out, and it is exactly
    # what somebody iterating on a real Pi does.
    #
    # Provision phase only: it is slow, and rerunning it on every boot
    # would test apt more than it tests idempotency.
    if [ -d /repo ]; then
        if /repo/device/install.sh --skip-apt >/tmp/rerun.log 2>&1; then
            check install-idempotent yes "second run exited 0"
        else
            check install-idempotent no "second run failed: $(tail -3 /tmp/rerun.log | tr '\n' ' ')"
        fi
        systemctl restart otp-unit.service 2>/dev/null || true
        sleep 10
        AFTER=$(systemctl is-active otp-unit.service 2>&1 || true)
        check service-survives-reprovision \
              "$(if [ "$AFTER" = "active" ]; then echo yes; else echo no; fi)" \
              "is-active=$AFTER"
    fi
fi

# --- the boots systemd drove --------------------------------------------

# NO READ-ONLY OVERLAY IS TESTED HERE. That is issue #9, and it is left
# open deliberately rather than faked: two mechanisms have been tried in
# this guest and neither works.
#
#   - Debian's `overlayroot` package feeds `mount` an option it rejects,
#     fails to pivot, and panics the kernel: "Attempted to kill init!".
#   - `systemd.volatile=overlay` is accepted on the kernel command line and
#     silently ignored. It is implemented by systemd INSIDE THE INITRD, and
#     Debian's initramfs-tools initrd has no systemd in it. Measured: the
#     flag present in /proc/cmdline, and / still plain rw ext4.
#
# What these phases DO establish is what issue #8 was about, and it is not
# a small thing: the unit comes up on boots that SYSTEMD drove, twice over,
# on a machine where nobody hand-started anything. Every core check above
# runs again in each of them.
if [ "$PHASE" = "reboot" ] || [ "$PHASE" = "persist" ]; then
    # The boot partition has to be its own mount, or the setting checked a
    # boot from now was never on the partition whose survival is the point.
    BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
    BOOT_FS=$(findmnt -no FSTYPE --target "$BOOTDIR" 2>/dev/null || echo "?")
    check boot-partition-separate \
          "$(if [ "$BOOT_SRC" != "?" ] && [ "$BOOT_SRC" != "$(findmnt -no SOURCE --target / 2>/dev/null)" ]; then echo yes; else echo no; fi)" \
          "$BOOTDIR src=$BOOT_SRC fstype=$BOOT_FS"
fi

if [ "$PHASE" = "reboot" ]; then
    # Through config.save() at its real CONFIG_PATH, not a shell redirect
    # to somewhere convenient. The code that persists settings on the
    # device is the thing under test, and it is the only writer that has to
    # work with the root read-only.
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
    check setting-saved \
          "$(if [ "${SAVED#*save=True}" != "$SAVED" ]; then echo yes; else echo no; fi)" \
          "$SAVED"

    # The remount fallback, which nothing else runs. config.save() writes
    # directly when the boot partition is writable -- the normal case, and
    # what the check above just exercised. An operator who has made it
    # read-only takes the other branch: remount rw, write, put it back.
    #
    # Worth its own check because that branch had a real bug -- the
    # `finally` was attached to the wrong `try`, so a write that failed for
    # an unrelated reason (a full boot partition, say) left the filesystem
    # remounted read-only as a side effect. Nothing would have noticed until
    # the next thing to need it writable.
    mount -o remount,ro "$BOOTDIR" 2>/dev/null || true
    FALLBACK=$(MARK="$MARK_PAGES" python3 - <<'PY' 2>&1 || true
import os, sys
sys.path.insert(0, "/opt/otp-unit")
from dataclasses import replace
from otpunit import config
saved = replace(config.load(), pages=int(os.environ["MARK"]))
print(f"save={config.save(saved)}")
PY
)
    AFTER_OPTS=$(findmnt -no OPTIONS --target "$BOOTDIR" 2>/dev/null || echo "?")
    check save-remounts-and-restores \
          "$(if [ "${FALLBACK#*save=True}" != "$FALLBACK" ] \
                && [ "${AFTER_OPTS%%,*}" = "ro" ]; then echo yes; else echo no; fi)" \
          "$FALLBACK left=${AFTER_OPTS%%,*}"
    mount -o remount,rw "$BOOTDIR" 2>/dev/null || true
fi

if [ "$PHASE" = "persist" ]; then
    # Read back through config.load(), not by grepping the file. A setting
    # that persisted as bytes but no longer parses is not a setting that
    # persisted -- load() falls back to the default for any field it cannot
    # use, silently and by design.
    KEPT=$(python3 - <<'PY' 2>&1 || true
import sys
sys.path.insert(0, "/opt/otp-unit")
from otpunit import config
print(config.load().pages)
PY
)
    KEPT=${KEPT//[$'\n\r']/}
    check settings-survive-reboot \
          "$(if [ "$KEPT" = "$MARK_PAGES" ]; then echo yes; else echo no; fi)" \
          "pages=$KEPT want=$MARK_PAGES from ${BOOTDIR}/otp-unit.conf"
fi

echo "--- otp-unit journal ($PHASE) ---"
journalctl -u otp-unit.service --no-pager -n 40 2>/dev/null || true
echo "--- end journal ---"

printf 'OTP-RESULT %s %s/%s\n' "$PHASE" "$PASS" "$TOTAL"

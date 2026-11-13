#!/usr/bin/env bash
#
# Runs inside the tier-2 virtual machine, as root.
#
#   vm-guest-check.sh <phase>
#
# Checks claims device/install.sh and otp-unit.service make that need a
# booted systemd with real virtual terminals. Three phases, three boots:
#
#   provision  first boot, right after install.sh ran. The service is
#              hand-started by the harness.
#   reboot     second boot; systemd started the unit. Writes a setting
#              through config.save() for the next phase to find.
#   persist    third boot. The setting must read back through
#              config.load().
#
# The core checks run in every phase: a unit that starts by hand but not
# at boot is what a single-boot harness cannot see. A read-only overlay
# root is not tested here (issue #9).
#
# Prints one line per check in a fixed format the host greps:
#
#   OTP-CHECK <phase> <name> PASS|FAIL <detail>
#   OTP-RESULT <phase> <passed>/<total>
#
# The phase is in both markers because the host requires every expected
# phase to have reported. Never exits non-zero on a failed check; the host
# decides.
set -uo pipefail

PHASE="${1:-provision}"
PASS=0
TOTAL=0

# The boot partition is its own disk, as on a Pi, where /boot/firmware is
# a separate FAT partition a read-only root overlay leaves alone.
BOOTDIR=/boot/firmware
# Written through config.save() in the reboot phase and read back in
# persist. Not one of PAGE_CHOICES: a value the panel can produce would
# not prove the write survived.
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
# Boots 2 and 3 take their command line from GRUB, not cloud-init's seed;
# a lost console=ttyS0 there is otherwise invisible.
echo "OTP-CMDLINE $PHASE $(tr -d '\n' < /proc/cmdline)"

# --- the service itself -------------------------------------------------

# RestartSec is 15s, so a unit that is going to restart-loop has done so
# at least once inside 45 seconds.
sleep 45

STATE=$(systemctl is-active otp-unit.service 2>&1 || true)
check service-active \
      "$(if [ "$STATE" = "active" ]; then echo yes; else echo no; fi)" \
      "is-active=$STATE"

RESTARTS=$(systemctl show otp-unit.service -p NRestarts --value 2>/dev/null || echo "?")
# Above one is a loop, not a hiccup.
check no-restart-loop \
      "$(if [ "${RESTARTS:-9}" -le 1 ] 2>/dev/null; then echo yes; else echo no; fi)" \
      "NRestarts=$RESTARTS"

# --- tty1, the riskiest untested change in the repository ---------------

# No login prompt on tty1: two processes holding /dev/tty1 interleave
# output and split keystrokes. Asks about the outcome, not the mechanism
# (install.sh's getty condition plus one explicit stop, issue #20).
GETTY1=$(systemctl is-active getty@tty1.service 2>&1 || true)
check getty1-stopped \
      "$(if [ "$GETTY1" != "active" ]; then echo yes; else echo no; fi)" \
      "getty@tty1=$GETTY1"

# The unit should be the one holding it.
MAIN_PID=$(systemctl show otp-unit.service -p MainPID --value 2>/dev/null || echo 0)
HOLDS_TTY1=no
if [ "${MAIN_PID:-0}" -gt 0 ] 2>/dev/null; then
    for fd in /proc/"$MAIN_PID"/fd/*; do
        target=$(readlink "$fd" 2>/dev/null || true)
        if [ "$target" = "/dev/tty1" ]; then HOLDS_TTY1=yes; break; fi
    done
fi
check unit-holds-tty1 "$HOLDS_TTY1" "MainPID=$MAIN_PID"

# A login prompt has to remain reachable on a unit with a monitor and no
# working panel.
check getty2-enabled "$(yesno systemctl is-enabled getty@tty2.service)" \
      "is-enabled=$(systemctl is-enabled getty@tty2.service 2>&1 || true)"

# --- what install.sh claims about the system ----------------------------

SWAP=$(swapon --show --noheadings 2>/dev/null || true)
check swap-off "$(if [ -z "$SWAP" ]; then echo yes; else echo no; fi)" \
      "swapon=${SWAP:-none}"

check cups-running "$(yesno systemctl is-active cups.service)" \
      "is-active=$(systemctl is-active cups.service 2>&1 || true)"

# install.sh gates on this only against the config as it stood at install
# time.
check cupsd-config-valid \
      "$(yesno cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf)" \
      ""

JOURNAL=$(grep -rhs '^Storage=' /etc/systemd/journald.conf.d/ /etc/systemd/journald.conf 2>/dev/null | tail -1)
check journal-volatile \
      "$(if [ "$JOURNAL" = "Storage=volatile" ]; then echo yes; else echo no; fi)" \
      "${JOURNAL:-unset}"

# LimitCORE=0 on the unit, not kernel.core_pattern: when systemd-coredump
# is not the registered handler, install.sh's Storage=none is never
# consulted, and the unit rlimit is what stops a dump.
CORELIMIT=$(systemctl show otp-unit.service -p LimitCORE --value 2>/dev/null || echo "?")
check coredumps-off-for-the-unit \
      "$(if [ "$CORELIMIT" = "0" ]; then echo yes; else echo no; fi)" \
      "LimitCORE=$CORELIMIT core_pattern=$(sysctl -n kernel.core_pattern 2>/dev/null)"

# --- the spool is where the hardening says it is ------------------------

# Read the configured RequestRoot, not /run/cups: `findmnt --target`
# reports the containing mount, and /run is a tmpfs whether or not
# install.sh redirected anything.
REQUEST_ROOT=$(awk '/^RequestRoot/{print $2}' /etc/cups/cups-files.conf 2>/dev/null)
SPOOL_FS=$(findmnt -no FSTYPE --target "${REQUEST_ROOT:-/var/spool/cups}" 2>/dev/null || echo "?")
check spool-redirected-to-tmpfs \
      "$(if [ -n "$REQUEST_ROOT" ] && [ "$SPOOL_FS" = "tmpfs" ] \
            && [ "${REQUEST_ROOT#/var}" = "$REQUEST_ROOT" ]; then echo yes; else echo no; fi)" \
      "RequestRoot=${REQUEST_ROOT:-unset} fstype=$SPOOL_FS"

# --- provision only -----------------------------------------------------

if [ "$PHASE" = "provision" ]; then
    # The boot partition has to be a separate mount before install.sh writes
    # a setting to it, or persist tests the wrong filesystem.
    BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
    check boot-partition-mounted \
          "$(if [ "$BOOT_SRC" != "?" ] && [ "$BOOT_SRC" != "$(findmnt -no SOURCE --target / 2>/dev/null)" ]; then echo yes; else echo no; fi)" \
          "$BOOTDIR src=$BOOT_SRC"

    # `tar -xf` against the wrong disk exits 0 having produced nothing.
    check repo-unpacked "$(yesno test -x /repo/device/install.sh)" \
          "/repo/device/install.sh"

    # install.sh claims to be safe to rerun. Provision only: it is slow, and
    # on every boot it would test apt more than idempotency.
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

# No read-only overlay is tested here (issue #9): Debian's `overlayroot`
# panics this guest, and `systemd.volatile=overlay` needs systemd in the
# initrd, which initramfs-tools does not provide. These phases establish
# that the unit comes up on boots systemd drove (issue #8).
if [ "$PHASE" = "reboot" ] || [ "$PHASE" = "persist" ]; then
    # The setting checked a boot from now must live on its own partition.
    BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
    BOOT_FS=$(findmnt -no FSTYPE --target "$BOOTDIR" 2>/dev/null || echo "?")
    check boot-partition-separate \
          "$(if [ "$BOOT_SRC" != "?" ] && [ "$BOOT_SRC" != "$(findmnt -no SOURCE --target / 2>/dev/null)" ]; then echo yes; else echo no; fi)" \
          "$BOOTDIR src=$BOOT_SRC fstype=$BOOT_FS"
fi

if [ "$PHASE" = "reboot" ]; then
    # Through config.save() at its real CONFIG_PATH, not a shell redirect:
    # the device's own writer is the thing under test.
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

    # The remount fallback: with the boot partition read-only, config.save()
    # remounts rw, writes, and must put it back read-only.
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
    # Read back through config.load(), not grep: load() silently defaults any
    # field it cannot parse.
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

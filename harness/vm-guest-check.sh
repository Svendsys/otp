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
#              hand-started; the root is still writable.
#   overlay    second boot. systemd started the unit on its own, and the
#              read-only overlay is engaged. Writes a sentinel to / and a
#              setting to the boot partition, for the next phase to find.
#   persist    third boot. The sentinel must be GONE (the overlay really
#              discards) and the setting must still be THERE (the boot
#              partition really is outside the overlay).
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

# The boot partition, mounted from a separate disk outside the overlay --
# the same geometry a Pi has, where /boot/firmware is its own FAT partition
# and raspi-config's overlay leaves it alone.
BOOTDIR=/boot/firmware
SENTINEL=/otp-harness-sentinel
# A page count no default produces, written through config.save() in the
# overlay phase and looked for again in persist. Deliberately not one of
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

# Conflicts=getty@tty1.service should have stopped the login prompt. Both
# processes holding /dev/tty1 means interleaved output and keystrokes going
# to whichever grabbed them.
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

    # Assert the overlay package is here before the run depends on it. It is
    # in the cloud-init package list, but a package that failed to install
    # would otherwise surface as an overlay that mysteriously did not
    # engage, two boots and several minutes later, with the real cause
    # scrolled far off the top of the console.
    check overlayroot-installed "$(yesno dpkg -s overlayroot)" \
          "$(dpkg-query -W -f='${Version}' overlayroot 2>/dev/null || echo 'not installed')"

    # install.sh says "safe to rerun" at the top of the file. Rerunning it
    # on a provisioned system is the only way to find out, and it is exactly
    # what somebody iterating on a real Pi does.
    #
    # Provision phase only: under the read-only overlay a rerun would fail
    # for a reason that has nothing to do with idempotency.
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

# --- the read-only overlay ----------------------------------------------

# WHAT THIS DOES AND DOES NOT PROVE. The guest uses Debian's `overlayroot`
# package; a Pi uses `raspi-config nonint enable_overlayfs`, which is a
# different mechanism. So these checks establish that THE UNIT SURVIVES A
# READ-ONLY ROOT -- that cupsd starts, the spool lands somewhere writable,
# settings still persist. They do NOT establish that the Pi's overlay is
# correctly configured in the shipped image. That claim belongs to tier 3,
# which boots the real thing. Conflating the two would be exactly the kind
# of overstatement this harness exists to catch.
if [ "$PHASE" = "overlay" ] || [ "$PHASE" = "persist" ]; then
    ROOT_OPTS=$(findmnt -no OPTIONS --target / 2>/dev/null || echo "?")
    ROOT_FS=$(findmnt -no FSTYPE --target / 2>/dev/null || echo "?")
    # Ask the kernel, not the config. An overlay that was configured and
    # failed to engage still leaves overlayroot.conf saying "tmpfs".
    check root-is-overlay \
          "$(if [ "$ROOT_FS" = "overlay" ]; then echo yes; else echo no; fi)" \
          "fstype=$ROOT_FS opts=${ROOT_OPTS%%,*}"

    # The lower layer is what must be read-only. The overlay itself accepts
    # writes -- into RAM -- which is the whole point, and is why "can I
    # write to /" is NOT the check here. `persist` is where that gets
    # settled, by finding the write gone.
    LOWER_RO=$(findmnt -no OPTIONS /media/root-ro 2>/dev/null || true)
    check lower-layer-read-only \
          "$(if [ "${LOWER_RO%%,*}" = "ro" ]; then echo yes; else echo no; fi)" \
          "/media/root-ro=${LOWER_RO:-not mounted}"

    # The boot partition has to stay writable and outside the overlay, or
    # settings cannot persist at all.
    BOOT_SRC=$(findmnt -no SOURCE --target "$BOOTDIR" 2>/dev/null || echo "?")
    BOOT_FS=$(findmnt -no FSTYPE --target "$BOOTDIR" 2>/dev/null || echo "?")
    check boot-partition-separate \
          "$(if [ "$BOOT_FS" != "overlay" ] && [ "$BOOT_SRC" != "?" ]; then echo yes; else echo no; fi)" \
          "$BOOTDIR src=$BOOT_SRC fstype=$BOOT_FS"
fi

if [ "$PHASE" = "overlay" ]; then
    # Leave a mark on the overlay. The next boot decides what happened to
    # it. Writing it is not the test -- the overlay accepts any write --
    # so nothing here is checked beyond the write having been possible.
    date > "$SENTINEL" 2>/dev/null || true
    check sentinel-written "$(yesno test -f "$SENTINEL")" \
          "wrote $SENTINEL, expect it GONE next boot"

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
    # The load-bearing pair. If the overlay is not really discarding, the
    # sentinel is still here. If the boot partition is inside the overlay,
    # the setting is gone. Either way somebody's assumption is wrong.
    check overlay-discards-root-writes \
          "$(if [ ! -e "$SENTINEL" ]; then echo yes; else echo no; fi)" \
          "$SENTINEL $(if [ -e "$SENTINEL" ]; then echo 'SURVIVED a reboot'; else echo gone; fi)"

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

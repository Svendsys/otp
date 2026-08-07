#!/usr/bin/env bash
#
# Runs INSIDE the tier-2 virtual machine, as root, after install.sh.
#
# Everything here is a claim device/install.sh or otp-unit.service makes
# that no other test can check, because checking it needs a booted systemd
# with real virtual terminals. pi-gen never boots the image it builds; a
# container has no VTs; the unit tests substitute systemd entirely. This is
# the only place these run.
#
# Prints one line per check in a fixed format so the host can grep it:
#
#   OTP-CHECK <name> PASS|FAIL <detail>
#   OTP-RESULT <passed>/<total>
#
# Never exits non-zero on a failed check -- it reports them all and lets
# the host decide. A script that stopped at the first failure would hide
# the other nine behind it.
set -uo pipefail

PASS=0
TOTAL=0

check() {
    local name="$1" ok="$2" detail="${3:-}"
    TOTAL=$((TOTAL + 1))
    if [ "$ok" = "yes" ]; then
        PASS=$((PASS + 1))
        printf 'OTP-CHECK %s PASS %s\n' "$name" "$detail"
    else
        printf 'OTP-CHECK %s FAIL %s\n' "$name" "$detail"
    fi
}

yesno() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }

echo "OTP-GUEST starting on $(uname -srm)"

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

SPOOL=$(findmnt -no FSTYPE --target /var/spool/cups 2>/dev/null || echo "?")
RUNCUPS=$(findmnt -no FSTYPE --target /run/cups 2>/dev/null || echo "?")
check spool-on-tmpfs \
      "$(if [ "$SPOOL" = "tmpfs" ] || [ "$RUNCUPS" = "tmpfs" ]; then echo yes; else echo no; fi)" \
      "/var/spool/cups=$SPOOL /run/cups=$RUNCUPS"

# --- idempotency, which is a documented promise -------------------------

# install.sh says "safe to rerun" at the top of the file. Rerunning it on a
# provisioned system is the only way to find out, and it is exactly what
# somebody iterating on a real Pi does.
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

echo "--- otp-unit journal ---"
journalctl -u otp-unit.service --no-pager -n 40 2>/dev/null || true
echo "--- end journal ---"

printf 'OTP-RESULT %s/%s\n' "$PASS" "$TOTAL"

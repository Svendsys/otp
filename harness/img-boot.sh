#!/usr/bin/env bash
#
# Tier 3: boot the image the release pipeline actually produces.
#
#   ./harness/img-boot.sh image/deploy/otp-print-unit.img.xz
#
# HISTORY, because this took three runs and each one moved the goalposts.
#
# Run 1 (31260949543): nothing at all on the serial console -- not even
# "Linux version". The MBR-derived boot offset, the power-of-two padding
# and both mcopy calls all worked; the kernel simply never got going.
#
# Run 2 (31263053487): IT BOOTS. The cause was the DTB -- the script chose
# bcm2710-rpi-3-b-PLUS.dtb, above a comment claiming that was what QEMU's
# raspi3b models. It is not: `-M raspi3b` is the plain 3 Model B, and a
# device tree describing hardware the emulator does not provide kills the
# kernel before console init. With the plain dtb: ext4 root mounted, init
# ran, systemd 257 came up, hostname set. Linux-version and systemd went
# from FAIL to PASS.
#
# Run 2 then stopped at 11.4 seconds with `qemu exit: 0`, right after
# "Detected first boot" -- a Pi OS image resizes its root filesystem on
# first boot and reboots, and -no-reboot turned that into a clean exit.
# That flag is gone; see the qemu invocation below.
#
# See issue #17.
#
# WHAT THIS CATCHES that the other tiers do not: whether the artifact
# BOOTS. pi-gen assembles a filesystem and never starts it; tier 2 boots a
# Debian cloud image rather than this one. Between them nothing has ever
# powered on the thing that gets flashed to a card. A missing kernel
# module, an fstab referring to a partition that moved, an initramfs that
# does not build -- all invisible until something tries.
#
# NOT cmdline.txt, and not config.txt. QEMU reads neither: -append below
# REPLACES the kernel command line wholesale, and config.txt is firmware
# configuration the emulator has no equivalent of. So dtparam=i2c_arm=on
# and the disable-wifi/disable-bt overlays are inert here. Do not read a
# green tier 3 as evidence about either file.
#
# WHAT IT DOES NOT CATCH, and this is the important caveat: QEMU's raspi3b
# is a Pi with nothing plugged into it. No OLED on the I2C bus, no buttons
# on the header, no printer. Peripheral coverage is WORSE here than tier
# 1, which simulates all three. This tier answers exactly one question --
# does the image come up and does the unit start -- and should be run once
# per image rather than per commit.
set -euo pipefail

IMAGE_XZ="${1:?usage: img-boot.sh <image.img.xz>}"
WORK="${OTP_IMG_WORK:-${TMPDIR:-/tmp}/otp-img}"
# Two emulated boots now, not one -- the first-boot resize reboots. Guest
# time ran at roughly half real time in the first successful boot (11.4s of
# guest in ~20s of wall), so this is headroom rather than an estimate.
TIMEOUT="${OTP_IMG_TIMEOUT:-1200}"

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
IMG="$WORK/card.img"
CONSOLE="$WORK/console.log"

log() { printf '\n== %s\n' "$*" >&2; }

for tool in qemu-system-aarch64 xz; do
    command -v "$tool" >/dev/null || { echo "ERROR: $tool missing" >&2; exit 1; }
done

log "Decompressing $IMAGE_XZ"
xz -dc "$IMAGE_XZ" > "$IMG"

# QEMU's sd-card device requires a power-of-two image size and refuses
# anything else outright. pi-gen's output never happens to be one.
SIZE=$(stat -c%s "$IMG")
TARGET=1
while [ "$TARGET" -lt "$SIZE" ]; do TARGET=$((TARGET * 2)); done
if [ "$TARGET" != "$SIZE" ]; then
    log "Padding $SIZE -> $TARGET bytes for the sd interface"
    truncate -s "$TARGET" "$IMG"
fi

# QEMU does not run the Pi's proprietary bootloader, so the kernel and the
# device tree have to be handed to it directly. Both live on the FAT boot
# partition, whose start is read from the MBR rather than hardcoded. It WAS hardcoded to sector 8192,
# and pi-gen's arm64 branch uses ALIGN=8MiB, so the boot partition starts
# at sector 16384 -- the extraction aimed at the zero-filled pre-partition
# gap, both mcopy calls failed silently, and the script blamed the image
# for a harness bug. Hardcoding 16384 instead would just move the breakage
# to pi-gen's next ALIGN change, which is how this arose.
log "Locating the boot partition from the partition table"
FIRST_LBA=$(od -An -tu4 -j454 -N4 "$IMG" | tr -d ' ')
if [ -z "$FIRST_LBA" ] || [ "$FIRST_LBA" -lt 1 ] 2>/dev/null; then
    echo "ERROR: could not read the first partition's start from the MBR" >&2
    exit 1
fi
BOOT_OFFSET=$((FIRST_LBA * 512))
log "boot partition starts at sector $FIRST_LBA (byte $BOOT_OFFSET)"
# Cleared, not just created: a second run against an image whose boot
# partition cannot be read would otherwise boot the PREVIOUS image's
# kernel and report a verdict on it.
rm -rf "${WORK:?}/boot"
mkdir -p "$WORK/boot"
if command -v mcopy >/dev/null; then
    # Errors NOT silenced: "Cannot initialize '::'" is the useful message,
    # and hiding it is what made a wrong offset look like a bad image.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::kernel8.img "$WORK/boot/" || true
    # PLAIN 3-b first, -plus only as a fallback. The order used to be the
    # other way round, above a comment asserting that "-plus is what QEMU's
    # raspi3b models" -- which is backwards. `-M raspi3b` models the
    # Raspberry Pi 3 Model B; the B+ is a different board and QEMU has no
    # machine for it. Handing the kernel a device tree describing hardware
    # the emulator is not providing is a strong candidate for the first
    # run's symptom: qemu exited 0 having written NOTHING to the serial
    # console, not even "Linux version", which is what an early reset
    # before console init looks like.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::bcm2710-rpi-3-b.dtb "$WORK/boot/" \
        || mcopy -n -i "$IMG@@$BOOT_OFFSET" ::bcm2710-rpi-3-b-plus.dtb "$WORK/boot/" || true
else
    echo "ERROR: mtools (mcopy) is needed to read the boot partition" >&2
    exit 1
fi

KERNEL="$WORK/boot/kernel8.img"
DTB=$(find "$WORK/boot" -name '*.dtb' | head -n 1)
if [ ! -f "$KERNEL" ] || [ -z "$DTB" ]; then
    echo "ERROR: no kernel8.img or dtb in the image's boot partition" >&2
    ls -la "$WORK/boot" >&2
    exit 1
fi
log "kernel=$KERNEL dtb=$DTB"

# root=/dev/mmcblk0p2 rather than whatever cmdline.txt says: pi-gen writes
# a PARTUUID, and QEMU's sd emulation does not reproduce it.
log "Booting under -M raspi3b (this is emulated, not KVM -- allow minutes)"
# BOTH UARTs, captured separately. A Pi 3 has two -- the PL011 (ttyAMA0)
# and the mini-UART (ttyS0) -- and which one -serial #1 lands on is exactly
# the kind of thing to have backwards. The first run captured one port and
# got a completely empty file, which is indistinguishable from "the kernel
# never ran". Capturing both, and reporting how many bytes each produced,
# tells the difference on the next run rather than the one after that.
CONSOLE2="$WORK/console-uart1.log"
CONSOLE_ALL="$WORK/console-all.log"
: > "$CONSOLE"
: > "$CONSOLE2"
set +e
# THE COMMAND LINE IS A PERFORMANCE DECISION, not just a configuration one.
# It used to read `loglevel=8 console=ttyAMA0 console=ttyS0`, and that run
# spent its entire 1200-second budget reaching 7.6 seconds of guest time --
# roughly a 160x slowdown.
#
# Under TCG every character out an emulated UART costs real work, and that
# line asked for the maximum: loglevel=8 prints debug-level messages, and
# naming two consoles makes the kernel emit every one of them TWICE.
#
#   loglevel=6  keeps KERN_NOTICE and above, which is what "Linux version"
#               is printed at, and drops the KERN_INFO flood that makes up
#               the bulk of a boot.
#   one console rather than two. The dual-UART capture below stays -- both
#               ports are still recorded -- but the kernel is only ASKED to
#               write to one. We know from the previous run which one that
#               is; there is no longer a reason to pay double to find out.
#
# earlyprintk is gone: it is the x86/arm32 spelling and a no-op on arm64.
# earlycon is the one that does anything here.
#
# systemd.show_status=1 is explicit because the verdict greps for "Started
# OTP pad print unit" and "Reached target", which are systemd's status
# lines rather than kernel output. Too much has been lost today to output
# that was assumed rather than asked for.
#
# NO -no-reboot, deliberately. A Pi OS image resizes its root filesystem on
# first boot and then REBOOTS; -no-reboot turned that into a clean qemu
# exit at 11.4 seconds, which read as "the unit never started":
#
#   systemd[1]: Detected first boot.
#   systemd[1]: Hostname set to <otp-unit>.
#   systemd[1]: Initializing machine ID from random generator.
#   [ qemu exit: 0 ]
#
# Letting it reboot is also what the real device does, so the run now
# covers the same two boots an operator gets from a freshly flashed card.
# A genuine reboot loop is still caught: it burns the timeout and exits
# 124, which the verdict gates on.
# -k 30 for the same reason as tier 2: bare `timeout` sends TERM and waits
# forever for a process that ignores it, and a job killed by GitHub's own
# timeout skips the steps that would have reported why.
timeout -k 30 "$TIMEOUT" qemu-system-aarch64 \
    -M raspi3b -m 1024 \
    -kernel "$KERNEL" -dtb "$DTB" \
    -append "rw earlycon loglevel=6 console=ttyAMA0,115200 systemd.show_status=1 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait" \
    -drive "file=$IMG,if=sd,format=raw" \
    -serial "file:$CONSOLE" \
    -serial "file:$CONSOLE2" \
    -display none &
QEMU_PID=$!

# Sample the console while it boots. A guest grinding slowly along and a
# guest wedged solid look identical from outside once the output stops, and
# telling them apart cost a 55-minute build and still ended in a guess. The
# growth column answers it directly: still climbing means slow, flat for
# minutes means stuck.
SAMPLE=30
ELAPSED=0
LAST=0
while kill -0 "$QEMU_PID" 2>/dev/null; do
    sleep "$SAMPLE"
    ELAPSED=$((ELAPSED + SAMPLE))
    NOW=$(wc -c < "$CONSOLE" 2>/dev/null || echo 0)
    NOW2=$(wc -c < "$CONSOLE2" 2>/dev/null || echo 0)
    printf '   %4ss  uart0=%-8s (+%-6s) uart1=%s\n' \
           "$ELAPSED" "$NOW" "$((NOW - LAST))" "$NOW2" >&2
    LAST=$NOW
done
wait "$QEMU_PID"
QEMU_RC=$?
set -e

# Everything downstream reads the combination, so a boot that talks to
# whichever port is still judged on what it said.
cat "$CONSOLE" "$CONSOLE2" > "$CONSOLE_ALL" 2>/dev/null || true
log "uart0=$(wc -c < "$CONSOLE") bytes  uart1=$(wc -c < "$CONSOLE2") bytes"
if [ ! -s "$CONSOLE_ALL" ]; then
    # Worth saying outright rather than leaving as a wall of FAILs. An
    # empty console is not "the unit did not start" -- it is "there is no
    # evidence the kernel ever ran", and the two have entirely different
    # causes.
    log "NEITHER uart produced any output: no evidence the kernel ran at all"
fi

# --- the verdict --------------------------------------------------------

VERDICT="$WORK/verdict.txt"
{
    printf 'qemu exit: %s\n' "$QEMU_RC"
    # NOT a bare grep for "otp-unit". The image's hostname IS otp-unit, so
    # `Set hostname to <otp-unit>` and the `otp-unit login:` prompt both
    # match -- proven against a synthetic console: a boot where the service
    # failed 216/GROUP and restart-looped, and a boot where the unit was
    # not installed at all, BOTH reported PASS. The unit's Description is
    # "OTP pad print unit", so systemd's success line does not contain the
    # string at all. Gate on what only a running unit emits.
    if grep -q "Started OTP pad print unit" "$CONSOLE_ALL" 2>/dev/null; then
        printf 'IMG-CHECK unit-started PASS\n'
    else
        printf 'IMG-CHECK unit-started FAIL\n'
    fi
    for bad in "status=216" "Failed with result" "Scheduled restart job" \
               "Kernel panic" "Unable to mount root"; do
        if grep -qF -- "$bad" "$CONSOLE_ALL" 2>/dev/null; then
            printf 'IMG-CHECK no-%s FAIL\n' "$(printf '%s' "$bad" | tr ' =' '--')"
        fi
    done
    for want in "Linux version" "systemd" "Reached target"; do
        if grep -qF -- "$want" "$CONSOLE_ALL" 2>/dev/null; then
            printf 'IMG-CHECK %s PASS\n' "$(printf '%s' "$want" | tr ' ' '-')"
        else
            printf 'IMG-CHECK %s FAIL\n' "$(printf '%s' "$want" | tr ' ' '-')"
        fi
    done
} > "$VERDICT"
cat "$VERDICT"

# qemu's own exit status is part of the verdict. It was printed and gated
# on nowhere, so a run killed by the timeout still reported success.
if [ "$QEMU_RC" != 0 ]; then
    echo "qemu exited $QEMU_RC (124 = killed by the ${TIMEOUT}s timeout)" >&2
    tail -n 60 "$CONSOLE_ALL" >&2
    exit 1
fi
if grep -q "FAIL" "$VERDICT"; then
    echo "the image did not boot cleanly to a started unit" >&2
    echo "--- last 60 console lines ---" >&2
    tail -n 60 "$CONSOLE_ALL" >&2
    exit 1
fi
log "the image boots and the unit starts"

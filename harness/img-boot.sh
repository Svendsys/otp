#!/usr/bin/env bash
#
# Tier 3: boot the image the release pipeline actually produces.
#
#   ./harness/img-boot.sh image/deploy/otp-print-unit.img.xz
#
# FIRST RUN: 2026-08-08, image.yml run 31260949543. It did not boot.
#
# What worked, all of it fixed by review before it had ever run: the boot
# partition was located from the MBR at sector 16384 (the old hardcoded
# 8192 would have read the pre-partition gap), the image was padded to a
# power of two for the sd interface, and both mcopy calls found their
# files. What failed: qemu exited 0 having written NOTHING to the serial
# console -- not even "Linux version" -- so all four checks reported FAIL
# with no evidence the kernel had run at all.
#
# Two candidate causes are addressed below, and the next run distinguishes
# them: the DTB was for the 3 Model B PLUS while `-M raspi3b` models the
# plain 3 Model B, and only one of the two UARTs was being captured. See
# issue #17.
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
TIMEOUT="${OTP_IMG_TIMEOUT:-900}"

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
# earlycon as well as earlyprintk: earlyprintk is the x86/arm32 spelling
# and is a no-op on arm64, so the window before the real console comes up
# was never being reported at all. console= for both ports for the same
# reason as the two -serial flags.
timeout "$TIMEOUT" qemu-system-aarch64 \
    -M raspi3b -m 1024 \
    -kernel "$KERNEL" -dtb "$DTB" \
    -append "rw earlycon earlyprintk loglevel=8 console=ttyAMA0,115200 console=ttyS0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait" \
    -drive "file=$IMG,if=sd,format=raw" \
    -serial "file:$CONSOLE" \
    -serial "file:$CONSOLE2" \
    -display none -no-reboot
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

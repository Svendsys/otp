#!/usr/bin/env bash
#
# Tier 3: boot the image the release pipeline actually produces.
#
#   ./harness/img-boot.sh image/deploy/otp-print-unit.img.xz
#
# NEVER EXECUTED. This is written but unrun -- tier 3 needs an image, the
# image workflow only runs from master, and this branch is not merged.
# Attached to image.yml so it validates itself the first time an image is
# built after it lands, rather than sitting here claiming to work.
#
# WHAT THIS CATCHES that the other tiers do not: whether the artifact
# BOOTS. pi-gen assembles a filesystem and never starts it; tier 2 boots a
# Debian cloud image rather than this one. Between them nothing has ever
# powered on the thing that gets flashed to a card. A missing kernel
# module, a broken cmdline.txt, an fstab referring to a partition that
# moved -- all invisible until something tries.
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
# partition, which starts at sector 8192 on a pi-gen image.
log "Extracting the kernel and device tree from the boot partition"
BOOT_OFFSET=$((8192 * 512))
mkdir -p "$WORK/boot"
if command -v mcopy >/dev/null; then
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::kernel8.img "$WORK/boot/" 2>/dev/null || true
    # -plus is the raspi3b QEMU models; fall back to the plain 3b dtb.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::bcm2710-rpi-3-b-plus.dtb "$WORK/boot/" 2>/dev/null \
        || mcopy -n -i "$IMG@@$BOOT_OFFSET" ::bcm2710-rpi-3-b.dtb "$WORK/boot/" 2>/dev/null || true
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
: > "$CONSOLE"
set +e
timeout "$TIMEOUT" qemu-system-aarch64 \
    -M raspi3b -m 1024 \
    -kernel "$KERNEL" -dtb "$DTB" \
    -append "rw earlyprintk loglevel=8 console=ttyAMA0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait" \
    -drive "file=$IMG,if=sd,format=raw" \
    -serial "file:$CONSOLE" \
    -display none -no-reboot
QEMU_RC=$?
set -e

# --- the verdict --------------------------------------------------------

VERDICT="$WORK/verdict.txt"
{
    printf 'qemu exit: %s\n' "$QEMU_RC"
    for want in "Linux version" "systemd" "otp-unit" "Reached target"; do
        if grep -qi -- "$want" "$CONSOLE" 2>/dev/null; then
            printf 'IMG-CHECK %s PASS\n' "$want"
        else
            printf 'IMG-CHECK %s FAIL\n' "$want"
        fi
    done
} > "$VERDICT"
cat "$VERDICT"

if ! grep -q "IMG-CHECK otp-unit PASS" "$VERDICT"; then
    echo "the image did not get as far as starting the unit" >&2
    echo "--- last 60 console lines ---" >&2
    tail -n 60 "$CONSOLE" >&2
    exit 1
fi
log "the image boots and the unit starts"

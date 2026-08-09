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
# "Detected first boot", and -no-reboot was removed on the theory that a
# Pi OS first-boot resize was rebooting. WRONG: -append carries no
# `init=`, so init=/usr/lib/raspberrypi-sys-mods/firstboot never runs and
# no resize happens. The flag is back; see the qemu invocation.
#
# Runs 4 and 5 (31283220918, 31283879022) added 30-second console
# sampling and put -no-reboot back, and between them -- re-reading runs
# 2 and 3 against them -- the picture unified:
#
#   THE GUEST MACHINE RESETS AT ~11.5-12.5 SECONDS GUEST TIME, EVERY
#   RUN, ON BOTH x86 AND ARM64 HOSTS, WHEREVER THE BOOT HAPPENS TO BE.
#
#   - Run 2 (x86, loglevel=8): root mounted, systemd 257 up, machine-id
#     initializing -- reset at 11.43s, surfaced as exit 0 by -no-reboot.
#   - Run 3 (x86): the SAME reset, looping. Reported at the time as a
#     "160x slowdown"; that was a misread of a reboot loop.
#   - Run 4 (arm64, loglevel=6): same loop, ~30s wall per lap. Reported
#     at the time as "dies around mmc0 at 2.37s" -- also a misread. The
#     tail was the SIGTERM-truncated final lap, and loglevel=6 had
#     suppressed every KERN_INFO line, which is where mmcblk
#     enumeration, "VFS: Mounted root" and every systemd[1] message
#     live. The harness blinded itself and called the silence a death.
#   - Run 5 (arm64, -no-reboot): one lap, exit 0 at ~12.4s guest. Last
#     visible line: the 10-second deferred-probe report at 12.35s.
#
# A reset at a fixed guest time regardless of boot progress is a TIMER:
# armed early, firing ~10 seconds later -- the shape of a hardware
# watchdog. Two built-in drivers probe QEMU's incomplete device models
# inside the arming window: the bcm2835-pm watchdog/power cluster
# (whose power-domain child read garbage from the stub ASB -- "register
# ID returned 0x00000000" at ~2.2s) and dwc_otg ("Init: Power Port" at
# ~2.0s). The -append below blacklists both initcalls; if the reset
# stops, one of them armed it, and a 3-minute cached run can later say
# which. Whether the blacklist TOOK is visible in the console: no "FIQ
# FSM" lines means dwc_otg was skipped, no ASB line means bcm2835-pm
# was. See issue #17.
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
# A GENEROUS DEFAULT FOR A HUMAN, a tight cap in CI. Someone running this
# by hand is debugging and wants the room; CI has a ten-minute budget and
# sets OTP_IMG_TIMEOUT=360 in image.yml. The two numbers are meant to
# differ, and the CI one is the one that expresses policy.
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
    # Evidence, not execution: the image's own cmdline.txt and config.txt
    # ship in the artifact and neither is consulted here (see the header).
    # They ride along into the failure artifact so the divergence between
    # what the device would boot and what this harness booted is part of
    # the record rather than a thing to remember.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::cmdline.txt "$WORK/boot/" || true
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::config.txt "$WORK/boot/" || true
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
if [ -s "$WORK/boot/cmdline.txt" ]; then
    log "the image's own cmdline.txt (replaced wholesale by -append): $(tr -d '\r\n' < "$WORK/boot/cmdline.txt")"
fi

# root=/dev/mmcblk0p2 rather than cmdline.txt's root=PARTUUID=..., but not
# for the reason previously written here. The old comment claimed QEMU does
# not reproduce PARTUUIDs -- false. A PARTUUID is the MBR disk identifier
# plus a partition number, both of which ship inside the image file, so it
# resolves under QEMU exactly as on the device. The real reason is that
# cmdline.txt is replaced wholesale (it also carries init=...firstboot and
# quiet, neither wanted here), so root= must be restated -- and an explicit
# device name beats depending on which revision of cmdline.txt this build
# happened to produce.
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
# THE COMMAND LINE, and what previous revisions of it got wrong.
#
# loglevel=7, not 6 and not 8. KERN_INFO is where a boot narrates itself
# -- mmcblk enumeration, "VFS: Mounted root", every systemd[1] line --
# and loglevel=6 suppressed all of it, which is how a systemd-era reset
# got reported as an early-boot death (see the header). The 8 -> 6 change
# was justified as fixing a "160x slowdown"; that number was a misread of
# a reboot loop, and the performance story died with it. 7 keeps INFO and
# drops only DEBUG.
#
# initcall_blacklist: bcm2835_pm_driver_init, and the run history of why.
#
# Blacklisting BOTH bcm2835-pm and dwc_otg stopped the reset -- qemu
# stayed alive 270+ seconds where every prior run died inside 40 -- but
# the boot then stalled silently a few guest-seconds in, output flat,
# which is what rootwait polling for a card that never enumerated looks
# like. So one of the two armed the watchdog and the other is needed by
# the boot. This revision keeps ONLY the bcm2835-pm blacklist, on the
# mechanism argument: the PM block demands a 0x5a-password on every
# write, its power-domain child writes passworded values against QEMU's
# partial model (the garbage ASB read at ~2.2s is that same probe), and
# a passworded write landing where the model keeps WDOG is an armed
# watchdog. dwc_otg has no such path to the PM block.
#
#   - reset gone AND boot proceeds => bcm2835-pm was the armer; dwc_otg
#     innocent and restored. Solved.
#   - reset returns => the armer was dwc_otg after all; swap the names.
#   - reset gone, boot still stalls => the stall belongs to the pm
#     blacklist itself (genpd consumers deferring); different problem,
#     now isolated.
#
# An emulation accommodation of the same kind as -append itself: the
# DEVICE still boots this driver, and this harness says so.
#
# One console on the kernel command line; BOTH captured below. Under
# -M raspi3b, ttyAMA0 comes out of the SECOND -serial, not the first --
# measured: uart0=0 bytes, uart1=175525. An earlier comment claimed the
# opposite while citing the run that proved it. It kept working only
# because both ports are captured and the verdict greps the
# concatenation.
#
# systemd.show_status=1 because "Started OTP pad print unit" and
# "Reached target" are systemd status lines, not kernel output.
timeout -k 30 "$TIMEOUT" qemu-system-aarch64 \
    -M raspi3b -m 1024 \
    -kernel "$KERNEL" -dtb "$DTB" \
    -append "rw earlycon loglevel=7 console=ttyAMA0,115200 systemd.show_status=1 initcall_blacklist=bcm2835_pm_driver_init root=/dev/mmcblk0p2 rootfstype=ext4 rootwait" \
    -drive "file=$IMG,if=sd,format=raw" \
    -serial "file:$CONSOLE" \
    -serial "file:$CONSOLE2" \
    -display none -no-reboot &
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
LAST_TS=$(grep -oE '\[ *[0-9]+\.[0-9]+\]' "$CONSOLE_ALL" 2>/dev/null | tail -1 | tr -d '[] ')
KERNEL_ENTRIES=$(grep -c "Booting Linux on physical CPU" "$CONSOLE_ALL" 2>/dev/null || true)
{
    printf 'qemu exit: %s\n' "$QEMU_RC"
    # How far it got and how many times the kernel STARTED. Entries > 1 is
    # a reboot loop stated as a number, instead of an inference from byte
    # counts that has already been misread once.
    printf 'guest reached: %ss guest time; kernel entered %s time(s)\n' \
           "${LAST_TS:-?}" "${KERNEL_ENTRIES:-0}"
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
    # "systemd[1]:" and not "systemd". The bare string matches the
    # KERNEL COMMAND LINE, which the kernel prints at boot and which now
    # carries systemd.show_status=1 -- so this check passed on a boot that
    # never reached userspace at all, matching a flag this harness itself
    # added. Only PID 1 writes "systemd[1]:".
    for want in "Linux version" "systemd[1]:" "Reached target"; do
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
    tail -n 100 "$CONSOLE_ALL" >&2
    exit 1
fi
if grep -q "FAIL" "$VERDICT"; then
    echo "the image did not boot cleanly to a started unit" >&2
    if [ "$QEMU_RC" = 0 ]; then
        # Exit 0 before the timeout + -no-reboot = the GUEST reset or
        # powered itself off. The reset instant is the end of the console
        # below -- this is not a hang and not a timeout.
        echo "qemu exited 0 before the timeout: the guest reset itself" >&2
        echo "at ~${LAST_TS:-?}s guest time. The console ends at the" >&2
        echo "reset instant." >&2
    fi
    echo "--- last 100 console lines ---" >&2
    tail -n 100 "$CONSOLE_ALL" >&2
    exit 1
fi
log "the image boots and the unit starts"

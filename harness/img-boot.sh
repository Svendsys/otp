#!/usr/bin/env bash
#
# Tier 3: boot the image the release pipeline actually produces.
#
#   ./harness/img-boot.sh image/deploy/otp-print-unit.img.xz
#
# HISTORY, because this took fifteen runs and each one moved the goalposts.
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
# watchdog. Run 6 blacklisted both suspects (bcm2835-pm and dwc_otg) and
# the reset STOPPED -- first run ever where qemu outlived 40 seconds.
# Run 7 blacklisted bcm2835-pm alone: kernel entered once, no reset,
# 31.6s of guest time reached, systemd running, targets reached --
# CONFIRMED. The armer is the bcm2835-pm watchdog/power cluster probing
# QEMU's partial PM model (the same probe whose ASB read returned
# garbage at ~2.2s). The reset was never the image's fault.
#
# Runs 7-11 then chased a SECOND fault that turned out not to exist.
# Every run "froze" silently at ~29.5-31.6s guest, always at the same
# structural moment -- journald just started -- and three theories died
# against it in sequence: dwc_otg wedging coldplug (run 9 froze without
# it), TCG slowness (the sampler showed the guest reaching 29.5s in
# under 60 wall seconds, then NOTHING for seven minutes), and an
# entropy stall, argued from "random: crng init done" being absent in
# every console -- a claim that was an artifact of reading job-log
# TAILS that begin after 8s guest, past where that line prints.
#
# The real fault was the harness's own console choice. On this DTB the
# PL011 is ttyAMA1 (serial0/stdout-path point at the mini-UART, whose
# bcm2835-aux driver fails to probe under QEMU), so console=ttyAMA0
# named a device that does not exist. No real console ever registered:
# kernel printk kept flowing only because the earlycon BOOTCONSOLE on
# the mini-UART is never handed off, and userspace's /dev/console was
# ENODEV. The moment journald started, PID 1 moved its logging into
# the journal, the audit stream redirected to journald, and unit
# status lines went to the dead console -- output stopped forever, AT
# EXACTLY THAT HANDOFF IN EVERY RUN, while the boot continued
# invisibly underneath. The "wedge" was a blindfold.
#
# Proven locally before being trusted, with the image's own kernel
# (6.12.96+rpt-rpi-v8), its own DTB, and CI's QEMU (8.2.2): with
# console=ttyAMA1 a busybox initramfs is fully visible, getrandom()
# unblocks at 3.4s (crng init at 2.4s -- entropy was always fine, so
# the rng_core/modules-load/trust_bootloader params runs 10-11 added
# are gone again), the machine outlives the old 11.5s watchdog point
# with the pm blacklist in place, and a replay of udev coldplug --
# modprobe of every /sys modalias, the exact activity the freeze was
# blamed on -- completes all 41 probes in 19.3s guest with zero hangs.
#
# Run 12, with the console finally visible, ended the hunt: the boot was
# HEALTHY. CUPS up, the unit STARTED -- and the run still failed on two
# measurement artifacts and one real find. "guest reached" took the
# concat's LAST timestamp, which is uart1's ~2s bootconsole handoff once
# uart0 became the live console; the unit-started pattern spanned
# "Started" into the Description and cannot straddle the ANSI color
# systemd wraps around the unit name; and the boot then parked forever
# on userconf-pi's first-boot wizard, which pi-gen's
# DISABLE_FIRST_BOOT_USER_RENAME=1 does not disable on a console-boot
# image (it only deletes the desktop wizard's autostart file). The first
# two are fixed in this file; the third is masked in install.sh, and is
# the kind of find this tier exists for -- a flashed device would have
# held its boot open on a whiptail dialog nobody can see.
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
# A BACKSTOP, not a target. Healthy boots end at the unit start line --
# the sampler stops the emulator the moment it appears (~430s wall under
# TCG), so this cap is only ever paid by a boot that never got there.
# CI sets OTP_IMG_TIMEOUT=600; the local default is roomier because
# someone running this by hand is debugging.
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
# BOTH UARTs, captured separately. A Pi 3 has two, and the measured QEMU
# mapping is: FIRST -serial = the PL011 (which this DTB names ttyAMA1),
# SECOND -serial = the mini-UART, where the earlycon bootconsole lives.
# For six runs the first file's stubborn 0 bytes was itself the clue
# nobody read: the PL011 never emitted because no console= ever named
# it. Capturing both is what eventually made the mapping provable.
CONSOLE2="$WORK/console-uart1.log"
CONSOLE_ALL="$WORK/console-all.log"
CONSOLE_TXT="$WORK/console-text.log"
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
# initcall_blacklist, one entry, one conviction:
#
#   bcm2835_pm_driver_init -- THE ~11.5s RESET, confirmed by bisection
#   (runs 6/7) and re-verified in the local rig (alive past 15s with
#   this blacklisted; runs 1-5 without it reset at ~11.5s every time).
#   The PM block demands a 0x5a password on every write; the pm
#   cluster writes passworded values against QEMU's partial model, and
#   one lands where the model keeps its watchdog. An emulation
#   accommodation: the DEVICE boots this driver against real hardware.
#
#   dwc_otg is deliberately NOT here. It spent runs 6-14 blacklisted on
#   precaution and was convicted of nothing in that time -- the reset
#   was the pm driver (bisected, run 7), and the "coldplug wedge" it
#   was suspected of was the dead-console blindfold. Run 15 restored
#   it; if the downstream USB driver ever does misbehave against
#   QEMU's controller model, the working console will print the line
#   that convicts it, which is more than any of its accusations had.
#
# console=ttyAMA1 because that is what the PL011 is CALLED under this
# DTB's aliases -- serial0 and stdout-path point at the mini-UART,
# whose driver does not probe under QEMU, and ttyAMA0 does not exist.
# Six runs diagnosed a "boot freeze" that was this parameter naming a
# nonexistent device (see the header). Both ports are still captured
# and the verdict greps the concatenation, so early bootconsole lines
# (mini-UART, second file) and the real console (PL011, first file)
# both land in evidence.
#
# systemd.show_status=1 because "Started OTP pad print unit" and
# "Reached target" are systemd status lines, not kernel output -- and
# they need the WORKING console above to reach the capture at all.
timeout -k 30 "$TIMEOUT" qemu-system-aarch64 \
    -M raspi3b -m 1024 \
    -kernel "$KERNEL" -dtb "$DTB" \
    -append "rw earlycon loglevel=7 console=ttyAMA1,115200 systemd.show_status=1 initcall_blacklist=bcm2835_pm_driver_init root=/dev/mmcblk0p2 rootfstype=ext4 rootwait" \
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
EARLY_STOP=
ESC=$(printf '\033')
while kill -0 "$QEMU_PID" 2>/dev/null; do
    sleep "$SAMPLE"
    ELAPSED=$((ELAPSED + SAMPLE))
    NOW=$(wc -c < "$CONSOLE" 2>/dev/null || echo 0)
    NOW2=$(wc -c < "$CONSOLE2" 2>/dev/null || echo 0)
    # A healthy boot of this image never ends by itself: the unit comes
    # up and the guest idles. Waiting out the full cap after the success
    # line has already been printed adds minutes and, worse, puts the
    # verdict in a race with the cap -- run 13's success line landed
    # around 420s of a 480s cap. Once the line is visible, give the
    # console a few seconds to drain and stop the emulator ourselves.
    if [ -z "$EARLY_STOP" ] && \
       sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' "$CONSOLE" 2>/dev/null \
           | grep -qF "Started otp-unit.service"; then
        EARLY_STOP=$ELAPSED
        printf '   unit start line visible at %ss wall; draining 10s, then stopping qemu\n' "$ELAPSED" >&2
        sleep 10
        kill "$QEMU_PID" 2>/dev/null || true
    fi
    # Host-side CPU%% of the emulator, because flat output alone cannot
    # distinguish a guest idle-waiting (near 0%%) from one spinning in
    # place (pegged). $QEMU_PID is the timeout(1) wrapper, so sample its
    # CHILD -- run 11 sampled the wrapper itself and printed a solemn
    # column of 0.0%% while the guest was demonstrably executing.
    CPU=$(ps -o %cpu= --ppid "$QEMU_PID" 2>/dev/null | head -1 | tr -d ' ' || true)
    printf '   %4ss  uart0=%-8s (+%-6s) uart1=%-8s qemu-cpu=%s%%\n' \
           "$ELAPSED" "$NOW" "$((NOW - LAST))" "$NOW2" "${CPU:-?}" >&2
    LAST=$NOW
done
wait "$QEMU_PID"
QEMU_RC=$?
set -e

# Everything downstream reads the combination, so a boot that talks to
# whichever port is still judged on what it said.
cat "$CONSOLE" "$CONSOLE2" > "$CONSOLE_ALL" 2>/dev/null || true
# The verdict greps run on an ANSI- and CR-stripped copy. systemd colors
# the unit name in its status lines, so the raw bytes of a success line
# are "Started ESC[0;1;39motp-unit.serviceESC[0m - ..." -- run 12's boot
# started the unit and the verdict still said FAIL because the pattern
# could never straddle the escape sequence.
sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' \
    "$CONSOLE_ALL" > "$CONSOLE_TXT" 2>/dev/null || cp "$CONSOLE_ALL" "$CONSOLE_TXT"
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
# Max over every timestamp in BOTH files, not the concat's last line:
# uart1 ends at the bootconsole handoff (~2s), and with uart1 concatenated
# second, run 12 reported "guest reached 1.9s" while the real console
# stood at 213s.
# `|| true` because a console with no kernel timestamp at all -- qemu
# failed to launch, or run 1's zero-byte symptom -- makes grep exit 1,
# and under pipefail+errexit an unguarded substitution here killed the
# script BEFORE verdict.txt was written: no IMG-CHECK lines, no rc
# report, no console tail, precisely in the no-evidence case. Found by
# the review panel; the identical guard was already on the next line.
LAST_TS=$(grep -oE '\[ *[0-9]+\.[0-9]+\]' "$CONSOLE_TXT" 2>/dev/null | tr -d '[] ' | sort -g | tail -1 || true)
KERNEL_ENTRIES=$(grep -c "Booting Linux on physical CPU" "$CONSOLE_TXT" 2>/dev/null || true)
{
    printf 'qemu exit: %s\n' "$QEMU_RC"
    # How far it got and how many times the kernel STARTED. Entries > 1 is
    # a reboot loop stated as a number, instead of an inference from byte
    # counts that has already been misread once.
    printf 'guest reached: %ss guest time; kernel entered %s time(s)\n' \
           "${LAST_TS:-?}" "${KERNEL_ENTRIES:-0}"
    # NOT a bare grep for "otp-unit" -- the image's hostname IS otp-unit,
    # so `Set hostname to <otp-unit>` and the login prompt both match, and
    # both were proven to green a boot where the unit failed or was absent.
    # And NOT the unit's Description either: systemd colors the unit name,
    # so the raw success line is "Started ESC[..m]otp-unit.serviceESC[0m -
    # OTP pad print unit." and a pattern spanning "Started" into the
    # description can never match -- run 12 started the unit and still
    # reported FAIL through that pattern. "Started otp-unit.service" on
    # the stripped text is what only THIS unit's success line contains
    # (otp-unit-etc-cups.service does not contain it as a substring).
    if grep -qF "Started otp-unit.service" "$CONSOLE_TXT" 2>/dev/null; then
        printf 'IMG-CHECK unit-started PASS\n'
    else
        printf 'IMG-CHECK unit-started FAIL\n'
    fi
    for bad in "status=216" "Failed with result" "Scheduled restart job" \
               "Kernel panic" "Unable to mount root"; do
        if grep -qF -- "$bad" "$CONSOLE_TXT" 2>/dev/null; then
            printf 'IMG-CHECK no-%s FAIL\n' "$(printf '%s' "$bad" | tr ' =' '--')"
        fi
    done
    # "systemd[1]:" and not "systemd". The bare string matches the
    # KERNEL COMMAND LINE, which the kernel prints at boot and which now
    # carries systemd.show_status=1 -- so this check passed on a boot that
    # never reached userspace at all, matching a flag this harness itself
    # added. Only PID 1 writes "systemd[1]:".
    # A boot that succeeded, reset, and looped would still have the
    # success line in its console. Entries != 1 is that loop stated
    # directly, and it fails the run no matter what else passed.
    if [ "${KERNEL_ENTRIES:-0}" = "1" ]; then
        printf 'IMG-CHECK single-kernel-entry PASS\n'
    else
        printf 'IMG-CHECK single-kernel-entry FAIL\n'
    fi
    # THE ENTROPY EVIDENCE -- the only place it is observed on the real
    # artifact rather than injected into a unit test. See issue #16.
    #
    # "hwrng registered" is bcm2835-rng's probe line. The driver is BUILTIN
    # (CONFIG_HW_RANDOM_BCM2835=y), so this line appearing is what proves
    # the SoC's TRNG is the pool's source on the image that gets flashed,
    # as opposed to timing jitter alone -- which is what a dead board or a
    # device-tree regression would silently leave behind. Nothing else in
    # this harness tells those two apart.
    #
    # "crng init done" is the kernel saying the CSPRNG is seeded. Every
    # byte this unit generates goes through getrandom(), which BLOCKS until
    # that line is printed, so its absence is not a warning: it is a unit
    # that cannot make key material and, before issue #16, would have hung
    # silently trying. Measured at ~2.4s guest in the local rig -- see the
    # header -- and present in every green run's console.
    #
    # loglevel=7 keeps both: bcm2835-rng's line is KERN_INFO and the crng
    # line is KERN_NOTICE, and neither is DEBUG.
    for want in "Linux version" "systemd[1]:" "Reached target" \
                "hwrng registered" "crng init done"; do
        if grep -qF -- "$want" "$CONSOLE_TXT" 2>/dev/null; then
            printf 'IMG-CHECK %s PASS\n' "$(printf '%s' "$want" | tr ' ' '-')"
        else
            printf 'IMG-CHECK %s FAIL\n' "$(printf '%s' "$want" | tr ' ' '-')"
        fi
    done
} > "$VERDICT"
cat "$VERDICT"

# qemu's exit code is CONTEXT, not the verdict. A healthy boot of this
# image never exits the emulator -- the unit starts and the guest idles,
# so the only endings are our own early stop (SIGTERM once the success
# line appears) or the backstop timeout. Run 13 booted, started the
# unit, passed every console check, and was failed here on rc=124: the
# exit code of the stopwatch. The console evidence decides; the rc is
# reported so a crash mid-run still shows up in the record.
if [ -n "$EARLY_STOP" ]; then
    echo "qemu stopped by the harness at ${EARLY_STOP}s wall, after the unit start line appeared (rc=$QEMU_RC)" >&2
elif [ "$QEMU_RC" = 124 ]; then
    echo "qemu ran to the ${TIMEOUT}s backstop without the unit start line (rc=124)" >&2
else
    echo "qemu exited rc=$QEMU_RC before the backstop" >&2
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
    echo "--- last 100 console lines (ANSI stripped) ---" >&2
    tail -n 100 "$CONSOLE_TXT" >&2
    exit 1
fi
log "the image boots and the unit starts"

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
# Pi OS first-boot resize was rebooting. It was not -- the reset was the
# bcm2835-pm watchdog, bisected in run 7 and described below -- and the
# reason given here for ruling the resize out was wrong as well, so it is
# restated. It said `-append` carries no `init=`, so
# init=/usr/lib/raspberrypi-sys-mods/firstboot never runs. That token is
# not in this picture at all: pi-gen's arm64 branch ships one cmdline.txt,
# stage1/00-boot-files/files/cmdline.txt, and it reads
#
#   console=serial0,115200 console=tty1 root=ROOTDEV rootfstype=ext4 \
#   fsck.repair=yes rootwait resize
#
# with no init= of any kind; the only `init=` anywhere in the tree is in
# export-noobs/00-release/files/partition_setup.sh, which STRIPS
# init=/usr/lib/raspi-config/init_resize.sh on the NOOBS path this build
# does not take. What actually grows the filesystem is the bare `resize`
# token above plus rpi-resize.service, enabled in
# stage2/01-sys-tweaks/01-run.sh -- and device/install.sh removes the token
# from cmdline.txt and disables the service, because an online resize of a
# read-only overlay lower layer cannot work. On top of that, `-append`
# replaces the command line wholesale here, so the token would not reach
# the kernel even on an unprovisioned image. The conclusion held; the
# reason did not. The flag is back; see the qemu invocation.
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
# THE READ-ONLY OVERLAY, and why this now boots the image TWICE. Issue #9:
# nothing anywhere had ever booted a machine with the overlay engaged. The
# image did not enable it, install.sh printed advice, and the two mechanisms
# tried in the tier-2 Debian guest both failed -- one of them by panicking
# the kernel. device/install.sh enables it at provisioning time now, so the
# artifact this boots has it, and the questions it raises can only be
# answered by a machine that has been powered off and on again:
#
#   boot1  the overlay is engaged. otp-unit-imgcheck.service reports what
#          the root filesystem is, that the unit and cupsd came up on it,
#          and writes a sentinel to / and a setting to /boot/firmware.
#   boot2  the SAME card image, booted again. The sentinel must be gone and
#          the setting must still be there.
#
# The two boots share one image file on purpose: writes that reach the card
# in boot1 are there in boot2, and writes that only reached the overlay's
# tmpfs are not. That difference is the entire claim.
#
# Run 16 (31752321387) is the first one that did it, and it went green on
# the first attempt: 9/9 guest checks in boot1 and 11/11 in boot2, both
# boots reaching ~150s guest time with one kernel entry each, 7m59s of wall
# clock for the pair on top of a 6m58s pi-gen build. Both boots ended
# `qemu exit: 0` -- qemu exits cleanly on the SIGTERM the early stop sends
# -- which is why the "guest reset itself" diagnosis below is conditioned on
# EARLY_STOP being empty as well. Without that it would have fired on two
# perfectly healthy boots.
#
# THE SEEDED userconf.txt PATH, which is issue #20 and rides on the same two
# boots. Run 12 found the built image parked forever on userconf-pi's
# first-boot wizard; #18 fixed that with a ConditionPathExists drop-in rather
# than a mask, precisely so the documented headless credential file keeps
# working -- an operator writes `name:crypted-password` into
# /boot/firmware/userconf.txt, the first boot applies it non-interactively and
# deletes it. Every run since has exercised only the UNSEEDED branch: nothing
# in the boot partition, no wizard, boot green. The other two branches ride
# along now, at the cost of no extra boot:
#
#   boot1  a VALID seed is mcopied into the FAT partition before the boot. It
#          has to be GONE afterwards with no failed_userconf.txt beside it,
#          and the guest has to find the seeded hash in /etc/shadow and
#          userconfig.service finished, successful and holding no job.
#   boot2  the delete boot1's wizard made has stuck -- the FAT partition is
#          outside the overlay -- so the wizard's condition is false, and it
#          has to be SKIPPED while staying enabled rather than masked. The
#          guest then hands the shipped userconf-service a MALFORMED seed by
#          hand with stdin closed: it has to fail fast and leave
#          failed_userconf.txt, not hang.
#
# The malformed seed is deliberately NOT left on the card for the unit to
# find at boot. Stock userconfig.service carries Restart=on-failure, so a
# boot that met one prints "Failed with result" and "Scheduled restart job",
# two of the strings this file hard-fails a release on -- and relaxing a
# release gate to accommodate a fault the harness injected itself is the
# wrong trade. What the drop-in contributes to that path, StandardInput=null,
# is read back off the running machine instead of assumed.
#
# RUN 17 (31968966879) is what the first real execution of all that found,
# and it found the image rather than the harness. Every credential check
# failed the same way in both boots:
#
#   OTP-CHECK boot1 userconf-seeded-boot-ran-no-wizard FAIL
#     condition=no result=success is-active=inactive jobs=1
#   OTP-CHECK boot2 userconf-unseeded-boot-skips-the-wizard FAIL
#     condition=no checked-at='never' is-active=inactive is-enabled=enabled
#   IMG-CHECK boot1 userconf-seed-consumed FAIL still there: userconf.txt
#
# with userconf-seed-planted PASS beside them: the seed reached the card and
# nothing on the machine ever looked at it. `jobs=1` with the condition never
# evaluated is a unit whose START JOB IS STILL QUEUED, and the consoles say
# what it was queued behind -- both of them end on
#
#   Job systemd-networkd-wait-online.service/start running (2min 37s / no limit)
#
# neither boot having reached multi-user.target at all. That wait is
# TimeoutStartSec=infinity on an appliance whose links NetworkManager owns;
# it blocks network-online.target, which blocks cloud-init's
# cloud-config.service, which stock userconfig.service is ordered after. So
# the documented headless credential file was ignored in silence on every
# unit this image would have produced. device/install.sh masks the wait and
# switches cloud-init off, and the probe now reads the mask back and asks
# the queue for its job.
#
# The same run's boot2 answered the one question the malformed-seed
# experiment was uncertain about: whiptail with no TERM FAILS rather than
# blocking. "rc=1 ... said: Entered username is invalid: ... TERM
# environment variable needs set." -- so the fail-fast is real, not a
# stopwatch artifact, and the 60s bound was never reached.
#
# RUN 18 (31972140190) booted the fixed image: multi-user.target reached in
# both boots for the first time in this tier's history, boot2 15/15, and
# boot1 12/13 with the seed applied, consumed, and the front panel still on
# tty1 afterwards. Its one red was the probe rather than the image -- a
# successful apply ends in cancel-rename, which disables userconfig.service,
# and systemd then garbage-collects the inactive unreferenced unit, so
# `systemctl show` answers with pristine defaults for a unit whose own
# console line says "Finished userconfig.service". The guest check reads the
# journal for that now; see harness/img-guest-check.sh.
#
# PARTLY cmdline.txt and config.txt now, where it used to be neither. QEMU
# still reads no firmware configuration -- -append below REPLACES the kernel
# command line wholesale, and dtparam=i2c_arm=on and the disable-wifi/bt
# overlays remain inert here. But the two things the overlay rides on are
# taken out of those files rather than invented: `boot=overlay` is copied
# from the image's cmdline.txt into -append, and the initramfs the firmware
# would auto-load is pulled off the boot partition and handed over with
# -initrd. An image built without the overlay therefore boots without it
# here too, and fails the verdict rather than being quietly compensated for.
#
# WHAT IT DOES NOT CATCH, and this is the important caveat: QEMU's raspi3b
# is a Pi with nothing plugged into it. No OLED on the I2C bus, no buttons
# on the header, no printer. Peripheral coverage is WORSE here than tier
# 1, which simulates all three. This tier answers whether the image comes
# up, whether the unit starts, and whether the root it starts on is the
# read-only overlay that discards -- nothing about hardware -- and should
# be run once per image rather than per commit.
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
ESC=$(printf '\033')
# ONE image file for both boots. Boot 2 is a power-cycle of the same card,
# and that is the only reason "the setting survived and the sentinel did
# not" says anything: a fresh copy of the image would answer both questions
# with the image build's own contents.
PHASES="${OTP_IMG_PHASES:-boot1 boot2}"
# AND NEITHER BOOT IS OPTIONAL. This variable exists so a run debugging the
# boot itself can stop after one, and nothing used to stop it dropping the
# second -- the same list drives the boot loop, the verdict loop and the set
# of guest checks demanded of each phase, so removing boot2 removes the two
# checks tier 3 was built for along with the boot that would have made them.
# Measured against the real verdict block: PHASES="boot1" plus a healthy
# boot1 console exits 0, with root-writes-discarded-by-the-power-cycle and
# settings-survive-the-power-cycle silently absent -- and the release note
# image.yml attaches to the tag says a file written to / in the first boot
# was gone in the second. Halving the CI time must cost a red run, not a
# true-looking sentence in someone's release body.
#
# BOOT1 IS DEMANDED TOO, and it was not, which is the newer half. The seed
# is mcopy'd onto the card unconditionally below -- there is no phase test
# around it, deliberately, because the listing taken before boot1 is the only
# vantage point the credential path can be observed from. So PHASES="boot2"
# was accepted and booted a SEEDED card straight into the unseeded checks:
# userconf-unseeded-boot-skips-the-wizard reads a condition that is TRUE
# because nothing has consumed the seed yet, and the run goes red for a
# reason that has nothing to do with the image. A debugging switch whose
# one-boot setting produces a confusing red is a switch people learn to
# distrust the harness over. Both phases, or say which one is missing.
PHASES_MISSING=""
for want in boot1 boot2; do
    case " $PHASES " in
        *" $want "*) ;;
        *) PHASES_MISSING="$PHASES_MISSING $want" ;;
    esac
done
if [ -n "$PHASES_MISSING" ]; then
    echo "ERROR: OTP_IMG_PHASES='$PHASES' leaves out:$PHASES_MISSING" >&2
    echo "       boot1 is the seeded boot. The credential file is copied" >&2
    echo "       onto the card whatever the phase list says, so a run" >&2
    echo "       without boot1 hands a seeded card to the checks that" >&2
    echo "       expect an unseeded one and fails for the wrong reason." >&2
    echo "       boot2 is the power-cycle, which is what this tier exists" >&2
    echo "       for: the sentinel written to / in boot1 has to be GONE" >&2
    echo "       and the setting written to /boot/firmware has to still be" >&2
    echo "       there, and only a second boot of the same card can say" >&2
    echo "       either. A one-boot run proves neither and must not be" >&2
    echo "       able to pass this gate." >&2
    exit 1
fi

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
    # Read, not merely carried along. -append still replaces the kernel
    # command line wholesale, so the firmware's own use of these files does
    # not happen here -- but the overlay tokens come OUT of cmdline.txt and
    # the initramfs name comes out of config.txt, below. They also ride into
    # the failure artifact, so the divergence between what the device would
    # boot and what this harness booted stays part of the record.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::cmdline.txt "$WORK/boot/" || true
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::config.txt "$WORK/boot/" || true
else
    echo "ERROR: mtools (mcopy) is needed to read the boot partition" >&2
    exit 1
fi

KERNEL="$WORK/boot/kernel8.img"
# A GLOB, not `find ... | head -n 1`. This script runs under pipefail and
# set -e: head closes the pipe the moment it has its line, find dies of
# SIGPIPE, the pipeline returns 141, and a bare assignment at top level then
# ends the script with no verdict and no explanation. It is the shape
# install.sh already carries a comment about, where lsinitramfs | grep -q
# returned 141 for exactly the input that should pass. Unreachable at this
# scale -- mcopy puts one dtb in one flat directory, so find can never
# outrun head -- and fixed rather than reasoned about, because the next
# person to add a file to that directory is not reading this line.
DTB=""
for candidate in "$WORK/boot"/*.dtb; do
    [ -f "$candidate" ] || continue
    DTB="$candidate"
    break
done
if [ ! -f "$KERNEL" ] || [ -z "$DTB" ]; then
    echo "ERROR: no kernel8.img or dtb in the image's boot partition" >&2
    ls -la "$WORK/boot" >&2
    exit 1
fi
log "kernel=$KERNEL dtb=$DTB"

# --- the board revision the firmware would have supplied -----------------
#
# THE FIRMWARE IS WHAT WRITES THIS, and QEMU is not the firmware. On a real
# Pi, start.elf adds a /system node to the device tree carrying
# linux,revision -- the 24-bit board code. The static DTB does not have one:
# measured with `fdtget` against bcm2710-rpi-3-b.dtb out of
# linux-image-6.12.47+rpt-rpi-v8, there is no /system node at all.
#
# WHAT IT COST, in run 72's console, both boots:
#
#   /usr/bin/rpi-eeprom-update: 512: arithmetic expression: expecting ')':
#   "(0x >> 23) & 1"
#   systemd[1]: rpi-eeprom-update.service: Failed with result 'exit-code'.
#
# rpi-eeprom-update reads /sys/firmware/devicetree/base/system/linux,revision,
# then /proc/cpuinfo, then vcgencmd; with none of them BOARD_INFO is empty
# and `0x` is a shell syntax error. On real hardware it reaches
# chipNotSupported() and exits 0, so the unit succeeds on every board
# docs/HARDWARE.md lists. It is the emulator that is wrong, and this is the
# same kind of thing the harness already does for the command line and the
# initramfs: supply what the firmware would have.
#
# MEASURED BEFORE IT WAS BUILT ON, by booting a stock Raspberry Pi OS Lite
# arm64 card under this same `-M raspi3b` with QEMU 8.2.2, twice:
#
#   without the property   linux,revision ABSENT, /proc/cpuinfo has no
#                          Revision line, rpi-eeprom-update rc=2 with the
#                          `(0x >> 23) & 1` message above
#   with it                dt-rev=00a02082, /proc/cpuinfo Revision a02082,
#                          rpi-eeprom-update rc=0: "Device does not a have
#                          a Raspberry Pi bootloader EEPROM (e.g. Pi 4 or
#                          Pi 5). Skipping bootloader update."
#
# 0xa02082 IS THE MACHINE QEMU IS PRETENDING TO BE, decoded field by field
# against the scheme rpi-eeprom-update itself reads: new-style flag set
# (bit 23), memory 1GB (bits 20-22 = 2, and -m 1024 below agrees), Sony UK,
# BCM2837, type 0x08 = 3 Model B, revision 1.2. Handing the guest a code for
# a board `-M raspi3b` does not model would be the DTB mistake from run 1 in
# a smaller font.
#
# THE SAME PROPERTY IS READ BY GPIOZERO, and that is not a side effect to be
# discovered later -- it is measured, in the same pair of boots:
#
#   without   Button(5) raised BadPinFactory: Unable to load any default
#             pin factory
#   with      Button(5) CONSTRUCTED factory='LGPIOFactory' pin=GPIO5
#
# (QEMU does provide /dev/gpiochip0 and /dev/gpiochip1, which is what lgpio
# opens.) So from now on the emulated unit finds BUTTONS -- exactly as every
# real Pi does, which is the whole reason hmi.Interface.prove exists. What it
# still does not find is a SCREEN: there is no /dev/i2c-* and /sys/class/drm
# is empty in both boots, so hmi.open_display raises and returns none. That
# is the half unit-detects-no-panel now keys on; see the comment on that
# check in harness/img-guest-check.sh.
#
# fdtput, NOT a dtc round trip. `dtc -I dtb -O dts` and back rewrites the
# whole blob -- phandles, __symbols__, the overlay fixups -- to change one
# property; fdtput patches the blob in place and resizes it by the 47 bytes
# the node costs. The patched copy is a COPY: the image's own DTB is left
# alone, so what a device would boot and what this booted stay comparable in
# the evidence.
#
# A HARD REQUIREMENT, like mcopy. Booting without the property is booting
# the configuration this exists to fix, and a boot that quietly did that
# would fail on rpi-eeprom-update.service half an hour later and read as a
# regression in the image.
if ! command -v fdtput >/dev/null; then
    echo "ERROR: fdtput (device-tree-compiler) is needed to give the guest" >&2
    echo "       a board revision. Without it rpi-eeprom-update.service" >&2
    echo "       fails on an empty BOARD_INFO and the boot is red for a" >&2
    echo "       fault in the emulator rather than in the image." >&2
    exit 1
fi
BOARD_REVISION=0xa02082
# Beside $WORK/boot rather than in it: that directory is what the *.dtb glob
# above picks the guest's device tree out of, and dropping a second .dtb into
# it is precisely the "next person adds a file to that directory" the comment
# on the glob warns about.
DTB_PATCHED="$WORK/otp-harness-$(basename "$DTB")"
cp "$DTB" "$DTB_PATCHED"
# `-c` on a node that already exists is FDT_ERR_EXISTS and rc 1, not a
# no-op -- measured against fdtput 1.7.0. A firmware DTB that one day ships
# its own empty /system must not fail the run for that, so the create is
# allowed to fail and only the property write is required. The read-back
# below is the gate in both cases.
fdtput -c "$DTB_PATCHED" /system 2>/dev/null || true
if ! fdtput -t x "$DTB_PATCHED" /system linux,revision "$BOARD_REVISION"; then
    echo "ERROR: could not write linux,revision into $DTB_PATCHED" >&2
    exit 1
fi
# READ BACK, because a write that did nothing looks exactly like one that
# worked from here, and the cost of not noticing is a red release gate
# blamed on the image. fdtget prints the property as a decimal integer.
DTB_REVISION=$(fdtget "$DTB_PATCHED" /system linux,revision 2>/dev/null || true)
if [ "${DTB_REVISION:-0}" != "$((BOARD_REVISION))" ]; then
    echo "ERROR: $DTB_PATCHED does not carry linux,revision after the patch" >&2
    echo "       (read back: '${DTB_REVISION:-nothing}', wanted $((BOARD_REVISION)))" >&2
    exit 1
fi
DTB="$DTB_PATCHED"
log "board revision $BOARD_REVISION synthesised into $DTB (the firmware's job)"
IMG_CMDLINE=""
if [ -s "$WORK/boot/cmdline.txt" ]; then
    IMG_CMDLINE=$(tr -d '\r\n' < "$WORK/boot/cmdline.txt")
    log "the image's own cmdline.txt (replaced wholesale by -append): $IMG_CMDLINE"
fi

# --- what the image says about the overlay -------------------------------

# COPIED from the image, never assumed. Adding boot=overlay here when the
# image's own cmdline.txt does not carry it would test a configuration no
# device has -- the harness would be proving its own -append line.
OVERLAY_TOKENS=""
case " $IMG_CMDLINE " in
    *" boot=overlay "*) OVERLAY_TOKENS="boot=overlay" ;;
esac

# QEMU does not run the Pi's firmware, so auto_initramfs=1 does nothing here:
# the initrd has to be pulled off the FAT partition and handed over with
# -initrd, exactly as the kernel and the DTB are. Its name comes from the two
# places the firmware itself would look -- an explicit `initramfs <file>` line
# in config.txt, or the auto_initramfs convention of initramfs8 sitting next
# to kernel8.img.
INITRD_NAME=$(sed -n -E 's/^[[:space:]]*initramfs[[:space:]]+([^[:space:]]+).*/\1/p' \
              "$WORK/boot/config.txt" 2>/dev/null | tail -1)
[ -n "$INITRD_NAME" ] || INITRD_NAME=initramfs8
mcopy -n -i "$IMG@@$BOOT_OFFSET" "::$INITRD_NAME" "$WORK/boot/" || true
INITRD="$WORK/boot/$INITRD_NAME"
if [ -s "$INITRD" ]; then
    log "initramfs=$INITRD ($(wc -c < "$INITRD") bytes) overlay tokens='${OVERLAY_TOKENS:-none}'"
else
    # NOT fatal, and deliberately so. Booting anyway is the more useful
    # failure: the guest comes up on a plain read-write root, says so, and
    # the verdict names both missing halves. Aborting here would produce no
    # verdict.txt at all, which is the state the evidence pipeline is worst
    # at explaining.
    log "no $INITRD_NAME on the boot partition: booting WITHOUT an initramfs"
    INITRD=""
fi

# --- the seeded credential file, planted before the first boot -----------

# THE ACCOUNT IN THE SEED IS THE IMAGE'S OWN FIRST USER, and that is a
# deliberate narrowing. /usr/lib/userconf-pi/userconf renames the UID-1000
# account whenever the seed names a different one -- `usermod -l`, a home
# directory move, sed over /etc/subuid, /etc/subgid and the sudoers drop-in
# -- and none of that is what this is here to observe. image/build.sh builds
# with FIRST_USER_NAME='otp', so naming otp here skips the rename branch and
# leaves exactly the part under test: the decision not to prompt, the
# `chpasswd -e`, and the delete. tests/test_img_verdict.py holds this name
# against image/build.sh so the two cannot drift.
USERCONF_USER="otp"
# sha512-crypt, generated with
#
#   openssl passwd -6 -salt otpimgcheck 'otp-imgcheck-not-a-credential'
#
# and verified byte-for-byte against glibc crypt(3) via Python's crypt
# module. NOT a secret and it reaches no shipped image: this is written into
# $WORK/card.img, the decompressed working copy this harness boots, and
# image/deploy/*.img.xz is never opened for writing. The SALT is the marker
# the guest looks for in /etc/shadow -- pi-gen hashes FIRST_USER_PASS with a
# random salt, so a shadow entry beginning $6$otpimgcheck$ can only be the
# line planted here.
# shellcheck disable=SC2016  # the $ signs are crypt(3) field separators
USERCONF_HASH='$6$otpimgcheck$xPbFgUo86.IHXj9q2xsofEuqNQoZjMUYoMw/E51qX6xuUT1zEPNF7hilpqHDnUoQ2F9YOlvzIbPyQOwpAHGPm0'
log "Seeding ::userconf.txt for $USERCONF_USER (the headless credential path, issue #20)"
printf '%s:%s\n' "$USERCONF_USER" "$USERCONF_HASH" > "$WORK/userconf.txt"
# NOT fatal if it fails, for the reason the initramfs copy is not: a run that
# aborts here produces no verdict at all, and "the seed never reached the
# card" is a thing the verdict can say precisely, out of the listing taken
# below.
mcopy -o -n -i "$IMG@@$BOOT_OFFSET" "$WORK/userconf.txt" ::userconf.txt || true

# The FAT root as the host sees it, listed either side of every boot. This is
# the only vantage point from which the credential path can be observed at
# all: the file is deleted by the machine that consumes it, so afterwards a
# seed that was applied and a seed that was never written look identical, and
# only the before-listing tells them apart.
fat_listing() {
    mdir -b -i "$IMG@@$BOOT_OFFSET" :: > "$1" 2>/dev/null || : > "$1"
}

# root=/dev/mmcblk0p2 rather than cmdline.txt's root=PARTUUID=..., but not
# for the reason previously written here. The old comment claimed QEMU does
# not reproduce PARTUUIDs -- false. A PARTUUID is the MBR disk identifier
# plus a partition number, both of which ship inside the image file, so it
# resolves under QEMU exactly as on the device. The real reason is that
# cmdline.txt is replaced wholesale, so root= must be restated -- and an
# explicit device name beats depending on which revision of cmdline.txt
# this build happened to produce. What it says cmdline.txt "also carries"
# was wrong twice over and is gone: pi-gen's arm64 stage1 cmdline.txt has
# no init= at all (see the header) and no `quiet` either, and the token it
# does carry, a bare `resize`, is one device/install.sh takes back out.
# One boot, into its own directory. Everything a phase produces -- both
# consoles, qemu's exit code, whether the harness stopped it -- is written
# down there, so the verdict below is a function of files on disk rather
# than of variables that happen to still be set. That is also what lets
# tests/test_img_verdict.py run the real verdict block over synthetic
# consoles instead of over a copy of it.
boot_phase() {
    local phase="$1"
    local dir="$WORK/$phase"
    rm -rf "$dir"
    mkdir -p "$dir"
    fat_listing "$dir/boot-files-before.txt"
    # BOTH UARTs, captured separately. A Pi 3 has two, and the measured QEMU
    # mapping is: FIRST -serial = the PL011 (which this DTB names ttyAMA1),
    # SECOND -serial = the mini-UART, where the earlycon bootconsole lives.
    # For six runs the first file's stubborn 0 bytes was itself the clue
    # nobody read: the PL011 never emitted because no console= ever named
    # it. Capturing both is what eventually made the mapping provable.
    local console="$dir/console.log"
    local console2="$dir/console-uart1.log"
    : > "$console"
    : > "$console2"
    log "Booting $phase under -M raspi3b (emulated, not KVM -- allow minutes)"
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
#
# systemd.journald.forward_to_console=1 IS AN OBSERVABILITY LEVER, AND
# EMULATION-ONLY. Issue #21: tier 3 could see that the unit STARTED and
# nothing it did afterwards. The journal is volatile by design
# (device/install.sh writes Storage=volatile, RuntimeMaxUse=16M), it dies
# with the guest, and nothing carried it to the serial port this harness
# captures -- so every line otp-unit.service logs about the hardware it
# found, and every line any other unit logs, was written to RAM and thrown
# away. With this the journal streams to /dev/console, which is the PL011
# named above, which is uart0. It changes nothing about a flashed device:
# -append REPLACES the kernel command line wholesale here (see the header),
# the image's own cmdline.txt is not edited, and the unit keeps its quiet
# console on real hardware. The flag is not assumed to have worked either
# -- harness/img-guest-check.sh puts a marker into the journal and NOWHERE
# else, and the verdict below requires it on the console.
#
# otp.imgcheck=<phase> is what wakes otp-unit-imgcheck.service, whose
# ConditionKernelCommandLine is that word. It ships in the image and is
# inert on a flashed card, where nothing puts the word there.
#
# $OVERLAY_TOKENS is boot=overlay, copied out of the image's own
# cmdline.txt above, or empty when the image does not carry it.
    timeout -k 30 "$TIMEOUT" qemu-system-aarch64 \
        -M raspi3b -m 1024 \
        -kernel "$KERNEL" -dtb "$DTB" \
        ${INITRD:+-initrd "$INITRD"} \
        -append "rw earlycon loglevel=7 console=ttyAMA1,115200 systemd.show_status=1 systemd.journald.forward_to_console=1 initcall_blacklist=bcm2835_pm_driver_init root=/dev/mmcblk0p2 rootfstype=ext4 rootwait $OVERLAY_TOKENS otp.imgcheck=$phase" \
        -drive "file=$IMG,if=sd,format=raw" \
        -serial "file:$console" \
        -serial "file:$console2" \
        -display none -no-reboot &
    local qemu_pid=$!

    # Sample the console while it boots. A guest grinding slowly along and a
    # guest wedged solid look identical from outside once the output stops,
    # and telling them apart cost a 55-minute build and still ended in a
    # guess. The growth column answers it directly: still climbing means
    # slow, flat for minutes means stuck.
    local sample=30 elapsed=0 last=0 early_stop='' now now2 cpu seen
    while kill -0 "$qemu_pid" 2>/dev/null; do
        sleep "$sample"
        elapsed=$((elapsed + sample))
        now=$(wc -c < "$console" 2>/dev/null || echo 0)
        now2=$(wc -c < "$console2" 2>/dev/null || echo 0)
        # A healthy boot of this image never ends by itself: the unit comes
        # up and the guest idles. Waiting out the full cap after the phase
        # has already finished adds minutes and, worse, puts the verdict in
        # a race with the cap -- run 13's success line landed around 420s of
        # a 480s cap.
        #
        # The marker is the GUEST CHECK's own done line, not the unit's
        # start line it used to be. The overlay checks run after the unit is
        # up, so stopping at "Started otp-unit.service" would now cut the
        # emulator off before the thing this tier exists to observe had said
        # anything. A boot where the guest check never runs pays the
        # backstop, which is the correct price for having no report.
        #
        # grep -c, not grep -q, and this script runs under pipefail. `-q`
        # closes the pipe on its first match, so sed dies of SIGPIPE and the
        # pipeline returns 141 -- measured at 141 against a producer still
        # writing when grep left. With `-q` the early stop simply never
        # fires on a console large enough to lose that race, and every boot
        # pays the full backstop instead. `-c` reads to the end.
        seen=$(sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' "$console" 2>/dev/null \
               | grep -cF "OTP-GUEST-DONE $phase" || true)
        if [ -z "$early_stop" ] && [ "${seen:-0}" != "0" ]; then
            early_stop=$elapsed
            printf '   %s reported done at %ss wall; draining 10s, then stopping qemu\n' \
                   "$phase" "$elapsed" >&2
            sleep 10
            kill "$qemu_pid" 2>/dev/null || true
        fi
        # Host-side CPU%% of the emulator, because flat output alone cannot
        # distinguish a guest idle-waiting (near 0%%) from one spinning in
        # place (pegged). $qemu_pid is the timeout(1) wrapper, so sample its
        # CHILD -- run 11 sampled the wrapper itself and printed a solemn
        # column of 0.0%% while the guest was demonstrably executing.
        cpu=$(ps -o %cpu= --ppid "$qemu_pid" 2>/dev/null | head -1 | tr -d ' ' || true)
        printf '   %-5s %4ss  uart0=%-8s (+%-6s) uart1=%-8s qemu-cpu=%s%%\n' \
               "$phase" "$elapsed" "$now" "$((now - last))" "$now2" "${cpu:-?}" >&2
        last=$now
    done
    wait "$qemu_pid"
    local rc=$?
    set -e
    printf '%s\n' "$rc" > "$dir/qemu-rc"
    printf '%s' "$early_stop" > "$dir/early-stop"
    log "$phase: uart0=$(wc -c < "$console") bytes  uart1=$(wc -c < "$console2") bytes  rc=$rc"
    # A power-cycle, not a reset button: the emulator is gone and the image
    # file is flushed before the next phase opens it again.
    sync 2>/dev/null || true
    # AFTER the sync, so what is listed is what reached the card rather than
    # what was still in a page cache when qemu was stopped.
    fat_listing "$dir/boot-files-after.txt"
}

for phase in $PHASES; do
    boot_phase "$phase"
done

# --- the verdict --------------------------------------------------------

# Everything downstream reads the combination, so a boot that talks to
# whichever port is still judged on what it said.
VERDICT="$WORK/verdict.txt"

# The evidence one phase left behind, named. Nothing below reads a variable
# the boot happened to leave set: a phase is a directory, and the verdict is
# a function of what is in it. That is also what lets
# tests/test_img_verdict.py drive this block over synthetic consoles.
phase_paths() {
    local phase="$1"
    CONSOLE="$WORK/$phase/console.log"
    CONSOLE2="$WORK/$phase/console-uart1.log"
    CONSOLE_ALL="$WORK/$phase/console-all.log"
    CONSOLE_TXT="$WORK/$phase/console-text.log"
    QEMU_RC=$(cat "$WORK/$phase/qemu-rc" 2>/dev/null || echo "?")
    EARLY_STOP=$(cat "$WORK/$phase/early-stop" 2>/dev/null || true)
    cat "$CONSOLE" "$CONSOLE2" > "$CONSOLE_ALL" 2>/dev/null || true
    # The verdict greps run on an ANSI- and CR-stripped copy. systemd colors
    # the unit name in its status lines, so the raw bytes of a success line
    # are "Started ESC[0;1;39motp-unit.serviceESC[0m - ..." -- run 12's boot
    # started the unit and the verdict still said FAIL because the pattern
    # could never straddle the escape sequence. The guest check's own lines
    # arrive over the same serial console and carry the same CRs.
    sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' \
        "$CONSOLE_ALL" > "$CONSOLE_TXT" 2>/dev/null || cp "$CONSOLE_ALL" "$CONSOLE_TXT"
}

# The lines the SYSTEM wrote, as opposed to the lines it carried for
# somebody else.
#
# journald formats a forwarded line as a monotonic timestamp, the speaker and
# its pid, then the text:
#
#   [   45.123456] python3[412]: no OLED (...)
#
# The kernel's own output has the timestamp and no speaker at all, and PID 1
# writes its status lines with no timestamp ("[  OK  ] Started ..."). Neither
# shape matches, so both are kept; systemd[1] is kept by name, because it is
# the one speaker whose "Failed with result" is systemd's verdict on a unit
# rather than a phrase inside somebody's log output.
#
# `|| true` for the same reason every other read of a console here carries
# one: an absent or empty file must produce an empty result, not kill the
# verdict before it has been written.
system_lines() {
    awk '!/^\[[^]]*\] [^ ]+\[[0-9]+\]:/ || /^\[[^]]*\] systemd\[1\]:/' \
        "$1" 2>/dev/null || true
}

per_boot_verdict() {
    local phase="$1"
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
    local LAST_TS KERNEL_ENTRIES SPOKEN
    LAST_TS=$(grep -oE '\[ *[0-9]+\.[0-9]+\]' "$CONSOLE_TXT" 2>/dev/null | tr -d '[] ' | sort -g | tail -1 || true)
    # `kernel: ` EXCLUDED, and that is a consequence of the journal now
    # streaming to this console. journald labels a forwarded kernel message
    # with the identifier `kernel`, and the kernel's own console output never
    # prefixes itself that way -- so if journald forwards the kmsg entries it
    # imported (it reads /dev/kmsg from the start of the buffer), the whole
    # early boot arrives a second time and a healthy boot counts TWO kernel
    # entries and is failed as a reboot loop.
    #
    # MEASURED NOW, and the answer is no. Run 72 is the first console this
    # repository has captured with forwarding actually on, and it carries
    # ZERO lines matching `kernel: ` in either boot: journald does not
    # re-forward the kmsg entries it imported. "Booting Linux on physical
    # CPU" appears exactly once in a 1070-line console and both boots report
    # `kernel entered 1 time(s)`, so the doubling this exclusion was written
    # against does not happen and never did.
    #
    # KEPT ANYWAY. It costs one grep, it is the documented behaviour of a
    # setting that could change with a systemd release, and it cannot hide a
    # real loop -- a second kernel entry prints its own "Booting Linux on
    # physical CPU" with a timestamp and no identifier, which is what is
    # still counted. What has changed is that this is now a guard against a
    # regression rather than a guess about an unknown.
    KERNEL_ENTRIES=$(grep -vE '^\[[^]]*\] kernel: ' "$CONSOLE_TXT" 2>/dev/null \
                     | grep -c "Booting Linux on physical CPU" || true)
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
        printf 'IMG-CHECK %s unit-started PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s unit-started FAIL\n' "$phase"
    fi
    # THE JOURNAL REACHED THE CONSOLE, which is issue #21's first clause and
    # the thing every behavioural reading below now rests on. -append carries
    # systemd.journald.forward_to_console=1; a kernel parameter that is
    # accepted and does nothing looks exactly like one that works, so this is
    # not taken on trust. harness/img-guest-check.sh writes a marker into the
    # journal with systemd-cat and NOWHERE else -- deliberately not to its own
    # stdout, which systemd already copies to this console through
    # StandardOutput=journal+console, and deliberately never echoed back into
    # a check's detail either. Forwarding is the only thing that can have put
    # it here.
    #
    # The guest's journal-marker-accepted is the other half of the pair: it
    # says the journal TOOK the marker, so an absence here is a forwarding
    # failure rather than a systemd-cat that did nothing.
    if grep -qE "OTP-JOURNAL-FORWARDED[[:space:]]+${phase}([[:space:]]|\$)" \
            "$CONSOLE_TXT" 2>/dev/null; then
        printf 'IMG-CHECK %s journal-forwarded-to-console PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s journal-forwarded-to-console FAIL\n' "$phase"
    fi
    # THE FORBIDDEN PHRASES, AND WHO IS ALLOWED TO SAY THEM.
    #
    # Until the journal was forwarded, this console carried kernel output and
    # PID 1's status lines and nothing else: once journald started, every
    # unit's stdout and stderr went into the journal and stayed there. Now it
    # carries whatever anything on the machine writes -- including this
    # harness's own probe, which quotes userconf-service's output into a check
    # detail and dumps thirty lines of the unit's journal at the end. Failing
    # a release because a unit QUOTED "Failed with result" is the same defect
    # as failing one because the image's hostname contains "otp-unit", which
    # is row 2 of issue #14.
    #
    # So the phrases are looked for in what the SYSTEM said. system_lines()
    # keeps kernel output and PID 1, and drops journald-forwarded lines from
    # anything else; a phrase found only in one of those is reported instead
    # of gated, so the scoping can never swallow one in silence.
    #
    # The note quotes the line, and the quote can itself trip the `grep -q
    # FAIL` over verdict.txt at the bottom of this file if what it repeats
    # contains that word. That is the safe direction -- a run that stops to be
    # read -- and the note sitting next to the red says exactly why.
    #
    # FALLS BACK TO THE WHOLE CONSOLE if the filtered copy cannot be written.
    # A failed redirect under errexit would otherwise kill the script here,
    # mid-verdict, with the file half written -- the no-evidence failure this
    # block already carries two `|| true`s for. Guarding it into an unwritten
    # $SPOKEN would be worse than either: every phrase would then be looked
    # for in a file that does not exist and none would ever be found. Wider
    # is the safe direction for a gate.
    SPOKEN="$WORK/$phase/console-system.log"
    if ! system_lines "$CONSOLE_TXT" > "$SPOKEN" 2>/dev/null; then
        SPOKEN="$CONSOLE_TXT"
    fi
    for bad in "status=216" "Failed with result" "Scheduled restart job" \
               "Kernel panic" "Unable to mount root"; do
        if grep -qF -- "$bad" "$SPOKEN" 2>/dev/null; then
            printf 'IMG-CHECK %s no-%s FAIL\n' "$phase" "$(printf '%s' "$bad" | tr ' =' '--')"
            # WHICH LINES, indented under the red, the way guest-no-fail-lines
            # already dumps its matches. Without this the verdict said only
            # that a phrase occurred somewhere in a 1000-line console, and
            # answering "which unit?" meant downloading the evidence artifact
            # and grepping it by hand -- which is exactly what run 72 cost.
            # The three units it would have named are the whole finding.
            #
            # Bounded at 10 lines and 200 columns because a reboot loop
            # repeats its phrase for as long as it loops, and a verdict file
            # that scrolls the real checks off the top of the job log is a
            # worse report than one that says nothing.
            #
            # Quoting into the verdict can trip the `grep -q FAIL` over
            # verdict.txt at the bottom of this file, and here that costs
            # nothing that is not already spent: this branch has JUST written
            # FAIL on the line above it, so the phase is red either way.
            grep -F -- "$bad" "$SPOKEN" 2>/dev/null \
                | head -10 | cut -c1-200 | sed 's/^/    /' || true
        elif grep -qF -- "$bad" "$CONSOLE_TXT" 2>/dev/null; then
            printf 'IMG-NOTE %s quoted-%s: %s\n' "$phase" \
                   "$(printf '%s' "$bad" | tr ' =' '--')" \
                   "$(grep -m1 -F -- "$bad" "$CONSOLE_TXT" 2>/dev/null | cut -c1-120)"
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
        printf 'IMG-CHECK %s single-kernel-entry PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s single-kernel-entry FAIL\n' "$phase"
    fi
    # THE ENTROPY EVIDENCE -- the only place it is observed on the real
    # artifact rather than injected into a unit test. See issue #16.
    #
    # "crng init done" is the kernel saying the CSPRNG is seeded. Every
    # byte this unit generates goes through getrandom(), which BLOCKS until
    # that line is printed, so its absence is not a warning: it is a unit
    # that cannot make key material and, before issue #16, would have hung
    # silently trying. Measured at ~2.4s guest in the local rig (see the
    # header), and the wording is confirmed verbatim against a running
    # kernel: "[    0.193894] random: crng init done". A hard gate.
    #
    # "hwrng registered" is the hw_random core accepting bcm2835-rng, which
    # is builtin (CONFIG_HW_RANDOM_BCM2835=y). It is what separates "the
    # SoC's ring oscillator seeded the pool" from "interrupt timing did" --
    # the difference between a one-time pad and a very good stream cipher,
    # and a distinction nothing else in this harness can draw.
    #
    # A GATE, but only since it was read. It shipped for one commit as a
    # report instead, because the wording was a guess and no console in
    # this repository carried it: hard-failing a release on an unread
    # string is the defect issue #14 catalogues -- its list already has
    # "tier 3 verdict grepped for otp-unit, which is the image's HOSTNAME"
    # -- with the sign flipped, blocking a boot that was fine. Run
    # 31693881773 then booted the built image and printed it verbatim:
    #
    #   IMG-NOTE hwrng-line-present: g 3f104000.rng: hwrng registered
    #
    # ("g " is the tail of "bcm2835-rng", clipped by the note's own quote
    # window.) Now it is evidence, so now it gates.
    #
    # loglevel=7 keeps both: the crng line is KERN_NOTICE and the hwrng
    # line is KERN_INFO, and neither is DEBUG.
    #
    # AND multi-user.target BY NAME, which is the second string in this list
    # to arrive by the report-then-gate route. "Reached target" on its own is
    # satisfied by remote-fs.target at 30 seconds: run 31968966879 reached
    # fourteen targets, passed that clause in both boots, and never finished
    # either -- both consoles ended on
    # `systemd-networkd-wait-online.service/start running (2min 37s / no
    # limit)`, with the credential wizard's job still queued behind it. The
    # only thing that says the boot FINISHED is multi-user.target, and it was
    # carried as a note (below) for exactly one run because nothing in this
    # repository had printed it yet. Run 31972140190 then printed it in BOTH
    # boots -- see the RUN 18 paragraph in this file's header. It is evidence
    # now, so it gates now, which is the same rule the hwrng line above was
    # promoted under.
    #
    # The margin is real rather than assumed: the sampler stops the emulator
    # ten seconds after OTP-GUEST-DONE, and the probe that prints that line
    # is a Type=oneshot WantedBy=multi-user.target -- so the target is
    # reached in the instant after the probe exits, well inside the drain.
    for want in "Linux version" "systemd[1]:" "Reached target" \
                "Reached target multi-user.target" \
                "crng init done" "hwrng registered"; do
        if grep -qF -- "$want" "$CONSOLE_TXT" 2>/dev/null; then
            printf 'IMG-CHECK %s %s PASS\n' "$phase" "$(printf '%s' "$want" | tr ' ' '-')"
        else
            printf 'IMG-CHECK %s %s FAIL\n' "$phase" "$(printf '%s' "$want" | tr ' ' '-')"
        fi
    done
    # Quoted back as well as gated, so the next kernel bump shows WHICH
    # driver registered rather than only that something did. Widened from
    # 16 characters of leading context to 40: at 16 the run above clipped
    # "bcm2835-rng" down to "g ", losing the one detail worth reading.
    if grep -qiE "hwrng|hw_random|rng_core" "$CONSOLE_TXT" 2>/dev/null; then
        printf 'IMG-NOTE hwrng-line: %s\n' \
            "$(grep -ioE '.{0,40}(hwrng|hw_random|rng_core).{0,44}' \
                 "$CONSOLE_TXT" 2>/dev/null | head -1)"
    fi
    # WHICH targets, and not merely that one was reached. This began as the
    # evidence multi-user.target needed before it could gate, and it printed
    # that evidence in run 31972140190; the gate is in the list above now.
    #
    # IT STAYS, as a note, because the gate and the note answer different
    # questions. The gate says whether the ONE target that means "the boot
    # finished" was reached. The note says which fourteen were and which one
    # was not, which is the only thing that made run 31968966879 legible at
    # all -- a red gate on its own says a boot did not finish and nothing
    # about where it stopped. It is also what the NEXT promotion will be
    # argued from, whatever target that turns out to be.
    printf 'IMG-NOTE %s targets-reached: %s\n' "$phase" \
        "$(grep -ohE 'Reached target [a-zA-Z0-9@:._-]+' "$CONSOLE_TXT" 2>/dev/null \
             | sort -u | tr '\n' ' ')"
}

# --- what the guest said about the overlay -------------------------------

# The checks tier 3 CLAIMS the guest made, named. A gate that only requires
# the ABSENCE of FAIL lines is satisfied by a guest that ran no checks at all
# -- that is row 4 of issue #14 arriving somewhere new, and it is why the
# names are listed here as well as counted. A check deleted from
# harness/img-guest-check.sh now fails the run instead of quietly shrinking
# what this tier proves; tests/test_img_verdict.py holds the two lists
# against each other so they cannot drift apart in the other direction.
GUEST_CHECKS_COMMON="root-is-overlay root-write-lands-and-is-readable \
unit-active unit-not-restart-looping journal-marker-accepted \
unit-detects-no-panel etc-cups-is-its-own-tmpfs cups-running \
cupsd-config-valid boot-partition-separate setting-saved-to-the-boot-partition \
network-wait-cannot-hold-the-boot-open \
machine-id-persisted-outside-the-overlay"
GUEST_CHECKS_BOOT1="userconf-seed-applied userconf-seeded-boot-ran-no-wizard \
front-panel-survives-the-credential-apply diagnostic-sheet-renders \
diagnostic-sheet-reaches-cups ssh-host-key-fingerprint-recorded"
GUEST_CHECKS_BOOT2="root-writes-discarded-by-the-power-cycle \
settings-survive-the-power-cycle userconf-unseeded-boot-skips-the-wizard \
userconf-wizard-cannot-prompt userconf-malformed-seed-fails-fast \
ssh-host-keys-identical-across-the-power-cycle"

guest_gate() {
    local phase="$1" count counts passed total missing want name

    # Exactly once, not at least once. A guest that reported twice is one
    # that rebooted underneath the harness, and "it reported" stays true of
    # a machine doing nothing else -- the same trap tier 2's gate is built
    # around.
    count=$(grep -cE "OTP-RESULT[[:space:]]+${phase}[[:space:]]" "$CONSOLE_TXT" 2>/dev/null || true)
    if [ "${count:-0}" = "1" ]; then
        printf 'IMG-CHECK %s guest-reported-once PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s guest-reported-once FAIL %s OTP-RESULT lines\n' \
               "$phase" "${count:-0}"
    fi

    # OTP-GUEST-DONE is the only evidence the phase RAN OUT rather than
    # being killed part way through, and this tier kills the emulator by
    # hand the moment it sees that line. Counts printed without it are a
    # guest that died mid-phase, whose numbers agree with themselves right
    # up to the axe.
    count=$(grep -cE "OTP-GUEST-DONE[[:space:]]+$phase([[:space:]]|\$)" "$CONSOLE_TXT" 2>/dev/null || true)
    if [ "${count:-0}" = "1" ]; then
        printf 'IMG-CHECK %s guest-finished PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s guest-finished FAIL %s OTP-GUEST-DONE lines\n' \
               "$phase" "${count:-0}"
    fi

    counts=$(grep -oE "OTP-RESULT[[:space:]]+${phase}[[:space:]]+[0-9]+/[0-9]+" \
             "$CONSOLE_TXT" 2>/dev/null | tail -1 || true)
    passed=${counts##* }
    total=${passed##*/}
    passed=${passed%%/*}
    if [ -n "$counts" ] && [ "${passed:-0}" = "${total:-1}" ] && [ "${total:-0}" -gt 0 ] 2>/dev/null; then
        printf 'IMG-CHECK %s guest-checks-all-passed PASS %s/%s\n' "$phase" "$passed" "$total"
    else
        printf 'IMG-CHECK %s guest-checks-all-passed FAIL %s\n' \
               "$phase" "${counts:-no OTP-RESULT line}"
    fi

    # Belt and braces over the counts, which come from the guest -- the
    # thing under test. A guest that miscounts must not be able to talk its
    # way to a green run, so one FAIL line fails the phase on its own.
    count=$(grep -cE "OTP-CHECK[[:space:]]+${phase}[[:space:]].*[[:space:]]FAIL([[:space:]]|\$)" \
            "$CONSOLE_TXT" 2>/dev/null || true)
    if [ "${count:-1}" = "0" ]; then
        printf 'IMG-CHECK %s guest-no-fail-lines PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s guest-no-fail-lines FAIL %s\n' "$phase" "${count:-?}"
        grep -E "OTP-CHECK[[:space:]]+${phase}[[:space:]].*[[:space:]]FAIL([[:space:]]|\$)" \
             "$CONSOLE_TXT" 2>/dev/null | sed 's/^/  /' || true
    fi

    missing=""
    want="$GUEST_CHECKS_COMMON"
    # Per phase, because the two boots do not ask the same questions: only
    # boot1 has a seed to consume, and only boot2 can speak about what
    # survived the power-cycle. A phase-blind list would demand boot2's
    # checks of boot1 and fail every run.
    case "$phase" in
        boot1) want="$want $GUEST_CHECKS_BOOT1" ;;
        boot2) want="$want $GUEST_CHECKS_BOOT2" ;;
    esac
    for name in $want; do
        grep -qE "OTP-CHECK[[:space:]]+${phase}[[:space:]]+${name}[[:space:]]+PASS" \
             "$CONSOLE_TXT" 2>/dev/null || missing="$missing $name"
    done
    if [ -z "$missing" ]; then
        printf 'IMG-CHECK %s guest-ran-every-named-check PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s guest-ran-every-named-check FAIL missing:%s\n' \
               "$phase" "$missing"
    fi
}

# --- the credential file, watched from outside the guest -----------------

# `mdir -b` prints one absolute path per line, "::/kernel8.img". Matched
# whole-line and case-insensitively: FAT stores an 8.3 name in upper case and
# the lower-case flag in a byte the tools do not all honour the same way, so
# a case-sensitive match here would be a property of whichever tool wrote the
# file rather than of whether the file is there.
fat_lists() {
    grep -qixF -- "::/$2" "$1" 2>/dev/null
}

userconf_gate() {
    local phase="$1"
    local before="$WORK/$phase/boot-files-before.txt"
    local after="$WORK/$phase/boot-files-after.txt"

    # THE POSITIVE CONTROL for every absence below, in the same fixture and
    # from the same command. "userconf.txt is gone" is just as true of a
    # partition nothing could read -- a bad offset, an mdir that failed, an
    # empty file -- and that is the shape of failure this repository keeps
    # being bitten by. kernel8.img is on the boot partition of every image
    # this harness can boot at all: it is the file it hands to qemu.
    if fat_lists "$after" kernel8.img; then
        printf 'IMG-CHECK %s boot-partition-listed PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s boot-partition-listed FAIL %s lines, no kernel8.img\n' \
               "$phase" "$(wc -l < "$after" 2>/dev/null || echo 0)"
    fi

    case "$phase" in
        boot1)
            # The seed reached the card. Without this the check below is
            # satisfied by a `mcopy` that silently did nothing, which is
            # exactly how the first version of this harness read a wrong
            # partition offset as a bad image.
            if fat_lists "$before" userconf.txt; then
                printf 'IMG-CHECK %s userconf-seed-planted PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s userconf-seed-planted FAIL not in the boot partition before the boot\n' \
                       "$phase"
            fi
            # CONSUMED means both halves. userconf.txt gone says the file was
            # taken; failed_userconf.txt absent says it was taken and APPLIED
            # rather than rejected as malformed -- the same two states an
            # operator's own card can end a first boot in, and the difference
            # between working credentials and none.
            if ! fat_lists "$after" userconf.txt && ! fat_lists "$after" failed_userconf.txt; then
                printf 'IMG-CHECK %s userconf-seed-consumed PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s userconf-seed-consumed FAIL still there:%s%s\n' "$phase" \
                       "$(fat_lists "$after" userconf.txt && printf ' userconf.txt')" \
                       "$(fat_lists "$after" failed_userconf.txt && printf ' failed_userconf.txt')"
            fi
            ;;
        boot2)
            # The other end of the guest's malformed-seed experiment, from
            # the host: a bad seed has to end up quarantined under its failed_
            # name and must not be left sitting where the next boot's wizard
            # would find it. This is the artefact the operator is told to look
            # for, read off the card rather than off the console.
            if fat_lists "$after" failed_userconf.txt && ! fat_lists "$after" userconf.txt; then
                printf 'IMG-CHECK %s userconf-malformed-seed-quarantined PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s userconf-malformed-seed-quarantined FAIL failed_userconf.txt:%s userconf.txt:%s\n' \
                       "$phase" \
                       "$(fat_lists "$after" failed_userconf.txt && printf yes || printf no)" \
                       "$(fat_lists "$after" userconf.txt && printf yes || printf no)"
            fi
            ;;
    esac
}

: > "$VERDICT"
for phase in $PHASES; do
    phase_paths "$phase"
    if [ ! -s "$CONSOLE_ALL" ]; then
        # Worth saying outright rather than leaving as a wall of FAILs. An
        # empty console is not "the unit did not start" -- it is "there is
        # no evidence the kernel ever ran", and the two have entirely
        # different causes.
        log "$phase: NEITHER uart produced any output: no evidence the kernel ran"
    fi
    {
        printf -- '--- %s ---\n' "$phase"
        per_boot_verdict "$phase"
        guest_gate "$phase"
        userconf_gate "$phase"
    } >> "$VERDICT"
    # qemu's exit code is CONTEXT, not the verdict. A healthy boot of this
    # image never exits the emulator -- the unit starts, the guest check
    # reports, and the guest idles -- so the only endings are our own early
    # stop (SIGTERM once the done line appears) or the backstop timeout. Run
    # 13 booted, started the unit, passed every console check, and was
    # failed on rc=124: the exit code of the stopwatch. The console evidence
    # decides; the rc is reported so a crash mid-run still shows up.
    if [ -n "$EARLY_STOP" ]; then
        echo "$phase: qemu stopped by the harness at ${EARLY_STOP}s wall, after the guest reported done (rc=$QEMU_RC)" >&2
    elif [ "$QEMU_RC" = 124 ]; then
        # "without a guest report" only if there is no report. rc=124 with
        # an empty early-stop means the sampler never saw the done line
        # while the boot was running -- which is not the same as the guest
        # never printing one: the sampler looks every 30 seconds and at the
        # backstop it has stopped looking, so a done line in the last
        # sampling window lands in the console and not in early-stop. The
        # console is on disk by now and can simply be asked.
        if grep -qF "OTP-GUEST-DONE $phase" "$CONSOLE_TXT" 2>/dev/null; then
            echo "$phase: the guest DID report done, but not in time for the sampler to stop qemu: ran to the ${TIMEOUT}s backstop (rc=124)" >&2
        else
            echo "$phase: qemu ran to the ${TIMEOUT}s backstop without a guest report (rc=124)" >&2
        fi
    else
        echo "$phase: qemu exited rc=$QEMU_RC before the backstop" >&2
    fi
done
cat "$VERDICT"

if grep -q "FAIL" "$VERDICT"; then
    echo "the image did not boot cleanly to a started unit on a read-only overlay" >&2
    for phase in $PHASES; do
        phase_paths "$phase"
        if [ "$QEMU_RC" = 0 ] && [ -z "$EARLY_STOP" ]; then
            # Exit 0 before the timeout + -no-reboot = the GUEST reset or
            # powered itself off. The reset instant is the end of the
            # console below -- this is not a hang and not a timeout.
            echo "$phase: qemu exited 0 before the timeout: the guest reset itself." >&2
            echo "The console below ends at the reset instant." >&2
        fi
        echo "--- $phase: last 80 console lines (ANSI stripped) ---" >&2
        tail -n 80 "$CONSOLE_TXT" >&2
    done
    exit 1
fi
# WHAT THE PHASES THAT RAN ACTUALLY SUPPORT, and nothing beyond it. This
# line was unconditional, so a run with boot2 taken out of PHASES ended by
# claiming two boots on the strength of one -- and image.yml quotes this
# claim, in its own words, into the body of a tagged release. The guard on
# PHASES above stops that combination happening at all; this is the second
# half of the same fix, and it is the half a future phase list nobody
# thought of still lands on.
CLAIM_MISSING=""
for want in boot1 boot2; do
    case " $PHASES " in
        *" $want "*) ;;
        *) CLAIM_MISSING="$CLAIM_MISSING $want" ;;
    esac
done
if [ -z "$CLAIM_MISSING" ]; then
    log "the image boots twice on a read-only overlay, and the unit starts on both"
else
    log "the image boots on a read-only overlay and the unit starts; phases run: $PHASES -- NOT the two-boot claim, which needs:$CLAIM_MISSING"
fi

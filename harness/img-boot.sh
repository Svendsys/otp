#!/usr/bin/env bash
#
# Tier 3: boot the image the release pipeline actually produces.
#
#   ./harness/img-boot.sh image/deploy/otp-print-unit.img.xz
#
# pi-gen assembles a filesystem and never starts it, and tier 2 boots a
# Debian cloud image. This boots the artifact that gets flashed, under
# QEMU's raspi3b, three times on the same card image:
#
#   boot1    the overlay is engaged (issue #9). otp-unit-imgcheck.service
#            reports what the root is and that the unit and cupsd came up,
#            and writes a sentinel to / and a setting to /boot/firmware. A
#            valid userconf.txt seed is on the card (issue #20): it must be
#            applied without a prompt and consumed.
#   boot2    the same card, powered off and on. The sentinel must be gone
#            and the setting still there. The userconf wizard must be
#            skipped while staying enabled, and a malformed seed handed to
#            it by the guest check must fail fast, not hang.
#   release  the same card with no otp.imgcheck token, as a flashed unit
#            boots. The probe ships in every image and must stay inert.
#
# One image file for all three: writes that reached the card are there on
# the next boot and writes that only reached the overlay's tmpfs are not.
#
# "Nothing happened" passes by accident, so every absence the release
# phase asserts sits beside a presence taken with the same fixture:
#   - the boot finished (multi-user.target by name);
#   - systemd evaluated otp-unit-imgcheck.service and skipped it, so
#     deleting the unit from the image cannot make the phase greener;
#   - the probe said nothing, each grep checked against boot1's console
#     where it does match;
#   - the probe's two records are deleted from the FAT partition before
#     the boot and do not come back, and the rest of the partition keeps
#     the same names and contents.
#
# QEMU runs no Pi firmware: -append replaces the kernel command line, and
# config.txt's dtparams and overlays are inert. `boot=overlay` is copied
# from the image's cmdline.txt and the initramfs is pulled off the boot
# partition, so an image built without the overlay boots without it here
# and fails the verdict.
#
# Not covered: peripherals. The emulated Pi has no OLED, buttons or
# printer; tier 1 simulates those. Run once per image, not per commit.
# See issue #17 and harness/README.md for how the emulation settings below
# were arrived at.
set -euo pipefail

IMAGE_XZ="${1:?usage: img-boot.sh <image.img.xz>}"
WORK="${OTP_IMG_WORK:-${TMPDIR:-/tmp}/otp-img}"
# A backstop, not a target: the sampler stops the emulator once the phase
# has reported, so only a boot that never got there pays this. CI sets
# OTP_IMG_TIMEOUT=600.
TIMEOUT="${OTP_IMG_TIMEOUT:-1200}"

mkdir -p "$WORK"
WORK="$(cd "$WORK" && pwd)"
IMG="$WORK/card.img"
ESC=$(printf '\033')
# One image file for all three boots: each is a power-cycle of the same
# card, which is the only reason "the setting survived and the sentinel
# did not" says anything.
PHASES="${OTP_IMG_PHASES:-boot1 boot2 release}"
# All three phases are demanded. The same list drives the boot loop, the
# verdict loop and the guest checks, so dropping one drops its checks
# silently:
#   - boot2 carries the power-cycle checks this tier exists for;
#   - boot1 consumes the seed that is copied onto the card regardless, so
#     without it the unseeded checks meet a seeded card;
#   - release is the only phase that observes the shipped probe staying
#     inert, and it asserts a silence, so its absence turns nothing red.
PHASES_MISSING=""
PHASES_EXPECTED=""
for want in boot1 boot2 release; do
    # The same loop builds the order the check below compares against.
    PHASES_EXPECTED="${PHASES_EXPECTED:+$PHASES_EXPECTED }$want"
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
    echo "       release is the boot with NO otp.imgcheck token, which is" >&2
    echo "       how every flashed unit boots. It is the only phase that" >&2
    echo "       observes the shipped probe staying inert, and it is the" >&2
    echo "       one whose absence turns nothing red on its own: the other" >&2
    echo "       phases report, this one asserts a silence. Drop it and the" >&2
    echo "       run still prints a green verdict for an image nobody" >&2
    echo "       checked the probe was quiet on." >&2
    exit 1
fi

# And in this order. A mis-ordered list ends red anyway, but in the wrong
# phase, with a message about the image. Relative order, not equality:
# $PHASES is filtered to the demanded names and compared, so an unknown
# extra phase passes through and a repeated one does not.
PHASES_SEEN=""
for got in $PHASES; do
    case " $PHASES_EXPECTED " in
        *" $got "*) PHASES_SEEN="${PHASES_SEEN:+$PHASES_SEEN }$got" ;;
    esac
done
if [ "$PHASES_SEEN" != "$PHASES_EXPECTED" ]; then
    echo "ERROR: OTP_IMG_PHASES='$PHASES' runs '$PHASES_SEEN'," >&2
    echo "       not '$PHASES_EXPECTED'." >&2
    echo "       The three boots are the same card in sequence, so the" >&2
    echo "       order is part of what each one means:" >&2
    echo "       boot1 seeds and writes, boot2 is the power-cycle that" >&2
    echo "       reads what boot1 left, and release is booted last because" >&2
    echo "       it DELETES the probe's two records from the card first." >&2
    echo "       Run release before boot2 and boot2 fails the machine-id" >&2
    echo "       and credential checks those records exist for -- red, but" >&2
    echo "       red in the wrong phase, with a message pointing at the" >&2
    echo "       image for a fault in this variable." >&2
    exit 1
fi

log() { printf '\n== %s\n' "$*" >&2; }

for tool in qemu-system-aarch64 xz; do
    command -v "$tool" >/dev/null || { echo "ERROR: $tool missing" >&2; exit 1; }
done

log "Decompressing $IMAGE_XZ"
xz -dc "$IMAGE_XZ" > "$IMG"

# QEMU's sd-card device requires a power-of-two image size.
SIZE=$(stat -c%s "$IMG")
TARGET=1
while [ "$TARGET" -lt "$SIZE" ]; do TARGET=$((TARGET * 2)); done
if [ "$TARGET" != "$SIZE" ]; then
    log "Padding $SIZE -> $TARGET bytes for the sd interface"
    truncate -s "$TARGET" "$IMG"
fi

# QEMU does not run the Pi's bootloader, so the kernel and the device tree
# are handed to it directly. Both live on the FAT boot partition, whose
# start is read from the MBR: it moves with pi-gen's ALIGN.
log "Locating the boot partition from the partition table"
FIRST_LBA=$(od -An -tu4 -j454 -N4 "$IMG" | tr -d ' ')
if [ -z "$FIRST_LBA" ] || [ "$FIRST_LBA" -lt 1 ] 2>/dev/null; then
    echo "ERROR: could not read the first partition's start from the MBR" >&2
    exit 1
fi
BOOT_OFFSET=$((FIRST_LBA * 512))
log "boot partition starts at sector $FIRST_LBA (byte $BOOT_OFFSET)"
# Cleared, not just created: otherwise a run whose boot partition cannot
# be read would boot the previous image's kernel.
rm -rf "${WORK:?}/boot"
mkdir -p "$WORK/boot"
if command -v mcopy >/dev/null; then
    # Errors not silenced: "Cannot initialize '::'" is the useful message.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::kernel8.img "$WORK/boot/" || true
    # Plain 3-b first: `-M raspi3b` models the 3 Model B, and a device tree
    # for hardware the emulator lacks kills the kernel before console init.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::bcm2710-rpi-3-b.dtb "$WORK/boot/" \
        || mcopy -n -i "$IMG@@$BOOT_OFFSET" ::bcm2710-rpi-3-b-plus.dtb "$WORK/boot/" || true
    # The overlay tokens come out of cmdline.txt and the initramfs name out
    # of config.txt, below. Both also ride into the failure artifact.
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::cmdline.txt "$WORK/boot/" || true
    mcopy -n -i "$IMG@@$BOOT_OFFSET" ::config.txt "$WORK/boot/" || true
else
    echo "ERROR: mtools (mcopy) is needed to read the boot partition" >&2
    exit 1
fi

KERNEL="$WORK/boot/kernel8.img"
# A glob, not `find ... | head -n 1`: under pipefail, head closing the pipe
# can kill find with SIGPIPE and end the script with 141.
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
# On a real Pi the firmware adds a /system node carrying linux,revision,
# the 24-bit board code; the static DTB has none. Without it
# rpi-eeprom-update.service fails on an empty BOARD_INFO, and gpiozero,
# which reads the same property, cannot load a pin factory.
#
# 0xa02082 decodes to the machine QEMU models: new-style flag, 1GB (as
# -m 1024 below), Sony UK, BCM2837, 3 Model B rev 1.2.
#
# The emulated unit still has no buttons (claiming GPIO5 on QEMU's
# gpiochip fails EINVAL) and no display; unit-detects-no-panel in
# harness/img-guest-check.sh keys on the display half.
#
# fdtput patches a copy in place, leaving the image's own DTB comparable.
# A hard requirement, like mcopy.
if ! command -v fdtput >/dev/null; then
    echo "ERROR: fdtput (device-tree-compiler) is needed to give the guest" >&2
    echo "       a board revision. Without it rpi-eeprom-update.service" >&2
    echo "       fails on an empty BOARD_INFO and the boot is red for a" >&2
    echo "       fault in the emulator rather than in the image." >&2
    exit 1
fi
BOARD_REVISION=0xa02082
# Beside $WORK/boot, not in it: the *.dtb glob above must find one file.
DTB_PATCHED="$WORK/otp-harness-$(basename "$DTB")"
cp "$DTB" "$DTB_PATCHED"
# `-c` on an existing node fails rather than no-ops, so the create may
# fail; the read-back below is the gate.
fdtput -c "$DTB_PATCHED" /system 2>/dev/null || true
if ! fdtput -t x "$DTB_PATCHED" /system linux,revision "$BOARD_REVISION"; then
    echo "ERROR: could not write linux,revision into $DTB_PATCHED" >&2
    exit 1
fi
# Read back: a write that did nothing looks the same from here. fdtget
# prints the property as a decimal integer.
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

# Copied from the image, never assumed: adding boot=overlay here would
# test a configuration no device has.
OVERLAY_TOKENS=""
case " $IMG_CMDLINE " in
    *" boot=overlay "*) OVERLAY_TOKENS="boot=overlay" ;;
esac

# auto_initramfs=1 does nothing under QEMU, so the initrd is handed over
# with -initrd. Its name comes from where the firmware would look: an
# `initramfs <file>` line in config.txt, or initramfs8 by convention.
INITRD_NAME=$(sed -n -E 's/^[[:space:]]*initramfs[[:space:]]+([^[:space:]]+).*/\1/p' \
              "$WORK/boot/config.txt" 2>/dev/null | tail -1)
[ -n "$INITRD_NAME" ] || INITRD_NAME=initramfs8
mcopy -n -i "$IMG@@$BOOT_OFFSET" "::$INITRD_NAME" "$WORK/boot/" || true
INITRD="$WORK/boot/$INITRD_NAME"
if [ -s "$INITRD" ]; then
    log "initramfs=$INITRD ($(wc -c < "$INITRD") bytes) overlay tokens='${OVERLAY_TOKENS:-none}'"
else
    # Not fatal: the guest comes up on a plain read-write root and the
    # verdict names what is missing. Aborting would produce no verdict.
    log "no $INITRD_NAME on the boot partition: booting WITHOUT an initramfs"
    INITRD=""
fi

# --- the seeded credential file, planted before the first boot -----------

# The seed names the image's own first user (image/build.sh's
# FIRST_USER_NAME, held equal by tests/test_img_verdict.py), which keeps
# userconf-pi's rename branch out of the plain apply. The rename case is
# synthesised in boot2: see
# credential-recovers-a-store-naming-another-account in
# harness/img-guest-check.sh.
USERCONF_USER="otp"
# sha512-crypt, generated with
#
#   openssl passwd -6 -salt otpimgcheck 'otp-imgcheck-not-a-credential'
#
# Not a secret: it is written only into $WORK/card.img, the working copy.
# The salt is the marker the guest looks for in /etc/shadow; pi-gen hashes
# FIRST_USER_PASS with a random salt.
# shellcheck disable=SC2016  # the $ signs are crypt(3) field separators
USERCONF_HASH='$6$otpimgcheck$xPbFgUo86.IHXj9q2xsofEuqNQoZjMUYoMw/E51qX6xuUT1zEPNF7hilpqHDnUoQ2F9YOlvzIbPyQOwpAHGPm0'
log "Seeding ::userconf.txt for $USERCONF_USER (the headless credential path, issue #20)"
printf '%s:%s\n' "$USERCONF_USER" "$USERCONF_HASH" > "$WORK/userconf.txt"
# Not fatal: the verdict reports a seed that never reached the card.
mcopy -o -n -i "$IMG@@$BOOT_OFFSET" "$WORK/userconf.txt" ::userconf.txt || true

# The FAT root as the host sees it, listed either side of every boot. The
# machine deletes the seed it consumes, so only the before-listing tells
# an applied seed from one never written.
fat_listing() {
    mdir -b -i "$IMG@@$BOOT_OFFSET" :: > "$1" 2>/dev/null || : > "$1"
    # `mdir -b` is not recursive, so the store is listed separately, giving
    # `::/otp-identity/<file>` lines for `fat_lists`. Not fatal: a card with
    # no store yet has no such directory.
    mdir -b -i "$IMG@@$BOOT_OFFSET" ::/otp-identity >> "$1" 2>/dev/null || true
}

# --- what a release boot has to leave exactly as it found it --------------

# The probe's own records. The release phase deletes these before it
# boots; the verdict requires them to have been there and not to come
# back. tests/test_img_verdict.py holds this list against
# harness/img-guest-check.sh, which writes them.
PROBE_DROPPINGS="otp-imgcheck-machine-id otp-imgcheck-credential"

# The operator's files, which a release boot must not disturb. Compared by
# content as well as by name: a file rewritten in place keeps its name.
FAT_CONTENTS="otp-unit.conf otp-identity/machine-id otp-identity/credential"

# A digest, never the bytes: ::/otp-identity/credential is a password hash
# and these files travel as CI artifacts. `absent` and `empty` are words,
# not digests, because the gate requires 64 hex characters on both sides;
# two digests of nothing would otherwise compare equal.
fat_digest_of() {
    # Through a file, not a variable: command substitution strips trailing
    # newlines. mtype's exit status separates a missing file (rc 1) from an
    # empty one (rc 0, zero bytes).
    local body digest
    body="$WORK/.fat-digest-body"
    if ! mtype -n -i "$IMG@@$BOOT_OFFSET" "::$1" > "$body" 2>/dev/null; then
        rm -f "$body"
        printf 'absent'
        return 0
    fi
    if [ ! -s "$body" ]; then
        rm -f "$body"
        printf 'empty'
        return 0
    fi
    # `|| true`: a missing sha256sum must leave a value the gate goes red
    # on, not kill the run before the verdict.
    digest=$(sha256sum < "$body" 2>/dev/null | cut -d' ' -f1 || true)
    rm -f "$body"
    printf '%s' "$digest"
}

# mtype and mdel are not hard requirements: without them the content and
# deletion gates go red, which is the direction a missing tool must fail.
fat_digests() {
    local out="$1" name
    : > "$out"
    for name in $FAT_CONTENTS; do
        printf '%s %s\n' "$name" "$(fat_digest_of "$name")" >> "$out"
    done
}

# Before the release boot and the listing it is judged against. Taking the
# records off first makes "they did not come back" a claim a boot can
# fail. The listing taken first is the control that they were there.
strip_probe_droppings() {
    local dir="$1" name
    fat_listing "$dir/boot-files-before-strip.txt"
    for name in $PROBE_DROPPINGS; do
        # `|| true`: mdel answers non-zero on a missing file, and the control
        # must get to report that absence.
        mdel -i "$IMG@@$BOOT_OFFSET" "::$name" 2>/dev/null || true
    done
}

# root=/dev/mmcblk0p2 rather than cmdline.txt's root=PARTUUID=...: -append
# replaces the command line wholesale, so root= must be restated, and an
# explicit device name does not depend on the image's cmdline.txt.
#
# One boot, into its own directory. Everything a phase produces is written
# there, so the verdict is a function of files on disk, which lets
# tests/test_img_verdict.py run the real verdict block over synthetic
# consoles.
boot_phase() {
    local phase="$1"
    local dir="$WORK/$phase"
    rm -rf "$dir"
    mkdir -p "$dir"
    # Before the before-listing, so the deletion is not attributed to the
    # boot.
    case "$phase" in
        release) strip_probe_droppings "$dir" ;;
    esac
    fat_listing "$dir/boot-files-before.txt"
    fat_digests "$dir/boot-digests-before.txt"
    # Both UARTs, captured separately: the first -serial is the PL011 (ttyAMA1
    # under this DTB), the second the mini-UART, where the earlycon
    # bootconsole lives.
    local console="$dir/console.log"
    local console2="$dir/console-uart1.log"
    : > "$console"
    : > "$console2"
    # Absent in the release phase, not empty: `otp.imgcheck=` would still
    # satisfy ConditionKernelCommandLine=otp.imgcheck and start the probe.
    # An empty variable leaves only extra spaces inside the one quoted
    # -append argument, which the kernel's tokenizer skips.
    local imgcheck_token=""
    case "$phase" in
        release) ;;
        *) imgcheck_token="otp.imgcheck=$phase" ;;
    esac
    log "Booting $phase under -M raspi3b (emulated, not KVM -- allow minutes)"
    set +e
# The command line:
#   loglevel=7 keeps KERN_INFO, where the boot narrates itself (mmcblk
#     enumeration, "VFS: Mounted root", systemd[1] lines), and drops DEBUG.
#   initcall_blacklist=bcm2835_pm_driver_init: the pm driver's writes
#     against QEMU's partial PM model arm its watchdog and reset the guest
#     at ~11.5s. Emulation only; `harness/img-local-rig.sh idle-survive`
#     re-verifies it.
#   console=ttyAMA1 is what the PL011 is called under this DTB. serial0
#     and stdout-path point at the mini-UART, whose driver does not probe
#     under QEMU, and ttyAMA0 does not exist.
#   systemd.show_status=1 puts "Started ..." and "Reached target" status
#     lines on the console.
#   systemd.journald.forward_to_console=1 streams the otherwise volatile
#     journal to the captured console (issue #21). Emulation only; the
#     verdict requires a marker only forwarding can deliver.
#   $imgcheck_token is otp.imgcheck=<phase>, which wakes
#     otp-unit-imgcheck.service; empty in the release phase.
#   $OVERLAY_TOKENS is boot=overlay when the image's cmdline.txt has it.
    timeout -k 30 "$TIMEOUT" qemu-system-aarch64 \
        -M raspi3b -m 1024 \
        -kernel "$KERNEL" -dtb "$DTB" \
        ${INITRD:+-initrd "$INITRD"} \
        -append "rw earlycon loglevel=7 console=ttyAMA1,115200 systemd.show_status=1 systemd.journald.forward_to_console=1 initcall_blacklist=bcm2835_pm_driver_init root=/dev/mmcblk0p2 rootfstype=ext4 rootwait $OVERLAY_TOKENS $imgcheck_token" \
        -drive "file=$IMG,if=sd,format=raw" \
        -serial "file:$console" \
        -serial "file:$console2" \
        -display none -no-reboot &
    local qemu_pid=$!

    # Sample the console while it boots: growth still climbing means slow,
    # flat for minutes means stuck.
    local sample=30 elapsed=0 last=0 early_stop='' now now2 cpu seen
    # What ends the phase early. Probe boots stop on the probe's done line.
    # The release boot has no probe and stops on multi-user.target, the
    # string the verdict gates on. The probe unit is Type=oneshot and
    # WantedBy=multi-user.target, so it is ordered before the target: a
    # probe that ran would have printed before this marker appears.
    #
    # The release settle is 45s because that phase asserts nothing was
    # written to the FAT partition, and a dirty page reaches the card only
    # after dirty_expire (30s default) plus a writeback tick (5s), plus
    # margin. Probe boots sync before printing done and drain for 10s.
    local stop_marker="OTP-GUEST-DONE $phase" stop_what="reported done" settle=10
    case "$phase" in
        release)
            stop_marker="Reached target multi-user.target"
            stop_what="finished booting"
            settle=45
            ;;
    esac
    while kill -0 "$qemu_pid" 2>/dev/null; do
        sleep "$sample"
        elapsed=$((elapsed + sample))
        now=$(wc -c < "$console" 2>/dev/null || echo 0)
        now2=$(wc -c < "$console2" 2>/dev/null || echo 0)
        # A healthy boot never ends by itself, so stop once the marker shows
        # rather than race the backstop.
        #
        # grep -c, not grep -q: under pipefail, -q closing the pipe kills sed
        # with SIGPIPE and the early stop never fires on a large console.
        #
        # The ANSI strip is needed because systemd colors the unit name inside
        # a status line, splitting "Reached target" from "multi-user.target".
        seen=$(sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' "$console" 2>/dev/null \
               | grep -cF "$stop_marker" || true)
        if [ -z "$early_stop" ] && [ "${seen:-0}" != "0" ]; then
            early_stop=$elapsed
            printf '   %s %s at %ss wall; draining %ss, then stopping qemu\n' \
                   "$phase" "$stop_what" "$elapsed" "$settle" >&2
            sleep "$settle"
            kill "$qemu_pid" 2>/dev/null || true
        fi
        # Host-side CPU% of the emulator: flat output alone cannot tell an
        # idle guest from one spinning. $qemu_pid is the timeout(1) wrapper,
        # so sample its child.
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
    # A power-cycle: the emulator is gone and the image file is flushed
    # before the next phase opens it.
    sync 2>/dev/null || true
    # After the sync, so the listing is what reached the card.
    fat_listing "$dir/boot-files-after.txt"
    fat_digests "$dir/boot-digests-after.txt"
}

for phase in $PHASES; do
    boot_phase "$phase"
done

# --- the verdict --------------------------------------------------------

# Everything downstream reads the combination, so a boot that talks to
# whichever port is still judged on what it said.
VERDICT="$WORK/verdict.txt"

# The evidence one phase left behind. A phase is a directory and the
# verdict is a function of what is in it, which lets
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
    # The verdict greps an ANSI- and CR-stripped copy: systemd colors the
    # unit name ("Started ESC[0;1;39motp-unit.serviceESC[0m - ..."), and
    # the serial console adds CRs.
    sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' \
        "$CONSOLE_ALL" > "$CONSOLE_TXT" 2>/dev/null || cp "$CONSOLE_ALL" "$CONSOLE_TXT"
}

# The lines the system wrote, as opposed to lines it carried for others.
# journald formats a forwarded line as timestamp, speaker[pid], text:
#
#   [   45.123456] python3[412]: no OLED (...)
#
# Kernel output has no speaker and PID 1's status lines no timestamp, so
# both are kept; systemd[1] is kept by name. `|| true` so an absent file
# gives an empty result rather than killing the verdict.
system_lines() {
    awk '!/^\[[^]]*\] [^ ]+\[[0-9]+\]:/ || /^\[[^]]*\] systemd\[1\]:/' \
        "$1" 2>/dev/null || true
}

per_boot_verdict() {
    local phase="$1"
# Max over every timestamp in both files, not the last line: uart1 ends at
# the bootconsole handoff (~2s). `|| true` because a console with no
# timestamp makes grep exit 1, which under pipefail+errexit would kill the
# script before verdict.txt is written.
    local LAST_TS KERNEL_ENTRIES SPOKEN FORWARDED FORWARDED_PID1 KCMDLINE SKIPLINE
    LAST_TS=$(grep -oE '\[ *[0-9]+\.[0-9]+\]' "$CONSOLE_TXT" 2>/dev/null | tr -d '[] ' | sort -g | tail -1 || true)
    # Lines journald labels `kernel: ` are excluded: forwarded copies of
    # kmsg entries would count a healthy boot as two kernel entries. A
    # real second entry prints with no identifier and is still counted.
    KERNEL_ENTRIES=$(grep -vE '^\[[^]]*\] kernel: ' "$CONSOLE_TXT" 2>/dev/null \
                     | grep -c "Booting Linux on physical CPU" || true)
    printf 'qemu exit: %s\n' "$QEMU_RC"
    # Kernel entries > 1 is a reboot loop stated as a number.
    printf 'guest reached: %ss guest time; kernel entered %s time(s)\n' \
           "${LAST_TS:-?}" "${KERNEL_ENTRIES:-0}"
    # Not a bare "otp-unit": that is also the image's hostname. Not the
    # Description either: systemd colors the unit name, so nothing can
    # span "Started" into it. "Started otp-unit.service" on the stripped
    # text is in this unit's success line only.
    if grep -qF "Started otp-unit.service" "$CONSOLE_TXT" 2>/dev/null; then
        printf 'IMG-CHECK %s unit-started PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s unit-started FAIL\n' "$phase"
    fi
    # The journal reached the console (issue #21). The guest writes a marker
    # into the journal with systemd-cat and nowhere else, so only
    # forwarding can have put it here; the guest's journal-marker-accepted
    # says the journal took it.
    #
    # The release phase has no probe to write the marker, so it is asked
    # for any line only forwarding can have produced: timestamp,
    # speaker[pid], text, from a speaker other than PID 1, which also
    # writes to /dev/console and /dev/kmsg directly.
    case "$phase" in
        release)
            FORWARDED=$(grep -cE '^\[[^]]*\] [^ ]+\[[0-9]+\]:' "$CONSOLE_TXT" 2>/dev/null || true)
            FORWARDED_PID1=$(grep -cE '^\[[^]]*\] systemd\[1\]:' "$CONSOLE_TXT" 2>/dev/null || true)
            if [ "$(( ${FORWARDED:-0} - ${FORWARDED_PID1:-0} ))" -gt 0 ]; then
                printf 'IMG-CHECK %s journal-forwarding-alive PASS %s forwarded line(s), %s of them PID 1\n' \
                       "$phase" "${FORWARDED:-0}" "${FORWARDED_PID1:-0}"
            else
                printf 'IMG-CHECK %s journal-forwarding-alive FAIL %s forwarded line(s), %s of them PID 1: nothing on this console can only have come through the journal, so every absence this phase reports is unbacked\n' \
                       "$phase" "${FORWARDED:-0}" "${FORWARDED_PID1:-0}"
            fi
            ;;
        *)
            if grep -qE "OTP-JOURNAL-FORWARDED[[:space:]]+${phase}([[:space:]]|\$)" \
                    "$CONSOLE_TXT" 2>/dev/null; then
                printf 'IMG-CHECK %s journal-forwarded-to-console PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s journal-forwarded-to-console FAIL\n' "$phase"
            fi
            ;;
    esac
    # The token that decides whether the probe runs, read from the kernel's
    # own "Kernel command line:" echo rather than from this script's
    # variables. Both directions: boot1 and boot2 require it with their
    # phase, which is what makes its absence in release mean something.
    KCMDLINE=$(grep -m1 -F "Kernel command line:" "$CONSOLE_TXT" 2>/dev/null || true)
    case "$phase" in
        release)
            if [ -n "$KCMDLINE" ] \
               && ! printf '%s' "$KCMDLINE" | grep -qF "otp.imgcheck"; then
                printf 'IMG-CHECK %s cmdline-carries-no-imgcheck-token PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s cmdline-carries-no-imgcheck-token FAIL %s\n' \
                       "$phase" "${KCMDLINE:-no Kernel command line line on this console}"
            fi
            ;;
        *)
            if printf '%s' "$KCMDLINE" | grep -qF "otp.imgcheck=$phase"; then
                printf 'IMG-CHECK %s cmdline-carries-the-imgcheck-token PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s cmdline-carries-the-imgcheck-token FAIL %s\n' \
                       "$phase" "${KCMDLINE:-no Kernel command line line on this console}"
            fi
            ;;
    esac
    # The forbidden phrases are looked for in what the system said. With
    # the journal forwarded, any unit can quote "Failed with result" on
    # this console, so system_lines() keeps kernel and PID 1 output only;
    # a phrase found only elsewhere is reported as a note, not gated.
    #
    # Only phrases PID 1 or the kernel utter can be enforced here. A
    # unit's own line (e.g. `sshd[744]: fatal: ...`) is dropped by
    # system_lines(); gate those in the guest check instead.
    #
    # Falls back to the whole console if the filtered copy cannot be
    # written: wider is the safe direction for a gate.
    SPOKEN="$WORK/$phase/console-system.log"
    if ! system_lines "$CONSOLE_TXT" > "$SPOKEN" 2>/dev/null; then
        SPOKEN="$CONSOLE_TXT"
    fi
    for bad in "status=216" "Failed with result" "Scheduled restart job" \
               "Kernel panic" "Unable to mount root"; do
        if grep -qF -- "$bad" "$SPOKEN" 2>/dev/null; then
            printf 'IMG-CHECK %s no-%s FAIL\n' "$phase" "$(printf '%s' "$bad" | tr ' =' '--')"
            # Which lines, indented under the red. Bounded at 10 lines and 200
            # columns because a reboot loop repeats its phrase.
            grep -F -- "$bad" "$SPOKEN" 2>/dev/null \
                | head -10 | cut -c1-200 | sed 's/^/    /' || true
        elif grep -qF -- "$bad" "$CONSOLE_TXT" 2>/dev/null; then
            printf 'IMG-NOTE %s quoted-%s: %s\n' "$phase" \
                   "$(printf '%s' "$bad" | tr ' =' '--')" \
                   "$(grep -m1 -F -- "$bad" "$CONSOLE_TXT" 2>/dev/null | cut -c1-120)"
        fi
    done
    # Entries != 1 is a boot that reset and looped; it fails the run
    # whatever else passed.
    if [ "${KERNEL_ENTRIES:-0}" = "1" ]; then
        printf 'IMG-CHECK %s single-kernel-entry PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s single-kernel-entry FAIL\n' "$phase"
    fi
    # "systemd[1]:", not "systemd": the bare string matches the kernel
    # command line, which carries systemd.show_status=1.
    #
    # "crng init done" is the kernel saying the CSPRNG is seeded.
    # getrandom() blocks until then, so without it the unit cannot make
    # key material (issue #16). `harness/img-local-rig.sh rng` re-measures
    # it.
    #
    # "hwrng registered" is the hw_random core accepting the builtin
    # bcm2835-rng: the SoC's oscillator seeded the pool, not interrupt
    # timing alone. loglevel=7 keeps both lines.
    #
    # multi-user.target by name, because "Reached target" alone is
    # satisfied by early targets on a boot that never finishes. The probe
    # is a oneshot WantedBy=multi-user.target, so the target is reached
    # just after OTP-GUEST-DONE, inside the 10s drain.
    for want in "Linux version" "systemd[1]:" "Reached target" \
                "Reached target multi-user.target" \
                "crng init done" "hwrng registered"; do
        if grep -qF -- "$want" "$CONSOLE_TXT" 2>/dev/null; then
            printf 'IMG-CHECK %s %s PASS\n' "$phase" "$(printf '%s' "$want" | tr ' ' '-')"
        else
            printf 'IMG-CHECK %s %s FAIL\n' "$phase" "$(printf '%s' "$want" | tr ' ' '-')"
        fi
    done
    # Quoted as well as gated, with 40 characters of leading context, so
    # the note shows which driver registered.
    if grep -qiE "hwrng|hw_random|rng_core" "$CONSOLE_TXT" 2>/dev/null; then
        printf 'IMG-NOTE hwrng-line: %s\n' \
            "$(grep -ioE '.{0,40}(hwrng|hw_random|rng_core).{0,44}' \
                 "$CONSOLE_TXT" 2>/dev/null | head -1)"
    fi
    # Which targets were reached: the gate says whether the boot finished,
    # the note says where an unfinished one stopped.
    printf 'IMG-NOTE %s targets-reached: %s\n' "$phase" \
        "$(grep -ohE 'Reached target [a-zA-Z0-9@:._-]+' "$CONSOLE_TXT" 2>/dev/null \
             | sort -u | tr '\n' ' ')"

    # first-boot-complete.target is pulled in only when PID 1 read
    # /etc/machine-id as uninitialised. With the machine-id persisted
    # outside the overlay only boot1 is a first boot. Both directions:
    # boot1 must print the string (each run starts from a fresh `xz -dc`),
    # which is what makes its absence in boot2 a check.
    case "$phase" in
        boot1)
            if grep -qF "Reached target first-boot-complete.target" \
                    "$CONSOLE_TXT" 2>/dev/null; then
                printf 'IMG-CHECK %s first-boot-really-was-a-first-boot PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s first-boot-really-was-a-first-boot FAIL\n' "$phase"
            fi
            ;;
        boot2)
            if grep -qF "Reached target first-boot-complete.target" \
                    "$CONSOLE_TXT" 2>/dev/null; then
                printf 'IMG-CHECK %s second-boot-is-not-a-first-boot FAIL\n' "$phase"
            else
                printf 'IMG-CHECK %s second-boot-is-not-a-first-boot PASS\n' "$phase"
            fi
            ;;
        release)
            # A first boot here would mean the machine-id was lost between
            # boot2 and this boot.
            if grep -qF "Reached target first-boot-complete.target" \
                    "$CONSOLE_TXT" 2>/dev/null; then
                printf 'IMG-CHECK %s release-boot-is-not-a-first-boot FAIL\n' "$phase"
            else
                printf 'IMG-CHECK %s release-boot-is-not-a-first-boot PASS\n' "$phase"
            fi
            # --- THE CLAUSE THE WHOLE PHASE EXISTS FOR ---------------------
            #
            # systemd's own statement that it looked at
            # otp-unit-imgcheck.service and did not start it. Every other
            # release clause is an absence that an image without the unit
            # would also satisfy.
            #
            # The wording varies by systemd version (src/core/job.c):
            #   "%s was skipped because of an unmet condition check (X=Y)."
            #   "%s skipped, unmet condition check X=Y"
            #   "Condition check resulted in %s being skipped."
            # All are accepted; the pattern anchors on the unit being named on
            # the same line, which does not vary.
            #
            # Not proof that forwarding works: PID 1 can reach the console
            # through /dev/kmsg. journal-forwarding-alive covers that.
            SKIPLINE=$(grep -m1 -E \
                'otp-unit-imgcheck\.service.*(unmet condition check|was skipped because|being skipped)|(unmet condition check|was skipped because|being skipped).*otp-unit-imgcheck\.service' \
                "$CONSOLE_TXT" 2>/dev/null || true)
            if [ -n "$SKIPLINE" ]; then
                # If the line names a condition it must be ours; the generic
                # wording carries none and must not fail for saying less.
                case "$SKIPLINE" in
                    *ConditionKernelCommandLine=otp.imgcheck*|*[Cc]ondition*check*resulted*)
                        printf 'IMG-CHECK %s imgcheck-unit-considered-and-skipped PASS\n' "$phase"
                        ;;
                    *Condition*=*)
                        printf 'IMG-CHECK %s imgcheck-unit-considered-and-skipped FAIL the unit was skipped for a condition that is not ConditionKernelCommandLine=otp.imgcheck\n' \
                               "$phase"
                        ;;
                    *)
                        printf 'IMG-CHECK %s imgcheck-unit-considered-and-skipped PASS\n' "$phase"
                        ;;
                esac
                # 400 columns, so the clip cannot cut off the condition
                # parameter the case above gates on.
                # tests/test_img_verdict.py re-derives the line lengths from
                # the unit's Description= and fails while there is still room.
                printf 'IMG-NOTE %s imgcheck-skip-line: %s\n' \
                       "$phase" "$(printf '%s' "$SKIPLINE" | cut -c1-400)"
            else
                printf 'IMG-CHECK %s imgcheck-unit-considered-and-skipped FAIL systemd never named otp-unit-imgcheck.service as skipped: either the unit is not in this image, or it was not enabled, or it RAN\n' \
                       "$phase"
                # Say what was there instead. No `head` in these pipelines:
                # under pipefail it kills grep with SIGPIPE and ends the
                # function before the remaining release clauses print.
                MENTIONS=$(grep -E 'otp-unit-imgcheck' "$CONSOLE_TXT" 2>/dev/null || true)
                SKIPS=$(grep -E '(was skipped because|being skipped)' "$CONSOLE_TXT" 2>/dev/null || true)
                printf 'IMG-NOTE %s imgcheck-unit-mentioned: %s line(s)\n' \
                       "$phase" "$(printf '%s' "$MENTIONS" | grep -c . || true)"
                printf 'IMG-NOTE %s skip-lines-seen: %s line(s)\n' \
                       "$phase" "$(printf '%s' "$SKIPS" | grep -c . || true)"
                printf '%s\n' "$MENTIONS" | sed -n '1,5p' | cut -c1-400 \
                    | while IFS= read -r line; do
                          [ -n "$line" ] || continue
                          printf 'IMG-NOTE %s imgcheck-unit-line: %s\n' "$phase" "$line"
                      done
                printf '%s\n' "$SKIPS" | sed -n '1,5p' | cut -c1-400 \
                    | while IFS= read -r line; do
                          [ -n "$line" ] || continue
                          printf 'IMG-NOTE %s skip-line-seen: %s\n' "$phase" "$line"
                      done
            fi
            ;;
    esac
}

# --- what the guest said about the overlay -------------------------------

# The checks the guest must have made, by name: requiring only the absence
# of FAIL lines is satisfied by a guest that ran none (issue #14).
# tests/test_img_verdict.py holds these lists against
# harness/img-guest-check.sh.
GUEST_CHECKS_COMMON="root-is-overlay root-write-lands-and-is-readable \
unit-active unit-not-restart-looping unit-will-make-live-pads \
journal-marker-accepted \
unit-detects-no-panel etc-cups-is-its-own-tmpfs cups-running \
cupsd-config-valid boot-partition-separate setting-saved-to-the-boot-partition \
cloud-init-is-not-installed cloud-init-kill-switch-survives-the-purge \
network-wait-cannot-hold-the-boot-open \
machine-id-persisted-outside-the-overlay"
GUEST_CHECKS_BOOT1="userconf-seed-applied userconf-seeded-boot-ran-no-wizard \
front-panel-survives-the-credential-apply diagnostic-sheet-renders \
diagnostic-sheet-reaches-cups machine-id-recorded-for-the-next-boot \
credential-recorded-outside-the-overlay credential-recorded-for-the-next-boot"
GUEST_CHECKS_BOOT2="root-writes-discarded-by-the-power-cycle \
settings-survive-the-power-cycle userconf-unseeded-boot-skips-the-wizard \
userconf-wizard-cannot-prompt userconf-malformed-seed-fails-fast \
machine-id-identical-across-the-power-cycle \
credential-survives-the-power-cycle \
credential-recovers-a-store-naming-another-account"

guest_gate() {
    local phase="$1" count counts passed total missing want name

    # Exactly once: a guest that reported twice rebooted underneath the
    # harness.
    count=$(grep -cE "OTP-RESULT[[:space:]]+${phase}[[:space:]]" "$CONSOLE_TXT" 2>/dev/null || true)
    if [ "${count:-0}" = "1" ]; then
        printf 'IMG-CHECK %s guest-reported-once PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s guest-reported-once FAIL %s OTP-RESULT lines\n' \
               "$phase" "${count:-0}"
    fi

    # OTP-GUEST-DONE is the only evidence the phase ran to its end rather
    # than dying part way with self-consistent counts.
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

    # The counts come from the guest, the thing under test, so one FAIL
    # line fails the phase on its own.
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
    # Per phase: only boot1 has a seed to consume and only boot2 can speak
    # about what survived the power-cycle.
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

# `mdir -b` prints one path per line, "::/kernel8.img". Matched whole-line
# and case-insensitively, because FAT tools differ on name case.
fat_lists() {
    grep -qixF -- "::/$2" "$1" 2>/dev/null
}

userconf_gate() {
    local phase="$1"
    local before="$WORK/$phase/boot-files-before.txt"
    local after="$WORK/$phase/boot-files-after.txt"

    # The positive control for every absence below: a listing nothing could
    # read also lacks userconf.txt. kernel8.img is on every bootable image.
    if fat_lists "$after" kernel8.img; then
        printf 'IMG-CHECK %s boot-partition-listed PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s boot-partition-listed FAIL %s lines, no kernel8.img\n' \
               "$phase" "$(wc -l < "$after" 2>/dev/null || echo 0)"
    fi

    case "$phase" in
        boot1)
            # The seed reached the card; otherwise an mcopy that did nothing
            # satisfies the check below.
            if fat_lists "$before" userconf.txt; then
                printf 'IMG-CHECK %s userconf-seed-planted PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s userconf-seed-planted FAIL not in the boot partition before the boot\n' \
                       "$phase"
            fi
            # Consumed means both: userconf.txt gone, and no failed_userconf.txt
            # saying it was rejected as malformed.
            if ! fat_lists "$after" userconf.txt && ! fat_lists "$after" failed_userconf.txt; then
                printf 'IMG-CHECK %s userconf-seed-consumed PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s userconf-seed-consumed FAIL still there:%s%s\n' "$phase" \
                       "$(fat_lists "$after" userconf.txt && printf ' userconf.txt')" \
                       "$(fat_lists "$after" failed_userconf.txt && printf ' failed_userconf.txt')"
            fi
            # The credential store, seen from the host with no running system
            # involved. The image ships no credential, so the file must be
            # absent before this boot and present after it.
            if ! fat_lists "$before" otp-identity/credential \
               && fat_lists "$after" otp-identity/credential; then
                printf 'IMG-CHECK %s credential-kept-on-the-card PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s credential-kept-on-the-card FAIL before:%s after:%s\n' \
                       "$phase" \
                       "$(fat_lists "$before" otp-identity/credential && printf yes || printf no)" \
                       "$(fat_lists "$after" otp-identity/credential && printf yes || printf no)"
            fi
            ;;
        boot2)
            # A malformed seed must end up quarantined under its failed_ name,
            # not left where the next boot's wizard would find it.
            if fat_lists "$after" failed_userconf.txt && ! fat_lists "$after" userconf.txt; then
                printf 'IMG-CHECK %s userconf-malformed-seed-quarantined PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s userconf-malformed-seed-quarantined FAIL failed_userconf.txt:%s userconf.txt:%s\n' \
                       "$phase" \
                       "$(fat_lists "$after" failed_userconf.txt && printf yes || printf no)" \
                       "$(fat_lists "$after" userconf.txt && printf yes || printf no)"
            fi
            # Present before and after: a power cycle did not take the file
            # away, and nothing rewrote it on a boot with no seed.
            if fat_lists "$before" otp-identity/credential \
               && fat_lists "$after" otp-identity/credential; then
                printf 'IMG-CHECK %s credential-kept-on-the-card PASS\n' "$phase"
            else
                printf 'IMG-CHECK %s credential-kept-on-the-card FAIL before:%s after:%s\n' \
                       "$phase" \
                       "$(fat_lists "$before" otp-identity/credential && printf yes || printf no)" \
                       "$(fat_lists "$after" otp-identity/credential && printf yes || printf no)"
            fi
            ;;
    esac
}

# --- the release boot: what a shipped probe must not do -------------------

# Every clause here is an absence, so each carries a control taken from
# boot1, where the probe demonstrably ran: would this grep have found the
# thing if it were there? A control that comes back zero fails its clause.
#
# probe_lines() strips ANSI and CRs off the raw consoles itself, so it
# does not depend on phase_paths() having processed the other phase.
probe_lines() {
    sed -e "s/${ESC}\[[0-9;]*[a-zA-Z]//g" -e 's/\r//g' \
        "$WORK/$2/console.log" "$WORK/$2/console-uart1.log" 2>/dev/null \
        | grep -cE "$1" || true
}

release_gate() {
    local phase="$1"
    local before="$WORK/$phase/boot-files-before.txt"
    local after="$WORK/$phase/boot-files-after.txt"
    local prestrip="$WORK/$phase/boot-files-before-strip.txt"
    local here control name missing changed dbefore dafter
    # Reads are guarded: a missing evidence file must produce a red check,
    # not a dead script.
    #
    # 1. The probe printed nothing.
    #
    # `OTP-GUEST`, without a trailing hyphen, so that the phase guard's
    # `OTP-GUEST refusing to run:` also matches. Over the whole console, not
    # system_lines(): the probe's output arrives as journald-forwarded lines,
    # which system_lines() drops.
    here=$(probe_lines 'OTP-GUEST' "$phase")
    control=$(probe_lines 'OTP-GUEST' boot1)
    if [ "${here:-1}" = "0" ] && [ "${control:-0}" -gt 0 ] 2>/dev/null; then
        printf 'IMG-CHECK %s guest-probe-silent PASS 0 here, %s in boot1\n' \
               "$phase" "$control"
    else
        printf 'IMG-CHECK %s guest-probe-silent FAIL %s OTP-GUEST line(s) here, %s in boot1 (the control: zero there means this grep no longer finds the probe even where it ran)\n' \
               "$phase" "${here:-?}" "${control:-?}"
    fi

    # 2. And wrote nothing to the journal under its own tag.
    #
    # Matched in the speaker position, `otp-imgcheck[pid]:`, because the bare
    # word is a substring of the two record filenames.
    # journal-forwarding-alive says the channel is open.
    here=$(probe_lines 'otp-imgcheck\[[0-9]+\]:' "$phase")
    control=$(probe_lines 'otp-imgcheck\[[0-9]+\]:' boot1)
    if [ "${here:-1}" = "0" ] && [ "${control:-0}" -gt 0 ] 2>/dev/null; then
        printf 'IMG-CHECK %s guest-probe-journal-tag-absent PASS 0 here, %s in boot1\n' \
               "$phase" "$control"
    else
        printf 'IMG-CHECK %s guest-probe-journal-tag-absent FAIL %s line(s) tagged otp-imgcheck here, %s in boot1 (the control)\n' \
               "$phase" "${here:-?}" "${control:-?}"
    fi

    # 3. And the CUPS queue it creates is not named anywhere.
    #
    # A fingerprint check, not a queue check: the name appears only in the
    # probe's own check details, and the host cannot read cupsd's state,
    # which lives on a tmpfs.
    here=$(probe_lines 'otpimgcheck' "$phase")
    control=$(probe_lines 'otpimgcheck' boot1)
    if [ "${here:-1}" = "0" ] && [ "${control:-0}" -gt 0 ] 2>/dev/null; then
        printf 'IMG-CHECK %s guest-probe-cups-queue-unnamed PASS 0 here, %s in boot1\n' \
               "$phase" "$control"
    else
        printf 'IMG-CHECK %s guest-probe-cups-queue-unnamed FAIL %s mention(s) of the otpimgcheck queue here, %s in boot1 (the control)\n' \
               "$phase" "${here:-?}" "${control:-?}"
    fi

    # 4. The control for the deletion: both records were on the card before
    # the strip, or the clauses below assert an absence they were handed.
    missing=""
    for name in $PROBE_DROPPINGS; do
        fat_lists "$prestrip" "$name" || missing="$missing $name"
    done
    if [ -z "$missing" ]; then
        printf 'IMG-CHECK %s probe-droppings-were-on-the-card PASS %s\n' \
               "$phase" "$PROBE_DROPPINGS"
    else
        printf 'IMG-CHECK %s probe-droppings-were-on-the-card FAIL not there to delete:%s -- the earlier boots stopped writing them, so this phase would be asserting an absence it did not create\n' \
               "$phase" "$missing"
    fi

    # 5. They were taken off and did not come back. Both ends, because "gone
    # afterwards" is also true of an mdel that did nothing.
    missing=""
    for name in $PROBE_DROPPINGS; do
        if fat_lists "$before" "$name"; then missing="$missing before:$name"; fi
        if fat_lists "$after" "$name"; then missing="$missing after:$name"; fi
    done
    if [ -z "$missing" ]; then
        printf 'IMG-CHECK %s probe-droppings-stayed-off-the-card PASS\n' "$phase"
    else
        printf 'IMG-CHECK %s probe-droppings-stayed-off-the-card FAIL%s\n' \
               "$phase" "$missing"
    fi

    # 6. And nothing else on the partition moved. Compared sorted: mtools
    # prints FAT directory order, which a rewrite in place can change.
    # Contents are clause 7.
    changed=$(diff <(sort "$before" 2>/dev/null) <(sort "$after" 2>/dev/null) 2>/dev/null \
              | grep -E '^[<>]' | head -10 | tr '\n' ' ' || true)
    if [ -z "$changed" ]; then
        printf 'IMG-CHECK %s boot-partition-unchanged PASS %s entries either side\n' \
               "$phase" "$(wc -l < "$after" 2>/dev/null || echo 0)"
    else
        printf 'IMG-CHECK %s boot-partition-unchanged FAIL %s\n' "$phase" "$changed"
    fi

    # 7. Including the three files whose contents are the point: the
    # operator's settings, the machine-id and the only login.
    #
    # `absent` and `empty` fail on either side because the length test
    # requires 64 characters. Digests print twelve characters wide: this
    # directory is a CI artifact.
    #
    # Driven by $FAT_CONTENTS, not by reading the digest file: a redirect
    # from a missing file dies under errexit mid-verdict, and a loop over an
    # empty file would compare nothing and pass.
    missing=""
    for name in $FAT_CONTENTS; do
        dbefore=$(awk -v n="$name" '$1 == n {print $2}' \
                  "$WORK/$phase/boot-digests-before.txt" 2>/dev/null || true)
        dafter=$(awk -v n="$name" '$1 == n {print $2}' \
                 "$WORK/$phase/boot-digests-after.txt" 2>/dev/null || true)
        if [ "${#dbefore}" != 64 ] || [ "$dbefore" != "${dafter:-nothing}" ]; then
            missing="$missing $name(${dbefore:0:12}../${dafter:0:12}..)"
        fi
    done
    if [ -z "$missing" ]; then
        printf 'IMG-CHECK %s identity-store-unchanged PASS %s\n' "$phase" "$FAT_CONTENTS"
    else
        printf 'IMG-CHECK %s identity-store-unchanged FAIL%s (a 64-character digest on both sides and equal, or it did not survive this boot)\n' \
               "$phase" "$missing"
    fi
}

: > "$VERDICT"
for phase in $PHASES; do
    phase_paths "$phase"
    if [ ! -s "$CONSOLE_ALL" ]; then
        # An empty console is not "the unit did not start": there is no
        # evidence the kernel ever ran.
        log "$phase: NEITHER uart produced any output: no evidence the kernel ran"
    fi
    {
        printf -- '--- %s ---\n' "$phase"
        per_boot_verdict "$phase"
        # The release phase has no probe by construction, so it gets its own
        # gate rather than a loosened guest_gate.
        case "$phase" in
            release) release_gate "$phase" ;;
            *) guest_gate "$phase" ;;
        esac
        userconf_gate "$phase"
    } >> "$VERDICT"
    # qemu's exit code is context, not the verdict: a healthy boot never
    # exits the emulator, so it ends on the early stop (SIGTERM) or the
    # backstop. The console evidence decides. The narration names the marker
    # each phase was waiting for.
    case "$phase" in
        release)
            STOP_EVIDENCE="Reached target multi-user.target"
            STOPPED_ON="the boot finished"
            STOP_REPORT="the boot DID finish"
            STOP_SILENT="without the boot ever finishing"
            ;;
        *)
            STOP_EVIDENCE="OTP-GUEST-DONE $phase"
            STOPPED_ON="the guest reported done"
            STOP_REPORT="the guest DID report done"
            STOP_SILENT="without a guest report"
            ;;
    esac
    if [ -n "$EARLY_STOP" ]; then
        echo "$phase: qemu stopped by the harness at ${EARLY_STOP}s wall, after $STOPPED_ON (rc=$QEMU_RC)" >&2
    elif [ "$QEMU_RC" = 124 ]; then
        # rc=124 with no early stop means the sampler never saw the marker,
        # not that it was never printed: it may have landed in the last 30s
        # sampling window, so ask the console.
        if grep -qF "$STOP_EVIDENCE" "$CONSOLE_TXT" 2>/dev/null; then
            echo "$phase: $STOP_REPORT, but not in time for the sampler to stop qemu: ran to the ${TIMEOUT}s backstop (rc=124)" >&2
        else
            echo "$phase: qemu ran to the ${TIMEOUT}s backstop $STOP_SILENT (rc=124)" >&2
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
            # Exit 0 before the timeout with -no-reboot means the guest reset
            # or powered itself off.
            echo "$phase: qemu exited 0 before the timeout: the guest reset itself." >&2
            echo "The console below ends at the reset instant." >&2
        fi
        echo "--- $phase: last 80 console lines (ANSI stripped) ---" >&2
        tail -n 80 "$CONSOLE_TXT" >&2
    done
    exit 1
fi
# Claim only what the phases that ran support: image.yml quotes this line
# into the body of a tagged release.
CLAIM_MISSING=""
for want in boot1 boot2 release; do
    case " $PHASES " in
        *" $want "*) ;;
        *) CLAIM_MISSING="$CLAIM_MISSING $want" ;;
    esac
done
if [ -z "$CLAIM_MISSING" ]; then
    log "the image boots twice on a read-only overlay and the unit starts on both, and a third boot with no otp.imgcheck token left the shipped probe inert: systemd named it and skipped it, it printed nothing, and the two records it writes stayed off the card"
else
    log "the image boots on a read-only overlay and the unit starts; phases run: $PHASES -- NOT the three-boot claim, which needs:$CLAIM_MISSING"
fi

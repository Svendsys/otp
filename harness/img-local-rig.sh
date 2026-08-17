#!/usr/bin/env bash
#
# The local repro rig: tier 3's boot environment, without tier 3's image.
#
#   ./harness/img-local-rig.sh rng
#   ./harness/img-local-rig.sh coldplug-replay
#   ./harness/img-local-rig.sh --plan console-test
#
# WHY THIS EXISTS, which is the only thing that justifies a second QEMU
# script in this directory.
#
# harness/img-boot.sh answers "does the built image come up". Its first
# statement is `IMAGE_XZ="${1:?...}"` and its second act is `xz -dc`, so it
# cannot run at all without image/deploy/*.img.xz -- and that artifact is
# the pi-gen arm64 job, which is the expensive half of every tier-3 round
# trip. Measured on run 16: 6m58s of pi-gen on a cache miss, 7m59s for the
# pair of boots, 16m16s for the whole job. A hypothesis about the kernel,
# the console, the watchdog or coldplug therefore cost a quarter of an hour
# and a push to test, and issue #17's narrative is largely a record of
# paying that price -- several times for theories that turned out to be
# wrong, which is exactly the case where the price is least affordable.
#
# This rig removes the image from the loop. The kernel and the device tree
# come out of the same Raspberry Pi archive .deb that pi-gen would install,
# the root filesystem is a ~1MB busybox initramfs, and the question under
# test is a shell heredoc in this file. Nothing is built; nothing is pushed.
#
# WHAT IT SETTLED THE FIRST TIME, before it was committed and while it
# existed only in a scratch directory (issue #17, and issue #22 asking for
# it back): the modules-load typo in run 10, the collapse of the entropy
# theory (crng inits at 2.4s, so the stall was never entropy), the collapse
# of the coldplug-wedge theory (41/41 probes clean), the PL011/ttyAMA1
# discovery that unblocked six runs of misdiagnosed "freeze", and the
# re-verification of the watchdog fix on the image's exact kernel. Each in
# minutes, none of them requiring an image.
#
# FINDINGS TRANSFER ONLY IF THE MACHINE MATCHES, so the flags that decide
# what the guest is are shared with img-boot.sh rather than restated from
# memory: -M raspi3b, -m 1024, loglevel=7, console=ttyAMA1, the
# bcm2835_pm_driver_init blacklist, and the synthesised board revision.
# tests/test_local_rig.py parses BOTH scripts and fails if they drift --
# including if img-boot.sh gains a kernel parameter this rig does not
# consciously omit. A rig that boots a subtly different machine is worse
# than no rig: it produces confident answers about somewhere else.
#
# WHAT IT IS NOT. It is not a check and it has no CI wiring, deliberately
# (issue #22). It boots no image, so it says nothing about the overlay, the
# unit, CUPS, the front panel or provisioning -- every one of those is
# tier 3's business and needs the real artifact. This answers kernel-level
# and early-userspace questions only, which is the class of question that
# cost the most to ask through CI.
#
# MEASURED, on the container this was written in (x86_64, QEMU 8.2.2
# "Debian 1:8.2.2+ds-0ubuntu1.18", TCG):
#
#   linux-image-rpi-v8 1:6.12.96-1+rpt1 depends on
#   linux-image-6.12.96+rpt-rpi-v8, whose .deb is 32,739,088 bytes and
#   carries the DTBs at usr/lib/linux-image-*/broadcom/.
#
#   THE DEB'S KERNEL IS GZIP, NOT AN IMAGE. boot/vmlinuz-6.12.96+rpt-rpi-v8
#   begins 1f 8b; on a real Pi it is the raspi-firmware postinst that
#   decompresses it into kernel8.img, and QEMU is not that postinst.
#   Handing the compressed file to -kernel produces a boot that writes
#   NOTHING to either UART -- the same symptom as run 1's wrong DTB, and
#   the same afternoon to diagnose. Decompressed, offset 56 reads "ARMd",
#   the arm64 Image magic, and rig_require_arm64_image() below refuses
#   anything else rather than letting it reach the emulator.
#
#   `poweroff -f` DOES NOT STOP THE EMULATOR under -M raspi3b. Measured:
#   a probe printed its own DONE marker at 4.15s guest and the machine
#   then sat in rcu_preempt stall reports until the 180s cap, exiting 124.
#   So the rig watches the console for the marker and stops qemu itself,
#   exactly as img-boot.sh does for its guest check. Without that the rig
#   is not "seconds" and its whole reason for existing is gone.
#
# HOST REQUIREMENTS, documented rather than vendored (issue #22):
# qemu-system-aarch64 with the raspi3b machine, curl, xz, gzip, cpio,
# dpkg-deb, fdtput (device-tree-compiler), and -- for coldplug-replay only
# -- depmod (kmod) and mkfs.ext4 (e2fsprogs). A static arm64 busybox is
# fetched from deb.debian.org unless OTP_RIG_BUSYBOX names one. Everything
# downloaded is cached in the work directory, so the second run of the day
# does no network at all.

set -euo pipefail

# --- the machine, shared with img-boot.sh --------------------------------
#
# These five are the reason a finding here means anything there. Read the
# header; tests/test_local_rig.py holds them against img-boot.sh's own
# invocation so neither can move alone.
RIG_MACHINE=raspi3b
RIG_MEM=1024
RIG_LOGLEVEL=7
RIG_CONSOLE=ttyAMA1
RIG_BLACKLIST=bcm2835_pm_driver_init
# The 24-bit board code the Pi firmware would have put in the device tree
# and QEMU does not. img-boot.sh carries the full derivation of this
# number; the short version is that 0xa02082 is a 1GB Sony UK BCM2837
# 3 Model B rev 1.2, which is the board -M raspi3b claims to be.
RIG_BOARD_REVISION=0xa02082

# Where the versioned kernel comes from. The metapackage indirection is the
# point: `linux-image-rpi-v8` never changes name, and what it depends on is
# whatever the archive currently ships, which is what pi-gen would install.
RIG_ARCHIVE="${OTP_RIG_ARCHIVE:-https://archive.raspberrypi.com/debian}"
RIG_SUITE="${OTP_RIG_SUITE:-bookworm}"
RIG_META="${OTP_RIG_META:-linux-image-rpi-v8}"
# Raspberry Pi OS is Debian bookworm, so bookworm's busybox is the one whose
# behaviour matches what an initramfs on that image would have.
RIG_BUSYBOX_URL="${OTP_RIG_BUSYBOX_URL:-https://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}"

WORK="${OTP_RIG_WORK:-${TMPDIR:-/tmp}/otp-rig}"
# A BACKSTOP, not a target, for the same reason img-boot.sh's is. Every
# probe here stops the emulator on its own DONE marker; this cap is only
# ever paid by a probe that never got there, which is itself the finding.
TIMEOUT="${OTP_RIG_TIMEOUT:-300}"
# How long idle-survive sits still. Long enough to outlast the ~11.5s
# watchdog reset that runs 1-5 hit, with room to see it not happen.
IDLE_SECONDS="${OTP_RIG_IDLE_SECONDS:-45}"

rig_log() { printf '\n== %s\n' "$*" >&2; }
rig_die() { printf 'ERROR: %s\n' "$1" >&2; shift; for l in "$@"; do printf '       %s\n' "$l" >&2; done; exit 1; }

# --- the probe menu, in one place ----------------------------------------
#
# ONE LIST, read by the usage text, by the dispatcher and by
# tests/test_local_rig.py. A menu that can disagree with the kitchen is how
# a rig ends up advertising a probe that was deleted, and the person who
# needed it concludes the rig is broken rather than that the probe is gone.
rig_probes() {
    cat <<'PROBES'
rng
coldplug-replay
console-test
idle-survive
PROBES
}

rig_usage() {
    cat <<USAGE
usage: img-local-rig.sh [--plan] <probe>

Boots the image's exact kernel under QEMU's raspi3b with a busybox
initramfs, runs one probe, and leaves both consoles in the work dir.
No image required; nothing is pushed.

probes:
  rng              crng/entropy state and a blocking getrandom() probe
  coldplug-replay  modprobe every /sys modalias one at a time, with markers
  console-test     which consoles actually registered, and what the DTB says
  idle-survive     sit still for ${IDLE_SECONDS}s and see whether the machine resets

  --plan           check preconditions and print the qemu argv; do not boot

environment:
  OTP_RIG_WORK      work dir (default \${TMPDIR:-/tmp}/otp-rig); downloads cached here
  OTP_RIG_KERNEL    pin the kernel version, e.g. 6.12.96+rpt-rpi-v8
                    (default: resolved from ${RIG_META} in the archive)
  OTP_RIG_BUSYBOX   a static arm64 busybox to use instead of fetching one
  OTP_RIG_TIMEOUT   backstop seconds (default ${TIMEOUT})
  OTP_RIG_IDLE_SECONDS  how long idle-survive waits (default ${IDLE_SECONDS})
USAGE
}

# --- ELF interrogation, because the wrong busybox is a silent failure -----
#
# A busybox for the wrong architecture produces "Run /init as init process"
# and then nothing at all -- indistinguishable, on a serial console, from
# the gzip-kernel failure and from a dozen real faults. It is worth twelve
# lines to say which one it is.
#
# od rather than `file`: file(1) is not in every container this might run
# in, and its output wording is not a stable interface.
rig_elf_u() {  # <file> <offset> <width-in-bytes>
    od -An -tu"$3" -j"$2" -N"$3" "$1" | tr -d ' \n'
}

# ELF64 e_machine lives at offset 18 and EM_AARCH64 is 183. A static binary
# is one with no PT_INTERP (type 3) program header -- checked by walking the
# program header table rather than by grepping for an interpreter path,
# because a static binary may contain that string for other reasons.
rig_require_arm64_static() {
    local f="$1" magic machine phoff phentsize phnum i off ptype
    [ -s "$f" ] || rig_die "$f is missing or empty"
    magic=$(od -An -c -N4 "$f" | tr -d ' \n')
    if [ "$magic" != '177ELF' ]; then
        rig_die "$f is not an ELF binary" \
                "The initramfs has no dynamic loader and no libc, so busybox" \
                "must be a static arm64 ELF. Got magic: '$magic'"
    fi
    machine=$(rig_elf_u "$f" 18 2)
    if [ "$machine" != "183" ]; then
        rig_die "$f is not an arm64 binary (ELF e_machine=$machine, want 183)" \
                "This is the wrong architecture for -M raspi3b. The guest" \
                "would reach 'Run /init as init process' and then say nothing," \
                "which looks exactly like four other faults." \
                "Set OTP_RIG_BUSYBOX to a static arm64 busybox, or unset it" \
                "and let the rig fetch one."
    fi
    phoff=$(rig_elf_u "$f" 32 8)
    phentsize=$(rig_elf_u "$f" 54 2)
    phnum=$(rig_elf_u "$f" 56 2)
    i=0
    while [ "$i" -lt "${phnum:-0}" ]; do
        off=$((phoff + i * phentsize))
        ptype=$(rig_elf_u "$f" "$off" 4)
        if [ "$ptype" = "3" ]; then
            rig_die "$f is dynamically linked (it has a PT_INTERP header)" \
                    "The initramfs carries no dynamic loader, so this busybox" \
                    "cannot execute in it. Use a busybox-static build."
        fi
        i=$((i + 1))
    done
}

# The Pi's own postinst is what turns the deb's gzipped vmlinuz into
# kernel8.img, and QEMU is not it. See the header: the compressed file
# reaches -kernel happily and produces a completely silent boot.
rig_require_arm64_image() {
    local f="$1" magic
    [ -s "$f" ] || rig_die "$f is missing or empty"
    # The arm64 Linux boot protocol puts the 4-byte magic "ARM\x64" at
    # offset 56 of the image header. od -c renders \x64 as the letter d.
    magic=$(od -An -c -j56 -N4 "$f" | tr -d ' \n')
    if [ "$magic" != "ARMd" ]; then
        rig_die "$f is not an uncompressed arm64 kernel Image" \
                "(magic at offset 56 is '$magic', want 'ARMd')" \
                "The archive .deb ships boot/vmlinuz-* GZIPPED; on a real Pi" \
                "the raspi-firmware postinst decompresses it into kernel8.img." \
                "Handing the compressed file to -kernel boots nothing and" \
                "writes nothing to either UART, which is the single most" \
                "expensive symptom to misdiagnose in this whole harness."
    fi
}

# --- resolving the kernel the image would have ---------------------------

# One field of one stanza of a Packages index.
rig_package_field() {  # <packages-file> <package> <field>
    awk -v pkg="$2" -v field="$3" '
        $0 == "Package: " pkg { inpkg = 1; next }
        /^Package: / { inpkg = 0 }
        inpkg && index($0, field ": ") == 1 { print substr($0, length(field) + 3); exit }
    ' "$1"
}

# THE METAPACKAGE INDIRECTION, which is the whole trick and the reason this
# is not a hardcoded version string. `linux-image-rpi-v8` is a 13-byte
# package whose only content is a dependency on whatever versioned kernel
# the archive currently ships -- which is what pi-gen installs, so it is
# what the built image will run. Reading it keeps the rig current for free.
rig_resolve_kernel_version() {  # <packages-file>
    local depends versioned
    depends=$(rig_package_field "$1" "$RIG_META" Depends)
    if [ -z "$depends" ]; then
        rig_die "no Depends for $RIG_META in $1" \
                "Without it there is no way to know which kernel the image" \
                "would run. Pin one with OTP_RIG_KERNEL if the archive moved."
    fi
    # grep -o, not a bare match: the Depends line carries a version
    # constraint in parentheses and may one day carry siblings.
    versioned=$(printf '%s\n' "$depends" \
                | grep -oE 'linux-image-[0-9][^ ,(]*' \
                | head -1 || true)
    if [ -z "$versioned" ]; then
        rig_die "$RIG_META depends on no versioned linux-image" \
                "Depends was: $depends" \
                "This is the indirection the rig resolves; if the archive" \
                "changed its shape, pin the kernel with OTP_RIG_KERNEL."
    fi
    # Strip the package-name prefix to leave the kernel release, which is
    # also the /lib/modules directory name.
    printf '%s\n' "${versioned#linux-image-}"
}

# --- preconditions -------------------------------------------------------
#
# EVERY ONE OF THESE FAILS LOUDLY AND EARLY, and that is the point of the
# function existing at all. This rig's predecessor was a scratch script, and
# a scratch script's response to a missing tool is a confusing error forty
# lines later, or -- worse -- a boot that happens anyway and answers the
# question wrongly.
rig_have_raspi_machine() {
    # grep -c, NOT grep -q, and this script runs under pipefail. `-q` closes
    # the pipe on its first match; the producer dies of SIGPIPE and the
    # pipeline returns 141, which under `set -e` is a failure indistinguishable
    # from "the machine is absent". This repo has been bitten by exactly that
    # twice -- see the lsinitramfs note in device/install.sh and the sampler
    # in img-boot.sh -- and the fact that `-M help` is only a few dozen lines
    # today is not a reason to write the fragile form.
    local n
    n=$(qemu-system-aarch64 -M help 2>/dev/null | grep -cE "^${RIG_MACHINE} " || true)
    [ "${n:-0}" != "0" ]
}

rig_preflight() {  # <probe>
    local probe="$1" tool missing=""
    for tool in qemu-system-aarch64 curl xz gzip cpio dpkg-deb fdtput od; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    # Only coldplug-replay builds a module disk, so only it pays for these.
    # Demanding them from every probe would make the cheap probes fail on a
    # host that could perfectly well run them.
    if [ "$probe" = "coldplug-replay" ]; then
        for tool in depmod mkfs.ext4; do
            command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
        done
    fi
    if [ -n "$missing" ]; then
        rig_die "missing host tools:$missing" \
                "Debian/Ubuntu: apt-get install qemu-system-arm curl xz-utils \\" \
                "                   cpio dpkg device-tree-compiler kmod e2fsprogs" \
                "The rig deliberately vendors nothing (issue #22); these are" \
                "the tools it expects to find."
    fi
    # A qemu-system-aarch64 that exists but has no raspi3b is a real
    # configuration -- some distributions build a reduced machine set -- and
    # it fails deep inside the emulator with a message about the machine
    # name that reads like a typo in this script.
    if ! rig_have_raspi_machine; then
        rig_die "this qemu-system-aarch64 has no '$RIG_MACHINE' machine" \
                "$(qemu-system-aarch64 --version 2>/dev/null | head -1)" \
                "Findings only transfer to tier 3 if the machine is the same" \
                "one img-boot.sh boots. Install a qemu built with the" \
                "Raspberry Pi machines (Debian's qemu-system-arm has them)."
    fi
}

# --- the probes, as heredocs ---------------------------------------------
#
# In this file rather than in a probes/ directory, because the rig's virtue
# is being ONE thing someone actually runs (issue #22). A probe reads
# nothing from the host and writes only to the console.
#
# EVERY PROBE ENDS WITH ITS DONE MARKER and does not try to power the
# machine off: see the header, `poweroff -f` does not stop -M raspi3b. The
# sampler in rig_boot() watches for the marker and kills the emulator.
rig_probe_body() {  # <probe>
    case "$1" in
    rng)
        cat <<'PROBE'
echo "-- /proc/sys/kernel/random:"
for f in entropy_avail poolsize urandom_min_reseed_secs; do
    [ -r "/proc/sys/kernel/random/$f" ] && echo "   $f=$(cat "/proc/sys/kernel/random/$f")"
done
echo "-- hwrng:"
cat /sys/class/misc/hw_random/rng_available 2>/dev/null || echo "   (no hw_random)"
cat /sys/class/misc/hw_random/rng_current 2>/dev/null || true
echo "-- crng line in dmesg:"
dmesg | grep -i 'crng\|random:' || echo "   (nothing)"
# THE BLOCKING PROBE. getrandom() with no flags blocks until the CRNG is
# initialised, which is the thing the entropy theory of issue #17 claimed
# never happened. Reading /dev/random is the same wait in a form busybox
# has: if the crng is not ready this does not return, and the elapsed
# seconds printed after it are the answer.
echo "-- blocking read of /dev/random (this is the entropy question):"
before=$(cut -d' ' -f1 /proc/uptime)
dd if=/dev/random of=/dev/null bs=1 count=16 2>/dev/null
after=$(cut -d' ' -f1 /proc/uptime)
echo "   16 blocking bytes: uptime $before -> $after"
PROBE
        ;;
    coldplug-replay)
        cat <<'PROBE'
# WHAT UDEV WOULD DO, one module at a time, with a marker either side.
# Issue #17 blamed a boot freeze on coldplug for three runs. The value of
# this probe is not that it usually passes -- it is that a module which
# hangs is named by the START line with no matching DONE, so the wedge has
# an address instead of a theory.
mkdir -p /mnt/modules
# The module tree rides on the SD card, which is free here because this rig
# boots no image. mmcblk0 is the whole disk: one ext4 filesystem, no table.
for try in 1 2 3 4 5 6 7 8 9 10; do
    [ -b /dev/mmcblk0 ] && break
    sleep 1
done
if ! mount -t ext4 -o ro /dev/mmcblk0 /mnt/modules 2>/dev/null; then
    echo "   FAIL: could not mount the module disk at /dev/mmcblk0"
    echo "   (block devices: $(ls /dev/mmcblk* 2>/dev/null || echo none))"
else
    echo "   modules: $(ls /mnt/modules/lib/modules 2>/dev/null)"
    total=0
    for m in $(find /sys/devices -name modalias -print 2>/dev/null | sort); do
        alias=$(cat "$m" 2>/dev/null) || continue
        [ -n "$alias" ] || continue
        total=$((total + 1))
        echo "   PROBE-START $total $alias"
        modprobe -d /mnt/modules "$alias" 2>&1 | sed 's/^/     /' || true
        echo "   PROBE-DONE $total $alias"
    done
    echo "   coldplug replayed $total modaliases"
    echo "   loaded: $(wc -l < /proc/modules) modules"
fi
PROBE
        ;;
    console-test)
        cat <<'PROBE'
# THE QUESTION SIX RUNS OF ISSUE #17 GOT WRONG. serial0 and stdout-path in
# this DTB point at the mini-UART, whose bcm2835-aux driver does not probe
# under QEMU, so console=ttyAMA0 named a device that does not exist and
# every "freeze" after journald started was a dead console. Anything that
# changes which port is live changes that answer, so it is worth asking
# directly rather than inferring it from silence.
echo "-- /proc/consoles (the registered ones, C = preferred):"
cat /proc/consoles 2>/dev/null || echo "   (none)"
echo "-- kernel cmdline:"
cat /proc/cmdline
echo "-- serial devices present:"
ls -l /dev/ttyAMA* /dev/ttyS* /dev/console 2>/dev/null || echo "   (none)"
echo "-- what the device tree calls them:"
for a in /proc/device-tree/aliases/serial*; do
    [ -e "$a" ] || continue
    echo "   $(basename "$a") -> $(tr -d '\000' < "$a")"
done
echo "-- stdout-path:"
tr -d '\000' < /proc/device-tree/chosen/stdout-path 2>/dev/null || echo "   (unset)"
echo ""
echo "-- writing a distinct line to each port; whichever appears in which"
echo "   console file is the mapping, and that is the whole finding:"
for t in /dev/ttyAMA0 /dev/ttyAMA1 /dev/ttyS0; do
    [ -e "$t" ] || continue
    echo "OTP-RIG-PORTMARK $t" > "$t" 2>/dev/null \
        && echo "   wrote to $t" || echo "   could not write to $t"
done
PROBE
        ;;
    idle-survive)
        cat <<'PROBE'
# THE WATCHDOG QUESTION. Runs 1-5 reset at ~11.5s guest time wherever the
# boot happened to be, which is the shape of a timer rather than of a
# crash; run 7 bisected it to bcm2835-pm probing QEMU's partial PM model.
# The rig boots with that initcall blacklisted, so surviving past the reset
# point is the re-verification, and a heartbeat is what makes a reset
# visible: the count restarts from 1 instead of continuing.
i=0
while [ "$i" -lt "$OTP_RIG_IDLE_SECONDS" ]; do
    i=$((i + 1))
    echo "   heartbeat $i/$OTP_RIG_IDLE_SECONDS uptime=$(cut -d' ' -f1 /proc/uptime)"
    sleep 1
done
echo "   survived $OTP_RIG_IDLE_SECONDS seconds with no reset"
echo "   (a reset would have restarted this count at 1)"
PROBE
        ;;
    *)
        return 1
        ;;
    esac
}

# --- building the initramfs ----------------------------------------------

rig_build_initramfs() {  # <busybox> <probe> <out>
    local busybox="$1" probe="$2" out="$3" root="$WORK/initramfs"
    rm -rf "$root"
    mkdir -p "$root/bin" "$root/proc" "$root/sys" "$root/dev" "$root/mnt"
    cp "$busybox" "$root/bin/busybox"
    chmod 755 "$root/bin/busybox"
    {
        printf '#!/bin/busybox sh\n'
        # Not `set -e`: a probe that fails half way through should print
        # everything up to the failure and then its DONE marker. An init
        # that dies leaves the sampler waiting for a marker that will never
        # come, and the run pays the full backstop for no evidence.
        printf '/bin/busybox --install -s /bin\n'
        printf 'mount -t proc proc /proc 2>/dev/null\n'
        printf 'mount -t sysfs sysfs /sys 2>/dev/null\n'
        printf 'mount -t devtmpfs devtmpfs /dev 2>/dev/null\n'
        printf 'export OTP_RIG_IDLE_SECONDS=%s\n' "$IDLE_SECONDS"
        printf 'echo "OTP-RIG-BEGIN %s"\n' "$probe"
        # shellcheck disable=SC2016  # $(uname -r) is the GUEST's job, not ours
        printf 'echo "kernel: $(uname -r)"\n'
        rig_probe_body "$probe"
        # The marker the sampler waits for. Printed unconditionally, after
        # whatever the probe did or failed to do.
        printf 'echo "OTP-RIG-DONE %s"\n' "$probe"
        # An idle loop rather than an exit: PID 1 exiting is a kernel panic,
        # which scrolls the evidence off the top of the console. The sampler
        # stops the emulator on the marker above.
        printf 'while true; do sleep 5; done\n'
    } > "$root/init"
    chmod 755 "$root/init"
    ( cd "$root" && find . | cpio -o -H newc --quiet ) | gzip -9 > "$out"
}

# --- the module disk, for coldplug-replay only ---------------------------

rig_build_module_disk() {  # <kernel-root> <release> <out>
    local kroot="$1" release="$2" out="$3" staged="$WORK/moddisk" bytes target
    rm -rf "$staged"
    mkdir -p "$staged/lib/modules"
    cp -a "$kroot/lib/modules/$release" "$staged/lib/modules/$release"
    # DECOMPRESSED, which is what issue #22 asked for and also what makes
    # this work: the archive ships .ko.xz, and a busybox modprobe without
    # xz support fails on every single module -- 1911 identical failures
    # that look like a broken probe rather than a missing feature.
    rig_log "decompressing the module tree (the archive ships .ko.xz)"
    find "$staged/lib/modules/$release" -name '*.ko.xz' -exec xz -d -q {} +
    # HOST-SIDE depmod, because the deb ships no modules.dep at all --
    # measured: the extracted tree has modules.builtin and modules.order and
    # nothing else. Without this, modprobe resolves nothing in the guest.
    depmod -b "$staged" "$release"
    bytes=$(du -sb "$staged" | cut -f1)
    # QEMU's sd interface refuses anything that is not a power of two, the
    # same constraint img-boot.sh pads the card image for. Half again the
    # tree plus 32MiB of filesystem overhead, rounded up.
    target=$((bytes * 3 / 2 + 33554432))
    local size=1048576
    while [ "$size" -lt "$target" ]; do size=$((size * 2)); done
    rig_log "module disk: $bytes bytes of modules into a ${size}-byte ext4 image"
    rm -f "$out"
    truncate -s "$size" "$out"
    mkfs.ext4 -q -F -d "$staged" "$out"
}

# --- the invocation ------------------------------------------------------

# ONE FUNCTION, printing one token per line, used by the real boot and by
# --plan alike. tests/test_local_rig.py calls this and compares the tokens
# against the ones parsed out of img-boot.sh, so "the machines match" is a
# measurement rather than a promise in a comment.
rig_qemu_argv() {  # <kernel> <dtb> <initrd> <console0> <console1> [drive]
    local kernel="$1" dtb="$2" initrd="$3" c0="$4" c1="$5" drive="${6:-}"
    printf '%s\n' \
        qemu-system-aarch64 \
        -M "$RIG_MACHINE" \
        -m "$RIG_MEM" \
        -kernel "$kernel" \
        -dtb "$dtb" \
        -initrd "$initrd" \
        -append "$(rig_append)"
    if [ -n "$drive" ]; then
        printf '%s\n' -drive "file=$drive,if=sd,format=raw"
    fi
    printf '%s\n' \
        -serial "file:$c0" \
        -serial "file:$c1" \
        -display none \
        -no-reboot
}

# The kernel command line. Every token here is one img-boot.sh also passes;
# what img-boot.sh passes and this does not is the image's business -- the
# root device, the overlay tokens, systemd's switches and the imgcheck word,
# none of which mean anything to an initramfs with no systemd and no root.
# tests/test_local_rig.py knows that list by name and goes red if img-boot.sh
# grows a token that is on neither side of it.
rig_append() {
    printf 'rw earlycon loglevel=%s console=%s,115200 initcall_blacklist=%s' \
           "$RIG_LOGLEVEL" "$RIG_CONSOLE" "$RIG_BLACKLIST"
}

rig_boot() {  # <probe> <kernel> <dtb> <initrd> <drive>
    local probe="$1" dir="$WORK/$1" c0 c1 argv rc pid
    rm -rf "$dir"; mkdir -p "$dir"
    c0="$dir/console.log"
    c1="$dir/console-uart1.log"
    : > "$c0"; : > "$c1"
    # BOTH UARTs, separately, and in img-boot.sh's order: the first -serial
    # is the PL011 (ttyAMA1 under this DTB) and the second is the mini-UART
    # where the earlycon bootconsole lives. Capturing only one is how the
    # PL011's stubborn zero bytes went unread for six runs.
    mapfile -t argv < <(rig_qemu_argv "$2" "$3" "$4" "$c0" "$c1" "$5")
    printf '%s\n' "${argv[@]}" > "$dir/qemu-argv.txt"
    rig_log "booting $probe under -M $RIG_MACHINE (emulated; the marker stops it)"
    set +e
    timeout -k 10 "$TIMEOUT" "${argv[@]}" &
    pid=$!
    local elapsed=0 seen stopped=''
    while kill -0 "$pid" 2>/dev/null; do
        sleep 2
        elapsed=$((elapsed + 2))
        # grep -c, not grep -q: see rig_have_raspi_machine. The producer here
        # is a file qemu is still writing, which is precisely the race that
        # returned 141 in img-boot.sh's sampler.
        seen=$(grep -acF "OTP-RIG-DONE $probe" "$c0" 2>/dev/null || true)
        if [ -z "$stopped" ] && [ "${seen:-0}" != "0" ]; then
            stopped=$elapsed
            printf '   %s reported done at %ss wall; draining 2s, stopping qemu\n' \
                   "$probe" "$elapsed" >&2
            sleep 2
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait "$pid"
    rc=$?
    set -e
    printf '%s\n' "$rc" > "$dir/qemu-rc"
    rig_log "$probe: uart0=$(wc -c < "$c0") bytes uart1=$(wc -c < "$c1") bytes rc=$rc"
    if [ -z "$stopped" ]; then
        # NOT silent, and not a hard failure either. A probe that never
        # reported is the interesting case -- it is what a wedge looks like
        # -- but the consoles are the evidence and they are on disk.
        rig_log "$probe NEVER REPORTED: no 'OTP-RIG-DONE $probe' on uart0."
        rig_log "The machine hung, reset, or never reached /init. Read:"
        rig_log "  $c0"
        rig_log "  $c1"
        return 1
    fi
    rig_log "$probe reported after ${stopped}s wall. Consoles:"
    printf '   %s\n   %s\n' "$c0" "$c1" >&2
    return 0
}

# --- main ----------------------------------------------------------------

main() {
    local plan='' probe=''
    while [ $# -gt 0 ]; do
        case "$1" in
            --plan) plan=1 ;;
            -h|--help) rig_usage; return 0 ;;
            -*) rig_usage >&2; rig_die "unknown option: $1" ;;
            *) probe="$1" ;;
        esac
        shift
    done
    if [ -z "$probe" ]; then
        rig_usage >&2
        rig_die "no probe named" "Pick one of: $(rig_probes | tr '\n' ' ')"
    fi
    # REFUSED, not defaulted. A typo'd probe name that quietly ran `rng`
    # would produce a completely plausible console for the wrong question,
    # and this rig exists to be trusted in a hurry.
    if ! rig_probes | grep -qxF "$probe"; then
        rig_die "unknown probe: $probe" "Known probes: $(rig_probes | tr '\n' ' ')"
    fi

    rig_preflight "$probe"
    mkdir -p "$WORK"
    WORK="$(cd "$WORK" && pwd)"

    local release
    if [ -n "${OTP_RIG_KERNEL:-}" ]; then
        release="$OTP_RIG_KERNEL"
        rig_log "kernel pinned by OTP_RIG_KERNEL: $release"
    else
        local packages="$WORK/Packages.gz" plain="$WORK/Packages"
        rig_log "resolving $RIG_META from $RIG_ARCHIVE ($RIG_SUITE)"
        curl -fsSL --retry 3 -o "$packages" \
             "$RIG_ARCHIVE/dists/$RIG_SUITE/main/binary-arm64/Packages.gz" \
            || rig_die "could not fetch the archive's Packages index" \
                       "The rig needs network for this. Pin a version with" \
                       "OTP_RIG_KERNEL=<release> to work offline against a" \
                       "kernel already in $WORK."
        gzip -dc "$packages" > "$plain"
        release=$(rig_resolve_kernel_version "$plain")
        rig_log "$RIG_META currently means $release"
    fi

    local kernel="$WORK/kernel8.img"
    local dtb="$WORK/rig-bcm2710-rpi-3-b.dtb"
    local initrd="$WORK/initrd-$probe.img"
    local drive=""
    [ "$probe" = "coldplug-replay" ] && drive="$WORK/modules.ext4"

    if [ "$plan" = "1" ]; then
        printf 'probe:   %s\n' "$probe"
        printf 'kernel:  %s\n' "$release"
        printf 'work:    %s\n' "$WORK"
        printf 'qemu argv (the paths are the ones a real run produces):\n'
        rig_qemu_argv "$kernel" "$dtb" "$initrd" \
                      "$WORK/$probe/console.log" "$WORK/$probe/console-uart1.log" \
                      "$drive" | sed 's/^/  /'
        return 0
    fi

    # --- the kernel and its device tree ----------------------------------
    #
    # CACHED BY RELEASE, so the second probe of the day and every probe
    # after it does no network at all. That is most of what makes this a
    # seconds-scale tool rather than a minutes-scale one.
    local kroot="$WORK/kernel-$release"
    if [ ! -d "$kroot" ]; then
        local deb="$WORK/linux-image-$release.deb" url
        if [ ! -s "$deb" ]; then
            local plain="$WORK/Packages" fn=""
            [ -s "$plain" ] && fn=$(rig_package_field "$plain" "linux-image-$release" Filename)
            if [ -z "$fn" ]; then
                rig_die "no Filename for linux-image-$release in the archive index" \
                        "A pinned OTP_RIG_KERNEL must name a release the archive" \
                        "still carries, and $WORK/Packages must have been fetched" \
                        "at least once. Unset OTP_RIG_KERNEL to resolve it fresh."
            fi
            url="$RIG_ARCHIVE/$fn"
            rig_log "fetching $url"
            curl -fsSL --retry 3 -o "$deb" "$url" \
                || rig_die "could not fetch $url"
        fi
        rig_log "unpacking $(basename "$deb")"
        rm -rf "$kroot.part"
        mkdir -p "$kroot.part"
        dpkg-deb -x "$deb" "$kroot.part"
        # Renamed only once it is complete, so an interrupted unpack does
        # not leave a half tree that the `-d` test above would accept.
        mv "$kroot.part" "$kroot"
    fi

    # See rig_require_arm64_image: the deb's vmlinuz is gzipped and QEMU is
    # not the postinst that decompresses it.
    if [ ! -s "$kernel" ] || [ "$kroot/boot/vmlinuz-$release" -nt "$kernel" ]; then
        rig_log "decompressing vmlinuz-$release into an arm64 Image"
        gzip -dc "$kroot/boot/vmlinuz-$release" > "$kernel"
    fi
    rig_require_arm64_image "$kernel"

    # THE PLAIN 3-b TREE, for the reason img-boot.sh gives at length: -M
    # raspi3b models the Raspberry Pi 3 Model B, and handing the kernel a
    # device tree describing a B+ the emulator is not providing killed
    # run 1 before console init.
    local src_dtb="$kroot/usr/lib/linux-image-$release/broadcom/bcm2710-rpi-3-b.dtb"
    [ -s "$src_dtb" ] || rig_die "no bcm2710-rpi-3-b.dtb in linux-image-$release" \
                                 "(looked in $(dirname "$src_dtb"))"
    cp "$src_dtb" "$dtb"
    # The board revision the firmware would have supplied and QEMU does not.
    # img-boot.sh does the same thing for the same reason and carries the
    # derivation; without it rpi-eeprom-update dies on an empty BOARD_INFO
    # and gpiozero cannot pick a pin factory.
    fdtput -c "$dtb" /system 2>/dev/null || true
    fdtput -t x "$dtb" /system linux,revision "$RIG_BOARD_REVISION" \
        || rig_die "could not write linux,revision into $dtb"
    local readback
    readback=$(fdtget "$dtb" /system linux,revision 2>/dev/null || true)
    # READ BACK, because a write that did nothing looks exactly like one
    # that worked from here.
    if [ "${readback:-0}" != "$((RIG_BOARD_REVISION))" ]; then
        rig_die "$dtb does not carry linux,revision after the patch" \
                "(read back '${readback:-nothing}', wanted $((RIG_BOARD_REVISION)))"
    fi

    # --- busybox ---------------------------------------------------------
    local busybox
    if [ -n "${OTP_RIG_BUSYBOX:-}" ]; then
        busybox="$OTP_RIG_BUSYBOX"
        rig_log "using the busybox named by OTP_RIG_BUSYBOX: $busybox"
    else
        busybox="$WORK/busybox-arm64"
        if [ ! -s "$busybox" ]; then
            local bbdeb="$WORK/busybox-static-arm64.deb"
            rig_log "fetching $RIG_BUSYBOX_URL"
            curl -fsSL --retry 3 -o "$bbdeb" "$RIG_BUSYBOX_URL" \
                || rig_die "could not fetch a static arm64 busybox" \
                           "Point OTP_RIG_BUSYBOX at one instead."
            rm -rf "$WORK/busybox-x"
            mkdir -p "$WORK/busybox-x"
            dpkg-deb -x "$bbdeb" "$WORK/busybox-x"
            local found=""
            for candidate in "$WORK/busybox-x/bin/busybox" "$WORK/busybox-x/usr/bin/busybox"; do
                [ -f "$candidate" ] || continue
                found="$candidate"
                break
            done
            [ -n "$found" ] || rig_die "no busybox binary inside $bbdeb"
            cp "$found" "$busybox"
        fi
    fi
    # CHECKED, not assumed. The wrong architecture here produces a boot that
    # reaches "Run /init as init process" and then says nothing forever,
    # which is the same console as four unrelated faults.
    rig_require_arm64_static "$busybox"

    # --- the module disk, coldplug-replay only ---------------------------
    if [ -n "$drive" ] && [ ! -s "$drive" ]; then
        rig_build_module_disk "$kroot" "$release" "$drive"
    fi

    rig_log "building the $probe initramfs"
    rig_build_initramfs "$busybox" "$probe" "$initrd"

    rig_boot "$probe" "$kernel" "$dtb" "$initrd" "$drive"
}

[ "${OTP_RIG_LIB_ONLY:-0}" = "1" ] || main "$@"

#!/usr/bin/env bash
#
# The local repro rig: tier 3's boot environment, without tier 3's image.
#
#   ./harness/img-local-rig.sh rng
#   ./harness/img-local-rig.sh coldplug-replay
#   ./harness/img-local-rig.sh --plan console-test
#
# Boots the kernel and device tree from the same Raspberry Pi archive .deb
# pi-gen would install, with a busybox initramfs whose /init is one of the
# probes below. No image is built or needed, so kernel-level and
# early-userspace questions cost seconds instead of a tier-3 round trip.
#
# Findings transfer only if the machine matches img-boot.sh's: the machine
# flags and kernel parameters are shared, and tests/test_local_rig.py
# parses both scripts and fails if they drift.
#
# Not a check and not wired into CI (issue #22). It says nothing about the
# overlay, the unit, CUPS, the front panel or provisioning.
#
# Host requirements for a real run: qemu-system-aarch64 with raspi3b, curl,
# xz, gzip, cpio, dpkg-deb, fdtput/fdtget (device-tree-compiler), and for
# coldplug-replay depmod (kmod) and mkfs.ext4 (e2fsprogs). A static arm64
# busybox is fetched unless OTP_RIG_BUSYBOX names one. Downloads are cached
# in the work directory. `--plan` needs none of the run tools: only curl
# and gzip to resolve the release, and OTP_RIG_KERNEL removes even that.

set -euo pipefail

# --- the machine, shared with img-boot.sh --------------------------------
#
# tests/test_local_rig.py holds these against img-boot.sh's invocation.
RIG_MACHINE=raspi3b
RIG_MEM=1024
RIG_LOGLEVEL=7
RIG_CONSOLE=ttyAMA1
RIG_BLACKLIST=bcm2835_pm_driver_init
# The board code the Pi firmware would put in the device tree and QEMU
# does not: 0xa02082 is a 1GB 3 Model B rev 1.2, the board -M raspi3b
# claims to be. img-boot.sh carries the derivation.
RIG_BOARD_REVISION=0xa02082

# The metapackage never changes name; it depends on whatever versioned
# kernel the archive currently ships, which is what pi-gen installs.
RIG_ARCHIVE="${OTP_RIG_ARCHIVE:-https://archive.raspberrypi.com/debian}"
RIG_SUITE="${OTP_RIG_SUITE:-bookworm}"
RIG_META="${OTP_RIG_META:-linux-image-rpi-v8}"
# Raspberry Pi OS is Debian bookworm, so bookworm's busybox matches.
RIG_BUSYBOX_URL="${OTP_RIG_BUSYBOX_URL:-https://deb.debian.org/debian/pool/main/b/busybox/busybox-static_1.35.0-4+deb12u1+b1_arm64.deb}"

WORK="${OTP_RIG_WORK:-${TMPDIR:-/tmp}/otp-rig}"
# A backstop, not a target: every probe stops the emulator on its own DONE
# marker.
TIMEOUT="${OTP_RIG_TIMEOUT:-300}"
# Long enough to outlast the ~11.5s watchdog reset the blacklisted
# bcm2835-pm initcall would cause.
IDLE_SECONDS="${OTP_RIG_IDLE_SECONDS:-45}"

rig_log() { printf '\n== %s\n' "$*" >&2; }
rig_die() { printf 'ERROR: %s\n' "$1" >&2; shift; for l in "$@"; do printf '       %s\n' "$l" >&2; done; exit 1; }

# --- the probe menu, in one place ----------------------------------------
#
# One list, read by the usage text, the dispatcher and
# tests/test_local_rig.py.
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

  --plan           print the qemu argv and exit; boots nothing, and needs
                   no emulator installed to say what it would run

environment:
  OTP_RIG_WORK      work dir (default \${TMPDIR:-/tmp}/otp-rig); downloads cached here
  OTP_RIG_KERNEL    pin the kernel version, e.g. 6.12.96+rpt-rpi-v8
                    (default: resolved from ${RIG_META} in the archive)
  OTP_RIG_BUSYBOX   a static arm64 busybox to use instead of fetching one
  OTP_RIG_TIMEOUT   backstop seconds (default ${TIMEOUT})
  OTP_RIG_IDLE_SECONDS  how long idle-survive waits (default ${IDLE_SECONDS})

exit status -- it means "the probe did its work", not "something ran":
  0  the probe reported, and did its work
  2  the probe reported, and said its work did not happen. Read the
     FAIL lines on uart0 before believing anything else it printed
  1  no marker at all: the guest hung, reset, or never reached /init.
     A different problem, so a different code
USAGE
}

# --- ELF interrogation, because the wrong busybox is a silent failure -----
#
# A wrong-architecture busybox prints "Run /init as init process" and then
# nothing, like several real faults. od, not `file`: file(1) is not in
# every container and its wording is not a stable interface.
rig_elf_u() {  # <file> <offset> <width-in-bytes>
    od -An -tu"$3" -j"$2" -N"$3" "$1" | tr -d ' \n'
}

# ELF64 e_machine is at offset 18 and EM_AARCH64 is 183. Static means no
# PT_INTERP (type 3) program header; a static binary may still contain an
# interpreter path as a string.
rig_require_arm64_static() {
    local f="$1" magic machine phoff phentsize phnum i off ptype bytes
    [ -s "$f" ] || rig_die "$f is missing or empty"
    # od refuses to skip past the end of its input, so check the size first.
    bytes=$(wc -c < "$f")
    if [ "$bytes" -lt 64 ]; then
        rig_die "$f is $bytes bytes, too short to be an ELF64 binary" \
                "(the header alone is 64 bytes)"
    fi
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

# The archive .deb ships vmlinuz gzipped; on a Pi the raspi-firmware
# postinst decompresses it. Handed to -kernel compressed, it boots silently.
rig_require_arm64_image() {
    local f="$1" magic bytes
    [ -s "$f" ] || rig_die "$f is missing or empty"
    # Size first: od refuses to skip past the end of its input, and under
    # `set -e` its complaint would replace this function's.
    bytes=$(wc -c < "$f")
    if [ "$bytes" -lt 64 ]; then
        rig_die "$f is $bytes bytes, too short to be an arm64 kernel Image" \
                "(the boot-protocol header alone is 64 bytes)" \
                "A gzipped vmlinuz out of the archive .deb lands here when it" \
                "compresses small; see below for why that matters."
    fi
    # The arm64 boot protocol puts the magic "ARM\x64" at offset 56; od -c
    # renders \x64 as the letter d.
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

# The release the metapackage currently depends on, which is what the
# built image will run.
rig_resolve_kernel_version() {  # <packages-file>
    local depends versioned
    depends=$(rig_package_field "$1" "$RIG_META" Depends)
    if [ -z "$depends" ]; then
        rig_die "no Depends for $RIG_META in $1" \
                "Without it there is no way to know which kernel the image" \
                "would run. Pin one with OTP_RIG_KERNEL if the archive moved."
    fi
    # grep -o: the Depends line carries a version constraint in parentheses
    # and may carry siblings.
    versioned=$(printf '%s\n' "$depends" \
                | grep -oE 'linux-image-[0-9][^ ,(]*' \
                | head -1 || true)
    if [ -z "$versioned" ]; then
        rig_die "$RIG_META depends on no versioned linux-image" \
                "Depends was: $depends" \
                "This is the indirection the rig resolves; if the archive" \
                "changed its shape, pin the kernel with OTP_RIG_KERNEL."
    fi
    # The kernel release is also the /lib/modules directory name.
    printf '%s\n' "${versioned#linux-image-}"
}

# --- preconditions -------------------------------------------------------
#
# Each tool list is named for the path that runs it and demanded where that
# path begins, so --plan works on a host with no emulator.

#: Everything a real run shells out to, including fdtget for the DTB
#: readback, od for the ELF and Image checks and timeout for the backstop.
#: tests/test_local_rig.py checks that every tool used is declared.
RIG_BOOT_TOOLS=(qemu-system-aarch64 curl xz gzip cpio dpkg-deb fdtput fdtget
                od timeout)
#: Only coldplug-replay builds a module disk. See rig_preflight.
RIG_COLDPLUG_TOOLS=(depmod mkfs.ext4 truncate)
#: Resolving the kernel release from the archive, which --plan also does.
RIG_RESOLVE_TOOLS=(curl gzip)

rig_require_tools() {  # <tool>...
    local tool missing=""
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    if [ -n "$missing" ]; then
        rig_die "missing host tools:$missing" \
                "Debian/Ubuntu: apt-get install qemu-system-arm curl xz-utils \\" \
                "                   cpio dpkg device-tree-compiler kmod e2fsprogs" \
                "The rig deliberately vendors nothing (issue #22); these are" \
                "the tools it expects to find."
    fi
}

rig_have_raspi_machine() {
    # grep -c, not grep -q: under pipefail, -q closing the pipe early kills
    # the producer with SIGPIPE and the pipeline returns 141.
    local n
    n=$(qemu-system-aarch64 -M help 2>/dev/null | grep -cE "^${RIG_MACHINE} " || true)
    [ "${n:-0}" != "0" ]
}

# What a real run of <probe> needs. Not reached by --plan.
rig_preflight() {  # <probe>
    local probe="$1" tools=("${RIG_BOOT_TOOLS[@]}")
    if [ "$probe" = "coldplug-replay" ]; then
        tools+=("${RIG_COLDPLUG_TOOLS[@]}")
    fi
    rig_require_tools "${tools[@]}"
    # Some distributions build qemu-system-aarch64 with a reduced machine
    # set, and the emulator's own error reads like a typo in this script.
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
# A probe reads nothing from the host and writes only to the console. Each
# ends with its DONE marker rather than powering off: `poweroff -f` does not
# stop -M raspi3b, so the sampler in rig_boot() kills the emulator.
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
# Reading /dev/random blocks until the CRNG is initialised, so the
# elapsed uptime printed after it is the answer.
echo "-- blocking read of /dev/random (this is the entropy question):"
before=$(cut -d' ' -f1 /proc/uptime)
# The exit status and stderr are kept: img-boot.sh and harness/README.md
# cite this measurement, so a read that never happened must fail.
# Substitution, not a pipe: busybox ash has no PIPESTATUS.
err=$(dd if=/dev/random of=/dev/null bs=1 count=16 2>&1)
rc=$?
after=$(cut -d' ' -f1 /proc/uptime)
if [ "$rc" = "0" ]; then
    echo "   16 blocking bytes: uptime $before -> $after"
else
    rig_fail "the blocking read did not happen (dd rc=$rc)." \
             "uptime $before -> $after is the cost of failing, not an" \
             "entropy figure, and must not be quoted as one." \
             "$err"
fi
PROBE
        ;;
    coldplug-replay)
        cat <<'PROBE'
# What udev would do, one module at a time, with a marker either side: a
# module that hangs is named by a START line with no matching DONE.
# MODDIR is overridable only so the tests can run this body on a host.
MODDIR="${RIG_MODDIR:-/mnt/modules}"
mkdir -p "$MODDIR"
# mmcblk0 is the whole disk: one ext4 filesystem, no partition table.
for try in 1 2 3 4 5 6 7 8 9 10; do
    [ -b /dev/mmcblk0 ] && break
    sleep 1
done
if ! mount -t ext4 -o ro /dev/mmcblk0 "$MODDIR" 2>/dev/null; then
    rig_fail "could not mount the module disk at /dev/mmcblk0" \
             "(block devices: $(ls /dev/mmcblk* 2>/dev/null || echo none))"
else
    # /lib/modules, not `modprobe -d`: busybox's modprobe has no -d option.
    mkdir -p /lib
    ln -sf "$MODDIR/lib/modules" /lib/modules
    release=$(uname -r)
    echo "   modules: $(ls "$MODDIR/lib/modules" 2>/dev/null)"
    if [ ! -f "$MODDIR/lib/modules/$release/modules.dep" ]; then
        rig_fail "no modules.dep for $release -- modprobe can resolve nothing," \
                 "so every probe below would 'succeed' without doing anything." \
                 "(the host-side depmod in rig_build_module_disk is what makes it)"
    else
        echo "   modules.dep: $(wc -l < "$MODDIR/lib/modules/$release/modules.dep") lines"
        total=0; failed=0
        for m in $(find /sys/devices -name modalias -print 2>/dev/null | sort); do
            alias=$(cat "$m" 2>/dev/null) || continue
            [ -n "$alias" ] || continue
            total=$((total + 1))
            echo "   PROBE-START $total $alias"
            # "No such module" is a legitimate answer for an alias with no
            # driver; every probe failing is a broken invocation. Substitution,
            # not a pipe into sed: busybox ash has no PIPESTATUS.
            out=$(modprobe -q "$alias" 2>&1)
            rc=$?
            [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/     /'
            [ "$rc" = "0" ] || failed=$((failed + 1))
            echo "   PROBE-DONE $total $alias rc=$rc"
        done
        echo "   coldplug replayed $total modaliases, $failed of them non-zero"
        echo "   loaded: $(wc -l < /proc/modules) modules now in /proc/modules"
        if [ "$total" = "0" ]; then
            # Zero probes is /sys not mounted or a find that matched nothing,
            # not a clean replay.
            rig_fail "no modalias files under /sys/devices at all, so this" \
                     "replayed NOTHING. Zero probes and zero failures is not" \
                     "a clean coldplug -- check that /sys mounted."
        elif [ "$failed" = "$total" ]; then
            rig_fail "EVERY probe returned non-zero. That is a broken" \
                     "invocation, not a clean coldplug -- read the lines above" \
                     "before believing the count."
        fi
    fi
fi
PROBE
        ;;
    console-test)
        cat <<'PROBE'
# Which console is live. serial0 and stdout-path in this DTB point at the
# mini-UART, whose bcm2835-aux driver does not probe under QEMU, so
# console=ttyAMA0 names a device that does not exist.
echo "-- /proc/consoles (the registered ones, C = preferred):"
# Unreadable means /proc did not mount, not that no console registered.
cat /proc/consoles 2>/dev/null \
    || rig_fail "/proc/consoles is unreadable, so nothing below can say which" \
                "console registered. /proc did not mount; this is not a" \
                "finding about consoles."
echo "-- kernel cmdline:"
cat /proc/cmdline || rig_fail "/proc/cmdline is unreadable (see above)"
echo "-- tty device nodes devtmpfs actually made:"
# A loop, not `ls /dev/ttyAMA*`: with a partial match busybox ls lists what
# exists and still exits nonzero.
found_tty=""
for t in /dev/tty[A-Z]*[0-9] /dev/ttyS[0-9]; do
    [ -e "$t" ] || continue
    found_tty="yes"
    echo "   $(ls -l "$t")"
done
# A finding, not a fault, so not rig_fail: a console can be registered in
# /proc/consoles (ttyAMA1 here) with no character device in /dev.
[ -n "$found_tty" ] || echo "   (no serial tty nodes at all)"
echo "   /dev/console: $(ls -l /dev/console 2>/dev/null || echo absent)"
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
for t in /dev/ttyAMA0 /dev/ttyAMA1 /dev/ttyS0 /dev/console; do
    if [ ! -e "$t" ]; then
        echo "   $t does not exist"
        continue
    fi
    if echo "OTP-RIG-PORTMARK $t" > "$t" 2>/dev/null; then
        echo "   wrote to $t"
    else
        echo "   could not write to $t"
    fi
done
PROBE
        ;;
    idle-survive)
        cat <<'PROBE'
# The rig boots with the bcm2835-pm initcall blacklisted, so surviving past
# the ~11.5s watchdog reset re-verifies that fix. A reset shows as the
# heartbeat count restarting from 1.
i=0
start=$(cut -d' ' -f1 /proc/uptime)
while [ "$i" -lt "$OTP_RIG_IDLE_SECONDS" ]; do
    i=$((i + 1))
    echo "   heartbeat $i/$OTP_RIG_IDLE_SECONDS uptime=$(cut -d' ' -f1 /proc/uptime)"
    sleep 1
done
end=$(cut -d' ' -f1 /proc/uptime)
# Measured from uptime, not counted: a sleep that returns immediately would
# finish the loop without waiting. Whole-second truncation can lose just
# under one second, hence N-1.
elapsed=$(( ${end%%.*} - ${start%%.*} ))
if [ "$elapsed" -ge $(( OTP_RIG_IDLE_SECONDS - 1 )) ]; then
    echo "   survived $OTP_RIG_IDLE_SECONDS seconds with no reset"
    echo "   (uptime $start -> $end; a reset would have restarted this count at 1)"
else
    rig_fail "the loop ran to $OTP_RIG_IDLE_SECONDS but only ${elapsed}s of" \
             "uptime passed (uptime $start -> $end). Nothing waited, so" \
             "nothing survived: this is a sleep that is not sleeping, not a" \
             "machine that stayed up."
fi
PROBE
        ;;
    *)
        return 1
        ;;
    esac
}

# --- the init the guest runs ---------------------------------------------
#
# Prints the script rather than writing the file, so the tests can `sh -n`
# and run every probe's init without an emulator; shellcheck reads the
# quoted heredocs above as data.
rig_init_script() {  # <probe>
    local probe="$1"
    # Not `set -e`: a probe that fails half way must still print its DONE
    # marker, or the run pays the full backstop.
    cat <<'INIT_HEAD'
#!/bin/busybox sh
/bin/busybox --install -s /bin

# Whether the probe did its work, in a file so a failure reported from a
# subshell still counts. Overridable only so the tests can run this script
# on a host.
RIG_FAILED="${RIG_FAILED:-/rig-failed}"
rm -f "$RIG_FAILED"

# The one way a probe reports failure; a printed FAIL line alone leaves
# the rig's exit status 0.
rig_fail() {
    echo "   FAIL: $1"
    shift
    for l in "$@"; do echo "   $l"; done
    echo failed > "$RIG_FAILED"
}

# Mount errors are reported, not suppressed: every probe reads these
# filesystems. Busy is tolerated for a kernel that mounted /dev itself
# (CONFIG_DEVTMPFS_MOUNT).
rig_mount() {  # <type> <dir>
    out=$(mount -t "$1" "$1" "$2" 2>&1)
    rc=$?
    if [ "$rc" = "0" ]; then
        return 0
    fi
    case "$out" in
    *"resource busy"*|*"Resource busy"*|*"esource or device busy"*)
        echo "   ($2 was already mounted; continuing)"
        return 0
        ;;
    esac
    rig_fail "could not mount $1 on $2 (rc=$rc)" \
             "$out" \
             "Everything below reads $2, so the probe's answers would be" \
             "about a filesystem that is not there."
    return 1
}

rig_mount proc /proc
rig_mount sysfs /sys
rig_mount devtmpfs /dev
INIT_HEAD
    printf 'export OTP_RIG_IDLE_SECONDS=%s\n' "$IDLE_SECONDS"
    printf 'echo "OTP-RIG-BEGIN %s"\n' "$probe"
    # shellcheck disable=SC2016  # $(uname -r) is the GUEST's job, not ours
    printf 'echo "kernel: $(uname -r)"\n'
    rig_probe_body "$probe"
    # The marker the sampler waits for: unconditional, so a failed probe
    # still ends the run, and carrying the probe's status.
    # shellcheck disable=SC2016  # $RIG_FAILED is the GUEST's variable
    printf 'if [ -f "$RIG_FAILED" ]; then rig_status=fail; else rig_status=ok; fi\n'
    # shellcheck disable=SC2016  # $rig_status is the GUEST's variable
    printf 'echo "OTP-RIG-DONE %s status=$rig_status"\n' "$probe"
    # An idle loop, not an exit: PID 1 exiting is a kernel panic, which
    # scrolls the evidence off the console.
    printf 'while true; do sleep 5; done\n'
}

# --- building the initramfs ----------------------------------------------

rig_build_initramfs() {  # <busybox> <probe> <out>
    local busybox="$1" probe="$2" out="$3" root="$WORK/initramfs"
    rm -rf "$root"
    mkdir -p "$root/bin" "$root/proc" "$root/sys" "$root/dev" "$root/mnt"
    cp "$busybox" "$root/bin/busybox"
    chmod 755 "$root/bin/busybox"
    rig_init_script "$probe" > "$root/init"
    chmod 755 "$root/init"
    ( cd "$root" && find . | cpio -o -H newc --quiet ) | gzip -9 > "$out"
}

# --- the module disk, for coldplug-replay only ---------------------------

rig_build_module_disk() {  # <kernel-root> <release> <out>
    local kroot="$1" release="$2" out="$3" staged="$WORK/moddisk" bytes target
    rm -rf "$staged"
    mkdir -p "$staged/lib/modules"
    cp -a "$kroot/lib/modules/$release" "$staged/lib/modules/$release"
    # Decompressed: the archive ships .ko.xz and busybox modprobe has no xz
    # support.
    rig_log "decompressing the module tree (the archive ships .ko.xz)"
    find "$staged/lib/modules/$release" -name '*.ko.xz' -exec xz -d -q {} +
    # Host-side depmod: the deb ships no modules.dep.
    depmod -b "$staged" "$release"
    bytes=$(du -sb "$staged" | cut -f1)
    # QEMU's sd interface requires a power-of-two size. Half again the tree
    # plus 32MiB of filesystem overhead, rounded up.
    target=$((bytes * 3 / 2 + 33554432))
    local size=1048576
    while [ "$size" -lt "$target" ]; do size=$((size * 2)); done
    rig_log "module disk: $bytes bytes of modules into a ${size}-byte ext4 image"
    rm -f "$out"
    truncate -s "$size" "$out"
    mkfs.ext4 -q -F -d "$staged" "$out"
}

# --- the invocation ------------------------------------------------------

# One token per line, used by the real boot and by --plan.
# tests/test_local_rig.py compares these tokens against img-boot.sh's.
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

# The kernel command line. Every token is one img-boot.sh also passes; the
# ones it omits (root device, overlay, systemd switches, the imgcheck word)
# mean nothing to an initramfs. tests/test_local_rig.py knows that list by
# name.
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
    # Both UARTs, in img-boot.sh's order: the first -serial is the PL011
    # (ttyAMA1 under this DTB), the second the mini-UART where the earlycon
    # bootconsole lives.
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
        # -a: a UART file with a stray byte in it is "binary" to grep, which
        # would report that instead of counting.
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
    # Exit status:
    #   1  no marker at all: the guest hung, reset, or never reached /init
    #   2  the probe reported failure
    #   0  the probe reported, and did its work
    if [ -z "$stopped" ]; then
        # NOT silent: the consoles are the evidence and they are on disk.
        rig_log "$probe NEVER REPORTED: no 'OTP-RIG-DONE $probe' on uart0."
        rig_log "The machine hung, reset, or never reached /init. Read:"
        rig_log "  $c0"
        rig_log "  $c1"
        return 1
    fi
    # -a as in the sampler: this file came off a UART.
    local ok
    ok=$(grep -acF "OTP-RIG-DONE $probe status=ok" "$c0" 2>/dev/null || true)
    if [ "${ok:-0}" = "0" ]; then
        rig_log "$probe REPORTED FAILURE after ${stopped}s wall."
        rig_log "The probe ran and said its work did not happen, so nothing"
        rig_log "else it printed is an answer. Read the FAIL lines on uart0:"
        printf '   %s\n   %s\n' "$c0" "$c1" >&2
        return 2
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
    # Refused, not defaulted: a typo'd probe name must not quietly run
    # another probe.
    if ! rig_probes | grep -qxF "$probe"; then
        rig_die "unknown probe: $probe" "Known probes: $(rig_probes | tr '\n' ' ')"
    fi

    # Preconditions belong to a real run; --plan boots nothing and is not
    # checked. Two rows in tests/mutations.toml pin this `if`.
    if [ "$plan" != "1" ]; then
        rig_preflight "$probe"
    fi
    mkdir -p "$WORK"
    WORK="$(cd "$WORK" && pwd)"

    local release
    if [ -n "${OTP_RIG_KERNEL:-}" ]; then
        release="$OTP_RIG_KERNEL"
        rig_log "kernel pinned by OTP_RIG_KERNEL: $release"
    else
        local packages="$WORK/Packages.gz" plain="$WORK/Packages"
        # Demanded here, not in rig_preflight: resolving the release is the one
        # piece of real work --plan does.
        rig_require_tools "${RIG_RESOLVE_TOOLS[@]}"
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
    # Cached by release, so later probes do no network.
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
        # Renamed only once complete, so an interrupted unpack does not leave
        # a half tree the `-d` test above would accept.
        mv "$kroot.part" "$kroot"
    fi

    # See rig_require_arm64_image: the deb's vmlinuz is gzipped.
    if [ ! -s "$kernel" ] || [ "$kroot/boot/vmlinuz-$release" -nt "$kernel" ]; then
        rig_log "decompressing vmlinuz-$release into an arm64 Image"
        gzip -dc "$kroot/boot/vmlinuz-$release" > "$kernel"
    fi
    rig_require_arm64_image "$kernel"

    # The plain 3-b tree: -M raspi3b models the Raspberry Pi 3 Model B, and a
    # B+ device tree kills the boot before console init.
    local src_dtb="$kroot/usr/lib/linux-image-$release/broadcom/bcm2710-rpi-3-b.dtb"
    [ -s "$src_dtb" ] || rig_die "no bcm2710-rpi-3-b.dtb in linux-image-$release" \
                                 "(looked in $(dirname "$src_dtb"))"
    cp "$src_dtb" "$dtb"
    # The board revision the firmware would have supplied. Without it
    # rpi-eeprom-update dies on an empty BOARD_INFO and gpiozero cannot pick
    # a pin factory.
    fdtput -c "$dtb" /system 2>/dev/null || true
    fdtput -t x "$dtb" /system linux,revision "$RIG_BOARD_REVISION" \
        || rig_die "could not write linux,revision into $dtb"
    local readback
    readback=$(fdtget "$dtb" /system linux,revision 2>/dev/null || true)
    # Read back: a write that did nothing looks the same from here.
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
    # See rig_require_arm64_static.
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

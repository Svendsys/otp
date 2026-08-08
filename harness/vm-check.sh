#!/usr/bin/env bash
#
# Tier 2: boot a real system, provision it with device/install.sh, and see
# whether the unit actually comes up.
#
#   ./harness/vm-check.sh
#
# WHY A VM AND NOT A CONTAINER. The riskiest untested change in this
# repository is otp-unit.service binding tty1 -- StandardInput=tty-force,
# TTYPath=/dev/tty1, Conflicts=getty@tty1.service. If that is wrong the
# unit restart-loops instead of starting, and nothing else will say so:
# pi-gen never boots the image it builds, containers have no virtual
# terminals at all, and the unit tests substitute systemd entirely. A VM
# is the cheapest thing with real VTs.
#
# WHY NOT ARM64. Nothing being tested here is architecture-specific --
# systemd's console handling, CUPS's config, swap, the journal. Emulating
# arm64 on an x86 host costs half an hour per run and buys none of it,
# where amd64 with KVM is a few minutes and can therefore run per commit
# instead of never. The arm64-specific half is already covered elsewhere:
# the image build runs install.sh in a real arm64 chroot, and tier 3 boots
# the actual image. This tier is about what happens AFTER boot.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="${OTP_VM_WORK:-${TMPDIR:-/tmp}/otp-vm}"
# A leading ~ arrives literally from a YAML env: block -- nothing expands
# it there, and nothing expands it in a variable either. Left alone it
# creates a directory actually named "~", and qemu-img then resolves the
# backing file relative to the overlay and asks for ~/otp-vm/~/otp-vm/...
TILDE='~'                                       # literal, not a path to expand
if [ "${WORK#"$TILDE"/}" != "$WORK" ]; then
    WORK="$HOME/${WORK#"$TILDE"/}"
fi
mkdir -p "$WORK"
# Absolute from here on. qemu-img resolves a relative backing path against
# the overlay's own directory, not the working directory.
WORK="$(cd "$WORK" && pwd)"
# Debian 13 (trixie) is what the unit ships on, so the systemd and CUPS
# under test are the versions the device will really have. The `generic`
# image rather than `genericcloud`: the cloud kernel drops drivers this
# needs, including the serial console the whole check reports through.
IMAGE_URL="${OTP_VM_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2}"
BASE="$WORK/base.qcow2"
# Three boots now, not one, each with a 45s settle inside it. Under KVM the
# whole run is around eight minutes; this is headroom, not an estimate.
#
# Deliberately WELL under the workflow's timeout-minutes (45). The gap is
# not slack, it is the budget for the failure path: a job killed by
# GitHub's timeout skips its remaining steps, so the console dump and the
# verdict would never run and the diagnosis would be lost. This timeout has
# to fire first, every time.
#
# 600, down from 1800, and this is a POLICY not an estimate. CI has a
# ten-minute budget; three boots of a healthy guest take under four
# minutes, measured. Anything that needs more than ten is either broken or
# has become too slow to keep, and both deserve a red run rather than a
# quiet twenty-minute drift.
#
# The earlier numbers were set by what a hang happened to cost: 1500, then
# 2400 for three boots, then 1800 because a dead VM took forty minutes to
# admit it. Sizing a timeout to the worst failure observed is how a
# ten-minute job becomes a forty-minute one nobody notices.
BOOT_TIMEOUT="${OTP_VM_TIMEOUT:-600}"

log() { printf '\n== %s\n' "$*" >&2; }

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "ERROR: $1 is not installed" >&2
        echo "  apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils" >&2
        exit 1
    }
}

need qemu-system-x86_64
need qemu-img
need cloud-localds

# --- the base image, cached ---------------------------------------------

if [ ! -f "$BASE" ]; then
    log "Fetching $IMAGE_URL"
    curl -fsSL --retry 3 -o "$BASE.part" "$IMAGE_URL"
    mv "$BASE.part" "$BASE"
fi

# Never boot the base directly; an overlay keeps it reusable across runs.
rm -f "$WORK/overlay.qcow2"
qemu-img create -q -f qcow2 -b "$BASE" -F qcow2 "$WORK/overlay.qcow2" 12G

# --- the repository, as a disk ------------------------------------------

# A tar handed to the guest as a raw block device. Simpler than 9p or
# virtiofs and it needs no filesystem driver on either side: tar reads
# straight from /dev/vdb and stops at the archive's end marker, ignoring
# the padding QEMU rounds the device up to.
log "Packing the repository"
tar -C "$REPO" --exclude=.git --exclude='image/pi-gen' --exclude='__pycache__' \
    -cf "$WORK/repo.tar" .
truncate -s %512 "$WORK/repo.tar"

# --- a boot partition, on its own disk -----------------------------------

# The Pi keeps /boot/firmware on a separate FAT partition, and raspi-config's
# overlay leaves it alone -- which is the only reason settings can persist on
# a device whose root filesystem discards every write. Reproducing that
# geometry is the point: a /boot/firmware that is just a directory on the
# root would be swallowed by the overlay, and the persistence checks would
# pass in the guest for a reason that does not hold on the device.
#
# Formatted in the guest rather than here, so the host needs no mtools and
# no loop mounts.
rm -f "$WORK/bootpart.img"
truncate -s 64M "$WORK/bootpart.img"

# --- what the guest does on first boot ----------------------------------

cat > "$WORK/user-data" <<'EOF'
#cloud-config
# The packages install.sh would apt-get, minus the ones that exist only in
# Raspberry Pi OS. python3-lgpio is the notable absence: it comes from
# archive.raspberrypi.org. Its absence is not a problem for this tier and
# is arguably the more interesting case -- a unit with no working GPIO must
# fall through to printing unattended rather than failing to start, which
# is precisely what is being checked.
packages:
  - python3-reportlab
  - python3-pil
  - python3-gpiozero
  - python3-smbus2
  - cups
  - cups-client
  - cups-filters
  - rng-tools5
  - dosfstools
package_update: true

write_files:
  # Runs on every boot AFTER the first. cloud-init's runcmd only fires once,
  # so the second and third boots need their own entry point -- and the
  # second and third boots are the entire point of this tier.
  - path: /usr/local/sbin/otp-harness-boot
    permissions: "0755"
    content: |
      #!/bin/sh
      # The phase lives on the boot partition, not on the root filesystem.
      # On the device the root discards every write, so state that has to
      # survive a reboot cannot live there -- and the harness should not
      # depend on something the device would not give it.
      set -u
      STATE=/boot/firmware/otp-harness-phase
      mount -o remount,rw /boot/firmware 2>/dev/null || true
      PHASE=$(cat "$STATE" 2>/dev/null || echo reboot)
      case "$PHASE" in
        reboot|persist) ;;
        *) echo "OTP-GUEST-DONE $PHASE (nothing to do)"; exit 0 ;;
      esac
      /repo/harness/vm-guest-check.sh "$PHASE"
      echo "OTP-GUEST-DONE $PHASE"
      mount -o remount,rw /boot/firmware 2>/dev/null || true
      if [ "$PHASE" = "reboot" ]; then
        echo persist > "$STATE"
        sync
        systemctl reboot
      else
        echo done > "$STATE"
        sync
        systemctl poweroff
      fi
  - path: /etc/systemd/system/otp-harness-boot.service
    content: |
      [Unit]
      Description=Tier 2 harness checks for this boot
      # After the unit, so is-active means something, and after CUPS so the
      # print path is up. Wants= rather than Requires=: a unit that failed
      # to start is the single most important thing this can report, and a
      # hard dependency would stop the report from ever running.
      After=multi-user.target otp-unit.service cups.service
      Wants=otp-unit.service
      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/otp-harness-boot
      StandardOutput=journal+console
      StandardError=journal+console
      TimeoutStartSec=600
      [Install]
      WantedBy=multi-user.target

runcmd:
  # By serial, not /dev/vdb. The kernel numbers virtio disks in probe
  # order, and adding an explicit -device for the root disk shifted this
  # one from vdb to vdd -- so this untarred the blank boot partition, /repo
  # was empty, and install.sh exited 127 for want of existing.
  - [ sh, -c, "REPO=$(readlink -f /dev/disk/by-id/virtio-otprepo); echo \"repo disk: $REPO\"; mkdir -p /repo && tar -C /repo -xf \"$REPO\" && ls /repo/device/install.sh" ]
  - [ sh, -c, "chmod +x /repo/device/install.sh /repo/harness/vm-guest-check.sh" ]
  # The boot partition, formatted and mounted before install.sh runs so the
  # unit's settings land on it from the start. Found by serial rather than
  # by /dev/vdN: the kernel names virtio disks in probe order, and adding a
  # drive to the qemu line above would silently renumber them.
  # LABEL= in fstab, not the device node. The kernel names virtio disks in
  # probe order, so a device path baked in on the first boot is a path that
  # can point somewhere else on the second -- and the second boot is where
  # this mount has to carry the settings.
  # No fallback to a guessed /dev/vdN. mkfs on a guess formats whichever
  # disk happens to be there -- and a wrong guess about device numbering is
  # precisely what broke the run before this one. Fail loudly instead.
  - [ sh, -c, "BOOT=$(readlink -f /dev/disk/by-id/virtio-otpboot) || { echo 'OTP-CHECK provision boot-disk-by-id FAIL not found'; exit 1; }; echo \"boot partition: $BOOT\"; mkfs.vfat -n OTPBOOT \"$BOOT\" && mkdir -p /boot/firmware && echo 'LABEL=OTPBOOT /boot/firmware vfat defaults,rw 0 0' >> /etc/fstab && mount /boot/firmware && df -h /boot/firmware" ]
  # --skip-apt because cloud-init installed what Debian has above. The
  # point of this tier is what install.sh does to a BOOTED system, not
  # whether apt can resolve a Raspberry Pi OS package list; the image
  # build already covers that in a real arm64 chroot.
  - [ sh, -c, "/repo/device/install.sh --skip-apt > /var/log/otp-install.log 2>&1; echo \"OTP-INSTALL rc=$?\"" ]
  - [ sh, -c, "systemctl daemon-reload; systemctl start otp-unit.service || true" ]
  - [ sh, -c, "/repo/harness/vm-guest-check.sh provision" ]
  - [ sh, -c, "echo OTP-GUEST-DONE provision" ]
  - [ sh, -c, "tail -40 /var/log/otp-install.log" ]
  # Everything from here is setup for the second and third boots.
  - [ sh, -c, "systemctl enable otp-harness-boot.service" ]
  # NO read-only overlay is configured. Issue #9 tracks that, and it is
  # left open rather than faked -- two mechanisms were tried here and
  # neither works:
  #
  #   - Debian's `overlayroot` package feeds `mount` an option it rejects,
  #     fails to pivot, and panics: "Attempted to kill init!".
  #   - `systemd.volatile=overlay` is accepted on the command line and
  #     silently ignored. It is implemented by systemd INSIDE THE INITRD,
  #     and Debian's initramfs-tools initrd has no systemd in it. Measured:
  #     the flag present in /proc/cmdline, and / still plain rw ext4.
  #
  # What remains here is the console and log-level configuration, which is
  # what boots 2 and 3 need in order to be heard at all.
  #
  # loglevel=3 rides along for a different reason: the guest and the kernel
  # share one serial console, and a kernel message landed in the MIDDLE of
  # a result marker --
  #
  #   OTP-RESULT provi[  104.959304] systemd-ssh-generator[5039]: Failed...
  #
  # -- splitting the word "provision" in half. Sixteen checks had passed
  # and the host correctly refused to believe a phase that never reported a
  # well-formed result. Quietening the kernel console is the fix for the
  # collision; the host-side regex below is the fix for tolerating one.
  #
  # APPENDED, never replaced. /etc/default/grub is shell-sourced by
  # grub-mkconfig, so a later assignment that references the earlier value
  # keeps whatever the image already set -- and what the image sets is
  # `console=ttyS0,115200`, which is the only reason the serial console
  # this whole tier reports through exists at all.
  #
  # An earlier version used `sed -i 's|^GRUB_CMDLINE_LINUX=.*|...|'` and
  # replaced the line outright. Boots 2 and 3 then ran with no serial
  # console: the guest almost certainly did the work, reported into
  # nothing, and powered off. qemu exited 0 after five minutes and the
  # harness concluded the phases never happened.
  #
  # console= is also stated explicitly here rather than relied upon, so
  # this does not quietly depend on what a future base image chooses.
  - [ sh, -c, "echo 'GRUB_CMDLINE_LINUX=\"$GRUB_CMDLINE_LINUX loglevel=3 console=ttyS0,115200\"' >> /etc/default/grub && update-grub 2>&1 | tail -3 && grep -n CMDLINE /etc/default/grub" ]
  # cloud-init must not run this runcmd again on boots 2 and 3 -- it ends
  # in `reboot`, so a repeat is an infinite boot loop that ends at the
  # timeout with nothing useful in the log.
  #
  # Belt and braces: cloud-init decides "have I run before?" from state
  # under /var/lib/cloud, which persists here, so it should already do the
  # right thing. It would NOT if this guest ever gains the read-only
  # overlay that issue #9 is about, because the overlay discards that state
  # and every boot then looks like a new instance. Cheap now, and it stops
  # the overlay work from tripping over this later.
  - [ sh, -c, "touch /etc/cloud/cloud-init.disabled" ]
  - [ sh, -c, "echo reboot > /boot/firmware/otp-harness-phase; sync" ]
  - [ sh, -c, "echo 'OTP-REBOOTING for the systemd-driven boot'" ]
  - [ reboot ]

# Everything goes to the serial console, which is what the host reads.
output: { all: "| tee -a /dev/ttyS0" }
EOF

printf 'instance-id: otp-harness\nlocal-hostname: otp-harness\n' > "$WORK/meta-data"
cloud-localds "$WORK/seed.iso" "$WORK/user-data" "$WORK/meta-data"

# --- boot ---------------------------------------------------------------

ACCEL=()
if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    ACCEL=(-enable-kvm -cpu host)
    log "Booting with KVM"
else
    # Works, but an order of magnitude slower. Say so rather than let the
    # timeout look like a hang.
    log "No /dev/kvm -- falling back to TCG, this will be slow"
fi

CONSOLE="$WORK/console.log"
: > "$CONSOLE"

# TWO things about the drive list, both learned the hard way, both from
# adding one extra disk.
#
# 1. The boot partition goes in as `if=none` plus an explicit -device,
#    rather than `if=virtio,serial=`. qemu 8.2 -- the runner's version --
#    rejects the latter outright:
#
#      Block format 'raw' does not support the option 'serial'
#
#    and does it before the machine starts, so the run dies in a tenth of a
#    second with no console at all. The serial is worth keeping: it gives
#    the guest /dev/disk/by-id/virtio-otpboot, so it can find this disk
#    without depending on virtio probe order.
#
# 2. bootindex=0 on the ROOT disk -- which is why that one had to move to
#    the if=none form too. Adding an explicit -device changes PCI
#    enumeration, SeaBIOS takes its boot order from that, and the blank
#    64MB boot partition became the first boot candidate. The Debian root
#    was never tried at all:
#
#      Booting from Hard Disk...
#      Boot failed: not a bootable disk
#      Booting from Floppy... / DVD-CD... / ROM...
#      No bootable device.
#
#    The guest then sat at the iPXE prompt for forty minutes until the
#    timeout killed it. Nothing about that presented as "the fourth disk
#    broke booting" -- it presented as a hang, and the hang got blamed on
#    the overlay for an hour. An explicit bootindex is the only thing that
#    keeps the order independent of how many devices are added later.
#
# 3. EVERY disk the guest looks up by name carries a serial, and the guest
#    uses /dev/disk/by-id rather than /dev/vdN. Fixing (2) shifted the
#    probe order in the same motion -- repo.tar moved from vdb to vdd --
#    so `tar -xf /dev/vdb` unpacked the blank boot partition, /repo was
#    empty, install.sh did not exist, and the run reported rc=127.
#
#    The comment for (1) already said the serial existed so the guest need
#    not depend on probe order. It then depended on probe order for the
#    disk right next to it. Names, not numbers, for all of them.

# qemu's own stderr, kept separate from the guest's console. When qemu
# refuses to start there IS no guest console, so the failure path had
# nothing to print and said "the guest never reported" -- true, useless,
# and thirty lines below the actual reason, which had scrolled past in the
# job log. A diagnostic nobody can find is not a diagnostic; this file gets
# printed wherever the run gives up.
QEMU_ERR="$WORK/qemu-stderr.log"
: > "$QEMU_ERR"

set +e
# -k 30: SIGTERM, then SIGKILL thirty seconds later if qemu is still there.
#
# Precautionary, not a fix for anything observed. Bare `timeout` sends TERM
# and then waits indefinitely for a process that ignores it; qemu has not
# actually done that here -- when this fired at 2400s it died on signal 15
# and the script carried on and reported normally. Kept because the failure
# mode it guards against is one where the harness cannot report at all,
# which is the expensive kind.
timeout -k 30 "$BOOT_TIMEOUT" qemu-system-x86_64 \
    "${ACCEL[@]}" \
    -m 2048 -smp 2 \
    -nographic -display none \
    -drive "file=$WORK/overlay.qcow2,if=none,format=qcow2,id=rootdisk" \
    -device "virtio-blk-pci,drive=rootdisk,bootindex=0" \
    -drive "file=$WORK/repo.tar,if=none,format=raw,id=repodisk" \
    -device "virtio-blk-pci,drive=repodisk,serial=otprepo" \
    -drive "file=$WORK/seed.iso,if=virtio,format=raw" \
    -drive "file=$WORK/bootpart.img,if=none,format=raw,id=otpboot" \
    -device "virtio-blk-pci,drive=otpboot,serial=otpboot" \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
    -serial "file:$CONSOLE" \
    -monitor none 2> >(tee "$QEMU_ERR" >&2)
QEMU_RC=$?
set -e
# Process substitution is asynchronous: without this the file can still be
# empty when the failure path below reads it.
wait 2>/dev/null || true

# --- the verdict --------------------------------------------------------

# Every boot the guest is expected to complete. Named here rather than
# counted from the output, because the whole point is to notice a boot that
# produced NO output -- which cannot be inferred from the output.
EXPECTED_PHASES="provision reboot persist"

# CR-stripped once, up front. The guest reports over a serial console, which
# emits CRLF, so every captured line carries a trailing \r. That is
# invisible in a log and lethal to string comparison -- it once made "13"
# and "13\r" unequal and failed a run in which everything passed.
CLEAN="$WORK/console-clean.log"
tr -d '\r' < "$CONSOLE" > "$CLEAN"

# Written out separately so the caller can print it last. The console is
# thousands of lines of boot chatter, and a verdict buried under that is a
# verdict nobody reads.
VERDICT="$WORK/verdict.txt"
grep -E "OTP-INSTALL|OTP-CHECK|OTP-RESULT|OTP-GUEST-DONE|OTP-REBOOTING|OTP-CMDLINE" \
    "$CLEAN" > "$VERDICT" 2>/dev/null || true

# qemu's stderr, if it said anything. Printed before the verdict rather
# than after, because when qemu refuses to start this is the ONLY thing
# that explains why.
if [ -s "$QEMU_ERR" ]; then
    log "qemu stderr"
    tail -20 "$QEMU_ERR" >&2
fi

log "Console output"
if grep -q "OTP-CHECK" "$CLEAN"; then
    cat "$VERDICT"
else
    echo "The guest never reported (qemu exited $QEMU_RC)." >&2
    if [ ! -s "$CONSOLE" ]; then
        echo "The console is EMPTY -- qemu never got as far as booting." >&2
        echo "Its stderr is the only evidence there is:" >&2
        cat "$QEMU_ERR" >&2
    else
        echo "Last 80 lines of the console:" >&2
        tail -80 "$CONSOLE" >&2
    fi
    exit 1
fi

FAILED=$(grep -c "OTP-CHECK .* FAIL" "$CLEAN" || true)
log "$(grep "OTP-RESULT" "$CLEAN" | tr '\n' ' ')"

# qemu first. A run killed by the timeout can still leave a complete-looking
# transcript for whichever phases did finish before the axe fell.
if [ "$QEMU_RC" != 0 ]; then
    echo "qemu exited $QEMU_RC (124 = killed by the ${BOOT_TIMEOUT}s timeout)" >&2
    echo "phases that reported: $(grep -c "OTP-RESULT" "$CLEAN" || true) of 3" >&2
    tail -n 40 "$CONSOLE" >&2
    exit 1
fi

# EVERY expected phase must have reported, exactly once, completely, and
# with its two numbers in agreement.
#
# This is the gate that makes a three-boot run mean anything. A boot that
# never happened produces no output, which is indistinguishable from a boot
# with nothing to say -- and the single-phase form this replaces took
# `tail -1` of the result line, so a phase that died silently would have
# left the PREVIOUS phase's success standing as the whole answer. Exactly
# once, not at least once, because the runner reboots itself: a failed state
# write would loop it on one phase forever, and "it reported" would be true
# of a machine doing nothing else.
# Matched as a WHOLE PATTERN wherever it appears in a line, not as a line
# prefix. The guest shares its serial console with the kernel, and a kernel
# message once landed mid-marker:
#
#   OTP-RESULT provi[  104.959304] systemd-ssh-generator[5039]: Failed...
#
# Anchoring on the line, or slicing with ${var#...}, treats that as no
# result at all. This extracts the marker from anywhere in the line and
# requires it to be complete -- phase word and both numbers -- so a
# half-written marker is still correctly rejected rather than half-read.
# The guest also runs with loglevel=3 now, which is the actual fix; this is
# what keeps one unlucky collision from costing a twenty-minute run.
MARKER="OTP-RESULT[[:space:]]+%s[[:space:]]+[0-9]+/[0-9]+"

BAD=""
for phase in $EXPECTED_PHASES; do
    # shellcheck disable=SC2059  # phase is ours, not user input
    pattern=$(printf "$MARKER" "$phase")
    count=$(grep -cE "$pattern" "$CLEAN" || true)
    if [ "${count:-0}" -eq 0 ]; then
        BAD="$BAD $phase(never reported)"
        continue
    fi
    if [ "${count:-0}" -gt 1 ]; then
        BAD="$BAD $phase(reported ${count}x)"
        continue
    fi
    if ! grep -qE "OTP-GUEST-DONE[[:space:]]+$phase([[:space:]]|\$)" "$CLEAN"; then
        BAD="$BAD $phase(unfinished)"
        continue
    fi
    counts=$(grep -oE "$pattern" "$CLEAN" | tail -1)
    counts=${counts##* }
    got=${counts%%/*}
    want=${counts##*/}
    # Numeric, not string, for the CRLF reason above.
    if ! [ "${got:-0}" -eq "${want:--1}" ] 2>/dev/null; then
        BAD="$BAD $phase($counts)"
    fi
done

if [ -n "$BAD" ]; then
    echo "phases incomplete or failing:$BAD" >&2
    # A clean power-off with phases missing means something different from a
    # hang or a panic: the guest reached the end of its sequence and shut
    # down deliberately. If it did that without reporting, the likeliest
    # cause is that it COULD NOT BE HEARD -- a broken serial console -- not
    # that the work never happened. Those have opposite fixes, and one run
    # was spent concluding the wrong one.
    if [ "$QEMU_RC" = 0 ]; then
        echo "NOTE: qemu exited cleanly (0), so the guest powered itself" >&2
        echo "off rather than hanging. A phase that is missing from a" >&2
        echo "clean run usually ran and could not report -- check the" >&2
        echo "OTP-CMDLINE lines for a console= that went missing." >&2
        grep "OTP-CMDLINE" "$CLEAN" >&2 || echo "(no OTP-CMDLINE at all)" >&2
    fi
    grep "OTP-CHECK .* FAIL" "$CLEAN" >&2 || true
    echo "--- otp-unit journal from the guest ---" >&2
    sed -n '/--- otp-unit journal/,/--- end journal ---/p' "$CLEAN" >&2 || true
    exit 1
fi

if [ "${FAILED:-1}" -ne 0 ]; then
    echo "$FAILED check(s) failed" >&2
    grep "OTP-CHECK .* FAIL" "$CLEAN" >&2 || true
    echo "--- otp-unit journal from the guest ---" >&2
    sed -n '/--- otp-unit journal/,/--- end journal ---/p' "$CLEAN" >&2 || true
    exit 1
fi
log "all checks passed across $EXPECTED_PHASES"

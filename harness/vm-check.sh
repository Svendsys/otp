#!/usr/bin/env bash
#
# Tier 2: boot a real system, provision it with device/install.sh, and see
# whether the unit actually comes up.
#
#   ./harness/vm-check.sh
#
# A VM, not a container: otp-unit.service binds tty1 (StandardInput=
# tty-force, TTYPath=/dev/tty1, getty conditioned off, issue #20), and only
# a booted systemd with real VTs shows whether that restart-loops.
#
# amd64, not arm64: nothing tested here is architecture-specific, and KVM
# keeps the run short enough for every commit. The image build and tier 3
# cover arm64.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
WORK="${OTP_VM_WORK:-${TMPDIR:-/tmp}/otp-vm}"
# A leading ~ arrives literally from a YAML env: block; unexpanded it
# creates a directory named "~".
TILDE='~'                                       # literal, not a path to expand
if [ "${WORK#"$TILDE"/}" != "$WORK" ]; then
    WORK="$HOME/${WORK#"$TILDE"/}"
fi
mkdir -p "$WORK"
# Absolute: qemu-img resolves a relative backing path against the
# overlay's directory, not the working directory.
WORK="$(cd "$WORK" && pwd)"
# Debian 13 (trixie) is what the unit ships on. `generic`, not
# `genericcloud`: the cloud kernel drops drivers this needs, including
# the serial console the check reports through.
IMAGE_URL="${OTP_VM_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2}"
BASE="$WORK/base.qcow2"
# Three boots, each with a 45s settle. Must stay well under the workflow's
# timeout-minutes (15): a job killed by GitHub's timeout skips the console
# dump and the verdict, so this timeout has to fire first. 600 is CI's
# budget, not an estimate of the run.
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

# A tar handed to the guest as a raw block device: no filesystem driver on
# either side. The guest finds it by serial, and tar stops at the archive's
# end marker, ignoring the padding.
log "Packing the repository"
tar -C "$REPO" --exclude=.git --exclude='image/pi-gen' --exclude='__pycache__' \
    -cf "$WORK/repo.tar" .
truncate -s %512 "$WORK/repo.tar"

# --- a boot partition, on its own disk -----------------------------------

# The Pi keeps /boot/firmware on a separate FAT partition that a
# read-only root overlay leaves alone; a plain directory on the root would
# make the persistence checks pass for a reason that does not hold on the
# device. Formatted in the guest, so the host needs no mtools.
rm -f "$WORK/bootpart.img"
truncate -s 64M "$WORK/bootpart.img"

# --- what the guest does on first boot ----------------------------------

cat > "$WORK/user-data" <<'EOF'
#cloud-config
# install.sh's packages minus the Raspberry Pi OS-only ones. Without
# python3-lgpio the unit has no working GPIO and must fall through to
# printing unattended rather than fail to start.
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
  # cloud-init's runcmd fires once; boots 2 and 3 enter here.
  - path: /usr/local/sbin/otp-harness-boot
    permissions: "0755"
    content: |
      #!/bin/sh
      # The phase lives on the boot partition: on the device the root
      # discards every write.
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
      # After the unit, so is-active means something, and after CUPS.
      # Wants=, not Requires=: a unit that failed to start must not stop
      # this report from running.
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
  # By serial, not /dev/vdN: the kernel numbers virtio disks in probe
  # order.
  - [ sh, -c, "REPO=$(readlink -f /dev/disk/by-id/virtio-otprepo); echo \"repo disk: $REPO\"; mkdir -p /repo && tar -C /repo -xf \"$REPO\" && ls /repo/device/install.sh" ]
  - [ sh, -c, "chmod +x /repo/device/install.sh /repo/harness/vm-guest-check.sh" ]
  # Format and mount the boot partition before install.sh runs, so the
  # unit's settings land on it. LABEL= in fstab and no fallback to a
  # guessed /dev/vdN: mkfs on a guess formats whichever disk is there.
  - [ sh, -c, "BOOT=$(readlink -f /dev/disk/by-id/virtio-otpboot) || { echo 'OTP-CHECK provision boot-disk-by-id FAIL not found'; exit 1; }; echo \"boot partition: $BOOT\"; mkfs.vfat -n OTPBOOT \"$BOOT\" && mkdir -p /boot/firmware && echo 'LABEL=OTPBOOT /boot/firmware vfat defaults,rw 0 0' >> /etc/fstab && mount /boot/firmware && df -h /boot/firmware" ]
  # --skip-apt: cloud-init installed what Debian has, and the image
  # build covers the real package list in an arm64 chroot.
  - [ sh, -c, "/repo/device/install.sh --skip-apt > /var/log/otp-install.log 2>&1; echo \"OTP-INSTALL rc=$?\"" ]
  - [ sh, -c, "systemctl daemon-reload; systemctl start otp-unit.service || true" ]
  - [ sh, -c, "/repo/harness/vm-guest-check.sh provision" ]
  - [ sh, -c, "echo OTP-GUEST-DONE provision" ]
  - [ sh, -c, "tail -40 /var/log/otp-install.log" ]
  # Setup for the second and third boots.
  - [ sh, -c, "systemctl enable otp-harness-boot.service" ]
  # No read-only overlay is configured (issue #9). loglevel=3 because the
  # guest and the kernel share one serial console and a kernel message
  # can split a result marker. Appended, never replaced: grub-mkconfig
  # shell-sources /etc/default/grub, so the image's own settings are
  # kept; console= is restated so the serial console does not depend on
  # the base image.
  - [ sh, -c, "echo 'GRUB_CMDLINE_LINUX=\"$GRUB_CMDLINE_LINUX loglevel=3 console=ttyS0,115200\"' >> /etc/default/grub && update-grub 2>&1 | tail -3 && grep -n CMDLINE /etc/default/grub" ]
  # The runcmd ends in `reboot`, so a repeat is an infinite boot loop.
  # cloud-init's own state under /var/lib/cloud would not survive a
  # read-only overlay (issue #9).
  - [ sh, -c, "touch /etc/cloud/cloud-init.disabled" ]
  - [ sh, -c, "echo reboot > /boot/firmware/otp-harness-phase; sync" ]
  - [ sh, -c, "echo 'OTP-REBOOTING for the systemd-driven boot'" ]
  - [ reboot ]

# The host reads the serial console.
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
    log "No /dev/kvm -- falling back to TCG, this will be slow"
fi

CONSOLE="$WORK/console.log"
: > "$CONSOLE"

# The drive list:
#  - Disks with a serial use `if=none` plus an explicit -device; qemu 8.2
#    rejects `if=virtio,serial=` for raw images before the machine starts.
#  - bootindex=0 on the root disk: explicit -device entries change PCI
#    enumeration, and SeaBIOS would otherwise try a blank disk first.
#  - Every disk the guest looks up carries a serial and is found through
#    /dev/disk/by-id, never /dev/vdN, which follows probe order.

# qemu's stderr, kept apart from the guest's console: when qemu refuses to
# start there is no console, and this is the only explanation.
QEMU_ERR="$WORK/qemu-stderr.log"
: > "$QEMU_ERR"

set +e
# -k 30: SIGKILL if qemu ignores SIGTERM, so the harness can still report.
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
# empty when the failure path reads it.
wait 2>/dev/null || true

# --- the verdict --------------------------------------------------------

# Named, not counted from the output: a boot that produced no output cannot
# be inferred from the output.
EXPECTED_PHASES="provision reboot persist"

# The serial console emits CRLF; strip the \r once so comparisons hold.
CLEAN="$WORK/console-clean.log"
tr -d '\r' < "$CONSOLE" > "$CLEAN"

# Written out separately so the caller can print it after the console.
VERDICT="$WORK/verdict.txt"
grep -E "OTP-INSTALL|OTP-CHECK|OTP-RESULT|OTP-GUEST-DONE|OTP-REBOOTING|OTP-CMDLINE" \
    "$CLEAN" > "$VERDICT" 2>/dev/null || true

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

# qemu first: a run killed by the timeout can still leave a
# complete-looking transcript for the phases that finished.
if [ "$QEMU_RC" != 0 ]; then
    echo "qemu exited $QEMU_RC (124 = killed by the ${BOOT_TIMEOUT}s timeout)" >&2
    echo "phases that reported: $(grep -c "OTP-RESULT" "$CLEAN" || true) of 3" >&2
    tail -n 40 "$CONSOLE" >&2
    exit 1
fi

# Every expected phase must report exactly once, completely, with its two
# numbers equal. Exactly once because the guest reboots itself: a failed
# state write would loop it on one phase. Matched anywhere in a line, not
# as a prefix, because a kernel message can land mid-line; a half-written
# marker still fails to match.
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
    # Numeric, not string, comparison.
    if ! [ "${got:-0}" -eq "${want:--1}" ] 2>/dev/null; then
        BAD="$BAD $phase($counts)"
    fi
done

if [ -n "$BAD" ]; then
    echo "phases incomplete or failing:$BAD" >&2
    # A clean power-off with phases missing usually means the guest could not
    # be heard (a broken serial console), not that the work never happened.
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

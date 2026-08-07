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
BOOT_TIMEOUT="${OTP_VM_TIMEOUT:-1500}"

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

# --- what the guest does on first boot ----------------------------------

cat > "$WORK/user-data" <<EOF
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
  - overlayroot
  - rng-tools5
package_update: true

runcmd:
  - [ sh, -c, "mkdir -p /repo && tar -C /repo -xf /dev/vdb" ]
  - [ sh, -c, "chmod +x /repo/device/install.sh /repo/harness/vm-guest-check.sh" ]
  # --skip-apt because cloud-init installed what Debian has above. The
  # point of this tier is what install.sh does to a BOOTED system, not
  # whether apt can resolve a Raspberry Pi OS package list; the image
  # build already covers that in a real arm64 chroot.
  - [ sh, -c, "/repo/device/install.sh --skip-apt > /var/log/otp-install.log 2>&1; echo \"OTP-INSTALL rc=\$?\"" ]
  - [ sh, -c, "systemctl daemon-reload; systemctl start otp-unit.service || true" ]
  - [ sh, -c, "/repo/harness/vm-guest-check.sh" ]
  - [ sh, -c, "echo OTP-GUEST-DONE" ]
  - [ sh, -c, "tail -40 /var/log/otp-install.log" ]
  - [ poweroff ]

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

set +e
timeout "$BOOT_TIMEOUT" qemu-system-x86_64 \
    "${ACCEL[@]}" \
    -m 2048 -smp 2 \
    -nographic -display none \
    -drive "file=$WORK/overlay.qcow2,if=virtio,format=qcow2" \
    -drive "file=$WORK/repo.tar,if=virtio,format=raw" \
    -drive "file=$WORK/seed.iso,if=virtio,format=raw" \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
    -serial "file:$CONSOLE" \
    -monitor none
QEMU_RC=$?
set -e

# --- the verdict --------------------------------------------------------

log "Console output"
if grep -q "OTP-CHECK" "$CONSOLE"; then
    grep -E "OTP-INSTALL|OTP-CHECK|OTP-RESULT" "$CONSOLE" || true
else
    echo "The guest never reported. Last 80 lines of the console:" >&2
    tail -80 "$CONSOLE" >&2
    echo "(qemu exited $QEMU_RC)" >&2
    exit 1
fi

FAILED=$(grep -c "OTP-CHECK .* FAIL" "$CONSOLE" || true)
RESULT=$(grep -o "OTP-RESULT .*" "$CONSOLE" | tail -1 || true)
log "${RESULT:-no result line}"

if [ "${FAILED:-1}" -ne 0 ]; then
    echo "$FAILED check(s) failed" >&2
    grep "OTP-CHECK .* FAIL" "$CONSOLE" >&2 || true
    echo "--- otp-unit journal from the guest ---" >&2
    sed -n '/--- otp-unit journal ---/,/--- end journal ---/p' "$CONSOLE" >&2 || true
    exit 1
fi
log "all checks passed"

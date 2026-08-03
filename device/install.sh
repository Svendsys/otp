#!/usr/bin/env bash
#
# Provision a Raspberry Pi as an OTP pad print unit.
#
# This script is the single source of truth for what the unit is. Run it on
# a stock Raspberry Pi OS Lite install to convert a Pi you already have, or
# let the pi-gen stage run it inside a chroot to bake an image. One code
# path, two entry points -- which is what keeps the fast iteration loop
# (edit, rerun, reboot) honest about what the image will do.
#
#   sudo ./device/install.sh
#   sudo ./device/install.sh --image-build     # inside pi-gen's chroot
#
# Idempotent: safe to rerun.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/otp-unit
IMAGE_BUILD=0
SKIP_APT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --image-build) IMAGE_BUILD=1 ;;
        --skip-apt) SKIP_APT=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo $0)" >&2
    exit 1
fi

log() { printf '\n== %s\n' "$*"; }

BOOT_DIR=/boot/firmware
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot
CONFIG_TXT="$BOOT_DIR/config.txt"


# --- packages -----------------------------------------------------------

if [ "$SKIP_APT" -eq 0 ]; then
    log "Installing packages"
    mapfile -t PACKAGES < <(grep -vE '^\s*(#|$)' "$REPO_DIR/device/packages.txt")
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${PACKAGES[@]}"
fi


# --- application --------------------------------------------------------

log "Installing the unit to $PREFIX"
install -d "$PREFIX" "$PREFIX/assets"
cp -a "$REPO_DIR/otpunit" "$PREFIX/"
cp -a "$REPO_DIR/codewords" "$PREFIX/"
install -m 0644 "$REPO_DIR/otp_generator.py" "$PREFIX/otp_generator.py"
install -m 0644 "$REPO_DIR/otp.md" "$PREFIX/assets/otp.md"
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +

# The manual is rendered at image-build time so the unit needs no pandoc.
if [ -f "$REPO_DIR/assets/otp-manual-a5.pdf" ]; then
    install -m 0644 "$REPO_DIR/assets/"otp-manual-*.pdf "$PREFIX/assets/"
else
    echo "NOTE: no pre-rendered manual found; run image/render-manual.sh to add it"
fi


# --- hardware -----------------------------------------------------------

log "Enabling I2C and disabling the radios"
set_config() {
    local key="$1"
    grep -qxF "$key" "$CONFIG_TXT" || printf '%s\n' "$key" >> "$CONFIG_TXT"
}
# The unit is offline by design: an OTP generator has no business holding a
# network interface, and the radios are the easiest thing to switch off.
set_config "dtparam=i2c_arm=on"
set_config "dtoverlay=disable-wifi"
set_config "dtoverlay=disable-bt"
grep -qxF "i2c-dev" /etc/modules || echo "i2c-dev" >> /etc/modules


# --- no swap ------------------------------------------------------------

log "Removing swap"
# Non-negotiable. Key material lives in a bytearray that is zeroed after
# printing; with swap enabled the kernel could page that buffer to the SD
# card first, and zeroing RAM would not touch the copy on disk.
systemctl disable --now dphys-swapfile 2>/dev/null || true
apt-get purge -y dphys-swapfile 2>/dev/null || true
rm -f /var/swap


# --- logging ------------------------------------------------------------

log "Making the journal volatile"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/otp-unit.conf <<'EOF'
# Logs live in RAM and die with the power. The unit logs job metadata only
# -- never codewords or key material -- but a persistent journal would
# still record which pads this machine produced and when.
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF


# --- CUPS ---------------------------------------------------------------

log "Hardening CUPS"
# CUPS always spools a job to disk; that is unavoidable short of writing raw
# to /dev/usb/lp0, which only PostScript and PCL printers accept. So the
# spool is moved to tmpfs, where it lives in RAM, never reaches the SD card,
# and vanishes on power-off.
install -d /etc/cups
cat > /etc/cups/cups-files.conf.d-otp <<'EOF'
RequestRoot /run/cups/spool
TempDir /run/cups/tmp
EOF
if [ -f /etc/cups/cups-files.conf ]; then
    sed -i -e 's|^RequestRoot .*|RequestRoot /run/cups/spool|' \
           -e 's|^TempDir .*|TempDir /run/cups/tmp|' /etc/cups/cups-files.conf
    grep -q '^RequestRoot' /etc/cups/cups-files.conf || \
        echo 'RequestRoot /run/cups/spool' >> /etc/cups/cups-files.conf
    grep -q '^TempDir' /etc/cups/cups-files.conf || \
        echo 'TempDir /run/cups/tmp' >> /etc/cups/cups-files.conf
fi
rm -f /etc/cups/cups-files.conf.d-otp

install -d /etc/cups/cupsd.conf.d
cat > /etc/cups/cupsd.conf.d/otp-unit.conf <<'EOF'
# Keep nothing. A finished job is a liability, not a convenience.
PreserveJobHistory No
PreserveJobFiles No
MaxJobs 1
# Nothing should reach this daemon from anywhere but this machine.
Listen /run/cups/cups.sock
EOF

cat > /etc/tmpfiles.d/otp-unit-cups.conf <<'EOF'
d /run/cups 0710 root lp -
d /run/cups/spool 0710 root lp -
d /run/cups/tmp 1770 root lp -
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/otp-unit-cups.conf 2>/dev/null || true


# --- service ------------------------------------------------------------

log "Installing the service"
install -m 0644 "$REPO_DIR/device/systemd/otp-unit.service" \
    /etc/systemd/system/otp-unit.service
install -m 0644 "$REPO_DIR/device/systemd/otp-unit-etc-cups.service" \
    /etc/systemd/system/otp-unit-etc-cups.service

if [ ! -f "$BOOT_DIR/otp-unit.conf" ]; then
    install -m 0644 "$REPO_DIR/device/boot/otp-unit.conf.example" \
        "$BOOT_DIR/otp-unit.conf"
fi

# A template of /etc/cups is kept aside because the overlay root makes /etc
# read-only: otp-unit-etc-cups.service lays a tmpfs over /etc/cups at boot
# and repopulates it, so the print queue is rebuilt fresh every power-on.
install -d "$PREFIX/cups-etc"
cp -a /etc/cups/. "$PREFIX/cups-etc/" 2>/dev/null || true

systemctl daemon-reload
systemctl enable otp-unit-etc-cups.service
systemctl enable otp-unit.service
systemctl enable cups.service 2>/dev/null || true

if [ "$IMAGE_BUILD" -eq 0 ]; then
    log "Done. Reboot to start the unit."
    cat <<'EOF'

Before this is a real appliance, enable the read-only overlay:

    sudo raspi-config nonint enable_overlayfs
    sudo reboot

After that the root filesystem is read-only with a RAM overlay, so a
power-cycle is a full reset and nothing a session touched survives it.
Settings still persist -- they live on the boot partition.
EOF
else
    log "Image build complete"
fi

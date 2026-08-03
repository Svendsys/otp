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
    # Process substitution hides grep's exit status from both `set -e` and
    # pipefail, so an unreadable manifest would leave PACKAGES empty and
    # turn the install below into a bare `apt-get install` that exits 0
    # having installed nothing.
    mapfile -t PACKAGES < <(grep -vE '^\s*(#|$)' "$REPO_DIR/device/packages.txt")
    if [ "${#PACKAGES[@]}" -eq 0 ]; then
        echo "ERROR: no packages read from device/packages.txt" >&2
        exit 1
    fi
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${PACKAGES[@]}"
fi


# --- application --------------------------------------------------------

log "Installing the unit to $PREFIX"
install -d "$PREFIX" "$PREFIX/assets"
# Replace, do not merge. `cp -a src dest/` onto an existing directory leaves
# whatever was there before, so a module deleted upstream stays installed
# and importable -- which on the edit-rerun-reboot loop is exactly the case
# that misleads.
#
# Guarded: if the clone itself lives at $PREFIX, this would delete the
# source it is about to copy from and abort the install half-done.
if [ "$REPO_DIR" = "$PREFIX" ]; then
    echo "ERROR: the repository is checked out at $PREFIX, which this script" >&2
    echo "       installs into. Move it elsewhere and rerun." >&2
    exit 1
fi
rm -rf "${PREFIX:?}/otpunit" "${PREFIX:?}/codewords"
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
# still record when this machine produced pads.
[Journal]
Storage=volatile
RuntimeMaxUse=16M
EOF

log "Disabling core dumps"
# The process holds several megabytes of key material while a job runs. A
# segfault would hand the whole address space to systemd-coredump, which has
# its own storage path (/var/lib/systemd/coredump) and does not care that
# the journal is volatile -- a compressed copy of the pad, on the SD card.
install -d /etc/systemd/coredump.conf.d
cat > /etc/systemd/coredump.conf.d/otp-unit.conf <<'EOF'
[Coredump]
Storage=none
ProcessSizeMax=0
EOF


# --- CUPS ---------------------------------------------------------------

# On a rerun of a provisioned unit -- the documented edit/rerun/reboot loop
# -- otp-unit-etc-cups.service has already laid a tmpfs over /etc/cups.
# Everything below would then write to, read from and delete through that
# tmpfs: the hardening would vanish at reboot, the template would be
# snapshotted from the live queue, and the on-card printers.conf would
# survive masked and unreachable. Unmount first and let the service remount
# it at next boot.
if mountpoint -q /etc/cups 2>/dev/null; then
    log "Unmounting the /etc/cups tmpfs so the real directory is what we edit"
    systemctl stop otp-unit-etc-cups.service 2>/dev/null || true
    # Stopping the service may already have taken the mount down, so this is
    # a genuine recheck rather than a redundant one. A umount that still
    # fails is not reported here: the guard below turns it into an
    # actionable message instead of a bare "umount: target is busy".
    if mountpoint -q /etc/cups 2>/dev/null; then
        umount /etc/cups || true
    fi
fi
if mountpoint -q /etc/cups 2>/dev/null; then
    echo "ERROR: /etc/cups is still a mount point. Provisioning it now would" >&2
    echo "       write into a tmpfs and be lost at reboot. Reboot and rerun." >&2
    exit 1
fi

log "Hardening CUPS"
# CUPS always spools a job to disk; that is unavoidable short of writing raw
# to /dev/usb/lp0, which only PostScript and PCL printers accept. So every
# directory it writes to is moved to tmpfs, where it lives in RAM, never
# reaches the SD card, and vanishes on power-off.
#
# CacheDir matters as much as the spool: job.cache records a job's name for
# every job in history. PageLog and AccessLog are blanked outright -- page
# logging records one line per printed page.
install -d /etc/cups
set_cups_file() {
    local key="$1" value="$2" file=/etc/cups/cups-files.conf
    [ -f "$file" ] || touch "$file"
    if grep -qE "^#?[[:space:]]*${key}[[:space:]]" "$file"; then
        sed -i -E "s|^#?[[:space:]]*${key}[[:space:]].*|${key} ${value}|" "$file"
    else
        printf '%s %s\n' "$key" "$value" >> "$file"
    fi
}
set_cups_file RequestRoot /run/cups/spool
set_cups_file TempDir /run/cups/tmp
set_cups_file CacheDir /run/cups/cache
# /dev/null, NOT an empty value. cups-files.conf(5) says a blank filename
# disables the log, but CUPS 2.4's parser yields NULL rather than "" for a
# valueless directive, logs "Missing value for PageLog", and -- because
# FatalErrors defaults to config -- cupsd then refuses to start at all.
# CUPS special-cases /dev/ paths to skip rotation, so this is the safe form.
set_cups_file PageLog /dev/null
set_cups_file AccessLog /dev/null
# ErrorLog defaults to /var/log/cups/error_log, which is NOT covered by
# making the journal volatile -- cupsd is a separate unit writing its own
# file handle. Send it to syslog so it lands in the RAM-backed journal.
set_cups_file ErrorLog syslog

# ipp-usb keeps its own per-device logs keyed by manufacturer, model and
# serial. That is the same artifact printers.conf is excluded for, arriving
# by a different door, so point its directory at tmpfs too.
install -d /etc/systemd/system/ipp-usb.service.d
cat > /etc/systemd/system/ipp-usb.service.d/otp-unit.conf <<'EOF'
[Service]
LogsDirectory=ipp-usb
EOF

# These go in cupsd.conf itself. CUPS has no Include directive and no
# cupsd.conf.d mechanism -- cupsdReadConfiguration() opens exactly
# cups-files.conf and cupsd.conf -- so a drop-in file is read by nothing and
# the defaults stand. The defaults are the opposite of what is wanted here:
# PreserveJobHistory is Yes and PreserveJobFiles is 86400, meaning the
# spooled document, i.e. the entire pad, is kept for a day after printing.
set_cupsd() {
    local key="$1" value="$2" file=/etc/cups/cupsd.conf
    [ -f "$file" ] || touch "$file"
    if grep -qE "^#?[[:space:]]*${key}[[:space:]]" "$file"; then
        sed -i -E "s|^#?[[:space:]]*${key}[[:space:]].*|${key} ${value}|" "$file"
    else
        printf '%s %s\n' "$key" "$value" >> "$file"
    fi
}
set_cupsd PreserveJobHistory No
set_cupsd PreserveJobFiles No
set_cupsd MaxJobs 1

# Written in one go: appending here would duplicate on every rerun.
# Prove the daemon will actually load what we just wrote. cupsd only reports
# a bad directive at startup, and otp-unit.service only Wants= cups, so
# without this the unit boots looking healthy and fails at print time.
if command -v cupsd >/dev/null; then
    if ! cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf >/dev/null 2>&1; then
        echo "ERROR: the generated CUPS configuration is invalid:" >&2
        cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf >&2 || true
        exit 1
    fi
fi

cat > /etc/tmpfiles.d/otp-unit-cups.conf <<'EOF'
d /run/cups 0710 root lp -
d /run/cups/spool 0710 root lp -
d /run/cups/tmp 1770 root lp -
d /run/cups/cache 0770 root lp -
d /run/ipp-usb 0750 root root -
L+ /var/log/ipp-usb - - - - /run/ipp-usb
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

# A template of /etc/cups is kept aside: otp-unit-etc-cups.service lays a
# tmpfs over /etc/cups at boot and repopulates it, so the print queue is
# rebuilt fresh every power-on.
#
# The template is built by WHITELIST, not blacklist. Everything that names
# a printer has to stay out of it, and a blacklist kept missing things:
# cupsd renames the old printers.conf to printers.conf.O on every save, and
# writes a per-queue PPD to ppd/<queue>.ppd whose NickName is the model. A
# snapshot taken on a rerun would bake those into the read-only root and lay
# them back over /etc/cups on every boot -- making the one artifact this
# mechanism exists to forget the one that survives longest.
install -d "$PREFIX/cups-etc"
rm -rf "${PREFIX:?}/cups-etc"
install -d "$PREFIX/cups-etc"
for keep in cupsd.conf cups-files.conf snmp.conf client.conf; do
    [ -f "/etc/cups/$keep" ] && install -m 0644 "/etc/cups/$keep" "$PREFIX/cups-etc/$keep"
done
# The mime rules are static package data, not device state.
for keep in /etc/cups/*.types /etc/cups/*.convs; do
    [ -f "$keep" ] && install -m 0644 "$keep" "$PREFIX/cups-etc/"
done

rm -f /etc/cups/printers.conf /etc/cups/printers.conf.O \
      /etc/cups/classes.conf /etc/cups/classes.conf.O \
      /etc/cups/subscriptions.conf /etc/cups/subscriptions.conf.O
rm -rf /etc/cups/ppd

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

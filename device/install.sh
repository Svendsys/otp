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
    # Process substitution hides grep's exit status, so an unreadable manifest
    # would leave PACKAGES empty and apt-get would install nothing.
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
# Replace, do not merge: a module deleted upstream must not stay installed
# and importable. Guarded because a clone living at $PREFIX would delete its
# own source.
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
# config.txt sections after a `[pi4]`-style header apply to that model only,
# so a setting counts only where it applies to every model, and an append is
# preceded by `[all]` unless the file already ends in one.
config_applies_to_all() {
    awk -v want="$1" '
        /^[[:space:]]*\[/ { all = ($0 ~ /^[[:space:]]*\[all\][[:space:]]*$/); seen = 1; next }
        (!seen || all) && $0 == want { found = 1 }
        END { exit found ? 0 : 1 }' "$CONFIG_TXT"
}
config_ends_unconditional() {
    awk '
        /^[[:space:]]*\[/ { all = ($0 ~ /^[[:space:]]*\[all\][[:space:]]*$/); seen = 1 }
        END { exit (!seen || all) ? 0 : 1 }' "$CONFIG_TXT"
}
set_config() {
    local key="$1"
    config_applies_to_all "$key" && return 0
    config_ends_unconditional || printf '[all]\n' >> "$CONFIG_TXT"
    printf '%s\n' "$key" >> "$CONFIG_TXT"
}
# The unit is offline by design; the radios are the easiest thing to switch
# off.
set_config "dtparam=i2c_arm=on"
set_config "dtoverlay=disable-wifi"
set_config "dtoverlay=disable-bt"
grep -qxF "i2c-dev" /etc/modules || echo "i2c-dev" >> /etc/modules


# --- no swap ------------------------------------------------------------

log "Removing swap"
# Key material lives in a bytearray zeroed after printing; with swap the
# kernel could page it to the SD card, where zeroing RAM does not reach.
systemctl disable --now dphys-swapfile 2>/dev/null || true
apt-get purge -y dphys-swapfile 2>/dev/null || true
rm -f /var/swap
# dphys-swapfile is the bookworm mechanism; trixie uses zram swap. Masking
# swap.target stops systemd activating any swap unit, whatever generated it.
# The packages stay: purging them could take raspberrypi-sys-mods along.
# otp-unit.service also runs `swapoff -a` before every start.
systemctl mask swap.target
swapoff -a 2>/dev/null || true


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

log "Ensuring the groups the service runs with exist"
# systemd refuses to start a unit whose SupplementaryGroups= names a missing
# group (status=216/GROUP), and Restart=on-failure makes that a restart loop
# on a device whose journal is volatile. Existing groups keep their GID.
for group in lp gpio i2c input; do
    if ! getent group "$group" >/dev/null 2>&1; then
        groupadd --system "$group"
        log "  created missing group: $group"
    fi
done

log "Disabling core dumps"
# A segfault would hand the address space, key material included, to
# systemd-coredump, which stores under /var/lib/systemd/coredump whether or
# not the journal is volatile.
install -d /etc/systemd/coredump.conf.d
cat > /etc/systemd/coredump.conf.d/otp-unit.conf <<'EOF'
[Coredump]
Storage=none
ProcessSizeMax=0
EOF


# --- CUPS ---------------------------------------------------------------

# On a rerun, otp-unit-etc-cups.service has already laid a tmpfs over
# /etc/cups: edits below would land in the tmpfs and vanish at reboot, and
# the template would be snapshotted from the live queue. Unmount first.
if mountpoint -q /etc/cups 2>/dev/null; then
    log "Unmounting the /etc/cups tmpfs so the real directory is what we edit"
    systemctl stop otp-unit-etc-cups.service 2>/dev/null || true
    # Stopping the service may already have unmounted it. A umount that still
    # fails is reported by the guard below.
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
# CUPS always spools to disk, so every directory it writes to is moved to
# tmpfs. CacheDir matters as much as the spool: job.cache records every
# job's name. PageLog and AccessLog are disabled outright.
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
# /dev/null, not an empty value: CUPS 2.4 treats a valueless directive as a
# config error and cupsd refuses to start. /dev/ paths skip rotation.
set_cups_file PageLog /dev/null
set_cups_file AccessLog /dev/null
# ErrorLog defaults to a file under /var/log/cups; syslog lands it in the
# RAM-backed journal.
set_cups_file ErrorLog syslog

# ipp-usb keeps per-device logs keyed by manufacturer, model and serial, so
# its directory goes to tmpfs too.
install -d /etc/systemd/system/ipp-usb.service.d
cat > /etc/systemd/system/ipp-usb.service.d/otp-unit.conf <<'EOF'
[Service]
LogsDirectory=ipp-usb
EOF

# CUPS has no Include or cupsd.conf.d mechanism, so these go in cupsd.conf
# itself. The defaults keep the spooled document -- the whole pad -- for a
# day after printing.
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
# MaxJobs must exceed 1: cupsd rejects rather than queues past it, and the
# unattended sequence submits seven jobs. The unit drains the queue before
# each submit; this is headroom against a timing race.
set_cupsd MaxJobs 4
# The default retry-job never gives up, so a job to a dead printer would
# stay active forever and the UI would hold key material waiting on it.
# abort-job lets the queue drain. The cost: a failed job leaves the queue as
# empty as a printed one, so Cups.printer_fault() asks the queue's own state.
set_cupsd ErrorPolicy abort-job

# Stock CUPS creates none of these directories.
cat > /etc/tmpfiles.d/otp-unit-cups.conf <<'EOF'
d /run/cups 0710 root lp -
d /run/cups/spool 0710 root lp -
d /run/cups/tmp 1770 root lp -
d /run/cups/cache 0770 root lp -
d /run/ipp-usb 0750 root root -
L+ /var/log/ipp-usb - - - - /run/ipp-usb
EOF

# Create them now as well: cupsd -t below checks that every directory named
# in cups-files.conf exists, and systemd-tmpfiles does not work in pi-gen's
# chroot.
install -d -m 0710 -o root -g lp /run/cups /run/cups/spool
install -d -m 1770 -o root -g lp /run/cups/tmp
install -d -m 0770 -o root -g lp /run/cups/cache
install -d -m 0750 -o root -g root /run/ipp-usb
# Best effort on top: this cannot work in a chroot, so failure is reported
# but not fatal.
if command -v systemd-tmpfiles >/dev/null; then
    systemd-tmpfiles --create /etc/tmpfiles.d/otp-unit-cups.conf \
        || log "systemd-tmpfiles declined; the directories above are already in place"
fi

# cupsd only reports a bad directive at startup, and otp-unit.service only
# Wants= cups, so without this the unit would boot looking healthy and fail
# at print time. Must run after the directories exist.
if command -v cupsd >/dev/null; then
    if ! cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf >/dev/null 2>&1; then
        echo "ERROR: the generated CUPS configuration is invalid:" >&2
        cupsd -t -c /etc/cups/cupsd.conf -s /etc/cups/cups-files.conf >&2 || true
        exit 1
    fi
fi


# --- service ------------------------------------------------------------

log "Installing the service"
install -m 0644 "$REPO_DIR/device/systemd/otp-unit.service" \
    /etc/systemd/system/otp-unit.service
install -m 0644 "$REPO_DIR/device/systemd/otp-unit-etc-cups.service" \
    /etc/systemd/system/otp-unit-etc-cups.service

# The tier-3 guest probe. Inert on a real unit: it runs only when the kernel
# command line carries otp.imgcheck. Installed unconditionally so the image
# tier 3 boots is the image that ships.
if [ -f "$REPO_DIR/harness/img-guest-check.sh" ]; then
    install -m 0755 "$REPO_DIR/harness/img-guest-check.sh" \
        "$PREFIX/img-guest-check.sh"
    install -m 0644 "$REPO_DIR/device/systemd/otp-unit-imgcheck.service" \
        /etc/systemd/system/otp-unit-imgcheck.service
elif [ "$IMAGE_BUILD" -eq 1 ]; then
    # Fatal in an image build: without the probe nothing can check the overlay,
    # and tier 3 would see it only as a guest that never reported.
    echo "ERROR: harness/img-guest-check.sh is not in $REPO_DIR, so the" >&2
    echo "       image being built would have nothing to report on the" >&2
    echo "       read-only overlay and tier 3 could not observe it." >&2
    exit 1
else
    echo "NOTE: harness/img-guest-check.sh is absent, so a tier-3 boot of"
    echo "      this system would have nothing to report with."
fi

if [ ! -f "$BOOT_DIR/otp-unit.conf" ]; then
    install -m 0644 "$REPO_DIR/device/boot/otp-unit.conf.example" \
        "$BOOT_DIR/otp-unit.conf"
fi

# otp-unit-etc-cups.service lays a tmpfs over /etc/cups at boot and
# repopulates it from this template, so the queue is rebuilt every power-on.
# Built by whitelist: anything naming a printer (printers.conf, its .O
# backup, ppd/<queue>.ppd) must stay out of the read-only root.
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
if [ -f /etc/systemd/system/otp-unit-imgcheck.service ]; then
    systemctl enable otp-unit-imgcheck.service 2>/dev/null || true
fi
systemctl enable cups.service 2>/dev/null || true

# otp-unit takes tty1 as its front panel, so the login prompt goes on tty2:
# with no network and no SSH, Alt+F2 is the only way into a unit that will
# not come up.
systemctl enable getty@tty2.service 2>/dev/null || true

# --- the boot has to be able to finish ------------------------------------
#
# systemd-networkd-wait-online has TimeoutStartSec=infinity and this
# appliance has no networkd configuration (NetworkManager owns the link), so
# the wait never returns and everything ordered after network-online.target
# stays queued for the whole boot -- including, via cloud-init's
# cloud-config.service, the userconfig wizard (issue #20). Hence the mask.
#
# cloud-init is also switched off with its own kill switch: ds-identify stops
# at /etc/cloud/cloud-init.disabled before any datasource search. The image
# build purges the packages (issue #34); the marker covers a Pi provisioned
# in place and a unit that gets cloud-init back as a dependency. Its NoCloud
# datasource would otherwise read user-data off the boot partition.
systemctl mask systemd-networkd-wait-online.service
install -d /etc/cloud
: > /etc/cloud/cloud-init.disabled

# Raspberry Pi OS's first-boot user wizard (userconf-pi) goes interactive on
# every console boot until someone answers a whiptail on tty8, holding
# multi-user.target open. It is not masked, because it is also the only
# consumer of the documented credential file userconf.txt. The drop-in:
#
#   - the ConditionPathExists |= pair: the service runs only when an
#     operator seeded userconf(.txt); an unseeded boot skips it.
#   - StandardInput=null: a malformed seed fails fast instead of prompting;
#     the boot completes and the file is left as failed_userconf.txt.
#
# The applied hash goes to /etc/shadow, inside the overlay, and the wizard
# deletes the seed, so device/persist-identity.sh keeps the credential on the
# boot partition (see the ExecStartPost drop-in below). That partition is vfat
# mounted `defaults`, readable by every local account, so the hash is exposed
# exactly as the operator's own userconf.txt was -- but deleting userconf.txt
# no longer removes it (docs/IMAGE.md gives the `rm`).
#
# The heredoc is unquoted so $BOOT_DIR expands: the condition must name the
# directory the firmware partition is actually mounted on, which is /boot on
# the pre-bookworm layout. Nothing else in the block contains a `$`.
systemctl unmask userconfig.service 2>/dev/null || true
install -d /etc/systemd/system/userconfig.service.d
cat > /etc/systemd/system/userconfig.service.d/otp-appliance.conf <<DROPIN
[Unit]
ConditionPathExists=|$BOOT_DIR/userconf
ConditionPathExists=|$BOOT_DIR/userconf.txt

[Service]
StandardInput=null
DROPIN

# Enabled explicitly in the image: with the machine-id persisted only a
# unit's first boot runs preset-all, and a disabled userconfig.service would
# silently ignore a userconf.txt on every later boot. The condition pair
# above makes it free on unseeded boots. `|| true` because this is not load
# bearing for the boot.
systemctl enable userconfig.service 2>/dev/null || true

# Applying a seed ends in cancel-rename, which enables and starts getty@tty1
# -- the front panel's tty. Masking the getty would make that start fail, so
# userconf-service (sh -e) would die before deleting the seed and
# restart-loop; Conflicts= on otp-unit would let the getty stop the panel. A
# condition-skipped start does neither.
#
# Both conditions are `|`-prefixed, i.e. triggering and ORed: tty1 gets a
# login if the panel is not enabled or its unit is masked or absent.
#
#   - the .wants symlink is what `enable` creates and `disable` removes.
#   - ConditionFileNotEmpty, not PathExists, on the unit file: a mask is a
#     symlink to /dev/null, which exists but is not a non-empty regular file.
install -d /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/otp-appliance.conf <<'GETTY'
[Unit]
ConditionPathExists=|!/etc/systemd/system/multi-user.target.wants/otp-unit.service
ConditionFileNotEmpty=|!/etc/systemd/system/otp-unit.service
GETTY

# A condition only stops a getty starting. On a Pi provisioned in place one
# is already running and would take the tty back off the panel
# (Restart=always, TTYVHangup), so it is stopped once, after the reload that
# puts the condition in force. In pi-gen's chroot systemctl ignores this.
systemctl daemon-reload
systemctl stop getty@tty1.service 2>/dev/null || true

# --- AND THE RELOAD AT THE END OF THAT SAME SCRIPT ------------------------
# cancel-rename's last act is to reload ssh if it is active. With ssh.socket
# enabled (Accept=no) the socket owns :22, so sshd's SIGHUP re-exec cannot
# bind, exits 255, and RestartPreventExitStatus=255 keeps it down. With the
# socket masked, ssh.service binds :22 itself and a reload works.
#
# Masked, not disabled: `disable` only removes symlinks, and preset-all would
# put them back. The image ships ssh.service disabled (ENABLE_SSH=0), but
# preset-all still starts it on a unit's first boot, so the mask is needed.
# ssh.service itself is not masked: that would take SSH off the machine as a
# side effect.
#
# No `|| true`: a mask that silently failed to apply must fail the build.
systemctl mask ssh.socket

# --- the read-only root overlay ------------------------------------------

# Hand-rolled rather than `raspi-config nonint enable_overlayfs`: on bookworm
# and later that installs `overlayroot`, whose initramfs script needs
# `mount --move`, which klibc's mount lacks -- a kernel panic unless busybox
# happens to be in the initramfs (issue #9).
#
# Instead `boot=overlay` makes the initrd source /scripts/overlay, which
# overrides local_mount_root to mount the card read-only under a tmpfs
# overlay, using only mounts klibc accepts. If the initramfs is missing the
# unit comes up on a writable root, which tier 3 fails on.
#
# /etc/fstab is not touched; systemd-remount-fs remounts / with the options
# fstab gives. The /boot/firmware entry stays `vfat defaults` too, so every
# file there is readable by every local account -- including the kept login
# at /boot/firmware/otp-identity/credential. No pad byte is ever written
# there. Narrowing it with fmask/dmask would mean rewriting fstab, where a
# wrong entry costs the boot-partition mount on a device with no network.
CMDLINE_TXT="$BOOT_DIR/cmdline.txt"
if [ ! -f "$CMDLINE_TXT" ]; then
    # The overlay is configured through the Pi boot firmware; a machine with no
    # cmdline.txt (the tier-2 Debian guest) has nothing to configure.
    if [ "$IMAGE_BUILD" -eq 1 ]; then
        echo "ERROR: no $CMDLINE_TXT in the image being built, so the" >&2
        echo "       read-only overlay cannot be enabled. An image without" >&2
        echo "       it keeps everything a session writes on the card." >&2
        exit 1
    fi
    log "No $CMDLINE_TXT here, so the read-only overlay was NOT enabled"
    echo "This machine keeps a writable root. That is correct for anything"
    echo "that is not a Raspberry Pi; on a Pi it means the boot partition is"
    echo "not mounted where this script looked."
else
    log "Enabling the read-only root overlay"
    install -d /etc/initramfs-tools/scripts
    # initramfs-tools' own local_mount_root (0.148.4) with the overlay steps
    # inserted; keep it in step when that function changes.
    cat > /etc/initramfs-tools/scripts/overlay <<'OVERLAY'
# Mount the root filesystem read-only under a RAM overlay.  -*- shell-script -*-
#
# initramfs-tools sources this file when the kernel command line says
# boot=overlay, after /scripts/local and before mountroot. Overriding
# local_mount_root is how mountroot reaches this code.

. /scripts/local

# The one thing allowed through the overlay. /etc/machine-id ships as
# `uninitialized`, which systemd reads as a first boot: it generates an ID
# into the tmpfs and runs preset-all, on every boot. PID 1 reads the file
# before any unit, so the initrd is the only place early enough to restore
# it. One named file, not /etc, and no SSH host keys (see
# device/persist-identity.sh).
#
# The source is the FAT boot partition, mounted read-only here; userspace
# records the id there later. Only klibc-utils commands are used and vfat is
# built into the -rpi-v8 kernels, so neither busybox nor a module is needed.
#
# Never panics: a missing, unreadable or malformed stored id leaves
# /etc/machine-id as the image shipped it.
#
# The mountpoint is a variable so tests/test_overlay_root.py can run this
# function against a tree it builds.
OTP_IDENTITY_MNT=/otp-boot

otp_restore_machine_id()
{
	local dev id

	mkdir -p "${OTP_IDENTITY_MNT}"
	# The FAT partition is partition 1 of the card the root came off:
	# ${ROOT%[0-9]}1 swaps the final partition digit for 1 (mmcblk0p2,
	# nvme0n1p2, sda2), with the literal device as a fallback. The file must
	# be under otp-identity/, so mounting the wrong thing does nothing.
	for dev in "${ROOT%[0-9]}1" /dev/mmcblk0p1; do
		[ -e "${dev}" ] || continue
		mount -r -t vfat "${dev}" "${OTP_IDENTITY_MNT}" 2>/dev/null || continue
		id=$(cat "${OTP_IDENTITY_MNT}/otp-identity/machine-id" 2>/dev/null)
		umount "${OTP_IDENTITY_MNT}" 2>/dev/null || true
		# systemd accepts only 32 lower-case hex characters; anything else is
		# dropped rather than written.
		case "${id}" in
		*[!0-9a-f]*|"") continue ;;
		esac
		[ "${#id}" = 32 ] || continue
		echo "${id}" > "${rootmnt}/etc/machine-id" 2>/dev/null || continue
		return 0
	done
	return 0
}

local_mount_root()
{
	local_top
	if [ -z "${ROOT}" ]; then
		panic "No root device specified. Boot arguments must include a root= parameter."
	fi
	local_device_setup "${ROOT}" "root file system"
	ROOT="${DEV}"

	if [ -z "${ROOTFSTYPE}" ] || [ "${ROOTFSTYPE}" = auto ]; then
		FSTYPE=$(get_fstype "${ROOT}")
	else
		FSTYPE=${ROOTFSTYPE}
	fi

	local_premount

	checkfs "${ROOT}" root "${FSTYPE}"

	mkdir -p /lower /upper

	# -r: the running system never has a writable handle on the card.
	# shellcheck disable=SC2086
	if ! mount -r ${FSTYPE:+-t "${FSTYPE}"} ${ROOTFLAGS} "${ROOT}" /lower; then
		panic "Failed to mount ${ROOT} read-only as the overlay's lower layer."
	fi

	# A no-op when overlayfs is built in. /etc/initramfs-tools/modules names
	# it as well, so init has already loaded it where it is a module.
	modprobe overlay || true

	if ! mount -t tmpfs tmpfs /upper; then
		panic "Failed to mount the overlay's upper layer in RAM."
	fi
	mkdir -p /upper/data /upper/work

	# Panic rather than fall back to read-write: a unit that came up without
	# the overlay would silently keep the session on the card.
	if ! mount -t overlay \
	     -o lowerdir=/lower,upperdir=/upper/data,workdir=/upper/work \
	     overlay "${rootmnt?}"; then
		panic "Failed to assemble the root overlay."
	fi

	# Last: this writes through the overlay into its tmpfs upper layer.
	otp_restore_machine_id
}
OVERLAY
    grep -qxF overlay /etc/initramfs-tools/modules 2>/dev/null \
        || echo overlay >> /etc/initramfs-tools/modules

    # Every installed kernel, from /lib/modules rather than `uname -r`: in
    # pi-gen's chroot uname reports the build host's kernel. The image also
    # carries a second kernel for the Pi 5.
    KERNELS=""
    for moddir in /lib/modules/*; do
        [ -f "$moddir/modules.dep" ] || continue
        KERNELS="$KERNELS ${moddir##*/}"
    done
    if [ -z "$KERNELS" ]; then
        echo "ERROR: no kernel found under /lib/modules, so no initramfs" >&2
        echo "       can be built and the overlay cannot be enabled." >&2
        exit 1
    fi
    for kern in $KERNELS; do
        # -c, not -u: pi-gen sets update_initramfs=no, which update-initramfs
        # honours on its update path only -- `-u` would exit 0 having done
        # nothing.
        update-initramfs -c -k "$kern"
    done

    # auto_initramfs=1 makes the firmware load /boot/firmware/initramfs8 next
    # to kernel8.img; raspi-firmware's post-update hook puts it there.
    set_config "auto_initramfs=1"

    if ! grep -q "boot=overlay" "$CMDLINE_TXT"; then
        sed -i -e "s|^|boot=overlay |" "$CMDLINE_TXT"
    fi

    # pi-gen grows the root filesystem on first boot, keyed on this token and
    # rpi-resize.service. A read-only lower layer cannot be resized online, and
    # the failing service would reboot-loop.
    sed -i -E 's/(^| )resize( |$)/\1/g' "$CMDLINE_TXT"
    # tty1 is the front panel. Without `quiet` the kernel writes over it
    # whenever a printer is plugged in.
    grep -qE '(^| )quiet( |$)' "$CMDLINE_TXT" \
        || sed -i -e 's|$| quiet|' "$CMDLINE_TXT"
    # No console on the GPIO UART: it puts the kernel log and a login
    # prompt on three header pins. For bring-up, add
    # `console=serial0,115200` back to cmdline.txt on the card.
    sed -i -E 's/(^| )console=(serial0|ttyAMA0|ttyS0)(,[^ ]*)?//g; s/^ +//' "$CMDLINE_TXT"
    # systemd-fstab-generator also hooks systemd-growfs-root onto the root
    # mount on every boot, and on an overlay root it can only fail. Masked, not
    # disabled: there is no symlink for `disable` to remove. No `|| true`: a
    # mask that silently failed to apply must fail the build.
    systemctl mask systemd-growfs-root.service
    systemctl disable rpi-resize.service 2>/dev/null || true

    # Verify, because every failure here is silent: the unit just boots
    # read-write. Not `lsinitramfs | grep -q`: under pipefail grep's early exit
    # SIGPIPEs lsinitramfs and the pipeline fails on a match. Every initramfs
    # is checked: the firmware picks one per board (initramfs8 on a Pi 3 or 4,
    # initramfs_2712 on a Pi 5).
    OVERLAY_INITRAMFS=""
    STALE_INITRAMFS=""
    for candidate in "$BOOT_DIR"/initramfs*; do
        [ -f "$candidate" ] || continue
        LISTING=$(lsinitramfs "$candidate" 2>/dev/null || true)
        case "$LISTING" in
            *scripts/overlay*) OVERLAY_INITRAMFS="$OVERLAY_INITRAMFS $candidate" ;;
            *) STALE_INITRAMFS="$STALE_INITRAMFS $candidate" ;;
        esac
    done
    if [ -z "$OVERLAY_INITRAMFS" ]; then
        echo "ERROR: no initramfs in $BOOT_DIR contains scripts/overlay, so" >&2
        echo "       boot=overlay would be read by nothing and the unit" >&2
        echo "       would come up on a writable root." >&2
        ls -la "$BOOT_DIR" >&2
        exit 1
    fi
    if [ -n "$STALE_INITRAMFS" ]; then
        echo "ERROR: these have no scripts/overlay:$STALE_INITRAMFS" >&2
        echo "       A board that boots one of them comes up on a writable" >&2
        echo "       root. Delete it if its kernel is gone, or reinstall" >&2
        echo "       that kernel and rerun this script." >&2
        exit 1
    fi
    if ! grep -q "boot=overlay" "$CMDLINE_TXT"; then
        echo "ERROR: boot=overlay is not in $CMDLINE_TXT" >&2
        exit 1
    fi
    log "  overlay initramfs: $OVERLAY_INITRAMFS"

    # --- the identity the overlay would otherwise throw away --------------
    # The userspace half of the initramfs exception above: the initrd restores
    # /etc/machine-id because PID 1 reads it before any unit exists; this unit
    # records it, because that needs a mounted boot partition. Only on an
    # overlay machine: a writable root keeps /etc/machine-id by itself. SSH
    # host keys are not persisted; the appliance does not run sshd.
    install -m 0755 "$REPO_DIR/device/persist-identity.sh" \
        "$PREFIX/persist-identity.sh"
    install -m 0644 "$REPO_DIR/device/systemd/otp-unit-identity.service" \
        /etc/systemd/system/otp-unit-identity.service
    systemctl daemon-reload
    systemctl enable otp-unit-identity.service

    # --- and the operator's login, recorded by the wizard that applies it --
    # An ExecStartPost on userconfig.service rather than a unit of its own, so
    # it fires only on a boot where an operator's userconf.txt was applied: the
    # image's random build-time hash is never copied to the card, and a fresh
    # seed replaces the store after it is applied. systemd skips ExecStartPost
    # when ExecStart fails, so a malformed seed leaves the store alone.
    #
    # The leading `-` keeps a failed record from failing the unit, whose
    # Restart=on-failure would loop. It also makes failures silent, so tier 3
    # reads the store after the apply and again across a power cycle.
    #
    # A separate drop-in from otp-appliance.conf, which is written on every
    # machine; persist-identity.sh is installed only on an overlay machine.
    install -d /etc/systemd/system/userconfig.service.d
    cat > /etc/systemd/system/userconfig.service.d/otp-credential.conf <<'CRED'
[Service]
ExecStartPost=-/opt/otp-unit/persist-identity.sh --record-credential
CRED
    systemctl daemon-reload
fi

# WHAT THIS SCRIPT DID TO SOMEONE ELSE'S MACHINE, said out loud: on a Pi
# provisioned in place, several steps above change the whole box for good.
if [ "$IMAGE_BUILD" -eq 0 ]; then
    log "Done. Reboot to start the unit."
    cat <<'EOF'

The root filesystem is now a read-only overlay: the card is mounted
read-only and everything written to / goes to a tmpfs on top of it, so a
power-cycle is a full reset and nothing a session touched survives it.
Settings still persist -- they live on the boot partition, which is outside
the overlay.

Five machine-wide changes were made as well -- two because an unbounded
network wait held this image's boot open for its whole length, one because
the overlay made every boot look like a first boot, one because an online
resize of a read-only root cannot work, and one because key material must
never be paged out (see the comments in this script):

  * systemd-networkd-wait-online.service is MASKED. Nothing on this machine
    can wait for network-online.target through networkd any more, including
    software installed later. Undo with
    `sudo systemctl unmask systemd-networkd-wait-online.service`.
  * cloud-init is switched off permanently, via /etc/cloud/cloud-init.disabled.
    This machine will not run any cloud-init datasource again -- including
    user-data written to the boot partition, which is what Raspberry Pi OS
    points it at. Undo by deleting that file. The PACKAGES are still
    installed here: they are purged out of the image this project builds,
    but this script does not remove software from a Pi you already had. So
    that one file is the whole of it, and cloud-init runs again on the next
    boot after anybody deletes it.
  * ssh.socket is MASKED, so sshd is never socket-activated here. It had to
    be: with the socket holding port 22, the reload that userconf-pi runs at
    the end of a seeded first boot left sshd unable to bind and
    RestartPreventExitStatus=255 kept it down for the rest of the boot. Undo
    with `sudo systemctl unmask ssh.socket`. ssh.service is NOT masked, and
    it is not enabled either: the image is built with ENABLE_SSH=0, so it is
    disabled and the only boot it runs on is a machine's very first, where
    systemd's own preset-all switches it on. From the second boot onwards
    this machine has no sshd.
  * systemd-growfs-root.service is MASKED. systemd-fstab-generator hooks it
    onto the root mount on every boot with no symlink for `disable` to
    remove, and on an overlay root it can only fail -- `File system "/" not
    backed by block device` -- so it printed a red line on the console of a
    key printer on every boot of every image this project built. Nothing on
    this machine can grow the root filesystem any more. Undo with
    `sudo systemctl unmask systemd-growfs-root.service`.

  * swap.target is MASKED, so systemd activates no swap of any kind on this
    machine -- not a swap file, not zram. Key material lives in RAM and
    must never be paged anywhere. Undo with
    `sudo systemctl unmask swap.target`.

All five are deliberate on an air-gapped key printer and all five outlive
this script. tty1 is the front panel; the login prompt is on tty2 (Alt+F2).

TWO THINGS ARE ON THE BOOT PARTITION, in /boot/firmware/otp-identity, and
they are the only exceptions to "nothing survives a power cycle".

  * machine-id, because /etc is inside the overlay -- without it systemd
    calls every boot a first boot, runs preset-all every time, and re-enables
    units nobody chose.
  * credential, but ONLY once you have set a password with a userconf.txt of
    your own. It is the same `user:hash` line that file carried. Without it
    the password you set worked for exactly one boot: the hash goes to
    /etc/shadow, which is inside the overlay, and the wizard deletes the seed
    as the last step of applying it.

No SSH host key leaves the overlay, because this machine does not run sshd,
and no pad byte is written to that partition ever.

THE CREDENTIAL IS A PASSWORD HASH ON A PARTITION ANYONE CAN READ, and you
should decide that is acceptable rather than discover it. The boot partition
is vfat mounted with `defaults`, so every file on it is 0755 root:root --
readable by EVERY account on this machine and by anyone who can put the card
in a reader. A hash is not a password, but it can be attacked offline for as
long as somebody likes, so a weak or reused password there is a weak or
reused password everywhere you use it. The bytes are the same ones your own
userconf.txt already put on that same partition; what changed is that they
are no longer deleted.

Deleting userconf.txt does NOT take it off the card any more. This does:

    sudo rm -f /boot/firmware/otp-identity/credential

after which the account goes back to the random password the image was built
with, and the login prompt on tty2 is no use to you. Set a new one by writing
a fresh userconf.txt instead.

To change the software afterwards, take `boot=overlay` back out of
/boot/firmware/cmdline.txt, reboot, edit, and rerun this script.
EOF
else
    log "Image build complete"
fi

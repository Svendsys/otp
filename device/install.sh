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

log "Ensuring the groups the service runs with exist"
# otp-unit.service has SupplementaryGroups=lp gpio i2c input, and systemd
# REFUSES to start a unit naming a group that does not exist:
#
#   otp-unit.service: Failed to determine supplementary groups: No such process
#   otp-unit.service: Failed at step GROUP spawning /usr/bin/python3
#   otp-unit.service: Main process exited, code=exited, status=216/GROUP
#
# With Restart=on-failure that is a restart loop, on a device whose journal
# is deliberately volatile -- so the evidence dies with the power and the
# operator gets a dark panel and nothing to read. Measured in a booted VM;
# no chroot or container can see it, because neither starts the unit.
#
# Raspberry Pi OS happens to ship gpio and i2c, so the intended platform
# was fine. But this script claims to be the single source of truth for
# what the unit is, and it creates every other precondition it needs. A
# group that must exist for the service to start at all is one of those.
# Existing groups keep their GID; this only fills in what is missing.
for group in lp gpio i2c input; do
    if ! getent group "$group" >/dev/null 2>&1; then
        groupadd --system "$group"
        log "  created missing group: $group"
    fi
done

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
# NOT 1. cupsd does not queue past MaxJobs, it REFUSES: `lp: Too many
# active jobs.` The unattended sequence submits seven jobs, and measured
# against a real cupsd with MaxJobs 1 the status sheet and the manual went
# through and every one after them was rejected -- no tabula, no pad, and
# no sheet to say why. The unit now drains the queue before each submit so
# in practice only one is ever live; this is the headroom that keeps a
# timing race from costing the whole run rather than one poll.
set_cupsd MaxJobs 4
# CUPS defaults ErrorPolicy to retry-job, which never gives up. With a
# bounded MaxJobs that is a permanent wedge: a job to a printer that is
# off, jammed or unplugged stays active forever, so active_jobs() returns
# nonzero forever, cups_busy() is true forever, copy B can never be
# submitted, and the UI holds key material until someone pulls the power --
# the one thing the whole design is trying to avoid. abort-job lets the job
# die so the queue drains and the panel can say something. Verified with a
# real cupsd: a job to an unreachable printer was still active after 20s
# under retry-job, with lpstat cheerfully reporting "now printing".
#
# The cost, which the code has to carry: a job that FAILED leaves the queue
# exactly as empty as one that printed, so an empty `lpstat -o` is not
# proof anything reached paper. Cups.printer_fault() asks the queue's own
# state instead, and unattended.run believes that over the drain.
set_cupsd ErrorPolicy abort-job

# A stock CUPS creates none of these: there is no /usr/lib/tmpfiles.d/cups*
# and no RuntimeDirectory= in cups.service, so the directories the hardening
# points at exist only because this file makes them.
cat > /etc/tmpfiles.d/otp-unit-cups.conf <<'EOF'
d /run/cups 0710 root lp -
d /run/cups/spool 0710 root lp -
d /run/cups/tmp 1770 root lp -
d /run/cups/cache 0770 root lp -
d /run/ipp-usb 0750 root root -
L+ /var/log/ipp-usb - - - - /run/ipp-usb
EOF

# Create them NOW as well, not just at next boot. Two reasons, and the
# ordering against the cupsd -t gate below is the whole point: cupsd -t
# checks that every directory named in cups-files.conf exists, so running
# the gate first aborted provisioning on any unit where /run/cups did not
# happen to be there already -- "the generated CUPS configuration is
# invalid" when the configuration was fine. And systemd-tmpfiles is not
# usable in pi-gen's chroot, so install -d is what actually works there.
install -d -m 0710 -o root -g lp /run/cups /run/cups/spool
install -d -m 1770 -o root -g lp /run/cups/tmp
install -d -m 0770 -o root -g lp /run/cups/cache
install -d -m 0750 -o root -g root /run/ipp-usb
# Best effort on top, and only that: the directories already exist by here,
# and in a chroot this cannot work. Failure is not fatal, but it is not
# hidden either -- silencing it hid the one step whose failure stops cupsd
# from starting at all, with the reason going only to the journal.
if command -v systemd-tmpfiles >/dev/null; then
    systemd-tmpfiles --create /etc/tmpfiles.d/otp-unit-cups.conf \
        || log "systemd-tmpfiles declined; the directories above are already in place"
fi

# Prove the daemon will actually load what we just wrote. cupsd only reports
# a bad directive at startup, and otp-unit.service only Wants= cups, so
# without this the unit boots looking healthy and fails at print time.
# This runs after the directories exist, so it now checks the config rather
# than the order the script happens to do things in.
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

# The tier-3 image boot's eyes inside the guest. Inert on a real unit: the
# service runs only when the kernel command line carries otp.imgcheck, which
# nothing but harness/img-boot.sh puts there. Installed unconditionally
# anyway, so the image tier 3 boots is the image that ships rather than a
# variant built for testing -- an overlay check that ran against a specially
# prepared image would prove nothing about the artifact people flash.
if [ -f "$REPO_DIR/harness/img-guest-check.sh" ]; then
    install -m 0755 "$REPO_DIR/harness/img-guest-check.sh" \
        "$PREFIX/img-guest-check.sh"
    install -m 0644 "$REPO_DIR/device/systemd/otp-unit-imgcheck.service" \
        /etc/systemd/system/otp-unit-imgcheck.service
elif [ "$IMAGE_BUILD" -eq 1 ]; then
    # EXIT, like every other overlay precondition in this file. This one
    # printed a NOTE and carried on, which is how a build ships an image
    # whose overlay nothing can probe -- and the miss does not surface for
    # about seven minutes of pi-gen plus two emulated boots, arriving then
    # as "the guest never reported", which reads as a boot failure rather
    # than as the packaging error it is. The only way the file goes missing
    # in a chroot is the copy list in image/stage-otpunit/01-otpunit, which
    # now fails on the same absence at the other end.
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
if [ -f /etc/systemd/system/otp-unit-imgcheck.service ]; then
    systemctl enable otp-unit-imgcheck.service 2>/dev/null || true
fi
systemctl enable cups.service 2>/dev/null || true

# otp-unit takes tty1 to use as its front panel, so the login prompt that
# normally lives there is gone. Put one on tty2 instead: with no network
# and no SSH, Alt+F2 is the only way into a unit that will not come up.
# This costs nothing in exposure -- anyone at the keyboard already has the
# SD card, and the SD card is the whole device.
systemctl enable getty@tty2.service 2>/dev/null || true

# --- the boot has to be able to FINISH ------------------------------------
#
# MEASURED, on the built image, in run 31968966879. Both emulated boots ran
# for three minutes and NEITHER reached multi-user.target. Both consoles end
# on the same line:
#
#   Job systemd-networkd-wait-online.service/start running (2min 37s / no limit)
#
# "no limit" is that unit's own TimeoutStartSec=infinity. This appliance has
# no networkd configuration -- NetworkManager owns the link, and its own
# wait-online finished in seconds -- so networkd's wait never returns,
# network-online.target is never reached, and every job ordered after it
# stays queued for the life of the boot.
#
# WHAT THAT COSTS is not just a slow boot. cloud-init's cloud-config.service
# is ordered after network-online.target, and stock userconfig.service is
# `After=cloud-config.service`, so the wizard's start job sat in the queue
# for the whole of both boots -- the guest read `jobs=1` with the condition
# never evaluated -- and the valid /boot/firmware/userconf.txt seeded onto
# the card came back off it untouched. An operator's credentials IGNORED IN
# SILENCE is the exact outcome the drop-in below replaced a mask to avoid,
# arriving from a direction nobody had looked at. See issue #20.
#
# TWO LOCKS, because either alone leaves half the trap armed:
#
#   - The wait is masked. An unbounded wait for a network this appliance
#     does not have must not be able to hold a boot open, whatever pulls
#     network-online.target in next.
#   - cloud-init is switched off with its own documented kill switch. It is
#     what pulls network-online.target in here, it cost 57 seconds of every
#     boot doing nothing, and a provisioning agent that takes user-data off
#     the boot partition -- the partition an operator is told to write files
#     on -- has no business on an air-gapped key printer. Its generator
#     reads this file before it links cloud-init.target into
#     multi-user.target, so cloud-config.service is not in the transaction
#     at all and the wizard's `After=` on it is void rather than waiting.
#
# THE TWO LOCKS ARE NOT EQUALLY WELL MEASURED, and the difference is worth
# knowing before anyone trusts a green tier 3 here.
#
#   - The mask IS measured on the booted image:
#     harness/img-guest-check.sh reads is-enabled back off the running
#     machine and asks the job queue for its job
#     (network-wait-cannot-hold-the-boot-open).
#   - The kill switch is NOT. Its only coverage is that install.sh writes
#     the file -- tests/test_overlay_root.py runs this block into a fake
#     /etc and looks for it. An earlier version of this comment claimed it
#     was "measured by what it unblocks, which is the wizard's job count in
#     the same probe", and that is false: once the wait above is masked,
#     network-online.target is satisfied by NetworkManager-wait-online in
#     about thirty seconds, so a cloud-init that came back would let every
#     guest check pass, just more slowly. No probe here can tell the two
#     apart, and no check goes red if this line stops working.
#
# It stays for the reasons in its bullet -- 57 wasted seconds, and a
# provisioning agent reading user-data off an operator's partition on an
# air-gapped key printer -- not because anything is watching it. A check
# that could tell would have to read the boot's own timing or ask systemd
# whether cloud-init.target is in the transaction; neither exists yet.
systemctl mask systemd-networkd-wait-online.service
install -d /etc/cloud
: > /etc/cloud/cloud-init.disabled

# Raspberry Pi OS's first-boot user wizard (userconf-pi) decides whether
# to prompt from get_boot_cli alone: on a console-boot image it goes
# INTERACTIVE on every boot until someone answers a whiptail on tty8,
# holding multi-user.target open forever ("no limit"). pi-gen's
# DISABLE_FIRST_BOOT_USER_RENAME=1 only deletes the DESKTOP wizard's
# autostart file; it never touches this service, so tier 3 caught the
# built image parked on it -- and a flashed device would boot the same
# way, a dialog nobody can see holding the boot open.
#
# NOT masked, though an earlier fix did -- the review panel caught what
# that broke: userconf-service is also the ONLY consumer of the
# documented headless credential file /boot/firmware/userconf.txt, and
# masking it silently ignored operator-provided credentials, leaving
# the tty2 recovery getty below with no knowable login. The drop-in
# keeps both properties instead:
#
#   - the ConditionPathExists |= pair: the service runs ONLY when an
#     operator actually seeded userconf(.txt), so an unseeded boot
#     skips it instantly instead of prompting, and a seeded one gets
#     the stock non-interactive apply-and-delete path.
#   - StandardInput=null: a MALFORMED seed file would fall back to the
#     interactive prompt; with no tty it fails fast instead, the boot
#     completes (wanted-by, not required-by), and the file is left
#     renamed failed_userconf.txt on the boot partition as evidence.
#
# All three branches are asserted on the real image now rather than argued
# here (issue #20): tier 3 plants a valid seed before boot1 and requires it
# consumed with the hash in /etc/shadow and no wizard job, requires boot2 to
# skip the unit while leaving it enabled, and hands the shipped
# userconf-service a malformed seed with stdin closed to see it end and
# quarantine the file rather than hang. See harness/img-guest-check.sh.
#
# KNOWN, MEASURED, AND NOT YET DECIDED: THE SEED LASTS ONE BOOT. The apply
# path is `chpasswd -e` into /etc/shadow. /etc is inside the read-only root
# overlay this script installs below, so the new hash lives in the tmpfs and
# dies with the power. The seed itself is on the FAT partition, which is
# OUTSIDE the overlay, and userconf-service deletes it as the last step of a
# successful apply -- so the one file that could reapply the credential is
# destroyed by the boot that consumed it, and the account goes back to the
# random FIRST_USER_PASS pi-gen generated at build time, which nobody has.
# The operator gets a working password for exactly one boot.
#
# Tier 3 measures both halves of that already and neither reads as a
# contradiction: boot1 finds the seeded hash in /etc/shadow, and boot2 finds
# the seed gone from the card. What is missing is a DECISION, and it is not
# one to make silently in a comment. The three candidates:
#
#   - refuse the seed loudly, so an operator is told the credential path
#     does not work on an overlay root rather than discovering it at the
#     second power-on;
#   - persist the credential, which means writing outside the overlay (the
#     boot partition, or a bind-mounted /etc/shadow) and deciding what a
#     password hash sitting on a FAT partition costs on a key printer;
#   - keep the seed file, so the wizard reapplies it every boot -- which
#     leaves the operator's credential line readable in any card reader for
#     the life of the device.
#
# Every one of those is a security trade on a machine that prints one-time
# pads, so it belongs to the repository and not to this script. Until it is
# made, the behaviour above is the behaviour, and it is stated here and in
# the release note image.yml attaches to a tag rather than implied away. Do
# NOT "fix" this by adding a persistence mechanism without the decision.
#
# $BOOT_DIR, not a hardcoded /boot/firmware, and the heredoc is UNQUOTED so
# that it expands. The condition has to name the directory the firmware
# partition is actually mounted on, because that is where the operator's file
# lands: userconf-service reads
# `/usr/lib/raspberrypi-sys-mods/get_fw_loc`, which answers /boot on the
# pre-bookworm layout, and BOOT_DIR above falls back to /boot for the same
# reason. Written flat, the pair named a path that does not exist on such a
# machine -- the condition is then false forever, the unit never runs, and a
# seeded userconf.txt is ignored in exactly the silence this drop-in replaced
# a mask to avoid. Nothing else in the block contains a `$`.
systemctl unmask userconfig.service 2>/dev/null || true
install -d /etc/systemd/system/userconfig.service.d
cat > /etc/systemd/system/userconfig.service.d/otp-appliance.conf <<DROPIN
[Unit]
ConditionPathExists=|$BOOT_DIR/userconf
ConditionPathExists=|$BOOT_DIR/userconf.txt

[Service]
StandardInput=null
DROPIN

# AND THE LAST THING THAT APPLY PATH DOES, which is where it bites. Applying
# a seed ends in /usr/lib/userconf-pi/userconf, whose final act is
# `cancel-rename`, and cancel-rename ends -- on every machine that boots to a
# console, which is this one -- with
#
#   systemctl --quiet enable getty@tty1
#   systemctl --quiet --no-block start getty@tty1
#
# So the documented credential file, used as documented, puts a login prompt
# on the front panel's tty. otp-unit.service used to answer that with
# Conflicts=getty@tty1.service, and Conflicts is symmetric: starting the
# getty STOPS the unit. An operator who set their password the supported way
# lost the panel until the next power-cycle.
#
# A CONDITION, and not either of the two obvious alternatives:
#
#   - Masked, and that start FAILS. cancel-rename's exit status is that
#     command's, `userconf` passes it up, and userconf-service runs under
#     `sh -e` -- so it dies BEFORE deleting the applied seed, and stock
#     userconfig.service's Restart=on-failure turns that into a restart
#     loop printing two of the strings tier 3 fails a release on.
#   - Left to Conflicts=, and the panel dies as described.
#
# A condition-skipped start does neither: systemd reports the job done, no
# getty ever runs on tty1, the seed is deleted and the unit succeeds. The
# machine without a front panel still gets its login prompt -- including one
# where provisioning failed half way, which is when it is wanted most.
#
# WHAT THE CONDITION KEYS ON: the panel BEING GOING TO RUN, not its unit file
# being on disk. The single `ConditionPathExists=!/etc/systemd/system/otp-\
# unit.service` that used to be here got that wrong in the direction that
# costs a login. `systemctl mask otp-unit` REPLACES that path with a symlink
# to /dev/null, and ConditionPathExists follows symlinks -- /dev/null exists
# -- so the condition stayed false and no getty ever started, on a machine
# whose panel was masked and therefore never started either. `systemctl
# disable otp-unit` is the same story from the other end: it removes the
# multi-user.target.wants symlink and leaves the unit file exactly where it
# was. Either one left tty1 dark and login-less forever, which is precisely
# the half-provisioned state the paragraph above says the prompt is for.
#
# Evaluated by systemd itself (`systemd-analyze condition`), over the four
# states a machine can be in:
#
#                    old: PathExists=!unit     new: the pair below
#   healthy            getty skipped             getty skipped
#   panel masked       getty skipped   <-- bug   getty starts
#   panel disabled     getty skipped   <-- bug   getty starts
#   panel absent       getty starts              getty starts
#
# TWO conditions, both `|`-prefixed so they are TRIGGERING: systemd starts
# the unit when at least one triggering condition holds, so this reads "give
# tty1 a login if the panel is not enabled OR its unit is masked/gone",
# which is the OR the property needs. Un-prefixed they would be ANDed, and
# the getty would come back only on a machine that was both disabled and
# masked.
#
#   - the .wants symlink is what `enable` creates and `disable` removes, so
#     it answers "is the panel going to be pulled into the boot".
#   - ConditionFileNotEmpty, not PathExists, on the unit file: it requires a
#     REGULAR file of non-zero size, and a mask is a symlink to a
#     zero-length character device. That is the whole reason this line is
#     spelled differently from the one it replaces.
install -d /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/otp-appliance.conf <<'GETTY'
[Unit]
ConditionPathExists=|!/etc/systemd/system/multi-user.target.wants/otp-unit.service
ConditionFileNotEmpty=|!/etc/systemd/system/otp-unit.service
GETTY

# AND THE ONE ALREADY RUNNING, because a condition only stops a getty
# STARTING. On the documented "run this on a Pi you already have" path there
# is one running: Raspberry Pi OS Lite started it at boot, before this file
# existed. Conflicts= used to stop it as a side effect of starting the unit,
# and tier 2 measured what dropping that costs on a live machine, in the run
# that arrived without this line:
#
#   OTP-CHECK provision getty1-stopped FAIL getty@tty1=active
#   OTP-CHECK provision service-active FAIL is-active=inactive
#   systemd[1]: Started otp-unit.service - OTP pad print unit.
#   systemd[1]: otp-unit.service: Deactivated successfully.      (1s later)
#
# -- the panel takes /dev/tty1 with tty-force, the getty is hung up, its
# Restart=always brings it straight back, and its TTYVHangup takes the tty
# off the panel, whose Python then exits. The two boots after that reboot
# were 14/14 and 13/13, because by then the condition was in force and no
# getty ran at all.
#
# Stopped ONCE, explicitly, after the reload that puts the condition in
# force -- not a standing rule that anything starting a getty takes the unit
# down with it, which is the trade Conflicts= made. In pi-gen's chroot
# systemctl ignores start/stop requests, which is exactly right there: no
# getty is running, and the condition ships for the first boot.
systemctl daemon-reload
systemctl stop getty@tty1.service 2>/dev/null || true

# --- AND THE RELOAD AT THE END OF THAT SAME SCRIPT ------------------------
#
# cancel-rename's last act, after the getty, is
#
#   if systemctl --quiet is-active ssh; then
#       systemctl --quiet reload ssh
#   fi
#
# -- read out of userconf-pi 0.19's /usr/bin/cancel-rename, the version in
# archive.raspberrypi.com/debian trixie. On the image as it stood that
# reload killed sshd for the rest of the boot. Run 72's tier-3 console,
# boot 1:
#
#   systemd[1]: Reloading ssh.service - OpenBSD Secure Shell server...
#   sshd[744]: Received SIGHUP; restarting.
#   sshd[744]: fatal: Cannot bind any address.
#   systemd[1]: ssh.service: Failed with result 'exit-code'.
#
# ssh.service carries RestartPreventExitStatus=255, so nothing brings it
# back. The ONE boot an operator expects to be able to SSH into -- the boot
# that applies their seeded credentials -- ends with no SSH, in silence.
#
# WHY IT DIES, which is what decides which line fixes it. Debian's
# openssh-server 1:10.0p1-7+deb13u4 ships ssh.socket next to ssh.service:
#
#   [Socket]
#   ListenStream=22
#   Accept=no
#
# Accept=no with no Service= means the socket's implied service is
# ssh.service, so when both are enabled ssh.socket binds :22 first and
# systemd hands sshd the listening descriptor instead of letting it bind.
# sshd's SIGHUP handler closes its listeners and re-execs; the re-exec has
# no LISTEN_FDS left to adopt, ssh.socket still owns :22, and there is
# nothing it can bind. Both units are enabled because systemd runs
# `preset-all` on a FIRST boot -- and until the machine-id below is
# persisted, every boot of this appliance is a first boot.
#
# THE SOCKET IS WHAT IS MASKED, and not either alternative:
#
#   - mask ssh.service, and `is-active ssh` above is false so no reload
#     happens at all -- but that leaves ssh.socket listening on :22 for a
#     service that can never start, and takes SSH off the machine as a
#     side effect of a bug fix.
#   - a drop-in ExecReload that does nothing hides the fault instead of
#     fixing it, and a reload that silently does not reload is the shape of
#     defect this repository exists to refuse.
#
# With the socket masked, ssh.service starts standalone, sshd binds :22
# itself, and the SIGHUP re-exec rebinds it -- which is the ordinary path on
# every machine that is not socket-activated.
#
# MASKED, NOT DISABLED, for the reason the growfs mask below is: `disable`
# removes symlinks under /etc/systemd/system, which is inside the overlay,
# so it would last one boot and the next preset-all would put the socket
# back. A mask is a symlink to /dev/null written into the image.
#
# Bare, with no `|| true`, like the networkd-wait-online mask above: this
# runs in the pi-gen chroot against an image that has openssh-server in it,
# and a mask that silently failed to apply is the defect it exists to stop.
systemctl mask ssh.socket

# --- the read-only root overlay ------------------------------------------

# This section used to be a paragraph of advice printed at the end telling
# the operator to run raspi-config themselves. Nothing enabled the overlay:
# not this script, not the pi-gen stage, not the image. So the property the
# whole design rests on -- a power-cycle is a full reset -- existed only on
# units whose owner had followed a printed instruction, and no tier of the
# harness had ever booted a machine that had one. That is issue #9.
#
# NOT `raspi-config nonint enable_overlayfs`, and that is why this is
# hand-rolled rather than a one-line call. On bookworm and later raspi-config
# implements the overlay by installing Debian's `overlayroot` package and
# putting `overlayroot=tmpfs` on the kernel command line. overlayroot's
# initramfs script moves the root aside with `mount --move`, and the mount(8)
# an initramfs-tools initrd actually contains is klibc's, which has no such
# option:
#
#     $ /usr/lib/klibc/bin/mount --move /a /b
#     mount: invalid option --
#
# -- run against the klibc-utils 2.0.14-1 binary from trixie. That is the
# string the tier-2 guest printed immediately before `Kernel panic - not
# syncing: Attempted to kill init!` (issue #9's first comment), and it is not
# a property of that guest: Raspberry Pi OS trixie installs the same
# overlayroot and builds its initrd with the same initramfs-tools. It
# survives only where busybox happens to have been packed into the initramfs,
# because busybox's mount does accept --move. Whether this appliance boots is
# not allowed to depend on that.
#
# What runs instead is what raspi-config itself did before that switch, and
# it is initramfs-tools' own documented mechanism rather than anything
# invented here: `boot=overlay` makes the initrd source /scripts/overlay
# (mkinitramfs copies /etc/initramfs-tools/scripts into the image; init
# sources /scripts/${BOOT} before calling mountroot), and that file overrides
# local_mount_root to mount the card read-only and lay a tmpfs overlay over
# it. Every mount it runs is one klibc's mount accepts -- measured, including
# `-t overlay -o lowerdir=...,upperdir=...,workdir=...`.
#
# The failure modes are the right way round too. If the initramfs is missing
# or unreadable the firmware boots the kernel without one, `boot=overlay` is
# read by nothing, and the unit comes up on a writable root -- which tier 3
# now fails on. overlayroot's failure mode is a kernel panic on a device with
# no console.
#
# /etc/fstab is deliberately not touched. overlayroot rewrites it, which is
# most of what its init-bottom script is doing by the time it panics; this
# mechanism leaves the root entry alone and lets systemd-remount-fs remount
# whatever is at / with the options fstab gives for /. That is untested here
# and it is the first thing to look at if a tier-3 console ever shows
# systemd-remount-fs failing.
CMDLINE_TXT="$BOOT_DIR/cmdline.txt"
if [ ! -f "$CMDLINE_TXT" ]; then
    # The overlay is configured through the Raspberry Pi boot firmware, so
    # there is nothing to configure on a machine that has no cmdline.txt --
    # the tier-2 Debian guest, whose /boot/firmware is a bare FAT partition
    # made to mirror the Pi's geometry, is exactly that machine.
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
    # A copy of initramfs-tools 0.148.4's own local_mount_root with the
    # overlay steps inserted, rather than of raspi-config's older copy of the
    # same function: the version in trixie grew a check for a missing root=
    # and handles ROOTFSTYPE=auto, and a stale copy of a function this
    # important is a bug waiting for a kernel bump.
    cat > /etc/initramfs-tools/scripts/overlay <<'OVERLAY'
# Mount the root filesystem read-only under a RAM overlay.  -*- shell-script -*-
#
# initramfs-tools sources this file instead of nothing when the kernel
# command line says boot=overlay, after /scripts/local and before mountroot.
# Overriding local_mount_root is how mountroot reaches this code.

. /scripts/local

# THE ONE THING ALLOWED THROUGH THE OVERLAY, AND WHY IT HAS TO BE HERE.
#
# /etc is inside the overlay, so /etc/machine-id is whatever the card says
# plus whatever this boot wrote to a tmpfs. pi-gen ships it holding the word
# `uninitialized` -- read out of a stock Raspberry Pi OS Lite arm64 image --
# and systemd's documented reading of that word is "this is a first boot":
# PID 1 generates an ID, writes it to /etc (the tmpfs), creates
# /run/systemd/first-boot, and runs preset-all. The write dies with the
# power, so the NEXT boot is a first boot too, and the one after that.
# Measured consequences on run 72's console: ssh.socket enabled again every
# boot, regenerate_ssh_host_keys.service and sshd-keygen.service run again
# every boot, and the host keys of a machine that prints one-time pads
# change on every power cycle.
#
# IN THE INITRAMFS BECAUSE NOTHING LATER IS EARLY ENOUGH. PID 1 reads
# /etc/machine-id before it looks at a single unit, so no service, drop-in
# or generator can put the file there in time. The initrd is the last moment
# that exists, and this file is already the thing that assembles the root --
# so the exception rides with the mechanism it is an exception to, rather
# than in some other file that has to be kept in step with it.
#
# NARROW ON PURPOSE: one file, 33 bytes, named. Not /etc, not a list, and
# the SSH host keys are deliberately NOT done here -- they need modes a
# FAT partition cannot express, and sshd starts late enough that ordinary
# userspace can place them (see otp-unit-identity.service).
#
# WHERE IT COMES FROM: the FAT boot partition, the same partition
# otpunit/config.py already persists settings to and the only writable
# storage this design has outside the overlay. It is mounted READ-ONLY
# here; the copy that puts a machine-id there is made from userspace, on a
# partition systemd has mounted read-write by then.
#
# EVERY COMMAND BELOW IS ONE THE INITRD REALLY HAS. `mount`, `mkdir`, `cat`
# and `umount` are klibc-utils, which initramfs-tools depends on; nothing
# here needs busybox, which is the trap the header of this section is
# written around. And no module is needed either: in the kernel this image
# installs (linux-image-6.12.47+rpt-rpi-v8, and every -rpi-v8 build) fat,
# vfat, nls_cp437 and nls_ascii are all in modules.builtin, with
# CONFIG_FAT_DEFAULT_IOCHARSET="ascii" -- so a bare `mount -t vfat` works
# with an empty /lib/modules.
#
# IT NEVER PANICS. The overlay is boot-critical and panics on failure; an
# identity is not. Everything here is best effort: a card with no stored
# machine-id, an unreadable partition, or a stored value that is not 32 hex
# characters all leave /etc/machine-id exactly as the image shipped it,
# which is the behaviour this replaces rather than a new failure. What
# notices is tier 3, not the boot.
#
# THE MOUNTPOINT IS A VARIABLE for the same reason $BOOTDIR is one in the
# guest probe: tests/test_overlay_root.py runs this function against a tree
# it can build, with stub mount/umount on PATH. A hardcoded path would leave
# the only exercise of it an emulated boot of an image nobody can build
# locally, which is the one place a mistake here is expensive to find.
OTP_IDENTITY_MNT=/otp-boot

otp_restore_machine_id()
{
	local dev id

	mkdir -p "${OTP_IDENTITY_MNT}"
	# CANDIDATES, AND A MARKER, rather than a guess. On the Pi the FAT
	# partition is always partition 1 of the card the root came off, so the
	# first candidate is ${ROOT} with its partition number replaced; the
	# second is the literal device for the layout every image this project
	# builds has. Nothing is trusted just for mounting: the file has to be
	# under otp-identity/, a directory only device/install.sh's own
	# persistence puts there, so mounting the wrong thing does nothing.
	for dev in "${ROOT%p[0-9]}p1" /dev/mmcblk0p1; do
		[ -e "${dev}" ] || continue
		mount -r -t vfat "${dev}" "${OTP_IDENTITY_MNT}" 2>/dev/null || continue
		id=$(cat "${OTP_IDENTITY_MNT}/otp-identity/machine-id" 2>/dev/null)
		umount "${OTP_IDENTITY_MNT}" 2>/dev/null || true
		# 32 lower-case hex characters, which is the only thing systemd
		# accepts. A short, empty or corrupt value is dropped rather than
		# written: systemd would reject it and call the boot a first boot,
		# which is the state this is trying to leave.
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

	# -r, and this is the line that makes the card read-only for the whole
	# life of the boot: the running system never has a writable handle on
	# the filesystem it booted from.
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

	# PANIC rather than fall back to mounting the card read-write. A unit
	# that quietly came up without the overlay would keep on the card
	# everything the session wrote, which is the one thing this mechanism
	# exists to prevent, and it would say nothing about it.
	if ! mount -t overlay \
	     -o lowerdir=/lower,upperdir=/upper/data,workdir=/upper/work \
	     overlay "${rootmnt?}"; then
		panic "Failed to assemble the root overlay."
	fi

	# LAST, and only once the overlay is up: this writes THROUGH the
	# overlay into its tmpfs upper layer, so it has to have somewhere to
	# write to. See the comment on the function for why one file is
	# allowed past a mechanism whose whole purpose is that nothing is.
	otp_restore_machine_id
}
OVERLAY
    grep -qxF overlay /etc/initramfs-tools/modules 2>/dev/null \
        || echo overlay >> /etc/initramfs-tools/modules

    # Every installed kernel, read out of /lib/modules rather than from
    # `uname -r`. In pi-gen's chroot uname reports the BUILD HOST's kernel,
    # for which there are no modules and no initramfs can be built -- that is
    # what raspi-config's enable_overlayfs does, and the reason it cannot be
    # run at image-build time at all. The image also carries a second kernel
    # for the Pi 5, so this is a loop rather than a single version.
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
        # -c, not -u. pi-gen sets update_initramfs=no in
        # update-initramfs.conf so kernel installs do not build one, and
        # update-initramfs honours that setting on its update path only --
        # `-u` would print "Not updating initramfs." and exit 0, leaving the
        # overlay out of an initramfs that already existed.
        update-initramfs -c -k "$kern"
    done

    # The firmware is what loads it. auto_initramfs=1 makes it pick up
    # /boot/firmware/initramfs8 next to kernel8.img without naming a file, and
    # raspi-firmware's /etc/initramfs/post-update.d hook is what puts it
    # there. pi-gen's stage1 config.txt already carries this line; set_config
    # only adds it if some other config.txt does not.
    set_config "auto_initramfs=1"

    if ! grep -q "boot=overlay" "$CMDLINE_TXT"; then
        sed -i -e "s|^|boot=overlay |" "$CMDLINE_TXT"
    fi

    # pi-gen grows the root filesystem on first boot, keyed on this token and
    # on rpi-resize.service. An online resize of a filesystem mounted
    # read-only as the overlay's lower layer cannot work, and a first-boot
    # service that fails and reboots is a loop rather than a message. The
    # appliance has nothing to grow: what it writes is RAM, and the card is
    # never written after provisioning.
    sed -i -E 's/(^| )resize( |$)/\1/g' "$CMDLINE_TXT"
    # SYSTEMD HAS A SECOND GROWER, and until run 72 nothing here took it out.
    # rpi-resize.service pulls systemd-growfs-root.service in with a Wants=,
    # which makes disabling rpi-resize LOOK like it settles the matter --
    # but systemd-fstab-generator hooks that unit onto the root mount as
    # well, on every boot, with no symlink anywhere for `disable` to remove.
    # It therefore ran on every boot of every image this project has built,
    # and on an overlay root it cannot do anything but fail:
    #
    #   systemd-growfs[293]: File system "/" not backed by block device.
    #   systemd[1]: systemd-growfs-root.service: Failed with result 'exit-code'.
    #   [FAILED] Failed to start systemd-growfs-root.service - Grow Root File System.
    #
    # quoted from run 72's tier-3 console, both boots. `/` is an overlayfs
    # here by construction, so there is no block device to grow and never
    # will be. The `[FAILED]` line was always on the console -- run 68's
    # carries it too -- and nothing gated on it; the `Failed with result`
    # wording only reaches a console journald is forwarding to, which is
    # what issue #21 turned on. A red line on the console of an appliance
    # that prints key material teaches its operator to read past red.
    #
    # MASKED, not disabled, and that is the whole point: `disable` removes
    # symlinks and there are none, so the generator would hook it up again
    # on the next boot. A mask is a link to /dev/null in
    # /etc/systemd/system -- in the image, not in the overlay's tmpfs -- and
    # systemd refuses the unit whatever asks for it. Bare, with no
    # `|| true`, for the reason the networkd-wait-online mask above is bare:
    # this runs in the pi-gen chroot where that mask demonstrably lands, and
    # a mask that silently failed to apply is the defect it exists to stop.
    systemctl mask systemd-growfs-root.service
    systemctl disable rpi-resize.service 2>/dev/null || true

    # PROVE it rather than trust the sequence above, because every way this
    # can go wrong is silent. config.txt asking the firmware for an initramfs
    # that was never written, an initramfs written before the overlay script
    # existed, a boot= token that never reached the file -- each of them
    # leaves a unit that boots read-write and says nothing at all about it.
    #
    # NOT `lsinitramfs "$candidate" | grep -q scripts/overlay`, which is how
    # this was first written. This script runs under pipefail; `grep -q`
    # closes the pipe on its first match, lsinitramfs dies of SIGPIPE, and
    # the pipeline returns 141 -- so the condition is FALSE for exactly the
    # initramfs that passes. Measured with a 300k-line producer: `NO MATCH
    # (rc of pipeline was nonzero)`, rc=141. A real listing is thousands of
    # lines and scripts/overlay sorts early in it, so this would have failed
    # the build on a good image every time -- the sign-flipped version of
    # issue #14's defect, aimed at the release gate.
    OVERLAY_INITRAMFS=""
    for candidate in "$BOOT_DIR"/initramfs*; do
        [ -f "$candidate" ] || continue
        LISTING=$(lsinitramfs "$candidate" 2>/dev/null || true)
        case "$LISTING" in
            *scripts/overlay*)
                OVERLAY_INITRAMFS="$candidate"
                break
                ;;
        esac
    done
    if [ -z "$OVERLAY_INITRAMFS" ]; then
        echo "ERROR: no initramfs in $BOOT_DIR contains scripts/overlay, so" >&2
        echo "       boot=overlay would be read by nothing and the unit" >&2
        echo "       would come up on a writable root." >&2
        ls -la "$BOOT_DIR" >&2
        exit 1
    fi
    if ! grep -q "boot=overlay" "$CMDLINE_TXT"; then
        echo "ERROR: boot=overlay is not in $CMDLINE_TXT" >&2
        exit 1
    fi
    log "  overlay initramfs: $OVERLAY_INITRAMFS"

    # --- the identity the overlay would otherwise throw away --------------
    #
    # The userspace half of the exception the initramfs script above makes.
    # Two jobs, and the split between them is about WHEN, not about taste:
    #
    #   - the initrd RESTORES /etc/machine-id, because PID 1 reads that file
    #     before any unit exists;
    #   - this unit RECORDS it, and looks after the SSH host keys, because
    #     both need a mounted boot partition and the host keys need modes a
    #     FAT filesystem cannot carry.
    #
    # ONLY ON AN OVERLAY MACHINE, which is why it is inside this branch. A
    # writable root keeps /etc/machine-id and /etc/ssh by itself; copying
    # SSH private keys onto a FAT partition there would be exposure bought
    # for nothing. The tier-2 Debian guest has no cmdline.txt, takes the
    # other branch, and never gets this unit.
    install -m 0755 "$REPO_DIR/device/persist-identity.sh" \
        "$PREFIX/persist-identity.sh"
    install -m 0644 "$REPO_DIR/device/systemd/otp-unit-identity.service" \
        /etc/systemd/system/otp-unit-identity.service
    systemctl daemon-reload
    systemctl enable otp-unit-identity.service
fi

# WHAT THIS SCRIPT DID TO SOMEONE ELSE'S MACHINE, said out loud. The
# documented path is "run this on a Pi you already have", and two of the
# steps above are not confined to the unit: they change how the whole box
# behaves and they do not come back on their own. A summary that lists only
# the overlay reads as though nothing else was touched.
if [ "$IMAGE_BUILD" -eq 0 ]; then
    log "Done. Reboot to start the unit."
    cat <<'EOF'

The root filesystem is now a read-only overlay: the card is mounted
read-only and everything written to / goes to a tmpfs on top of it, so a
power-cycle is a full reset and nothing a session touched survives it.
Settings still persist -- they live on the boot partition, which is outside
the overlay.

Three machine-wide changes were made as well -- two because an unbounded
network wait held this image's boot open for its whole length, and one
because the overlay made every boot look like a first boot (see the
comments in this script):

  * systemd-networkd-wait-online.service is MASKED. Nothing on this machine
    can wait for network-online.target through networkd any more, including
    software installed later. Undo with
    `sudo systemctl unmask systemd-networkd-wait-online.service`.
  * cloud-init is switched off permanently, via /etc/cloud/cloud-init.disabled.
    This machine will not run any cloud-init datasource again -- including
    user-data written to the boot partition. Undo by deleting that file.
  * ssh.socket is MASKED, so sshd is never socket-activated here. It had to
    be: with the socket holding port 22, the reload that userconf-pi runs at
    the end of a seeded first boot left sshd unable to bind and
    RestartPreventExitStatus=255 kept it down for the rest of the boot. Undo
    with `sudo systemctl unmask ssh.socket`. ssh.service itself is untouched.

All three are deliberate on an air-gapped key printer and all three outlive
this script. tty1 is the front panel; the login prompt is on tty2 (Alt+F2).

THIS UNIT'S IDENTITY IS ON THE BOOT PARTITION, in /boot/firmware/otp-identity:
its machine-id and a copy of its SSH host keys. That is the one exception to
"nothing survives a power cycle", and it exists because /etc is inside the
overlay -- without it systemd calls every boot a first boot and the host keys
change every time the power is pulled. FAT has no permission bits, so the
private keys there are readable by anyone who can mount the card. That is the
same set of people who can already read them off the root filesystem, which
is not encrypted either, but it is worth knowing before you hand the card to
anyone.

To change the software afterwards, take `boot=overlay` back out of
/boot/firmware/cmdline.txt, reboot, edit, and rerun this script.
EOF
else
    log "Image build complete"
fi

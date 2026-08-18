#!/bin/bash -e
#
# Copy the repository into the image and run the same install.sh that
# provisions a running Pi. One provisioning path, two entry points -- if
# this stage did its own thing, the fast iteration loop on real hardware
# would stop predicting what the image does.

REPO_SRC="${OTP_REPO_DIR:?OTP_REPO_DIR must point at the repository}"
STAGING=/tmp/otp-src

install -d "${ROOTFS_DIR}${STAGING}"

# Everything install.sh reads, and nothing else. Notably not .git.
#
# `harness` is in the list for one file: img-guest-check.sh, which
# install.sh puts in /opt/otp-unit for otp-unit-imgcheck.service to run. The
# whole staging directory is deleted below, so the rest of the harness never
# reaches the image.
#
# REQUIRED, not copied-if-present. The `if [ -e ]` this replaces made a
# missing item a silent no-op: install.sh then died inside the chroot with
# a path error for the lucky ones, and for `harness` it did not die at all
# -- it printed a NOTE and built an image whose overlay nothing could probe,
# which only shows up two emulated boots later as a guest that never
# reported. Deleting a name from this list is the one way that happens, so
# this is where it stops.
for item in otpunit codewords device harness otp_generator.py otp.md; do
	if [ ! -e "${REPO_SRC}/${item}" ]; then
		echo "ERROR: ${REPO_SRC}/${item} is missing, and install.sh reads" >&2
		echo "       it inside the chroot. Refusing to build an image" >&2
		echo "       without it." >&2
		exit 1
	fi
	cp -a "${REPO_SRC}/${item}" "${ROOTFS_DIR}${STAGING}/"
done

# The one genuinely optional item: install.sh guards its own use of the
# manual PDFs, and an image without them boots and prints pads.
if [ -e "${REPO_SRC}/assets" ]; then
	cp -a "${REPO_SRC}/assets" "${ROOTFS_DIR}${STAGING}/"
fi

if [ ! -f "${ROOTFS_DIR}${STAGING}/assets/otp-manual-a5.pdf" ]; then
	echo "WARNING: manual PDFs missing; PRINT MANUAL will be unavailable." >&2
	echo "         Run image/render-manual.sh before building." >&2
fi

# --- cloud-init, removed rather than left switched off --------------------
#
# WHAT THE BUILD KNOB DOES NOT DO. image/build.sh sets ENABLE_CLOUD_INIT=0,
# and reading that line it looks as though the image has no cloud-init on it.
# It has. pi-gen's stage2/04-cloud-init has two halves and the variable
# guards one: 01-run.sh, which preseeds a NoCloud datasource, checks it;
# 00-packages, which lists `cloud-init` and `rpi-cloud-init-mods`, is
# collected and installed with no condition anywhere in that path. So every
# image this project has built shipped a provisioning agent, its units and
# its generator, and nobody went looking for a cloud-init problem because the
# build config said cloud-init was off. Issue #34.
#
# WHY REMOVE IT RATHER THAN DISABLE IT. rpi-cloud-init-mods ships
# /etc/cloud/cloud.cfg.d/99_raspberry-pi.cfg, which is
#
#     datasource_list: [ NoCloud, None ]
#     datasource:
#       NoCloud:
#         seedfrom: file:///boot/firmware
#
# -- a provisioning agent that reads user-data off the one partition an
# operator is told to write files on, on an air-gapped one-time-pad printer
# whose threat model is somebody holding the card. It also cost 57 seconds of
# every boot finding nothing there, and it is what pulled network-online.target
# into the transaction that ended both boots of run 31968966879 without either
# reaching multi-user.target.
#
# BEFORE install.sh, deliberately, and the ordering is the point rather than
# tidiness. install.sh writes cloud-init's own kill switch,
# /etc/cloud/cloud-init.disabled, and that file is the belt to this purge's
# braces: it is what disables a cloud-init that some later dependency drags
# back in, and it is what a hand-provisioned Pi gets, where no purge ever
# ran. dpkg removes a package's conffiles on purge and then removes the
# directories it owned once they are empty -- /etc/cloud is one of those --
# so a purge ordered after the write would leave the marker standing only
# because dpkg will not delete a directory holding a file it does not own.
# Ordering it first does not depend on that.
# harness/img-guest-check.sh reads the outcome back off the booted image
# either way, as cloud-init-kill-switch-survives-the-purge.
#
# WHAT APT WOULD TAKE WITH IT, asked before it takes anything. `apt-get purge
# -y` removes every package that depends on the named ones, without a
# prompt, so a Raspberry Pi OS that had grown a dependency on either would
# have the dependent silently deleted out of a key printer's image and the
# first anyone heard of it would be a unit that no longer boots. Checked
# against the archives on 2026-08-18: nothing in the Raspberry Pi archive's
# trixie/main declares any relationship on rpi-cloud-init-mods, and in Debian
# trixie/main only debian-cloud-images-packages -- which Raspberry Pi OS Lite
# does not carry -- depends on cloud-init. That is a claim about two Packages
# indexes, not about the rootfs in front of us, so the rootfs is asked
# directly and the build stops rather than shipping the difference.
CLOUD_PACKAGES="cloud-init rpi-cloud-init-mods"
CLOUD_PACKAGES_RE="$(echo "$CLOUD_PACKAGES" | tr ' ' '|')"

# The simulation reads the chroot's own dpkg state and needs no network,
# which is the same reason install.sh runs below with --skip-apt. `Purg` and
# `Remv` both count: apt prints the first for the packages it purges and the
# second for anything it removes on the way, and it is the second that would
# be the finding.
#
# ONE COMMAND PER LINE IN HERE, however long the line gets, and this is
# the reason rather than a preference. on_chroot's delimiter is unquoted,
# so the HOST shell expands this body before capsh hands it to `sh -e` in
# the chroot -- which means a `\` at end of line is joined by the host,
# and `<<-` has already stripped only the FIRST physical line's tab by
# then. Measured: a three-line pipeline arrived in the chroot as one line
# with two stray tabs in the middle of it. It runs; it reads like a fault
# in the build log, which is the one place anybody would be looking.
on_chroot <<-EOF
	set -e
	unexpected=\$(apt-get -s purge -y ${CLOUD_PACKAGES} | awk '\$1 == "Remv" || \$1 == "Purg" { print \$2 }' | grep -vxE '${CLOUD_PACKAGES_RE}' || true)
	if [ -n "\$unexpected" ]; then
		echo "ERROR: purging cloud-init would also remove:" >&2
		echo "\$unexpected" | sed 's/^/         /' >&2
		echo "       Something in this image depends on it, which is the" >&2
		echo "       one thing issue #34 said had to be confirmed before" >&2
		echo "       the packages went. Refusing to build." >&2
		exit 1
	fi
	apt-get purge -y ${CLOUD_PACKAGES}
EOF

# Packages are already installed from 00-packages, so skip apt in the
# chroot: it has no network and does not need one.
on_chroot <<-EOF
	set -e
	cd ${STAGING}
	./device/install.sh --image-build --skip-apt
	rm -rf ${STAGING}
EOF

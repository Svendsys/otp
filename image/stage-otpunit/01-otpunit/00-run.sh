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

# Packages are already installed from 00-packages, so skip apt in the
# chroot: it has no network and does not need one.
on_chroot <<-EOF
	set -e
	cd ${STAGING}
	./device/install.sh --image-build --skip-apt
	rm -rf ${STAGING}
EOF

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
for item in otpunit codewords device harness otp_generator.py otp.md assets; do
	if [ -e "${REPO_SRC}/${item}" ]; then
		cp -a "${REPO_SRC}/${item}" "${ROOTFS_DIR}${STAGING}/"
	fi
done

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

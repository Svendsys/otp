#!/bin/bash -e
#
# Copy the repository into the image and run the same install.sh that
# provisions a running Pi. One provisioning path, two entry points -- if
# this stage did its own thing, the fast iteration loop on real hardware
# would stop predicting what the image does.

REPO_SRC="${OTP_REPO_DIR:?OTP_REPO_DIR must point at the repository}"
STAGING=/tmp/otp-src

install -d "${ROOTFS_DIR}${STAGING}"

# Everything install.sh reads, and nothing else (notably not .git). `harness`
# is there for img-guest-check.sh only; the staging directory is deleted
# below. Required, not copied-if-present: a missing item must stop the build
# here rather than ship an image whose overlay nothing can probe.
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
# ENABLE_CLOUD_INIT=0 in image/build.sh only stops pi-gen preseeding a
# datasource; stage2 still installs cloud-init and rpi-cloud-init-mods
# (issue #34). The latter points NoCloud at file:///boot/firmware, so
# cloud-init would read user-data off the partition an operator writes files
# on, and it pulls network-online.target into every boot.
#
# Before install.sh, which writes /etc/cloud/cloud-init.disabled: a purge
# ordered after that would be removing /etc/cloud around the marker.
#
# `apt-get purge -y` also removes every dependent package without a prompt,
# so the plan is simulated first and the build stops if it names anything
# else.
CLOUD_PACKAGES="cloud-init rpi-cloud-init-mods"
CLOUD_PACKAGES_RE="$(echo "$CLOUD_PACKAGES" | tr ' ' '|')"

# The simulation must prove it ran before its silence means anything: its
# exit status is checked, and the plan must name both packages (for an
# unknown package apt exits 100 and prints no plan at all).
#
# `Purg` and `Remv`: under `purge` apt marks every scheduled deletion Purg,
# so that arm is what catches a dependent; Remv guards against the command
# being edited to `remove`.
#
# `-vxE`: the whole-line anchor matters, because Debian ships packages whose
# names contain cloud-init (cloud-initramfs-growroot and relatives).
#
# `-y` on the simulation: this heredoc is the shell's own stdin, so an apt
# that prompted would read the rest of the block as the answer.
#
# One command per line: on_chroot's delimiter is unquoted, so the host shell
# expands the body and joins `\` continuations, leaving stray tabs.
#
# on_chroot runs the block under `bash -e`; it is kept POSIX because
# tests/test_overlay_root.py runs it under `sh`.
on_chroot <<-EOF
	set -e
	if ! sim=\$(apt-get -s purge -y ${CLOUD_PACKAGES}); then
		echo "ERROR: apt-get -s purge -y ${CLOUD_PACKAGES}" >&2
		echo "       failed in the chroot; apt's own message is above." >&2
		echo "       An unread plan is not a plan that named only" >&2
		echo "       cloud-init, and the purge below does not run on the" >&2
		echo "       strength of one. Refusing to build." >&2
		exit 1
	fi
	scheduled=\$(echo "\$sim" | awk '\$1 == "Remv" || \$1 == "Purg" { print \$2 }')
	for want in ${CLOUD_PACKAGES}; do
		if ! echo "\$scheduled" | grep -qxF "\$want"; then
			echo "ERROR: apt-get -s purge did not schedule \$want for removal." >&2
			echo "       The refusal below reads that plan for names it did" >&2
			echo "       not expect; a plan missing the names it DID expect" >&2
			echo "       is a simulation that did not happen rather than a" >&2
			echo "       clean one. Refusing to build." >&2
			exit 1
		fi
	done
	unexpected=\$(echo "\$scheduled" | grep -vxE '${CLOUD_PACKAGES_RE}' || true)
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

# An image-wide login, only when the builder asked for one. -e because the
# value is already a crypt(3) string; quoted heredoc so its `$` survive.
if [ -n "${OTP_USER_PASS_HASH:-}" ]; then
	case "${OTP_USER_PASS_HASH}" in
	\$*) ;;
	*)
		echo "ERROR: OTP_USER_PASS_HASH is not a crypt(3) string. Make" >&2
		echo "       one with: openssl passwd -6" >&2
		exit 1
		;;
	esac
	printf '%s:%s\n' "${FIRST_USER_NAME}" "${OTP_USER_PASS_HASH}" \
		> "${ROOTFS_DIR}/tmp/otp-pass"
	on_chroot <<-'EOF'
		set -e
		chpasswd -e < /tmp/otp-pass
		rm -f /tmp/otp-pass
	EOF
fi

# --skip-apt: pi-gen has already installed the packages, and the chroot has
# no network.
on_chroot <<-EOF
	set -e
	cd ${STAGING}
	./device/install.sh --image-build --skip-apt
	rm -rf ${STAGING}
EOF

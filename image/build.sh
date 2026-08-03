#!/usr/bin/env bash
#
# Build a flashable OTP print unit image.
#
#   ./image/build.sh
#
# Clones pi-gen, generates its config and custom stage from this repo, and
# runs the Docker build. Works on x86 and Apple Silicon; x86 hosts need
# binfmt_misc registered for arm64 (see docs/IMAGE.md).
#
# Output lands in image/deploy/.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_DIR="$REPO_DIR/image"
PI_GEN_DIR="$IMAGE_DIR/pi-gen"
PI_GEN_URL="${PI_GEN_URL:-https://github.com/RPi-Distro/pi-gen.git}"
PI_GEN_BRANCH="${PI_GEN_BRANCH:-master}"

log() { printf '\n== %s\n' "$*"; }

command -v docker >/dev/null || { echo "ERROR: docker is required" >&2; exit 1; }


# --- the manual --------------------------------------------------------

if [ ! -f "$REPO_DIR/assets/otp-manual-a5.pdf" ]; then
    log "Rendering the manual"
    # Rendered here rather than on the Pi so the unit needs no pandoc and no
    # network, and so the layout is fixed at build time.
    "$IMAGE_DIR/render-manual.sh"
fi


# --- pi-gen ------------------------------------------------------------

if [ ! -d "$PI_GEN_DIR" ]; then
    log "Cloning pi-gen"
    git clone --depth 1 --branch "$PI_GEN_BRANCH" "$PI_GEN_URL" "$PI_GEN_DIR"
fi

log "Configuring"
cp -a "$IMAGE_DIR/stage-otpunit" "$PI_GEN_DIR/"
chmod +x "$PI_GEN_DIR/stage-otpunit/prerun.sh" \
         "$PI_GEN_DIR/stage-otpunit"/*/*.sh

# stage2 is Raspberry Pi OS Lite and exports an image of its own; suppress
# it so the build produces only the print unit.
touch "$PI_GEN_DIR/stage2/SKIP_IMAGES"

# Packages come from the same manifest install.sh uses.
grep -vE '^\s*(#|$)' "$REPO_DIR/device/packages.txt" | tr '\n' ' ' \
    > "$PI_GEN_DIR/stage-otpunit/00-packages"

# STAGE_LIST has to be explicit. Under pi-gen's default `stage*` glob,
# "stage-otpunit" sorts BEFORE "stage0" ('-' is 0x2D, '0' is 0x30) and would
# run first, against an empty rootfs.
cat > "$PI_GEN_DIR/config" <<EOF
IMG_NAME='otp'
RELEASE='trixie'
ARCH='arm64'
STAGE_LIST='stage0 stage1 stage2 stage-otpunit'
TARGET_HOSTNAME='otp-unit'
FIRST_USER_NAME='otp'
DISABLE_FIRST_BOOT_USER_RENAME=1
ENABLE_SSH=0
LOCALE_DEFAULT='en_GB.UTF-8'
KEYBOARD_KEYMAP='gb'
TIMEZONE_DEFAULT='Europe/London'
DEPLOY_COMPRESSION='xz'
DEPLOY_DIR='$IMAGE_DIR/deploy'
EOF

# The stage script reads the repository from the host, not the chroot.
export OTP_REPO_DIR="$REPO_DIR"

log "Building (this takes roughly half an hour)"
cd "$PI_GEN_DIR"
PIGEN_DOCKER_OPTS="-e OTP_REPO_DIR=/otp-repo -v ${REPO_DIR}:/otp-repo:ro" \
    ./build-docker.sh

log "Done"
ls -lh "$IMAGE_DIR/deploy/" 2>/dev/null || true
cat <<'EOF'

Flash the .img.xz with Raspberry Pi Imager or:

    xzcat image/deploy/*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress

First boot takes a minute or so. The unit needs no network and no keyboard:
plug in a USB printer and use the three buttons.

Then enable the read-only overlay, which is what makes a power-cycle a full
reset (see docs/IMAGE.md):

    sudo raspi-config nonint enable_overlayfs
EOF

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
# The arm64 branch, not master. pi-gen hardcodes `export ARCH=armhf` *after*
# sourcing config, so setting ARCH in the config file is silently ignored
# and master always produces a 32-bit image.
PI_GEN_BRANCH="${PI_GEN_BRANCH:-arm64}"

log() { printf '\n== %s\n' "$*"; }

for tool in docker pandoc weasyprint; do
    command -v "$tool" >/dev/null || {
        echo "ERROR: $tool is required. See docs/IMAGE.md." >&2
        exit 1
    }
done
# build-docker.sh runs `which qemu-aarch64` on the HOST and exits if it is
# missing, so registering binfmt via a container is not enough on x86.
#
# It has to be the NON-static name. qemu-user-static ships only
# qemu-aarch64-static, which this check used to accept -- passing here and
# then failing inside pi-gen with a much less obvious message.
case "$(uname -m)" in
    aarch64|arm*) ;;
    *)
        if ! command -v qemu-aarch64 >/dev/null; then
            echo "ERROR: qemu-aarch64 is required on non-arm64 hosts." >&2
            echo "       sudo apt-get install -y qemu-user qemu-user-binfmt" >&2
            echo "       (qemu-user-static alone is NOT enough: pi-gen looks" >&2
            echo "        for the non-static interpreter on PATH.)" >&2
            exit 1
        fi
        ;;
esac


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

# Packages come from the same manifest install.sh uses. This must live in
# the SUB-stage directory: pi-gen looks for ${i}-packages inside each
# sub-stage and only descends into directories, so a file at the stage root
# is skipped without a word and the image ships with none of them.
grep -vE '^\s*(#|$)' "$REPO_DIR/device/packages.txt" | tr '\n' ' ' \
    > "$PI_GEN_DIR/stage-otpunit/01-otpunit/00-packages"

# STAGE_LIST has to be explicit. Under pi-gen's default `stage*` glob,
# "stage-otpunit" sorts BEFORE "stage0" ('-' is 0x2D, '0' is 0x30) and would
# run first, against an empty rootfs.
# DEPLOY_DIR is deliberately left at pi-gen's default. It is honoured
# *inside* the container, so pointing it at a host path just writes the
# image somewhere the container's own `docker cp` never looks.
# FIRST_USER_PASS is required whenever DISABLE_FIRST_BOOT_USER_RENAME is
# set; pi-gen refuses to start without it. The account has no SSH and no
# console use -- the unit boots straight into the panel -- but set
# OTP_USER_PASS_HASH if you want a known one.
cat > "$PI_GEN_DIR/config" <<EOF
IMG_NAME='otp'
RELEASE='trixie'
STAGE_LIST='stage0 stage1 stage2 stage-otpunit'
TARGET_HOSTNAME='otp-unit'
FIRST_USER_NAME='otp'
FIRST_USER_PASS='${OTP_USER_PASS_HASH:-$(head -c 18 /dev/urandom | base64)}'
DISABLE_FIRST_BOOT_USER_RENAME=1
ENABLE_SSH=0
ENABLE_CLOUD_INIT=0
LOCALE_DEFAULT='en_GB.UTF-8'
KEYBOARD_KEYMAP='gb'
TIMEZONE_DEFAULT='Europe/London'
DEPLOY_COMPRESSION='xz'
EOF

log "Building (this takes roughly half an hour)"
cd "$PI_GEN_DIR"
PIGEN_DOCKER_OPTS="-e OTP_REPO_DIR=/otp-repo -v ${REPO_DIR}:/otp-repo:ro" \
    ./build-docker.sh

log "Collecting the image"
mkdir -p "$IMAGE_DIR/deploy"
find "$PI_GEN_DIR" -name '*.img.xz' -newer "$PI_GEN_DIR/config" \
    -exec cp -v {} "$IMAGE_DIR/deploy/" \;
if ! ls "$IMAGE_DIR"/deploy/*.img.xz >/dev/null 2>&1; then
    echo "ERROR: the build produced no image. Check $PI_GEN_DIR/work/*/build.log" >&2
    exit 1
fi

log "Done"
ls -lh "$IMAGE_DIR/deploy/"
cat <<'EOF'

Flash the .img.xz with Raspberry Pi Imager or:

    xzcat image/deploy/*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress

First boot takes a minute or so. The unit needs no network and no keyboard:
plug in a USB printer and use the three buttons.

The read-only overlay is already on -- device/install.sh enables it during
the build, so a power-cycle is a full reset from the first boot. Confirm it
with `findmnt /`, which should say `overlay` (see docs/IMAGE.md).
EOF

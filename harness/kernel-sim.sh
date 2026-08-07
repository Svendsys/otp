#!/usr/bin/env bash
#
# Stand up kernel-level stand-ins for the hardware this unit talks to, so
# the real driver code runs against real kernel interfaces.
#
#   ./harness/kernel-sim.sh up      bring up what this kernel can offer
#   ./harness/kernel-sim.sh down    tear it all down
#   ./harness/kernel-sim.sh status  what is currently up
#
# The point is NOT to emulate a Raspberry Pi. Emulating a Pi gives you a Pi
# with nothing plugged into it, which is the one configuration the unit
# already handles and the test suite already covers. What has never run is
# the code that talks to hardware that IS present: gpiozero opening a real
# gpiochip, luma driving a real SMBus, the DRM probe reading a real
# connector. These simulators provide exactly that, on any Linux box, with
# no Pi involved.
#
#   gpio-sim   a virtual gpiochip whose line values are driven from sysfs.
#              A button press through the real gpiozero -> lgpio -> kernel
#              path.
#   i2c-stub   a fake SMBus device at 0x3C. The SSD1306 init sequence
#              really runs; there is simply nothing to look at.
#   vkms       a virtual DRM device, so /sys/class/drm/card*/status is a
#              real file with a real answer rather than a monkeypatch.
#   uinput     a virtual keyboard, so /proc/bus/input/devices carries a
#              real kbd handler.
#
# Needs root and a kernel with these modules. Everything is best effort and
# reported individually: a kernel missing one of them should still exercise
# the other three, and the tests skip what is not up rather than failing.
set -euo pipefail

STATE=/run/otp-kernel-sim
GPIO_SIM_NAME=otp
OLED_ADDRESS=0x3C

log() { printf '%s\n' "$*" >&2; }

have_module() {
    # Built in counts as available, and modinfo does not know about builtins.
    local name="${1//-/_}"
    grep -qw "$name" /proc/modules 2>/dev/null && return 0
    modinfo "$1" >/dev/null 2>&1 && return 0
    grep -qw "$name" "/lib/modules/$(uname -r)/modules.builtin" 2>/dev/null
}

load() {
    if ! have_module "$1"; then
        log "  $1: not available in this kernel"
        return 1
    fi
    # Already loaded is success, not failure.
    modprobe "$@" 2>/dev/null || {
        log "  $1: modprobe failed"
        return 1
    }
    return 0
}

# --- configfs, which gpio-sim is driven through ------------------------

mount_configfs() {
    [ -d /sys/kernel/config/gpio-sim ] && return 0
    mkdir -p /sys/kernel/config 2>/dev/null || true
    mountpoint -q /sys/kernel/config && return 0
    mount -t configfs none /sys/kernel/config 2>/dev/null || return 1
}

# --- gpio-sim ----------------------------------------------------------

up_gpio() {
    load gpio-sim || return 1
    if ! mount_configfs; then
        log "  gpio-sim: configfs is not mountable"
        return 1
    fi
    local root=/sys/kernel/config/gpio-sim/$GPIO_SIM_NAME
    if [ -d "$root" ]; then
        log "  gpio-sim: already up"
    else
        mkdir -p "$root/bank0"
        # 32 lines so the unit's GPIO 5, 6 and 13 all exist at their real
        # offsets. gpiozero addresses pins by BCM number, and the whole
        # point is to exercise the shipped pin numbers rather than
        # renumber them for the test.
        echo 32 > "$root/bank0/num_lines"
        echo 1 > "$root/live"
    fi

    local dev chip
    dev=$(cat "$root/dev_name")
    chip=$(cat "$root/bank0/chip_name")
    local control="/sys/devices/platform/$dev/$chip"
    if [ ! -d "$control" ]; then
        log "  gpio-sim: live, but $control did not appear"
        return 1
    fi

    # Buttons connect their GPIO to ground and the internal pull-up is
    # enabled in software, so idle is HIGH. Start every line there or the
    # first read looks like three buttons held down.
    local pin
    for pin in 5 6 13; do
        [ -w "$control/sim_gpio$pin/pull" ] && \
            echo pull-up > "$control/sim_gpio$pin/pull"
    done

    mkdir -p "$STATE"
    printf '%s\n' "$control" > "$STATE/gpio-control"
    printf '%s\n' "${chip#gpiochip}" > "$STATE/gpio-chip"
    log "  gpio-sim: $chip  (drive lines under $control)"
}

# --- i2c-stub ----------------------------------------------------------

up_i2c() {
    load i2c-stub "chip_addr=$OLED_ADDRESS" || return 1
    # i2c-dev is what creates /dev/i2c-N, which is what smbus2 opens. The
    # stub alone registers an adapter with no character device.
    load i2c-dev || log "  i2c-dev: not available; /dev/i2c-* may be absent"

    local path bus
    for path in /sys/bus/i2c/devices/i2c-*; do
        [ -r "$path/name" ] || continue
        # i2c-stub names its adapter "SMBus stub driver"; match loosely in
        # case that wording ever shifts.
        case "$(cat "$path/name")" in
            *stub*)
                bus="${path##*/i2c-}"
                mkdir -p "$STATE"
                printf '%s\n' "$bus" > "$STATE/i2c-bus"
                log "  i2c-stub: bus $bus at $OLED_ADDRESS (/dev/i2c-$bus)"
                return 0
                ;;
        esac
    done
    log "  i2c-stub: loaded but no stub adapter appeared"
    return 1
}

# --- vkms --------------------------------------------------------------

up_drm() {
    load vkms || return 1
    local status found=""
    for status in /sys/class/drm/card*/status; do
        [ -r "$status" ] || continue
        found="$found ${status%/status}=$(cat "$status")"
    done
    if [ -z "$found" ]; then
        log "  vkms: loaded but no DRM connector appeared"
        return 1
    fi
    mkdir -p "$STATE"
    : > "$STATE/vkms"
    log "  vkms:$found"
}

# --- uinput ------------------------------------------------------------

up_keyboard() {
    load uinput || return 1
    if ! python3 -c "import evdev" 2>/dev/null; then
        log "  uinput: python3-evdev is not installed; no virtual keyboard"
        return 1
    fi
    mkdir -p "$STATE"
    # Holds the device open in the background; the node disappears with it.
    python3 "$(dirname "$0")/uinput-keyboard.py" "$STATE/keyboard.ready" &
    printf '%s\n' "$!" > "$STATE/keyboard.pid"

    local waited=0
    while [ ! -f "$STATE/keyboard.ready" ] && [ "$waited" -lt 50 ]; do
        sleep 0.1
        waited=$((waited + 1))
    done
    if [ ! -f "$STATE/keyboard.ready" ]; then
        log "  uinput: the virtual keyboard did not come up"
        return 1
    fi
    log "  uinput: virtual keyboard at $(cat "$STATE/keyboard.ready")"
}

# --- commands ----------------------------------------------------------

cmd_up() {
    if [ "$(id -u)" != 0 ]; then
        log "kernel-sim.sh up needs root: it loads kernel modules"
        exit 1
    fi
    mkdir -p "$STATE"
    log "Bringing up kernel simulators on $(uname -r):"
    local ok=0
    up_gpio     && ok=$((ok + 1)) || true
    up_i2c      && ok=$((ok + 1)) || true
    up_drm      && ok=$((ok + 1)) || true
    up_keyboard && ok=$((ok + 1)) || true
    log "$ok of 4 up. Tests skip whatever is missing rather than failing."
    # Deliberately exit 0 even at 0 of 4. A kernel without these modules is
    # a reason to skip the hardware tests, not to fail the build -- and the
    # count above is the honest report.
}

cmd_down() {
    [ "$(id -u)" = 0 ] || { log "needs root"; exit 1; }
    if [ -f "$STATE/keyboard.pid" ]; then
        kill "$(cat "$STATE/keyboard.pid")" 2>/dev/null || true
    fi
    local root=/sys/kernel/config/gpio-sim/$GPIO_SIM_NAME
    if [ -d "$root" ]; then
        echo 0 > "$root/live" 2>/dev/null || true
        rmdir "$root/bank0" 2>/dev/null || true
        rmdir "$root" 2>/dev/null || true
    fi
    local module
    for module in vkms i2c-stub gpio-sim; do
        modprobe -r "$module" 2>/dev/null || true
    done
    rm -rf "$STATE"
    log "torn down"
}

cmd_status() {
    if [ ! -d "$STATE" ]; then
        log "nothing up"
        return 0
    fi
    local file
    for file in "$STATE"/*; do
        [ -e "$file" ] || continue
        printf '%-16s %s\n' "$(basename "$file")" "$(cat "$file" 2>/dev/null)"
    done
}

case "${1:-status}" in
    up)     cmd_up ;;
    down)   cmd_down ;;
    status) cmd_status ;;
    *)      log "usage: $0 {up|down|status}"; exit 2 ;;
esac

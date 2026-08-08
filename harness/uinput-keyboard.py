"""Hold a virtual keyboard open so /proc/bus/input/devices reports one.

hmi.keyboard_connected() looks for a udev-named `kbd` handler, which is the
signal a real USB keyboard produces. A uinput device produces the same
signal through the same kernel path, so this makes that probe testable for
real rather than by monkeypatching the file it reads.

The device exists only while this process lives. It writes the event node's
path to the file named on the command line once the device is up, so the
caller can wait for it rather than sleeping and hoping.
"""
import signal
import sys
import time

from evdev import UInput, ecodes


def main(ready_path):
    # The arrow keys and Enter are what the unit's KeyboardButtons maps, so
    # advertise those: a device with no EV_KEY capability would not get a
    # kbd handler, which is the whole thing being tested.
    capabilities = {
        ecodes.EV_KEY: [
            ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
            ecodes.KEY_ENTER, ecodes.KEY_K, ecodes.KEY_Q,
        ]
    }
    with UInput(capabilities, name="otp-sim-keyboard", version=1) as device:
        with open(ready_path, "w") as handle:
            handle.write(getattr(device, "device", None) and device.device.path
                         or "uinput")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/run/otp-keyboard.ready")

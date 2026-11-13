"""Hold a virtual keyboard open so /proc/bus/input/devices reports one.

A uinput device gives hmi.keyboard_connected() the same udev-named `kbd`
handler a real USB keyboard does. The device exists only while this process
lives; once it is up, the event node's path is written to the file named on
the command line.
"""
import signal
import sys
import time

from evdev import UInput, ecodes


def main(ready_path):
    # The keys KeyboardButtons maps. A device with no EV_KEY capability
    # would not get a kbd handler.
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

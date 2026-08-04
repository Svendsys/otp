# Building the print unit

A Raspberry Pi, a small OLED, three buttons and a USB laser printer. About
£30 plus the printer.

## Parts

| Part | Notes |
|------|-------|
| Raspberry Pi Zero 2 W, 3, 4 or 5 | The image is arm64 and boots on any of them. A Zero 2 W is the cheapest dedicated unit; a Pi 4 is faster and has real USB-A ports. |
| microSD card, 8GB+ | Class 10 or better. |
| SSD1306 128×64 OLED, I²C | The common blue-or-white 0.96" module. SH1106 modules also work — see below. |
| 3 × momentary push buttons | Any tactile switch. |
| USB laser printer | Monochrome is the target. See [PRINTERS.md](PRINTERS.md). |
| USB OTG adapter | Zero 2 W only — its data port is micro-USB. |

The Zero 2 W has no USB-A socket, so the printer needs a micro-USB-to-USB-A
OTG adapter. The printer is mains-powered, so the Pi never has to supply it.

## Wiring

Pins avoid GPIO 0/1 (HAT EEPROM), 2/3 (I²C) and 14/15 (UART).

| Signal | GPIO | Header pin | To |
|--------|------|------------|-----|
| OLED VCC | — | 1 (3V3) | OLED VCC |
| OLED GND | — | 6 | OLED GND |
| OLED SDA | 2 | 3 | OLED SDA |
| OLED SCL | 3 | 5 | OLED SCL |
| UP | 5 | 29 | button → pin 30 (GND) |
| DOWN | 6 | 31 | button → pin 34 (GND) |
| OK | 13 | 33 | button → pin 39 (GND) |

Buttons connect their GPIO to ground; the internal pull-ups are enabled in
software, so no external resistors.

```
   3V3 (1) ──── OLED VCC          GPIO5  (29) ──[UP]──── GND (30)
   SDA (3) ──── OLED SDA          GPIO6  (31) ──[DOWN]── GND (34)
   SCL (5) ──── OLED SCL          GPIO13 (33) ──[OK]──── GND (39)
   GND (6) ──── OLED GND
```

**The OLED is 3.3V.** Most SSD1306 modules are 3.3–5V tolerant, but take
power from pin 1 (3V3), not pin 2 (5V), unless the module's datasheet says
otherwise.

## Controls

Three buttons, because a long press does the work of a fourth:

| Press | Does |
|-------|------|
| UP / DOWN | Move the selection, change a value |
| OK (tap) | Select, confirm, advance |
| OK (hold ~1s) | Back, or cancel a running job |

## Before you have any of it: the status sheet

You do not need the OLED or the buttons to make a start. Flash the image,
plug in a USB printer, and power up with nothing else attached. The unit
finds no panel on the I2C bus, so instead of sitting there mute it waits
for the printer and prints a single sheet telling you what it found:

- the wiring table below, so you can build the panel from the sheet alone
- an I2C scan, so you can tell "nothing wired up" from "wired up, but at
  0x3D"
- which of `luma.oled`, `gpiozero` and `lgpio` actually imported
- the printer it detected, the queue it created and the driver it matched
- whether swap is off, whether the root filesystem is a read-only overlay,
  and whether any network link is up — the three claims this device makes
  about itself, checked rather than asserted
- Pi model, serial, memory, temperature, kernel, entropy, disk

It reprints when the printer is unplugged and reconnected, not on a timer,
so a unit left plugged in overnight produces one sheet and not a ream. To
ask for one deliberately:

```sh
sudo systemctl stop otp-unit
sudo -u otp python3 -m otpunit --diagnostic
```

The sheet carries no key material and is safe to photograph or email when
you want help with a unit that will not come up.

**It will not print pads in this state.** Choosing a codeword and a page
count needs the panel and the buttons; there is no way to drive a pad pair
without them, and no attempt is made to fake one.

## Checking the hardware

With the unit powered and the image flashed:

```bash
# Is the OLED on the bus? Expect 3c (or 3d on some modules).
sudo i2cdetect -y 1

# Are the buttons wired right? Press each; the pin should read 0.
raspi-gpio get 5,6,13

# Is the unit running?
systemctl status otp-unit

# What is it doing?
journalctl -u otp-unit -f
```

If the OLED shows nothing but `i2cdetect` finds it at `3d` rather than `3c`,
the address differs — pass it in the service's `ExecStart`. If the display
is an SH1106 (common on 1.3" modules) the panel will look shifted; those
need `sh1106` instead of `ssd1306` in `otpunit/hw/display.py`.

## Smoke test

Run through this once before trusting the unit with anything:

- [ ] `i2cdetect` finds the display, and the splash screen appears at boot
- [ ] Each of the three buttons moves the menu; holding OK goes back
- [ ] With no printer plugged in, the panel says `PLUG IN A USB PRINTER`
- [ ] Plugging the printer in advances to the main menu within a few seconds
- [ ] `PRINT TEST PAGE` produces a sharp page with even shading bands
- [ ] A 12-page pad pair prints two stacks
- [ ] Both stacks are **identical** — hold two matching pages up to a window
- [ ] Each pad's page numbers run 1..N with no repeats and no gaps
- [ ] Cut one sheet and check the crop marks line up with the cut
- [ ] `PRINT TABULA RECTA` and `PRINT MANUAL` both produce output
- [ ] The confirm screen says `LIVE KEY MATERIAL` or `*** TRAINING ***` —
      never neither
- [ ] `swapon --show` prints nothing
- [ ] After a job, `ls /var/spool/cups` is empty and `findmnt /var/spool/cups`
      or `/run/cups` shows tmpfs
- [ ] **`findmnt /` reports `overlay`.** This is the single most load-bearing
      step in the design and the easiest to forget: the panel looks identical
      before and after you enable it, so nothing else will tell you. Until it
      does, everything a session writes stays on the card.
- [ ] Power-cycle, then confirm the job left no trace

## Developing without hardware

The whole UI runs in a terminal:

```bash
python3 -m otpunit --sim
```

`u`/`d` move, `k` is OK, `K` is a long press, `q` quits. The printer is
simulated, so jobs complete instantly. This is the fastest way to work on
screens, and it is what the test suite drives.

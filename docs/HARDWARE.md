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

## If you cannot get these parts

The OLED and the buttons are the *nicest* interface, not the only one.
The unit looks for a display and an input separately and takes the best
of each, so these all work:

| Display | Input | Result |
|---|---|---|
| SSD1306 OLED | 3 buttons | The intended panel |
| SSD1306 OLED | USB keyboard | Full menu |
| **Any HDMI monitor or TV** | **USB keyboard** | Full menu |
| Any HDMI monitor or TV | 3 buttons | Full menu |
| — | — | Prints unattended, see below |

**A monitor and a keyboard are the easy answer.** Almost every house has
both, they need no soldering, and the menu they give you is the same one
the OLED shows — the unit binds itself to `tty1`, so plug an HDMI screen
and a USB keyboard into a Pi and it just appears. Arrow keys move,
Enter selects, and **SHIFT+K** is back or cancel. There is no
hold-to-go-back on a keyboard: `KeyboardButtons` maps single keys, so
the long press that the three-button panel uses has its own key here.

It checks the DRM connector rather than trusting the presence of a
terminal, so a unit with nothing plugged into its HDMI socket does not
sit at a menu nobody can see — it goes and prints instead.

### And if you have none of that either

Then the unit still makes pads, and that is the point. Assume the shops
are shut and nothing is coming: flash the image, plug in a USB printer,
power up with nothing else attached, and leave it alone. Five minutes
later it starts printing a complete, usable set.

The panel was never load-bearing. All it ever did was choose a codeword
and a page count, and both have defaults that are fine. What replaces it
is built from things that cannot run out:

| Instead of | Use |
|---|---|
| A display | Paper. The unit prints what it would have shown. |
| Buttons | Time. It waits, and tells you on paper how long. |
| A cancel button | The plug. Unplugging the printer aborts everything. |
| A confirm button | Any wire. Bridging pin 33 to pin 34 means "now". |
| A settings menu | The SD card. `otp-unit.conf` in any computer. |

The sequence, once a printer appears:

1. **Status sheet**, at once — what it found, a countdown saying exactly
   what is about to print, how much paper it will take, and how to stop
   it.
2. **Five minutes**, so there is time to read that and pull the plug.
   Bridging header pin 33 to pin 34 with a wire, a paperclip or a
   screwdriver skips the wait.
3. **The manual** — 28 A5 pages, printed two to a sheet on A4, so 14
   sheets. It goes *before* the pads deliberately: a pad is useless to
   someone who does not know the rules, and if the paper runs out, what
   survives should be the instructions rather than half a pad.
4. **A tabula recta card** — the lookup table that lets you encrypt and
   decrypt by hand without doing any arithmetic.
5. **Copy A** of the pad — 100 A6 pages by default, four to an A4 sheet,
   so 25 sheets.
6. **A separator sheet**: take copy A out of the tray, copy B follows in
   90 seconds. With no buttons, the sheet *is* the prompt.
7. **Copy B** — byte-identical to A, which is what makes them a pair.
8. **A final sheet**: what you are holding, the four rules that matter,
   and how to use it.

That is about **68 sheets of A4** in total for the defaults, and the
status sheet tells you the number before any of it starts. Load more than
that if you can: running out mid-pair loses the pair, not just the paper.

It does this once per connection, not on a timer. To make another pair,
power-cycle with the printer attached. To change anything, edit
`otp-unit.conf` on the SD card's first partition — it is FAT, so any
computer can read it:

```ini
auto_print    = yes          # print a pair unattended
auto_delay    = 300          # seconds to wait first; 0 prints at once
auto_manual   = yes          # print the manual before the pads
pages         = 100          # A6 pad pages per copy
paper         = A4           # A4, LETTER or A6
auto_codeword =              # leave empty to have one rolled
```

The unit **reads** `auto_codeword` but never writes one. A codeword is not
key material, but it names a live pad, and the SD card is the part most
likely to be captured along with the unit.

## The status sheet

Whether or not you let it print pads, the first sheet tells you what the
unit found:

- the wiring table, so you can build the panel from the sheet alone
- an I2C scan, so you can tell "nothing wired up" from "wired up, but at
  0x3D"
- which of `luma.oled`, `gpiozero` and `lgpio` actually imported
- the printer it detected, the queue it created and the driver it matched
- whether swap is off, whether the root filesystem is a read-only overlay,
  whether any network link is up, and whether the key came from the
  hardware RNG — the claims this device makes
  about itself, checked rather than asserted
- Pi model, serial, memory, temperature, kernel, entropy, disk

It reprints when the printer is unplugged and reconnected, not on a timer,
so a unit left plugged in overnight runs the sequence once, not repeatedly. To
ask for one deliberately:

```sh
sudo systemctl stop otp-unit
sudo python3 -m otpunit --diagnostic
```

Note this runs the **whole** unattended sequence, pads included — it is
not a status-sheet-only switch. To get the sheet without the pads, set
`auto_print = no` first. (The service runs as root; there is no `otp`
user.)

The sheet carries no key material and is safe to photograph or email when
you want help with a unit that will not come up. The sheets that come out
*with a pad* are a different matter, and say so at the top: those are key
material.

To get the status sheet without the pads that normally follow it, set
`auto_print = no` in `otp-unit.conf`. The unit then reports and stops.

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

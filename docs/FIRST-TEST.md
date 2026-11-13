# First test on a real Pi

Everything before this document was verified in emulation and CI. This is
the walkthrough for the first run on hardware: what to prepare, what you
should see at each step, how to check the security properties, and what to
do when a step does not match.

Work through it in order. Each stage adds one thing, so a failure points at
the thing just added.

## What you need

| Item | Notes |
|---|---|
| Raspberry Pi 3, 4 or Zero 2 W | A Pi 5 should work but is the least verified; see [Not yet verified](#not-yet-verified-on-hardware). |
| microSD card, 8 GB or more | |
| USB laser printer | One listed under *Known good* in [PRINTERS.md](PRINTERS.md) if you have the choice. |
| HDMI monitor and USB keyboard | Needed for this test even if you fit the OLED: they are the only way to a shell. |
| OLED and three buttons | Optional. Wire them per [HARDWARE.md](HARDWARE.md#wiring) after stage 2. |
| A4 or Letter paper | About 70 sheets for the full unattended run in stage 6; 10 is enough before that. |
| Another computer | To flash the card and edit files on its boot partition. |

On a Zero 2 W the printer goes on the micro-USB port marked **USB**, through
an OTG adapter, not the one marked **PWR**.

## 1. Get the image onto the card

Either download it or build it.

- **Download:** on GitHub, open *Actions → Build image*, pick the latest
  green run, and download the `otp-print-unit-image` artifact. Unzip it to
  get the `.img.xz`.
- **Build:** `./image/build.sh`, as described in [IMAGE.md](IMAGE.md).

Flash it with Raspberry Pi Imager (*Use custom*), or:

```bash
xzcat otp-*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
```

If Imager offers to apply OS customisation settings, answer **No**. The
login is set in the next step instead.

## 2. Prepare the boot partition

Re-insert the card. Its first partition (`bootfs`, FAT) mounts on any
computer. Make three changes there before the first boot.

**A login, so you can get a shell.** The image ships with a random password
nobody knows. Create `userconf.txt` containing one line, `otp:` followed by
a password hash:

```bash
echo "otp:$(openssl passwd -6)" > /path/to/bootfs/userconf.txt
```

Use a password you use nowhere else: the hash stays on the card (see
[IMAGE.md](IMAGE.md#your-login-and-what-keeping-it-costs)). The user name
must be `otp`.

**Safe settings for a first test.** Edit `otp-unit.conf` and set:

```ini
auto_print = no      # stage 3 prints a status sheet and stops
training   = yes     # pads are watermarked TRAINING, not secret
pages      = 10      # a 10-page pad is three sheets per copy
paper      = A4      # or LETTER
```

**Optionally, a serial console.** If you have a USB-serial adapter on the
GPIO UART, append ` console=serial0,115200` to the single line in
`cmdline.txt`. The image removes it by default. Keep the file as one line.

## 3. First boot, printer only

Connect the monitor, keyboard and printer. Leave the OLED off for now.
Power up.

What you should see:

1. A mostly blank screen for up to a minute. The kernel is told to be
   quiet because this screen is the front panel.
2. A box reading **PRESS ANY BUTTON**. Do not press anything this time.
   After 20 seconds the unit decides nobody is there and goes unattended.
3. Within about a minute, one **status sheet** from the printer. Because
   `auto_print = no`, nothing else prints.

Read the status sheet's SECURITY POSTURE section. It should say:

| Row | Expected |
|---|---|
| Live pads | `allowed` |
| Key source | `hardware TRNG + CSPRNG (correct)` |
| Swap | `none (correct)` |
| Root filesystem | `read-only overlay` |
| Network links | `no links up (correct)` |
| Kernel CSPRNG | `CRNG seeded (correct)` |

and under PRINTER, a device URI, a queue named `OTP` and a driver. Keep
this sheet; it is the fastest thing to compare against later.

If no sheet appears, go to [When something does not match](#when-something-does-not-match).

## 4. Get a shell and check the machine

Press **Alt+F2** for a login prompt and log in as `otp`. **Alt+F1** returns
to the panel. Run each of these and compare:

| Command | Expected |
|---|---|
| `findmnt -n -o FSTYPE /` | `overlay` |
| `swapon --show` | no output |
| `cat /proc/cmdline` | contains `boot=overlay` and `quiet`; no `console=serial0` unless you added it |
| `systemctl is-active otp-unit cups` | `active` twice |
| `systemctl show otp-unit -p NRestarts --value` | `0` |
| `findmnt -n -o FSTYPE -T /run/cups/spool` | `tmpfs` |
| `findmnt -n -o FSTYPE /etc/cups` | `tmpfs` |
| `cd /opt/otp-unit && sudo python3 otp_generator.py --entropy` | first line starts `hardware TRNG (/dev/hwrng)` |
| `ip -br addr` | only `lo` has an address |
| `sudo journalctl -u otp-unit -b --no-pager` | see below |

In the journal, look for these lines:

- `interface -- display: HDMI console, input: GPIO buttons and USB keyboard`
  — what it found. The GPIO pins open whether or not buttons are wired.
- `no OLED (...)` — expected until the OLED is fitted.
- `no GPIO buttons (...)` — **not** expected. Opening the buttons should
  succeed even with nothing wired. If this line appears, copy its text;
  see the table below.
- `live pads refused -- ...` — must not appear.

To keep the journal past power-off, copy it to the boot partition, the one
place that persists, and read it on another computer:

```bash
sudo sh -c 'journalctl -b --no-pager > /boot/firmware/journal.txt'
```

It holds no key material or codeword, but it does name the printer and its
serial. Delete the file when you are done.

## 5. Drive the menu

Power-cycle. This time press a key while **PRESS ANY BUTTON** is showing.
The arrow keys move, **Enter** selects, **Shift+K** goes back or cancels.

Do these in order:

1. **PRINT TEST PAGE.** One sheet with sharp letters and even grey bands.
2. **PRINT PAD PAIR → ROLL RANDOM → accept.** The confirm screen must show
   `*** TRAINING ***`. Note how long **GENERATING** takes for 10 pages; that
   is the real speed of the Pi's hardware RNG and nothing in CI could
   measure it. Lift copy A off the tray when prompted, then print copy B.
3. Check the two stacks: both watermarked TRAINING, page numbers 1 to 10
   in each, and matching pages **identical** when held together against a
   window.
4. Cut one sheet on the crop marks and confirm the four quarters are the
   same size.
5. **SETTINGS → PAGES → 20 → SAVE SETTINGS**, then power-cycle and confirm
   the setting survived. Then, from the shell,
   `sudo touch /should-vanish`, power-cycle, and confirm
   `ls /should-vanish` fails. That is the overlay doing its job.

**If you are fitting the OLED and buttons, do it now**, with the power off.
After boot the panel should show the same PRESS ANY BUTTON prompt on the
OLED; press a button within 20 seconds. Then check:

| Command | Expected |
|---|---|
| `sudo i2cdetect -y 1` | `3c` or `3d` in the grid |
| `pinctrl get 5,6,13` | each line `hi`; `lo` while its button is held |

A tap of OK selects; holding OK about a second and **releasing** goes back.

## 6. A live pair, then the unattended run

Only once stage 5 is clean:

1. On another computer set `training = no` in `otp-unit.conf`, or use
   SETTINGS → TRAINING → LIVE MATERIAL → SAVE SETTINGS.
2. Print a pad pair from the menu. The confirm screen must show
   `LIVE KEY MATERIAL`. Treat what comes out as real key material, or
   destroy it.
3. Afterwards, from the shell: `sudo ls -A /run/cups/spool /run/cups/tmp`
   should list no job files.
4. For the unattended path, set `auto_print = yes` and `pages = 100`,
   remove the keyboard, load 70 sheets, and power up. Expect the status
   sheet, a five-minute wait, the manual, a tabula recta card, copy A, a
   separator sheet, a 90-second pause, copy B and a final sheet. Pulling
   the printer's USB cable at any point aborts.

Power the unit off between pairs. Pulling the plug is safe: the card is
never written. The menu's SHUT DOWN is the tidy way.

## When something does not match

| What you see | Likely cause | What to do |
|---|---|---|
| Rainbow square or nothing on HDMI, ever | Card not flashed properly, or unsupported board | Re-flash; try a Pi 3 or 4. |
| Text scrolls and stops at a `(initramfs)` prompt or a panic | The overlay script could not mount the card | Photograph the last screen. Remove `boot=overlay` from `cmdline.txt` on another computer to boot without it, and report the photo. |
| Boots, but no PRESS ANY BUTTON box and no status sheet | The service is not running | Alt+F2, then `systemctl status otp-unit` and the journal. |
| Status sheet says `Live pads: REFUSED: ROOT DISK IS WRITABLE` | The firmware did not load the initramfs | `ls /boot/firmware/initramfs*` and `grep auto_initramfs /boot/firmware/config.txt`; both must exist. Training pads still print meanwhile. |
| `Live pads: REFUSED: SWAP IS ON` | Something re-enabled swap | `swapon --show` names it. Report it; this is a provisioning gap. |
| No status sheet, panel says `PLUG IN A USB PRINTER` | CUPS cannot see the printer | `sudo lpinfo -v` should list a `usb://` line. If not: cable, port (Zero 2 W: the USB port, not PWR), or the printer is off. |
| Panel says `NO DRIVER FOR ...` | No matching driver in the image | [PRINTERS.md](PRINTERS.md); `sudo lpinfo -m \| grep -i <model>`. |
| Job accepted, nothing prints, panel says STILL PRINTING or PRINTER STOPPED | Printer fault or wrong driver | `lpstat -t`; check paper and cover; OK rechecks and re-enables the queue. |
| Pages print scaled, or crop marks are off the cut | Driver ignoring `print-scaling=none` | Note the printer model and driver from the status sheet. |
| Journal says `no GPIO buttons (...)` | The GPIO library could not open the pins | Copy the text in the brackets. `ls /run/otp-unit` and `python3 -c "import lgpio"` narrow it down. |
| OLED dark, `i2cdetect` shows nothing | Wiring, or I2C not enabled | Check VCC is on pin 1 (3V3), SDA pin 3, SCL pin 5; `ls /dev/i2c-1`. |
| OLED lit but picture shifted | It is an SH1106, not an SSD1306 | See [HARDWARE.md](HARDWARE.md#checking-the-hardware). |
| Arrow keys do nothing on the keyboard | Keyboard not seen at boot | It must be plugged in before power-up; `u`, `d`, `k` also work. |
| Unit prints by itself five minutes after boot | Nobody pressed a key in the 20-second window and `auto_print = yes` | Expected. Unplug the printer to stop it. |

When reporting a problem, the three most useful things are a photo of the
status sheet, `journal.txt` from stage 4, and the Pi model.

## Not yet verified on hardware

These could only be reasoned about or emulated. The first test is what
settles them, so note the outcome of each:

- **The firmware loads the initramfs**, so the root is the overlay.
  Emulation supplies its own kernel and command line. Stage 4, first row.
- **`cmdline.txt` edits** (`quiet`, no serial console). Stage 4, third row.
- **GPIO buttons open under the service's sandbox.** Stage 4 journal.
- **The hardware RNG's real throughput.** Stage 5, step 2.
- **Your printer's driver match and fault reporting.** Stages 3 and 5.
- **Print geometry:** crop marks, unscaled output. Stage 5, step 4.
- **Pi 5:** a different kernel, initramfs and GPIO chip from the board the
  emulator models. Everything above applies; expect surprises here first.
- **Memory on a Zero 2 W** with a 1000-page pad. Start with 100.

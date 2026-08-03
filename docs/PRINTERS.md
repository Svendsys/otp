# Printers

The unit targets simple monochrome USB laser printers. Most work; some
cannot be made to work at all on Linux. **Run `PRINT TEST PAGE` first** — it
takes thirty seconds and tells you what a thousand-page job would tell you
much later.

## How a printer gets set up

When a USB printer appears, the unit tries two things in order:

1. **Driverless.** `lpadmin -m everywhere`, which covers IPP Everywhere and
   anything reachable through `ipp-usb` — broadly, printers made since about
   2017. Nothing to install.
2. **A Foomatic PPD**, matched against the printer's USB device ID. This is
   the path for older host-based lasers, and needs every model token to
   match — an `M12w` will not be handed an `M12a` PPD.

If neither works the panel says `NO DRIVER FOR <printer>` and no job is
accepted. That is deliberate: a wrong PPD does not fail loudly, it produces
a queue that accepts jobs and prints garbage.

## Known good

| Printer | Driver | Notes |
|---------|--------|-------|
| HP LaserJet Pro M12a / M12w | `foo2zjs` (`-z2`) | Host-based. Needs the Foomatic PPD set, which is in the image. No firmware download required. |
| HP LaserJet 1020 / 1018 / 1005 | `foo2zjs` | **Needs a firmware upload at power-on** — see below. |
| HP LaserJet P1005–P1008, P1505 | `foo2xqx` | Also needs firmware. |
| Brother HL-2030 / 2140 / L2300 series | `brlaser` | Well supported, no firmware. |
| Samsung ML / Xerox Phaser mono | `splix` | |
| Anything PostScript or PCL5e/6 | driverless or generic | The easy case. |

## The firmware exception

A handful of older HP LaserJets keep no firmware of their own — the host
uploads it every time the printer powers on. The affected models are the
LaserJet 1000, 1005, 1018, 1020 and P1005–P1008, P1505.

Those blobs are not redistributable, so they are not in the image. Since the
unit is meant to run offline, fetch them at build time on your build machine
and they will be baked in:

```bash
sudo apt-get install -y printer-driver-foo2zjs-common
sudo getweb 1020        # or 1018, 1005, p1005, ...
```

Then rebuild the image. **The HP LaserJet Pro M12w does not need this** — it
is in the foo2zjs family but not the firmware-upload group.

## Known bad

Some cheap lasers are pure GDI/host-based with no free driver and no vendor
Linux support. There is no fix short of a different printer. Symptoms: the
printer appears in `lpinfo -v` but nothing renders, or output is blank pages.

Printers requiring HP's proprietary HPLIP binary plugin are also awkward:
the plugin is downloaded from HP at install time, which an offline unit
cannot do. If your printer needs it, run `hp-plugin -i` on a networked
machine during image building rather than on the unit.

## Paper

Pad pages are A6. The default setting tiles four of them onto A4 (or Letter)
with crop marks, imposed **cut-and-stack**, and the printed stack is cut
down on a guillotine.

Cut-and-stack means each of the four piles the cuts leave behind is already
in page order. Assemble the pad by dropping them on top of one another:
top-left, then top-right, then bottom-left, then bottom-right. Do not try to
interleave them.

The tiling is done in the PDF rather than by CUPS, because a guillotine cuts
the whole stack at once and every sheet therefore needs identical geometry.
For the same reason, **do not enable scaling or N-up in any print dialog or
queue default** — it will move the cut line away from the crop marks.

Set `PAPER` to `A6 SHEETS` if you really do have A6 paper loaded.

## Diagnosing

```bash
lpinfo -v                    # what CUPS can see
lpstat -p -d                 # queues and their state
lpstat -o                    # jobs waiting
journalctl -u otp-unit -n 50 # what the unit thought happened
journalctl -u cups -n 50     # what CUPS thought happened

# Does the printer speak at all? This bypasses the unit entirely.
echo "hello" | lp -d OTP
```

If `lpinfo -v` does not list the printer, the problem is USB, not printing:
check the OTG adapter on a Zero 2 W, and try a different cable.

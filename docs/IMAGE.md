# Building and flashing the image

Two routes. Use the second while developing — it is seconds rather than half
an hour, and it runs the same provisioning script the image does.

## Route 1: a flashable image

```bash
./image/build.sh
```

Clones [pi-gen](https://github.com/RPi-Distro/pi-gen), renders the manual,
generates the config and custom stage from this repository, and runs the
Docker build. Output lands in `image/deploy/` as an `.img.xz`.

Requirements:

- Docker
- `pandoc` and `weasyprint`, to render the manual. The PDFs are not checked
  into the repository, so a fresh clone always renders them:
  ```bash
  sudo apt-get install -y pandoc python3-weasyprint
  ```
- On **x86** hosts, arm64 emulation. Both halves are needed — registering
  binfmt is not enough on its own, because pi-gen's `build-docker.sh` looks
  for the interpreter binary on the *host* `PATH`:
  ```bash
  sudo apt-get install -y qemu-user-static qemu-user-binfmt
  docker run --privileged --rm tonistiigi/binfmt --install arm64
  ```
- On **Apple Silicon**, nothing extra — the build is native.
- Roughly 10GB of free disk and half an hour.

The build uses pi-gen's **`arm64` branch**, not `master`. pi-gen hardcodes
`export ARCH=armhf` after sourcing its config, so setting `ARCH` in the
config has no effect and `master` only ever produces 32-bit images.

Flash it:

```bash
xzcat image/deploy/*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress
```

or point Raspberry Pi Imager at the file.

## Route 2: provision a Pi you already have

```bash
sudo ./device/install.sh
sudo reboot
```

Works on a stock Raspberry Pi OS Lite (arm64) install. This is the fast
iteration loop — and because the image build runs this same script, what you
test here is what the image does.

## After first boot

The unit starts automatically, needs no network and no keyboard. Plug in a
USB printer and use the three buttons.

One step is left manual, because it makes the filesystem read-only and you
want to be sure everything works first:

```bash
sudo raspi-config nonint enable_overlayfs
sudo reboot
```

This is what turns the Pi into an appliance rather than a computer that runs
one program. Afterwards the root filesystem is read-only with a RAM overlay:
nothing a session touches survives a power-cycle, and pulling the plug
cannot corrupt the card. Settings still persist — they live on the boot
partition, which is outside the overlay.

**Until you do this, the image boots with a writable root.** Every test
print before that point leaves whatever CUPS and systemd wrote on the SD
card permanently. It is a manual step because you want to confirm the unit
works before making the filesystem read-only — but it is not optional if you
want the reset-on-power-cycle property.

Note that `enable_overlayfs` leaves `/boot` writable; making it read-only is
a separate `enable_bootro`, and raspi-config refuses to run it once the
overlay is active. Do that one first if you want it.

To change the software afterwards, disable the overlay
(`sudo raspi-config nonint disable_overlayfs`), make the change, re-enable
it.

## What the image does to the system

All of it is in [`device/install.sh`](../device/install.sh), which is meant
to be read:

- Installs the packages in `device/packages.txt`, all from Debian Trixie, so
  first boot needs no network.
- Deploys the unit to `/opt/otp-unit` and enables `otp-unit.service`.
- Enables I²C; disables Wi-Fi and Bluetooth.
- **Purges swap.** Key material lives in a buffer that gets zeroed after
  printing; with swap enabled the kernel could page that buffer to the SD
  card first, and zeroing RAM would not touch the copy on disk.
- Makes the journal volatile and disables core dumps — a crash mid-job would
  otherwise write the whole pad to `/var/lib/systemd/coredump`.
- Moves everything CUPS writes to tmpfs: spool, temp **and cache**, plus
  blanking `PageLog` and `AccessLog`. The cache matters because `job.cache`
  records every job's name, and page logging writes a line per printed page.
- Sets `PreserveJobHistory No` and `PreserveJobFiles No` **in
  `cupsd.conf` itself**. CUPS has no `Include` directive and no
  `cupsd.conf.d`, so a drop-in file would be silently ignored — and the
  defaults are the opposite of what is wanted here: history is kept forever
  and the spooled document, the entire pad, is kept for 24 hours.
- Mounts a tmpfs over `/etc/cups` at boot from a baked-in template, so the
  print queue is rebuilt from whatever is plugged in. The template excludes
  `printers.conf`, which holds the last printer's make, model and serial.

## Why pi-gen and not rpi-image-gen

`rpi-image-gen` is Raspberry Pi's newer builder and is aimed squarely at
appliances like this one, but it officially supports only arm64 Debian
hosts. pi-gen's `build-docker.sh` runs anywhere Docker does, which matters
more here than the newer tooling does.

## Notes for anyone editing the build

- `STAGE_LIST` is set explicitly. Under pi-gen's default `stage*` glob,
  `stage-otpunit` sorts *before* `stage0` (`-` is 0x2D, `0` is 0x30) and
  would run first against an empty rootfs.
- `stage2/SKIP_IMAGES` is touched so the build does not also export a plain
  Raspberry Pi OS Lite image.
- The stage's `00-packages` is generated from `device/packages.txt` — do not
  edit it directly, it is overwritten on every build.
- `install.sh --skip-apt` runs in the chroot, because the packages are
  already installed by pi-gen and the chroot has no network.
- Resume a failed build with `CONTINUE=1` in `image/pi-gen`.

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
- On **x86** hosts, `binfmt_misc` registered for arm64 so the chroot can run
  ARM binaries:
  ```bash
  docker run --privileged --rm tonistiigi/binfmt --install arm64
  ```
- On **Apple Silicon**, nothing extra — the build is native.
- Roughly 10GB of free disk and half an hour.

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
partition, which the unit remounts briefly when you choose SAVE SETTINGS.

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
- Makes the journal volatile.
- Moves the CUPS spool to tmpfs and sets `PreserveJobFiles No`.
- Mounts a tmpfs over `/etc/cups` at boot from a baked-in template, so the
  print queue is rebuilt from whatever is plugged in and no record of past
  printers survives.

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

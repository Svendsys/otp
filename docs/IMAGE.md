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
  sudo apt-get install -y pandoc weasyprint
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

The unit starts automatically and needs no network. Plug in a USB
printer. If an OLED and buttons are fitted it uses those; an HDMI
monitor and a USB keyboard work just as well; with neither it prints
unattended.

**No panel wired up yet?** Boot it anyway with a printer attached. It
prints a status sheet at once — the wiring table, an I2C scan, which
drivers loaded, the printer it matched, and whether swap, the overlay,
the network and the hardware RNG are where they should be — and that
sheet is the fastest way to tell whether the image is healthy before you
have any hardware to look at it with.

Then, **after five minutes, it prints a pad pair**: the manual, a tabula
recta card, and two copies of a 100-page pad, about 68 sheets of A4 in
all. That is deliberate — see
[HARDWARE.md](HARDWARE.md#if-you-cannot-get-these-parts) — but if you only
want the status sheet, unplug the printer when it lands, or set
`auto_print = no` in `otp-unit.conf` on the boot partition first.

## The read-only overlay

**It is already on.** `device/install.sh` enables it, so the image ships with
it and so does any Pi you provision by hand. Nothing is left for you to run.

The root filesystem is a RAM overlay over a card mounted read-only: nothing a
session touches survives a power-cycle, and pulling the plug cannot corrupt
the card, because nothing writes to it. Settings still persist — they live on
the boot partition, which is outside the overlay.

This used to be a manual step, printed as advice at the end of `install.sh`.
It was a manual step nowhere else in this project: the image did not do it,
the pi-gen stage did not do it, and no tier of the harness had ever booted a
machine that had it. That is [issue
#9](https://github.com/Svendsys/otp/issues/9). Tier 3 now boots the built
image twice and asserts the overlay from inside it — see
[harness/README.md](../harness/README.md).

**Not `raspi-config nonint enable_overlayfs`, and do not run it.** On
bookworm and later that command installs Debian's `overlayroot` package and
puts `overlayroot=tmpfs` on the kernel command line. overlayroot's initramfs
script moves the root aside with `mount --move`, and the `mount` an
initramfs-tools initrd actually contains is klibc's, which has no such
option:

```
$ /usr/lib/klibc/bin/mount --move /a /b
mount: invalid option --
```

That is the exact message the tier-2 guest printed immediately before
`Kernel panic - not syncing: Attempted to kill init!`. It survives only where
busybox happens to have been packed into the initramfs, because busybox's
`mount` does accept `--move`.

What `install.sh` sets up instead is initramfs-tools' own `boot=` hook, which
is what raspi-config itself used before the switch: `boot=overlay` on the
kernel command line makes the initrd source `/etc/initramfs-tools/scripts/overlay`,
which mounts the card read-only and lays a tmpfs overlay over it. Every mount
it runs is one klibc's `mount` accepts.

To change the software afterwards, take `boot=overlay` out of
`/boot/firmware/cmdline.txt` on another computer (or from a shell on the
unit, since the boot partition stays writable), reboot, make the change, and
rerun `install.sh` to put it back.

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
- Sets `MaxJobs 4` and `ErrorPolicy abort-job`. Both numbers have a
  history. `MaxJobs 1` looks right — one job's key material in the spool
  at a time — but cupsd does not *queue* past `MaxJobs`, it **refuses**:
  `lp: Too many active jobs.` Against a real cupsd that meant the status
  sheet and the manual printed and every job after them was rejected, so
  the unit promised a pad pair and then produced nothing. The unit waits
  for the queue to drain before each submit, so only one job is live in
  practice; the headroom is what stops a timing race costing the whole
  run. `abort-job` then means a failed job is discarded as promptly as a
  successful one — so an empty queue is *not* proof anything printed, and
  the unit asks `lpstat -p` for the printer's own state before it tells
  anyone they are holding a pair.
- Mounts a tmpfs over `/etc/cups` at boot from a baked-in template, so the
  print queue is rebuilt from whatever is plugged in. The template excludes
  `printers.conf`, which holds the last printer's make, model and serial.
- **Enables the read-only root overlay**, and refuses to finish if it cannot
  prove it did: the initramfs it built has to contain `scripts/overlay` and
  the command line has to carry `boot=overlay`. Every way this can go wrong
  is otherwise silent — a unit that boots read-write looks exactly like one
  that does not.
- Drops the `resize` token from `cmdline.txt` and disables `rpi-resize`. An
  online resize cannot grow a filesystem mounted read-only as the overlay's
  lower layer, and there is nothing to grow: what the unit writes is RAM.

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

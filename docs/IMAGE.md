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

The build uses one pinned commit of pi-gen's **`arm64` branch**, named in
`image/build.sh` as `PI_GEN_REF`, so the same checkout of this repository
always builds from the same pi-gen. Move it deliberately, and let the image
workflow boot the result. It is the `arm64` branch because pi-gen hardcodes
`export ARCH=armhf` after sourcing its config, so `master` only ever
produces 32-bit images.

Flash it:

```bash
xzcat image/deploy/*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress
```

or point Raspberry Pi Imager at the file. CI also builds the image: the
`otp-print-unit-image` artifact of any green *Build image* run is the same
file.

To ship an image-wide login, set `OTP_USER_PASS_HASH` to a crypt(3) string
(`openssl passwd -6`) before building. It is applied to the `otp` account
and is readable by anyone who holds the image. Without it the account has a
random password nobody knows, and each unit's owner sets their own (below).

**For the first run on real hardware, follow
[FIRST-TEST.md](FIRST-TEST.md).**

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

**Two other things live there, and they are the only exceptions to the
sentence above.** Both are in `/boot/firmware/otp-identity`.

`machine-id`. systemd treats a missing machine-id as a first boot and
re-runs its first-boot setup every time, so this one identifier is kept and
put back by the initramfs before systemd starts.

`credential`, **but only if you set a password yourself.** See the next
section: it is the one thing here that is not merely an identifier, and it is
worth reading before you decide it is acceptable.

## Your login, and what keeping it costs

**Set a password the documented way** — write a `userconf.txt` holding
`username:hash` to the boot partition, with the hash from `openssl passwd -6`,
or let Raspberry Pi Imager do it. `userconfig.service` applies it on the next
boot and deletes the file. The account is `otp`; the login prompt is on tty2
(**Alt+F2**), because tty1 is the front panel.

**Put `otp` in that file.** Naming anyone else works for one boot and then
undoes itself: `userconf-pi` *renames* the UID-1000 account to whatever your
seed says — `usermod -l`, a home directory move, and edits to `/etc/group`,
`/etc/subuid`, `/etc/subgid` and the sudoers drop-in — and every one of those
lives in `/etc` or `/home`, which are inside the RAM overlay. The rename is
gone at the next power-cycle. **Your password is not**: the unit notices that
the kept credential names an account it no longer has, applies it to whoever
holds UID 1000, and rewrites the store to match, saying so in the journal.
So a seed naming `pi` costs you the name and never the login — from the
second boot on, **log in as `otp`**.

**The password is kept across power cycles**, in
`/boot/firmware/otp-identity/credential`, and restored early in every boot
before any login prompt exists. It has to live there: `/etc/shadow` is
inside the RAM overlay. The precedence, if you
ever write a new `userconf.txt` onto a unit that already has a kept password:

| what is there | what wins this boot | what the next boot uses |
|---|---|---|
| a kept credential only | the kept one, on the UID-1000 account | the kept one |
| a kept credential **and** a fresh `userconf.txt` | the fresh one | the fresh one |
| a fresh `userconf.txt` only | the fresh one | the fresh one |
| a malformed `userconf.txt` | the kept one | the kept one |
| an empty or half-written `credential` | nothing — the unit fails and says so | nothing |

so writing a new `userconf.txt` is always how you change the password, and a
seed the wizard rejects costs you nothing — it is renamed `failed_userconf.txt`
and the password you already had still works.

**Row 1 says *on the UID-1000 account* rather than *on the account the file
names*, and that is the whole of the caveat above.** The kept credential is
stored as `username:hash`, but the username in it is a label: the password is
put on whichever account holds UID 1000 this boot, which is the only account
a `userconf.txt` could ever have set a password on in the first place. If the
two disagree — because your seed renamed the account and the rename did not
survive — the unit applies the hash to the live account, rewrites the store,
and notes all of it. It refuses only if there is no UID-1000 account at all.

**The last row is a failure, not a fallback.** The store is written beside
itself and renamed into place, and vfat has no atomic rename, so a power cut
mid-write or a full boot partition can leave a zero-length `credential` or a
leftover `credential.new`. That is *not* the same as a card nobody has
seeded, and the unit does not treat it as one: `otp-unit-identity.service`
fails, the reason is on the screen during the boot, and a copy of it is left
in `/boot/firmware/otp-identity/credential-not-restored.txt` where you can
read it with the card in another machine. Write a fresh `userconf.txt`.

**THE COST, which you should decide about rather than discover.** That file is
a password hash on a vfat partition mounted with `defaults` — `0755
root:root`, readable by **every account on the unit** and by anyone who can
put the card in a reader. A hash is not a password, but it can be attacked
offline for as long as somebody likes: `sha512crypt` at the rounds `openssl
passwd -6` uses verifies in about two milliseconds on an ordinary core, before
anyone reaches for a GPU. **Do not use a password you use anywhere else.**

Two things make that bound smaller than it sounds. The bytes are the same
bytes your own `userconf.txt` put on that same partition — persisting them
adds no exposure that seeding them did not — and nothing is written there at
all unless you seeded a credential yourself, so a unit whose owner never set a
password has no hash on its card.

**If the login does not come back, the unit says why in two places.** The
notes go to the screen while the boot is happening — `otp-unit-identity.service`
writes them to the console before any login prompt exists — and every refusal
is also left in `/boot/firmware/otp-identity/credential-not-restored.txt`,
which is there for the case where you cannot log in to read a journal that
lives in RAM anyway. Take the card out and read that file; it names the
reason and never contains a hash. It is removed by the first boot that
restores the login successfully.

**Deleting `userconf.txt` does not take the hash off the card.** This does:

```
sudo rm -f /boot/firmware/otp-identity/credential
```

after which the account goes back to the build-time password nobody has, and
the tty2 prompt is no use to you. Set a new one with a fresh `userconf.txt`
instead.

**A password you set with `passwd` at the console is NOT kept.** It lives in
`/etc/shadow`, which is inside the overlay, so it lasts until the power goes
off — only the wizard's path is persisted, because that is the path where you
have already chosen to put the hash on the card. **Tightening the partition**
so that only root could read it (`fmask=0077`) is possible and is discussed in
the comments in `device/install.sh`; it is not done, because it means
rewriting `/etc/fstab`, and an `/etc/fstab` that is wrong costs you the boot
partition and everything on it.

**The SSH host keys are not kept.** The image is built with SSH disabled
and the unit does not run `sshd` after its first boot, so there is no
fingerprint worth a private key outside the overlay.

The boot partition is vfat mounted with `defaults`, so every file on it is
`0755 root:root` — readable by **every account on the unit**, not only by
someone who takes the card out. A machine-id is an identifier and the
settings are the operator's own; the one thing there that is neither is the
kept login hash, which is why it has a section of its own above. No pad byte
is ever written to that partition.

CI boots the built image twice under emulation and asserts the overlay
from inside it; see [harness/README.md](../harness/README.md). The unit
also checks for itself: **it refuses to make a live pad unless the root is
this overlay and swap is off**, and says so on the panel and the status
sheet. Training pads still print. A machine that deliberately has no
overlay can set `allow_writable_root = yes` in `otp-unit.conf`.

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

The result is a kernel panic at boot unless busybox happens to be in the
initramfs.

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

- Installs the packages in `device/packages.txt` without their Recommends,
  so first boot needs no network and nothing unnamed comes along.
- Deploys the unit to `/opt/otp-unit` and enables `otp-unit.service`.
- Enables I²C; disables Wi-Fi and Bluetooth.
- **Turns swap off, whatever provides it.** `swap.target` is masked and the
  service runs `swapoff -a` before every start. With swap on, the kernel
  could page key material out, and zeroing RAM would not touch that copy.
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
- Sets `MaxJobs 4` and `ErrorPolicy abort-job`. cupsd refuses jobs past
  `MaxJobs` rather than queueing them, so the unit waits for the queue to
  drain before each submit and the headroom covers a timing race.
  `abort-job` means a failed job leaves the queue as promptly as a printed
  one, so an empty queue is not proof anything printed: the unit asks the
  printer's own state before telling anyone they hold a pair.
- Mounts a tmpfs over `/etc/cups` at boot from a baked-in template, so the
  print queue is rebuilt from whatever is plugged in. The template excludes
  `printers.conf`, which holds the last printer's make, model and serial.
- **Enables the read-only root overlay**, and refuses to finish if it cannot
  prove it did: the initramfs it built has to contain `scripts/overlay` and
  the command line has to carry `boot=overlay`. Every way this can go wrong
  is otherwise silent — a unit that boots read-write looks exactly like one
  that does not.
- Adds `quiet` to the kernel command line and removes the serial console,
  so the kernel does not write over the front panel on tty1 or expose a
  login on the GPIO UART. Put `console=serial0,115200` back in
  `cmdline.txt` if you want one for debugging.
- Writes its `config.txt` settings under `[all]`, so they apply to every
  board the card is put in.
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
- The stage's `00-packages-nr` is generated from `device/packages.txt` — do not
  edit it directly, it is overwritten on every build.
- `install.sh --skip-apt` runs in the chroot, because the packages are
  already installed by pi-gen and the chroot has no network.
- Resume a failed build with `CONTINUE=1` in `image/pi-gen`.

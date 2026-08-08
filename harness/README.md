# Running the unit without a Raspberry Pi

Six rounds of adversarial code review found real bugs and still left a
device that could not print a pad, and could not start at all.

Reading finds defects in code; only running finds defects in assumptions.
What each tier found, once it existed:

| Found by | Defect |
|---|---|
| Tier 1, a real `cupsd` | `MaxJobs 1` makes CUPS **refuse** a second job rather than queue it. The unit got two sheets out and was rejected for the rest — it could never print a pad. Four seconds to find. |
| Tier 1, a real `cupsd` | A drained queue was read as proof of printing. Under `ErrorPolicy abort-job` a failed job empties the queue as fast as a successful one, so an empty tray produced "YOUR PAD PAIR IS PRINTED". |
| **Tier 2, a booted VM** | **`216/GROUP`.** `otp-unit.service` names supplementary groups `install.sh` never created; systemd refuses to start such a unit, and `Restart=on-failure` looped it forever. Pi OS ships those groups, so the intended platform hid it. |
| Six rounds of reading | Many real bugs — none of these three. |

None of that needed a Raspberry Pi. The `216/GROUP` one could not have been
found by emulating one either: pi-gen never boots the image it builds, and
a container has no systemd to refuse the unit.

## Is there a Raspberry Pi emulator?

Yes — QEMU has real Pi machine models (`raspi0`, `raspi2b`, `raspi3b`, and
`raspi4b` on newer versions; check `qemu-system-aarch64 -M help`). You can
boot a Pi OS image under one, usually by extracting the kernel and DTB from
the image's boot partition first, because QEMU does not run the Pi's
proprietary bootloader.

**But it is not the most useful thing to do.** Emulating a Pi gives you *a
Pi with nothing plugged into it* — no OLED on the I²C bus, no buttons on
the header, no printer. That is the one configuration the unit already
handles and the test suite already covers well. What has never run is the
code that talks to hardware that **is** present.

Linux has better tools for that than a Pi emulator, and they need no Pi at
all.

## Tier 1 — kernel simulators and a real cupsd

`pytest -m hardware`, with `./kernel-sim.sh up` first.

| Never-run path | What runs it for real |
|---|---|
| `GpioButtons` → gpiozero → lgpio → `/dev/gpiochipN` | **`gpio-sim`** — a virtual gpiochip whose line values are driven from sysfs. A real press, through the real driver stack. |
| `Ssd1306Display` → luma.oled → smbus2, and `_i2c_scan` | **`i2c-stub`** — a fake SMBus device at `0x3C`. The init sequence really runs; there is nothing to look at. |
| `hmi.screen_connected()` reading DRM status | **`vkms`** — a real DRM connector, so the probe reads a file a kernel wrote. |
| `hmi.keyboard_connected()` reading `/proc/bus/input/devices` | **`uinput`** — a virtual keyboard with a real `kbd` handler. |
| Everything in `printer.py`, and the whole unattended sequence | **A real `cupsd`** in a temp directory, with a backend that records the bytes that reached the printer. |

The CUPS rig (`tests/cupsrig.py`) takes its `cupsd.conf` directives *and*
its spool/tmp/cache modes out of `device/install.sh` by parsing it, rather
than restating them. A rig carrying its own copy of `MaxJobs` would have
gone on passing after somebody changed the shipped value — which is
precisely the bug it exists to catch.

Two things it found while being written, neither of which is a defect but
both of which are worth knowing:

- **The job title is embedded in a PJL header inside the data stream**, so
  it reaches the printer's own memory. The unit's titles are deliberately
  codeword-free, and there is now a test covering the *uncompressed*
  channels -- the PJL job name and the PDF Info dictionary -- rather than
  only the title. It is not, and cannot be, a claim that the codeword
  never reaches the printer: the codeword is printed on every pad page by
  design, so a device that did not send it could not print it.
- **The filter chain is deterministic.** Two submissions of one document
  differ in exactly two places, both intentional: the PJL job name and the
  PDF `/ID`. Normalising those, copy A and copy B are byte-identical all
  the way to the backend.

```sh
sudo ./harness/kernel-sim.sh up      # loads the modules, reports what it got
sudo pytest -m hardware -v
sudo ./harness/kernel-sim.sh down
```

**On Ubuntu, unconfine cupsd first.** Ubuntu ships an AppArmor profile that
permits cupsd to execute backends from `/usr/lib/cups/backend` and nowhere
else — and the rig is deliberately hermetic, with its own ServerBin in a
temp directory. A confined cupsd starts, answers `lpstat`, and reports no
printers at all:

```
apparmor="DENIED" operation="exec" profile="/usr/sbin/cupsd"
  name="/tmp/.../cups/bin/backend/usb" comm="cups-deviced"
```

```sh
sudo apparmor_parser -R /etc/apparmor.d/usr.sbin.cupsd
```

The rig detects this and skips with that instruction rather than failing
obscurely. CI does the same removal and then **asserts it worked** — a
silently-skipped harness is a green job that tested nothing, which is worse
than a red one.

Anything unavailable is **skipped with a reason, not failed** — a kernel
without `vkms` should still exercise the other four. `kernel-sim.sh up`
exits 0 even at zero of four, because that is a reason to skip the
hardware tests rather than to fail the build.

On a GitHub runner, `gpio-sim`, `i2c-stub` and `vkms` are not in the base
kernel — they need `linux-modules-extra-$(uname -r)`. Measured before that
was installed: `1 of 4 up`, with only `uinput` present.

### The gpiozero limitation, stated plainly

`gpio-sim` gives you a real gpiochip. Getting **gpiozero** to talk to it is
a separate problem: gpiozero picks its chip from Raspberry Pi board
detection — `/proc/device-tree/model`, or a `Revision` line in
`/proc/cpuinfo` — and a machine that is not a Pi has neither. Where it does
construct, it opens `gpiochip0`, which on an ordinary host is some real
controller rather than the one `gpio-sim` just made.

So the button tests check which chip the process actually opened, and skip
with that reason if it is not the simulated one. Asserting against a chip
the driver never opened would fail for a reason unrelated to the code under
test; quietly passing would be worse. Closing this properly means faking
the board identity in a mount namespace, which is a bigger piece of work
than it looks and is not done yet.

Runs in CI as the `hardware` job.

## Tier 2 — a VM that actually boots the thing

`./harness/vm-check.sh`, or the `vm` job in CI.

The riskiest untested change in the repository is `otp-unit.service`
binding `tty1` — `StandardInput=tty-force`, `TTYPath=/dev/tty1`,
`Conflicts=getty@tty1.service`. If that is wrong the unit restart-loops
instead of starting, and **nothing else will say so**: pi-gen never boots
the image it builds, a container has no virtual terminals at all, and the
unit tests substitute systemd entirely.

So this boots a Debian 13 cloud image, runs `device/install.sh` on it, and
asks the questions only a booted system can answer:

- is `otp-unit.service` active, and has it restarted more than once?
- did `Conflicts=` actually stop `getty@tty1`?
- is the unit's main process really holding `/dev/tty1`?
- is a login prompt still reachable on tty2?
- is swap off, is the journal volatile, are core dumps disabled?
- is the spool on tmpfs?
- does `cupsd -t` still accept the config *after* install.sh edited it?
- is `install.sh` idempotent, as the top of the file claims — and does the
  service survive being reprovisioned under it?

### Three boots, because one boot answers the wrong question

Every check above used to run in the *same boot* `install.sh` ran in, with
the service hand-started by the harness. That describes a machine seconds
after provisioning. The device's actual life is flash → boot → run →
power-cycle, and **a unit that starts when hand-started but not when systemd
starts it at boot would have passed**.

| Phase | Boot | What is true of it |
|---|---|---|
| `provision` | 1 | `install.sh` has just run. Root still writable, service hand-started. |
| `overlay` | 2 | systemd started the unit on its own, and the read-only overlay is engaged. Writes a sentinel to `/` and a setting through `config.save()`. |
| `persist` | 3 | The sentinel must be **gone** and the setting must still be **there**. |

The core checks run in all three. The gate requires **every** phase to have
reported, exactly once, completely, with its counts in agreement — because a
boot that never happened produces no output, which is indistinguishable from
a boot with nothing to say. The single-phase form this replaced took
`tail -1` of the result line, so a phase that died silently would have left
the previous phase's success standing as the whole answer.

`tests/test_vm_verdict.py` runs that gate against synthetic consoles and is
itself mutation-tested: reverting it to last-phase-only, or dropping the
finished / reported-twice / FAIL-line / qemu-exit checks, each turns the
right tests red. That is deliberate — four harness checks that could not
fail have been found in this repository so far, and reading did not catch
any of them.

### What the overlay phases do and do not prove

The guest uses Debian's `overlayroot` package. A Pi uses `raspi-config
nonint enable_overlayfs`, which is a **different mechanism**. So tier 2
establishes that *the unit survives a read-only root* — cupsd starts, the
spool lands somewhere writable, `config.save()` still persists. It does
**not** establish that the Pi's overlay is correctly configured in the
shipped image. That claim belongs to tier 3, which boots the real thing.

`/boot/firmware` is given its own virtio disk, formatted FAT and mounted by
label, mirroring the Pi's geometry — a `/boot/firmware` that were merely a
directory on the root would be swallowed by the overlay, and the persistence
checks would pass in the guest for a reason that does not hold on the
device.

One trap worth knowing if you touch this: cloud-init decides "have I run
before?" from state under `/var/lib/cloud`, which the overlay discards. From
boot 2 onward it would see a new instance every time and re-run the whole
`runcmd` — `reboot` included. `/etc/cloud/cloud-init.disabled` is written on
boot 1, before the overlay engages, which is why it survives.

**Why amd64 rather than arm64.** Nothing on that list is
architecture-specific. Emulating arm64 on an x86 host costs half an hour a
run and buys none of it; amd64 with KVM is a few minutes, which is the
difference between running per commit and never running. The
arm64-specific half is covered elsewhere — the image build runs
`install.sh` in a real arm64 chroot, and tier 3 boots the actual image.
This tier is about what happens *after* boot.

The repository reaches the guest as a tar handed over as a raw block
device (`tar -xf /dev/vdb`). That needs no filesystem driver on either
side, which is one less thing to be wrong.

**`python3-lgpio` is deliberately absent** in the guest — it comes from
`archive.raspberrypi.org`, not Debian. That is the more interesting case
anyway: a unit with no working GPIO must fall through to printing
unattended rather than failing to start.

## Tier 3 — the built image under `-M raspi3b`

`./harness/img-boot.sh <image.img.xz>`, or the `Boot the image` step in
`image.yml`.

**First run: 2026-08-08. It did not boot.** Everything the review fixed
before it had ever executed did work — the boot partition was found from
the MBR at sector 16384 (the old hardcoded 8192 read the pre-partition
gap), the image was padded to a power of two, and both `mcopy` calls found
their files. Then QEMU exited 0 having written **nothing** to the serial
console, not even `Linux version`. Tracked in issue #17.

It no longer carries `continue-on-error`. It used to, so that an
unvalidated step could not cost anyone the image — and the effect on its
first real run was worse than the problem it solved: tier 3 exited 1 and
the job went green, with the API reporting the step as `success`. Anyone
reading the status rather than the log would have concluded the image
booted. The boot now runs **after** the artifact is uploaded, so it is free
to fail loudly without costing anything, and the verdict is written to the
run's step summary as well as the log.

The image build also now runs on any pull request touching `image/**`,
`device/**`, `harness/img-boot.sh` or the workflow itself. Before that,
nothing checked the image on the PR that broke it.

It answers exactly one question nothing else can: **does the thing that
gets flashed to a card actually boot?** pi-gen assembles a filesystem and
never starts it; tier 2 boots a Debian cloud image rather than this one. A
missing kernel module, a broken `cmdline.txt`, an fstab naming a partition
that moved — all invisible until something powers it on.

Two details that make it fiddly, both handled in the script: QEMU does not
run the Pi's proprietary bootloader, so `kernel8.img` and the DTB are
pulled off the FAT boot partition with `mcopy` and passed directly; and
QEMU's `sd` interface refuses any image whose size is not a power of two,
which pi-gen's output never is.

**Its peripheral coverage is worse than tier 1's**, and that is not a
defect in the plan — a QEMU Pi is a Pi with nothing plugged into it. Once
per image, not per commit.

## What none of this gives you

Print quality. Whether 5.5pt Courier is legible at 600dpi, whether the crop
marks line up under a guillotine, whether a specific GDI laser has a
working driver at all. Those need paper and a real printer. A Pi Zero 2 W
is about £15 if it comes to that.

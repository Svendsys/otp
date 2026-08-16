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

### Making gpiozero talk to gpio-sim

`gpio-sim` gives you a real gpiochip. Getting **gpiozero** to use it was a
separate problem, and for a long time it was the reason the entire input
surface of this device — three buttons and a long press — had never
executed against anything but a fake. gpiozero decides it is on a Pi by
finding a board revision, and a machine that is not a Pi has none; without
one it declines to build *any* local pin factory:

    PinFactoryFallback: Falling back from lgpio: unable to locate Pi
    revision in /proc/device-tree or /proc/cpuinfo
    gpiozero could not open a gpiochip here: Unable to load any default
    pin factory!

`tests/pirig.py` lends the process a revision — `0x902120`, a Pi Zero 2 W
rev 1.0, the board this unit ships on — inside a mount namespace of its
own, and the button tests then assert the binding instead of skipping on
it. Three measurements shaped how:

- **The order gpiozero reads in.** It takes
  `/proc/device-tree/system/linux,revision` first and falls back to the
  `Revision` line in `/proc/cpuinfo` only when that is absent.
  `/proc/device-tree/model` is never consulted for this, and could not be
  conjured anyway: procfs refuses to create entries (`mkdir
  /proc/device-tree` → "No such file or directory"). Bind-mounting over a
  procfs file that *already* exists works, which is why `/proc/cpuinfo` is
  the one that can be forged.
- **The namespace has to be made private first.** A new mount namespace
  inherits the propagation of the one it was copied from, and on a systemd
  host — every GitHub runner — `/` is shared, so a bind mount inside
  propagates straight back out. Measured, without and with
  `MS_REC|MS_PRIVATE`: the outer namespace saw `Revision: 902120`, then
  saw nothing.
- **The revision decides the pin header.** Get it wrong and you get a Pi,
  but the wrong one: `0x2` decodes to an original model B, whose headers
  are P1/P2/P3, and `Button(5)` dies with `PinInvalidPin: GPIO5 is not a
  valid pin name` — a message with no visible connection to the mistake.

`lgpio` addresses chips by *number*, so where `gpio-sim` does not land on
`gpiochip0` the factory is built against the recorded chip explicitly. The
fixture then asserts which `/dev/gpiochipN` the process actually has open,
so a panel talking to some other controller fails loudly rather than
quietly proving nothing.

Leaving the namespace is done by unmounting, not by `setns` back to where
we came from: the kernel refuses `setns(CLONE_NEWNS)` once the process has
threads (`mntns_install` wants `fs->users == 1`, and pthreads share `fs`),
and lgpio's alert thread starts at `import lgpio` — `_notify_thread =
_callback_thread()` is module level and its constructor calls `start()` —
and never stops. Not the first panel: `diagnostics.py` imports lgpio for
a version string on the status sheet, so the thread is already running by
the end of the CUPS tests. Measured: `[Errno 22] setns back: Invalid
argument`, in the teardown of the first button test.

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
| `provision` | 1 | `install.sh` has just run. The service is hand-started by the harness. |
| `reboot` | 2 | **systemd** started the unit this time. Writes a setting through `config.save()`. |
| `persist` | 3 | The setting must still be there, read back through `config.load()`. |

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
any of them. Those mutations are no longer applied by hand: they are rows in
`tests/mutations.toml`, and CI runs them on every pull request. See
[proving the guards can fail](#proving-the-guards-can-fail).

### The read-only overlay is NOT tested here — tier 3 owns it

Two mechanisms were tried in this guest and neither works. Both are worth
writing down, because both look like they should:

- **Debian's `overlayroot` package** feeds `mount` an option it rejects,
  fails to pivot, and panics the kernel: `Attempted to kill init!`.
- **`systemd.volatile=overlay`** is accepted on the kernel command line and
  silently ignored. It is implemented by systemd *inside the initrd*, and
  Debian's initramfs-tools initrd has no systemd in it. Measured: the flag
  present in `/proc/cmdline`, and `/` still plain `rw` ext4.

The first of those turned out not to be a property of this guest at all. The
option `mount` rejects is `--move`, and the `mount` in an initramfs-tools
initrd is klibc's, which does not implement it:

```
$ /usr/lib/klibc/bin/mount --move /a /b
mount: invalid option --
```

— byte for byte the string the guest printed. Raspberry Pi OS trixie
installs the same `overlayroot` and builds its initrd the same way, so
`raspi-config nonint enable_overlayfs`, which is what the documentation told
operators to run, would have taken a flashed unit down the same way. It
survives only where busybox happens to be in the initramfs, because
busybox's `mount` accepts `--move`.

So the overlay is enabled by `install.sh` through initramfs-tools' own
`boot=overlay` hook, and **tier 3 is what proves it** — the tier that boots
the real image. Tier 2 still claims only what it demonstrates, and what it
demonstrates does not include a read-only root.

`/boot/firmware` still gets its own virtio disk, formatted FAT and mounted
by label, mirroring the Pi's geometry — the device keeps settings there
precisely *because* it is outside the overlay, so testing persistence
against a directory on the root would pass for a reason that does not hold
on hardware.

Two traps worth knowing if you touch this:

- **Never replace `GRUB_CMDLINE_LINUX`; append to it.** The image sets
  `console=ttyS0,115200` there, and that is the only reason this tier has a
  serial console. Replacing it cost a run in which boots 2 and 3 did all
  their work and reported into a console connected to nothing.
- **The kernel and the guest share that console.** A kernel message once
  landed mid-marker and split `OTP-RESULT provi|sion` in half. The guest
  runs at `loglevel=3` and the host matches the marker as a whole pattern
  anywhere in the line.

**Why amd64 rather than arm64.** Nothing on that list is
architecture-specific. Emulating arm64 on an x86 host costs half an hour a
run and buys none of it; amd64 with KVM is a few minutes, which is the
difference between running per commit and never running. The
arm64-specific half is covered elsewhere — the image build runs
`install.sh` in a real arm64 chroot, and tier 3 boots the actual image.
This tier is about what happens *after* boot.

The repository reaches the guest as a tar handed over as a raw block
device, resolved by serial (`/dev/disk/by-id/virtio-otprepo`) rather than
by number -- adding a disk once renumbered `vdb` to `vdd` and tar quietly
unpacked a blank partition. That needs no filesystem driver on either
side, which is one less thing to be wrong.

**`python3-lgpio` is deliberately absent** in the guest — it comes from
`archive.raspberrypi.org`, not Debian. That is the more interesting case
anyway: a unit with no working GPIO must fall through to printing
unattended rather than failing to start.

## Tier 3 — the built image under `-M raspi3b`

`./harness/img-boot.sh <image.img.xz>`, or the `Boot the image` step in
`image.yml`.

**First run: 2026-08-08. It did not boot** — QEMU exited 0 having
written nothing to either UART. Fifteen runs later it goes green in
~4.5 minutes: the wrong DTB (raspi3b vs the B+ tree), a watchdog in
QEMU's partial PM model resetting the guest at 11.5s
(`initcall_blacklist=bcm2835_pm_driver_init`), a console parameter
naming a device this DTB doesn't have (`ttyAMA1`, not `ttyAMA0` — six
runs diagnosed a "freeze" that was a dead console), and Raspberry Pi
OS's first-boot wizard holding `multi-user.target` open forever — a
real bug that would have shipped to hardware, now handled by a
systemd drop-in in `install.sh`. Run 17 found two more of that kind:
an unbounded wait for a network the appliance does not have holding
`multi-user.target` open on every boot, and the credential path
stopping the front panel as its last act. Both are `install.sh` fixes
with checks on the booted image, below. The harness stops the emulator
the moment the guest reports done and judges the boot on ANSI-stripped
console evidence, not qemu's exit code. The full fifteen-run narrative
lives in `img-boot.sh`'s header and issue #17.

### Two boots, because the overlay is only observable across a power-cycle

Issue #9: the read-only root was the largest completely unobserved surface
in the project, and it is the property everything else rests on — a
power-cycle is meant to be a full reset. `install.sh` enables the overlay
now, so the artifact this tier boots has it, and this is the only tier that
can ask whether it works: tier 2's guest is a Debian cloud image with a
different mechanism, and pi-gen never boots what it builds.

| Phase | What is true of it |
|---|---|
| `boot1` | The overlay is engaged. Writes a sentinel to `/` and a setting to `/boot/firmware` through `config.save()`. Goes in with a valid `userconf.txt` seeded into the FAT partition, which has to be consumed. |
| `boot2` | The **same card image**, booted again. The sentinel must be gone; the setting must still be there; the consumed seed must still be consumed. |

One image file across both boots is the whole mechanism: a write that
reached the card is there in boot 2 and a write that only reached the
overlay's tmpfs is not. A fresh copy would answer both questions with the
image build's own contents.

**Run 16 (31752321387) is the first one that did this, and it went green on
its first attempt** — 9/9 guest checks in boot 1, 11/11 in boot 2, each boot
reaching ~150s guest time with one kernel entry, and both ANSI-stripped
consoles carrying `random: crng init done` and `bcm2835-rng 3f104000.rng:
hwrng registered`. Measured cost on a cache miss: 6m58s of pi-gen, then
7m59s for the pair of boots, 16m16s for the whole job.

Those two totals are run 16's and stay run 16's. The credential checks
below added three names to what boot 1 must report and three to boot 2, and
one more — the network wait — to both, so a green run now counts
differently: 13 in boot 1 and 15 in boot 2. Run 17 measured 9/11 and 13/14
against the counts as they stood then, which is a different arithmetic
again; the numbers move with the list and only the list is authoritative.
The cost is unchanged: no extra boot, and the one bounded experiment inside
boot 2 is capped at 60 seconds.

**`OTP_IMG_PHASES` picks the boots, and it may not drop `boot2`.** It
defaults to `boot1 boot2`; a run debugging the boot itself can shorten it,
and `img-boot.sh` exits 1 with a message if what is left does not include
`boot2`. That guard is not tidiness. The same list drives the boot loop,
the verdict loop *and* the set of guest checks each phase is required to
have reported, so removing `boot2` removed
`root-writes-discarded-by-the-power-cycle` and
`settings-survive-the-power-cycle` from what the run demanded, along with
the boot that would have produced them: measured, `OTP_IMG_PHASES=boot1`
over a healthy boot-1 console exited 0 and printed *"the image boots twice
on a read-only overlay"*, and `image.yml` puts that claim, in its own
words, into the body of a tagged release. The concluding line is now
conditional on the phases that actually ran as well, so the two guards
have to fail together for the claim to be wrong.

### The seeded `userconf.txt`, which is the other thing a first boot does

Issue #20. The wizard that held run 12's boot open is also the only consumer
of Raspberry Pi OS's documented headless credential file: an operator writes
`name:crypted-password` into `/boot/firmware/userconf.txt`, and the first
boot applies it non-interactively and deletes it. That is why `install.sh`
replaced the mask with a `ConditionPathExists` drop-in rather than turning
the unit off. Every run up to #32 exercised only the branch where nobody had
written one — no seed, no wizard, boot green — so the two branches an
operator actually meets rode along with the two boots that already existed:

| Branch | Where | What has to be true |
|---|---|---|
| seeded | `boot1` | The seed reaches the card (checked *before* the boot), is gone afterwards with no `failed_userconf.txt` beside it, its hash is in `/etc/shadow`, `userconfig.service` finished successfully holding no job, and the front panel still owns tty1 afterwards. |
| unseeded | `boot2` | The delete stuck — the FAT partition is outside the overlay — so the condition is false and the unit is skipped **while staying enabled**. |
| malformed | `boot2` | The shipped `userconf-service`, handed a bad seed with stdin closed and no `TERM`, terminates inside a 60s bound and leaves `failed_userconf.txt` with no `userconf.txt` behind it. |

### Run 17 (31968966879): the first seeded boot, and what it found

Every credential check failed, in both boots, and the image was what was
wrong. The guest reported `condition=no result=success is-active=inactive
jobs=1` in boot 1 and `condition=no checked-at='never'` in boot 2, with
`userconf-seed-planted PASS` beside them: the seed reached the card and
nothing on the machine ever looked at it. A **queued start job** with the
condition never evaluated is not a skip, and the consoles say what it was
queued behind — both of them end on

```
Job systemd-networkd-wait-online.service/start running (2min 37s / no limit)
```

with `multi-user.target` never reached in either boot. That unit is
`TimeoutStartSec=infinity` on an appliance whose links NetworkManager owns,
so it blocks `network-online.target`, which blocks cloud-init's
`cloud-config.service`, which stock `userconfig.service` is ordered after.
**Every unit this image would have produced ignored the operator's
`userconf.txt` in silence** — the outcome `install.sh` replaced a mask to
avoid, arriving through the ordering instead of the condition.
`install.sh` masks the wait and switches cloud-init off, and the probe reads
the mask back with its job queue rather than trusting the line that wrote it.

The apply's own tail came out of the same reading. It ends in
`/usr/lib/userconf-pi/userconf` → `cancel-rename` → `systemctl --no-block
start getty@tty1`, and `otp-unit.service` carried
`Conflicts=getty@tty1.service`: setting a password the documented way would
have **stopped the print unit** until the next power-cycle. A condition on
the getty replaces the conflict — masking it would fail that start, and
`userconf-service` runs under `sh -e`, so it would die before deleting the
applied seed and `Restart=on-failure` would loop it.

The same run settled the one question the malformed-seed experiment was
uncertain about: whiptail with no `TERM` **fails** rather than blocking —
`rc=1 ... TERM environment variable needs set.` — so the fail-fast is real
and the 60s bound was never reached.

Three details worth knowing about that last row. It is run **by hand from
the probe** rather than left on the card for the unit to find at boot,
because the stock `userconfig.service` carries `Restart=on-failure`: a boot
that met a malformed seed would print `Failed with result` and `Scheduled
restart job`, two of the strings `img-boot.sh` fails a release on, and
weakening a release gate to accommodate a fault the harness injected itself
is the wrong trade. What the unit contributes to the fail-fast,
`StandardInput=null`, is instead read back off the running machine with
`systemctl show`. And what is *gated* is that it ended and left the
evidence; the exit status is reported rather than gated, because the failure
that hurts on a device is a script that never returns.

The seed names the image's own first user (`FIRST_USER_NAME=otp`) on
purpose: `/usr/lib/userconf-pi/userconf` renames the UID-1000 account when
the seed names a different one, and that is a much larger experiment than
"were the credentials applied". Its hash is sha512-crypt with a fixed salt,
generated with `openssl passwd -6 -salt otpimgcheck` and verified against
glibc's `crypt(3)`; pi-gen salts `FIRST_USER_PASS` randomly, so that salt
appearing in a shadow entry can only have come from the seed. It never
reaches a shipped image — `img-boot.sh` writes it into the decompressed
working copy it boots, and `image/deploy/*.img.xz` is never opened for
writing.

The other variables: `OTP_IMG_TIMEOUT` is the per-boot backstop in seconds
(CI sets 600; the local default is 1200 because someone running this by
hand is debugging), and `OTP_IMG_WORK` is where the decompressed card, the
per-phase console directories and `verdict.txt` are written.

The reporting comes from inside the guest, because `findmnt /` and
`cupsd -t` need a running machine. `otp-unit-imgcheck.service` **ships in
the image** and is gated on `otp.imgcheck` in the kernel command line, which
nothing but `img-boot.sh` supplies — so it never runs on a flashed unit.
The probe refuses to run without that token too: it writes a sentinel to
`/` and a marker page count into `/boot/firmware/otp-unit.conf`, which is
outside the overlay, and one `ConditionKernelCommandLine=` line in one unit
file is thin protection for a 0755 script that ships on every appliance.
What tier 3 boots is still the artifact people flash rather than a variant
built for testing.

The gate over what it says names the checks it expects rather than only
counting `FAIL` lines. Zero FAIL lines is trivially true of a guest that
checked nothing, which is row 4 of issue #14 arriving somewhere new;
`tests/test_img_verdict.py` holds the harness's list and the guest script
against each other so neither can shrink quietly.

**One correction to how the issue phrased it.** "A write to `/` fails" is
not what a working overlay does, and asserting it would have been asserting
a broken one. Writes to `/` succeed and land in the tmpfs upper layer; the
card underneath is mounted read-only and is never written. What tier 3
gates on is therefore that `/` is an overlay, that a write to it succeeds,
and that it is *gone after the power-cycle* — reported last, after two
positive controls in the same phase, because an absence on its own is
equally satisfied by a boot 1 that never ran, a sentinel written somewhere
else, and a rig that cannot write to `/` at all.

It no longer carries `continue-on-error`. It used to, so that an
unvalidated step could not cost anyone the image — and the effect on its
first real run was worse than the problem it solved: tier 3 exited 1 and
the job went green, with the API reporting the step as `success`. Anyone
reading the status rather than the log would have concluded the image
booted. The boot now runs **after** the artifact is uploaded, so it is free
to fail loudly without costing anything, and the verdict is written to the
run's step summary as well as the log.

The image build also now runs on any pull request touching `image/**`,
`device/**`, `harness/img-boot.sh`, `harness/img-guest-check.sh` or the
workflow itself. Before that, nothing checked the image on the PR that broke
it. The probe is in that list, and in the cache key, because it **runs
inside the image**: the pi-gen stage copies `harness/` into the rootfs and
`install.sh` installs it to `/opt/otp-unit`. While it was in neither, a pull
request that changed only a guest check ran no image job at all, and a run
that did would have restored an image built before the edit.

It answers the questions nothing else can: **does the thing that gets
flashed to a card actually boot, and does it boot the way it is supposed
to?** pi-gen assembles a filesystem and never starts it; tier 2 boots a
Debian cloud image rather than this one. A missing kernel module, a broken
`cmdline.txt`, an fstab naming a partition that moved, an overlay that was
configured but never assembled — all invisible until something powers it on.

Three details that make it fiddly, all handled in the script: QEMU does not
run the Pi's proprietary bootloader, so `kernel8.img`, the DTB **and the
initramfs** are pulled off the FAT boot partition with `mcopy` and passed
directly (`auto_initramfs=1` means nothing to an emulator that reads no
firmware configuration); `-append` replaces the kernel command line
wholesale, so `boot=overlay` is *copied out of the image's own cmdline.txt*
rather than added — an image built without the overlay boots without it here
too, and fails; and QEMU's `sd` interface refuses any image whose size is
not a power of two, which pi-gen's output never is.

**Its peripheral coverage is worse than tier 1's**, and that is not a
defect in the plan — a QEMU Pi is a Pi with nothing plugged into it. Once
per image, not per commit.

## Proving the guards can fail

Every tier above is a check, and a check has one failure mode that nothing
else in CI can see: **it stops being able to fail**. A broken unit test goes
red and somebody fixes it. A broken guard goes green and nobody hears
anything.

Five have been found here, one per audit round (issue #14): a boot offset
hardcoded at sector 8192 while pi-gen's arm64 branch aligns at 16384, so both
`mcopy` calls read a zero-filled gap; a tier-3 verdict that grepped for
`otp-unit`, which is the image's *hostname*; a tier-2 spool check using
`findmnt --target`, which reports the containing mount, and `/run` is always
tmpfs; a truncated-run gate satisfied by zero FAIL lines, which is trivially
true of a guest killed after two checks; and a group guard that matched the
*text* of a `for group in …; do` header and never ran the body. Each was
found by hand and mutated by hand, and the next one was found the same way a
round later.

A sixth arrived with the overlay work, and it points the other way: a check
that could not *pass*. `install.sh` runs under `pipefail`, and the new
"does this initramfs contain the overlay script" check was first written as
`lsinitramfs … | grep -q scripts/overlay`. `grep -q` closes the pipe on its
first match, the producer dies of SIGPIPE, and the pipeline returns 141 — so
the condition is false for exactly the initramfs that should pass, and the
image build fails on a good image. Measured at rc 141 against a 300k-line
producer. It survived its own unit test at first because a three-line
fixture finishes writing before grep can exit; the fixture is twenty
thousand lines now, and `install-overlay-listing-piped-into-grep-q` is what
keeps it that size.

`tests/mutations.toml` is that round as a table, and `tests/mutation_gate.py`
runs it: for each row it applies exact edits to the shipped files, runs the
tests named as having to notice, restores the tree, and fails if the suite
stayed green.

```sh
python3 tests/mutation_gate.py --list
python3 tests/mutation_gate.py --tier fast       # 38 rows, 29s
sudo python3 tests/mutation_gate.py --tier hardware   # 3 rows, 36s, needs cupsd
```

**Runtime decided the trigger.** The issue expected nightly or
label-triggered; measured, the fast tier is twenty-nine seconds at 38 rows —
still cheaper than the suite it audits — so it runs per pull request as its
own `mutation` job, and the ordinary suite's wall clock does not move. The three CUPS-rig rows run in
the existing `hardware` job, the only place with a real `cupsd`, for 36
seconds on top of about eight minutes.

Every way this could rot into a no-op is a loud failure rather than a skip: a
`find` string that no longer matches (the fast suite checks that much without
running anything), a `find` that matches twice, tests that were already red
before the mutation, tests that all *skipped*, a run that hangs, and a tier
that was not selected — which is named in the summary with its row count, so
a partial run cannot read as a complete one.

Two rows carry **two edits**, and those are the interesting output so far.
Both were single-edit rows that survived:

- the tier-2 CR strip. Removing it left all 17 tier-2 tests green, because
  `grep -oE` prints only the matched text and the pattern ends at `[0-9]+`,
  so the `\r` never reaches the comparison. The property has two defences and
  the row now removes both.
- `MaxJobs`. Putting the shipped value back to 1 — the defect that made the
  unit unable to print a pad at all — leaves the whole hardware tier green,
  because `send()` now drains the queue before every submit. Removing that
  drain alone also survives, at MaxJobs 4, because generating a pad is slower
  than printing one. Together they are red in 3.8 seconds. The number stopped
  being what protects that sequence; the wait is.

**What is not covered.** No row needs a booted guest, and the checks that
only exist *inside* one — tier 2's spool redirect above, tty1 ownership, the
persistence phases, and now tier 3's overlay probe — cannot be mutated from
here. What the nineteen overlay rows attack is the *host-side gate* over what the
guest says and the *provisioning* that sets the overlay up; that the guest
reports the truth about a machine with a read-only root is a claim only a
booted image can settle, and only the image build makes one. Proving the
guest's own checks needs a `vm` or `img` tier with its own trigger; tiers
carry their own pytest arguments and timeout, so adding one is a table entry
rather than a change to the runner. None is seeded, because a row whose red
was never observed is worse than no row at all.

**Restoring the tree.** Not `git checkout` — a hand-run round used it and
destroyed an uncommitted fix. Not `git stash push` + `git stash drop` either:
measured, that reverts the file to HEAD and carries the working copy's edits
into the dropped stash, which is the same defect one step removed. The runner
reads each file's bytes before mutating and writes exactly those back, so a
tree with work in progress in it comes out unchanged; `git stash create`
takes a recoverable snapshot at the start without touching the worktree.

## What none of this gives you

Print quality. Whether 5.5pt Courier is legible at 600dpi, whether the crop
marks line up under a guillotine, whether a specific GDI laser has a
working driver at all. Those need paper and a real printer. A Pi Zero 2 W
is about £15 if it comes to that.

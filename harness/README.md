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

### The one thread every button in the process shares

That same singleton is why `GpioButtons` wraps every callback it hands to
gpiozero. `_callback_thread.run` dispatches with no guard around the call
(lgpio 0.2.2.0, lgpio.py:531‑559):

    for cb in self.callbacks:
        if cb.chip == chip and cb.gpio == gpio:
            cb.func(chip, gpio, level, tick)

so an exception out of `cb.func` ends the thread — and because the thread
is process-wide, **no button anywhere in the process fires again**,
including a panel built afterwards, for as long as nothing puts a reader
back on that handle. Nothing did until `GpioButtons._watch`, which notices
the death on the next `wait()` and starts a replacement over the same
handle, file object and callbacks list; that covers the lgpio factory and
no other, and which factory the shipped unit runs on is issue #43.
gpiozero adds nothing on
the way in: `LGPIOPin._call_when_changed` → `PiPin._call_when_changed` →
`Button._pin_changed` → `_fire_events` → `_fire_activated` contains no
`try` at all. The only report is a `PytestUnhandledThreadExceptionWarning`
under pytest, or an ignored-exception line otherwise. Observed while
fixing issue #12: `lgpio: notify thread alive=False go=True`, with the
gpio-sim line moving perfectly.

On the unit the trigger to expect is not #12's dead weakref — a real panel
is built once — but a `MemoryError`: 512 MiB, no swap, pads of up to 1000
pages. A dead panel there costs a power cycle, which discards the pad in
progress.

`GpioButtons._guarded` catches, names what was lost on stderr (the
journal), and carries on. It is checked in three places, deliberately:
`tests/test_hardware.py` drives the shipped callbacks through a stand-in
dispatcher with lgpio's fatal shape **and** through lgpio's own `run` fed
forged edges down a pipe — neither needs a kernel — and
`TestTheRealButtonPath::test_a_raising_callback_does_not_kill_the_panel`
here does it with a real gpiochip and the real gpiozero → lgpio chain. All
of them assert the same property: a *later* press still arrives. "An
exception was caught" is also true of a panel that has stopped delivering
anything.

Three places, and for a while only two of them ran anywhere. `pytest -m
hardware` deselects `tests/test_hardware.py` wholesale — nothing in it is
marked `hardware` — and the `test` job installs `requirements-dev.txt`,
which has neither gpiozero nor lgpio, so in a venv with only that in it the
file measures **52 passed, 13 skipped**: the six `[lgpio]` parametrisations,
all four `TestRealGpiozero` tests — the control among them — and the three
in `TestRevivingLgpiosOwnDispatchThread`, which need a real lgpio to kill
and revive. This job runs
the file by name as well, in a step that treats a skip as a failure —
`gpiozero` and `lgpio` are installed here, so nothing in it has a reason to
skip, and a skipped test in a summary line looks exactly like a passing
one.

Nothing may **park** that thread either, which is a hazard the guard
created: the report goes to stderr, and on the unit stderr is a stream
socket to journald. A socket nobody is draining does not raise, it blocks,
and a thread stuck in `write(2)` is as dead to the panel as one that ended
— while still answering `is_alive()`. `_report` asks `select()` whether the
write can complete before making it, and `_note_lost` bounds how often
there is anything to write; both have tests and mutation rows of their own.

Runs in CI as the `hardware` job.

## Tier 2 — a VM that actually boots the thing

`./harness/vm-check.sh`, or the `vm` job in CI.

The riskiest untested change in the repository is `otp-unit.service`
binding `tty1` — `StandardInput=tty-force`, `TTYPath=/dev/tty1`, and a
getty that `install.sh` conditions off that tty instead of conflicting
with it (issue #20 — see tier 3 below for why the conflict had to go).
If that is wrong the unit restart-loops
instead of starting, and **nothing else will say so**: pi-gen never boots
the image it builds, a container has no virtual terminals at all, and the
unit tests substitute systemd entirely.

So this boots a Debian 13 cloud image, runs `device/install.sh` on it, and
asks the questions only a booted system can answer:

- is `otp-unit.service` active, and has it restarted more than once?
- is `getty@tty1` off the panel's tty — conditioned off for every boot,
  and stopped on the live machine `install.sh` just provisioned?
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

Those two totals are run 16's and stay run 16's. Every count below them is
a different arithmetic again: the credential checks added three names to
boot 1 and three to boot 2 with one — the network wait — to both, and run 20
(31979545889) measured **13/13 and 15/15** against that list, at 175.5s and
151.1s of guest time, 7m58s for the pair of boots on top of 7m06s of pi-gen
and 16m14s for the whole job. The journal work then added two names to both
boots and two more to boot 1, so a green run is **17 and 17** now. The
numbers move with the list and only the list is authoritative;
`tests/test_img_verdict.py` reads it out of `img-boot.sh` rather than
restating it, so no fixture here can describe a healthy boot that is missing
a check.

The cost stays inside one budget: no extra boot, and the two bounded
experiments are in different phases — 90 seconds for the diagnostic sheet in
boot 1, 60 for the malformed seed in boot 2.

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
The packages themselves are purged out of the image now — see *The
provisioning agent that was on every image, and is not now* below.

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

### Run 18 (31972140190): the image was fixed, and the probe was not

With both blockers gone the boot changed shape completely: **`multi-user.target`
reached in both boots**, `network-online.target` with it, boot 1 at 12/13 and
boot 2 at **15/15**. The credential path did every documented thing —
`Finished userconfig.service - User configuration dialog.` on the console,
`userconf-seed-applied PASS ... begins $6$otpimgcheck$: yes`,
`userconf-seed-consumed PASS` off the card, and
`front-panel-survives-the-credential-apply PASS otp-unit=active
getty@tty1=inactive`.

The one red was the probe measuring a unit that no longer exists. A
successful apply ends in `cancel-rename`, which runs `systemctl disable
userconfig` and a daemon-reload; the oneshot is inactive and unreferenced by
then, so **systemd garbage-collects it**, and every property a later
`systemctl show` returns is a pristine default:

```
OTP-CHECK boot1 userconf-seeded-boot-ran-no-wizard FAIL
  condition=no result=success is-active=inactive jobs=0
```

— on the boot whose console says the unit finished. The 120s settle poll
spent all of itself waiting for a `ConditionTimestamp` that had been
collected with the unit, which is why boot 1 took 307s of guest time. The
check now reads systemd's own **log** for the unit (`Finished
userconfig.service`, and no `Failed with result` / `Scheduled restart job` /
`was skipped`), which outlives the unit object and says more than
`ConditionResult=yes` ever did: not "systemd let it start" but "it ran to
completion, once, cleanly".

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

### The journal on the console, which is an emulation-only lever

Issue #21. Run 20 (31979545889) was the first fully green pair of boots —
13/13 in boot 1, 15/15 in boot 2, both reaching `multi-user.target`, 46848
and 45302 bytes of uart0 — and everything in it was a statement about
*getting started*. Whether the unit **did** anything afterwards was
unobservable: the journal is volatile by design (`Storage=volatile`,
`RuntimeMaxUse=16M` from `install.sh`), it dies with the guest, and nothing
carried it to the serial port the harness captures. The only exception was
the thirty lines the probe dumps at the very end, and nothing asserted them.

`-append` now carries **`systemd.journald.forward_to_console=1`**, and that
is where it stays. `-append` replaces the kernel command line wholesale
under emulation — the same reason `root=` has to be restated and
`boot=overlay` has to be copied out of the image — so the image's own
`cmdline.txt` is untouched and a flashed unit keeps its quiet console. A
device narrating its journal to whoever is holding the serial header is not
a feature; a test rig doing it is the whole point.

**The flag is not taken on trust**, because a kernel parameter that is
accepted and ignored looks exactly like one that works. The probe writes one
marker into the journal with `systemd-cat` and by *no other route* — not on
its own stdout, which `StandardOutput=journal+console` already copies to the
console, and never echoed back into a check's detail — and the verdict
requires it. The guest's `journal-marker-accepted` is the other half: it says
the journal *took* the marker, so a console without it is a forwarding
failure rather than a `systemd-cat` that did nothing.

**Two behavioural checks ride on it.** `unit-detects-no-panel` reads the
unit's own journal for three strings: `no OLED (` (the display probe raised),
`interface -- display: none,` (nothing was found to draw on) and `no usable
interface; printing unattended` (the unit chose the headless route). The
emulated Pi has no I²C panel and `-append` drops the `dtparam=i2c_arm=on`
that would give the bus a node, so the headless route is the correct one and
nothing had ever watched the shipped code choose it. The middle string is the
one that survives the board revision the harness now supplies — see run 72
below — and it also closes a hole the other two had: a unit with an HDMI
console and no buttons logs both of them. All three are held against
`otpunit/hmi.py` and `otpunit/__main__.py` by `tests/test_img_verdict.py`,
because a check whose needle lives in another file is one rewording away from
matching nothing forever.

`diagnostic-sheet-renders` and `diagnostic-sheet-reaches-cups` are the print
path, and they come with a correction to how the issue framed it. **The
headless path does not fire under QEMU**, by design rather than by accident:
`otpunit/__main__` falls into `diagnostics.run_headless()`, which loops on
`cups.devices()`, and `Cups.devices()` is built from `lpinfo -v` and keeps
only `usb://`, a loopback IPP endpoint, or a `dnssd://` entry matching an
attached USB device. An emulated Pi has no printer of any kind, so the list
is empty on every poll and the unit waits in silence — a unit with neither a
panel nor a printer says nothing at all, which is worth knowing about a
design whose answer to a missing panel is "the printer becomes the console".
So the probe supplies the one thing the emulator cannot, a queue, and drives
the rest through the shipped code: `diagnostics.collect()` over the real
machine, `render_bytes()` through the image's own reportlab, and
`Cups.submit()` into the shipped `cupsd`. What is gated is that the bytes are
a PDF and that cupsd answered with a job id naming that queue — enqueued,
not printed; there is nothing behind the URI and there does not need to be.
The queue is the probe's own name, removed afterwards, and `/etc/cups` is a
tmpfs, so none of it can reach the card. `lpadmin -p NAME -E -v usb://…`
with no `-m` at all was measured against a real cupsd 2.4.7 configured from
`install.sh`'s own directives before being relied on; the image is asked for
the first time in CI.

**The forbidden-pattern list was re-read at the same time**, because it had
to be. `status=216`, `Failed with result`, `Scheduled restart job`, `Kernel
panic` and `Unable to mount root` were written for a console carrying kernel
output and PID 1's status lines and nothing else — once journald started,
every unit's stdout stayed in the journal. Now the console carries whatever
anything on the machine writes, *including this harness's own probe*, which
quotes `userconf-service`'s output into a check detail and dumps the unit's
journal at the end of every phase. Failing a release because a unit
**repeated** one of those phrases is row 2 of issue #14 in a new place. The
greps run over the lines the *system* wrote: journald prefixes a forwarded
line with `[   45.123456] python3[412]: `, the kernel's own output has the
timestamp and no speaker, and `systemd[1]` is kept by name because it is the
one speaker whose "Failed with result" is a verdict rather than a quotation.
A phrase found only in a forwarded line is reported as an `IMG-NOTE` instead,
so the scoping cannot swallow one in silence.

**What it costs.** uart0 was 46848 and 45302 bytes in run 20 with the journal
invisible; forwarding it will grow that, and the size is now a number worth
reading rather than ignoring — the sampler's byte column prints it every 30
seconds. There are five copies of each console in the work directory (the two
ports, the concatenation, the ANSI-stripped copy and the speaker-filtered
one), all of them matched by the `console*.log` glob the failure artifact
uploads, and all of them well inside the 16 MB budget at these sizes. The
early-stop grep still reads the whole file once per sample; if that ever
shows up in the sampler's timing, `tail -c` a recent window instead.

One more thing moved for the same reason and it is a guess rather than a
measurement: `single-kernel-entry` counts `Booting Linux on physical CPU`,
and journald imports `/dev/kmsg` from the start of the buffer. If it forwards
those entries to the console too, every early kernel line arrives twice and a
healthy boot is failed as a reboot loop. Whether it does has **not** been
measured here — there is no systemd on the machine this was written on — so
the count excludes the `kernel:` identifier journald would label such a copy
with. It costs nothing if the copy never comes, and it cannot hide a real
loop, whose second entry prints with no speaker in front of it.

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

### Run 72 (31983736617): three units that had been failing all along

The first run with the journal actually on the console failed, in both
boots, on exactly one check — `no-Failed-with-result` — and everything else
passed: 17/17 in each guest, `single-kernel-entry PASS`, the marker
forwarded, the panel detected, the sheet rendered and enqueued. Neither
hazard the change was written against had bitten. journald does **not**
re-forward the kmsg it imported (zero `kernel: ` lines, one
`Booting Linux on physical CPU`), and the speaker scoping worked exactly as
designed: every matched line was a genuine `systemd[1]:` verdict, and no
forwarded line from anything else tripped anything.

What it caught was three real unit failures that a non-forwarding console
had simply never carried. All three predate issue #21.

| Unit | Boots | Whose defect |
| --- | --- | --- |
| `systemd-growfs-root.service` | both | **Ours.** Fixed — see the mask in `install.sh`. |
| `rpi-eeprom-update.service` | both | **The emulator's.** Fixed — see the board revision in `img-boot.sh`. |
| `ssh.service` | boot 1 | **Ours.** Fixed — see the `ssh.socket` mask and the persisted machine-id in `install.sh`. |

**`rpi-eeprom-update.service`** dies on
`arithmetic expression: expecting ')': "(0x >> 23) & 1"`. It reads the board
revision from `/proc/device-tree/system/linux,revision`, then `/proc/cpuinfo`,
then `vcgencmd`; QEMU is not the Pi firmware and supplies none of them, so
`BOARD_INFO` is empty and `0x` is a syntax error. The same absence is
independently visible two lines away in the unit's own log — gpiozero:
`unable to locate Pi revision in /proc/device-tree or /proc/cpuinfo`. On real
hardware the revision exists and the script reaches `chipNotSupported()`,
which **exits 0**, so on every board `docs/HARDWARE.md` lists this unit
succeeds. It is the emulator that is wrong, not the image.

The repair is to synthesise `linux,revision` into the DTB the harness already
passes, the way it already synthesises the command line and the initramfs the
firmware would have supplied. `img-boot.sh` does that now, with `fdtput` into
a **copy** of the image's own DTB — so the evidence still shows what a device
would have booted — and it reads the property back, because an `fdtput` that
did nothing looks exactly like one that worked.

**It was measured before it was built on**, by booting a stock Raspberry Pi OS
Lite arm64 card under the same `-M raspi3b` with QEMU 8.2.2, twice, with an
`init=` probe in place of systemd:

| | without the property | with `0xa02082` |
| --- | --- | --- |
| `/proc/device-tree/system/linux,revision` | absent | `00a02082` |
| `/proc/cpuinfo` `Revision` | no such line | `a02082` |
| `rpi-eeprom-update` | rc 2, `(0x >> 23) & 1` | rc 0, "Skipping bootloader update." |
| `gpiozero` `Button(5)` | `BadPinFactory` | `CONSTRUCTED factory='LGPIOFactory'` |
| `/dev/i2c-*` | none | none |
| `/sys/class/drm` | empty | empty |

The revision is decoded field by field in the script: new-style flag set,
1 GB (which `-m 1024` agrees with), BCM2837, type `0x08` = 3 Model B. A code
for a board `-M raspi3b` does not model would be run 1's DTB mistake again.

**The `Button(5)` row is the one to read carefully, because the image's own
boot does not agree with it.** That row was measured with an `init=` probe on
a stock card. Run 32020772161's boot 1, which is this image running its own
code, says something else — `console-text.log:713-714`:

    no GPIO buttons ([Errno 22] Invalid argument)
    interface -- display: none, input: none

So the revision does change what happens — an `OSError` from the pin request
is not a `BadPinFactory`, so the factory did load — but claiming GPIO5 on
QEMU's gpiochip then fails `EINVAL`, `hmi.open_buttons()` falls through to
`(None, "none")`, and **the emulated unit has neither buttons nor a screen**.
Why the standalone probe and the booted image differ is not established here.
Where they disagree, the booted artifact is the evidence.

**`unit-detects-no-panel` gained a third clause anyway, and it was not for
the emulator's sake.** The two strings it used to be made of — `no OLED (`
and `printing unattended` — are both logged by a unit that found an *HDMI
console* and no buttons: `open_display` still reports the failed SSD1306
probe, and `Interface.interactive` still refuses because it wants a display
*and* buttons. A monitor plugged into a unit with nothing on the GPIO header
is a configuration `docs/HARDWARE.md` describes, and it would have passed a
check whose name says the unit detected no panel. The third clause,
`interface -- display: none,`, keys on the display half and closes that hole
on real hardware, whatever any emulator provides.
`tests/test_img_verdict.py` holds that needle against the real
`Interface.describe()` by running it — for `input: none` and for
`input: GPIO buttons`, so it cannot come to depend on which machine it saw —
and `test_breaking_the_panel_absence_detection_still_turns_the_check_red`
drives the shipped `hmi.detect` with the OLED probe made to succeed and
requires the check to go red on the journal that real code produces.

**`ssh.service`** is the one to look at first. `userconf-pi` ends a
successful seeded first boot in `/usr/bin/cancel-rename`, which finishes
with `systemctl --quiet reload ssh`. That reload kills sshd for good:

    systemd[1]: Reloading ssh.service - OpenBSD Secure Shell server...
    sshd[744]: Received SIGHUP; restarting.
    sshd[744]: fatal: Cannot bind any address.
    systemd[1]: ssh.service: Failed with result 'exit-code'.

`ssh.service` carries `RestartPreventExitStatus=255`, so nothing brings it
back. **The provisioning boot — the one boot an operator expects to SSH into
— silently ends with no SSH.**

The cause is that `ssh.socket` is also active and owns `[::]:22`: it is
listening at 87s, sshd starts at 131s and reports `Server listening on ::
port 22` without ever binding it, and on `SIGHUP` sshd closes the inherited
descriptor, re-execs, re-adopts `LISTEN_FDS`, finds nothing usable and dies
without attempting a bind — which is why no `Bind to port … failed` line
appears anywhere. Nothing in this repository enables `ssh.socket`; systemd's
first-boot `preset-all` does, and `/etc/machine-id` says `uninitialized`
while `/etc` is inside the overlay, so **every boot of this appliance is a
first boot**. Boot 2 shows it: `regenerate_ssh_host_keys.service` and
`sshd-keygen.service` run again, and `ssh.socket` comes up again. The host
keys change on every power cycle.

Both halves are fixed now, and the machine-id one is the larger of the two.

`ssh.socket` is **masked** in the image. Debian's `ssh.socket` is
`ListenStream=22`, `Accept=no`, with no `Service=`, so its implied service is
`ssh.service`: with both enabled the socket binds :22 and sshd runs on an
inherited descriptor it cannot get back after `execv`. With the socket
masked, sshd binds :22 itself and the SIGHUP re-exec rebinds. Masked rather
than disabled because `enable` symlinks live in `/etc/systemd/system`, which
is inside the overlay. Masking `ssh.service` instead was refused: the reload
is guarded by `systemctl --quiet is-active ssh` in `cancel-rename`, so it
would also stop the reload — while leaving `ssh.socket` listening on :22 for
a service that can never start.

**`/etc/machine-id` is persisted**, which is what stops every boot being a
first boot. It cannot be done by a unit: PID 1 reads that file before it
looks at anything, so the restore is in the initramfs — one file, 33 bytes,
copied out of `/boot/firmware/otp-identity/machine-id` on the FAT partition
into the overlay's upper layer, after the overlay is assembled and before
`run-init`. It uses `mount`, `mkdir`, `cat` and `umount`, all klibc, and no
module: `fat`, `vfat`, `nls_cp437` and `nls_ascii` are in `modules.builtin`
for every `-rpi-v8` kernel and `CONFIG_FAT_DEFAULT_IOCHARSET` is `"ascii"`.
It never panics — the overlay is boot-critical, an identity is not.

**That function was run in a real initramfs before it was trusted.** Sliced
out of `install.sh` unmodified, packed into a stock Raspberry Pi OS Lite arm64
initramfs as `/scripts/otptest` and booted under the same `-M raspi3b`, twice:
with a machine-id in `::otp-identity/machine-id` it printed
`OTP-INITRD rc=0 id=aabbccddeeff00112233445566778899` and userspace read that
back out of `/etc/machine-id` at mode 444; on an unprovisioned card — which is
every unit's first boot — it printed `OTP-INITRD rc=0 id=uninitialized`, left
the file exactly as the image ships it, and the boot carried on. The vfat
mount with no module, the `${ROOT%p[0-9]}p1` derivation and the write through
`${rootmnt}` are measurements, not arguments.

`otp-unit-identity.service` is the userspace half: it records the machine-id
the first boot generated. It refuses to run at all if the store turns out to
be on the same filesystem as `/` — otherwise a `/boot/firmware` that failed to
mount would give a store in the overlay's tmpfs, agreeing with itself
perfectly on every boot while nothing survived any of them.

**The SSH host keys are NOT persisted, and that is a decision rather than an
omission.** They were, in the first version of this: `regenerate_ssh_host_keys
.service` opens with `rm -f /etc/ssh/ssh_host_*_key*` on every first boot, so
the fingerprint of a machine that prints one-time pads changed every time
somebody switched it off. But `image/build.sh` sets `ENABLE_SSH=0`, so pi-gen
leaves `ssh.service` **disabled** and this appliance does not run `sshd` —
the only thing that ever started it was the first-boot `preset-all` that
persisting the machine-id ends. Run 32020772161 measures both halves of that:
boot 1 starts `ssh.service` at 130s, because a genuine first boot still runs
`preset-all`; boot 2 does not contain the string `ssh.service`, `ssh.socket`
or `OpenBSD` anywhere and still reaches `multi-user.target`. So the
persistence was buying a fingerprint nobody could be shown, at the price of
private keys on a partition every local account can read. It is gone, and so
is every ordering in the unit that existed for it.

Three named guest checks read the machine-id back off the machine:
`machine-id-persisted-outside-the-overlay` in both boots,
`machine-id-recorded-for-the-next-boot` in boot 1, and
`machine-id-identical-across-the-power-cycle` in boot 2 — with the boot-1 one
as the positive control, because two absences are identical and a machine
that lost its identity entirely would otherwise certify that it kept it.

**Why the last two exist, and it is not symmetry for its own sake.** The
first check compares the live id against the store — and
`otp-unit-identity.service` has already run by then, filling an empty store
from the live id and exiting 0. Verified by running the shipped script
against an empty store: `rc=0`, store silently populated, nothing said. So a
card whose store was truncated or deleted between boots is a card where every
boot is a first boot and the check reads PASS on every one of them. Boot 1
therefore records the id into `/boot/firmware/otp-imgcheck-machine-id`, a
file the **probe** owns, and boot 2 is held against that. The
`first-boot-complete.target` gate above is the third, independent answer.

**The cost, said out loud.** `/boot/firmware` is vfat mounted with
`defaults`, so everything on it is `0755 root:root` — readable by **every
account on the unit**, including the `otp` user and the `lp` uid CUPS runs
filters as, not only by someone holding the card. A machine-id is an
identifier rather than a secret; the release note and `install.sh`'s closing
summary both say so in those terms rather than the older "the same people who
can already read the root filesystem", which understated who that is.

**"and no password is written there" is no longer true.** The operator's
login is kept on that partition now — see the section below — and that is the
one thing there which is not an identifier. No pad byte is, and none ever may
be.

**Tightening the mount to `fmask=0077,dmask=0077`** was considered and not
done, and the reason recorded here was **wrong**: it said
`otp-unit.service` carries `ReadWritePaths=-/boot/firmware` so
`otpunit/config.py` can save settings there, and that a root-only partition
would break that. But that unit has no `User=` — it runs as **root**, and so
do all four units in `device/systemd`; `ReadWritePaths` is a mount-namespace
grant, not a uid drop, and root writes through `0700` vfat. No non-root writer
to that partition exists in this repository. The real costs are different
ones: `install.sh` would have to rewrite `/etc/fstab`, which it deliberately
does not; a wrong entry there costs the boot-partition mount and takes the
settings, the machine-id and the credential with it, on a machine with no
network to recover over; and `config.py`'s `mount -o remount,rw` fallback
would need checking, because vfat re-parses its options on remount. It is the
obvious next narrowing for whoever wants one.

### The operator's login, which used to last exactly one boot

`userconf-service` applies `/boot/firmware/userconf.txt` with `chpasswd -e`
into `/etc/shadow` and then **deletes the seed**. On a normal Pi that is
right; here `/etc` is inside the overlay and the FAT partition is not, so the
credential died with the power while the only file that could reapply it was
destroyed by the boot that consumed it. The account reverted to the random
`FIRST_USER_PASS` `image/build.sh` generates, which nobody has — on a device
that can be connected to a keyboard and a screen, where that login is the
recovery path. The owner's decision out of *refuse the seed loudly*, *persist
it* and *keep the seed file* is **persist it**.

Three named guest checks read it back off the machine:

| check | boot | what it says |
|---|---|---|
| `credential-recorded-outside-the-overlay` | 1 | the applied hash is in `/boot/firmware/otp-identity/credential`, under the account the seed named, on a filesystem that is not the root's |
| `credential-recorded-for-the-next-boot` | 1 | the positive control: a digest of the applied hash reached a **probe-owned** file and read back |
| `credential-survives-the-power-cycle` | 2 | the hash in force carries the seed's salt, digests to what boot 1 recorded, and there is no seed on the card it could have come from |

**Boot 1 is the control for the same reason it is on the machine-id side.**
`credential-recorded-outside-the-overlay` compares the store against
`/etc/shadow`, and both were written this boot — so it cannot tell "kept"
from "written a moment ago". The probe's own record is what boot 2 is held
against, and it predates boot 2 entirely.

**And one check owes nothing to the guest at all.** `credential-kept-on-the-card`
is a host-side `IMG-CHECK` in both boots: mtools lists the FAT partition
either side of every boot, and the file either is on the card or is not. A
guest that lied, a probe that never started, or a store written into the
overlay's tmpfs instead of onto the partition all produce nothing here. Both
ends are required in both boots — **absent** before boot 1 and present after
(the image ships no credential, so it appeared *because of* that boot), and
**present at both ends** of boot 2 (a power cycle did not take it, and
nothing wrote one on a boot with no seed).

That needed a change to the listing itself: `mdir -b` is **not recursive**.
Measured with mtools on a FAT image built by hand — a card holding
`otp-identity/{machine-id,credential}` lists as `::/kernel8.img` and
`::/otp-identity/` and nothing else, so the store's contents were invisible
to the host. `fat_listing` now makes a second call naming that directory and
appends it; on a card with no store yet, mdir answers non-zero, prints
nothing, and the listing is just the root.

**The hash never travels and never prints.** This console is uploaded as a CI
artifact, so what crosses the power cycle is a `sha256` of the hash, required
to be 64 characters on **both** sides: two empty strings compare equal, and a
missing `sha256sum` empties both at once, which is exactly how *the credential
survived* would come to mean *there was never a credential*. That was found by
the mutation gate rather than by review — the length clauses looked like
belt-and-braces until a row proved no test could tell if they went.

**What this costs is a password hash on a world-readable partition**, and it
is not narrower than the *keep the seed file* option it was chosen over: a
`userconf.txt` **is** `user:hash`, so both leave the same crypt string on the
same vfat at the same `0755`. What persisting buys is that `userconf-pi`'s
apply path — which ends in `cancel-rename` starting a getty on the front
panel's tty — runs once rather than every boot, that the store gets the
validation and the outside-the-overlay refusal, and that a new `userconf.txt`
still works. It is written **only** from the wizard's own `ExecStartPost`, so
the only hash that reaches a card is one the operator put on that card
themselves.

**A store may name an account this unit no longer has, and that has to end in
a working login rather than a refusal.** `/usr/lib/userconf-pi/userconf` takes
`getent passwd 1000` and *renames* that account whenever the seed names
another one, then sets the password on the new name — and `rename_user`
touches only `/etc/{passwd,shadow,group,gshadow,subuid,subgid,sudoers.d}` and
`/home`, all inside the overlay. So the rename dies with the power and the
store does not: a seed naming anything but `otp` left the next boot holding a
credential for an account that no longer existed. Refusing it took away the
only login on a unit with no network and no `sshd`.

The seed this tier plants is still `otp` — naming another one would take the
plain documented apply out of the only boot that observes it — so boot 2 runs
`credential-recovers-a-store-naming-another-account` as an experiment instead:
the store's *name* field is relabelled by hand, the live password is put one
byte off the kept one, and the shipped `persist-identity.sh` is run for real
against the real `chpasswd`, the real `/etc/shadow` and the real store. A
working restore rewrites the label back and re-applies the kept hash, so both
files end byte-for-byte as they were found and nothing needs undoing. No hash
is invented and none is printed: the "before" password is this unit's own,
one character changed, and only digests reach the console.

The counts move with it: **23 guest checks in boot 1 and 23 in boot 2**, up
from 19 and 19 — the credential work added two to each and the cloud-init
purge below two more. The run recorded below predates all of it.

### The provisioning agent that was on every image, and is not now

`image/build.sh` sets `ENABLE_CLOUD_INIT=0`, and reading that line it looks
as though the image has no cloud-init on it. It had. pi-gen's
`stage2/04-cloud-init` has two halves and the variable guards one:
`01-run.sh`, which preseeds a NoCloud datasource, checks it; the sibling
`00-packages`, which lists `cloud-init` and `rpi-cloud-init-mods`, is
collected and installed with no condition anywhere in that path. So the knob
means *do not preseed a datasource* and nothing more — issue #34 — and this
is how tier 3 ended up with a `TimeoutStartSec=infinity` networkd wait on a
NetworkManager machine, because `cloud-init.target` is what pulled
`network-online.target` into the transaction that ended both boots of run
31968966879 without either reaching `multi-user.target`. Nobody went looking
for a cloud-init problem, because the build config said cloud-init was off.

**The owner's decision out of *leave it*, *purge it* and *both* is both.**
`image/stage-otpunit/01-otpunit/00-run.sh` purges the two packages, and
`device/install.sh` goes on writing cloud-init's own kill switch
`/etc/cloud/cloud-init.disabled` — the belt to the purge's braces, so a
reintroduced dependency cannot re-arm it silently, and because the
documented *run this on a Pi you already have* path never meets a pi-gen
stage at all.

**The purge asks what it would take with it before it takes anything.**
`apt-get purge -y` removes reverse dependencies without a prompt, so the
stage runs `apt-get -s purge` first and refuses to build if the simulation
names anything but the two packages. Off the archives on 2026-08-18 there is
nothing to name: no package in the Raspberry Pi archive's `trixie/main`
declares any relationship on `rpi-cloud-init-mods`, and in Debian
`trixie/main` only `debian-cloud-images-packages` — which Raspberry Pi OS
Lite does not carry — *depends* on `cloud-init` (three packages *suggest* it
and one *breaks* it; none of the four can cause a removal). An index is not a
rootfs, which is why the refusal is in the build rather than in this
paragraph.

**And the simulation has to prove it ran.** Everything the refusal reads is
an absence — names apt did *not* print — so the first version of it passed
for an apt that printed nothing, an apt that exited 100, and an apt that
wrote its plan to stderr, running the real purge in all three cases. The
middle one is what `apt-get -s purge -y <name in no index>` does. The stage
now checks the simulation's exit status and requires the plan to name **both**
packages before its silence about everything else means anything.

The one file `rpi-cloud-init-mods` ships that is not cloud-init's own is
`/usr/lib/netplan/00-network-manager-all.yaml`, which sets netplan's renderer
to NetworkManager. It goes with the purge, and the argument that this is
harmless is in the comment beside the guest check rather than on the console:
the Raspberry Pi archive's `network-manager` (`1.52.1-1+rpt4`) **depends** on
`netplan.io`, so netplan itself cannot go with the purge; searching
`Contents-arm64` for both archives, that yaml is the **only** netplan config
file any package ships, so afterwards netplan has nothing to render and never
reaches `enable_networkd()`; and the `10-globally-managed-devices.conf` the
renderer default exists to shadow is shipped by no package in either archive
— it is an Ubuntu convention, so the override was a no-op here before the
purge as well. That file's presence and `NetworkManager.service`'s state are
**reported** in the check line, which is worth having on the console and is
not the evidence above: on a healthy unit that line reads
`netplan-renderer=no NetworkManager=active` every time, so by itself it
distinguishes nothing.

Two named guest checks, in both boots, and they are deliberately two:

| check | what it says |
|---|---|
| `cloud-init-is-not-installed` | `dpkg-query` has neither package (`config-files` fails too — a *remove* leaves the Raspberry Pi datasource config behind), systemd's `LoadState` is `not-found` for `cloud-init.target`, `cloud-init-local.service` and `cloud-config.service`, and `/usr/lib/systemd/system-generators/cloud-init-generator` is gone |
| `cloud-init-kill-switch-survives-the-purge` | `/etc/cloud/cloud-init.disabled` is a **regular file** on the booted machine |

**Every absence has a positive control beside it**, because a `dpkg-query`
that answers nothing, a `systemctl` that answers nothing and a generator
directory that is not there produce all of those absences at once, on a
machine with cloud-init running — and this probe runs on a machine whose
`/usr` the purge has just edited. So `systemd` must come back `installed`
through the same query, `otp-unit.service` must come back `loaded` through
the same `systemctl show`, and the generator directory must list its
siblings.

**Why two checks and not one.** After a purge nearly every piece of
cloud-init evidence is absent for a reason that has nothing to do with the
kill switch. One check over all of it would pass for the wrong reason and
would go on passing if `install.sh` stopped writing the marker tomorrow.
Split, the second goes red on exactly that machine while the first stays
green. It is also the check that would catch the ordering mistake: `dpkg`
sweeps the directories a package owned once they are empty, and `/etc/cloud`
is cloud-init's, so the stage purges **before** `install.sh` runs rather
than relying on dpkg declining to delete a directory that still holds a file
it does not own.

**What is still not measured, said plainly.** Neither check runs
cloud-init's own code — there is none left on the machine to run. The kill
switch is read by `/usr/lib/cloud-init/ds-identify`, not by the generator
directly: `is_disabled()` returns 0 on
`[ -f /etc/cloud/cloud-init.disabled ]` before any datasource search,
`_main` returns 2 at that, and the generator's `rc=2` branch is what leaves
`cloud-init.target` out of `multi-user.target`'s wants (cloud-init
`25.2-1~bpo13+1+rpt20`, the build the Raspberry Pi archive would put on this
image). That the **runtime** honours the marker is upstream's property,
quoted from upstream's source and tested by nothing in this repository, on
any tier. `-f` rather than `-e` is where that reading shows up in the check:
a directory of that name, or a symlink pointing at nothing, is not a kill
switch while satisfying every looser test.

### Run 32020772161: what stopping the first-boot loop uncovered

The first boot pair with all of the above in it. Boot 1 went **19/19** and
every host-side check passed. Boot 2 went **18/19**, and the single failure is
the interesting part of the run.

**All three of run 72's unit failures are gone**, in both boots: no
`Failed with result` reached the speaker-scoped console at all, so neither
`rpi-eeprom-update.service` nor `ssh.service` nor `systemd-growfs-root.service`
failed. `unit-detects-no-panel` passed in both boots, and the check keyed on
the display exactly as intended — **not** for the predicted reason: the
console says `no GPIO buttons ([Errno 22] Invalid argument)` and
`interface -- display: none, input: none`, so the emulated unit has neither
half. The prediction that a `Button` would construct here came from an
`init=` probe on a stock card and does not hold for the booted image; see the
`linux,revision` section above.
`machine-id-persisted-outside-the-overlay` passed in both boots.

The targets each boot reached carry the machine-id fix independently, without
reading a single check: boot 1 reached `first-boot-complete.target` and boot 2
**did not**. `regenerate_ssh_host_keys.service` pulls that target in and is
`ConditionFirstBoot=yes`, so its absence in boot 2 is systemd saying the second
boot was not a first boot — which had never been true of this image before.
That note is a **gate** now, in both directions: boot 1 must reach the target
(it is a first boot on every run — the harness gives each one a fresh
`xz -dc` of the image) and boot 2 must not.

What failed was:

```
OTP-CHECK boot2 userconf-unseeded-boot-skips-the-wizard FAIL
  condition=no checked-at='never' is-active=inactive is-enabled=disabled
```

`condition=no` and `is-active=inactive` are the wanted answers. The other two
are not, and both come from `is-enabled=disabled` — a unit systemd never
looked at has no condition timestamp either.

**Nothing disabled it on that boot.** `/etc` is inside the overlay, so boot 1's
`systemctl disable userconfig` — `cancel-rename`'s own second-to-last act —
died with the power. `disabled` is what the IMAGE says, and it always was.
What had been hiding it is the bug this branch fixed: every boot was a first
boot, systemd ran `preset-all` every time, and `preset-all` re-enabled the
unit every time. Stop the first-boot loop and the accident stops with it.

That is a real regression for an operator, not a stale expectation:
`userconfig.service` is the only consumer of `userconf.txt`, so a disabled
unit means a credential file written to the card is ignored in silence on
every boot after the first — precisely the trade `install.sh`'s comment
refuses to make by masking, arriving through a different door. So the fix is
`systemctl enable userconfig.service` in the image, which is what the
`ConditionPathExists` drop-in was written for in the first place: an unseeded
boot pays two condition evaluations and a skip. The check was right; the image
was wrong.

## When CI evidence runs out — the local repro rig

`./harness/img-local-rig.sh <probe>`. Not a tier and not a check: no CI
wiring, deliberately (issue #22). It is what you reach for when the tiers
above have told you everything they can and you still do not know why.

**The problem it solves is the round trip.** Tier 3 cannot run without
`image/deploy/*.img.xz`, and that artifact is the pi-gen arm64 job —
16m16s for the whole thing on run 16, of which 6m58s is pi-gen on a cache
miss and 7m59s is the pair of boots. So every hypothesis about the kernel,
the console, the watchdog or coldplug cost a quarter of an hour *and a
push*. Issue #17's fifteen-run narrative is largely a record of paying
that, several times for theories that turned out to be wrong — which is
exactly when the price is least affordable.

The rig removes the image from the loop. The kernel and the DTB come out of
the same `archive.raspberrypi.com` `.deb` pi-gen would install, resolved
through the `linux-image-rpi-v8` metapackage so it stays current by itself;
the root filesystem is a ~1MB busybox initramfs; the question is a shell
heredoc in the one file. **Measured on the container it was written in
(x86_64, QEMU 8.2.2, TCG): 24–27 seconds for the first `rng` run including
a cold 32,739,088-byte kernel download, then against the warm cache 20s for
`console-test`, 35s for `coldplug-replay` (which builds and `depmod`s a
module disk on its first run) and 67s for `idle-survive`, which is 45s of
deliberate waiting.** Against 16m16s.

| Probe | The question it was built to answer |
|---|---|
| `rng` | crng/entropy state, and a genuinely blocking read. Issue #17 spent two runs on an entropy stall that did not exist. |
| `coldplug-replay` | `modprobe` every `/sys` modalias one at a time with START/DONE markers, against the kernel's own module tree on an ext4 disk, `depmod`'d host-side. A module that hangs is *named* instead of theorised about. |
| `console-test` | Which consoles actually registered, what the DTB aliases say, and which port a write lands on. This is the one that ended six runs of misdiagnosed "freeze". |
| `idle-survive` | Sit still and see whether the machine resets. The watchdog re-verification. |

**The exit code means "the probe did its work", in three states, and it is
safe to script on.** `0` the probe reported and did its work; `2` the probe
reported and said its work did not happen — a module disk that would not
mount, a `/dev/random` that could not be read, a `/proc` that never
appeared, so read the `FAIL:` lines on uart0 before believing anything else
it printed; `1` no marker at all, meaning the guest hung, reset or never
reached `/init`, which is a different problem and stays a different code.
Until QA on PR #36 the code meant only "something printed a marker":
`./harness/img-local-rig.sh coldplug-replay && echo clean` printed *clean*
for a replay that mounted nothing.

**The worked example, which is why the rig is in the repository at all.**
Issue #17's boot "froze" at ~29.5–31.6s guest, always the moment journald
started, and three theories died against it: dwc_otg wedging coldplug, TCG
slowness, and an entropy stall. All three were argued from CI consoles. The
rig answered all three in an afternoon:

- **entropy** — `random: crng init done` at 2.4s, and a blocking read
  returning immediately. The claim that the line was "absent in every
  console" was an artifact of reading job-log *tails* that begin after 8s
  guest, past where it prints. Re-measured with the rig as committed, over
  three runs: crng init at **2.62s, 2.63s and 3.32s** — it moves run to run
  under TCG, so the load-bearing fact is that it happens early and always,
  not the digits — with `entropy_avail=256` (a full pool), hwrng
  `3f104000.rng` registered, and sixteen *blocking* bytes of `/dev/random`
  returning in 0.02s every time. **That last figure was unfalsifiable until
  QA on PR #36.** The probe threw away `dd`'s exit status and its stderr, so
  the "16 blocking bytes" line printed whatever happened: booting the
  verbatim probe with `/dev/random` deleted still reported `uptime 3.29 ->
  3.31` and the rig still exited 0. The status is read now and the run fails
  loudly, which is what makes the sentence above worth quoting.
- **coldplug** — all 41 modaliases probed, no hangs. Re-measured with the
  rig as committed: `modules.dep` 1911 lines, **41 modaliases replayed, 4
  returning non-zero, 19 modules loaded**, 41 START lines and 41 DONE lines.
- **the console** — and this was the actual fault. `/proc/consoles` lists
  `ttyAMA1` and nothing else; the device tree says `serial0 ->
  /soc/serial@7e215040`, which is the *mini-UART*, and `stdout-path` points
  at it; `serial1 -> /soc/serial@7e201000` is the PL011. So `console=ttyAMA0`
  named a device that does not exist, no real console ever registered, and
  the "wedge" was a blindfold. The rig prints that in twenty seconds.
- **the watchdog** — `idle-survive` climbs from uptime 4.57 to 49.70 with
  one kernel entry, well past the ~11.5s reset that runs 1–5 hit every time.

**Findings only transfer if the machine matches**, so the five flags that
decide what the guest *is* — `-M raspi3b`, `-m 1024`, `loglevel=7`,
`console=ttyAMA1`, `initcall_blacklist=bcm2835_pm_driver_init` — plus the
synthesised `0xa02082` board revision are held against `img-boot.sh` by
`tests/test_local_rig.py`, which parses both scripts. It fails if the rig
gains a kernel parameter tier 3 does not have, *and* if tier 3 gains one the
rig does not consciously omit. A rig that boots a subtly different machine is
worse than no rig: it produces confident answers about somewhere else.

**What it will not do.** It boots no image, so it says nothing about the
overlay, the unit, CUPS, the front panel or provisioning — all of that is
tier 3's business and needs the real artifact. It answers kernel-level and
early-userspace questions, which is the class that cost the most to ask
through CI.

**Host requirements**, documented rather than vendored: `qemu-system-aarch64`
with the `raspi3b` machine, plus `curl xz-utils gzip cpio dpkg
device-tree-compiler`, and for `coldplug-replay` only, `kmod` and
`e2fsprogs`. Each is checked before the emulator starts and named if absent
— because *every* one of these, missing, produces the same console: nothing
at all, or a stop at `Run /init as init process`. That is the single most
expensive symptom in this harness's history to misdiagnose.

**`--plan` needs none of them.** It prints the qemu argv and exits, so it
works on a machine with no emulator installed — which is the machine of the
person deciding whether to install one. It used to demand the whole list
before it would print, and that was the check in the wrong place: printing a
plan is string construction. (It was also six red tests on `ubuntu-latest`,
which has neither `qemu-system-aarch64` nor `fdtput`.) The only thing behind
the plan that wants anything is resolving the release name out of the
archive, which asks for `curl` and `gzip` at the point it uses them, and
`OTP_RIG_KERNEL=<release>` removes even that. A *run* is still refused up
front, before it fetches or unpacks anything.

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
python3 tests/mutation_gate.py --tier fast       # 219 rows, 240s
sudo python3 tests/mutation_gate.py --tier hardware   # 3 rows, 36s, needs cupsd
```

**Runtime decided the trigger.** The issue expected nightly or
label-triggered; measured, the fast tier is about four minutes at 219 rows
— still cheaper than the suite it audits — so it runs per pull
request as its own `mutation` job, and the ordinary suite's wall clock does
not move. The three CUPS-rig rows run in the existing `hardware` job, the
only place with a real `cupsd`, for 36 seconds on top of about eight minutes.
Those numbers are counted, not carried forward: an earlier revision of this
paragraph still said 38 rows and twenty-nine seconds long after both had
moved, and the revision after that said 114 rows for a round in which there
were 115, and it sat at 145 rows and 123s for two rounds after that. The row
count is exact and the seconds are not: three runs of the same 159 rows on
the same container measured 232s, 190s and 192s, and two runs of the 164
rows measured 196s and 198s.

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

**The rig round produced a third kind of survivor, and it was the test's
fault rather than the property's.** Both were fixed in
`tests/test_local_rig.py`, not by adding a second edit:

- the `grep -q`/SIGPIPE row survived because its fixture was inert. The fake
  `qemu -M help` built its 20,000-line listing with `$(seq 1 20000)`, `seq`
  is not on the stubbed `PATH` the rig runs with, the substitution expanded
  to nothing, and the "large producer" emitted two lines — so the pipe never
  filled, `grep -q` never closed it early, and the test was green with the
  mutation applied. Confirmed separately that the bug is real: the `grep -q`
  form returns **141** against a genuinely large producer and reports the
  machine absent. The loop is pure bash arithmetic now, with a test that
  asserts the fixture really emits more than 200,000 bytes.
- the versioned-kernel row survived because the negative fixture used
  `Depends: linux-headers-rpi-v8`, which the pattern rejects on the
  `linux-image-` prefix alone. It was testing the prefix and calling it the
  version check. The fixture is `linux-image-rpi-v8-current` now — a
  metapackage pointing at another metapackage, which is the rot that would
  actually happen — and the prefix case is kept as its own test.

Both are the same lesson one level in from the usual one: a mutation row
proves the *test* can fail, and a test can only fail as hard as its fixture
is real.

**What is not covered.** No row needs a booted guest, and the checks that
only exist *inside* one — tier 2's spool redirect above, tty1 ownership, the
persistence phases, and now tier 3's overlay probe — cannot be mutated from
here. What the overlay and identity rows attack is the *host-side gate* over what the
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

"""The tier-3 verdict logic, tested against synthetic consoles.

The real verdict block is sliced out of img-boot.sh, as test_vm_verdict.py
does for tier 2, so a copy cannot stay green while the original regresses.
Every console is built to fail one specific way, and the healthy one uses
the raw bytes of a real success line, ANSI escapes included.

Tier 3 boots the image three times. boot1 and boot2 are a power cycle of
the same card for the read-only overlay (issue #9), gated on what the guest
reported. The `release` phase boots with no otp.imgcheck token, as a
flashed unit does, and everything it claims is an absence, which a boot
that never happened also satisfies. So each absence is paired with a
presence proving the observation was possible, with one test per clause.
"""
import hashlib
import inspect
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
IMG_BOOT = REPO / "harness" / "img-boot.sh"
GUEST_CHECK = REPO / "harness" / "img-guest-check.sh"
IMGCHECK_UNIT = REPO / "device" / "systemd" / "otp-unit-imgcheck.service"
MARKER = "# Everything downstream reads the combination"


def shell_list(name: str) -> list:
    """A space-separated `NAME="a b c"` list, read out of img-boot.sh so the
    fixtures cannot keep a stale copy of the names."""
    match = re.search(rf'^{name}="(.*?)"$', IMG_BOOT.read_text(), re.S | re.M)
    assert match, f"{name} is gone from {IMG_BOOT}"
    return match.group(1).replace("\\\n", " ").split()


# The probe's two records, and the three files a release boot must leave
# byte-for-byte alone.
PROBE_DROPPINGS = shell_list("PROBE_DROPPINGS")
FAT_CONTENTS = shell_list("FAT_CONTENTS")

# The raw bytes of a real success line: color around the OK, color around the
# unit name, CR at the end.
OK_PREFIX = "[\x1b[0;32m  OK  \x1b[0m] "
SUCCESS = OK_PREFIX + "Started \x1b[0;1;39motp-unit.service\x1b[0m - OTP pad print unit.\r"


def verdict_block() -> str:
    """The real verdict logic, from the console concat to the end. Sliced, not
    duplicated: a copy would keep passing after the original changed."""
    text = IMG_BOOT.read_text()
    assert MARKER in text, f"{IMG_BOOT} no longer contains {MARKER!r}"
    return text[text.index(MARKER):]


def required_checks(phase: str) -> list:
    """The guest checks img-boot.sh insists on, read out of img-boot.sh so a
    healthy fixture is complete by construction."""
    text = IMG_BOOT.read_text()

    def names(var: str) -> list:
        match = re.search(rf'^{var}="(.*?)"$', text, re.S | re.M)
        assert match, f"{var} is gone from {IMG_BOOT}"
        return match.group(1).replace("\\\n", " ").split()

    found = names("GUEST_CHECKS_COMMON")
    if phase == "boot1":
        found += names("GUEST_CHECKS_BOOT1")
    if phase == "boot2":
        found += names("GUEST_CHECKS_BOOT2")
    return found


# The two entropy lines as the kernel spells them. bcm2835-rng is builtin, so
# both land within the first three seconds.
HWRNG = "[    2.383417] bcm2835-rng 3f104000.rng: hwrng registered"
CRNG = "[    2.421905] random: crng init done"


# The line that says the boot finished, as systemd 252 spells it. "Reached
# target" alone is satisfied by remote-fs.target.
#
# Stamped with `last_ts` so the fixture has exactly one latest timestamp:
# "guest reached" is a max over the concatenated consoles.
def multi_user_line(ts):
    return (f"[   {ts}] systemd[1]: Reached target multi-user.target "
            f"- Multi-User System.")


# The target PID 1 pulls in only when it decided this boot was a first boot.
FIRST_BOOT_TARGET = ("[   15.000000] systemd[1]: Reached target "
                     "first-boot-complete.target - First Boot Complete.")


# The kernel's own echo of its command line, which is where the verdict reads
# the otp.imgcheck token from; the harness's -append would only agree with
# itself.
#
# The base is img-boot.sh's -append with its two shell expansions resolved:
# boot=overlay from the image's cmdline.txt, and the phase token, which the
# release phase does not get.
CMDLINE_BASE = ("rw earlycon loglevel=7 console=ttyAMA1,115200 "
                "systemd.show_status=1 systemd.journald.forward_to_console=1 "
                "initcall_blacklist=bcm2835_pm_driver_init "
                "root=/dev/mmcblk0p2 rootfstype=ext4 rootwait boot=overlay")


def cmdline_line(phase, *, token=None):
    """`Kernel command line:` as the kernel prints it, for one phase.

    `token` overrides what follows the base, so a test can give the release
    phase the token it must not have, including `otp.imgcheck=`: systemd
    matches the left-hand side of an assignment, so an empty value still
    starts the unit.
    """
    if token is None:
        token = "" if phase == "release" else f"otp.imgcheck={phase}"
    tail = f" {token}" if token else ""
    return f"[    0.000000] Kernel command line: {CMDLINE_BASE}{tail}"


def kernel_lines(*, entries=1, last_ts="20.000000", first_boot=False,
                 phase="boot1", cmdline=True, multi_user=True):
    lines = []
    for _ in range(entries):
        lines.append("[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x410fd034]")
    if cmdline:
        lines.append(cmdline_line(phase))
    lines += [
        "[    0.100000] Linux version 6.12.96+rpt-rpi-v8 (build@host)",
        # Filtered back out by the two tests that need them missing.
        HWRNG,
        CRNG,
        "[    9.180895] systemd[1]: Hostname set to <otp-unit>.",
        f"[   {last_ts}] systemd[1]: Reached target sysinit.target - System Initialization.",
    ]
    if multi_user:
        lines.append(multi_user_line(last_ts))
    # boot1 really is a first boot (each run starts from a fresh image, so
    # /etc/machine-id is `uninitialized`); boot2 must not be one.
    if first_boot:
        lines.append(FIRST_BOOT_TARGET)
    return lines


def guest_report(phase, *, checks=None, failing=(), done=True, counts=None):
    """What otp-unit-imgcheck.service prints, in the real line format, with
    the trailing CR a serial console produces."""
    names = required_checks(phase) if checks is None else list(checks)
    lines = [f"OTP-GUEST starting phase={phase} on Linux 6.12.96+rpt-rpi-v8 aarch64\r"]
    passed = 0
    for name in names:
        state = "FAIL" if name in failing else "PASS"
        passed += state == "PASS"
        # The CUPS queue's name travels in this one check's detail. The
        # release phase's guest-probe-cups-queue-unnamed clause uses boot1's
        # console as the control that its grep can find the name.
        detail = "detail=whatever"
        if name == "diagnostic-sheet-reaches-cups":
            detail = "lpadmin rc=0 | queue=device for otpimgcheck: usb://OTP/imgcheck"
        lines.append(f"OTP-CHECK {phase} {name} {state} {detail}\r")
    if done:
        lines.append(f"OTP-GUEST-DONE {phase}\r")
    got, total = counts if counts else (passed, len(names))
    lines.append(f"OTP-RESULT {phase} {got}/{total}\r")
    return lines


# A journald-forwarded line: monotonic timestamp, syslog identifier, pid,
# text. The probe writes this marker only via `systemd-cat -t otp-imgcheck`,
# never on stdout, so it is on the console only if the journal reached it
# (issue #21).
def journal_forwarded(phase, ident="otp-imgcheck", pid=912, ts="45.123456"):
    return f"[   {ts}] {ident}[{pid}]: OTP-JOURNAL-FORWARDED {phase}\r"


def forwarded(text, *, ident="python3", pid=412, ts="46.000000"):
    """Any other line journald carried to the console for somebody else."""
    return f"[   {ts}] {ident}[{pid}]: {text}\r"


# systemd saying it looked at the probe unit and did not start it. This is
# the v257 wording (src/core/job.c), where the unit is named as "<id> -
# <description>".
#
# A condition skip is never printed as a `[ INFO ]` status line, but PID 1
# falls back to /dev/kmsg when journald is down, so the line reaching the
# console does not show forwarding works; journal-forwarding-alive does that.
SKIP_WORDING = ("was skipped because of an unmet condition check "
                "(ConditionKernelCommandLine=otp.imgcheck).")

# The reworded message systemd prints after v258, which is what the image
# really emits; see
# test_the_wording_a_real_image_actually_printed_is_accepted.
SKIP_WORDING_MAIN = ("skipped, unmet condition check "
                     "ConditionKernelCommandLine=otp.imgcheck")


def imgcheck_description() -> str:
    """The probe unit's Description=, read out of the shipped unit file. Its
    length matters to the harness's `cut`, so the fixture must not keep a
    copy."""
    match = re.search(r"^Description=(.*)$", IMGCHECK_UNIT.read_text(), re.M)
    assert match, f"{IMGCHECK_UNIT} no longer has a Description="
    return match.group(1).strip()


def condition_skipped(unit="otp-unit-imgcheck.service",
                      description=None,
                      wording=SKIP_WORDING, ts="18.400000"):
    if description is None:
        description = imgcheck_description()
    return f"[   {ts}] systemd[1]: {unit} - {description} {wording}\r"


# The probe's other fingerprints, as the release phase greps for them: the
# journal tag in speaker position and the name of its CUPS queue. Both appear
# in boot1, which gives their absence in the release phase meaning.
def probe_journal_tag(phase="boot1"):
    return journal_forwarded(phase)


def release_console(*, skip_line=True, forwarding=True, first_boot=False,
                    probe_said=(), **kw):
    """A healthy release boot: the machine came up and the probe stayed inert.

    No OTP-GUEST-, no journal marker under the probe's tag, no mention of
    its queue, plus two positives: the unit started, and systemd named the
    probe unit while skipping it.

    `forwarding` controls whether the console carries any
    journald-forwarded line (timestamped, from a speaker that is not PID
    1). Without one the silence is not backed by a working channel.
    """
    lines = kernel_lines(phase="release", first_boot=first_boot, **kw) + [SUCCESS]
    if skip_line:
        lines.append(condition_skipped())
    if forwarding:
        lines.append(forwarded("interface -- display: none, input: none"))
    return lines + list(probe_said)


def healthy(phase):
    if phase == "release":
        return release_console()
    return (kernel_lines(phase=phase, first_boot=(phase == "boot1"))
            + [SUCCESS] + [journal_forwarded(phase)]
            + guest_report(phase))


# What `mdir -b` prints for the FAT partition: absolute paths, one per line.
# kernel8.img is the verdict's positive control that the listing is a listing
# at all.
FAT_ROOT = ["::/kernel8.img", "::/bcm2710-rpi-3-b.dtb", "::/config.txt",
            "::/cmdline.txt", "::/initramfs8", "::/otp-unit.conf"]


# The store, one level down. `mdir -b` is not recursive (a directory shows as
# `::/otp-identity/` alone), so the harness's second `mdir` call produces
# these two lines.
FAT_STORE = ["::/otp-identity/machine-id", "::/otp-identity/credential"]


# The probe's two records in `::/name` form, named off img-boot.sh's
# PROBE_DROPPINGS. boot1 writes them, boot2 leaves them, and the release
# phase deletes them before booting and requires them not to come back.
FAT_DROPPINGS = [f"::/{name}" for name in PROBE_DROPPINGS]


def healthy_listings(phase):
    """The boot partition either side of a healthy phase.

    boot1 goes in with the harness's seed and comes out without it, holding
    the probe's two records and the credential, which the image does not
    ship and only the wizard's ExecStartPost writes. boot2 has the
    credential at both ends and comes out also holding the quarantined
    malformed seed. The release phase's before-listing is boot2's
    after-listing minus the two records the harness stripped, and it comes
    out the same.
    """
    if phase == "boot1":
        return (FAT_ROOT + ["::/userconf.txt", "::/otp-identity/machine-id"],
                FAT_ROOT + FAT_STORE + FAT_DROPPINGS)
    if phase == "boot2":
        return (FAT_ROOT + FAT_STORE + FAT_DROPPINGS,
                FAT_ROOT + FAT_STORE + FAT_DROPPINGS + ["::/failed_userconf.txt"])
    if phase == "release":
        stripped = FAT_ROOT + FAT_STORE + ["::/failed_userconf.txt"]
        return list(stripped), list(stripped)
    return list(FAT_ROOT), list(FAT_ROOT)


# --- the card functions, driven against a real FAT partition ---------------
#
# fat_listing, fat_digest_of, fat_digests and strip_probe_droppings sit above
# the marker verdict_block() slices from, so the verdict tests only ever see
# their output as text. Here mformat and mcopy build a real FAT partition at
# a real offset, the shipped functions run against it, and the digests are
# compared with sha256 taken in Python.
#
# These tests need mtools and skip without it. ci.yml installs it in the
# `test` and `mutation` jobs, test_image_workflow.py holds those installs in
# place, and tests/mutation_gate.py refuses a row whose named tests skipped.

MTOOLS = [tool for tool in ("mformat", "mcopy", "mdir", "mtype", "mdel")
          if shutil.which(tool) is None]
needs_mtools = pytest.mark.skipif(
    bool(MTOOLS), reason=f"mtools missing: {' '.join(MTOOLS)}")

#: Where a real card's FAT partition starts. Non-zero because the harness
#: addresses the partition as `$IMG@@$BOOT_OFFSET`.
CARD_OFFSET = 1024 * 1024
CARD_FAT_BYTES = 16 * 1024 * 1024


def card_block() -> str:
    """The card functions, sliced out of img-boot.sh from fat_listing() to the
    end of strip_probe_droppings(), which brings the shipped
    $PROBE_DROPPINGS and $FAT_CONTENTS lists with it."""
    text = IMG_BOOT.read_text()
    start = text.index("fat_listing() {")
    end = text.index("\n# root=/dev/mmcblk0p2 rather than", start)
    block = text[start:end]
    for name in ("fat_digest_of() {", "fat_digests() {",
                 "strip_probe_droppings() {", 'PROBE_DROPPINGS="',
                 'FAT_CONTENTS="'):
        assert name in block, f"{name} is no longer inside the card slice"
    return block


def build_card(path: Path, files: dict) -> Path:
    """A FAT partition at CARD_OFFSET inside `path`, holding `files`: a map
    from a path relative to the FAT root to its exact bytes. Directories
    are made as needed."""
    with open(path, "wb") as handle:
        handle.truncate(CARD_OFFSET + CARD_FAT_BYTES)
    image = f"{path}@@{CARD_OFFSET}"

    def mtool(*args):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)

    mtool("mformat", "-i", image, "-F", "-T", str(CARD_FAT_BYTES // 512), "::")
    made = set()
    for name, data in files.items():
        if "/" in name:
            directory = name.rsplit("/", 1)[0]
            if directory not in made:
                mtool("mmd", "-i", image, f"::/{directory}")
                made.add(directory)
        scratch = path.parent / ("_" + name.replace("/", "_"))
        scratch.write_bytes(data)
        mtool("mcopy", "-o", "-i", image, str(scratch), f"::{name}")
    return path


def run_card_block(tmp_path, card: Path, body: str):
    """Run the shipped card functions against `card`, then `body`."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    script = "\n".join([
        "set -euo pipefail",
        f'WORK="{work}"',
        f'IMG="{card}"',
        f'BOOT_OFFSET="{CARD_OFFSET}"',
        card_block(),
        body,
    ])
    proc = subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def digest_lines(text: str) -> dict:
    """`name digest` lines, as fat_digests writes them."""
    out = {}
    for line in text.splitlines():
        if line.strip():
            name, value = line.split(None, 1)
            out[name] = value.strip()
    return out


def prestrip_listing(phase):
    """What the card held before the harness deleted the probe's records:
    boot2's after-listing. It is the control showing the files were there
    to delete."""
    if phase == "release":
        return healthy_listings("boot2")[1]
    return healthy_listings(phase)[0]


def digest_of(name):
    """A stable 64-character stand-in for one file's contents, distinct per
    name. 64 hex characters because the harness requires that length on
    both sides, so that two empty digests cannot compare equal."""
    return hashlib.sha256(name.encode()).hexdigest()


def healthy_digests(phase):
    """The three content digests either side of a phase, unchanged."""
    kept = {name: digest_of(name) for name in FAT_CONTENTS}
    return dict(kept), dict(kept)


def run_verdict(tmp_path, consoles, *, phases=("boot1",), uart1_lines=None,
                qemu_rc="124", early_stop="", boot_files=None,
                prestrip=None, digests=None):
    """Run the sliced block over synthetic per-phase evidence directories.

    `consoles` maps a phase name to its uart0 lines. The fixture writes
    what a real boot leaves in a phase directory: both consoles, qemu's
    exit code, whether the harness stopped it, the FAT listings either side
    of the boot and before the release strip, and the content digests.
    """
    if uart1_lines is None:
        # What the real second UART holds: early bootconsole lines that stop
        # at the ~2s handoff. Their small timestamps come last in the concat.
        uart1_lines = ["[    1.900000] printk: legacy bootconsole [bcm2835aux0] disabled"]
    work = tmp_path
    for phase in phases:
        pdir = work / phase
        pdir.mkdir(parents=True, exist_ok=True)
        lines = consoles.get(phase, [])
        (pdir / "console.log").write_text("\n".join(lines) + ("\n" if lines else ""))
        (pdir / "console-uart1.log").write_text(
            "\n".join(uart1_lines) + ("\n" if uart1_lines else ""))
        (pdir / "qemu-rc").write_text(qemu_rc + "\n")
        (pdir / "early-stop").write_text(early_stop)
        before, after = (boot_files or {}).get(phase, healthy_listings(phase))
        (pdir / "boot-files-before.txt").write_text(
            "".join(f"{line}\n" for line in before))
        (pdir / "boot-files-after.txt").write_text(
            "".join(f"{line}\n" for line in after))
        (pdir / "boot-files-before-strip.txt").write_text(
            "".join(f"{line}\n"
                    for line in (prestrip or {}).get(phase,
                                                     prestrip_listing(phase))))
        dbefore, dafter = (digests or {}).get(phase, healthy_digests(phase))
        for which, values in (("before", dbefore), ("after", dafter)):
            (pdir / f"boot-digests-{which}.txt").write_text(
                "".join(f"{name} {value}\n" for name, value in values.items()))
    script = "\n".join(
        [
            "set -euo pipefail",
            "log() { printf 'LOG %s\\n' \"$*\" >&2; }",
            "ESC=$(printf '\\033')",
            f'WORK="{work}"',
            f'PHASES="{" ".join(phases)}"',
            'TIMEOUT="600"',
            # The two lists the verdict block reads, re-emitted from what
            # shell_list() read out of img-boot.sh.
            f'PROBE_DROPPINGS="{" ".join(PROBE_DROPPINGS)}"',
            f'FAT_CONTENTS="{" ".join(FAT_CONTENTS)}"',
            verdict_block(),
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    verdict = work / "verdict.txt"
    return proc, (verdict.read_text() if verdict.exists() else None)


# --- which boots the run is allowed to consist of -------------------------

PHASES_ASSIGNMENT = 'PHASES="${OTP_IMG_PHASES:-boot1 boot2 release}"'


#: The first thing in img-boot.sh after the phase guards. Ending the slice at
#: the first function definition takes in every guard, however many there are.
PHASE_GUARD_END = "\nlog() { printf"


def phase_list_block() -> str:
    """The phase list and the guards on it, sliced out of img-boot.sh. It sits
    above the verdict block, so it is a separate slice."""
    text = IMG_BOOT.read_text()
    assert PHASES_ASSIGNMENT in text, f"{IMG_BOOT} no longer sets PHASES"
    start = text.index(PHASES_ASSIGNMENT)
    assert PHASE_GUARD_END in text[start:], (
        f"{IMG_BOOT} no longer defines log() after the phase guards, so this "
        f"slice does not know where they end")
    end = start + text[start:].index(PHASE_GUARD_END)
    return text[start:end]


def run_phase_list(tmp_path, phases=None):
    runner = tmp_path / "phases.sh"
    runner.write_text("set -euo pipefail\n" + phase_list_block()
                      + '\nprintf "PHASES=%s\\n" "$PHASES"\n')
    env = {k: v for k, v in os.environ.items() if k != "OTP_IMG_PHASES"}
    if phases is not None:
        env["OTP_IMG_PHASES"] = phases
    return subprocess.run(["bash", str(runner)], capture_output=True,
                          text=True, timeout=30, env=env)


def demanded_phases() -> list:
    """The phases img-boot.sh refuses to run without, read off its own guard
    loop so the default assignment and the guard are held to agree."""
    match = re.search(r"^for want in ([a-z0-9 ]+); do$",
                      phase_list_block(), re.M)
    assert match, "the phase guard no longer loops over the phases it demands"
    return match.group(1).split()


def test_the_default_runs_every_phase_the_guard_demands(tmp_path):
    # The default is the whole tier.
    proc = run_phase_list(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"PHASES={' '.join(demanded_phases())}" in proc.stdout, proc.stdout
    # And the list is the three boots this tier is built from.
    assert demanded_phases() == ["boot1", "boot2", "release"], demanded_phases()


def test_an_empty_phase_list_falls_back_to_the_default(tmp_path):
    # `:-`, not `-`: OTP_IMG_PHASES="" would otherwise boot nothing and have
    # no phase to fail on.
    proc = run_phase_list(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert f"PHASES={' '.join(demanded_phases())}" in proc.stdout, proc.stdout


def phases_refused(proc) -> list:
    """The phases the guard named as missing, off its first line."""
    head = proc.stderr.splitlines()[0]
    assert "leaves out:" in head, proc.stderr
    return head.split("leaves out:")[1].split()


def test_a_phase_list_without_boot2_is_refused(tmp_path):
    """A phase list without boot2 is refused. OTP_IMG_PHASES drives the boot
    loop, the verdict loop and the required checks, so dropping boot2 would
    drop the power-cycle checks while the conclusion still claimed two
    boots."""
    for phases in ("boot1 release", "boot1 boot3 release", "noboot2 boot1 release",
                   "boot22 boot1 release"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "boot2" in phases_refused(proc), proc.stderr
        assert "PHASES=" not in proc.stdout, proc.stdout


def test_a_phase_list_without_boot1_is_refused_too(tmp_path):
    """A phase list without boot1 is refused. The seed is copied onto the card
    unconditionally, so boot2 alone would run its unseeded-card checks
    against a seeded card and go red for a reason unrelated to the image."""
    for phases in ("boot2 release", "boot2 boot3 release", "boot2 boot2 release"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "boot1" in phases_refused(proc), proc.stderr
        assert "PHASES=" not in proc.stdout, proc.stdout
    # And the reason is stated: the card is seeded whatever was asked for.
    proc = run_phase_list(tmp_path, "boot2 release")
    assert "seeded" in proc.stderr, proc.stderr


def test_a_phase_list_without_the_release_boot_is_refused(tmp_path):
    """A phase list without the release boot is refused. That phase asserts a
    silence, so a run without it is fully green for an image nobody asked
    the question of; the guard is what makes dropping it cost anything."""
    for phases in ("boot1 boot2", "boot1 boot2 boot3", "boot1 boot2 releases"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "release" in phases_refused(proc), proc.stderr
        assert "PHASES=" not in proc.stdout, proc.stdout
    # And the reason names what a run without it fails to observe.
    proc = run_phase_list(tmp_path, "boot1 boot2")
    assert "otp.imgcheck" in proc.stderr, proc.stderr
    assert "inert" in proc.stderr, proc.stderr


def test_a_phase_list_that_keeps_every_boot_is_accepted(tmp_path):
    # Positive control: the guard does not refuse everything. The second list
    # shows the order check applies to the three demanded boots only; an
    # unknown extra phase passes through.
    for phases in ("boot1 boot2 release", "boot1 boot2 release boot3"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode == 0, proc.stderr
        assert f"PHASES={phases}" in proc.stdout, proc.stdout


def test_a_phase_list_in_the_wrong_order_is_refused(tmp_path):
    """A phase list in the wrong order is refused, and so is a repeated phase.

    The boots are the same card in sequence: `boot1 release boot2` strips
    the probe's records before the boot that reads them, and `boot2 boot1
    release` hands a seeded card to checks expecting an unseeded one. Every
    mis-ordering ends red anyway; the guard saves an arm64 run that would
    blame the image for a fault in an environment variable.
    """
    for phases in ("boot1 release boot2", "boot2 boot1 release",
                   "release boot2 boot1", "boot2 release boot1"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "PHASES=" not in proc.stdout, proc.stdout
        assert "boot1 boot2 release" in proc.stderr, proc.stderr
        # The message says it is the order, not a missing boot.
        assert "leaves out" not in proc.stderr, proc.stderr
    # A demanded phase repeated is refused too: the seeded card would be
    # booted as boot1 twice.
    proc = run_phase_list(tmp_path, "boot1 boot1 boot2 release")
    assert proc.returncode != 0, proc.stderr


def test_the_order_the_guard_demands_is_the_order_it_checks_for(tmp_path):
    """The loop that names the demanded phases also builds the expected order,
    so the set and the sequence cannot drift apart."""
    block = phase_list_block()
    assert "PHASES_EXPECTED=" in block, block
    # Built inside the `for want in ...` loop, not written out beside it.
    loop = block[block.index("for want in "):]
    assert 'PHASES_EXPECTED="${PHASES_EXPECTED:+$PHASES_EXPECTED }$want"' \
        in loop[:loop.index("\ndone")], loop
    # And what it ends up holding is exactly what the guard demands.
    proc = run_phase_list(tmp_path, "boot1 release boot2")
    assert " ".join(demanded_phases()) in proc.stderr, proc.stderr


def test_the_full_claim_is_only_made_by_a_run_that_booted_every_phase(tmp_path):
    # The concluding line is what a reader takes away, and image.yml's
    # release note says the same thing.
    proc, _ = run_verdict(tmp_path, {"boot1": healthy("boot1")},
                          phases=("boot1",))
    assert proc.returncode == 0, proc.stderr
    assert "boots twice" not in proc.stderr, proc.stderr
    assert "NOT the three-boot claim" in proc.stderr, proc.stderr


def test_a_two_phase_run_does_not_claim_the_probe_stayed_inert(tmp_path):
    """A two-phase run's conclusion still claims the overlay and the power
    cycle but not the release phase's claim that the probe stayed inert,
    which no boot in that run observed."""
    proc, _ = run_verdict(
        tmp_path, {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"))
    assert proc.returncode == 0, proc.stderr
    assert "NOT the three-boot claim" in proc.stderr, proc.stderr
    assert "release" in proc.stderr.split("which needs:")[1], proc.stderr
    assert "inert" not in proc.stderr, proc.stderr


def test_a_three_phase_run_makes_the_whole_claim(tmp_path):
    proc, _ = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"), "boot2": healthy("boot2"),
         "release": healthy("release")},
        phases=("boot1", "boot2", "release"))
    assert proc.returncode == 0, proc.stderr + str(_)
    assert "the image boots twice on a read-only overlay" in proc.stderr, \
        proc.stderr
    # And the third boot's own sentence names what it observed.
    for clause in ("no otp.imgcheck token", "inert", "skipped",
                   "stayed off the card"):
        assert clause in proc.stderr, (clause, proc.stderr)


def test_fixture_success_line_really_carries_escape_codes():
    # The healthy fixture must keep ANSI between "Started" and the
    # description, or the ANSI-strip coverage disappears.
    assert "\x1b" in SUCCESS
    assert "Started OTP pad print unit" not in SUCCESS
    between = SUCCESS.split("Started ")[1]
    assert between.startswith("\x1b"), "unit name must be color-wrapped"


def test_the_harness_and_the_guest_agree_on_which_checks_exist():
    """The checks img-boot.sh requires are exactly the checks
    img-guest-check.sh emits. A name the guest no longer emits fails every
    run; a check the harness does not require can be deleted with nothing
    going red."""
    emitted = set(re.findall(r"^\s*check\s+([a-z0-9-]+)[\s\\]",
                             GUEST_CHECK.read_text(), re.M))
    assert emitted, "no check calls found in img-guest-check.sh"
    # The union over the phases: only boot1 has a seed to consume, and only
    # boot2 can speak about what a power cycle kept.
    demanded = set(required_checks("boot1")) | set(required_checks("boot2"))
    assert emitted == demanded, (
        f"img-guest-check.sh emits {sorted(emitted)}, img-boot.sh requires "
        f"{sorted(demanded)}")
    # And no name is demanded of a phase that cannot emit it.
    common = set(required_checks("boot1")) & set(required_checks("boot2"))
    for phase in ("boot1", "boot2"):
        only = set(required_checks(phase)) - common
        assert only, f"{phase} demands nothing of its own"


def test_the_probe_looks_for_the_words_the_unit_actually_logs():
    """The two journal strings the headless check greps for are the ones
    otpunit actually logs. They are written in one file and matched in
    another, and a reworded line would fail every tier-3 run with an
    unhelpful message."""
    probe = GUEST_CHECK.read_text()
    for phrase, source in (
        ("no OLED (", REPO / "otpunit" / "hmi.py"),
        ("no usable interface; printing unattended",
         REPO / "otpunit" / "__main__.py"),
    ):
        assert f'*"{phrase}"*' in probe, \
            f"the probe no longer looks for {phrase!r}"
        assert phrase in source.read_text(), \
            f"{source.name} no longer logs {phrase!r}, so the probe's " \
            f"unit-detects-no-panel check matches nothing"


def test_the_probe_looks_for_the_display_the_unit_actually_reports():
    """The probe's `interface -- display: none,` needle matches what the unit
    really reports.

    Neither file contains that string (__main__ has the prefix and an
    f-string, hmi the field names), so the real describe() is run. It must
    match for both input kinds: a real Pi with nothing wired reports
    `input: GPIO buttons`, the emulated Pi `input: none`, so the needle
    stops before the input field.
    """
    import sys
    sys.path.insert(0, str(REPO))
    from otpunit import hmi

    needle = "interface -- display: none,"
    assert f'*"{needle}"*' in GUEST_CHECK.read_text(), \
        f"the probe no longer looks for {needle!r}"
    # Both machines that reach the headless decision: the emulated Pi, whose
    # pin claim fails EINVAL, and a real Pi, where gpiozero succeeds.
    for input_kind, buttons in (("none", None), ("GPIO buttons", object())):
        described = hmi.Interface(display=None, buttons=buttons,
                                  display_kind="none",
                                  input_kind=input_kind).describe()
        assert needle == f"interface -- {described.split(' input:')[0]}", \
            f"Interface.describe() now says {described!r}, so the probe's " \
            f"unit-detects-no-panel check matches nothing"
    # And the line __main__ wraps it in. Read as text, because importing
    # __main__ runs it.
    assert 'log(f"interface -- {interface.describe()}")' in \
        (REPO / "otpunit" / "__main__.py").read_text(), \
        "otpunit/__main__ no longer logs the interface it detected"


# --- the seed itself, which three files have to agree about ---------------

BUILD_SH = REPO / "image" / "build.sh"


def shell_value(path: Path, name: str) -> str:
    """A plain `NAME=value` assignment, read out of a shipped script."""
    match = re.search(rf"^[ \t]*{name}=(?:'([^']*)'|\"([^\"]*)\"|(\S+))[ \t]*$",
                      path.read_text(), re.M)
    assert match, f"{name} is gone from {path}"
    return next(g for g in match.groups() if g is not None)


def test_the_seed_names_the_account_the_image_actually_ships(tmp_path):
    """The harness's seed names the account image/build.sh ships as
    FIRST_USER_NAME.

    userconf-pi renames the UID-1000 user when a seed names a different one
    (usermod -l, home move, subuid/subgid, sudoers), a much larger
    experiment than applying credentials. The state a rename leaves is
    covered separately: see
    credential-recovers-a-store-naming-another-account.
    """
    first_user = shell_value(BUILD_SH, "FIRST_USER_NAME")
    assert shell_value(IMG_BOOT, "USERCONF_USER") == first_user
    assert shell_value(GUEST_CHECK, "USERCONF_USER") == first_user
    assert "credential-recovers-a-store-naming-another-account" \
        in GUEST_CHECK.read_text(), \
        "nothing in tier 3 exercises the rename branch this seed avoids"


def test_the_guest_looks_for_the_salt_the_harness_actually_planted():
    # The guest never sees the hash, so it compares the salt marker. A hash
    # regenerated with a different salt would leave it looking for a string
    # nothing writes.
    salt = shell_value(GUEST_CHECK, "USERCONF_SALT")
    assert salt.startswith("$6$") and salt.endswith("$"), salt
    assert shell_value(IMG_BOOT, "USERCONF_HASH").startswith(salt)


def userconf_pi_rejects(line: str) -> bool:
    """userconf-pi's own validation, transcribed from userconf-service.

        NEW_USER="$(echo "$LINE" | cut -f1 -d:)"
        NEW_PASS="$(echo "$LINE" | cut -f2 -d:)"
        ... [ -z "$NEW_USER" ] || [ ${#NEW_USER} -gt 32 ]        -> invalid
        ... grep -q '^[a-z]\\{1\\}[a-z0-9\\-]*$'                   -> required
        ... [ "$NEW_USER" = "root" ]                             -> invalid
        ... [ -z "$NEW_PASS" ]                                   -> invalid

    A seed it rejects is renamed failed_userconf.txt; a seed it accepts is
    applied and deleted. Which of the two the harness plants is the whole
    difference between the two experiments this tier now runs.
    """
    fields = line.split(":")
    user, password = fields[0], (fields[1] if len(fields) > 1 else "")
    return not (user and len(user) <= 32
                and re.fullmatch(r"[a-z][a-z0-9-]*", user)
                and user != "root" and password)


def test_the_planted_seed_is_one_userconf_pi_accepts():
    user = shell_value(IMG_BOOT, "USERCONF_USER")
    seed = f"{user}:{shell_value(IMG_BOOT, 'USERCONF_HASH')}"
    assert not userconf_pi_rejects(seed), seed
    # And it is one line: userconf-service reads `head -n1` and nothing else.
    assert "\n" not in seed


def test_the_malformed_fixture_is_one_userconf_pi_rejects():
    """The "malformed" fixture really is rejected by userconf-pi. A valid one
    would be applied and deleted, leaving no failed_userconf.txt and never
    exercising the fail-fast path."""
    bad = shell_value(GUEST_CHECK, "UC_BAD")
    assert userconf_pi_rejects(bad), bad
    # Both validators: the username has spaces and capitals, and the password
    # half is empty.
    assert userconf_pi_rejects(bad.split(":")[0] + ":something")
    assert userconf_pi_rejects("validname:")


def test_the_emulated_boot_asks_for_the_journal_on_its_console():
    """img-boot.sh's -append asks for the journal on the console (issue #21),
    and the image's own cmdline.txt does not.

    -append replaces the kernel command line under emulation only; a
    flashed unit must not narrate its journal on the serial header.
    Asserted here because otherwise only a CI boot would notice the token
    missing.
    """
    text = IMG_BOOT.read_text()
    append = next(line for line in text.splitlines()
                  if line.strip().startswith("-append "))
    assert "systemd.journald.forward_to_console=1" in append, append
    for shipped in (REPO / "device" / "install.sh",
                    REPO / "image" / "build.sh"):
        assert "forward_to_console" not in shipped.read_text(), \
            f"{shipped.name} puts journal forwarding on the DEVICE"


def test_the_before_listing_is_taken_before_the_boot():
    """boot-files-before.txt is snapshotted before qemu runs. Taken afterwards
    it would not show that the seed reached the card."""
    text = IMG_BOOT.read_text()
    before = text.index('fat_listing "$dir/boot-files-before.txt"')
    launch = text.index("qemu-system-aarch64 \\")
    after = text.index('fat_listing "$dir/boot-files-after.txt"')
    assert before < launch < after, (before, launch, after)


def test_the_seed_is_planted_before_the_first_boot():
    # The mcopy must precede the boot loop. Matched on the copy, not the
    # name, because a log line says ::userconf.txt too.
    text = IMG_BOOT.read_text()
    plant = re.search(r"^mcopy[^\n]*::userconf\.txt", text, re.M)
    assert plant, "nothing copies a seed into the boot partition"
    assert plant.start() < text.index('for phase in $PHASES; do')


def test_a_healthy_two_boot_run_passes(tmp_path):
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"),
    )
    assert verdict is not None
    for phase in ("boot1", "boot2"):
        assert f"IMG-CHECK {phase} unit-started PASS" in verdict, verdict
        assert f"IMG-CHECK {phase} guest-reported-once PASS" in verdict, verdict
        assert f"IMG-CHECK {phase} guest-finished PASS" in verdict, verdict
        assert f"IMG-CHECK {phase} guest-ran-every-named-check PASS" in verdict, verdict
    # rc 124 is the normal ending of a healthy appliance boot (the guest
    # idles until the timeout), so it must not fail the run.
    assert proc.returncode == 0, proc.stderr + verdict


def test_a_healthy_run_reports_the_credential_file_too(tmp_path):
    # The four host-side lines the seeded path adds, on the healthy fixture.
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"),
    )
    for line in ("IMG-CHECK boot1 boot-partition-listed PASS",
                 "IMG-CHECK boot1 userconf-seed-planted PASS",
                 "IMG-CHECK boot1 userconf-seed-consumed PASS",
                 "IMG-CHECK boot1 credential-kept-on-the-card PASS",
                 "IMG-CHECK boot2 credential-kept-on-the-card PASS",
                 "IMG-CHECK boot2 userconf-malformed-seed-quarantined PASS"):
        assert line in verdict, verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_a_credential_that_never_reached_the_card_fails(tmp_path):
    """A boot1 whose after-listing holds no credential fails, even though
    every guest check passed: the listing is the one vantage point that
    owes nothing to the guest."""
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before,
                              [x for x in after if "credential" not in x])})
    assert "IMG-CHECK boot1 credential-kept-on-the-card FAIL" in verdict, verdict
    assert proc.returncode == 1
    # The seed checks still pass here: applied-and-consumed is also true of
    # an image that kept nothing.
    assert "IMG-CHECK boot1 userconf-seed-consumed PASS" in verdict, verdict


def test_a_credential_the_image_shipped_is_not_one_this_boot_kept(tmp_path):
    """A credential already on the card before boot1 fails: it shows the image
    shipped one (the same hash on every unit), not that this boot kept one."""
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before + ["::/otp-identity/credential"], after)})
    assert "IMG-CHECK boot1 credential-kept-on-the-card FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_credential_that_did_not_outlive_the_power_cycle_fails(tmp_path):
    """The credential must be on the card before boot2. One that appears only
    afterwards was written by a boot with no seed."""
    before, after = healthy_listings("boot2")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"),
        boot_files={"boot1": healthy_listings("boot1"),
                    "boot2": ([x for x in before if "credential" not in x],
                              after)})
    assert "IMG-CHECK boot2 credential-kept-on-the-card FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_readme_counts_the_checks_this_tier_actually_requires():
    """The number of guest checks harness/README.md states for each boot
    equals the count read from img-boot.sh, so the summary cannot go stale."""
    readme = (REPO / "harness" / "README.md").read_text()
    counts = {"boot 1": len(required_checks("boot1")),
              "boot 2": len(required_checks("boot2"))}
    match = re.search(
        r"\*\*(\d+) guest checks in boot 1 and (\d+) in boot 2\*\*", readme)
    assert match, \
        "harness/README.md no longer states how many guest checks a boot owes"
    assert (int(match.group(1)), int(match.group(2))) \
        == (counts["boot 1"], counts["boot 2"]), (
            f"harness/README.md says {match.group(1)}/{match.group(2)} guest "
            f"checks, img-boot.sh requires {counts['boot 1']}/{counts['boot 2']}")


def test_the_host_reads_the_store_a_level_below_the_root(tmp_path):
    """The harness lists the store directory with a second `mdir`. `mdir -b`
    is not recursive, so without it the credential one level down never
    appears and the check could not pass on any card."""
    text = IMG_BOOT.read_text()
    assert re.search(r"^\s*mdir -b -i \"\$IMG@@\$BOOT_OFFSET\" ::/otp-identity",
                     text, re.M), \
        "the host lists only the FAT root, so nothing in the store is visible"
    # Appended, not overwriting the root listing it follows.
    assert ">> \"$1\"" in text, text


# --- the card functions, on a card ----------------------------------------
#
# The shipped functions, run against a FAT partition built here with mformat
# and mcopy.

#: A card in the state boot2 leaves behind: the operator's three files, the
#: probe's two records, and the kernel image the listing check uses as its
#: control. The contents all differ and end in a newline, the byte a digest
#: taken through command substitution would drop.
CARD_AFTER_BOOT2 = {
    "kernel8.img": b"\x7fELF not really\n",
    "otp-unit.conf": b"pages=137\n",
    "otp-imgcheck-machine-id": b"0123456789abcdef0123456789abcdef\n",
    "otp-imgcheck-credential": b"a" * 64 + b"\n",
    "otp-identity/machine-id": b"0123456789abcdef0123456789abcdef\n",
    "otp-identity/credential": b"$6$otpimgcheck$xPbFgUo86\n",
}


@needs_mtools
def test_the_listing_the_harness_takes_reaches_into_the_store(tmp_path):
    """Runs the harness's listing on a real card with a store on it, where
    test_the_host_reads_the_store_a_level_below_the_root only reads the
    command out of the file."""
    card = build_card(tmp_path / "card.img", CARD_AFTER_BOOT2)
    run_card_block(tmp_path, card,
                   'fat_listing "$WORK/listing.txt"')
    listing = (tmp_path / "work" / "listing.txt").read_text().split()
    assert "::/kernel8.img" in listing, listing
    # The two lines the second mdir exists to produce.
    assert "::/otp-identity/machine-id" in listing, listing
    assert "::/otp-identity/credential" in listing, listing


@needs_mtools
def test_the_digest_is_the_bytes_on_the_card(tmp_path):
    """fat_digest_of's digest equals sha256 of the exact bytes on the card,
    computed here in Python. A digest taken through command substitution
    loses trailing newlines and gives two different files one digest."""
    card = build_card(tmp_path / "card.img", CARD_AFTER_BOOT2)
    run_card_block(tmp_path, card, 'fat_digests "$WORK/digests.txt"')
    got = digest_lines((tmp_path / "work" / "digests.txt").read_text())
    assert sorted(got) == sorted(FAT_CONTENTS), got
    for name in FAT_CONTENTS:
        want = hashlib.sha256(CARD_AFTER_BOOT2[name]).hexdigest()
        assert got[name] == want, (
            f"{name}: the harness digested {got[name]}, sha256 of the bytes "
            f"on the card is {want}")


@needs_mtools
def test_two_files_that_differ_only_in_trailing_newlines_differ(tmp_path):
    """Two files that differ only in trailing newlines get different digests.
    """
    same = dict(CARD_AFTER_BOOT2)
    card_a = build_card(tmp_path / "a.img", same)
    grown = dict(same, **{"otp-unit.conf": same["otp-unit.conf"] + b"\n\n"})
    card_b = build_card(tmp_path / "b.img", grown)
    digests = []
    for index, card in enumerate((card_a, card_b)):
        run_card_block(tmp_path, card, f'fat_digests "$WORK/d{index}.txt"')
        digests.append(digest_lines(
            (tmp_path / "work" / f"d{index}.txt").read_text())["otp-unit.conf"])
    assert digests[0] != digests[1], (
        f"'pages=137\\n' and 'pages=137\\n\\n\\n' both digest to {digests[0]}, "
        f"so a boot that appended to the operator's settings would read as "
        f"unchanged")
    assert len(digests[0]) == 64 and len(digests[1]) == 64, digests


@needs_mtools
def test_a_file_that_is_there_and_empty_is_not_reported_absent(tmp_path):
    """A present but empty file is not reported `absent`. `absent` replaces
    the hash of nothing so that a missing credential cannot compare equal
    to another missing one; an empty file gets a different word, and
    neither is a 64-character digest, so both stay red."""
    card = build_card(tmp_path / "card.img",
                      dict(CARD_AFTER_BOOT2,
                           **{"otp-identity/credential": b""}))
    run_card_block(tmp_path, card, 'fat_digests "$WORK/digests.txt"')
    got = digest_lines((tmp_path / "work" / "digests.txt").read_text())
    assert got["otp-identity/credential"] == "empty", got
    # And the file that really is not there says the other word.
    gone = {k: v for k, v in CARD_AFTER_BOOT2.items()
            if k != "otp-identity/credential"}
    card2 = build_card(tmp_path / "card2.img", gone)
    run_card_block(tmp_path, card2, 'fat_digests "$WORK/d2.txt"')
    got2 = digest_lines((tmp_path / "work" / "d2.txt").read_text())
    assert got2["otp-identity/credential"] == "absent", got2
    # Neither is a digest, which is what keeps the clause red for both.
    assert len(got["otp-identity/credential"]) != 64
    assert len(got2["otp-identity/credential"]) != 64


@needs_mtools
def test_the_strip_takes_the_probes_records_off_a_real_card(tmp_path):
    """strip_probe_droppings, run on a real card: the control listing is taken
    before the deletion, and the deletion really happens."""
    card = build_card(tmp_path / "card.img", CARD_AFTER_BOOT2)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    run_card_block(tmp_path, card,
                   'strip_probe_droppings "$WORK"\n'
                   'fat_listing "$WORK/boot-files-after-strip.txt"')
    control = (work / "boot-files-before-strip.txt").read_text().split()
    after = (work / "boot-files-after-strip.txt").read_text().split()
    for name in PROBE_DROPPINGS:
        assert f"::/{name}" in control, (
            f"the control listing does not carry {name}, so it is not "
            f"evidence the strip had anything to delete: {control}")
        assert f"::/{name}" not in after, (
            f"{name} survived strip_probe_droppings: {after}")
    # And it took nothing else: the operator's files are what a release boot
    # is judged on keeping.
    for name in FAT_CONTENTS:
        assert f"::/{name}" in after, (name, after)
    assert "::/kernel8.img" in after, after


@needs_mtools
def test_the_digest_file_answers_for_every_name_the_clause_reads(tmp_path):
    """fat_digests writes a line for every name in $FAT_CONTENTS, which is
    what identity-store-unchanged looks up. A missing line would read as an
    empty digest and fail with no explanation."""
    card = build_card(tmp_path / "card.img", CARD_AFTER_BOOT2)
    run_card_block(tmp_path, card, 'fat_digests "$WORK/digests.txt"')
    got = digest_lines((tmp_path / "work" / "digests.txt").read_text())
    assert list(got) == FAT_CONTENTS, (
        f"fat_digests wrote {list(got)} for a list of {FAT_CONTENTS}")


def test_a_seed_that_never_reached_the_card_fails(tmp_path):
    """Positive control for the seed checks: a seed that never reached the
    card fails. mcopy against a wrong partition offset writes nothing
    silently, and a seed never written is gone after the boot just as a
    consumed one is."""
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": ([x for x in before if "userconf" not in x], after)})
    assert "IMG-CHECK boot1 userconf-seed-planted FAIL" in verdict, verdict
    # The consumption check still says PASS on this evidence: absence is not
    # consumption.
    assert "IMG-CHECK boot1 userconf-seed-consumed PASS" in verdict, verdict
    assert proc.returncode == 1


def test_a_seed_still_sitting_on_the_card_after_the_first_boot_fails(tmp_path):
    # The seeded branch not running at all: the condition never matched, the
    # unit was masked, or the wizard is still waiting.
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before, after + ["::/userconf.txt"])})
    assert "IMG-CHECK boot1 userconf-seed-consumed FAIL" in verdict, verdict
    assert "userconf.txt" in verdict
    assert proc.returncode == 1


def test_a_seed_the_image_rejected_fails_the_first_boot(tmp_path):
    """A seed renamed to failed_userconf.txt fails boot1: userconf.txt is gone
    either way, but the operator got no credentials."""
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before, after + ["::/failed_userconf.txt"])})
    assert "IMG-CHECK boot1 userconf-seed-consumed FAIL" in verdict, verdict
    assert "failed_userconf.txt" in verdict
    assert proc.returncode == 1


def test_a_malformed_seed_that_left_no_evidence_fails_boot2(tmp_path):
    # Nothing quarantined the malformed seed, or the experiment never ran;
    # either way there is no failed_userconf.txt for the operator to read.
    before, after = healthy_listings("boot2")
    proc, verdict = run_verdict(
        tmp_path, {"boot2": healthy("boot2")}, phases=("boot2",),
        boot_files={"boot2": (before, [x for x in after if "failed_" not in x])})
    assert "IMG-CHECK boot2 userconf-malformed-seed-quarantined FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_bad_seed_left_where_the_next_wizard_finds_it_fails_boot2(tmp_path):
    # Quarantined means moved: a userconf.txt still holding the bad line
    # prompts on every boot.
    before, after = healthy_listings("boot2")
    proc, verdict = run_verdict(
        tmp_path, {"boot2": healthy("boot2")}, phases=("boot2",),
        boot_files={"boot2": (before, after + ["::/userconf.txt"])})
    assert "IMG-CHECK boot2 userconf-malformed-seed-quarantined FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_boot_partition_nothing_could_read_fails_rather_than_passing(tmp_path):
    """A boot partition nothing could read fails. An empty listing has no
    userconf.txt and would read as consumed; kernel8.img, which the harness
    itself pulled off that partition, is the control that it is a listing."""
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"),
        boot_files={"boot1": ([], []), "boot2": ([], [])})
    assert "IMG-CHECK boot1 boot-partition-listed FAIL" in verdict, verdict
    assert "IMG-CHECK boot2 boot-partition-listed FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_second_boot_must_report_too(tmp_path):
    # The power-cycle half of issue #9 lives in boot2, and a boot2 that never
    # happened leaves an empty console, so silence has to fail.
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"), "boot2": kernel_lines() + [SUCCESS]},
        phases=("boot1", "boot2"),
    )
    assert "IMG-CHECK boot1 guest-reported-once PASS" in verdict
    assert "IMG-CHECK boot2 guest-reported-once FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_guest_fail_line_fails_the_phase(tmp_path):
    lines = kernel_lines() + [SUCCESS] + guest_report(
        "boot2", failing=("root-writes-discarded-by-the-power-cycle",))
    proc, verdict = run_verdict(tmp_path, {"boot2": lines}, phases=("boot2",))
    assert "IMG-CHECK boot2 guest-no-fail-lines FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_dropped_guest_check_fails_even_with_no_fail_lines(tmp_path):
    """A guest check missing from the report fails the run even with zero FAIL
    lines, which a guest that checked nothing also has (issue #14)."""
    kept = [n for n in required_checks("boot2")
            if n != "root-writes-discarded-by-the-power-cycle"]
    lines = kernel_lines() + [SUCCESS] + guest_report("boot2", checks=kept)
    proc, verdict = run_verdict(tmp_path, {"boot2": lines}, phases=("boot2",))
    assert "IMG-CHECK boot2 guest-no-fail-lines PASS" in verdict, verdict
    assert "IMG-CHECK boot2 guest-checks-all-passed PASS" in verdict, verdict
    assert "guest-ran-every-named-check FAIL" in verdict, verdict
    assert "root-writes-discarded-by-the-power-cycle" in verdict
    assert proc.returncode == 1


def test_a_dropped_boot1_check_fails_too(tmp_path):
    """The same gate for boot1's own list: its seeded-credential checks could
    be dropped with every other boot1 check still passing."""
    kept = [n for n in required_checks("boot1") if n != "userconf-seed-applied"]
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1", checks=kept)
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 guest-no-fail-lines PASS" in verdict, verdict
    assert "guest-ran-every-named-check FAIL" in verdict, verdict
    assert "userconf-seed-applied" in verdict
    assert proc.returncode == 1


def test_a_phase_that_reported_twice_fails(tmp_path):
    # A guest that reported twice rebooted underneath the harness.
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1") + guest_report("boot1")
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 guest-reported-once FAIL 2" in verdict, verdict
    assert proc.returncode == 1


def test_a_phase_that_started_but_never_finished_fails(tmp_path):
    # Counts with no OTP-GUEST-DONE are a guest killed part way through.
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1", done=False)
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 guest-finished FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_counts_that_disagree_fail(tmp_path):
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1", counts=(8, 9))
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 guest-checks-all-passed FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_boot_with_no_hardware_rng_fails(tmp_path):
    """A boot where bcm2835-rng never registered fails: the pool then has no
    TRNG behind it, and nothing downstream complains because getrandom() is
    satisfied once seeded by anything."""
    lines = [ln for ln in kernel_lines() if "hwrng registered" not in ln]
    proc, verdict = run_verdict(
        tmp_path, {"boot1": lines + [SUCCESS] + guest_report("boot1")})
    assert "IMG-CHECK boot1 hwrng-registered FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_hardware_rng_line_is_quoted_back_with_its_driver(tmp_path):
    """The hwrng line is quoted into a note as well as gated, with enough
    leading context to keep the driver name, so a kernel bump that swaps
    the driver is visible."""
    plain = [ln for ln in kernel_lines(first_boot=True)
             if "hwrng" not in ln.lower()]
    real = "[    2.401337] bcm2835-rng 3f104000.rng: hwrng registered"
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": plain + [real, SUCCESS, journal_forwarded("boot1")]
                  + guest_report("boot1")})
    assert "IMG-CHECK boot1 hwrng-registered PASS" in verdict, verdict
    assert "bcm2835-rng" in verdict, \
        "the note clipped the driver name off, which is the only thing " \
        "it adds over the gate: " + verdict
    assert proc.returncode == 0, proc.stderr


def test_a_boot_that_never_finished_fails(tmp_path):
    """A boot that never reached multi-user.target fails. `Reached target`
    alone is satisfied by early targets such as remote-fs.target, while a
    boot can stall behind systemd-networkd-wait-online and never finish."""
    lines = [ln for ln in kernel_lines() if "multi-user.target" not in ln]
    still_assembling = [
        "[   30.5] systemd[1]: Reached target remote-fs.target - Remote File Systems.",
        "[  157.0] systemd[1]: Job systemd-networkd-wait-online.service/start "
        "running (2min 37s / no limit)"]
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": lines + still_assembling + [SUCCESS] + guest_report("boot1")})
    # The weaker clause is still satisfied.
    assert "IMG-CHECK boot1 Reached-target PASS" in verdict, verdict
    assert "IMG-CHECK boot1 Reached-target-multi-user.target FAIL" in verdict, \
        verdict
    assert proc.returncode == 1


def test_a_boot_that_did_finish_passes(tmp_path):
    # Positive control: the gate is keyed to a string a healthy boot prints.
    proc, verdict = run_verdict(tmp_path, {"boot1": healthy("boot1")})
    assert "IMG-CHECK boot1 Reached-target-multi-user.target PASS" in verdict, \
        verdict
    assert proc.returncode == 0, proc.stderr


def test_every_target_reached_is_named_not_just_counted(tmp_path):
    """The targets-reached note names each target once on one line, so a red
    gate also shows how far the boot got."""
    reached = ["[   30.5] systemd[1]: Reached target remote-fs.target - Remote File Systems.",
               "[  180.2] systemd[1]: Reached target multi-user.target - Multi-User System.",
               "[  180.3] systemd[1]: Reached target multi-user.target - Multi-User System."]
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": kernel_lines(first_boot=True) + reached
                  + [SUCCESS, journal_forwarded("boot1")]
                  + guest_report("boot1")})
    note = next(ln for ln in verdict.splitlines() if "targets-reached" in ln)
    assert "multi-user.target" in note, note
    assert "remote-fs.target" in note, note
    # One line, and each target once: this is a note somebody reads.
    assert note.count("multi-user.target") == 1, note
    assert proc.returncode == 0, proc.stderr


# --- systemd's own answer to "was this a first boot" ----------------------
#
# first-boot-complete.target is pulled in only when PID 1 read
# /etc/machine-id and decided this was a first boot: evidence for machine-id
# persistence that depends on no guest check and no file on the card.


def test_a_second_boot_that_was_a_first_boot_all_over_again_fails(tmp_path):
    """A boot2 that reached first-boot-complete.target fails. Every guest
    check can stay green on a unit whose machine-id reverted, because
    otp-unit-identity.service refills the store before the checks run."""
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"),
         "boot2": kernel_lines(first_boot=True) + [SUCCESS]
                  + [journal_forwarded("boot2")] + guest_report("boot2")},
        phases=("boot1", "boot2"))
    assert "IMG-CHECK boot2 second-boot-is-not-a-first-boot FAIL" in verdict, \
        verdict
    assert proc.returncode == 1


def test_a_second_boot_that_was_not_a_first_boot_passes(tmp_path):
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"))
    assert "IMG-CHECK boot2 second-boot-is-not-a-first-boot PASS" in verdict, \
        verdict
    assert proc.returncode == 0, proc.stderr


def test_a_first_boot_that_never_completed_one_fails(tmp_path):
    """Positive control for the clause above: boot1 is a first boot on every
    run (each starts from a fresh image), so the same grep must find the
    target there, or boot2's absence means nothing."""
    proc, verdict = run_verdict(tmp_path, {"boot1": kernel_lines() + [SUCCESS]
                                           + [journal_forwarded("boot1")]
                                           + guest_report("boot1")})
    assert "IMG-CHECK boot1 first-boot-really-was-a-first-boot FAIL" in verdict, \
        verdict
    assert proc.returncode == 1


def test_a_first_boot_that_did_complete_one_passes(tmp_path):
    proc, verdict = run_verdict(tmp_path, {"boot1": healthy("boot1")})
    assert "IMG-CHECK boot1 first-boot-really-was-a-first-boot PASS" in verdict, \
        verdict
    assert proc.returncode == 0, proc.stderr


def test_neither_first_boot_clause_is_asked_of_the_other_phase(tmp_path):
    """Each first-boot clause is asked only of its own phase; a phase-blind
    pair would fail every run."""
    _, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"))
    assert "boot2 first-boot-really-was-a-first-boot" not in verdict, verdict
    assert "boot1 second-boot-is-not-a-first-boot" not in verdict, verdict


def test_a_boot_whose_crng_never_seeds_fails(tmp_path):
    # Without this line the unit has started but its first draw blocks inside
    # getrandom().
    lines = [ln for ln in kernel_lines() if "crng init done" not in ln]
    proc, verdict = run_verdict(
        tmp_path, {"boot1": lines + [SUCCESS] + guest_report("boot1")})
    assert "IMG-CHECK boot1 crng-init-done FAIL" in verdict
    assert proc.returncode == 1


def test_guest_reached_is_the_max_not_the_concats_last_line(tmp_path):
    # uart1 (small timestamps) concatenates second: the last timestamp in
    # file order is 1.9s, the max is 20s.
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": kernel_lines(last_ts="20.000000") + [SUCCESS] + guest_report("boot1")})
    assert "guest reached: 20.000000s" in verdict


def test_hostname_and_login_prompt_do_not_count_as_the_unit(tmp_path):
    lines = kernel_lines() + ["otp-unit login: "]
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 unit-started FAIL" in verdict
    assert proc.returncode == 1


def test_starting_line_is_not_started(tmp_path):
    lines = kernel_lines() + [
        "         Starting \x1b[0;1;39motp-unit.service\x1b[0m - OTP pad print unit...\r"
    ]
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 unit-started FAIL" in verdict
    assert proc.returncode == 1


def test_the_etc_cups_unit_is_not_the_unit(tmp_path):
    lines = kernel_lines() + [
        OK_PREFIX + "Finished \x1b[0;1;39motp-unit-etc-cups.service\x1b[0m - tmpfs over /etc/cups.\r",
        "Started otp-unit-etc-cups.service - tmpfs over /etc/cups.",
    ]
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 unit-started FAIL" in verdict
    assert proc.returncode == 1


def test_a_boot_reset_loop_fails_even_with_a_successful_lap(tmp_path):
    lines = kernel_lines(entries=2) + [SUCCESS] + guest_report("boot1")
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 single-kernel-entry FAIL" in verdict
    assert proc.returncode == 1


def test_kernel_panic_fails_a_boot_that_also_started_the_unit(tmp_path):
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1") + [
        "[   25.000000] Kernel panic - not syncing: Attempted to kill init!"]
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 no-Kernel-panic FAIL" in verdict
    assert proc.returncode == 1


# --- the journal on the console, and what else it brought with it ---------


def test_a_console_the_journal_never_reached_fails(tmp_path):
    """A console without the probe's journal marker fails (issue #21). The
    marker is written only into the journal, so its absence is what
    distinguishes forward_to_console being ignored from a unit with nothing
    to say."""
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1")
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 journal-forwarded-to-console FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_marker_has_to_name_the_phase_that_is_being_judged(tmp_path):
    """The marker must name the phase being judged. Both boots share one work
    directory, and a phase-blind grep would let boot1's marker answer for
    boot2."""
    lines = (kernel_lines() + [SUCCESS, journal_forwarded("boot2")]
             + guest_report("boot1"))
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 journal-forwarded-to-console FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_unit_quoting_a_systemd_phrase_does_not_fail_the_release(tmp_path):
    """A forbidden phrase that only a unit quoted does not fail the release
    (issue #21): the probe quotes other units' output, and journald
    forwards all of it to the console. It is reported as a note, not
    dropped."""
    quoted = forwarded(
        "Aug 16 22:05:01 otp-unit systemd[1]: Failed with result 'exit-code'.",
        ident="img-guest-check.sh", pid=900)
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [quoted]})
    assert "no-Failed-with-result FAIL" not in verdict, verdict
    assert "IMG-NOTE boot1 quoted-Failed-with-result:" in verdict, verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_systemds_own_verdict_on_a_unit_still_fails_the_release(tmp_path):
    """Positive control for the scoping above: the same phrase from PID 1
    still fails the release."""
    real = "[   50.000000] systemd[1]: otp-unit.service: Failed with result 'exit-code'."
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [real]})
    assert "IMG-CHECK boot1 no-Failed-with-result FAIL" in verdict, verdict
    assert proc.returncode == 1


def forbidden_phrases() -> list:
    """The phrase list, read out of img-boot.sh rather than restated here."""
    text = IMG_BOOT.read_text()
    match = re.search(r"for bad in (.*?); do", text, re.S)
    assert match, "the forbidden-phrase loop is gone from " + str(IMG_BOOT)
    return re.findall(r'"([^"]+)"', match.group(1).replace("\\\n", " "))


def check_name(phrase: str) -> str:
    """The name the loop derives from a phrase: `tr ' =' '--'`."""
    return "no-" + phrase.replace(" ", "-").replace("=", "-")


def test_every_forbidden_phrase_is_one_the_scoping_can_actually_enforce(tmp_path):
    """Every forbidden phrase fails the phase when PID 1 says it.

    system_lines() keeps kernel and PID 1 output and drops other forwarded
    lines, so only a phrase PID 1 or the kernel utters can be gated. A
    phrase that only a unit prints (`sshd[744]: fatal: ...`) would be a
    gate that can never fire, and fails here on the day it is added.
    """
    phrases = forbidden_phrases()
    assert len(phrases) >= 5, phrases
    for phrase in phrases:
        spoken = f"[   50.000000] systemd[1]: otp-unit.service: {phrase} here."
        proc, verdict = run_verdict(
            tmp_path / check_name(phrase),
            {"boot1": healthy("boot1") + [spoken]})
        assert f"IMG-CHECK boot1 {check_name(phrase)} FAIL" in verdict, verdict
        assert proc.returncode == 1, verdict


def test_no_forbidden_phrase_is_gated_when_only_a_unit_said_it(tmp_path):
    """No forbidden phrase is gated when only a unit said it, checked over the
    whole list; it must be a note."""
    for phrase in forbidden_phrases():
        quoted = forwarded(f"Aug 16 22:05:01 otp-unit systemd[1]: {phrase} here.",
                           ident="img-guest-check.sh", pid=900)
        proc, verdict = run_verdict(
            tmp_path / check_name(phrase),
            {"boot1": healthy("boot1") + [quoted]})
        assert f"{check_name(phrase)} FAIL" not in verdict, verdict
        assert f"IMG-NOTE boot1 quoted-{check_name(phrase)[3:]}:" in verdict, \
            verdict
        assert proc.returncode == 0, proc.stderr + verdict


def test_the_forbidden_phrase_says_which_lines_it_found(tmp_path):
    """A forbidden-phrase failure names every matching line, so the verdict
    says which units failed without anyone grepping the console."""
    growfs = ("[   38.453838] systemd[1]: systemd-growfs-root.service: "
              "Failed with result 'exit-code'.")
    ssh = ("[  172.770348] systemd[1]: ssh.service: "
           "Failed with result 'exit-code'.")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [growfs, ssh]})
    assert "IMG-CHECK boot1 no-Failed-with-result FAIL" in verdict, verdict
    assert "systemd-growfs-root.service" in verdict, verdict
    # Both of them: a report that stops at the first match would hide the
    # second.
    assert "ssh.service" in verdict, verdict
    assert proc.returncode == 1


def test_a_quoted_phrase_is_not_dumped_as_if_the_system_had_said_it(tmp_path):
    """The FAIL branch's dump is scoped as the gate is: a line where a unit
    only quoted the phrase is not printed under the red as if systemd had
    said it."""
    real = ("[   50.000000] systemd[1]: otp-unit.service: "
            "Failed with result 'exit-code'.")
    echoed = forwarded(
        "Aug 16 22:05:01 otp-unit systemd[1]: nosuch.service: "
        "Failed with result 'exit-code'.",
        ident="img-guest-check.sh", pid=900)
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [real, echoed]})
    assert "IMG-CHECK boot1 no-Failed-with-result FAIL" in verdict, verdict
    assert "otp-unit.service" in verdict, verdict
    assert "nosuch.service" not in verdict, verdict
    assert proc.returncode == 1


def test_a_journald_copy_of_a_kernel_line_is_not_a_second_boot(tmp_path):
    """Kernel lines re-forwarded by journald (identifier `kernel`) are not
    counted as a second kernel entry, so forwarding cannot fake a reboot
    loop. test_a_boot_reset_loop_fails_even_with_a_successful_lap is the
    control: a real second entry has no identifier and still fails."""
    echo = "[   50.000000] kernel: Booting Linux on physical CPU 0x0000000000 [0x410fd034]"
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [echo]})
    assert "IMG-CHECK boot1 single-kernel-entry PASS" in verdict, verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_empty_consoles_still_produce_a_verdict(tmp_path):
    # The no-evidence case: qemu never launched or the boot wrote zero bytes.
    # The script must still reach verdict.txt, so every grep has to tolerate
    # no match under pipefail and errexit.
    proc, verdict = run_verdict(tmp_path, {"boot1": []}, uart1_lines=[])
    assert proc.returncode == 1
    assert verdict is not None, "no verdict.txt: the verdict block died mid-way"
    assert "IMG-CHECK boot1 unit-started FAIL" in verdict
    assert "IMG-CHECK boot1 Linux-version FAIL" in verdict


def test_early_stop_is_reported_as_the_harnesss_own_hand(tmp_path):
    proc, _ = run_verdict(
        tmp_path, {"boot1": healthy("boot1")}, qemu_rc="143", early_stop="397"
    )
    assert proc.returncode == 0, proc.stderr
    assert "stopped by the harness at 397s" in proc.stderr


def test_a_backstopped_boot_that_did_report_is_not_called_silent(tmp_path):
    """rc=124 with no early stop but a complete guest report is not described
    as "without a guest report". The sampler polls every 30 seconds, so a
    done line can land after its last look; the healthy fixture is this
    case."""
    proc, _ = run_verdict(tmp_path, {"boot1": healthy("boot1")},
                          qemu_rc="124", early_stop="")
    assert proc.returncode == 0, proc.stderr
    assert "without a guest report" not in proc.stderr, proc.stderr
    assert "the guest DID report done" in proc.stderr, proc.stderr


def test_a_backstopped_boot_that_said_nothing_is_still_called_silent(tmp_path):
    # An empty console at the backstop is still described as silent.
    proc, _ = run_verdict(tmp_path, {"boot1": kernel_lines()},
                          qemu_rc="124", early_stop="")
    assert proc.returncode == 1
    assert "without a guest report" in proc.stderr, proc.stderr


def test_the_release_phase_is_not_narrated_as_a_missing_guest_report(tmp_path):
    """The release phase, which never has a guest report, is not narrated as
    missing one: when it hits the backstop the cause is a boot that did not
    finish."""
    proc, _ = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"),
         "release": release_console(multi_user=False)},
        phases=("boot1", "release"), qemu_rc="124", early_stop="")
    assert proc.returncode == 1
    assert "release: qemu ran to the 600s backstop without the boot ever " \
           "finishing" in proc.stderr, proc.stderr
    assert "release: qemu ran to the 600s backstop without a guest report" \
        not in proc.stderr, proc.stderr
    # A release boot that finished too late for the sampler is said to have
    # finished.
    proc, _ = run_verdict(
        tmp_path / "late",
        {"boot1": healthy("boot1"), "release": healthy("release")},
        phases=("boot1", "release"), qemu_rc="124", early_stop="")
    assert proc.returncode == 0, proc.stderr
    assert "the boot DID finish" in proc.stderr, proc.stderr


def test_guest_self_reset_is_explained_when_checks_fail(tmp_path):
    proc, verdict = run_verdict(tmp_path, {"boot1": kernel_lines()}, qemu_rc="0")
    assert proc.returncode == 1
    assert "the guest reset itself" in proc.stderr


# --- the third boot: the shipped probe, and everything it did not do ------
#
# Every clause below is an absence, which a boot that never ran or a stale
# grep pattern satisfies as well as a healthy boot does. So each clause gets
# a console that violates only it, beside a healthy one that must pass.
# test_a_release_boot_whose_probe_unit_is_missing_fails matters most: without
# it, deleting the probe unit from the image would make this phase greener.


def run_release(tmp_path, release_lines=None, *, boot1_lines=None, **kw):
    """One healthy boot1 for the controls, and one release phase under test.
    boot1 is the only place in a run that shows the "found nothing" greps
    could have found something."""
    consoles = {
        "boot1": healthy("boot1") if boot1_lines is None else boot1_lines,
        "release": healthy("release") if release_lines is None else release_lines,
    }
    return run_verdict(tmp_path, consoles, phases=("boot1", "release"), **kw)


def check_state(verdict, phase, name):
    """PASS or FAIL for one named IMG-CHECK, read off the verdict file."""
    match = re.search(
        rf"^IMG-CHECK {re.escape(phase)} {re.escape(name)} (PASS|FAIL)",
        verdict, re.M)
    assert match, f"no `IMG-CHECK {phase} {name}` line in:\n{verdict}"
    return match.group(1)


RELEASE_CLAUSES = (
    "Reached-target-multi-user.target",
    "unit-started",
    "journal-forwarding-alive",
    "cmdline-carries-no-imgcheck-token",
    "release-boot-is-not-a-first-boot",
    "imgcheck-unit-considered-and-skipped",
    "guest-probe-silent",
    "guest-probe-journal-tag-absent",
    "guest-probe-cups-queue-unnamed",
    "probe-droppings-were-on-the-card",
    "probe-droppings-stayed-off-the-card",
    "boot-partition-unchanged",
    "identity-store-unchanged",
)


def test_a_healthy_release_boot_passes_every_clause(tmp_path):
    """Positive control for the whole section: a healthy release boot passes
    every clause. Every failure test below is one edit away from this
    fixture."""
    proc, verdict = run_release(tmp_path)
    for name in RELEASE_CLAUSES:
        assert check_state(verdict, "release", name) == "PASS", verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_the_release_phase_is_not_asked_for_a_guest_report(tmp_path):
    """The release phase is not asked for OTP-RESULT, OTP-GUEST-DONE or named
    guest checks: it has no probe, and those demands would fail every
    healthy release boot."""
    _, verdict = run_release(tmp_path)
    for name in ("guest-reported-once", "guest-finished",
                 "guest-checks-all-passed", "guest-ran-every-named-check"):
        assert f"IMG-CHECK release {name}" not in verdict, verdict
    # And boot1 in the same run is still asked all of them.
    for name in ("guest-reported-once", "guest-ran-every-named-check"):
        assert check_state(verdict, "boot1", name) == "PASS", verdict


# --- clause: systemd looked at the unit and skipped it --------------------

def test_a_release_boot_whose_probe_unit_is_missing_fails(tmp_path):
    """A release boot of an image with the probe unit deleted fails. Such an
    image satisfies every absence clause, but the claim under test is that
    the shipped probe is inert, not that there is no probe."""
    proc, verdict = run_release(tmp_path, release_console(skip_line=False))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "FAIL", verdict
    assert "not in this image" in verdict, verdict
    assert proc.returncode == 1
    # The absences are all still true here.
    for name in ("guest-probe-silent", "guest-probe-journal-tag-absent",
                 "probe-droppings-stayed-off-the-card"):
        assert check_state(verdict, "release", name) == "PASS", verdict


def test_a_skip_line_about_some_other_unit_is_not_this_one(tmp_path):
    """A skip line about some other unit does not count: Raspberry Pi OS skips
    many units on conditions."""
    other = condition_skipped(unit="systemd-firstboot.service",
                              description="First Boot Wizard")
    proc, verdict = run_release(
        tmp_path, release_console(skip_line=False, probe_said=[other]))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "FAIL", verdict
    assert proc.returncode == 1


def test_either_wording_systemd_uses_for_a_condition_skip_is_accepted(tmp_path):
    """Either wording systemd v257 uses for a condition skip is accepted: the
    detailed one naming the condition, or the generic "Condition check
    resulted in %s being skipped.". The unit's name is on the line in both."""
    generic = condition_skipped(
        unit="Condition check resulted in otp-unit-imgcheck.service",
        description="Report the overlay root to the tier-3 image boot",
        wording="being skipped.")
    proc, verdict = run_release(
        tmp_path, release_console(skip_line=False, probe_said=[generic]))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "PASS", verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_the_wording_a_real_image_actually_printed_is_accepted(tmp_path):
    """The post-v258 wording a real image prints is accepted:

        systemd[1]: otp-unit-imgcheck.service - Report the overlay root to
        the tier-3 image boot skipped, unmet condition check
        ConditionKernelCommandLine=otp.imgcheck

    v257 and v258 say "was skipped because of an unmet condition check
    (X=Y).", so the pattern spans both.
    """
    observed = ("[  102.043301] systemd[1]: otp-unit-imgcheck.service - "
                "Report the overlay root to the tier-3 image boot skipped, "
                "unmet condition check ConditionKernelCommandLine=otp.imgcheck")
    proc, verdict = run_release(
        tmp_path, release_console(skip_line=False, probe_said=[observed]))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "PASS", verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_a_unit_skipped_for_someone_elses_condition_is_not_our_evidence(tmp_path):
    """A probe unit skipped for a different condition does not satisfy the
    clause: when the line names a condition it must be
    ConditionKernelCommandLine=otp.imgcheck, which ties the skip to the
    missing token."""
    other = ("[  102.043301] systemd[1]: otp-unit-imgcheck.service - Report "
             "the overlay root to the tier-3 image boot skipped, unmet "
             "condition check ConditionPathExists=/some/other/thing")
    proc, verdict = run_release(
        tmp_path, release_console(skip_line=False, probe_said=[other]))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "FAIL", verdict
    assert "not ConditionKernelCommandLine=otp.imgcheck" in verdict, verdict
    assert proc.returncode == 1


def test_the_skip_line_is_quoted_into_the_evidence(tmp_path):
    """The skip line is quoted into the evidence as an IMG-NOTE, so the exact
    wording this image's systemd uses is on record."""
    _, verdict = run_release(tmp_path)
    note = re.search(r"^IMG-NOTE release imgcheck-skip-line: (.*)$",
                     verdict, re.M)
    assert note, verdict
    assert "otp-unit-imgcheck.service" in note.group(1), note.group(1)
    assert "ConditionKernelCommandLine=otp.imgcheck" in note.group(1), \
        note.group(1)


#: How much room the quoted skip line must have left after the longest wording
#: systemd could print for the shipped unit, so a growing Description= fails
#: this test before the note starts clipping.
SKIP_NOTE_MARGIN = 100


def test_the_quoted_skip_line_cannot_clip_the_condition_it_now_gates_on(tmp_path):
    """The harness's `cut` on the quoted skip line leaves SKIP_NOTE_MARGIN to
    spare for both wordings. The clause gates on
    `ConditionKernelCommandLine=otp.imgcheck`, which comes last on the line
    and reaches the record only through this note. Both lengths are derived
    from the shipped unit file."""
    text = IMG_BOOT.read_text()
    note = re.search(
        r"IMG-NOTE %s imgcheck-skip-line.*?\n.*?cut -c1-(\d+)", text, re.S)
    assert note, "the skip line is no longer quoted through a cut"
    width = int(note.group(1))

    stamp = "[  102.043301] systemd[1]: "
    unit = "otp-unit-imgcheck.service"
    desc = imgcheck_description()
    longest = 0
    for wording in (SKIP_WORDING, SKIP_WORDING_MAIN):
        line = f"{stamp}{unit} - {desc} {wording}"
        longest = max(longest, len(line))
        assert "ConditionKernelCommandLine=otp.imgcheck" in line[:width], (
            f"the note clips at {width} characters and this wording puts the "
            f"condition parameter past it ({len(line)} characters):\n{line}")
    assert width - longest >= SKIP_NOTE_MARGIN, (
        f"the longest skip line systemd can print for this unit is {longest} "
        f"characters and the note clips at {width}, leaving "
        f"{width - longest}. Under {SKIP_NOTE_MARGIN} characters of headroom "
        f"is close enough that the next edit to Description= clips the "
        f"condition parameter out of the record silently -- widen the cut, "
        f"or shorten the description")


def test_the_fixture_takes_the_probe_units_description_from_the_unit():
    """The fixture takes the probe unit's description from the unit file. A
    hard-coded copy would keep the clip test above green while the real
    note truncated."""
    assert imgcheck_description() in condition_skipped(), condition_skipped()
    assert imgcheck_description() in IMGCHECK_UNIT.read_text()
    # And it is not this file's own string.
    assert 'description="' not in inspect.getsource(condition_skipped), \
        "condition_skipped() carries a hard-coded description again"


# --- clause: the boot really happened and finished ------------------------

def test_a_release_boot_that_never_finished_fails(tmp_path):
    """A release boot that never reached multi-user.target fails, as in the
    other phases: a boot that stopped short never had the chance to run the
    probe it is claimed to have kept quiet."""
    proc, verdict = run_release(tmp_path, release_console(multi_user=False))
    assert check_state(verdict, "release",
                       "Reached-target-multi-user.target") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_release_boot_that_never_started_the_unit_fails(tmp_path):
    # The rest of per_boot_verdict's bar applies too: a release boot is still
    # a boot of this appliance.
    proc, verdict = run_release(
        tmp_path, [line for line in healthy("release") if "Started " not in line])
    assert check_state(verdict, "release", "unit-started") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_release_boot_that_was_a_first_boot_fails(tmp_path):
    # first-boot-complete.target in the third boot of the same card means the
    # persisted machine-id is gone.
    proc, verdict = run_release(tmp_path, release_console(first_boot=True))
    assert check_state(verdict, "release",
                       "release-boot-is-not-a-first-boot") == "FAIL", verdict
    assert proc.returncode == 1


# --- clause: the journal really does reach this console -------------------

def test_a_release_console_the_journal_never_reached_fails(tmp_path):
    """A release console with no journald-forwarded line from a speaker other
    than PID 1 fails. Without that control, the absence of the probe's
    journal tag is equally true of a console the journal never reached."""
    proc, verdict = run_release(tmp_path, release_console(forwarding=False))
    assert check_state(verdict, "release",
                       "journal-forwarding-alive") == "FAIL", verdict
    assert "unbacked" in verdict, verdict
    assert proc.returncode == 1


def test_pid_1_talking_to_itself_is_not_journal_forwarding(tmp_path):
    """PID 1's own lines do not count as forwarding: they reach the console
    through /dev/kmsg whether or not journald forwards anything."""
    only_pid1 = release_console(forwarding=False) + [
        "[   21.000000] systemd[1]: Startup finished in 4.115s.",
    ]
    proc, verdict = run_release(tmp_path, only_pid1)
    assert check_state(verdict, "release",
                       "journal-forwarding-alive") == "FAIL", verdict
    assert proc.returncode == 1


# --- clause: the probe said nothing ---------------------------------------

def test_a_release_boot_where_the_probe_spoke_fails(tmp_path):
    proc, verdict = run_release(
        tmp_path,
        release_console(probe_said=[forwarded(
            "OTP-GUEST starting phase=release on Linux 6.12.96+rpt-rpi-v8 aarch64",
            ident="img-guest-check.sh", pid=903)]))
    assert check_state(verdict, "release", "guest-probe-silent") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_probe_that_ran_and_refused_is_not_a_probe_that_stayed_inert(tmp_path):
    """`OTP-GUEST refusing to run:` counts as the probe speaking. It is
    printed by the probe's own phase guard when the unit started without
    the token, so the pattern has no trailing hyphen."""
    refusal = forwarded("OTP-GUEST refusing to run: the kernel command line "
                        "carries no", ident="img-guest-check.sh", pid=903)
    proc, verdict = run_release(tmp_path, release_console(probe_said=[refusal]))
    assert check_state(verdict, "release", "guest-probe-silent") == "FAIL", verdict
    assert proc.returncode == 1
    # And that wording is the shipped one.
    assert "OTP-GUEST refusing to run" in GUEST_CHECK.read_text()


def test_the_silence_is_not_believed_when_the_control_boot_is_silent_too(tmp_path):
    """The silence is not believed when the same grep finds nothing in boot1,
    where the probe did run: the pattern may have stopped matching."""
    deaf = [line for line in healthy("boot1") if "OTP-GUEST" not in line]
    proc, verdict = run_release(tmp_path, boot1_lines=deaf)
    assert check_state(verdict, "release", "guest-probe-silent") == "FAIL", verdict
    assert "0 in boot1" in verdict, verdict
    assert proc.returncode == 1


def test_a_release_boot_carrying_the_probes_journal_tag_fails(tmp_path):
    """A release console carrying a line whose speaker is the probe's journal
    tag (`systemd-cat -t otp-imgcheck`) fails: the probe reached the
    journal."""
    proc, verdict = run_release(
        tmp_path, release_console(probe_said=[journal_forwarded("release")]))
    assert check_state(verdict, "release",
                       "guest-probe-journal-tag-absent") == "FAIL", verdict
    assert proc.returncode == 1


def test_the_probe_unit_naming_itself_is_not_the_probes_journal_tag(tmp_path):
    """The required skip line, which names otp-unit-imgcheck.service, does not
    trip the tag clause: the tag is otp-imgcheck and is matched in speaker
    position."""
    _, verdict = run_release(tmp_path)
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "PASS", verdict
    assert check_state(verdict, "release",
                       "guest-probe-journal-tag-absent") == "PASS", verdict


def test_a_release_boot_naming_the_probes_cups_queue_fails(tmp_path):
    proc, verdict = run_release(
        tmp_path,
        release_console(probe_said=[forwarded(
            "queue=device for otpimgcheck: usb://OTP/imgcheck",
            ident="img-guest-check.sh", pid=903)]))
    assert check_state(verdict, "release",
                       "guest-probe-cups-queue-unnamed") == "FAIL", verdict
    assert proc.returncode == 1


def test_the_cups_queue_control_comes_off_the_boot_that_made_one(tmp_path):
    # boot1 is where the probe creates and reports `otpimgcheck`, so only its
    # console can show this grep works.
    quiet = [line.replace("otpimgcheck", "somequeue")
             for line in healthy("boot1")]
    proc, verdict = run_release(tmp_path, boot1_lines=quiet)
    assert check_state(verdict, "release",
                       "guest-probe-cups-queue-unnamed") == "FAIL", verdict
    assert "0 in boot1" in verdict, verdict
    assert proc.returncode == 1


# --- clause: the token was absent, not empty ------------------------------

def test_a_release_boot_handed_the_token_fails(tmp_path):
    """A release boot whose kernel command line carries the token fails,
    including `otp.imgcheck=`: ConditionKernelCommandLine matches the
    left-hand side of an assignment, so the empty form still starts the
    unit."""
    for token in ("otp.imgcheck=release", "otp.imgcheck", "otp.imgcheck="):
        lines = [line for line in healthy("release")
                 if "Kernel command line:" not in line]
        lines.insert(1, cmdline_line("release", token=token))
        proc, verdict = run_release(tmp_path / token.replace("=", "_"), lines)
        assert check_state(verdict, "release",
                           "cmdline-carries-no-imgcheck-token") == "FAIL", verdict
        assert proc.returncode == 1


def test_a_release_boot_with_no_command_line_echo_at_all_fails(tmp_path):
    # The absence must be measured on a `Kernel command line:` line that
    # exists.
    proc, verdict = run_release(tmp_path, release_console(cmdline=False))
    assert check_state(verdict, "release",
                       "cmdline-carries-no-imgcheck-token") == "FAIL", verdict
    assert "no Kernel command line line" in verdict, verdict
    assert proc.returncode == 1


def test_the_probe_phases_are_required_to_carry_the_token(tmp_path):
    """boot1 and boot2 are required to carry the token, found by the same
    grep. This makes the release phase's absence meaningful and names the
    fault if a probe boot never got the token."""
    lines = [line for line in healthy("boot1")
             if "Kernel command line:" not in line]
    lines.insert(1, cmdline_line("boot1", token=""))
    proc, verdict = run_verdict(tmp_path, {"boot1": lines}, phases=("boot1",))
    assert check_state(verdict, "boot1",
                       "cmdline-carries-the-imgcheck-token") == "FAIL", verdict
    assert proc.returncode == 1


def token_case_block() -> str:
    """The shipped per-phase token choice, sliced out of boot_phase()."""
    text = IMG_BOOT.read_text()
    start = text.index('    local imgcheck_token=""')
    end = text.index("    esac\n", start) + len("    esac\n")
    return text[start:end]


def test_the_release_phase_is_handed_no_token_and_the_others_are(tmp_path):
    """The shipped `case` is run to show the release phase gets no token and
    the other phases do, so a change that put the token back fails in the
    fast suite."""
    # Only `local` is removed, since it is a syntax error outside a function.
    # The rest is the shipped code.
    block = token_case_block()
    assert '    local imgcheck_token=""\n' in block, block
    block = block.replace('    local imgcheck_token=""\n',
                          '    imgcheck_token=""\n')
    runner = tmp_path / "token.sh"
    runner.write_text('set -euo pipefail\nphase="$1"\n' + block
                      + '\nprintf "TOKEN=[%s]\\n" "$imgcheck_token"\n')
    for phase, want in (("boot1", "otp.imgcheck=boot1"),
                        ("boot2", "otp.imgcheck=boot2"),
                        ("release", "")):
        proc = subprocess.run(["bash", str(runner), phase],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert f"TOKEN=[{want}]" in proc.stdout, (phase, proc.stdout)
    # Empty, not `otp.imgcheck=`. The -append value is one double-quoted
    # argument, so the empty expansion leaves a doubled space that the
    # kernel's parser skips; an assignment with nothing after it would still
    # satisfy the unit's condition.
    append = next(line for line in IMG_BOOT.read_text().splitlines()
                  if line.strip().startswith("-append "))
    assert append.strip().startswith('-append "') and append.rstrip().endswith('" \\'), \
        ("the -append value is no longer one double-quoted argument, so the "
         "reasoning above about how the empty token disappears no longer "
         "describes this line: " + append)
    assert "$imgcheck_token" in append, append
    assert "otp.imgcheck" not in append, \
        "the token is spelled literally on the -append line, so no phase " \
        "can be without it"


# --- clause: the card came out the way it went in -------------------------

def test_a_release_phase_with_nothing_to_delete_fails(tmp_path):
    """Control for the deletion: a release phase whose pre-strip listing lacks
    the probe's records fails, because "they did not come back" would then
    be true of a broken image too."""
    proc, verdict = run_release(
        tmp_path,
        prestrip={"release": [line for line in prestrip_listing("release")
                              if "otp-imgcheck" not in line]})
    assert check_state(verdict, "release",
                       "probe-droppings-were-on-the-card") == "FAIL", verdict
    assert "not there to delete" in verdict, verdict
    assert proc.returncode == 1
    # The absence afterwards still reads PASS, which is why the control
    # exists.
    assert check_state(verdict, "release",
                       "probe-droppings-stayed-off-the-card") == "PASS", verdict


def test_a_release_boot_that_put_the_probes_records_back_fails(tmp_path):
    # The probe running would write both of these again.
    before, after = healthy_listings("release")
    proc, verdict = run_release(
        tmp_path, boot_files={"release": (before, after + FAT_DROPPINGS)})
    assert check_state(verdict, "release",
                       "probe-droppings-stayed-off-the-card") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_strip_that_did_nothing_fails_rather_than_passing(tmp_path):
    # mdel against a wrong offset writes nothing silently, so both ends are
    # required.
    before, after = healthy_listings("release")
    proc, verdict = run_release(
        tmp_path,
        boot_files={"release": (before + FAT_DROPPINGS, after + FAT_DROPPINGS)})
    assert check_state(verdict, "release",
                       "probe-droppings-stayed-off-the-card") == "FAIL", verdict
    assert "before:" in verdict, verdict
    assert proc.returncode == 1


def test_a_release_boot_that_changed_the_boot_partition_fails(tmp_path):
    """A release boot that changed the boot partition's file list fails: the
    identity store, the credential and the saved settings live there,
    outside the overlay."""
    before, after = healthy_listings("release")
    for label, changed in (
            ("lost", [x for x in after if "credential" not in x]),
            ("gained", after + ["::/otp-unit.conf.new"])):
        proc, verdict = run_release(
            tmp_path / label, boot_files={"release": (before, changed)})
        assert check_state(verdict, "release",
                           "boot-partition-unchanged") == "FAIL", verdict
        assert proc.returncode == 1


def test_a_listing_in_a_different_order_is_not_a_changed_partition(tmp_path):
    # Compared sorted, by name: mtools prints FAT directory order and a
    # rewritten file can take a freed slot.
    before, after = healthy_listings("release")
    proc, verdict = run_release(
        tmp_path, boot_files={"release": (before, list(reversed(after)))})
    assert check_state(verdict, "release",
                       "boot-partition-unchanged") == "PASS", verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_a_release_boot_that_rewrote_a_kept_file_fails(tmp_path):
    """A release boot that rewrote a kept file in place fails. The listing
    cannot see that, so the credential and machine-id are compared by
    content, as digests because the work directory is a CI artifact."""
    for name in FAT_CONTENTS:
        before, after = healthy_digests("release")
        after[name] = digest_of("something else entirely")
        proc, verdict = run_release(
            tmp_path / name.replace("/", "_"),
            digests={"release": (before, after)})
        assert check_state(verdict, "release",
                           "identity-store-unchanged") == "FAIL", verdict
        assert name in verdict, verdict
        assert proc.returncode == 1


def test_two_files_that_are_not_there_are_not_an_unchanged_store(tmp_path):
    """Two missing files are not an unchanged store: `absent` equals `absent`,
    so the harness also requires 64 hex characters on both sides."""
    gone = {name: "absent" for name in FAT_CONTENTS}
    proc, verdict = run_release(
        tmp_path, digests={"release": (dict(gone), dict(gone))})
    assert check_state(verdict, "release",
                       "identity-store-unchanged") == "FAIL", verdict
    assert proc.returncode == 1


def test_digests_that_were_never_taken_fail_and_still_produce_a_verdict(tmp_path):
    """Missing digest files fail the clause and still produce a verdict. The
    clause iterates the list of names, so every name must yield 64
    characters, and it must not read a missing file in a way that kills the
    script under errexit while verdict.txt is being written."""
    proc, verdict = run_release(
        tmp_path, digests={"release": ({}, {})})
    assert verdict is not None, "the verdict file was never written"
    assert check_state(verdict, "release",
                       "identity-store-unchanged") == "FAIL", verdict
    # The rest of the phase still reported, so the block ran to the end.
    assert check_state(verdict, "release",
                       "boot-partition-unchanged") == "PASS", verdict
    assert proc.returncode == 1
    # Every name is accounted for in the failure, not just the first.
    for name in FAT_CONTENTS:
        assert name in verdict, (name, verdict)


def enclosing_phase_guard(text: str, needle: str):
    """Which `if [ "$PHASE" = "X" ]` block the line holding `needle` is in.

    img-guest-check.sh gates per-boot work in column-zero blocks, so walk
    back to the first column-zero `if [ "$PHASE" = ...` or `fi`. `fi` first
    means the line is outside every block, and the answer is None.
    """
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} appears on {len(hits)} lines"
    for i in range(hits[0], -1, -1):
        opened = re.match(r'^if \[ "\$PHASE" = "([a-z0-9]+)" \]; then$',
                          lines[i])
        if opened:
            return opened.group(1)
        if lines[i] == "fi":
            return None
    return None


# --- the harness structure the phase depends on ---------------------------

def test_the_strip_happens_before_the_listing_the_boot_is_judged_against():
    """The release phase's three listings are taken in order: what the card
    held, then the strip, then what the emulator was handed, then what came
    back. A strip after the before-listing would read as something the boot
    did."""
    text = IMG_BOOT.read_text()
    strip = text.index('        release) strip_probe_droppings "$dir" ;;')
    before = text.index('    fat_listing "$dir/boot-files-before.txt"')
    launch = text.index("qemu-system-aarch64 \\")
    after = text.index('    fat_listing "$dir/boot-files-after.txt"')
    assert strip < before < launch < after, (strip, before, launch, after)
    # And the control listing is taken inside the strip, before the mdel.
    block = text[text.index("strip_probe_droppings() {"):]
    block = block[:block.index("\n}\n")]
    assert block.index("boot-files-before-strip.txt") < block.index("mdel"), block


def test_the_records_the_release_phase_deletes_are_the_ones_the_probe_writes():
    """The records img-boot.sh deletes ($PROBE_DROPPINGS) are the ones
    img-guest-check.sh writes into $BOOTDIR, so a rename in the probe
    cannot leave the harness deleting a file nothing creates."""
    probe = GUEST_CHECK.read_text()
    written = set(re.findall(r'"\$BOOTDIR/(otp-imgcheck-[a-z-]+)"', probe))
    assert written, "img-guest-check.sh no longer writes any record of its own"
    assert set(PROBE_DROPPINGS) == written, (
        f"img-boot.sh strips {sorted(PROBE_DROPPINGS)} and the probe writes "
        f"{sorted(written)}")
    # And they are written in boot1. A write that moved to the release phase,
    # or to a phase outside the run, would turn
    # probe-droppings-were-on-the-card into a control for an absence nobody
    # created.
    #
    # Checked structurally: find the variable each name is assigned to, the
    # redirection that writes it, and the top-level `if [ "$PHASE" = ... ]`
    # block that redirection sits in.
    for name in PROBE_DROPPINGS:
        holder = re.search(rf'^([A-Z_]+)="\$BOOTDIR/{re.escape(name)}"$',
                           probe, re.M)
        assert holder, (
            f"{name} is no longer assigned to a variable in "
            f"img-guest-check.sh, so this test cannot find where it is "
            f"written")
        write = f'> "${holder.group(1)}"'
        assert probe.count(write) == 1, (
            f"{write} appears {probe.count(write)} times; which one is the "
            f"boot1 write is now a matter of reading order")
        assert enclosing_phase_guard(probe, write) == "boot1", (
            f"{name} is written outside boot1 ("
            f"{enclosing_phase_guard(probe, write)!r}). The release phase "
            f"deletes it before booting and requires it to have been there "
            f"to delete, and only boot1 puts it there.")


def test_the_release_phase_has_a_stop_condition_of_its_own():
    """The release phase has its own sampler stop marker. It has no probe done
    line, so without one every green run would wait out OTP_IMG_TIMEOUT.

    The marker is the one the verdict gates every phase on, and the settle
    window after it is derived from the kernel's writeback defaults
    (dirty_expire_centisecs=3000, dirty_writeback_centisecs=500): a guest
    write in the page cache reaches the card only once writeback runs.
    """
    text = IMG_BOOT.read_text()
    block = text[text.index("    local stop_marker="):text.index("    while kill -0")]
    assert 'stop_marker="Reached target multi-user.target"' in block, block
    settle = re.search(r"^\s*settle=(\d+)$", block, re.M)
    assert settle, block
    assert int(settle.group(1)) >= 35, (
        f"a {settle.group(1)}s settle does not clear the 30s dirty-page "
        f"expiry plus the 5s writeback tick, so a write this phase is "
        f"supposed to catch could still be in the guest's page cache")
    # The marker is one the verdict demands of every phase, so the sampler
    # cannot stop on evidence the verdict does not accept.
    assert '"Reached target multi-user.target"' in verdict_block()
    # And the grep uses the sampler's own variable.
    assert 'grep -cF "$stop_marker"' in text, text

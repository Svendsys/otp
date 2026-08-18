"""The tier-3 verdict logic, tested against synthetic consoles.

Run 12 started the unit and the verdict said FAIL: the grep pattern could
not straddle the ANSI color systemd wraps around the unit name, and
"guest reached" reported the bootconsole's 2s handoff instead of the real
console's 213s. Both were invisible to the test suite, because the suite
covered only the tier-2 gate. These tests close that gap the same way
test_vm_verdict.py does -- by slicing the real verdict block out of the
real script, so a copy cannot drift green while the original regresses.

Every console here is built to fail one specific way, and the healthy one
is built with the REAL raw bytes of run 12's success line -- escape codes
included -- so dropping the ANSI strip turns tests red.

TWO PHASES NOW. Tier 3 boots the image twice for the read-only overlay
(issue #9): the second boot is a power-cycle of the same card, and the
sentinel written to / in the first must be gone while the setting written
to /boot/firmware must not. That means a gate over what the guest reported,
and the gate has the failure mode every gate in this repository has been
caught with -- a phase that never ran produces no output, which looks
exactly like a phase with nothing to say.

AND A THIRD, WHOSE EVIDENCE IS THE OTHER SHAPE. The `release` phase boots the
same card with no otp.imgcheck token, which is how a flashed unit boots, and
everything it claims is an absence: the shipped probe printed nothing, tagged
nothing, and left nothing on the card. That is the failure mode above in its
purest form -- a boot that never happened satisfies every clause -- so each
absence here is paired with the presence that proves the observation was
possible, and there is a test per clause for the console that violates only
that clause. The one that matters most is
test_a_release_boot_whose_probe_unit_is_missing_fails: without it, DELETING
the probe from the image would make this phase greener.
"""
import hashlib
import os
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
IMG_BOOT = REPO / "harness" / "img-boot.sh"
GUEST_CHECK = REPO / "harness" / "img-guest-check.sh"
IMGCHECK_UNIT = REPO / "device" / "systemd" / "otp-unit-imgcheck.service"
MARKER = "# Everything downstream reads the combination"


def shell_list(name: str) -> list:
    """A space-separated `NAME="a b c"` list, read out of img-boot.sh.

    Read rather than restated, for the reason `required_checks` is: a fixture
    that carried its own copy of these names would go on describing a healthy
    release boot after somebody renamed one in the harness, and the clause
    that renamed file belongs to would be exercised by nothing.
    """
    match = re.search(rf'^{name}="(.*?)"$', IMG_BOOT.read_text(), re.S | re.M)
    assert match, f"{name} is gone from {IMG_BOOT}"
    return match.group(1).replace("\\\n", " ").split()


# The probe's own two records, and the three files a release boot must leave
# byte-for-byte alone. Both lists live in img-boot.sh.
PROBE_DROPPINGS = shell_list("PROBE_DROPPINGS")
FAT_CONTENTS = shell_list("FAT_CONTENTS")

# The raw bytes of the success line as run 12's evidence artifact recorded
# them: color around the OK, color around the unit name, CR at the end.
OK_PREFIX = "[\x1b[0;32m  OK  \x1b[0m] "
SUCCESS = OK_PREFIX + "Started \x1b[0;1;39motp-unit.service\x1b[0m - OTP pad print unit.\r"


def verdict_block() -> str:
    """The real verdict logic, from the console concat to the end.

    Sliced rather than duplicated, for the same reason test_vm_verdict.py
    slices vm-check.sh: a copy would go on passing after somebody changed
    the original.
    """
    text = IMG_BOOT.read_text()
    assert MARKER in text, f"{IMG_BOOT} no longer contains {MARKER!r}"
    return text[text.index(MARKER):]


def required_checks(phase: str) -> list:
    """The guest checks img-boot.sh insists on, read out of img-boot.sh.

    Read rather than restated so a healthy fixture here is complete by
    construction. A list written out in this file would go on describing a
    healthy boot after somebody added a check to the harness, which is the
    same defect as a rig carrying its own copy of MaxJobs.
    """
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


# The two entropy lines, spelled as the kernel spells them. bcm2835-rng is
# builtin, so its probe and the CRNG seeding it causes both land within the
# first three seconds -- long before the unit could ask for a pad byte.
HWRNG = "[    2.383417] bcm2835-rng 3f104000.rng: hwrng registered"
CRNG = "[    2.421905] random: crng init done"


# The line that says the boot FINISHED, as systemd 252 spells it: unit name
# first, description after. "Reached target" alone is satisfied by
# remote-fs.target at 30 seconds, which is how run 31968966879 passed that
# clause in both boots while neither one ever finished.
#
# Stamped with `last_ts` rather than a time of its own, so that the fixture
# keeps exactly one latest timestamp -- "guest reached" is a max over the
# concatenated consoles, and a second clock in here would be testing this
# file's arithmetic instead of the harness's.
def multi_user_line(ts):
    return (f"[   {ts}] systemd[1]: Reached target multi-user.target "
            f"- Multi-User System.")


# The target PID 1 pulls in only when it decided this boot was a first boot,
# spelled the way the targets-reached note in run 32020772161 caught it.
FIRST_BOOT_TARGET = ("[   15.000000] systemd[1]: Reached target "
                     "first-boot-complete.target - First Boot Complete.")


# THE KERNEL'S OWN ECHO OF WHAT IT WAS HANDED, which is where the verdict
# reads the otp.imgcheck token back from. Not from the harness's -append: the
# harness building the command line and the harness checking it are the same
# program, so the only statement about the token that cannot be wrong by
# agreeing with itself is the one the kernel prints.
#
# The base is img-boot.sh's -append with its two shell expansions resolved the
# way a real run resolves them -- boot=overlay out of the image's cmdline.txt,
# and the phase token, which the release phase does not get at all.
CMDLINE_BASE = ("rw earlycon loglevel=7 console=ttyAMA1,115200 "
                "systemd.show_status=1 systemd.journald.forward_to_console=1 "
                "initcall_blacklist=bcm2835_pm_driver_init "
                "root=/dev/mmcblk0p2 rootfstype=ext4 rootwait boot=overlay")


def cmdline_line(phase, *, token=None):
    """`Kernel command line:` as the kernel prints it, for one phase.

    `token` overrides what follows the base, so a test can hand the release
    phase the very token it is supposed not to have -- including the
    `otp.imgcheck=` form, which is the interesting one: systemd matches a
    bare word against the left-hand side of an assignment too, so an empty
    value still starts the unit.
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
        # Filtered back out by the two tests that need them missing, so
        # what they prove is the ABSENCE of the line rather than a flag.
        HWRNG,
        CRNG,
        "[    9.180895] systemd[1]: Hostname set to <otp-unit>.",
        f"[   {last_ts}] systemd[1]: Reached target sysinit.target - System Initialization.",
    ]
    if multi_user:
        lines.append(multi_user_line(last_ts))
    # boot1 REALLY IS a first boot, every run: the harness gives each run a
    # fresh `xz -dc` of the image, so /etc/machine-id still says
    # `uninitialized` when PID 1 reads it. boot2 must not be one, and that is
    # the whole point of persisting the id -- so the healthy fixture carries
    # the line in boot1 and withholds it in boot2, which is exactly what run
    # 32020772161's two targets-reached notes showed.
    if first_boot:
        lines.append(FIRST_BOOT_TARGET)
    return lines


def guest_report(phase, *, checks=None, failing=(), done=True, counts=None):
    """What otp-unit-imgcheck.service prints, in the real line format.

    Every line carries the trailing CR a serial console produces, because
    the gate reads the stripped copy and a gate that only worked on clean
    text would pass here and fail on a real boot.
    """
    names = required_checks(phase) if checks is None else list(checks)
    lines = [f"OTP-GUEST starting phase={phase} on Linux 6.12.96+rpt-rpi-v8 aarch64\r"]
    passed = 0
    for name in names:
        state = "FAIL" if name in failing else "PASS"
        passed += state == "PASS"
        # The CUPS queue's name really does travel on this console, inside
        # this one check's detail -- `queue=$(lpstat -v otpimgcheck)`. The
        # release phase's guest-probe-cups-queue-unnamed clause uses boot1's
        # console as the control that its grep can find the name at all, so a
        # fixture that left it out would make that control read zero and the
        # clause fail for a fixture's reason rather than an image's.
        detail = "detail=whatever"
        if name == "diagnostic-sheet-reaches-cups":
            detail = "lpadmin rc=0 | queue=device for otpimgcheck: usb://OTP/imgcheck"
        lines.append(f"OTP-CHECK {phase} {name} {state} {detail}\r")
    if done:
        lines.append(f"OTP-GUEST-DONE {phase}\r")
    got, total = counts if counts else (passed, len(names))
    lines.append(f"OTP-RESULT {phase} {got}/{total}\r")
    return lines


# WHAT JOURNAL FORWARDING LOOKS LIKE ON THE WIRE, which is a shape and not
# just a string. journald writes a forwarded line as a monotonic timestamp,
# the syslog identifier, the pid, and the text:
#
#   [   45.123456] otp-imgcheck[912]: OTP-JOURNAL-FORWARDED boot1
#
# The probe writes this marker with `systemd-cat -t otp-imgcheck` and by no
# other route -- never on its own stdout, which systemd already copies to the
# console -- so the line can only be here if the journal reached the console.
# Issue #21.
def journal_forwarded(phase, ident="otp-imgcheck", pid=912, ts="45.123456"):
    return f"[   {ts}] {ident}[{pid}]: OTP-JOURNAL-FORWARDED {phase}\r"


def forwarded(text, *, ident="python3", pid=412, ts="46.000000"):
    """Any other line journald carried to the console for somebody else."""
    return f"[   {ts}] {ident}[{pid}]: {text}\r"


# SYSTEMD SAYING IT LOOKED AT THE PROBE AND DID NOT START IT.
#
# The wording is systemd v257's, from src/core/job.c: a start job that ends
# JOB_DONE with u->condition_result false and a non-trigger failed condition
# logs "%s was skipped because of an unmet condition check (%s=%s%s)." with
# %s = unit_status_string(), which under the default StatusUnitFormat is
# "<id> - <description>". Run 12's console proves that format is the one this
# image uses -- its success line is "Started ESC[..motp-unit.serviceESC[0m -
# OTP pad print unit." -- so the unit's NAME is on the line whatever systemd
# does to the prose around it.
#
# job.c also sets do_console = false for exactly this case, so a condition
# skip is never printed as a `[ INFO ]` status line: this arrives through the
# journal or not at all, which is why it doubles as evidence of forwarding.
SKIP_WORDING = ("was skipped because of an unmet condition check "
                "(ConditionKernelCommandLine=otp.imgcheck).")


def condition_skipped(unit="otp-unit-imgcheck.service",
                      description="Report the overlay root to the tier-3 image boot",
                      wording=SKIP_WORDING, ts="18.400000"):
    return f"[   {ts}] systemd[1]: {unit} - {description} {wording}\r"


# The probe's other fingerprints, as the release phase greps for them: the
# journal tag in speaker position, and the name of the CUPS queue the probe
# creates for its diagnostic sheet. Both appear in boot1 because the probe
# ran there, and that is what makes their absence in the release phase mean
# something.
def probe_journal_tag(phase="boot1"):
    return journal_forwarded(phase)


def release_console(*, skip_line=True, forwarding=True, first_boot=False,
                    probe_said=(), **kw):
    """A healthy release boot: the machine came up and the probe stayed inert.

    No OTP-GUEST-, no journal marker under the probe's tag, no mention of its
    queue -- and, because every one of those is an absence, two positives: the
    unit started, and systemd named the probe unit while skipping it.

    `forwarding` controls whether anything on the console arrived by the only
    route journald forwarding provides -- a timestamped line from a speaker
    that is not PID 1. Without one, nothing this phase says about silence is
    backed by a working channel, and the harness says so.
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


# What `mdir -b` prints for the FAT partition of an image this harness can
# boot: absolute paths, one per line. kernel8.img is the load-bearing entry --
# the verdict uses it as the positive control that the listing is a listing at
# all, because every "the file is gone" below is equally true of a partition
# nothing could read.
FAT_ROOT = ["::/kernel8.img", "::/bcm2710-rpi-3-b.dtb", "::/config.txt",
            "::/cmdline.txt", "::/initramfs8", "::/otp-unit.conf"]


# The store, one level down, and the two lines the harness's second `mdir`
# call is there to produce. `mdir -b` is NOT recursive -- measured with mtools
# on a hand-built FAT image: a directory shows up as `::/otp-identity/` alone
# and none of its contents appear -- so a fixture without these would describe
# a listing the harness cannot take and let a check that can never pass look
# healthy here.
FAT_STORE = ["::/otp-identity/machine-id", "::/otp-identity/credential"]


# The probe's own two records, in the `::/name` form the listing carries them
# in. boot1 writes both; boot2 reads them and leaves them; the release phase
# deletes them before it boots and requires them not to come back. Named off
# img-boot.sh's PROBE_DROPPINGS rather than spelled here, so a rename in the
# harness cannot leave this fixture describing files nothing looks for.
FAT_DROPPINGS = [f"::/{name}" for name in PROBE_DROPPINGS]


def healthy_listings(phase):
    """The boot partition either side of a healthy phase.

    boot1 goes in with the harness's seed and comes out without it, and with
    the probe's two records on it; boot2 goes in with those and comes out
    holding the quarantined malformed seed the guest fed to userconf-service
    by hand as well.

    THE CREDENTIAL IS NOT THERE BEFORE boot1 and is afterwards, because the
    image ships none: it is written by the wizard's own ExecStartPost on the
    boot that applies an operator's seed, and by nothing else. From boot2 on
    it is on the card at both ends, which is what a power cycle keeping it
    looks like from outside the machine.

    THE RELEASE PHASE GOES IN WITHOUT THE DROPPINGS, because the harness took
    them off between boot2 and this boot, and comes out the same way. Its
    before-listing is therefore boot2's after-listing minus exactly those two
    names -- which is what makes the strip observable at all.
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


def prestrip_listing(phase):
    """What the card held before the harness deleted the probe's records.

    Boot 2's after-listing, unchanged: nothing touches the card between that
    snapshot and the strip. This is the control the deletion needs -- if the
    earlier boots ever stop writing these files, the release phase is
    asserting an absence it was handed rather than one it created.
    """
    if phase == "release":
        return healthy_listings("boot2")[1]
    return healthy_listings(phase)[0]


def digest_of(name):
    """A stable 64-character stand-in for one file's contents.

    Per-name, so two different files never compare equal by accident, and 64
    hex characters because the harness requires that length on both sides --
    the empty string hashes to a perfectly good digest and two of those are
    equal, which is how "the credential survived" would come to mean "there
    has never been a credential".
    """
    return hashlib.sha256(name.encode()).hexdigest()


def healthy_digests(phase):
    """The three content digests either side of a phase, unchanged."""
    kept = {name: digest_of(name) for name in FAT_CONTENTS}
    return dict(kept), dict(kept)


def run_verdict(tmp_path, consoles, *, phases=("boot1",), uart1_lines=None,
                qemu_rc="124", early_stop="", boot_files=None,
                prestrip=None, digests=None):
    """Run the sliced block over synthetic per-phase evidence directories.

    `consoles` maps a phase name to its uart0 lines. The verdict block reads
    a phase as a directory on disk -- both consoles, qemu's exit code, whether
    the harness stopped it, the FAT root either side of the boot, the listing
    taken before the release phase's strip, and the content digests of the
    three files a release boot must not disturb -- so the fixture writes
    exactly what a real boot leaves behind.
    """
    if uart1_lines is None:
        # What the real second UART holds: early bootconsole lines that stop
        # at the ~2s handoff. Their small timestamps sit LAST in the concat,
        # which is exactly the trap "guest reached" used to fall into.
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
            # The two lists the verdict block reads and the boot loop above
            # it sets. Re-emitted from what shell_list() read out of
            # img-boot.sh rather than written out here, so this preamble
            # cannot describe a set of files the harness no longer has.
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


def phase_list_block() -> str:
    """The phase list and the guard on it, sliced out of img-boot.sh.

    Sits above the verdict block and outside it, so it is a second slice
    rather than part of `run_verdict`'s.
    """
    text = IMG_BOOT.read_text()
    assert PHASES_ASSIGNMENT in text, f"{IMG_BOOT} no longer sets PHASES"
    start = text.index(PHASES_ASSIGNMENT)
    end = text.index("\n    exit 1\nfi", start) + len("\n    exit 1\nfi")
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
    """The phases img-boot.sh refuses to run without, off its own guard loop.

    Read rather than restated: the default assignment and the guard are two
    lines that have to agree, and a test that hard-coded either would go on
    approving a default that had quietly lost a phase.
    """
    match = re.search(r"^for want in ([a-z0-9 ]+); do$",
                      phase_list_block(), re.M)
    assert match, "the phase guard no longer loops over the phases it demands"
    return match.group(1).split()


def test_the_default_runs_every_phase_the_guard_demands(tmp_path):
    # The default is the whole tier: one boot cannot observe a power-cycle,
    # and neither of the two that can is booted the way a flashed unit boots.
    proc = run_phase_list(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"PHASES={' '.join(demanded_phases())}" in proc.stdout, proc.stdout
    # And the list really is the three this tier is built out of, so a guard
    # that had lost one could not agree with a default that had lost it too.
    assert demanded_phases() == ["boot1", "boot2", "release"], demanded_phases()


def test_an_empty_phase_list_falls_back_to_the_default(tmp_path):
    # `:-`, not `-`: OTP_IMG_PHASES="" is an empty run, and an empty run
    # would boot nothing at all and have no phase to fail on.
    proc = run_phase_list(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert f"PHASES={' '.join(demanded_phases())}" in proc.stdout, proc.stdout


def phases_refused(proc) -> list:
    """The phases the guard named as missing, off its first line."""
    head = proc.stderr.splitlines()[0]
    assert "leaves out:" in head, proc.stderr
    return head.split("leaves out:")[1].split()


def test_a_phase_list_without_boot2_is_refused(tmp_path):
    """
    OTP_IMG_PHASES drives the boot loop, the verdict loop and the set of
    guest checks each phase must have reported -- so dropping boot2 dropped
    root-writes-discarded-by-the-power-cycle and
    settings-survive-the-power-cycle from what the run required, and the
    gate passed. Measured before this guard: PHASES="boot1" over a healthy
    boot1 console exited 0 and still printed the two-boot conclusion, which
    image.yml quotes into a tagged release's body.
    """
    for phases in ("boot1 release", "boot1 boot3 release", "noboot2 boot1 release",
                   "boot22 boot1 release"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "boot2" in phases_refused(proc), proc.stderr
        assert "PHASES=" not in proc.stdout, proc.stdout


def test_a_phase_list_without_boot1_is_refused_too(tmp_path):
    """
    The half that was missing, and it fails in the confusing direction.

    The seed is mcopy'd onto the card unconditionally -- no phase test
    guards it, deliberately, because the FAT listing taken before boot1 is
    the only vantage point the credential path can be observed from. So
    PHASES="boot2" was ACCEPTED and booted a seeded card straight into the
    checks that assume an unseeded one: nothing has consumed the seed, the
    wizard's condition is true, and userconf-unseeded-boot-skips-the-wizard
    goes red over a card the harness set up that way itself.

    A debugging switch whose one-boot setting produces a red with no
    relation to the image is one people learn to distrust the harness over.
    """
    for phases in ("boot2 release", "boot2 boot3 release", "boot2 boot2 release"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "boot1" in phases_refused(proc), proc.stderr
        assert "PHASES=" not in proc.stdout, proc.stdout
    # And the reason is stated, not just the name: the next person to try
    # this has to be told the card is seeded whatever they asked for.
    proc = run_phase_list(tmp_path, "boot2 release")
    assert "seeded" in proc.stderr, proc.stderr


def test_a_phase_list_without_the_release_boot_is_refused(tmp_path):
    """
    THE PHASE WHOSE ABSENCE TURNS NOTHING RED ON ITS OWN.

    boot1 and boot2 report: drop one and named checks go missing and the run
    is loud about it. The release boot asserts a SILENCE -- the shipped probe
    printed nothing, tagged nothing, wrote nothing -- so a run without it
    produces a completely green verdict for an image nobody asked the
    question of. That is the shape of missing coverage this repository keeps
    finding, and the guard is the only thing that makes dropping it cost
    anything.

    It is also the phase that deletes the probe's two records from the card,
    so a list that kept it and dropped the boots that write them would have
    nothing to delete -- which the two tests above already refuse.
    """
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
    # The positive control: a guard that refused everything would satisfy
    # the three tests above.
    for phases in ("boot1 boot2 release", "boot1 boot2 release boot3"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode == 0, proc.stderr
        assert f"PHASES={phases}" in proc.stdout, proc.stdout


def test_the_full_claim_is_only_made_by_a_run_that_booted_every_phase(tmp_path):
    # The concluding line is what a reader takes away, and image.yml's
    # release note says the same thing in its own words.
    proc, _ = run_verdict(tmp_path, {"boot1": healthy("boot1")},
                          phases=("boot1",))
    assert proc.returncode == 0, proc.stderr
    assert "boots twice" not in proc.stderr, proc.stderr
    assert "NOT the three-boot claim" in proc.stderr, proc.stderr


def test_a_two_phase_run_does_not_claim_the_probe_stayed_inert(tmp_path):
    """
    The half a reader would take on trust, dropped along with its boot.

    Two boots still prove the overlay and the power-cycle, and the sentence
    goes on saying so. What it must stop saying is the release phase's claim:
    nothing in a boot1/boot2 run ever asked what an image with no
    otp.imgcheck token does, and a sentence that said otherwise would be
    describing an experiment nobody ran.
    """
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
    # And the third boot's own sentence, which is the one that has to name
    # what it observed rather than what it hoped.
    for clause in ("no otp.imgcheck token", "inert", "skipped",
                   "stayed off the card"):
        assert clause in proc.stderr, (clause, proc.stderr)


def test_fixture_success_line_really_carries_escape_codes():
    # The whole point of the healthy fixture is that ANSI sits between
    # "Started" and the description. If someone simplifies the fixture,
    # the ANSI-strip coverage silently evaporates -- fail here instead.
    assert "\x1b" in SUCCESS
    assert "Started OTP pad print unit" not in SUCCESS
    between = SUCCESS.split("Started ")[1]
    assert between.startswith("\x1b"), "unit name must be color-wrapped"


def test_the_harness_and_the_guest_agree_on_which_checks_exist():
    """
    The named-check gate is only worth having while the names are real.

    img-boot.sh lists what tier 3 claims the guest reported; img-guest-check.sh
    is what reports it. Drift in either direction is a defect: a name the
    guest no longer emits fails every run, and a check the guest emits but
    the harness does not require is one that can be deleted without anything
    going red.
    """
    emitted = set(re.findall(r"^\s*check\s+([a-z0-9-]+)[\s\\]",
                             GUEST_CHECK.read_text(), re.M))
    assert emitted, "no check calls found in img-guest-check.sh"
    # The UNION over the phases, because the two boots no longer ask the same
    # questions: only boot1 has a seed to consume, only boot2 can speak about
    # what a power-cycle kept.
    demanded = set(required_checks("boot1")) | set(required_checks("boot2"))
    assert emitted == demanded, (
        f"img-guest-check.sh emits {sorted(emitted)}, img-boot.sh requires "
        f"{sorted(demanded)}")
    # And no name is demanded of a phase that cannot emit it. A phase-specific
    # check listed as COMMON fails every run of the other phase -- red rather
    # than green, but red for a bookkeeping mistake rather than for the image.
    common = set(required_checks("boot1")) & set(required_checks("boot2"))
    for phase in ("boot1", "boot2"):
        only = set(required_checks(phase)) - common
        assert only, f"{phase} demands nothing of its own"


def test_the_probe_looks_for_the_words_the_unit_actually_logs():
    """
    The headless check reads the unit's journal for two strings. They are
    written in one file and matched in another, which is exactly how a check
    stops checking without anything going red: reword either line in
    otpunit/ and the probe goes on matching nothing, forever, in silence.

    Both are gated together in the guest -- see unit-detects-no-panel -- so
    a drift in either one fails every tier-3 run rather than passing it. That
    is red rather than green, but it is red for a reason nobody would guess
    from the failure, and this test names it in the fast suite instead.
    """
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
    """
    The third clause, and the only one that is assembled at runtime rather
    than written out anywhere -- which makes it the easiest of the three to
    break by accident.

    `interface -- display: none,` is otpunit/__main__ logging
    hmi.Interface.describe(), and neither file contains that string: __main__
    has the prefix and an f-string, hmi has the two field names. A grep over
    either would pass while the probe matched nothing, so this RUNS the real
    describe() on the real dataclass instead, with the interface a unit that
    found nothing to draw on ends up holding.

    It has to hold for BOTH input halves, and that is the half of this test
    an earlier version got wrong. The needle stops at `display: none,` on
    purpose: a unit with lgpio and nothing wired to the header reports
    `input: GPIO buttons`, and the emulated Pi reports `input: none` -- run
    32020772161, boot1 console-text.log:713-714, `no GPIO buttons ([Errno 22]
    Invalid argument)`. A needle that ran on into the input field would key
    the check on which of those two machines it was looking at.
    """
    import sys
    sys.path.insert(0, str(REPO))
    from otpunit import hmi

    needle = "interface -- display: none,"
    assert f'*"{needle}"*' in GUEST_CHECK.read_text(), \
        f"the probe no longer looks for {needle!r}"
    # Both machines that reach the headless decision with nothing to draw on:
    # the emulated Pi, whose pin claim fails EINVAL, and a real Pi, where
    # gpiozero reserves the pin and succeeds. The needle must match each.
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
    """
    Three files, one account, and the reason it matters is what happens when
    they disagree.

    /usr/lib/userconf-pi/userconf RENAMES the UID-1000 user when the seed
    names a different one: usermod -l, a home directory move, sed over
    /etc/subuid, /etc/subgid and the sudoers drop-in. That is a much larger
    experiment than "were the credentials applied", and it is one this tier
    would be running by accident. Naming the image's own first user keeps the
    rename branch out of it -- so if image/build.sh ever changes
    FIRST_USER_NAME, this seed silently starts testing something else.

    WHAT THAT NARROWING NO LONGER HIDES. The rename is exactly what an
    operator gets for writing any other username, it lives entirely inside
    the overlay, and the store outlives it -- so the next boot met a
    credential naming an account that was gone, and refusing it took the
    unit's only login away. The state that leaves is synthesised in boot2 and
    handed to the real shipped script instead of being seeded here, so this
    agreement stays a narrowing rather than a blind spot: see
    credential-recovers-a-store-naming-another-account.
    """
    first_user = shell_value(BUILD_SH, "FIRST_USER_NAME")
    assert shell_value(IMG_BOOT, "USERCONF_USER") == first_user
    assert shell_value(GUEST_CHECK, "USERCONF_USER") == first_user
    assert "credential-recovers-a-store-naming-another-account" \
        in GUEST_CHECK.read_text(), \
        "nothing in tier 3 exercises the rename branch this seed avoids"


def test_the_guest_looks_for_the_salt_the_harness_actually_planted():
    # The guest never sees the hash -- it is not installed on a unit -- so
    # what it compares is the salt marker. A hash regenerated with a different
    # salt would leave the guest looking for a string nothing writes.
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
    """
    The other half, and it is not decoration: a "malformed" seed that happens
    to be valid would be APPLIED and deleted, leaving no failed_userconf.txt
    -- the boot2 gate would go red for a fixture bug, and the fail-fast it
    exists to observe would never have been exercised.
    """
    bad = shell_value(GUEST_CHECK, "UC_BAD")
    assert userconf_pi_rejects(bad), bad
    # Both validators, not one: the username has spaces and capitals and the
    # password half is empty.
    assert userconf_pi_rejects(bad.split(":")[0] + ":something")
    assert userconf_pi_rejects("validname:")


def test_the_emulated_boot_asks_for_the_journal_on_its_console():
    """
    The parameter the whole of issue #21 hangs off, asserted where a fast
    test can see it.

    Everything else about the forwarding is checked against evidence: the
    verdict requires the marker on the console, and the probe requires the
    journal to have taken it. But both of those only speak during a CI boot,
    and the token itself lives in one string in one line. Delete it and the
    fast suite is entirely green -- for sixteen minutes, until an arm64
    runner says otherwise.

    On -append and nowhere else, deliberately: -append REPLACES the kernel
    command line under emulation, so this changes nothing about a flashed
    unit, and the image's own cmdline.txt must not grow it. A device with its
    journal on the console is a device narrating itself to anyone watching
    the serial header.
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
    """
    Ordering, because the control is worthless if it is taken afterwards.

    boot-files-before.txt is what says the seed reached the card. Snapshot it
    after qemu has run and it says only that the boot left the partition in
    some state, and "the seed was planted" becomes a statement about nothing.
    """
    text = IMG_BOOT.read_text()
    before = text.index('fat_listing "$dir/boot-files-before.txt"')
    launch = text.index("qemu-system-aarch64 \\")
    after = text.index('fat_listing "$dir/boot-files-after.txt"')
    assert before < launch < after, (before, launch, after)


def test_the_seed_is_planted_before_the_first_boot():
    # The mcopy has to happen before the boot loop, or boot1 boots a card the
    # operator never wrote to. Matched on the COPY rather than on the name --
    # the log line above it says ::userconf.txt too, and an assertion that
    # the string appears somewhere is satisfied by the announcement alone.
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
    # rc 124 is the NORMAL ending of a healthy appliance boot (the guest
    # idles; only the stopwatch ends it). It must not fail the run.
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
    """
    THE DEFECT, from the one vantage point that owes nothing to the guest.

    Before this change the seed was applied to /etc/shadow -- inside the
    overlay -- and deleted from the card, so the card came out of boot1
    holding nothing and the operator's password was gone at the power cycle.
    That is this fixture: a healthy boot1 in every other respect, with no
    credential in the listing afterwards.
    """
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before,
                              [x for x in after if "credential" not in x])})
    assert "IMG-CHECK boot1 credential-kept-on-the-card FAIL" in verdict, verdict
    assert proc.returncode == 1
    # The seed checks are untouched by it, which is the whole reason this one
    # had to be added: applied-and-consumed was always true of the broken
    # image too.
    assert "IMG-CHECK boot1 userconf-seed-consumed PASS" in verdict, verdict


def test_a_credential_the_image_shipped_is_not_one_this_boot_kept(tmp_path):
    """
    The before-half of boot1's check, which is what makes it evidence.

    "the file is on the card afterwards" is equally true of an image that was
    built with one -- and a credential baked into a public .img.xz is the same
    hash on every unit ever flashed from it, which is worse than the defect
    this replaces rather than better.
    """
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before + ["::/otp-identity/credential"], after)})
    assert "IMG-CHECK boot1 credential-kept-on-the-card FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_credential_that_did_not_outlive_the_power_cycle_fails(tmp_path):
    """
    boot2's half: the file has to be on the card BEFORE the second boot. A
    listing that only shows it afterwards is a boot that wrote one on a boot
    with no seed -- which is the exposure this design refuses, not the
    persistence it promises.
    """
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
    """
    The floor, held against the list rather than restated beside it.

    `harness/README.md` tells a reader how many guest checks each boot has to
    report, and that number is the only summary of what tier 3 proves that
    anybody reads before trusting a green run. Written by hand it goes stale
    on the first commit that adds a check -- silently, in the direction that
    overstates, because a reader takes the larger number for the older list.
    Counted from `img-boot.sh` here, the way `required_checks` counts
    everything else in this file.
    """
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
    """
    The harness's own listing, held against what mtools actually does.

    `mdir -b -i IMG ::` is NOT recursive: measured on a FAT image built by
    hand, a directory appears as the single line `::/otp-identity/` and none
    of its contents appear at all. The credential lives one level down, so
    without a second call naming that directory this check could never pass
    on any card -- the sign-flipped failure, red on a healthy image, which is
    the one CI would have blamed on the image.
    """
    text = IMG_BOOT.read_text()
    assert re.search(r"^\s*mdir -b -i \"\$IMG@@\$BOOT_OFFSET\" ::/otp-identity",
                     text, re.M), \
        "the host lists only the FAT root, so nothing in the store is visible"
    # Appended, not overwriting the root listing it follows.
    assert ">> \"$1\"" in text, text


def test_a_seed_that_never_reached_the_card_fails(tmp_path):
    """
    The positive control, and the reason the before-listing is taken at all.

    mcopy against a wrong partition offset writes nothing and says nothing --
    that is how run 1 read a harness bug as a bad image -- and a seed that was
    never written is gone after the boot in exactly the way a seed that was
    consumed is. Without this the whole credential gate is satisfied by an
    image the harness never touched.
    """
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": ([x for x in before if "userconf" not in x], after)})
    assert "IMG-CHECK boot1 userconf-seed-planted FAIL" in verdict, verdict
    # And the consumption check still says PASS on that evidence, which is the
    # whole point of having the control: absence is not consumption.
    assert "IMG-CHECK boot1 userconf-seed-consumed PASS" in verdict, verdict
    assert proc.returncode == 1


def test_a_seed_still_sitting_on_the_card_after_the_first_boot_fails(tmp_path):
    # The seeded branch not running at all: the condition never matched, the
    # unit was masked, or the wizard is parked on it right now.
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before, after + ["::/userconf.txt"])})
    assert "IMG-CHECK boot1 userconf-seed-consumed FAIL" in verdict, verdict
    assert "userconf.txt" in verdict
    assert proc.returncode == 1


def test_a_seed_the_image_rejected_fails_the_first_boot(tmp_path):
    """
    Consumed has two halves and this is the second one.

    A first boot that renamed the operator's file to failed_userconf.txt took
    the seed and gave them NO credentials -- and from the outside that looks
    the same as success: userconf.txt is gone either way. A device that lands
    here has an unknowable login on the tty2 recovery getty.
    """
    before, after = healthy_listings("boot1")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1")},
        boot_files={"boot1": (before, after + ["::/failed_userconf.txt"])})
    assert "IMG-CHECK boot1 userconf-seed-consumed FAIL" in verdict, verdict
    assert "failed_userconf.txt" in verdict
    assert proc.returncode == 1


def test_a_malformed_seed_that_left_no_evidence_fails_boot2(tmp_path):
    # The guest ran the experiment and userconf-service did not quarantine the
    # file -- or the experiment never ran. Either way the operator would get
    # no failed_userconf.txt to read.
    before, after = healthy_listings("boot2")
    proc, verdict = run_verdict(
        tmp_path, {"boot2": healthy("boot2")}, phases=("boot2",),
        boot_files={"boot2": (before, [x for x in after if "failed_" not in x])})
    assert "IMG-CHECK boot2 userconf-malformed-seed-quarantined FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_bad_seed_left_where_the_next_wizard_finds_it_fails_boot2(tmp_path):
    # Quarantined means MOVED. A failed_userconf.txt beside a userconf.txt
    # still holding the bad line is a card that prompts on every boot from
    # here on.
    before, after = healthy_listings("boot2")
    proc, verdict = run_verdict(
        tmp_path, {"boot2": healthy("boot2")}, phases=("boot2",),
        boot_files={"boot2": (before, after + ["::/userconf.txt"])})
    assert "IMG-CHECK boot2 userconf-malformed-seed-quarantined FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_boot_partition_nothing_could_read_fails_rather_than_passing(tmp_path):
    """
    Every credential check above an absence is satisfied by an empty listing.

    mdir against a wrong offset, an image that moved, mtools missing: all
    produce no lines, and no lines means no userconf.txt, which reads as
    consumed. kernel8.img is the positive control -- it is the file the
    harness pulled off that partition to boot the guest with, so a listing
    without it is not a listing of the boot partition.
    """
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"),
        boot_files={"boot1": ([], []), "boot2": ([], [])})
    assert "IMG-CHECK boot1 boot-partition-listed FAIL" in verdict, verdict
    assert "IMG-CHECK boot2 boot-partition-listed FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_second_boot_must_report_too(tmp_path):
    # The power-cycle half of issue #9 lives entirely in boot2. A boot2 that
    # never happened produces an empty console, which is indistinguishable
    # from a boot2 with nothing to say -- so silence has to fail.
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
    """
    Zero FAIL lines is trivially true of a guest that checked nothing.

    That is row 4 of issue #14 -- a truncated-run gate satisfied by the
    absence of failures -- arriving in a new place. The overlay checks are
    the ones worth deleting by accident, so the gate names them.
    """
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
    """
    The same gate, for the phase that grew its own list.

    boot1's two checks are the seeded credential path, and they are the ones
    a phase-blind list would silently stop demanding: every other check in
    boot1 would still pass without them, and the run would go green having
    observed nothing about the file the operator wrote.
    """
    kept = [n for n in required_checks("boot1") if n != "userconf-seed-applied"]
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1", checks=kept)
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 guest-no-fail-lines PASS" in verdict, verdict
    assert "guest-ran-every-named-check FAIL" in verdict, verdict
    assert "userconf-seed-applied" in verdict
    assert proc.returncode == 1


def test_a_phase_that_reported_twice_fails(tmp_path):
    # A guest that reported twice rebooted underneath the harness, and the
    # second lap's evidence would otherwise stand as the whole answer.
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1") + guest_report("boot1")
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 guest-reported-once FAIL 2" in verdict, verdict
    assert proc.returncode == 1


def test_a_phase_that_started_but_never_finished_fails(tmp_path):
    # Counts with no OTP-GUEST-DONE in front of them is a guest killed part
    # way through, whose numbers agree with themselves right up to the axe.
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
    """
    bcm2835-rng not probing means the pool has no TRNG behind it, which is
    the difference between a one-time pad and a very good stream cipher --
    and nothing downstream would complain, because getrandom() is happy
    once seeded by anything.

    A gate, and only since the wording was READ rather than guessed. It
    shipped for one commit as a report instead: no console in this
    repository carried the line, and hard-failing a release on an unread
    string is issue #14's catalogued defect with the sign flipped. Run
    31693881773 booted the built image and printed it:

        IMG-NOTE hwrng-line-present: g 3f104000.rng: hwrng registered
    """
    lines = [ln for ln in kernel_lines() if "hwrng registered" not in ln]
    proc, verdict = run_verdict(
        tmp_path, {"boot1": lines + [SUCCESS] + guest_report("boot1")})
    assert "IMG-CHECK boot1 hwrng-registered FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_hardware_rng_line_is_quoted_back_with_its_driver(tmp_path):
    """
    Gated AND quoted. The gate answers "did something register"; the note
    says WHICH, so a kernel bump that swapped the driver is readable
    rather than merely green.

    The quote window was 16 characters of leading context and clipped
    "bcm2835-rng" to "g " on the very first real run -- losing the one
    detail the note exists to carry.
    """
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
    """
    The gate the note below was collecting evidence for.

    `Reached target` on its own is satisfied by remote-fs.target at 30
    seconds. Run 31968966879 reached fourteen targets, passed that clause in
    BOTH boots, and finished neither: the consoles end on
    `systemd-networkd-wait-online.service/start running (2min 37s / no
    limit)`, with the credential wizard's job queued behind it and the seeded
    userconf.txt coming back off the card untouched. Everything the verdict
    could see was green.

    multi-user.target is the line that says the boot FINISHED. It was carried
    as a note for exactly one run, because hard-failing a release on a string
    no console here had ever printed is issue #14's defect with the sign
    flipped -- then run 31972140190 printed it in both boots, which is the
    evidence this repository's report-then-gate rule asks for.
    """
    lines = [ln for ln in kernel_lines() if "multi-user.target" not in ln]
    still_assembling = [
        "[   30.5] systemd[1]: Reached target remote-fs.target - Remote File Systems.",
        "[  157.0] systemd[1]: Job systemd-networkd-wait-online.service/start "
        "running (2min 37s / no limit)"]
    proc, verdict = run_verdict(
        tmp_path,
        {"boot1": lines + still_assembling + [SUCCESS] + guest_report("boot1")})
    # The weaker clause is still satisfied, which is the whole point.
    assert "IMG-CHECK boot1 Reached-target PASS" in verdict, verdict
    assert "IMG-CHECK boot1 Reached-target-multi-user.target FAIL" in verdict, \
        verdict
    assert proc.returncode == 1


def test_a_boot_that_did_finish_passes(tmp_path):
    # The positive control: a gate keyed to a string nothing prints would
    # satisfy the test above and fail every release.
    proc, verdict = run_verdict(tmp_path, {"boot1": healthy("boot1")})
    assert "IMG-CHECK boot1 Reached-target-multi-user.target PASS" in verdict, \
        verdict
    assert proc.returncode == 0, proc.stderr


def test_every_target_reached_is_named_not_just_counted(tmp_path):
    """
    The note the gate above was promoted out of, which stays.

    The gate answers "did the boot finish". The note answers "how far did it
    get", and that is the only thing that made run 31968966879 legible: a red
    gate says a boot did not finish and nothing about where it stopped. It is
    also what the next promotion, whatever target that turns out to be, will
    be argued from.
    """
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
# The one piece of evidence for the machine-id fix that owes nothing to a
# check inside the guest and nothing to a file on the card:
# first-boot-complete.target is pulled in only when PID 1 read
# /etc/machine-id and decided this was a first boot. It rode as a note in the
# targets-reached line for one run -- 32020772161, present in boot1 and
# absent in boot2 -- which is the promotion rule multi-user.target arrived
# under.


def test_a_second_boot_that_was_a_first_boot_all_over_again_fails(tmp_path):
    """
    THE regression this gate exists for. Every guest check can still be green
    on a unit whose machine-id reverted: the store agrees with the live id
    because otp-unit-identity.service refilled it a second earlier, the
    overlay still discards, the settings still survive. The target says it
    anyway, in PID 1's own words.
    """
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
    """
    THE POSITIVE CONTROL for the clause above, and the reason boot2's absence
    can mean anything. An absence is equally satisfied by a console nothing
    was written to, by a boot that died in the initrd, and by a grep looking
    for a string systemd stopped printing. boot1 is a first boot on every run
    -- the harness gives each one a fresh `xz -dc` of the image -- so the same
    string, found by the same grep over the same kind of file, has to be
    THERE.
    """
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
    """
    Two names, one per phase, and neither may appear in the other's verdict:
    a phase-blind pair would demand the target of boot2 and refuse it of
    boot1, and fail every run there has ever been.
    """
    _, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"))
    assert "boot2 first-boot-really-was-a-first-boot" not in verdict, verdict
    assert "boot1 second-boot-is-not-a-first-boot" not in verdict, verdict


def test_a_boot_whose_crng_never_seeds_fails(tmp_path):
    # Without this line the unit has started and cannot generate: the
    # first draw blocks inside getrandom() for as long as it takes. A
    # green tier 3 over that console would be reporting a working image.
    lines = [ln for ln in kernel_lines() if "crng init done" not in ln]
    proc, verdict = run_verdict(
        tmp_path, {"boot1": lines + [SUCCESS] + guest_report("boot1")})
    assert "IMG-CHECK boot1 crng-init-done FAIL" in verdict
    assert proc.returncode == 1


def test_guest_reached_is_the_max_not_the_concats_last_line(tmp_path):
    # uart1 (small timestamps) concatenates second. The last timestamp in
    # file order is 1.9s; the max is 20s. Run 12 reported 1.9.
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
    """
    The whole of issue #21's first clause, stated as a red.

    systemd.journald.forward_to_console=1 is a kernel parameter, and a
    parameter that is accepted and ignored looks exactly like one that works
    -- from outside, "the unit logged nothing interesting" and "nothing the
    unit logged could get out" are the same silence. The probe's marker is
    written into the journal and nowhere else, so its absence here is the
    only thing that can tell those apart.
    """
    lines = kernel_lines() + [SUCCESS] + guest_report("boot1")
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 journal-forwarded-to-console FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_the_marker_has_to_name_the_phase_that_is_being_judged(tmp_path):
    """
    Per phase, like every other marker this tier reads.

    Both boots write into the same working directory and the same evidence
    pipeline, and a phase-blind grep would let boot 1's marker answer for a
    boot 2 whose journald never forwarded anything -- the same trap the
    OTP-RESULT and OTP-GUEST-DONE counts are scoped to a phase to avoid.
    """
    lines = (kernel_lines() + [SUCCESS, journal_forwarded("boot2")]
             + guest_report("boot1"))
    proc, verdict = run_verdict(tmp_path, {"boot1": lines})
    assert "IMG-CHECK boot1 journal-forwarded-to-console FAIL" in verdict, verdict
    assert proc.returncode == 1


def test_a_unit_quoting_a_systemd_phrase_does_not_fail_the_release(tmp_path):
    """
    Issue #21's third clause: the forbidden list, re-read now that the
    console carries every unit's stdout.

    The probe itself is the reason this is not hypothetical. It quotes
    userconf-service's output into a check detail and dumps thirty lines of
    the unit's journal at the end of every phase, and journald now carries
    all of that to the console. Failing a release because a unit REPEATED
    "Failed with result" is row 2 of issue #14 in a new place -- the harness
    matching text it wrote itself.

    Reported rather than dropped, so the scoping cannot swallow one quietly.
    """
    quoted = forwarded(
        "Aug 16 22:05:01 otp-unit systemd[1]: Failed with result 'exit-code'.",
        ident="img-guest-check.sh", pid=900)
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [quoted]})
    assert "no-Failed-with-result FAIL" not in verdict, verdict
    assert "IMG-NOTE boot1 quoted-Failed-with-result:" in verdict, verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_systemds_own_verdict_on_a_unit_still_fails_the_release(tmp_path):
    """
    The positive control for the scoping above, on the same fixture.

    "A unit quoting the phrase does not fail the run" is equally true of a
    gate that stopped looking altogether, and that is the direction this
    change could break in. PID 1 saying it is the case the gate exists for
    and it has to stay red.
    """
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
    """
    THE CONSTRAINT ON THIS LIST, made executable instead of remembered.

    system_lines() keeps kernel output and PID 1 and drops journald's
    forwarded lines from anything else, so ONLY a phrase that PID 1 or the
    kernel utters can be gated here. All five phrases are such a phrase
    today. The line that diagnosed the bug this harness was extended for --
    `sshd[744]: fatal: Cannot bind any address.` -- is not: it is a unit's
    own output, and a phrase of that shape added to the list would be a gate
    that can never fire, green forever, on the exact fault it was added for.

    Each phrase is put on a PID 1 line and required to fail the phase. A
    phrase the filter cannot see fails this test on the day it is added
    rather than on the day it was needed.
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
    """
    The other half of the same rule, over the whole list rather than the one
    phrase the probe happens to quote. A unit REPEATING one of these is row 2
    of issue #14 in a new place, and it must be a note.
    """
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
    """
    The red has to name the unit, and until run 72 it did not.

    That run failed on `no-Failed-with-result` with no detail at all, and
    the verdict was the same four words whether one unit had failed or
    three. Answering "which unit?" meant downloading the evidence artifact
    and grepping a thousand-line console by hand -- for a fact the harness
    had already found and then thrown away.

    Three units were failing on every boot of the shipped image. The gate
    was right; only its report was useless.
    """
    growfs = ("[   38.453838] systemd[1]: systemd-growfs-root.service: "
              "Failed with result 'exit-code'.")
    ssh = ("[  172.770348] systemd[1]: ssh.service: "
           "Failed with result 'exit-code'.")
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [growfs, ssh]})
    assert "IMG-CHECK boot1 no-Failed-with-result FAIL" in verdict, verdict
    assert "systemd-growfs-root.service" in verdict, verdict
    # BOTH of them. A report that stops at the first match would have named
    # growfs and hidden ssh, and hiding the second of three is most of the
    # cost of hiding all three.
    assert "ssh.service" in verdict, verdict
    assert proc.returncode == 1


def test_a_quoted_phrase_is_not_dumped_as_if_the_system_had_said_it(tmp_path):
    """
    The dump is scoped exactly as the gate is.

    IMG-NOTE already quotes one line for the reported case. If the FAIL
    branch's dump read the whole console instead of the filtered copy, a
    boot where PID 1 failed one unit and the probe quoted the phrase would
    print the probe's own output under the red as though systemd had said
    it -- and the reader would go looking for a unit that never failed.
    """
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
    """
    The count that says "reboot loop", protected from the forwarding.

    journald imports /dev/kmsg from the start of the buffer and labels those
    entries with the identifier `kernel`. If it forwards them to the console
    as well, every early kernel line arrives a second time and a perfectly
    healthy boot counts two kernel entries. Whether it does has not been
    measured -- there is no systemd on the machine this was written on -- so
    the count excludes the shape rather than betting on the answer.

    test_a_boot_reset_loop_fails_even_with_a_successful_lap is the control:
    a real second entry has no identifier in front of it and still fails.
    """
    echo = "[   50.000000] kernel: Booting Linux on physical CPU 0x0000000000 [0x410fd034]"
    proc, verdict = run_verdict(
        tmp_path, {"boot1": healthy("boot1") + [echo]})
    assert "IMG-CHECK boot1 single-kernel-entry PASS" in verdict, verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_empty_consoles_still_produce_a_verdict(tmp_path):
    # The no-evidence case: qemu never launched, or run 1's zero-byte boot.
    # An unguarded grep under pipefail+errexit used to kill the script HERE,
    # before verdict.txt existed -- the diagnosis died with it.
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
    """
    rc=124 with no early stop used to print "ran to the backstop without a
    guest report" whatever the console said. The sampler looks every 30
    seconds, so a done line printed inside the last window is in the
    console and not in early-stop -- and the diagnosis then contradicts the
    evidence sitting next to it in the same directory. The healthy fixture
    IS this case: rc=124, no early stop, a complete guest report.
    """
    proc, _ = run_verdict(tmp_path, {"boot1": healthy("boot1")},
                          qemu_rc="124", early_stop="")
    assert proc.returncode == 0, proc.stderr
    assert "without a guest report" not in proc.stderr, proc.stderr
    assert "the guest DID report done" in proc.stderr, proc.stderr


def test_a_backstopped_boot_that_said_nothing_is_still_called_silent(tmp_path):
    # The other half: an empty console at the backstop is exactly what the
    # old wording described, and it must keep saying so.
    proc, _ = run_verdict(tmp_path, {"boot1": kernel_lines()},
                          qemu_rc="124", early_stop="")
    assert proc.returncode == 1
    assert "without a guest report" in proc.stderr, proc.stderr


def test_the_release_phase_is_not_narrated_as_a_missing_guest_report(tmp_path):
    """
    The release boot has no guest report and never will. Telling a reader
    that it "ran to the backstop without a guest report" is a true sentence
    pointing at the wrong thing entirely -- the phase is late because the
    boot did not finish, which is what it was waiting for.
    """
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
    # And a release boot that DID finish, too late for the sampler, is said
    # to have finished rather than to have been silent.
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
# Every clause below is an absence, and an absence is the easiest thing in
# this repository to pass by accident: a boot that never ran, a console
# nothing was written to, an image with the unit removed and a grep for a
# string systemd no longer prints all produce exactly the same silence a
# healthy release boot produces. So each clause gets a console that violates
# only it, and the healthy one has to keep passing beside them.
#
# test_a_release_boot_whose_probe_unit_is_missing_fails is the load-bearing
# one. Without that clause, DELETING otp-unit-imgcheck.service from the image
# would make this phase greener -- and the decision this phase exists to back
# is to SHIP the probe and rely on its condition.


def run_release(tmp_path, release_lines=None, *, boot1_lines=None, **kw):
    """One healthy boot1 for the controls, and one release phase under test.

    boot1 is not decoration here. Three of the release clauses are "this grep
    found nothing", and the only thing in a run that can say the grep would
    have found something is the boot where the probe demonstrably ran.
    """
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
    """The positive control for the whole section.

    A set of clauses that could not pass would satisfy every failure test
    below, and would fail every real release boot. This is also the fixture
    all of them are one edit away from.
    """
    proc, verdict = run_release(tmp_path)
    for name in RELEASE_CLAUSES:
        assert check_state(verdict, "release", name) == "PASS", verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_the_release_phase_is_not_asked_for_a_guest_report(tmp_path):
    """
    guest_gate demands an OTP-RESULT line, an OTP-GUEST-DONE line and every
    named guest check. This phase has no probe by construction, so being
    asked those questions would fail every healthy release boot -- and
    answering them by loosening guest_gate would loosen it for the two
    phases it was written for.
    """
    _, verdict = run_release(tmp_path)
    for name in ("guest-reported-once", "guest-finished",
                 "guest-checks-all-passed", "guest-ran-every-named-check"):
        assert f"IMG-CHECK release {name}" not in verdict, verdict
    # And boot1 in the same run is still asked all of them.
    for name in ("guest-reported-once", "guest-ran-every-named-check"):
        assert check_state(verdict, "boot1", name) == "PASS", verdict


# --- clause: systemd looked at the unit and skipped it --------------------

def test_a_release_boot_whose_probe_unit_is_missing_fails(tmp_path):
    """
    THE INVERSION THIS PHASE MUST NOT HAVE.

    An image with otp-unit-imgcheck.service deleted from it satisfies every
    other clause here perfectly: no probe output, no journal tag, no CUPS
    queue, nothing written to the card. If that read as a pass, this gate
    would be rewarding the one change the owner decided against -- stripping
    the probe from release images -- and the claim "the probe is inert on a
    production unit" would be backed by an image with no probe in it.
    """
    proc, verdict = run_release(tmp_path, release_console(skip_line=False))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "FAIL", verdict
    assert "not in this image" in verdict, verdict
    assert proc.returncode == 1
    # And the absences are all still "true", which is exactly the point.
    for name in ("guest-probe-silent", "guest-probe-journal-tag-absent",
                 "probe-droppings-stayed-off-the-card"):
        assert check_state(verdict, "release", name) == "PASS", verdict


def test_a_skip_line_about_some_other_unit_is_not_this_one(tmp_path):
    """
    A boot of Raspberry Pi OS skips a dozen units on conditions. Matching
    "was skipped because" anywhere on the console would be satisfied by any
    one of them, and would say nothing at all about the probe.
    """
    other = condition_skipped(unit="systemd-firstboot.service",
                              description="First Boot Wizard")
    proc, verdict = run_release(
        tmp_path, release_console(skip_line=False, probe_said=[other]))
    assert check_state(verdict, "release",
                       "imgcheck-unit-considered-and-skipped") == "FAIL", verdict
    assert proc.returncode == 1


def test_either_wording_systemd_uses_for_a_condition_skip_is_accepted(tmp_path):
    """
    systemd v257's job.c has two shapes for this message and picks between
    them at runtime: the detailed one when unit_find_failed_condition()
    returns a non-trigger condition, and the generic "Condition check
    resulted in %s being skipped." when it returns nothing. Ours is the
    detailed one today -- one ConditionKernelCommandLine=, not a trigger --
    but a gate keyed on systemd's prose is a gate that goes red on a healthy
    image the day systemd rewrites a sentence. What is not a matter of
    wording is that the unit's NAME is on the line.
    """
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
    """
    The wording this gate was first written against is not the wording the
    image prints. Run 32180661689 -- the release phase's first execution on
    a machine -- failed the clause while every other release clause passed,
    and the diagnostic quoted what was really on the console:

        systemd[1]: otp-unit-imgcheck.service - Report the overlay root to
        the tier-3 image boot skipped, unmet condition check
        ConditionKernelCommandLine=otp.imgcheck

    systemd reworded it after v258. Both v257 and v258 carry "was skipped
    because of an unmet condition check (X=Y)."; main carries "skipped,
    unmet condition check X=Y". So the source read behind this gate was
    accurate for a systemd this image does not run, which is the whole
    reason the pattern now spans the change instead of pinning to one side
    of it.
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
    """
    The two condition wordings name the condition that failed, and that is
    the strongest thing this phase can be handed: it separates "systemd
    skipped this unit" from "systemd skipped it because our token was
    absent". A unit skipped for ConditionPathExists, say, would satisfy a
    gate that only looked for the name and the word skipped -- and would say
    nothing at all about the token.
    """
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
    """
    Reported as well as gated, so the exact wording this image's systemd
    uses enters the record. The narrower form -- the one that names
    ConditionKernelCommandLine=otp.imgcheck -- becomes promotable to a gate
    of its own once a real boot has printed it, which is the same
    report-then-gate ladder multi-user.target and the hwrng line came up.
    """
    _, verdict = run_release(tmp_path)
    note = re.search(r"^IMG-NOTE release imgcheck-skip-line: (.*)$",
                     verdict, re.M)
    assert note, verdict
    assert "otp-unit-imgcheck.service" in note.group(1), note.group(1)
    assert "ConditionKernelCommandLine=otp.imgcheck" in note.group(1), \
        note.group(1)


# --- clause: the boot really happened and finished ------------------------

def test_a_release_boot_that_never_finished_fails(tmp_path):
    """
    The same bar the other two phases hold. "Reached target" alone is
    satisfied by remote-fs.target at 30 seconds -- run 31968966879 reached
    fourteen targets and finished neither boot -- and a release phase that
    stopped short would have had no chance to run the probe it is claiming
    stayed quiet.
    """
    proc, verdict = run_release(tmp_path, release_console(multi_user=False))
    assert check_state(verdict, "release",
                       "Reached-target-multi-user.target") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_release_boot_that_never_started_the_unit_fails(tmp_path):
    # The rest of per_boot_verdict's bar, reused rather than reinvented: a
    # release boot is still a boot of this appliance.
    proc, verdict = run_release(
        tmp_path, [line for line in healthy("release") if "Started " not in line])
    assert check_state(verdict, "release", "unit-started") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_release_boot_that_was_a_first_boot_fails(tmp_path):
    # first-boot-complete.target in the third boot of the same card means
    # PID 1 could not read a machine-id -- the identity the two boots before
    # it persisted has gone, which is damage this phase is watching for.
    proc, verdict = run_release(tmp_path, release_console(first_boot=True))
    assert check_state(verdict, "release",
                       "release-boot-is-not-a-first-boot") == "FAIL", verdict
    assert proc.returncode == 1


# --- clause: the journal really does reach this console -------------------

def test_a_release_console_the_journal_never_reached_fails(tmp_path):
    """
    The positive control the probe's marker provides in the other two
    phases, and which this phase cannot have because the marker is the
    probe's.

    Without it, "the probe's journal tag never appeared" is equally true of
    a console the journal never reached at all -- and the tag clause below
    would be measuring a channel that was closed. A forwarded line from a
    speaker that is not PID 1 is one nothing else writes: PID 1 also writes
    status lines straight to /dev/console and falls back to /dev/kmsg, and a
    `python3[412]:` line has no such second route.
    """
    proc, verdict = run_release(tmp_path, release_console(forwarding=False))
    assert check_state(verdict, "release",
                       "journal-forwarding-alive") == "FAIL", verdict
    assert "unbacked" in verdict, verdict
    assert proc.returncode == 1


def test_pid_1_talking_to_itself_is_not_journal_forwarding(tmp_path):
    """
    The clause counts forwarded lines MINUS PID 1's, and this is why. A
    console carrying nothing but systemd[1] lines is what a boot with
    forwarding switched off looks like: PID 1's own log reaches the console
    through /dev/kmsg whether or not journald is passing anything on.
    """
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
    """
    The third line the probe can print, and the reason the pattern has no
    trailing hyphen.

    `OTP-GUEST refusing to run:` is what img-guest-check.sh's own phase guard
    prints when the unit STARTED on a command line with no otp.imgcheck
    token. That is the probe running -- the second lock catching what the
    first one let through -- and a unit that started when its condition
    should have stopped it is exactly what this phase is here to notice. A
    pattern of `OTP-GUEST-` would have walked straight past it.
    """
    refusal = forwarded("OTP-GUEST refusing to run: the kernel command line "
                        "carries no", ident="img-guest-check.sh", pid=903)
    proc, verdict = run_release(tmp_path, release_console(probe_said=[refusal]))
    assert check_state(verdict, "release", "guest-probe-silent") == "FAIL", verdict
    assert proc.returncode == 1
    # And the wording really is the shipped one, not a plausible invention.
    assert "OTP-GUEST refusing to run" in GUEST_CHECK.read_text()


def test_the_silence_is_not_believed_when_the_control_boot_is_silent_too(tmp_path):
    """
    THE POSITIVE CONTROL, broken on purpose.

    If the probe's own output stops matching this grep -- a renamed marker, a
    changed prefix, a console the ANSI strip no longer handles -- then the
    release phase would report a silence it can no longer distinguish from
    deafness. So the clause requires the same grep to find the probe in
    boot1, where the probe demonstrably ran, and goes red when it does not.
    """
    deaf = [line for line in healthy("boot1") if "OTP-GUEST" not in line]
    proc, verdict = run_release(tmp_path, boot1_lines=deaf)
    assert check_state(verdict, "release", "guest-probe-silent") == "FAIL", verdict
    assert "0 in boot1" in verdict, verdict
    assert proc.returncode == 1


def test_a_release_boot_carrying_the_probes_journal_tag_fails(tmp_path):
    """
    The probe's side effects go beyond files. It writes a marker into the
    journal with `systemd-cat -t otp-imgcheck`, and journald puts the tag in
    the speaker position of the forwarded line -- so a release console with
    that speaker on it is one where the probe reached the journal.
    """
    proc, verdict = run_release(
        tmp_path, release_console(probe_said=[journal_forwarded("release")]))
    assert check_state(verdict, "release",
                       "guest-probe-journal-tag-absent") == "FAIL", verdict
    assert proc.returncode == 1


def test_the_probe_unit_naming_itself_is_not_the_probes_journal_tag(tmp_path):
    """
    The skip line this phase REQUIRES names otp-unit-imgcheck.service, and
    the tag clause looks for otp-imgcheck. Those two strings do not contain
    one another -- the unit's name reads `otp-` then `unit-` -- and the tag
    is matched in speaker position anyway. A clause tuned so loosely that
    the evidence it depends on tripped it would be unable to pass.
    """
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
    # boot1 is where the probe creates `otpimgcheck` and reports it, so
    # boot1's console is the only place in a run that can say this grep works.
    quiet = [line.replace("otpimgcheck", "somequeue")
             for line in healthy("boot1")]
    proc, verdict = run_release(tmp_path, boot1_lines=quiet)
    assert check_state(verdict, "release",
                       "guest-probe-cups-queue-unnamed") == "FAIL", verdict
    assert "0 in boot1" in verdict, verdict
    assert proc.returncode == 1


# --- clause: the token was absent, not empty ------------------------------

def test_a_release_boot_handed_the_token_fails(tmp_path):
    """
    Including the empty-assignment form, which is the one that would make
    this phase test nothing at all. systemd's ConditionKernelCommandLine
    matches a bare word AND the left-hand side of an assignment, so
    `otp.imgcheck=` still satisfies the condition, still starts the unit, and
    leaves only the probe's own phase guard between a release image and a
    script that writes to /boot/firmware.
    """
    for token in ("otp.imgcheck=release", "otp.imgcheck", "otp.imgcheck="):
        lines = [line for line in healthy("release")
                 if "Kernel command line:" not in line]
        lines.insert(1, cmdline_line("release", token=token))
        proc, verdict = run_release(tmp_path / token.replace("=", "_"), lines)
        assert check_state(verdict, "release",
                           "cmdline-carries-no-imgcheck-token") == "FAIL", verdict
        assert proc.returncode == 1


def test_a_release_boot_with_no_command_line_echo_at_all_fails(tmp_path):
    # The absence has to be measured on a line that exists. A console with no
    # `Kernel command line:` on it does not carry the token either, and that
    # is not the same statement.
    proc, verdict = run_release(tmp_path, release_console(cmdline=False))
    assert check_state(verdict, "release",
                       "cmdline-carries-no-imgcheck-token") == "FAIL", verdict
    assert "no Kernel command line line" in verdict, verdict
    assert proc.returncode == 1


def test_the_probe_phases_are_required_to_carry_the_token(tmp_path):
    """
    The other direction, and what makes the release phase's absence mean
    anything: the same grep over the same kind of file finds the token in
    boot1 and boot2. A boot that never got it would run no probe and report
    nothing, which is a red the harness should name rather than leave to the
    guest gate to imply.
    """
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
    """
    Run the real `case`, rather than read it.

    The verdict clause above reads the token back off the kernel's own echo,
    which is the right place to check a BOOT. This checks the code that
    builds the command line in the first place, so a change that put the
    token back is red in the fast suite instead of sixteen minutes later.
    """
    # ONE WORD REMOVED, `local`, which only means anything inside a function
    # and is a syntax error outside one. Everything that decides the token --
    # the empty initial value and the case that leaves it that way for the
    # release phase -- is the code that ships.
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
    # EMPTY, NOT `otp.imgcheck=`: the -append line splits on whitespace, so
    # an empty variable leaves no word behind at all. An assignment with
    # nothing after it would still satisfy the unit's condition.
    append = next(line for line in IMG_BOOT.read_text().splitlines()
                  if line.strip().startswith("-append "))
    assert "$imgcheck_token" in append, append
    assert "otp.imgcheck" not in append, \
        "the token is spelled literally on the -append line, so no phase " \
        "can be without it"


# --- clause: the card came out the way it went in -------------------------

def test_a_release_phase_with_nothing_to_delete_fails(tmp_path):
    """
    THE CONTROL FOR THE DELETION.

    Both records are written by boot1 and left there by boot2, so the strip
    has something to take off. If those boots ever stop writing them, the
    release phase's "they did not come back" is an absence it was handed for
    free -- true of a working image and of a broken one alike -- and the run
    has to say so instead of counting it.
    """
    proc, verdict = run_release(
        tmp_path,
        prestrip={"release": [line for line in prestrip_listing("release")
                              if "otp-imgcheck" not in line]})
    assert check_state(verdict, "release",
                       "probe-droppings-were-on-the-card") == "FAIL", verdict
    assert "not there to delete" in verdict, verdict
    assert proc.returncode == 1
    # And the absence afterwards still reads PASS, which is the whole reason
    # the control has to exist.
    assert check_state(verdict, "release",
                       "probe-droppings-stayed-off-the-card") == "PASS", verdict


def test_a_release_boot_that_put_the_probes_records_back_fails(tmp_path):
    # The probe running is the case this is really about: it writes both of
    # these in boot1 and would write them again here.
    before, after = healthy_listings("release")
    proc, verdict = run_release(
        tmp_path, boot_files={"release": (before, after + FAT_DROPPINGS)})
    assert check_state(verdict, "release",
                       "probe-droppings-stayed-off-the-card") == "FAIL", verdict
    assert proc.returncode == 1


def test_a_strip_that_did_nothing_fails_rather_than_passing(tmp_path):
    # mdel against a wrong offset writes nothing and says nothing -- that is
    # how run 1 read a harness bug as a bad image. Both ends are required.
    before, after = healthy_listings("release")
    proc, verdict = run_release(
        tmp_path,
        boot_files={"release": (before + FAT_DROPPINGS, after + FAT_DROPPINGS)})
    assert check_state(verdict, "release",
                       "probe-droppings-stayed-off-the-card") == "FAIL", verdict
    assert "before:" in verdict, verdict
    assert proc.returncode == 1


def test_a_release_boot_that_changed_the_boot_partition_fails(tmp_path):
    """
    A release boot must not be the thing that eats the operator's login. The
    identity store, the credential and the saved settings all live on this
    partition, outside the overlay, and nothing on a boot with no
    otp.imgcheck token has any business touching them.
    """
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
    # Compared sorted, because mtools prints FAT directory order and a file
    # rewritten in place can take a freed slot. Names, not positions.
    before, after = healthy_listings("release")
    proc, verdict = run_release(
        tmp_path, boot_files={"release": (before, list(reversed(after)))})
    assert check_state(verdict, "release",
                       "boot-partition-unchanged") == "PASS", verdict
    assert proc.returncode == 0, proc.stderr + verdict


def test_a_release_boot_that_rewrote_a_kept_file_fails(tmp_path):
    """
    The listing cannot see this one: a file rewritten in place keeps its
    name. The credential is a password hash and the machine-id is what keeps
    a power-cycled appliance the same machine, so both are compared by
    content -- as digests, because this work directory is a CI artifact.
    """
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
    """
    The absence trap, in the one clause that compares values. `absent` is
    equal to `absent`, and so is the sha256 of nothing to the sha256 of
    nothing -- which is exactly how "the credential survived this boot" comes
    to mean "there has never been a credential". The harness requires 64 hex
    characters on both sides as well as a match.
    """
    gone = {name: "absent" for name in FAT_CONTENTS}
    proc, verdict = run_release(
        tmp_path, digests={"release": (dict(gone), dict(gone))})
    assert check_state(verdict, "release",
                       "identity-store-unchanged") == "FAIL", verdict
    assert proc.returncode == 1


def test_digests_that_were_never_taken_fail_and_still_produce_a_verdict(tmp_path):
    """
    TWO FAILURES IN ONE FIXTURE, and the first draft of this clause had both.

    A phase whose digest files are missing must not read as three files that
    came through the boot unchanged -- an empty comparison compares nothing
    and reports success, which is row 4 of issue #14 arriving in a new place.
    The clause is driven by the list of names rather than by the file, so
    every name has to produce 64 characters or the clause is red.

    And it must still produce a verdict at all. The loop was written as
    `while read ... done < "$file"`, which under errexit dies on a file that
    is not there -- measured, rc 1 -- and dies in the middle of writing
    verdict.txt, in exactly the case where there is least other evidence.
    """
    proc, verdict = run_release(
        tmp_path, digests={"release": ({}, {})})
    assert verdict is not None, "the verdict file was never written"
    assert check_state(verdict, "release",
                       "identity-store-unchanged") == "FAIL", verdict
    # The rest of the phase still reported, which is what says the block ran
    # to the end rather than dying part way through it.
    assert check_state(verdict, "release",
                       "boot-partition-unchanged") == "PASS", verdict
    assert proc.returncode == 1
    # Every name is accounted for in the failure, not just the first.
    for name in FAT_CONTENTS:
        assert name in verdict, (name, verdict)


# --- the harness structure the phase depends on ---------------------------

def test_the_strip_happens_before_the_listing_the_boot_is_judged_against():
    """
    Ordering, because a deletion taken after the before-listing reads as
    something the boot did -- and one taken after the boot does not happen at
    all. The three listings in the release phase's directory have to be, in
    file order: what the card held, what the emulator was handed, what came
    back.
    """
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
    """
    Two files, two scripts, and nothing but this test holding them together.

    img-boot.sh deletes $PROBE_DROPPINGS from the card; img-guest-check.sh
    writes them, in boot1, into $BOOTDIR. Rename one in the probe and the
    harness goes on deleting a file nothing creates -- the control above
    would catch that on the next CI run, sixteen minutes later, but the
    deletion would have stopped meaning anything and the phase would have
    stopped watching the write it was built around.
    """
    probe = GUEST_CHECK.read_text()
    written = set(re.findall(r'"\$BOOTDIR/(otp-imgcheck-[a-z-]+)"', probe))
    assert written, "img-guest-check.sh no longer writes any record of its own"
    assert set(PROBE_DROPPINGS) == written, (
        f"img-boot.sh strips {sorted(PROBE_DROPPINGS)} and the probe writes "
        f"{sorted(written)}")
    # And they are written in boot1, which is what puts them on the card
    # before the release phase can take them off.
    for name in PROBE_DROPPINGS:
        assert f'"$BOOTDIR/{name}"' in probe, name


def test_the_release_phase_has_a_stop_condition_of_its_own():
    """
    The sampler stops the other two phases on the probe's done line. This
    phase has no probe, so with the same marker it would burn the whole
    OTP_IMG_TIMEOUT backstop on every green run -- 600 seconds of CI, on
    every build, to observe nothing.

    The marker it uses instead is the one the verdict already gates every
    phase on, and the settle window after it is the one thing here that is
    arithmetic rather than a habit: a write the guest made into its page
    cache reaches the card once writeback runs, and the kernel's defaults for
    that are dirty_expire_centisecs=3000 and dirty_writeback_centisecs=500.
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
    # The marker is the one the verdict demands of every phase, so the
    # sampler cannot stop on evidence the verdict does not accept.
    assert '"Reached target multi-user.target"' in verdict_block()
    # And it is the sampler's own variable that is grepped for, not the
    # probe's line with a second grep bolted on beside it.
    assert 'grep -cF "$stop_marker"' in text, text

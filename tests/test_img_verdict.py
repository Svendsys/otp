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
"""
import os
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
IMG_BOOT = REPO / "harness" / "img-boot.sh"
GUEST_CHECK = REPO / "harness" / "img-guest-check.sh"
MARKER = "# Everything downstream reads the combination"

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


def kernel_lines(*, entries=1, last_ts="20.000000", first_boot=False):
    lines = []
    for _ in range(entries):
        lines.append("[    0.000000] Booting Linux on physical CPU 0x0000000000 [0x410fd034]")
    lines += [
        "[    0.100000] Linux version 6.12.96+rpt-rpi-v8 (build@host)",
        # Filtered back out by the two tests that need them missing, so
        # what they prove is the ABSENCE of the line rather than a flag.
        HWRNG,
        CRNG,
        "[    9.180895] systemd[1]: Hostname set to <otp-unit>.",
        f"[   {last_ts}] systemd[1]: Reached target sysinit.target - System Initialization.",
        multi_user_line(last_ts),
    ]
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
        lines.append(f"OTP-CHECK {phase} {name} {state} detail=whatever\r")
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


def healthy(phase):
    return (kernel_lines(first_boot=(phase == "boot1"))
            + [SUCCESS] + [journal_forwarded(phase)]
            + guest_report(phase))


# What `mdir -b` prints for the FAT partition of an image this harness can
# boot: absolute paths, one per line. kernel8.img is the load-bearing entry --
# the verdict uses it as the positive control that the listing is a listing at
# all, because every "the file is gone" below is equally true of a partition
# nothing could read.
FAT_ROOT = ["::/kernel8.img", "::/bcm2710-rpi-3-b.dtb", "::/config.txt",
            "::/cmdline.txt", "::/initramfs8", "::/otp-unit.conf"]


def healthy_listings(phase):
    """The boot partition either side of a healthy phase.

    boot1 goes in with the harness's seed and comes out without it; boot2
    goes in clean and comes out holding the quarantined malformed seed the
    guest fed to userconf-service by hand.
    """
    if phase == "boot1":
        return FAT_ROOT + ["::/userconf.txt"], list(FAT_ROOT)
    if phase == "boot2":
        return list(FAT_ROOT), FAT_ROOT + ["::/failed_userconf.txt"]
    return list(FAT_ROOT), list(FAT_ROOT)


def run_verdict(tmp_path, consoles, *, phases=("boot1",), uart1_lines=None,
                qemu_rc="124", early_stop="", boot_files=None):
    """Run the sliced block over synthetic per-phase evidence directories.

    `consoles` maps a phase name to its uart0 lines. The verdict block reads
    a phase as a directory on disk -- both consoles, qemu's exit code, whether
    the harness stopped it, and the FAT root either side of the boot -- so the
    fixture writes exactly what a real boot leaves behind.
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
    script = "\n".join(
        [
            "set -euo pipefail",
            "log() { printf 'LOG %s\\n' \"$*\" >&2; }",
            "ESC=$(printf '\\033')",
            f'WORK="{work}"',
            f'PHASES="{" ".join(phases)}"',
            'TIMEOUT="600"',
            verdict_block(),
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60
    )
    verdict = work / "verdict.txt"
    return proc, (verdict.read_text() if verdict.exists() else None)


# --- which boots the run is allowed to consist of -------------------------

PHASES_ASSIGNMENT = 'PHASES="${OTP_IMG_PHASES:-boot1 boot2}"'


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


def test_the_default_is_two_boots(tmp_path):
    # The default is the whole tier: one boot cannot observe a power-cycle.
    proc = run_phase_list(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "PHASES=boot1 boot2" in proc.stdout, proc.stdout


def test_an_empty_phase_list_falls_back_to_the_default(tmp_path):
    # `:-`, not `-`: OTP_IMG_PHASES="" is an empty run, and an empty run
    # would boot nothing at all and have no phase to fail on.
    proc = run_phase_list(tmp_path, "")
    assert proc.returncode == 0, proc.stderr
    assert "PHASES=boot1 boot2" in proc.stdout, proc.stdout


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
    for phases in ("boot1", "boot1 boot3", "noboot2", "boot22"):
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
    for phases in ("boot2", "boot2 boot3", "boot2 boot2"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode != 0, f"{phases!r} was accepted"
        assert "boot1" in phases_refused(proc), proc.stderr
        assert "PHASES=" not in proc.stdout, proc.stdout
    # And the reason is stated, not just the name: the next person to try
    # this has to be told the card is seeded whatever they asked for.
    proc = run_phase_list(tmp_path, "boot2")
    assert "seeded" in proc.stderr, proc.stderr


def test_a_phase_list_that_keeps_both_boots_is_accepted(tmp_path):
    # The positive control: a guard that refused everything would satisfy
    # the two tests above.
    for phases in ("boot1 boot2", "boot1 boot2 boot3"):
        proc = run_phase_list(tmp_path, phases)
        assert proc.returncode == 0, proc.stderr
        assert f"PHASES={phases}" in proc.stdout, proc.stdout


def test_the_two_boot_claim_is_only_made_by_a_run_that_booted_twice(tmp_path):
    # The concluding line is what a reader takes away, and image.yml's
    # release note says the same thing in its own words.
    proc, _ = run_verdict(tmp_path, {"boot1": healthy("boot1")},
                          phases=("boot1",))
    assert proc.returncode == 0, proc.stderr
    assert "boots twice" not in proc.stderr, proc.stderr
    assert "NOT the two-boot claim" in proc.stderr, proc.stderr


def test_a_two_phase_run_does_make_the_two_boot_claim(tmp_path):
    proc, _ = run_verdict(
        tmp_path, {"boot1": healthy("boot1"), "boot2": healthy("boot2")},
        phases=("boot1", "boot2"))
    assert proc.returncode == 0, proc.stderr
    assert "the image boots twice on a read-only overlay" in proc.stderr, \
        proc.stderr


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
    """
    first_user = shell_value(BUILD_SH, "FIRST_USER_NAME")
    assert shell_value(IMG_BOOT, "USERCONF_USER") == first_user
    assert shell_value(GUEST_CHECK, "USERCONF_USER") == first_user


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
                 "IMG-CHECK boot2 userconf-malformed-seed-quarantined PASS"):
        assert line in verdict, verdict
    assert proc.returncode == 0, proc.stderr + verdict


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


def test_guest_self_reset_is_explained_when_checks_fail(tmp_path):
    proc, verdict = run_verdict(tmp_path, {"boot1": kernel_lines()}, qemu_rc="0")
    assert proc.returncode == 1
    assert "the guest reset itself" in proc.stderr

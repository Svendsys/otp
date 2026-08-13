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
    if phase == "boot2":
        found += names("GUEST_CHECKS_BOOT2")
    return found


# The two entropy lines, spelled as the kernel spells them. bcm2835-rng is
# builtin, so its probe and the CRNG seeding it causes both land within the
# first three seconds -- long before the unit could ask for a pad byte.
HWRNG = "[    2.383417] bcm2835-rng 3f104000.rng: hwrng registered"
CRNG = "[    2.421905] random: crng init done"


def kernel_lines(*, entries=1, last_ts="20.000000"):
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
    ]
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


def healthy(phase):
    return kernel_lines() + [SUCCESS] + guest_report(phase)


def run_verdict(tmp_path, consoles, *, phases=("boot1",), uart1_lines=None,
                qemu_rc="124", early_stop=""):
    """Run the sliced block over synthetic per-phase evidence directories.

    `consoles` maps a phase name to its uart0 lines. The verdict block reads
    a phase as a directory on disk -- both consoles, qemu's exit code and
    whether the harness stopped it -- so the fixture writes exactly what a
    real boot leaves behind.
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
    assert emitted == set(required_checks("boot2")), (
        f"img-guest-check.sh emits {sorted(emitted)}, img-boot.sh requires "
        f"{sorted(required_checks('boot2'))}")


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
    plain = [ln for ln in kernel_lines() if "hwrng" not in ln.lower()]
    real = "[    2.401337] bcm2835-rng 3f104000.rng: hwrng registered"
    proc, verdict = run_verdict(
        tmp_path, {"boot1": plain + [real, SUCCESS] + guest_report("boot1")})
    assert "IMG-CHECK boot1 hwrng-registered PASS" in verdict, verdict
    assert "bcm2835-rng" in verdict, \
        "the note clipped the driver name off, which is the only thing " \
        "it adds over the gate: " + verdict
    assert proc.returncode == 0, proc.stderr


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


def test_guest_self_reset_is_explained_when_checks_fail(tmp_path):
    proc, verdict = run_verdict(tmp_path, {"boot1": kernel_lines()}, qemu_rc="0")
    assert proc.returncode == 1
    assert "the guest reset itself" in proc.stderr

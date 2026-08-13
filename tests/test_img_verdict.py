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
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMG_BOOT = REPO / "harness" / "img-boot.sh"
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


def run_verdict(tmp_path, uart0_lines, uart1_lines=None, qemu_rc="124", early_stop=""):
    """Run the sliced block against synthetic consoles; return the result."""
    if uart1_lines is None:
        # What the real second UART holds: early bootconsole lines that stop
        # at the ~2s handoff. Their small timestamps sit LAST in the concat,
        # which is exactly the trap "guest reached" used to fall into.
        uart1_lines = ["[    1.900000] printk: legacy bootconsole [bcm2835aux0] disabled"]
    work = tmp_path
    (work / "console.log").write_text("\n".join(uart0_lines) + ("\n" if uart0_lines else ""))
    (work / "console-uart1.log").write_text("\n".join(uart1_lines) + ("\n" if uart1_lines else ""))
    script = "\n".join(
        [
            "set -euo pipefail",
            "log() { printf 'LOG %s\\n' \"$*\"; }",
            "ESC=$(printf '\\033')",
            f'WORK="{work}"',
            f'CONSOLE="{work}/console.log"',
            f'CONSOLE2="{work}/console-uart1.log"',
            f'CONSOLE_ALL="{work}/console-all.log"',
            f'CONSOLE_TXT="{work}/console-text.log"',
            f'QEMU_RC="{qemu_rc}"',
            'TIMEOUT="600"',
            f'EARLY_STOP="{early_stop}"',
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


def test_healthy_boot_passes_on_console_evidence_despite_rc_124(tmp_path):
    proc, verdict = run_verdict(tmp_path, kernel_lines() + [SUCCESS])
    assert verdict is not None
    assert "IMG-CHECK unit-started PASS" in verdict
    assert "IMG-CHECK single-kernel-entry PASS" in verdict
    # rc 124 is the NORMAL ending of a healthy appliance boot (the guest
    # idles; only the stopwatch ends it). It must not fail the run.
    assert proc.returncode == 0, proc.stderr


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
    proc, verdict = run_verdict(tmp_path, lines + [SUCCESS])
    assert "IMG-CHECK hwrng-registered FAIL" in verdict, verdict
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
    proc, verdict = run_verdict(tmp_path, plain + [real, SUCCESS])
    assert "IMG-CHECK hwrng-registered PASS" in verdict, verdict
    assert "bcm2835-rng" in verdict, \
        "the note clipped the driver name off, which is the only thing " \
        "it adds over the gate: " + verdict
    assert proc.returncode == 0, proc.stderr


def test_a_boot_whose_crng_never_seeds_fails(tmp_path):
    # Without this line the unit has started and cannot generate: the
    # first draw blocks inside getrandom() for as long as it takes. A
    # green tier 3 over that console would be reporting a working image.
    lines = [ln for ln in kernel_lines() if "crng init done" not in ln]
    proc, verdict = run_verdict(tmp_path, lines + [SUCCESS])
    assert "IMG-CHECK crng-init-done FAIL" in verdict
    assert proc.returncode == 1


def test_guest_reached_is_the_max_not_the_concats_last_line(tmp_path):
    # uart1 (small timestamps) concatenates second. The last timestamp in
    # file order is 1.9s; the max is 20s. Run 12 reported 1.9.
    proc, verdict = run_verdict(tmp_path, kernel_lines(last_ts="20.000000") + [SUCCESS])
    assert "guest reached: 20.000000s" in verdict


def test_hostname_and_login_prompt_do_not_count_as_the_unit(tmp_path):
    lines = kernel_lines() + ["otp-unit login: "]
    proc, verdict = run_verdict(tmp_path, lines)
    assert "IMG-CHECK unit-started FAIL" in verdict
    assert proc.returncode == 1


def test_starting_line_is_not_started(tmp_path):
    lines = kernel_lines() + [
        "         Starting \x1b[0;1;39motp-unit.service\x1b[0m - OTP pad print unit...\r"
    ]
    proc, verdict = run_verdict(tmp_path, lines)
    assert "IMG-CHECK unit-started FAIL" in verdict
    assert proc.returncode == 1


def test_the_etc_cups_unit_is_not_the_unit(tmp_path):
    lines = kernel_lines() + [
        OK_PREFIX + "Finished \x1b[0;1;39motp-unit-etc-cups.service\x1b[0m - tmpfs over /etc/cups.\r",
        "Started otp-unit-etc-cups.service - tmpfs over /etc/cups.",
    ]
    proc, verdict = run_verdict(tmp_path, lines)
    assert "IMG-CHECK unit-started FAIL" in verdict
    assert proc.returncode == 1


def test_a_boot_reset_loop_fails_even_with_a_successful_lap(tmp_path):
    proc, verdict = run_verdict(tmp_path, kernel_lines(entries=2) + [SUCCESS])
    assert "IMG-CHECK single-kernel-entry FAIL" in verdict
    assert proc.returncode == 1


def test_kernel_panic_fails_a_boot_that_also_started_the_unit(tmp_path):
    lines = kernel_lines() + [SUCCESS, "[   25.000000] Kernel panic - not syncing: Attempted to kill init!"]
    proc, verdict = run_verdict(tmp_path, lines)
    assert "IMG-CHECK no-Kernel-panic FAIL" in verdict
    assert proc.returncode == 1


def test_empty_consoles_still_produce_a_verdict(tmp_path):
    # The no-evidence case: qemu never launched, or run 1's zero-byte boot.
    # An unguarded grep under pipefail+errexit used to kill the script HERE,
    # before verdict.txt existed -- the diagnosis died with it.
    proc, verdict = run_verdict(tmp_path, [], uart1_lines=[])
    assert proc.returncode == 1
    assert verdict is not None, "no verdict.txt: the verdict block died mid-way"
    assert "IMG-CHECK unit-started FAIL" in verdict
    assert "IMG-CHECK Linux-version FAIL" in verdict


def test_early_stop_is_reported_as_the_harnesss_own_hand(tmp_path):
    proc, _ = run_verdict(
        tmp_path, kernel_lines() + [SUCCESS], qemu_rc="143", early_stop="397"
    )
    assert proc.returncode == 0
    assert "stopped by the harness at 397s" in proc.stderr


def test_guest_self_reset_is_explained_when_checks_fail(tmp_path):
    proc, verdict = run_verdict(tmp_path, kernel_lines(), qemu_rc="0")
    assert proc.returncode == 1
    assert "the guest reset itself" in proc.stderr

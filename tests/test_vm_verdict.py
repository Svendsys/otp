"""The tier-2 verdict logic, tested against synthetic consoles.

The three-phase gate in `vm-check.sh` has to notice a boot that did not
happen, and a missing boot produces no output at all. These tests run the
real verdict block, sliced out of the script, against consoles built to
fail one way each.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VM_CHECK = REPO / "harness" / "vm-check.sh"
MARKER = "# --- the verdict ---"

PHASES = ("provision", "reboot", "persist")


def verdict_block() -> str:
    """The real verdict logic, from the marker to the end of the script.

    Sliced rather than duplicated: a copy would keep passing after the
    original changed.
    """
    text = VM_CHECK.read_text()
    assert MARKER in text, f"{VM_CHECK} no longer contains {MARKER!r}"
    return text[text.index(MARKER):]


def console(phases=PHASES, counts=None, done=None, fails=(), repeat=()):
    """Build a guest console transcript: a clean three-phase run by default,
    and each argument makes it worse in one specific way."""
    counts = counts or {}
    done = PHASES if done is None else done
    lines = ["OTP-INSTALL rc=0"]
    for phase in phases:
        total = 14
        passed = counts.get(phase, total)
        for i in range(passed):
            lines.append(f"OTP-CHECK {phase} check-{i} PASS fine")
        for name in fails:
            lines.append(f"OTP-CHECK {phase} {name} FAIL broken")
        reps = 2 if phase in repeat else 1
        for _ in range(reps):
            lines.append(f"OTP-RESULT {phase} {passed}/{total}")
        if phase in done:
            lines.append(f"OTP-GUEST-DONE {phase}")
    # A serial console emits CRLF, so every line carries a trailing CR.
    return "".join(line + "\r\n" for line in lines)


def run_verdict(tmp_path, transcript, qemu_rc=0, qemu_stderr=""):
    work = tmp_path / "work"
    work.mkdir()
    (work / "console.log").write_text(transcript)
    (work / "qemu-stderr.log").write_text(qemu_stderr)
    script = tmp_path / "verdict.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            set -uo pipefail
            WORK={work}
            CONSOLE="$WORK/console.log"
            QEMU_ERR="$WORK/qemu-stderr.log"
            QEMU_RC={qemu_rc}
            BOOT_TIMEOUT=2400
            log() {{ printf '\\n== %s\\n' "$*" >&2; }}
            """
        )
        + verdict_block()
    )
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, timeout=60
    )


class TestTheGatePasses:
    def test_a_clean_three_phase_run_passes(self, tmp_path):
        result = run_verdict(tmp_path, console())
        assert result.returncode == 0, result.stderr
        assert "all checks passed" in result.stderr


class TestTheGateFails:
    """Each case is one way a run is broken; a zero exit means the gate aimed
    at it has stopped discriminating."""

    @pytest.mark.parametrize("missing", PHASES)
    def test_a_phase_that_never_reported_fails(self, tmp_path, missing):
        """A run missing a whole phase fails and names the phase. Dropping
        `persist` is the realistic case: the guest reboots and never comes
        back."""
        kept = [p for p in PHASES if p != missing]
        result = run_verdict(tmp_path, console(phases=kept, done=kept))
        assert result.returncode != 0, (
            f"a run missing the {missing} phase entirely was accepted"
        )
        assert missing in result.stderr

    def test_a_phase_that_started_but_never_finished_fails(self, tmp_path):
        """Results printed, no OTP-GUEST-DONE: killed mid-phase."""
        result = run_verdict(
            tmp_path, console(done=("provision", "reboot"))
        )
        assert result.returncode != 0, "an unfinished phase was accepted"
        assert "persist" in result.stderr

    def test_a_phase_with_a_failing_check_fails(self, tmp_path):
        result = run_verdict(
            tmp_path, console(counts={"reboot": 13}, fails=("unit-holds-tty1",))
        )
        assert result.returncode != 0, "13/14 was accepted as a pass"

    def test_a_fail_line_fails_even_when_the_counts_agree(self, tmp_path):
        """A FAIL line fails the run even when the counts agree, because the
        counts come from the guest, which is the thing under test."""
        result = run_verdict(tmp_path, console(fails=("unit-holds-tty1",)))
        assert result.returncode != 0, "a FAIL line was accepted"

    def test_a_phase_reporting_twice_fails(self, tmp_path):
        """A phase that reports twice fails: a failed state write loops the
        self-rebooting runner on one phase."""
        result = run_verdict(tmp_path, console(repeat=("reboot",)))
        assert result.returncode != 0, "a boot loop was accepted"
        assert "2x" in result.stderr

    def test_a_nonzero_qemu_exit_fails(self, tmp_path):
        """A nonzero qemu exit fails the run (124 is the timeout) even when
        the transcript looks complete."""
        result = run_verdict(tmp_path, console(), qemu_rc=124)
        assert result.returncode != 0, "a killed qemu was accepted"

    def test_a_silent_guest_fails(self, tmp_path):
        result = run_verdict(tmp_path, "no markers here at all\r\n")
        assert result.returncode != 0, "a guest that never reported was accepted"


class TestASharedSerialConsole:
    """The guest and the kernel share one serial port, so a kernel message can
    land inside or after a marker."""

    def test_a_marker_with_a_kernel_message_after_it_is_still_read(self, tmp_path):
        """Noise that lands after the marker must not hide it."""
        noise = "[  104.9] systemd-ssh-generator[5039]: Failed to query CID"
        transcript = console().replace(
            "OTP-RESULT persist 14/14",
            "OTP-RESULT persist 14/14" + noise,
        )
        result = run_verdict(tmp_path, transcript)
        assert result.returncode == 0, result.stderr

    def test_a_marker_with_a_timestamp_prefix_is_still_read(self, tmp_path):
        """Every guest line arrives prefixed by cloud-init and a timestamp,
        so the marker is never at the start of a line."""
        transcript = "".join(
            f"[   92.2] cloud-init[505]: {line}\r\n"
            for line in console().replace("\r\n", "\n").split("\n")
            if line
        )
        result = run_verdict(tmp_path, transcript)
        assert result.returncode == 0, result.stderr

    def test_a_marker_split_mid_word_still_fails(self, tmp_path):
        """A marker split mid-word is not salvaged into a pass: guessing at a
        truncated phase name would invent a result the guest never
        reported."""
        transcript = console().replace(
            "OTP-RESULT persist 14/14",
            "OTP-RESULT persi[  104.9] systemd-ssh-generator: Failed",
        )
        result = run_verdict(tmp_path, transcript)
        assert result.returncode != 0, "a split marker was read as a pass"
        assert "persist" in result.stderr

    def test_a_marker_missing_its_counts_fails(self, tmp_path):
        """`OTP-RESULT persist` with no numbers is not a result."""
        transcript = console().replace(
            "OTP-RESULT persist 14/14", "OTP-RESULT persist"
        )
        result = run_verdict(tmp_path, transcript)
        assert result.returncode != 0, "a marker with no counts was accepted"


class TestTheFailurePathSaysWhy:
    """A failing gate has to say why. When qemu dies before the guest boots
    there is no console, and the only explanation is qemu's stderr."""

    def test_an_empty_console_surfaces_qemu_stderr(self, tmp_path):
        boom = (
            "qemu-system-x86_64: -drive file=bootpart.img,if=virtio,"
            "format=raw,serial=otpboot: Block format 'raw' does not "
            "support the option 'serial'"
        )
        result = run_verdict(tmp_path, "", qemu_rc=1, qemu_stderr=boom + "\n")
        assert result.returncode != 0
        assert "Block format 'raw'" in result.stderr, (
            "qemu's stderr is the only evidence there is when the console is "
            "empty, and it was not printed"
        )
        assert "EMPTY" in result.stderr

    def test_qemu_stderr_is_shown_even_when_the_guest_did_report(self, tmp_path):
        """A warning that did not stop the boot is still worth seeing."""
        result = run_verdict(
            tmp_path, console(), qemu_stderr="warning: something odd\n"
        )
        assert result.returncode == 0, result.stderr
        assert "something odd" in result.stderr


class TestTheGateIsNotFooledByWhitespace:
    def test_crlf_does_not_break_the_count_comparison(self, tmp_path):
        """The count comparison survives CRLF line endings; named separately
        because the difference is invisible in a log."""
        transcript = console()
        assert "\r\n" in transcript
        assert run_verdict(tmp_path, transcript).returncode == 0

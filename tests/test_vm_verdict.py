"""The tier-2 verdict logic, tested against synthetic consoles.

WHY THIS EXISTS. Every audit round so far has found a harness check that
could not fail: a boot offset aimed at a zero-filled gap, a grep for the
image's hostname, a spool check that was really a systemd invariant, a
truncated-run gate that passed a truncated run. Each was found by hand,
mutated by hand, and confirmed by hand -- and the next one was found the
same way, one round later.

`vm-check.sh` grew a three-phase gate, and the whole value of three boots
rests on that gate noticing when a boot did not happen. A boot that never
happens produces no output, which is indistinguishable from a boot with
nothing to say. So the gate is the load-bearing part, and it is exactly the
kind of code that quietly stops discriminating.

These tests run the real verdict block -- sliced out of the real script, not
a copy of it -- against consoles built to fail one way each. A gate that
has stopped discriminating fails here rather than two months from now on a
green run that meant nothing.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VM_CHECK = REPO / "harness" / "vm-check.sh"
MARKER = "# --- the verdict ---"

PHASES = ("provision", "overlay", "persist")


def verdict_block() -> str:
    """The real verdict logic, from the marker to the end of the script.

    Sliced rather than duplicated. A copy of this logic in the test file
    would go on passing after somebody changed the original -- which is the
    same defect as a rig carrying its own copy of MaxJobs.
    """
    text = VM_CHECK.read_text()
    assert MARKER in text, f"{VM_CHECK} no longer contains {MARKER!r}"
    return text[text.index(MARKER):]


def console(phases=PHASES, counts=None, done=None, fails=(), repeat=()):
    """Build a guest console transcript.

    Defaults to a clean three-phase run; every argument makes it worse in
    one specific way.
    """
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
    # A serial console emits CRLF. Every line really does arrive with a
    # trailing \r, and that has broken this script's comparisons before.
    return "".join(line + "\r\n" for line in lines)


def run_verdict(tmp_path, transcript, qemu_rc=0):
    work = tmp_path / "work"
    work.mkdir()
    (work / "console.log").write_text(transcript)
    script = tmp_path / "verdict.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            set -uo pipefail
            WORK={work}
            CONSOLE="$WORK/console.log"
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
    """Each of these is a way the run is broken. If any returns 0, the gate
    it is aimed at has stopped discriminating."""

    @pytest.mark.parametrize("missing", PHASES)
    def test_a_phase_that_never_reported_fails(self, tmp_path, missing):
        """The gate this whole three-boot design rests on.

        Dropping `persist` is the realistic case: the guest reboots, does
        not come back, and the transcript ends looking tidy. Under the
        single-phase form this replaces -- `tail -1` of the result line --
        that run passed on the strength of the previous phase.
        """
        kept = [p for p in PHASES if p != missing]
        result = run_verdict(tmp_path, console(phases=kept, done=kept))
        assert result.returncode != 0, (
            f"a run missing the {missing} phase entirely was accepted"
        )
        assert missing in result.stderr

    def test_a_phase_that_started_but_never_finished_fails(self, tmp_path):
        """Results printed, no OTP-GUEST-DONE: killed mid-phase."""
        result = run_verdict(
            tmp_path, console(done=("provision", "overlay"))
        )
        assert result.returncode != 0, "an unfinished phase was accepted"
        assert "persist" in result.stderr

    def test_a_phase_with_a_failing_check_fails(self, tmp_path):
        result = run_verdict(
            tmp_path, console(counts={"overlay": 13}, fails=("root-is-overlay",))
        )
        assert result.returncode != 0, "13/14 was accepted as a pass"

    def test_a_fail_line_fails_even_when_the_counts_agree(self, tmp_path):
        """Belt and braces: the counts come from the guest, which is the
        thing under test. A guest that miscounts must not be able to talk
        its way to a green run."""
        result = run_verdict(tmp_path, console(fails=("root-is-overlay",)))
        assert result.returncode != 0, "a FAIL line was accepted"

    def test_a_phase_reporting_twice_fails(self, tmp_path):
        """The runner reboots itself. A failed state write loops it on one
        phase forever, and 'it reported' stays true of a machine doing
        nothing else."""
        result = run_verdict(tmp_path, console(repeat=("overlay",)))
        assert result.returncode != 0, "a boot loop was accepted"
        assert "2x" in result.stderr

    def test_a_nonzero_qemu_exit_fails(self, tmp_path):
        """124 is the timeout. The transcript can look complete right up to
        where the axe fell."""
        result = run_verdict(tmp_path, console(), qemu_rc=124)
        assert result.returncode != 0, "a killed qemu was accepted"

    def test_a_silent_guest_fails(self, tmp_path):
        result = run_verdict(tmp_path, "no markers here at all\r\n")
        assert result.returncode != 0, "a guest that never reported was accepted"


class TestTheGateIsNotFooledByWhitespace:
    def test_crlf_does_not_break_the_count_comparison(self, tmp_path):
        """A trailing \\r once made "13" and "13\\r" unequal and failed a run
        in which everything passed. The transcripts here all carry CRLF, so
        the clean-run test above already covers this -- but it is named
        because the regression is cheap to reintroduce and expensive to
        diagnose from a log where the difference is invisible."""
        transcript = console()
        assert "\r\n" in transcript
        assert run_verdict(tmp_path, transcript).returncode == 0

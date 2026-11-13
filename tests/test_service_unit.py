"""Static checks that the systemd unit and the provisioning script agree. systemd
refuses to start a unit whose SupplementaryGroups name a missing group, and
with Restart=on-failure and a volatile journal that is a dark panel with no
evidence.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

UNIT = REPO / "device" / "systemd" / "otp-unit.service"
INSTALL = REPO / "device" / "install.sh"


def unit_text() -> str:
    return UNIT.read_text()


def directive(name: str) -> str | None:
    """The last value systemd would use for a directive, or None."""
    found = None
    for line in unit_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name}="):
            found = stripped.split("=", 1)[1].strip()
    return found


def seconds(value: str) -> float:
    """systemd time spans: bare digits are seconds, suffixes are not."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|sec|min|m|h)?", value.strip())
    if not match:
        return -1.0
    scale = {None: 1, "s": 1, "sec": 1, "ms": 0.001,
             "min": 60, "m": 60, "h": 3600}[match.group(2)]
    return float(match.group(1)) * scale


def declared_groups() -> list:
    """The groups otp-unit.service will refuse to start without."""
    found = []
    for line in unit_text().splitlines():
        if line.startswith("SupplementaryGroups="):
            found += line.split("=", 1)[1].split()
    return found


class TestTheUnitCanActuallyStart:
    def test_the_service_declares_supplementary_groups(self):
        # If this ever becomes empty the test below passes vacuously.
        assert declared_groups(), "no SupplementaryGroups to check"

    def test_install_sh_creates_every_group_the_service_needs(self, tmp_path):
        """Every group named in the unit is one install.sh really creates.

        The script is run with groupadd and getent stubbed onto PATH, not
        pattern-matched, so an empty loop body cannot pass.
        """
        script = INSTALL.read_text()
        block = re.search(r"log \"Ensuring the groups.*?\ndone\n", script, re.S)
        assert block, "install.sh no longer has a group-creation block"

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        log = tmp_path / "asked"
        # getent always says "missing" so every group takes the create path.
        (bin_dir / "getent").write_text("#!/bin/sh\nexit 2\n")
        (bin_dir / "groupadd").write_text(
            f'#!/bin/sh\nfor a in "$@"; do case "$a" in -*) ;; *) '
            f'echo "$a" >> {log} ;; esac; done\n')
        for stub in ("getent", "groupadd"):
            (bin_dir / stub).chmod(0o755)

        runner = tmp_path / "run.sh"
        runner.write_text("set -eu\nlog() { :; }\n" + block.group(0))
        subprocess.run(["bash", str(runner)], check=True,
                       env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})

        created = set(log.read_text().split()) if log.exists() else set()
        missing = [g for g in declared_groups() if g not in created]
        assert not missing, (
            f"otp-unit.service needs {missing} but install.sh does not "
            f"create them; systemd fails the unit with 216/GROUP and "
            f"Restart=on-failure loops it forever. Created: {created}")

    def test_the_unit_still_bans_core_dumps_itself(self):
        """The unit sets LimitCORE=0. coredump.conf only binds when
        systemd-coredump is the registered handler, so the unit-level limit is
        the one that holds regardless.
        """
        # Parsed, not a substring search: a commented-out `#LimitCORE=0` must
        # not pass.
        assert directive("LimitCORE") == "0", (
            f"LimitCORE is {directive('LimitCORE')!r}, so the process can "
            f"dump the pad it is holding")

    def test_nothing_can_stop_the_panel_by_starting_a_getty_on_tty1(self):
        """The unit has no Conflicts= on the tty1 getty, and install.sh keeps the
        login prompt off tty1 with a condition on the getty instead. Conflicts=
        is symmetric: anything starting that getty (userconf-pi does) would
        stop the panel.
        """
        # Parsed, not searched: the unit's own comments mention the directive.
        assert directive("Conflicts") is None, (
            f"Conflicts={directive('Conflicts')} is back: a getty started "
            f"by cancel-rename after a credential apply stops the panel")
        # The replacement must be in install.sh, or removing the conflict hands
        # tty1 to the login prompt.
        install = INSTALL.read_text()
        assert "/etc/systemd/system/getty@tty1.service.d" in install, (
            "install.sh writes no drop-in for getty@tty1, so with Conflicts= "
            "gone there is nothing keeping a login prompt off the panel's tty")
        # The condition keys on the panel going to run, so a masked panel still
        # gets a getty. tests/test_overlay_root.py has the truth table.
        for directive_line in (
                "ConditionPathExists=|!/etc/systemd/system/"
                "multi-user.target.wants/otp-unit.service",
                "ConditionFileNotEmpty=|!/etc/systemd/system/otp-unit.service"):
            assert directive_line in install, (
                f"install.sh no longer writes {directive_line!r}, so the "
                f"getty drop-in does not say what it is conditional on")

    def test_restart_on_failure_is_paired_with_a_backoff(self):
        # Without RestartSec above systemd's burst limit, a unit that cannot
        # start goes permanently `failed` after five tries in ten seconds.
        assert directive("Restart") == "on-failure"
        # Seconds, honouring systemd's suffixes: `500ms` must not read as 500
        # and `1min` must pass.
        raw = directive("RestartSec") or ""
        assert seconds(raw) >= 10, (
            f"RestartSec={raw!r} is under systemd's 5-in-10s burst limit, "
            f"so a unit that cannot start goes permanently failed")

"""The systemd unit and the provisioning script have to agree.

These are static checks over two files, so they cost nothing and run in
the fast suite -- but the defect they guard against is only observable on
a booted system, which is the expensive place to find it.

What was found there: otp-unit.service names four supplementary groups and
install.sh created none of them. systemd refuses to start a unit whose
SupplementaryGroups reference a group that does not exist --

    Failed at step GROUP spawning /usr/bin/python3
    Main process exited, code=exited, status=216/GROUP

-- and with Restart=on-failure the unit loops forever. On a device whose
journal is volatile by design, that is a dark panel and no evidence.
Raspberry Pi OS ships gpio and i2c so the intended platform worked, which
is exactly why nothing caught it.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

UNIT = REPO / "device" / "systemd" / "otp-unit.service"
INSTALL = REPO / "device" / "install.sh"


def unit_text() -> str:
    return UNIT.read_text()


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

    def test_install_sh_creates_every_group_the_service_needs(self):
        """
        Every group named in the unit must be one install.sh guarantees.

        Not "exists on the developer's machine" -- guaranteed by the
        script, because the script is what runs on a fresh image.
        """
        script = INSTALL.read_text()
        # The loop that fills in missing groups; take its word list.
        match = re.search(r"for group in ([^;\n]+); do", script)
        assert match, "install.sh no longer creates groups"
        created = set(match.group(1).split())
        missing = [g for g in declared_groups() if g not in created]
        assert not missing, (
            f"otp-unit.service needs {missing} but install.sh does not "
            f"create them; systemd will fail the unit with 216/GROUP and "
            f"Restart=on-failure will loop it forever")

    def test_the_unit_still_bans_core_dumps_itself(self):
        """
        LimitCORE=0 is what actually protects THIS process.

        install.sh also sets Storage=none in coredump.conf, but that only
        binds when systemd-coredump is the registered handler -- measured
        in a booted VM, kernel.core_pattern was plain `core`, so dumps
        would have gone to a file in the working directory and the
        coredump.conf would never have been consulted. The unit-level
        limit is the one that holds regardless.
        """
        assert "LimitCORE=0" in unit_text()

    def test_restart_on_failure_is_paired_with_a_backoff(self):
        # Without RestartSec above systemd's burst limit, a unit that
        # cannot start goes permanently `failed` after five tries in ten
        # seconds -- which on this device means it never comes back.
        text = unit_text()
        assert "Restart=on-failure" in text
        match = re.search(r"RestartSec=(\d+)", text)
        assert match and int(match.group(1)) >= 10, \
            "Restart=on-failure needs a RestartSec above the burst limit"

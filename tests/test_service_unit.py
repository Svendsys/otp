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
        """
        Every group named in the unit must be one install.sh really creates.

        RUN the script rather than pattern-match it. The first version of
        this test matched the text of a `for group in ...; do` header and
        never looked at the body: replacing the body with `:` -- which
        leaves the device with no groups and the unit in a permanent
        restart loop -- still reported 4 passed. It was also brittle in the
        wrong direction, failing on a hoisted variable or a wrapped line,
        both behaviourally identical.

        So: stub groupadd and getent onto PATH, run install.sh far enough
        to execute the loop, and see which groups it actually asks for.
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
        """
        LimitCORE=0 is what actually protects THIS process.

        install.sh also sets Storage=none in coredump.conf, but that only
        binds when systemd-coredump is the registered handler -- measured
        in a booted VM, kernel.core_pattern was plain `core`, so dumps
        would have gone to a file in the working directory and the
        coredump.conf would never have been consulted. The unit-level
        limit is the one that holds regardless.
        """
        # Parsed, not a substring search over the whole file: commenting
        # the directive out (`#LimitCORE=0`) satisfied `in unit_text()`
        # while systemd ignored the line entirely.
        assert directive("LimitCORE") == "0", (
            f"LimitCORE is {directive('LimitCORE')!r}, so the process can "
            f"dump the pad it is holding")

    def test_restart_on_failure_is_paired_with_a_backoff(self):
        # Without RestartSec above systemd's burst limit, a unit that
        # cannot start goes permanently `failed` after five tries in ten
        # seconds -- which on this device means it never comes back.
        assert directive("Restart") == "on-failure"
        # Seconds, honouring systemd's suffixes. A bare \\d+ regex read
        # `RestartSec=500ms` as 500 and passed it -- five restarts in 2.5s
        # is inside the default burst window, so the unit goes permanently
        # `failed`, which is exactly what this test says it prevents. The
        # same regex failed `RestartSec=1min`, which is correct.
        raw = directive("RestartSec") or ""
        assert seconds(raw) >= 10, (
            f"RestartSec={raw!r} is under systemd's 5-in-10s burst limit, "
            f"so a unit that cannot start goes permanently failed")

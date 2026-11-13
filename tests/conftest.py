"""Shared test setup: keep the suite off the host's hardware RNG, its CUPS daemon
and its filesystem, so results do not depend on the machine.
"""
import os
import tempfile
from pathlib import Path

import pytest

# otp_generator reads the environment at import time, so set this first.
os.environ.setdefault("OTP_HWRNG_PATH", "/nonexistent/hwrng-under-test")


@pytest.fixture(autouse=True)
def no_real_hwrng(monkeypatch):
    """Pin the fake hwrng path per-test as well as at import."""
    import otp_generator

    if otp_generator.HWRNG_PATH == "/dev/hwrng":
        monkeypatch.setattr(otp_generator, "HWRNG_PATH",
                            "/nonexistent/hwrng-under-test")


@pytest.fixture(autouse=True)
def no_real_cups(monkeypatch, request):
    """Make Cups' default runner refuse, so a method a double does not override
    cannot reach the host's daemon. printer.py treats the exception as "cannot
    tell", the answer a machine with no CUPS gives.

    Not applied to the hardware harness, nor to tests marked
    `real_cups_runner`, which stub subprocess.run themselves.
    """
    if request.node.get_closest_marker("hardware") \
            or request.node.get_closest_marker("real_cups_runner"):
        return
    from otpunit import printer

    def refuse(argv, stdin=None):
        raise OSError(
            f"the fast suite must not run {argv[0]}: this Cups double left "
            f"a method unstubbed and fell through to the host's CUPS. Stub "
            f"it, or use the hardware harness.")

    monkeypatch.setattr(printer.Cups, "_subprocess_run", staticmethod(refuse))


@pytest.fixture(autouse=True)
def settings_stay_in_tmp(monkeypatch):
    """Fail if a settings file lands outside the temp area.

    Guarded on the outcome, not the attempt: some tests save to a genuinely
    unwritable path to prove save() returns False.
    """
    from otpunit import config, ui

    temp_root = Path(tempfile.gettempdir()).resolve()
    real = config.save

    def guarded(settings, path=config.CONFIG_PATH, remount=True):
        resolved = Path(path).resolve()
        result = real(settings, path, remount)
        if temp_root not in resolved.parents and resolved.is_file():
            resolved.unlink()                    # do not leave it behind
            raise AssertionError(
                f"a test wrote a settings file to {resolved}, outside "
                f"{temp_root}. Use tmp_path -- an absolute path that sounds "
                f"unwritable is writable when the suite runs as root.")
        return result

    monkeypatch.setattr(config, "save", guarded)
    # ui.py does `from .config import save`, so it holds its own reference.
    monkeypatch.setattr(ui, "save", guarded)

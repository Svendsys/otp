"""Shared test setup.

Two things worth doing globally.

**Keep the suite off the machine's real hardware RNG.** Key material now
comes from /dev/hwrng XORed with the CSPRNG, which is correct for a
one-time pad but slow -- a throttled virtio-rng manages about 6 KiB/s, and
the suite generates megabytes of key material. Left alone it drains the
device and the run takes minutes instead of seconds, with the timing
depending on whatever host the tests happen to run on.

So the default here is "no hardware RNG", which is also what CI runners
and most laptops look like. tests/test_entropy.py points HWRNG_PATH at
fakes it controls, which is the only place the mixing logic needs to be
exercised.

**Keep the suite off the machine's real CUPS.** Every Cups double in the
suite is built as `printer.Cups(run=None)` -- and `run=None` means "use the
real subprocess runner", so any method the double does not override shells
out to the host's `lp`/`lpstat`. That was harmless only for as long as
nothing in the fast path called such a method. The moment RunJob started
consulting `printer_fault`, a machine with a real `OTP` queue in a fault
state failed eleven tests, including the control that asserts a healthy
pair still completes. The doubles were never the problem; the default was.

**Keep the suite off the machine's real filesystem.** Several tests build
an App with config_path="/nonexistent", meaning "somewhere we will never
write". Running as root -- which CI and every container do -- that path is
perfectly writable, so any test that reached SAVE SETTINGS created a real
/nonexistent file at the root of the host. It did, for months. A path that
merely sounds unwritable is not a guarantee, so this asserts the real one.
"""
import os
import tempfile
from pathlib import Path

import pytest

# Set before otp_generator is imported, since it reads the environment at
# import time to pick its default.
os.environ.setdefault("OTP_HWRNG_PATH", "/nonexistent/hwrng-under-test")


@pytest.fixture(autouse=True)
def no_real_hwrng(monkeypatch):
    """Belt and braces: pin it per-test as well as at import."""
    import otp_generator

    if otp_generator.HWRNG_PATH == "/dev/hwrng":
        monkeypatch.setattr(otp_generator, "HWRNG_PATH",
                            "/nonexistent/hwrng-under-test")


@pytest.fixture(autouse=True)
def no_real_cups(monkeypatch, request):
    """
    Make Cups' default runner refuse, so a stubbed-out method cannot
    silently reach the host's daemon.

    Refusing is safe by construction: every query in printer.py already
    treats an exception as "cannot tell" -- `_text` returns "", `_run_text`
    returns None -- which is exactly the answer a machine with no CUPS
    gives. The change is that the answer stops depending on whether the
    developer's laptop happens to be running cupsd, which is the whole
    point of a hermetic suite.

    Not applied to the hardware harness, which runs a real cupsd on
    purpose. Nor to a test marked `real_cups_runner`, of which there is
    one: the timeout guard in test_mutation_survivors.py has to call
    _subprocess_run for real to see the kwargs it passes, and stubs
    subprocess.run itself so nothing leaves the process.
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
    """
    Fail loudly if a settings file appears outside the temp area.

    Guarded on the outcome, not on the attempt: some tests point save() at
    a genuinely unwritable path (/proc/...) precisely to prove it returns
    False rather than raising, and those must keep working. What must never
    happen is a file actually landing on the host, which is the failure
    that went unnoticed.
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

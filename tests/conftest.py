"""Shared test setup.

The one thing worth doing globally: keep the suite off the machine's real
hardware RNG.

Key material now comes from /dev/hwrng XORed with the CSPRNG, which is
correct for a one-time pad but slow -- a throttled virtio-rng manages
about 6 KiB/s, and the suite generates megabytes of key material. Left
alone it drains the device and the run takes minutes instead of seconds,
with the timing depending on whatever host the tests happen to run on.

So the default here is "no hardware RNG", which is also what CI runners
and most laptops look like. tests/test_entropy.py points HWRNG_PATH at
fakes it controls, which is the only place the mixing logic needs to be
exercised.
"""
import os

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

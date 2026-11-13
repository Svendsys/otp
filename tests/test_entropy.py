"""Where key material comes from: the hardware TRNG mixed with the CSPRNG. These
tests cover the silent failure modes: a TRNG that is absent, short, slow or
stuck, and a mix that drops one of its inputs.
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import otp_generator as gen


@pytest.fixture
def hwrng(tmp_path):
    """A fake /dev/hwrng whose contents the test controls."""
    path = tmp_path / "hwrng"

    def write(data):
        path.write_bytes(data)
        return str(path)
    return write


class TestTheHardwareSourceIsUsed:
    def test_hwrng_bytes_are_read_when_present(self, hwrng):
        path = hwrng(b"\xa5" * 4 + bytes(range(60)))
        assert gen._hwrng_bytes(8, path) == b"\xa5\xa5\xa5\xa5\x00\x01\x02\x03"

    def test_the_source_is_reported_honestly(self, hwrng, monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(bytes(range(256))))
        assert gen.entropy_source(gen.HWRNG_PATH) == "hwrng+urandom"
        assert gen.entropy_source("/nonexistent/hwrng") == "urandom"

    def test_output_mixes_both_sources(self, hwrng, monkeypatch):
        # Pin both inputs and check the output is exactly their XOR. The
        # hardware side must vary: constant bytes are rejected as a stuck TRNG.
        hardware = bytes(range(32))
        system = bytes([0x33] * 32)
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(hardware))
        monkeypatch.setattr(gen.os, "urandom", lambda n: system[:n])
        assert gen.get_random_bytes(32) == bytes(i ^ 0x33 for i in range(32))

    def test_length_is_exact_even_when_the_xor_has_leading_zeros(self, hwrng,
                                                                monkeypatch):
        # Identical inputs XOR to all zeros, the worst case for losing leading
        # zero bytes in an int round-trip.
        same = bytes(range(64))
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(same))
        monkeypatch.setattr(gen.os, "urandom", lambda n: same[:n])
        out = gen.get_random_bytes(64)
        assert len(out) == 64
        assert out == bytes(64)


class TestItFailsSafeRatherThanQuietly:
    def test_no_hwrng_falls_back_to_urandom(self, monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", "/nonexistent/hwrng")
        data = gen.get_random_bytes(64)
        assert len(data) == 64
        assert len(set(data)) > 1

    def test_a_short_read_is_refused_rather_than_padded(self, hwrng):
        # A short return would leave the caller filling key material from
        # elsewhere without knowing.
        path = hwrng(b"only-eight")
        assert gen._hwrng_bytes(4096, path) is None

    def test_a_stuck_trng_is_not_trusted(self, hwrng):
        # Constant output silently accepted produces a pad that is not a pad.
        assert gen._hwrng_bytes(256, hwrng(bytes(256))) is None
        assert gen._hwrng_bytes(256, hwrng(b"\xff" * 256)) is None

    def test_a_stuck_trng_still_yields_usable_key_material(self, hwrng,
                                                          monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(bytes(4096)))
        data = gen.get_random_bytes(512)
        assert len(data) == 512
        assert len(set(data)) > 16, "must fall back, not emit zeros"

    def test_a_slow_trng_gives_up_instead_of_blocking(self, monkeypatch):
        """A multi-chunk read from a slow device stops at the deadline and returns
        None. The fake returns exactly n bytes, so only the deadline can reject
        the result.
        """
        class SlowFile:
            def read(self, n):
                clock[0] += 1.0
                return bytes(range(256)) * (n // 256) + bytes(n % 256)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        clock = [0.0]
        monkeypatch.setattr(gen, "HWRNG_TIMEOUT", 3.0)
        monkeypatch.setattr(gen.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr("builtins.open", lambda *a, **k: SlowFile())
        # Many chunks at one simulated second each, against a 3s budget.
        assert gen._hwrng_bytes(gen._HWRNG_CHUNK * 100) is None

    def test_the_deadline_bounds_a_device_that_stops_answering(self, tmp_path,
                                                               monkeypatch):
        """A device that opens but never has data (a FIFO here) is abandoned at
        the deadline. Only O_NONBLOCK makes that possible: a blocking read
        never returns, and select() reports /dev/hwrng ready unconditionally.
        """
        import os as real_os
        import time as real_time

        fifo = tmp_path / "hwrng.fifo"
        real_os.mkfifo(fifo)
        # Held open read-write so the reader's open() does not block; no
        # data is ever written, so every read returns EAGAIN.
        holder = real_os.open(fifo, real_os.O_RDWR | real_os.O_NONBLOCK)
        try:
            monkeypatch.setattr(gen, "HWRNG_TIMEOUT", 0.3)
            start = real_time.monotonic()
            assert gen._hwrng_bytes(4096, str(fifo)) is None
            elapsed = real_time.monotonic() - start
            assert elapsed < 5, f"took {elapsed:.1f}s against a 0.3s budget"
        finally:
            real_os.close(holder)

    def test_the_deadline_is_checked_for_a_single_chunk_request(self,
                                                                monkeypatch):
        """The deadline applies to a request that fits in one chunk, which is
        every request this program makes (an A6 page is 1340 bytes against a
        4096 chunk).
        """
        seen = []
        real_monotonic = gen.time.monotonic

        def clock():
            seen.append(1)
            return real_monotonic()

        monkeypatch.setattr(gen.time, "monotonic", clock)
        gen._hwrng_bytes(1340, "/dev/zero")
        assert seen, "the deadline must be evaluated for a 1340-byte request"

    def test_a_fast_trng_is_not_cut_off_early(self, hwrng, monkeypatch):
        # The converse: a device that keeps up must be read in full.
        size = gen._HWRNG_CHUNK * 4
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(os.urandom(size)))
        data = gen._hwrng_bytes(size)
        assert data is not None and len(data) == size, \
            "a device that answers immediately must not hit the deadline"

    def test_a_live_trng_passes_the_health_check(self):
        assert gen._looks_alive(os.urandom(256))
        assert not gen._looks_alive(bytes(256))

    def test_an_unreadable_device_is_not_fatal(self, tmp_path, monkeypatch):
        # /dev/hwrng exists but the process lacks permission, as when run as
        # non-root.
        path = tmp_path / "hwrng"
        path.write_bytes(os.urandom(64))
        path.chmod(0o000)
        monkeypatch.setattr(gen, "HWRNG_PATH", str(path))
        try:
            assert len(gen.get_random_bytes(32)) == 32
        finally:
            path.chmod(0o644)


class TestTheLettersStayUnbiased:
    """Mixing sources must not disturb the rejection sampling that keeps A-Z
    uniform.
    """

    def test_letters_are_still_uniform_over_a_large_sample(self, hwrng,
                                                           monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(os.urandom(1 << 16)))
        text = gen.generate_random_letters(26_000)
        counts = {chr(65 + i): text.count(chr(65 + i)) for i in range(26)}
        expected = 26_000 / 26
        # Generous band: a smoke test for gross bias, not a statistical proof.
        assert all(abs(n - expected) < expected * 0.25 for n in counts.values()), counts
        assert set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def test_letters_are_uniform_with_no_hwrng_either(self, monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", "/nonexistent/hwrng")
        text = gen.generate_random_letters(26_000)
        expected = 26_000 / 26
        counts = [text.count(chr(65 + i)) for i in range(26)]
        assert all(abs(n - expected) < expected * 0.25 for n in counts)

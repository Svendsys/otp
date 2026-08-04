"""Where key material actually comes from.

A one-time pad is only a one-time pad if its key came from a physical
process. Anything expanded algorithmically from a seed -- however good the
algorithm, and os.urandom's ChaCha20 is very good -- is a stream cipher by
definition. Every Pi has a hardware TRNG, so the unit uses it.

These tests are about the failure modes that are silent: a TRNG that is
absent, short, or stuck, and a mix that quietly drops one of its inputs.
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
        # Pin both inputs and check the output is exactly their XOR, so a
        # refactor that drops one of them fails here rather than silently
        # halving where the key comes from. Note the hardware side has to
        # VARY -- constant bytes are rejected as a stuck TRNG, which is
        # the health check doing its job.
        hardware = bytes(range(32))
        system = bytes([0x33] * 32)
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(hardware))
        monkeypatch.setattr(gen.os, "urandom", lambda n: system[:n])
        assert gen.get_random_bytes(32) == bytes(i ^ 0x33 for i in range(32))

    def test_length_is_exact_even_when_the_xor_has_leading_zeros(self, hwrng,
                                                                monkeypatch):
        # int.from_bytes/to_bytes round-trips lose leading zero bytes if
        # the width is not pinned. Identical inputs XOR to all zeros,
        # which is the worst case for that bug.
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
        # Returning fewer bytes than asked would leave the caller filling
        # key material from somewhere else without knowing.
        path = hwrng(b"only-eight")
        assert gen._hwrng_bytes(4096, path) is None

    def test_a_stuck_trng_is_not_trusted(self, hwrng):
        # The failure that matters: constant output, silently accepted,
        # produces a pad that is not a pad.
        assert gen._hwrng_bytes(256, hwrng(bytes(256))) is None
        assert gen._hwrng_bytes(256, hwrng(b"\xff" * 256)) is None

    def test_a_stuck_trng_still_yields_usable_key_material(self, hwrng,
                                                          monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(bytes(4096)))
        data = gen.get_random_bytes(512)
        assert len(data) == 512
        assert len(set(data)) > 16, "must fall back, not emit zeros"

    def test_a_slow_trng_gives_up_instead_of_blocking(self, tmp_path,
                                                       monkeypatch):
        """
        TRNG throughput varies by two orders of magnitude -- a Pi's does
        ~100 KiB/s, a throttled virtio-rng ~6 KiB/s. A 1000-page pad needs
        ~700 KB, so an unbounded read is minutes of a printer doing
        nothing with no way to tell why.

        The deadline has to be checked DURING the read, not between reads:
        a single large read blocks in the kernel until satisfied, which
        measured 73 seconds against a 1-second budget before the reads
        were chunked.
        """
        class SlowFile:
            def __init__(self, *args, **kwargs):
                self.served = 0

            def read(self, n):
                # Every chunk costs "time"; the fake clock does the rest.
                clock[0] += 1.0
                self.served += n
                return bytes(range(256)) * (n // 256 + 1)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        clock = [0.0]
        monkeypatch.setattr(gen, "HWRNG_TIMEOUT", 3.0)
        monkeypatch.setattr(gen.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr("builtins.open", lambda *a, **k: SlowFile())
        # Far more than the budget allows at one chunk per simulated
        # second, so it must abandon the request.
        assert gen._hwrng_bytes(gen._HWRNG_CHUNK * 100) is None

    def test_a_fast_trng_is_not_cut_off_early(self, hwrng, monkeypatch):
        # The converse of the test above: a device that keeps up must be
        # read in full, or the deadline would be quietly costing every
        # unit its hardware entropy.
        size = gen._HWRNG_CHUNK * 4
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(os.urandom(size)))
        data = gen._hwrng_bytes(size)
        assert data is not None and len(data) == size, \
            "a device that answers immediately must not hit the deadline"

    def test_a_live_trng_passes_the_health_check(self):
        assert gen._looks_alive(os.urandom(256))
        assert not gen._looks_alive(bytes(256))

    def test_an_unreadable_device_is_not_fatal(self, tmp_path, monkeypatch):
        # /dev/hwrng exists but the unit lacks permission -- common when
        # something is run as a non-root user.
        path = tmp_path / "hwrng"
        path.write_bytes(os.urandom(64))
        path.chmod(0o000)
        monkeypatch.setattr(gen, "HWRNG_PATH", str(path))
        try:
            assert len(gen.get_random_bytes(32)) == 32
        finally:
            path.chmod(0o644)


class TestTheLettersStayUnbiased:
    """
    Mixing sources must not disturb the rejection sampling that keeps A-Z
    uniform -- the property the whole scheme rests on.
    """

    def test_letters_are_still_uniform_over_a_large_sample(self, hwrng,
                                                           monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", hwrng(os.urandom(1 << 16)))
        text = gen.generate_random_letters(26_000)
        counts = {chr(65 + i): text.count(chr(65 + i)) for i in range(26)}
        expected = 26_000 / 26
        # Generous band; this is a smoke test for gross bias, not a
        # statistical proof.
        assert all(abs(n - expected) < expected * 0.25 for n in counts.values()), counts
        assert set(text) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def test_letters_are_uniform_with_no_hwrng_either(self, monkeypatch):
        monkeypatch.setattr(gen, "HWRNG_PATH", "/nonexistent/hwrng")
        text = gen.generate_random_letters(26_000)
        expected = 26_000 / 26
        counts = [text.count(chr(65 + i)) for i in range(26)]
        assert all(abs(n - expected) < expected * 0.25 for n in counts)

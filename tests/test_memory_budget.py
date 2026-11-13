"""What the largest pad this unit offers costs a 512MB board with no swap.

`otpunit/jobs.py` renders the whole PDF into one in-memory buffer and holds it
across both copies, so that A and B are byte-identical and nothing is written
to a file. The failure guarded against is the unit being OOM-killed mid-job,
with a volatile journal and nothing to read afterwards.

The budget is an allocation from the board, not "current usage plus ten
percent":

      512 MiB   Pi Zero 2 W
    -  64 MiB   VideoCore split (firmware default; config.txt names no gpu_mem)
      448 MiB   what Linux has; install.sh removes swap
    - 160 MiB   kernel, systemd, cupsd and its filters, the tmpfs spool
      288 MiB   available to the unit process

`PROCESS_CEILING_MIB` gives 128 of those to a job the panel can ask for
(`PAGE_CHOICES` tops out at 1000). The full 288 is the looser bound for
`MAX_PAGES`, reachable only by hand-editing otp-unit.conf. `PER_PAGE_KIB`
bounds the per-page slope.

A6 is the worst format: A4 and A7 impose four and two pad pages per sheet, so
the same pad is fewer PDF page objects. `test_a6_is_still_the_worst_format`
fails if that inverts.

Peaks are read from `VmHWM` in /proc/self/status, not `ru_maxrss`: on exec the
kernel folds the parent's peak into the child's ru_maxrss, so a child of a
large pytest reports pytest.

Every child is pinned to a nonexistent `OTP_HWRNG_PATH` unless it tests the
TRNG path; a throttled host /dev/hwrng makes generation orders of magnitude
slower. A subprocess inherits the environment, not the conftest fixture.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from otpunit import config                        # noqa: E402

# Every measurement reads /proc/self/status, so skip on other platforms.
pytestmark = pytest.mark.skipif(
    not Path("/proc/self/status").exists(),
    reason="the memory budget is measured through /proc, which is Linux-only")

# --- the budget ----------------------------------------------------------

BOARD_MIB = 512
GPU_SPLIT_MIB = 64
SYSTEM_ALLOWANCE_MIB = 160
LINUX_MIB = BOARD_MIB - GPU_SPLIT_MIB
AVAILABLE_MIB = LINUX_MIB - SYSTEM_ALLOWANCE_MIB

#: What a job the panel can ask for may peak at.
PROCESS_CEILING_MIB = 128
# Marginal cost per pad page, measured at about 18.5 KiB. 25 leaves headroom
# for reportlab version drift while still failing a doubling of per-page
# retention.
PER_PAGE_KIB = 25

NO_TRNG = "/nonexistent/hwrng-under-test"

# Kills a child instead of hanging the suite. Short enough that the sum over
# all children stays under CI's own timeout, which would kill the run with no
# test name.
CHILD_TIMEOUT = 60


# --- measuring -----------------------------------------------------------

_REPORT = r"""
import json, os, resource, sys

def rss_mib():
    # VmHWM, NOT resource.getrusage(RUSAGE_SELF).ru_maxrss.
    #
    # ru_maxrss in a subprocess is not this process's high-water mark. On
    # exec the kernel folds the OLD mm's peak into signal->maxrss
    # (setmax_mm_hiwater_rss in exec_mmap), and getrusage returns the max
    # of that and the current mm -- so a child forked from a large parent
    # reports the PARENT. Measured directly: with pytest at 310 MiB, a
    # child that allocated nothing reported ru_maxrss 310.3 MiB and VmHWM
    # 7.7 MiB.
    #
    # This is not academic. Written with ru_maxrss, the pad measurement
    # passed on its own file and reported base == peak == 63.8 MiB under
    # the full suite -- the same number for a 250-page pad and a 1000-page
    # one, because both were really pytest's own footprint.
    # test_a_large_parent_does_not_inflate_the_reading pins it.
    #
    # VmHWM is the current mm's peak only, so exec resets it.
    for line in open("/proc/self/status"):
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024.0
    raise AssertionError("no VmHWM in /proc/self/status")

def rusage_mib():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

sys.path.insert(0, {repo!r})
{body}
"""

_PAD_BODY = r"""
from otpunit import config, jobs
base = rss_mib()
settings = config.Settings(pages={pages}, paper={paper!r}, a7={a7!r},
                           training={training!r}, auth_size={auth_size},
                           with_auth=True)
import otp_generator
otp_generator.TALLY.reset()
buffer = jobs.generate(jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                                    settings))
peak = rss_mib()
# Which source actually served the draws. Reported so a test that means to
# exercise the TRNG path can CHECK that it did: _hwrng_bytes returns None
# on an OSError, a short read or a _looks_alive rejection, and
# get_random_bytes then falls back to os.urandom with no signal at all. A
# comparison of "with TRNG" against "without" is otherwise satisfied by
# comparing the CSPRNG path against itself.
print(json.dumps({{"base": base, "peak": peak, "pdf": len(buffer),
                   "rusage": rusage_mib(),
                   "hardware_draws": otp_generator.TALLY.hardware,
                   "software_draws": otp_generator.TALLY.software,
                   "chars_per_page": settings.chars_per_page}}))
"""

_ALLOCATION_BODY = r"""
base = rss_mib()
# Touched, not just reserved: an untouched bytearray is still resident
# under CPython, but writing to it removes any doubt.
block = bytearray({mib} * 1024 * 1024)
block[::4096] = b"x" * (len(block) // 4096 + (1 if len(block) % 4096 else 0))
# FREED before sampling, deliberately. A high-water mark still reports it;
# a reading of CURRENT residency does not. That is the entire difference
# between VmHWM and the VmRSS line one row above it in /proc/self/status,
# and without this `del` the swap between the two passes every assertion in
# this file -- while quietly understating the pad's real peak by 5% at 1000
# pages and 12% at 4000, because reportlab frees its intermediates before
# generate() returns.
del block
peak = rss_mib()
print(json.dumps({{"base": base, "peak": peak, "pdf": {mib} * 1024 * 1024,
                   "rusage": rusage_mib(), "chars_per_page": 0}}))
"""


def child(body: str, hwrng: str = NO_TRNG) -> dict:
    """Run `body` in a fresh interpreter; return its peak RSS report.

    A subprocess, because the peak is a high-water mark and pytest has already
    imported reportlab, pypdf and every test module.
    """
    # Prepended, not assigned: the runner's PYTHONPATH may carry the
    # dependencies.
    environment = dict(os.environ, OTP_HWRNG_PATH=hwrng,
                       PYTHONPATH=os.pathsep.join(
                           p for p in (str(REPO), os.environ.get("PYTHONPATH"))
                           if p))
    result = subprocess.run(
        [sys.executable, "-c", _REPORT.format(repo=str(REPO), body=body)],
        capture_output=True, timeout=CHILD_TIMEOUT, env=environment,
        cwd=str(REPO))
    if result.returncode != 0:
        raise AssertionError(
            "the measurement child died; nothing was measured:\n"
            + result.stderr.decode("utf-8", "replace")[-2000:])
    # Parse the last line, and say what the child printed if it will not parse.
    out = result.stdout.decode("utf-8", "replace")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise AssertionError(
            f"the measurement child printed something that is not the "
            f"report ({exc}).\nstdout:\n{out[-2000:]}\nstderr:\n"
            + result.stderr.decode("utf-8", "replace")[-2000:]) from exc


def pad(pages: int, paper: str = "A6", a7: bool = False,
        hwrng: str = NO_TRNG, training: bool = True,
        auth_size: int = max(config.AUTH_SIZE_CHOICES)) -> dict:
    """One pad, measured in a fresh interpreter.

    The defaults are the expensive end of every panel-reachable setting, not
    config.Settings()'s defaults: `training` stamps a watermark on every page
    and `auth_size` lengthens the draw.
    """
    return child(_PAD_BODY.format(pages=pages, paper=paper, a7=a7,
                                  training=training, auth_size=auth_size),
                 hwrng)


_SUBMIT_BODY = r"""
from otpunit import config, jobs
base = rss_mib()
settings = config.Settings(pages={pages}, paper="A6", training=True,
                           auth_size=max(config.AUTH_SIZE_CHOICES))

class Recording:
    # Enough of Cups for PadPairJob, and nothing that leaves the process.
    def __init__(self):
        self.sizes = []
    def submit(self, data, name="OTP", title="OTP", options=None):
        # The line that matters: printer.Cups.submit hands `bytes(data)` to
        # subprocess, which is a SECOND full copy of the pad held across the
        # call. Reproduced here rather than mocked away.
        blob = bytes(data)
        self.sizes.append(len(blob))
        return "job-1"
    def purge(self, name="OTP"):
        pass

cups = Recording()
job = jobs.PadPairJob(jobs.JobSpec(jobs.JobKind.PAD_PAIR, "RUSTED-BADGER",
                                   settings), cups, "OTP")
job.generate()
after_generate = rss_mib()
# tracemalloc for the COPY COUNT, VmHWM for the process ceiling. They are
# different questions and RSS can only answer the second one.
#
# The obvious form -- compare the process high-water mark before and after
# the submits -- cannot count copies. VmHWM only ever rises, so the old
# `peak >= after_generate` was true by construction. Rebasing it with
# /proc/self/clear_refs looked like the fix and measured 1.00 pads here,
# but CI measured 0.00 for the same code: generate frees a great deal on
# its way out, so the allocator can serve bytes(data) from arena space
# that is ALREADY resident and no new page is ever faulted. Whether a copy
# shows up in RSS is a property of the allocator's free lists, not of how
# many copies are live.
#
# tracemalloc counts Python-level allocation and is exact. Verified across
# a mutation: holding 1, 2 and 3 copies per submit reports 1.00, 2.00 and
# 3.00 pads, on the nose.
import tracemalloc
tracemalloc.start()
traced_base, _ = tracemalloc.get_traced_memory()
job.print_next_copy()
job.print_next_copy()
_, traced_peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
peak = max(after_generate, rss_mib())
job.finish()
print(json.dumps({{"base": base, "peak": peak, "pdf": cups.sizes[0],
                   "after_generate": after_generate, "copies": len(cups.sizes),
                   "submit_bytes": traced_peak - traced_base,
                   "rusage": rusage_mib(), "chars_per_page": 0}}))
"""


@pytest.fixture(scope="module")
def measured():
    """The expensive measurements, taken once per module and shared; nothing
    mutates the result.

    All three formats are measured at the same page count out of one dict, so
    only the format differs.
    """
    return {
        "a6-250": pad(250),
        "a6-1000": pad(1000),
        "a4-1000": pad(1000, paper="A4"),
        "a7-1000": pad(1000, a7=True),
        "submit-1000": child(_SUBMIT_BODY.format(pages=1000)),
    }


# --- the budget, asserted ------------------------------------------------


def test_the_largest_pad_the_panel_offers_fits_the_process_budget(measured):
    """1000 A6 pages, the worst supported case, peaks under the process ceiling.
    """
    seen = measured["a6-1000"]
    assert seen["peak"] < PROCESS_CEILING_MIB, (
        f"a {max(config.PAGE_CHOICES)}-page A6 pad peaked at "
        f"{seen['peak']:.1f} MiB against a {PROCESS_CEILING_MIB} MiB budget "
        f"on a board with {AVAILABLE_MIB} MiB to spare and no swap. "
        f"Read this module's docstring before raising the number.")


def test_the_whole_job_fits_it_too_and_not_just_the_generate(measured):
    """Submitting holds about one extra copy of the pad live (`Cups.submit` passes
    `bytes(data)` to subprocess), never several.

    Counted with tracemalloc over the submit phase: RSS cannot see a copy the
    allocator serves from arena space generate() just freed.
    """
    seen = measured["submit-1000"]
    assert seen["copies"] == 2, "a pad is a PAIR; both copies must be submitted"
    copies_live = seen["submit_bytes"] / seen["pdf"]
    # The floor matters as much as the ceiling: at zero the measurement has
    # stopped seeing the copy.
    assert 0.5 < copies_live < 1.5, (
        f"the submit phase allocated {copies_live:.2f} copies of the "
        f"{seen['pdf'] / 1048576:.2f} MiB pad; one is expected -- the "
        f"bytes(data) handed to subprocess. Below 0.5 the measurement is "
        f"not seeing the copy at all; above 1.5 submit is holding more of "
        f"the pad live than it needs to.")
    assert seen["peak"] < PROCESS_CEILING_MIB, (
        f"the full pad-pair job peaked at {seen['peak']:.1f} MiB against a "
        f"{PROCESS_CEILING_MIB} MiB budget")


def test_the_measurement_saw_a_pad_and_not_an_empty_buffer(measured):
    """The measured PDF is at least 500 bytes per pad page (A6 is ~2250), so a job
    that generated little or nothing cannot satisfy the budget tests.
    """
    seen = measured["a6-1000"]
    assert seen["pdf"] > 1000 * 500, seen
    assert seen["peak"] > seen["base"], \
        "peak RSS never rose above the import baseline; nothing was built"


def test_the_per_page_cost_stays_flat(measured):
    """The marginal cost per pad page stays under PER_PAGE_KIB. A slope between
    two sizes catches per-page retention and is stable across reportlab
    versions.
    """
    small, large = measured["a6-250"], measured["a6-1000"]
    per_page_kib = (large["peak"] - small["peak"]) * 1024 / (1000 - 250)
    assert per_page_kib < PER_PAGE_KIB, (
        f"{per_page_kib:.1f} KiB per pad page against a {PER_PAGE_KIB} KiB "
        f"budget (measured ~17.8 when this was written)")


def test_the_hand_edited_page_ceiling_still_fits_the_board(measured):
    """A pad at `MAX_PAGES`, reachable by hand-editing otp-unit.conf, projects to
    less than the board has available: it need not fit the process budget, only
    not kill the board.
    """
    small, large = measured["a6-250"], measured["a6-1000"]
    per_page_mib = (large["peak"] - small["peak"]) / (1000 - 250)
    projected = large["base"] + config.MAX_PAGES * per_page_mib
    assert projected < AVAILABLE_MIB, (
        f"pages={config.MAX_PAGES} projects to {projected:.0f} MiB against "
        f"{AVAILABLE_MIB} MiB available. Either MAX_PAGES comes down to "
        f"{max(config.PAGE_CHOICES)} or the pair stops being buffered whole "
        f"-- and the second one needs care, since byte-identity is what "
        f"makes A and B a pair.")


def test_a6_is_still_the_worst_format(measured):
    """A6 peaks highest of the three formats, so the headline test measures the
    worst case. Taken at 1000 pages, where the separation (several MiB) is far
    above RSS noise.
    """
    peaks = {
        "A6": measured["a6-1000"]["peak"],
        "A4": measured["a4-1000"]["peak"],
        "A7": measured["a7-1000"]["peak"],
    }
    runner_up = max(v for k, v in peaks.items() if k != "A6")
    # Strict, by a stated margin: a tie means A6 is no longer the worst case.
    # 2.0 MiB is well above RSS noise and well below the measured separation.
    assert peaks["A6"] > runner_up + 2.0, peaks


def test_a_present_trng_does_not_change_the_shape_of_the_cost():
    """With a hardware RNG present, which is what ships, the peak matches the
    no-TRNG path.

    A regular file stands in for /dev/hwrng. The tally of hardware bytes is
    asserted, because every way of failing to reach the fake falls back to
    os.urandom silently and would compare the CSPRNG path with itself.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".hwrng") as fake:
        fake.write(os.urandom(4 * 1024 * 1024))
        fake.flush()
        with_trng = pad(250, hwrng=fake.name)
    without = pad(250)

    assert with_trng["hardware_draws"] > 0, (
        "the TRNG path was never taken, so this compared the CSPRNG "
        "against itself: " + repr(with_trng))
    assert with_trng["software_draws"] == 0, (
        f"{with_trng['software_draws']} draws fell back to the CSPRNG; the "
        f"fake device was not serving all of them")
    assert without["hardware_draws"] == 0, (
        "the no-TRNG control found hardware anyway, so NO_TRNG is not "
        "pointing at a nonexistent path: " + repr(without))
    assert without["software_draws"] > 0, without

    assert with_trng["peak"] < PROCESS_CEILING_MIB
    # Within a megabyte of each other. Measured identical to 0.1 MiB.
    assert abs(with_trng["peak"] - without["peak"]) < 1.0, \
        (with_trng, without)


# --- the guard on the guard ----------------------------------------------


def test_the_measurement_can_actually_fail():
    """`child()` sees a child that allocates a known 192 MiB. That is above
    PROCESS_CEILING_MIB, so the ceiling is a number the measurement can exceed.
    """
    seen = child(_ALLOCATION_BODY.format(mib=192))
    assert seen["peak"] > 160, \
        f"a child that allocated 192 MiB was measured at {seen['peak']} MiB"
    assert seen["peak"] > PROCESS_CEILING_MIB, \
        "the ceiling is unreachable by this measurement, so it guards nothing"


def test_a_large_parent_does_not_inflate_the_reading():
    """A child of a large parent reports its own peak. `ru_maxrss` would report
    the parent's, because exec folds the outgoing mm's peak into it; `VmHWM`
    does not.
    """
    small = child("peak = base = rss_mib()\n"
                  'print(json.dumps({"base": base, "peak": peak, "pdf": 0,'
                  ' "rusage": rusage_mib(), "chars_per_page": 0}))')
    # Make this process genuinely large, then measure the same tiny child.
    hog = bytearray(256 * 1024 * 1024)
    hog[::4096] = b"x" * (len(hog) // 4096)
    try:
        after = child("peak = base = rss_mib()\n"
                      'print(json.dumps({"base": base, "peak": peak,'
                      ' "pdf": 0, "rusage": rusage_mib(),'
                      ' "chars_per_page": 0}))')
    finally:
        del hog
    assert after["peak"] < 3 * small["peak"], (
        f"a 256 MiB parent moved an empty child's reading from "
        f"{small['peak']:.1f} to {after['peak']:.1f} MiB; the measurement "
        f"is of the wrong process")
    # And prove the trap is real, so nobody simplifies this back to ru_maxrss.
    assert after["rusage"] > 200, (
        "ru_maxrss no longer inherits the parent's peak on this kernel; "
        f"got {after['rusage']:.1f} MiB from a 256 MiB parent. If that is "
        f"genuinely fixed, this test and the VmHWM comment can go.")


def test_a_child_that_dies_is_not_read_as_a_small_footprint():
    """A crashed child prints nothing, and that must raise, not parse as a low
    peak.
    """
    with pytest.raises(AssertionError, match="nothing was measured"):
        child("raise SystemExit(3)")


def test_the_budget_arithmetic_is_stated_and_consistent():
    # The numbers in the docstring and the constants are the same numbers.
    assert LINUX_MIB == 448 and AVAILABLE_MIB == 288
    assert PROCESS_CEILING_MIB < AVAILABLE_MIB, \
        "the process budget must leave room for the rest of the system"
    assert max(config.PAGE_CHOICES) == 1000, \
        "the panel's largest offer changed; re-derive the budget"
    assert config.MAX_PAGES == 9999, \
        "the hand-edited ceiling changed; re-derive the projection"

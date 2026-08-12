"""What the largest pad this unit offers costs a 512MB board with no swap.

`otpunit/jobs.py` renders the whole PDF into one in-memory buffer and holds
it for the duration of BOTH copies -- deliberately, because copies A and B
must be byte-identical, and because writing it to a file is the thing this
project exists to avoid. Nothing measured what that costs, and the panel
offers `pages = 1000`.

The failure this guards against is not a slow print. It is the unit being
OOM-killed part-way through a job on a board with no swap, leaving no
evidence: the journal is volatile by design, so the operator gets a printer
that stopped and nothing to read.

THE BUDGET, and it is a budget rather than a measurement
--------------------------------------------------------

Written as an allocation from the board so that it cannot ratchet upward
every time someone makes the code worse. A ceiling set to "current usage
plus ten percent" is not a ceiling.

    512 MiB   Pi Zero 2 W, and there is no other variant
  -  64 MiB   the VideoCore split, taken by the firmware before Linux sees
              a byte. 64 is the firmware default when config.txt names no
              gpu_mem, and nothing in this repository names one. If the
              real split is smaller the error is in the safe direction.
  ---------
    448 MiB   what Linux has, all of it (device/install.sh:110-118 removes
              dphys-swapfile, so there is no second chance)
  - 160 MiB   everything that is not the unit: kernel, systemd, journald,
              udev, dbus, cupsd AND its filter chain -- plus the tmpfs
              spool, which holds each submitted copy in RAM, and the
              filters' scratch under /run/cups/tmp
  ---------
    288 MiB   available to the unit process

`PROCESS_CEILING_MIB` allocates 128 of those 288 to a job the PANEL can
ask for -- `PAGE_CHOICES` tops out at 1000. That leaves 160 MiB of margin
against a figure that is itself conservative, and it is 2.9x what the job
measures today, so it is a tripwire for a real regression rather than for
a font change.

`BOARD_MIB` minus the two deductions is the looser bound, and it applies
to `MAX_PAGES` -- 9999, reachable only by hand-editing otp-unit.conf on
the SD card. Two tiers on purpose: the supported configuration gets the
tight budget, the hand-edited one only has to not kill the board.

MEASURED here, no TRNG, x86-64, CPython + reportlab, peak VmHWM in MiB.
Every figure is at the EXPENSIVE end of each panel-reachable setting --
`training` stamps a rotated watermark on every page and `auth_size` takes
its largest choice, together worth about 1.3 MiB at 1000 pages:

    format   100 pages   250 pages   1000 pages   PDF at 1000
    A6           28.0        31.0         45.0       2.15 MiB
    A4           27.6        29.6         39.9       1.67 MiB
    A7           27.2        28.9         36.7       1.24 MiB
    (interpreter + imports alone: 26.0 MiB)

And generate() is not the end of it: `Cups.submit` hands `bytes(data)` to
subprocess, a second full copy held across the call, once per copy. That
extra copy does NOT raise the process high-water mark at these sizes --
measured, the whole PadPairJob peaks at exactly the generate figure,
because the submit copy fits under reportlab's own transient peak. It is
still real, and it is still measured: the submit phase is timed against a
REBASED VmHWM (see _SUBMIT_BODY) and costs 1.00 pads of live memory over
what generate leaves resident. An earlier version of this file claimed
"about 4% above" from a reading that could not distinguish the two.

A6 is the worst case and not by accident: A4 and A7 impose four and two
pad pages onto one sheet, so a 1000-page pad is 250 or 500 PDF page
objects against A6's 1000. `test_a6_is_still_the_worst_format` exists so
that if that ever inverts, the headline test gets retargeted by a red run
rather than quietly measuring the second-worst case.

The slope is ~18.5 KiB per pad page and flat, which is the number that
generalises: at MAX_PAGES it projects to about 210 MiB, inside the 288.
`PER_PAGE_KIB` is the budget for it and is deliberately near the
measurement rather than double it -- see the constant.

WHAT NOT TO MEASURE IT WITH
---------------------------

`resource.getrusage(RUSAGE_SELF).ru_maxrss` in a subprocess -- the obvious
choice, and the one this was first written with -- does not measure the
subprocess. On exec the kernel folds the outgoing mm's peak into
`signal->maxrss`, so a child forked from a large parent reports the
PARENT. It passed when this file was run alone and reported base == peak
== 63.8 MiB for BOTH a 250-page and a 1000-page pad under the full suite,
which is pytest's own footprint wearing the pad's name. `VmHWM` from
/proc/self/status is the current mm's peak only and is reset by exec.
Two tests pin this; see `test_a_large_parent_does_not_inflate_the_reading`.

A NOTE ON TIMING, because it cost an afternoon
----------------------------------------------

Every child below is pinned to a NONEXISTENT `OTP_HWRNG_PATH` unless it is
deliberately testing the TRNG path. Left alone, `_hwrng_bytes` opens the
host's real /dev/hwrng, and on a throttled virtio-rng -- which is what a
CI runner or a Firecracker VM has -- it spends its whole time in the
EAGAIN sleep loop. Measured here: the identical 1000-page pad took 2.7
seconds pinned and 205 seconds unpinned. The peak RSS was the same to
within 1%, so the measurement was never wrong; the suite just took three
and a half minutes to say so. tests/conftest.py pins the parent for the
same reason, but a subprocess inherits the environment, not the fixture.
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

# Every measurement here reads /proc/self/status, and the ru_maxrss test
# asserts a Linux exec detail outright. Without this the whole module turned
# into nine hard failures on macOS or Windows, each reported as "the
# measurement child died; nothing was measured" -- a misleading diagnosis
# for what is really an unsupported platform. tests/test_simulated_hardware
# guards its kernel dependencies the same way.
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
#: Marginal cost per pad page. Measured repeatedly at 18.4-18.5 KiB on this
#: machine (250 -> 1000 pages), and this is the growth guard -- the one that
#: generalises past the sizes actually measured.
#:
#: 25, not 40. At 40 the budget sat at 2.17x the measured slope, so a
#: regression that DOUBLED per-page retention -- unambiguously "it now keeps
#: something per page" -- landed at 36 KiB and passed, and its 1000-page
#: peak of ~61 MiB also cleared the 128 MiB ceiling. Nothing in the file
#: went red. 25 leaves 35% headroom over the measurement, which covers
#: reportlab version drift without covering a doubling.
PER_PAGE_KIB = 25

NO_TRNG = "/nonexistent/hwrng-under-test"

# Killing the child rather than hanging the suite.
#
# 60, not 300. The failure this bounds is the unpinned-TRNG one in the
# module docstring: 205 seconds for a 1000-page pad against 2.7 pinned. At
# 300 no single child could exceed the timeout, so instead the eight
# pad-generating children summed to roughly 640 seconds and CI's own
# `timeout 600 pytest -q` killed the whole run with no test name and no
# output -- a hang that loses its diagnosis, which is the outcome the
# timeout exists to prevent. 60 is still 20x the pinned figure and lands
# well below the documented bad case.
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

def rss_now_mib():
    # VmRSS: what is resident RIGHT NOW, not the high-water mark. The peak
    # answers "how much did this ever need"; this answers "how much is
    # still held", which is the question when the next phase is about to
    # add to it.
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024.0
    raise AssertionError("no VmRSS in /proc/self/status")

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

    A subprocess and not this one, because ru_maxrss is a high-water mark
    that never comes down: pytest has already imported reportlab, pypdf and
    every test module, so measuring in-process would report the suite's
    peak and call it the pad's.
    """
    # PREPENDED, not assigned. Overwriting PYTHONPATH discarded whatever
    # the runner had set -- a vendored dependency directory, a tox path --
    # and the child then failed its reportlab import and reported "the
    # measurement child died" for a reason with nothing to do with memory.
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
    # The LAST line, and a diagnosis if it will not parse. A child that
    # exits 0 having also written something else to stdout -- a deprecation
    # notice routed there, a progress reporter reintroduced -- otherwise
    # raised a bare JSONDecodeError naming neither the child nor what it
    # printed.
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

    The defaults are the EXPENSIVE end of every panel-reachable setting,
    not config.Settings()'s defaults: `training` stamps a rotated
    watermark on every page and `auth_size` lengthens the draw, and the
    first version of this file varied only pages/paper/a7 while its
    docstring claimed to measure the worst supported case. Measured at
    1000 A6 pages: 43.6 plain, 44.7 with training, 45.0 with both.
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
# REBASE the high-water mark before the submits, so what follows measures
# the submit phase and not the tallest thing reportlab did on the way here.
#
# Without this the submit test could not fail. VmHWM only ever rises, so
# `peak >= after_generate` is true by construction -- and it measured 0.00
# MiB of difference at 1000 pages, against a docstring claiming 1.8. A
# regression to three live copies per submit stayed comfortably under
# generate's own transient peak and every assertion passed.
#
# clear_refs "5" resets VmHWM to the current VmRSS (Linux 4.0+). Verified
# in this environment: 128.3 -> 8.3 immediately after freeing a 120 MiB
# buffer.
resident_after_generate = rss_now_mib()
open("/proc/self/clear_refs", "w").write("5")
job.print_next_copy()
job.print_next_copy()
submit_peak = rss_mib()
peak = max(after_generate, submit_peak)
job.finish()
print(json.dumps({{"base": base, "peak": peak, "pdf": cups.sizes[0],
                   "after_generate": after_generate, "copies": len(cups.sizes),
                   "submit_peak": submit_peak,
                   "resident_after_generate": resident_after_generate,
                   "rusage": rusage_mib(), "chars_per_page": 0}}))
"""


@pytest.fixture(scope="module")
def measured():
    """The expensive measurements, taken once and shared.

    Module-scoped deliberately: a 1000-page pad is ~2.7 seconds, and three
    tests want the same number. Nothing here mutates the result.
    """
    return {
        "a6-250": pad(250),
        "a6-1000": pad(1000),
        "submit-1000": child(_SUBMIT_BODY.format(pages=1000)),
    }


# --- the budget, asserted ------------------------------------------------


def test_the_largest_pad_the_panel_offers_fits_the_process_budget(measured):
    """The headline. 1000 A6 pages, the worst supported case."""
    seen = measured["a6-1000"]
    assert seen["peak"] < PROCESS_CEILING_MIB, (
        f"a {max(config.PAGE_CHOICES)}-page A6 pad peaked at "
        f"{seen['peak']:.1f} MiB against a {PROCESS_CEILING_MIB} MiB budget "
        f"on a board with {AVAILABLE_MIB} MiB to spare and no swap. "
        f"Read this module's docstring before raising the number.")


def test_the_whole_job_fits_it_too_and_not_just_the_generate(measured):
    """
    generate() is not the process peak, and the headline above measures
    only generate(). `Cups.submit` hands `bytes(data)` to subprocess -- a
    SECOND full copy of the pad, held live across the call -- and it does
    that once per copy.

    Measured against the SUBMIT PHASE IN ISOLATION, with VmHWM rebased
    after generate (see _SUBMIT_BODY). At 1000 pages the pad is 2.23 MiB
    and the phase costs 2.23 MiB over what generate left resident: exactly
    one extra copy, live at its widest point.

    The earlier form of this test could not fail. It asserted
    `peak >= after_generate` on a high-water mark, which only ever rises --
    true by construction. At 1000 pages the two were EQUAL to the
    hundredth of a MiB, because the submit copy fits under reportlab's own
    transient peak, and the docstring's "44.6 against 46.4" did not
    reproduce. A regression to three live copies passed every assertion.
    """
    seen = measured["submit-1000"]
    assert seen["copies"] == 2, "a pad is a PAIR; both copies must be submitted"
    pad_mib = seen["pdf"] / (1024.0 * 1024.0)
    extra = seen["submit_peak"] - seen["resident_after_generate"]
    # One pad of headroom, plus half a pad of slack for interpreter noise
    # and the page-granularity of RSS. Two copies would be 2.0 pads.
    assert extra < 1.5 * pad_mib, (
        f"the submit phase needed {extra:.2f} MiB on top of the "
        f"{seen['resident_after_generate']:.1f} MiB generate left resident, "
        f"which is {extra / pad_mib:.2f} copies of a {pad_mib:.2f} MiB pad. "
        f"submit is holding more of the pad live than the one bytes() copy "
        f"it hands to subprocess.")
    assert extra > 0.5 * pad_mib, (
        f"the submit phase cost only {extra:.2f} MiB for a {pad_mib:.2f} MiB "
        f"pad. bytes(data) should copy it, so either submit stopped being "
        f"called or this measurement stopped seeing it -- both make the "
        f"bound above vacuous.")
    assert seen["peak"] < PROCESS_CEILING_MIB, (
        f"the full pad-pair job peaked at {seen['peak']:.1f} MiB against a "
        f"{PROCESS_CEILING_MIB} MiB budget")


def test_the_measurement_saw_a_pad_and_not_an_empty_buffer(measured):
    """
    Without this every assertion above is satisfied by generating nothing.

    A floor of 500 bytes per pad page, against a measured ~2250 for A6 --
    loose enough to survive a layout change, tight enough that a job which
    silently produced ten pages instead of a thousand cannot pass.
    """
    seen = measured["a6-1000"]
    assert seen["pdf"] > 1000 * 500, seen
    assert seen["peak"] > seen["base"], \
        "peak RSS never rose above the import baseline; nothing was built"


def test_the_per_page_cost_stays_flat(measured):
    """
    The scale-invariant statement, and the one that catches the real
    regression: not "it got a bit bigger" but "it now retains something per
    page". A slope measured between two sizes in the same interpreter is
    also far more stable across reportlab versions than an absolute.
    """
    small, large = measured["a6-250"], measured["a6-1000"]
    per_page_kib = (large["peak"] - small["peak"]) * 1024 / (1000 - 250)
    assert per_page_kib < PER_PAGE_KIB, (
        f"{per_page_kib:.1f} KiB per pad page against a {PER_PAGE_KIB} KiB "
        f"budget (measured ~17.8 when this was written)")


def test_the_hand_edited_page_ceiling_still_fits_the_board(measured):
    """
    `PAGE_CHOICES` stops at 1000; `MAX_PAGES` is 9999, and a hand-edited
    otp-unit.conf on the SD card is invited by the file's own header. That
    path gets the looser bound -- it need not fit the process budget, but
    it must not take the board down.
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
    """
    The headline measures A6 because A6 measured worst: A4 and A7 impose
    four and two pad pages per sheet, so the same pad is a quarter or a
    half as many PDF page objects. If that ever inverts, this goes red and
    the headline gets retargeted -- rather than quietly measuring the
    second-worst case for ever after.

    200 pages, not 1000: the ORDER is what is being asserted, and it is
    established well before the sizes stop being cheap.
    """
    peaks = {
        "A6": pad(200, paper="A6")["peak"],
        "A4": pad(200, paper="A4")["peak"],
        "A7": pad(200, a7=True)["peak"],
    }
    runner_up = max(v for k, v in peaks.items() if k != "A6")
    # STRICT, and by a stated margin. `==  max(...)` was satisfied by a
    # tie, so the ordering could collapse to equality -- the point at
    # which A6 stops being the worst case -- without going red. Measured
    # separation is ~1.1 MiB against ~0.2 MiB of run-to-run noise.
    assert peaks["A6"] > runner_up + 0.5, peaks


def test_a_present_trng_does_not_change_the_shape_of_the_cost():
    """
    The device HAS a TRNG -- every Pi does -- so the measured configuration
    has to be the one that ships. `get_random_bytes` XORs two big integers
    per draw, and a change there would show up as a per-draw allocation
    that the no-TRNG path never pays.

    A regular file stands in for /dev/hwrng: O_NONBLOCK is a no-op on one,
    so reads always succeed and the throttling that dominates a real VM's
    /dev/hwrng cannot skew this.

    The tally is checked, not assumed. `_hwrng_bytes` returns None on an
    OSError, a short read, or a `_looks_alive` rejection, and
    `get_random_bytes` then falls back to os.urandom silently -- so every
    way this setup can fail to reach the fake device (a renamed env var, a
    file the child cannot read, content that looks stuck) degrades the test
    into comparing the CSPRNG path against itself and passing. Filling the
    file with zeros instead of urandom makes `_looks_alive` reject it and
    was measured to leave the old assertions green.
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
    """
    Everything above is vacuous if `child()` does not really observe the
    child's memory -- a wrong ru_maxrss unit, a subprocess that never ran
    the body, a report read from the wrong process. So run the identical
    machinery against a child that allocates a known amount and insist it
    is seen.

    192 MiB is chosen to sit ABOVE PROCESS_CEILING_MIB: this also proves
    the ceiling is a number the measurement could exceed, rather than one
    nothing could ever reach.
    """
    seen = child(_ALLOCATION_BODY.format(mib=192))
    assert seen["peak"] > 160, \
        f"a child that allocated 192 MiB was measured at {seen['peak']} MiB"
    assert seen["peak"] > PROCESS_CEILING_MIB, \
        "the ceiling is unreachable by this measurement, so it guards nothing"


def test_a_large_parent_does_not_inflate_the_reading():
    """
    The bug this file was first written with, pinned.

    `ru_maxrss` in the child is `max(signal->maxrss, current mm)`, and exec
    folds the outgoing mm's peak into the first term -- so a child forked
    from a fat pytest reports pytest. Measured: parent at 310 MiB, child
    that allocated nothing, `ru_maxrss` 310.3 MiB against `VmHWM` 7.7 MiB.

    Under the full suite that turned the headline test into a measurement
    of the test runner: base == peak == 63.8 MiB for a 250-page pad AND a
    1000-page one. It passed on this file alone, which is the worst way for
    it to fail.
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
    # And prove the trap is real rather than hypothetical, so nobody
    # "simplifies" this back to ru_maxrss.
    assert after["rusage"] > 200, (
        "ru_maxrss no longer inherits the parent's peak on this kernel; "
        f"got {after['rusage']:.1f} MiB from a 256 MiB parent. If that is "
        f"genuinely fixed, this test and the VmHWM comment can go.")


def test_a_child_that_dies_is_not_read_as_a_small_footprint():
    """
    A crashed child prints nothing, and "nothing" must not parse as a low
    peak. This is how a measurement harness goes quietly green.
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

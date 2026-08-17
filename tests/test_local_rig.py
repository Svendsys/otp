"""The local repro rig's preconditions, and its agreement with tier 3.

harness/img-local-rig.sh exists so a tier-3 hypothesis costs seconds
instead of a pi-gen build plus two emulated boots. That only works if two
things hold, and neither is checkable by running the rig in CI -- which is
the point of the rig, and also why it has no CI wiring (issue #22):

  1. IT BOOTS THE SAME MACHINE img-boot.sh BOOTS. A finding from a rig
     whose kernel command line has quietly drifted is a confident answer
     about somewhere else. These tests parse BOTH scripts and compare, so
     the claim in the rig's header is a measurement rather than a promise.

  2. IT SAYS WHY WHEN IT CANNOT RUN. The rig needs a qemu with raspi3b, a
     handful of host tools, an arm64 kernel Image and a static arm64
     busybox. Every one of those, absent, produces a boot that writes
     nothing to either UART or stops at "Run /init as init process" -- the
     single most expensive symptom in this harness's history to
     misdiagnose, and one issue #17 paid for repeatedly. So each is
     refused BEFORE the emulator starts, with a message naming the cause.

Every negative here has a positive control built from the same fixture: a
test that only ever asserts a refusal cannot tell "refused for the right
reason" from "refused for any reason at all", and this repository has
already shipped one guard that could not pass.
"""
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
RIG = REPO / "harness" / "img-local-rig.sh"
IMG_BOOT = REPO / "harness" / "img-boot.sh"
README = REPO / "harness" / "README.md"


# --- driving the rig ------------------------------------------------------

def rig_eval(snippet, *, env=None, timeout=60):
    """Source the rig for its functions only, then run `snippet`.

    The rig is ONE self-contained file by design (issue #22: "the rig's
    virtue is being a single self-contained thing someone actually runs"),
    so there is no library to import. Sourcing it with OTP_RIG_LIB_ONLY=1
    defines its functions and runs no main, which is what lets these guards
    be ordinary offline pytest with no emulator anywhere near them.
    """
    script = f'OTP_RIG_LIB_ONLY=1 . "{RIG}"\n{snippet}\n'
    full = dict(os.environ)
    # Never inherit a developer's own pin into a test that is about
    # resolution, or their work dir into one that writes.
    for leak in ("OTP_RIG_KERNEL", "OTP_RIG_WORK", "OTP_RIG_BUSYBOX",
                 "OTP_RIG_IDLE_SECONDS", "OTP_RIG_TIMEOUT"):
        full.pop(leak, None)
    full.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=timeout, env=full)


# --- 1. the machine the rig boots is tier 3's machine ---------------------

def img_boot_append() -> str:
    """The kernel command line img-boot.sh hands the emulator, as written."""
    text = IMG_BOOT.read_text()
    m = re.search(r'^\s*-append "([^"]*)"', text, re.M)
    assert m, f"{IMG_BOOT} no longer passes a quoted -append"
    return m.group(1)


def rig_argv() -> list:
    """The argv the rig would actually use, from the rig's own function.

    Called rather than parsed: a test that read the source could agree with
    a function that no longer produces what the source appears to say.
    """
    proc = rig_eval('rig_qemu_argv /K /D /I /C0 /C1 /DRIVE')
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


# What img-boot.sh passes and the rig deliberately does not, each with the
# reason it cannot apply to an initramfs with no systemd and no root
# filesystem. This list is the WHOLE POINT of the test below: any token
# img-boot.sh gains that is on neither side of it turns this red, and
# somebody has to decide whether the rig needs it too. That decision going
# unmade by default is how the two would drift.
DELIBERATELY_NOT_IN_THE_RIG = {
    # The rig's root IS the initramfs; there is no second-stage root.
    "root=/dev/mmcblk0p2": "the rig boots no image, so there is no root device",
    "rootfstype=ext4": "same: no root filesystem to type",
    "rootwait": "same: nothing to wait for",
    # No systemd in a busybox initramfs, so none of its switches mean anything.
    "systemd.show_status=1": "no systemd in the rig's initramfs",
    "systemd.journald.forward_to_console=1": "no journald in the rig's initramfs",
    # Shell expansions in img-boot.sh's own line, not literal tokens.
    "$OVERLAY_TOKENS": "the overlay is the image's, and the rig boots no image",
    "otp.imgcheck=$phase": "wakes a unit that ships in the image; the rig has none",
}


def test_the_rig_boots_the_machine_img_boot_boots():
    """-M, -m, and the display/reboot flags, held against tier 3."""
    argv = rig_argv()
    img = IMG_BOOT.read_text()
    assert argv[0] == "qemu-system-aarch64"
    for flag, why in (("-M", "the emulated board"), ("-m", "the memory size")):
        rig_value = argv_value(argv, flag)
        # Not anchored to the line start: img-boot.sh writes
        # `-M raspi3b -m 1024 \` on a single line.
        m = re.search(rf'(?:^|\s){re.escape(flag)} (\S+)', img, re.M)
        assert m, f"{IMG_BOOT} no longer passes {flag} ({why})"
        assert rig_value == m.group(1), (
            f"the rig passes {flag} {rig_value} and img-boot.sh passes "
            f"{flag} {m.group(1)}. Findings do not transfer between "
            f"different machines."
        )
    # -no-reboot in particular: without it a guest that resets loops, and
    # runs 3 and 4 of issue #17 were both misread reboot loops.
    for flag in ("-display", "-no-reboot"):
        assert flag in argv, f"the rig no longer passes {flag}"
        assert flag in img, f"{IMG_BOOT} no longer passes {flag}"


def test_every_kernel_parameter_the_rig_passes_is_one_tier_3_passes():
    """The rig's command line must be a SUBSET of img-boot.sh's.

    A token here that tier 3 does not have is a machine tier 3 does not
    boot, and a finding about it does not transfer.
    """
    rig_tokens = set(argv_value(rig_argv(), "-append").split())
    img_tokens = set(img_boot_append().split())
    extra = rig_tokens - img_tokens
    assert not extra, (
        f"the rig passes kernel parameters img-boot.sh does not: "
        f"{sorted(extra)}. Either tier 3 needs them too, or the rig is "
        f"booting a machine its findings do not describe."
    )


def test_no_tier_3_parameter_is_dropped_without_a_stated_reason():
    """The other direction, which is the one that rots quietly.

    img-boot.sh gaining a parameter is the likely future event -- it has
    gained four so far (the journal forward, show_status, the imgcheck word,
    the overlay tokens). Each time, somebody has to decide whether the rig
    needs it. This makes that decision compulsory instead of accidental.
    """
    rig_tokens = set(argv_value(rig_argv(), "-append").split())
    img_tokens = set(img_boot_append().split())
    unexplained = img_tokens - rig_tokens - set(DELIBERATELY_NOT_IN_THE_RIG)
    assert not unexplained, (
        f"img-boot.sh passes {sorted(unexplained)} and the rig does not, and "
        f"no reason is recorded. Add each to DELIBERATELY_NOT_IN_THE_RIG "
        f"with why it cannot apply to a busybox initramfs, or add it to the "
        f"rig so the two machines still match."
    )


def test_the_stated_reasons_are_about_real_parameters():
    """The positive control for the list above.

    A stale entry would silently excuse a parameter that no longer exists,
    and the excuse list would grow into a place where drift hides.
    """
    img_tokens = set(img_boot_append().split())
    stale = set(DELIBERATELY_NOT_IN_THE_RIG) - img_tokens
    assert not stale, (
        f"DELIBERATELY_NOT_IN_THE_RIG explains {sorted(stale)}, which "
        f"img-boot.sh no longer passes."
    )


def test_the_rig_synthesises_the_board_revision_tier_3_synthesises():
    """The firmware's job, done identically in both, or gpiozero differs."""
    rig_rev = re.search(r'^RIG_BOARD_REVISION=(\S+)', RIG.read_text(), re.M)
    img_rev = re.search(r'^BOARD_REVISION=(\S+)', IMG_BOOT.read_text(), re.M)
    assert rig_rev and img_rev, "one of the scripts no longer sets a board revision"
    assert rig_rev.group(1) == img_rev.group(1), (
        f"the rig synthesises {rig_rev.group(1)} and img-boot.sh "
        f"{img_rev.group(1)}; these are different boards."
    )


def test_the_rig_captures_both_uarts_in_tier_3s_order():
    """First -serial is the PL011, second is the mini-UART.

    Six runs of issue #17 turned on this mapping. Reversing it here would
    put the rig's probe output in the file the reader treats as bootconsole
    noise.
    """
    argv = rig_argv()
    serials = [argv[i + 1] for i, a in enumerate(argv) if a == "-serial"]
    assert serials == ["file:/C0", "file:/C1"], serials


# --- 2. the probe menu cannot disagree with the kitchen -------------------

def declared_probes() -> list:
    proc = rig_eval("rig_probes")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def implemented_probes() -> list:
    """The case labels inside rig_probe_body, from the source."""
    text = RIG.read_text()
    start = text.index("rig_probe_body() {")
    end = text.index("\n}\n", start)
    body = text[start:end]
    return re.findall(r'^    ([a-z][a-z-]*)\)$', body, re.M)


def test_every_advertised_probe_is_implemented():
    missing = set(declared_probes()) - set(implemented_probes())
    assert not missing, (
        f"rig_probes advertises {sorted(missing)} with no case in "
        f"rig_probe_body. The rig would refuse a name it just offered."
    )


def test_every_implemented_probe_is_advertised():
    """The other direction: a probe nobody can find is a probe nobody uses."""
    extra = set(implemented_probes()) - set(declared_probes())
    assert not extra, (
        f"rig_probe_body implements {sorted(extra)} which rig_probes does "
        f"not list, so `--help` and the unknown-probe guard both hide it."
    )


def test_the_issue_22_probes_are_all_there():
    """Issue #22 named four by name; this is the acceptance criterion."""
    assert set(declared_probes()) >= {
        "rng", "coldplug-replay", "console-test", "idle-survive"}


def test_the_usage_text_lists_the_probes_it_dispatches():
    proc = rig_eval("rig_usage")
    assert proc.returncode == 0, proc.stderr
    for probe in declared_probes():
        assert re.search(rf'^  {re.escape(probe)}\s+\S', proc.stdout, re.M), (
            f"`--help` does not describe the {probe} probe")


def test_the_readme_names_the_rig_and_its_probes():
    """Issue #22's second clause: the README has to point at this tool."""
    text = README.read_text()
    assert "img-local-rig.sh" in text, (
        "harness/README.md does not mention the rig, so nobody looking for "
        "'what do I do when CI evidence runs out' will find it.")
    for probe in declared_probes():
        assert probe in text, f"harness/README.md does not name the {probe} probe"


# --- 3. an unknown probe is refused ---------------------------------------

def run_rig(args, *, env=None, timeout=120):
    full = dict(os.environ)
    for leak in ("OTP_RIG_KERNEL", "OTP_RIG_WORK", "OTP_RIG_BUSYBOX"):
        full.pop(leak, None)
    full.update(env or {})
    return subprocess.run(["bash", str(RIG), *args], capture_output=True,
                          text=True, timeout=timeout, env=full)


@pytest.fixture
def offline(tmp_path):
    """A --plan run that needs no network: the kernel version is pinned.

    Shared by the refusal tests and their positive controls, so the two
    differ in exactly the thing under test.
    """
    return {"OTP_RIG_KERNEL": "6.12.96+rpt-rpi-v8",
            "OTP_RIG_WORK": str(tmp_path / "work")}


def test_an_unknown_probe_is_refused(offline):
    proc = run_rig(["--plan", "rng-typo"], env=offline)
    assert proc.returncode != 0
    assert "unknown probe" in proc.stderr
    # The message has to be useful, not merely present: the whole reason a
    # typo must not fall through to a default is that a plausible console
    # for the wrong question is worse than an error.
    for probe in declared_probes():
        assert probe in proc.stderr, "the refusal does not list the real probes"


def test_a_known_probe_is_not_refused(offline):
    """Positive control for the refusal above, same fixture."""
    proc = run_rig(["--plan", "rng"], env=offline)
    assert proc.returncode == 0, proc.stderr
    assert "unknown probe" not in proc.stderr


def test_naming_no_probe_at_all_is_refused(offline):
    proc = run_rig(["--plan"], env=offline)
    assert proc.returncode != 0
    assert "no probe named" in proc.stderr


# --- 4. the host it cannot run on, said out loud --------------------------
#
# WHICH PATH PAYS FOR THE TOOLS, which is the question these tests are
# really about. A real run needs an emulator, a deb unpacker and a device
# tree compiler; --plan needs none of them, because it builds strings and
# prints them. Demanding all of it up front made `--plan rng` -- the
# cheapest thing the rig can do, and the one somebody runs to decide whether
# to install 200MB of emulator -- fail on any host without the emulator.
# Measured on github's ubuntu-latest, which has neither qemu-system-aarch64
# nor fdtput: six of these tests were red for that reason and nothing else.
#
# So the preconditions are exercised where they now live -- on the run --
# and the plan is exercised for needing none of them. The refusals below go
# through rig_preflight directly, because a run that gets PAST the preflight
# on a stubbed host then fails four steps later for an unrelated reason, and
# a positive control should assert the thing it is a control for. What keeps
# main() actually calling it is the pair of CLI tests further down.

TOOLS = ["bash", "sh", "curl", "xz", "gzip", "cpio", "dpkg-deb", "fdtput",
         "fdtget", "od", "depmod", "mkfs.ext4", "grep", "sed", "awk", "tr",
         "cut", "find", "sort", "head", "cat", "ls", "wc", "mkdir", "rm",
         "cp", "mv", "chmod", "du", "truncate", "sleep", "kill", "timeout",
         "basename", "dirname", "ln", "mount", "printf", "uname", "stat"]

#: Tools the rig only ever asks `command -v` about before the emulator
#: starts; it RUNS none of them there. A host that has not got one -- the
#: runner has no device-tree-compiler, so no fdtput -- gets a stub, so that
#: a refusal test and its positive control still differ in exactly the
#: omission rather than in what the runner happens to ship.
#:
#: qemu-system-aarch64 is deliberately NOT in here. The preflight executes
#: it (`-M help`), so a stub would be a lie about what qemu says; tests that
#: need one pass fake_qemu and control the lie themselves.
PRESENCE_ONLY = {"curl", "xz", "cpio", "dpkg-deb", "fdtput", "fdtget",
                 "depmod", "mkfs.ext4", "mount", "truncate"}


def stub_path(tmp_path, *, omit=(), fake_qemu=None):
    """A PATH with every tool the rig needs except the omitted ones.

    Symlinks the host's own tool where there is one, so the omission is the
    only difference between a refusal test and its positive control. Where
    there is not one, a PRESENCE_ONLY tool gets a stub and anything else is
    an error rather than a quiet gap -- a fixture that silently does not do
    its job is a guard that cannot fail, which this file has already paid
    for once (see BIG_RASPI_QEMU and `seq`).
    """
    d = tmp_path / ("bin-" + ("-".join(omit) or "all"))
    d.mkdir(parents=True, exist_ok=True)
    wanted = list(TOOLS)
    if fake_qemu is None:
        wanted.append("qemu-system-aarch64")
    for tool in wanted:
        if tool in omit:
            continue
        host = shutil.which(tool)
        if host is not None:
            (d / tool).symlink_to(host)
        elif tool in PRESENCE_ONLY:
            stub = d / tool
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        else:
            raise AssertionError(
                f"this host has no {tool}, and the rig runs that one rather "
                f"than merely looking for it, so a stub would make the test "
                f"below prove something else. Install it, or move {tool} to "
                f"PRESENCE_ONLY if the rig stopped executing it.")
    if fake_qemu is not None:
        q = d / "qemu-system-aarch64"
        q.write_text(fake_qemu)
        q.chmod(0o755)
    return str(d)


def rig_tools(name) -> list:
    """One of the rig's own tool lists, from the rig rather than restated."""
    proc = rig_eval(f'printf "%s\\n" "${{{name}[@]}}"')
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split()


def preflight(probe, path, *, env=None):
    """rig_preflight for one probe, on a PATH we control.

    The function rather than the CLI: this is the check under test, and the
    positive controls want to assert that it PASSED rather than that some
    later step failed differently.
    """
    full = {"PATH": path}
    full.update(env or {})
    return rig_eval(f'rig_preflight {probe} && echo PREFLIGHT-OK', env=full)


def test_the_stub_path_carries_every_tool_the_preflight_asks_for(tmp_path):
    """The control on the fixture, without which every control below rots.

    If the rig starts demanding a tool this file has never heard of, the
    stub PATH quietly omits it, every positive control here refuses, and the
    reason is a tool nobody was testing. Named here instead.
    """
    path = Path(stub_path(tmp_path, fake_qemu=RASPI_QEMU))
    demanded = rig_tools("RIG_BOOT_TOOLS") + rig_tools("RIG_COLDPLUG_TOOLS") \
        + rig_tools("RIG_RESOLVE_TOOLS")
    assert demanded, "the rig no longer declares its tools in named lists"
    missing = [t for t in demanded if not (path / t).exists()]
    assert not missing, (
        f"the rig demands {missing} and the stub PATH has not got them. Add "
        f"them to TOOLS (and to PRESENCE_ONLY if the preflight only looks "
        f"for them), or every positive control below refuses for a reason "
        f"it is not about.")


def test_a_host_with_no_qemu_says_so(tmp_path):
    proc = preflight("rng", stub_path(tmp_path, omit=("qemu-system-aarch64",)))
    assert proc.returncode != 0
    assert "qemu-system-aarch64" in proc.stderr
    assert "missing host tools" in proc.stderr


@pytest.mark.skipif(shutil.which("qemu-system-aarch64") is None,
                    reason="no qemu-system-aarch64 on this host to hold the "
                           "preflight against; the fake-qemu control below "
                           "still runs, and so does everything else in this "
                           "section. This one exists for the part a fake "
                           "cannot prove -- that a REAL `-M help` listing "
                           "matches the pattern rig_have_raspi_machine "
                           "greps for. apt-get install qemu-system-arm")
def test_the_hosts_own_qemu_satisfies_the_preflight(tmp_path):
    """The one test here that wants the real emulator.

    Everything else in this section runs against a shell script pretending
    to be qemu, which proves the rig's logic and nothing about qemu. This
    proves the two agree: the real binary is found, and its real `-M help`
    output really does carry a line the machine check accepts.
    """
    proc = preflight("rng", stub_path(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "PREFLIGHT-OK" in proc.stdout


NO_RASPI_QEMU = """#!/bin/bash
if [ "$1 $2" = "-M help" ]; then
  echo "Supported machines are:"
  echo "virt                 QEMU 8.2 ARM Virtual Machine"
  exit 0
fi
[ "$1" = "--version" ] && { echo "QEMU emulator version 9.9.9 (no pi machines)"; exit 0; }
exit 0
"""

RASPI_QEMU = NO_RASPI_QEMU.replace(
    'echo "virt                 QEMU 8.2 ARM Virtual Machine"',
    'echo "raspi3b              Raspberry Pi 3B (revision 1.2)"')


def test_a_qemu_without_the_raspi_machine_says_so(tmp_path):
    """A real configuration: some distributions build a reduced machine set.

    Without this the failure arrives from inside the emulator and reads like
    a typo in the rig.
    """
    proc = preflight("rng", stub_path(tmp_path, fake_qemu=NO_RASPI_QEMU))
    assert proc.returncode != 0
    assert "raspi3b" in proc.stderr
    assert "no 'raspi3b' machine" in proc.stderr


def test_a_qemu_with_the_raspi_machine_gets_through(tmp_path):
    """Positive control: the same stub, one machine line different.

    Also the control that runs everywhere for
    test_a_host_with_no_qemu_says_so, whose omission this restores.
    """
    proc = preflight("rng", stub_path(tmp_path, fake_qemu=RASPI_QEMU))
    assert proc.returncode == 0, proc.stderr
    assert "PREFLIGHT-OK" in proc.stdout


# A `-M help` far larger than a pipe buffer, with raspi3b on the FIRST
# machine line so a `grep -q` matches and closes the pipe while the producer
# is still writing. That is the whole 141 shape.
#
# The loop is pure bash arithmetic and NOT `$(seq ...)`: the first version
# of this fixture used seq, seq is not on the stubbed PATH the rig runs
# with, the command substitution expanded to nothing, and the "20,000-line"
# producer emitted two lines. The test passed, and it passed just as
# happily with the `grep -q` mutation applied -- a fixture that silently
# does not do its job is a guard that cannot fail.
BIG_RASPI_QEMU = NO_RASPI_QEMU.replace(
    'echo "virt                 QEMU 8.2 ARM Virtual Machine"',
    'echo "raspi3b              Raspberry Pi 3B (revision 1.2)"\n'
    '  i=0\n'
    '  while [ "$i" -lt 20000 ]; do i=$((i+1)); echo "machine$i            filler"; done')


def test_the_big_listing_fixture_really_is_big(tmp_path):
    """The positive control on the fixture, without which the test below
    cannot be trusted -- and, measured, could not fail."""
    path = stub_path(tmp_path, fake_qemu=BIG_RASPI_QEMU)
    proc = subprocess.run([str(Path(path) / "qemu-system-aarch64"), "-M", "help"],
                          capture_output=True, text=True, timeout=120,
                          env={"PATH": path})
    assert proc.returncode == 0, proc.stderr
    # Comfortably past a 64KiB pipe buffer, so grep -q closing early really
    # does leave the producer writing into a closed pipe.
    assert len(proc.stdout) > 200_000, len(proc.stdout)
    assert proc.stdout.splitlines()[1].startswith("raspi3b")


def test_the_machine_check_survives_a_producer_that_keeps_writing(tmp_path):
    """`grep -q` here returns 141 under pipefail, not 0.

    device/install.sh carries a comment about exactly this shape and
    img-boot.sh's sampler was measured at 141 for it. Measured again for
    this check: with the `grep -q` form the pipeline returns 141, the
    function reports the machine ABSENT, and the rig refuses to run on a
    host that is perfectly capable. A `-M help` large enough to lose the
    race must still be read to the end.
    """
    proc = preflight("rng", stub_path(tmp_path, fake_qemu=BIG_RASPI_QEMU))
    assert proc.returncode == 0, (
        "the raspi3b check failed against a large -M help listing, which is "
        "the SIGPIPE/141 shape: " + proc.stderr)


def test_coldplug_replay_demands_the_tools_only_it_needs(tmp_path):
    proc = preflight("coldplug-replay",
                     stub_path(tmp_path, omit=("depmod",), fake_qemu=RASPI_QEMU))
    assert proc.returncode != 0
    assert "depmod" in proc.stderr


def test_the_cheap_probes_do_not_pay_for_them(tmp_path):
    """Positive control, and a real property: no depmod is fine for rng.

    Demanding kmod from every probe would make a perfectly capable host
    refuse the probe it could have run.
    """
    proc = preflight("rng",
                     stub_path(tmp_path, omit=("depmod",), fake_qemu=RASPI_QEMU))
    assert proc.returncode == 0, proc.stderr
    assert "PREFLIGHT-OK" in proc.stdout


# --- 4b. the plan pays for none of it, and the run pays for all of it -----
#
# The two CLI tests that keep the split above wired to the thing people
# actually type. Everything in section 4 calls rig_preflight directly, so
# without these main() could stop calling it, or start calling it on the
# printing path again, and every one of them would still pass.

#: A host that has never installed an emulator: no qemu, and no
#: device-tree-compiler either. This is ubuntu-latest.
NO_EMULATOR = ("qemu-system-aarch64", "fdtput", "fdtget")


def test_the_plan_prints_on_a_host_with_no_emulator_at_all(tmp_path, offline):
    """--plan is string construction, and must cost what string construction
    costs.

    The person running it is deciding whether to install 200MB of emulator;
    requiring the emulator first is the wrong way round. This is also the
    whole of why six tests in this file were red on the runner.
    """
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, omit=NO_EMULATOR)
    proc = run_rig(["--plan", "rng"], env=env)
    assert proc.returncode == 0, proc.stderr
    assert "missing host tools" not in proc.stderr
    # The argv, in full, not merely a clean exit: an emulator-less host must
    # get the same thing to read as anybody else. Held against the rig's own
    # argv function, minus the tokens that are work-dir paths.
    printed = [line.strip() for line in proc.stdout.splitlines()]
    reference = rig_eval('rig_qemu_argv /K /D /I /C0 /C1')
    assert reference.returncode == 0, reference.stderr
    for token in reference.stdout.splitlines():
        if token.startswith(("/", "file:")):
            continue
        assert token in printed, f"the plan printed no {token!r}"
    # And it did none of the work it was printing about: no deb, no kernel,
    # no initramfs. A plan that downloads 32MB is not a plan.
    work = Path(env["OTP_RIG_WORK"])
    assert sorted(p.name for p in work.iterdir()) == []


def test_the_plan_says_the_same_thing_with_the_tools_and_without(tmp_path,
                                                                 offline):
    """The output is not merely produced without them -- it is identical.

    A plan that quietly says something else on a host with no emulator is
    worse than one that refuses: the argv is what the reader takes away.
    """
    with_tools = dict(offline)
    with_tools["PATH"] = stub_path(tmp_path, fake_qemu=RASPI_QEMU)
    without = dict(offline)
    without["PATH"] = stub_path(tmp_path, omit=NO_EMULATOR)
    first = run_rig(["--plan", "rng"], env=with_tools)
    second = run_rig(["--plan", "rng"], env=without)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout, (
        "the plan differs depending on what the host has installed")


def test_a_real_run_is_refused_before_it_costs_anything(tmp_path, offline):
    """The other half: the run itself is still checked, and checked FIRST.

    Every precondition unmet produces the same console -- nothing, or a stop
    at "Run /init as init process" -- so a run that discovers the missing
    emulator four steps later has already spent the download and lost the
    diagnosis.
    """
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, omit=("qemu-system-aarch64",))
    proc = run_rig(["rng"], env=env)
    assert proc.returncode != 0
    assert "missing host tools" in proc.stderr
    assert "qemu-system-aarch64" in proc.stderr
    # From the preflight, not from a later stage that happened to trip over
    # the same absence. See the positive control below for what "later"
    # looks like on this fixture.
    assert "no Filename for linux-image-" not in proc.stderr


def test_a_real_run_with_the_tools_present_gets_past_the_preflight(tmp_path,
                                                                   offline):
    """Positive control: the same run, the same fixture, nothing omitted.

    It cannot boot -- there is no archive index in a fresh work dir and the
    kernel is pinned, so nothing knows which deb to fetch -- and that is the
    point: the message it stops on comes from AFTER the preflight, which is
    how we know the preflight passed rather than that the run never happened.
    """
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, fake_qemu=RASPI_QEMU)
    proc = run_rig(["rng"], env=env)
    assert "missing host tools" not in proc.stderr
    assert "no 'raspi3b' machine" not in proc.stderr
    assert "no Filename for linux-image-" in proc.stderr, proc.stderr


# --- 5. the kernel the image would run ------------------------------------

PACKAGES = """Package: something-else
Version: 1

Package: linux-image-rpi-v8
Source: linux
Version: 1:6.12.96-1+rpt1
Architecture: arm64
Depends: linux-image-6.12.96+rpt-rpi-v8 (= 1:6.12.96-1+rpt1)
Filename: pool/main/l/linux/linux-image-rpi-v8_6.12.96-1+rpt1_arm64.deb

Package: linux-image-6.12.96+rpt-rpi-v8
Version: 1:6.12.96-1+rpt1
Filename: pool/main/l/linux/linux-image-6.12.96+rpt-rpi-v8_6.12.96-1+rpt1_arm64.deb
"""


def resolve(tmp_path, text):
    p = tmp_path / "Packages"
    p.write_text(text)
    return rig_eval(f'rig_resolve_kernel_version "{p}"')


def test_the_metapackage_indirection_is_followed(tmp_path):
    """The whole trick: the meta name is stable, its target is not.

    Reading it is what keeps the rig booting the kernel pi-gen would
    install, without anybody editing a version string here.
    """
    proc = resolve(tmp_path, PACKAGES)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "6.12.96+rpt-rpi-v8"


def test_a_moved_archive_fails_loudly_rather_than_resolving_to_nothing(tmp_path):
    """A Depends with no versioned image must not yield an empty release.

    An empty release would be concatenated into a URL and a modules path and
    fail four steps later, about something else.
    """
    # The realistic rot is a metapackage pointing at ANOTHER metapackage,
    # not at a versioned kernel -- so the fixture keeps the `linux-image-`
    # prefix and removes only the version. An earlier fixture used
    # `linux-headers-rpi-v8`, which the pattern rejects on the prefix alone:
    # measured, loosening `linux-image-[0-9]` to `linux-image-` left that
    # version of this test green, so it was testing the prefix and calling
    # it the version check.
    proc = resolve(tmp_path, PACKAGES.replace(
        "Depends: linux-image-6.12.96+rpt-rpi-v8 (= 1:6.12.96-1+rpt1)",
        "Depends: linux-image-rpi-v8-current"))
    assert proc.returncode != 0
    assert "no versioned linux-image" in proc.stderr


def test_a_depends_on_something_unrelated_also_fails(tmp_path):
    """The prefix half of the same pattern, kept as its own case."""
    proc = resolve(tmp_path, PACKAGES.replace(
        "Depends: linux-image-6.12.96+rpt-rpi-v8 (= 1:6.12.96-1+rpt1)",
        "Depends: linux-headers-rpi-v8"))
    assert proc.returncode != 0
    assert "no versioned linux-image" in proc.stderr


def test_a_metapackage_that_vanished_fails_loudly(tmp_path):
    proc = resolve(tmp_path, PACKAGES.replace("Package: linux-image-rpi-v8",
                                              "Package: linux-image-rpi-v9"))
    assert proc.returncode != 0
    assert "no Depends" in proc.stderr


def test_the_field_reader_does_not_bleed_between_stanzas(tmp_path):
    """A stanza's field must come from that stanza.

    The shape this guards against is an awk that keeps matching after its
    package ended and returns the NEXT package's Filename -- which would
    download a real file with a plausible name and boot the wrong kernel.
    """
    p = tmp_path / "Packages"
    p.write_text(PACKAGES)
    proc = rig_eval(f'rig_package_field "{p}" something-else Filename')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", (
        f"something-else has no Filename, but the reader returned "
        f"{proc.stdout.strip()!r}")


def test_the_field_reader_finds_a_field_that_is_there(tmp_path):
    """Positive control for the bleed test, same fixture."""
    p = tmp_path / "Packages"
    p.write_text(PACKAGES)
    proc = rig_eval(f'rig_package_field "{p}" linux-image-6.12.96+rpt-rpi-v8 Filename')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("_arm64.deb")


# --- 6. the gzip trap -----------------------------------------------------

def arm64_image(tmp_path, name="Image", magic=b"ARM\x64"):
    """A 64-byte arm64 Linux boot-protocol header.

    The magic lives at offset 56. Synthesised rather than carved out of a
    real kernel so the fixture cannot rot with an archive.
    """
    p = tmp_path / name
    p.write_bytes(b"\x00" * 56 + magic + b"\x00" * 4)
    return p


def test_a_real_arm64_image_is_accepted(tmp_path):
    """Positive control, first: the check has to be able to pass."""
    p = arm64_image(tmp_path)
    proc = rig_eval(f'rig_require_arm64_image "{p}" && echo ACCEPTED')
    assert proc.returncode == 0, proc.stderr
    assert "ACCEPTED" in proc.stdout


def test_the_gzipped_vmlinuz_from_the_deb_is_refused(tmp_path):
    """The measured trap: the archive ships boot/vmlinuz-* GZIPPED.

    On a real Pi the raspi-firmware postinst decompresses it into
    kernel8.img; QEMU is not that postinst. Handed the compressed file,
    -kernel produces a boot that writes NOTHING to either UART -- run 1's
    symptom, and an afternoon to diagnose.
    """
    import gzip
    import os as _os
    p = tmp_path / "vmlinuz"
    # Incompressible filler so the gzip is comfortably longer than the
    # 64-byte header, as a real 10MB vmlinuz is. A tiny gzip would be
    # caught by the size check instead, which is a different guard.
    p.write_bytes(gzip.compress(b"\x00" * 56 + b"ARM\x64" + _os.urandom(4096)))
    assert p.stat().st_size > 64
    proc = rig_eval(f'rig_require_arm64_image "{p}"')
    assert proc.returncode != 0
    assert "not an uncompressed arm64 kernel Image" in proc.stderr
    assert "GZIPPED" in proc.stderr


def test_an_image_with_the_wrong_magic_is_refused(tmp_path):
    """The general case behind the gzip one, on a full-length file."""
    p = arm64_image(tmp_path, name="wrong", magic=b"XXXX")
    proc = rig_eval(f'rig_require_arm64_image "{p}"')
    assert proc.returncode != 0
    assert "not an uncompressed arm64 kernel Image" in proc.stderr


def test_a_truncated_or_empty_kernel_is_refused(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    proc = rig_eval(f'rig_require_arm64_image "{p}"')
    assert proc.returncode != 0


def test_a_short_file_is_refused_by_the_rig_not_by_od(tmp_path):
    """od(1) says "cannot skip past end of combined input" and exits 1.

    Under `set -e` that ends the function with od's complaint and no
    explanation, which is exactly the confusion these checks exist to
    remove. Measured: a 68-byte gzip fixture produced precisely that.
    """
    p = tmp_path / "short"
    p.write_bytes(b"\x1f\x8b\x08\x00short")
    proc = rig_eval(f'rig_require_arm64_image "{p}"')
    assert proc.returncode != 0
    assert "too short" in proc.stderr
    assert "cannot skip past end" not in proc.stderr


# --- 7. the wrong busybox -------------------------------------------------

def elf(tmp_path, name, *, machine, interp=False):
    """A minimal ELF64 header with the fields the rig reads.

    Built byte by byte rather than compiled: the rig reads e_machine and
    walks the program headers for PT_INTERP, and a synthetic file exercises
    exactly that with no toolchain and no architecture assumptions about
    the machine running the tests.
    """
    phnum = 1 if interp else 0
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2          # ELFCLASS64
    header[5] = 1          # ELFDATA2LSB
    header[6] = 1          # EV_CURRENT
    struct.pack_into("<H", header, 16, 2)          # e_type = ET_EXEC
    struct.pack_into("<H", header, 18, machine)    # e_machine
    struct.pack_into("<I", header, 20, 1)          # e_version
    struct.pack_into("<Q", header, 32, 64)         # e_phoff
    struct.pack_into("<H", header, 52, 64)         # e_ehsize
    struct.pack_into("<H", header, 54, 56)         # e_phentsize
    struct.pack_into("<H", header, 56, phnum)      # e_phnum
    body = bytearray()
    if interp:
        ph = bytearray(56)
        struct.pack_into("<I", ph, 0, 3)           # PT_INTERP
        body += ph
    p = tmp_path / name
    p.write_bytes(bytes(header) + bytes(body))
    return p


EM_AARCH64 = 183
EM_X86_64 = 62


def test_a_static_arm64_busybox_is_accepted(tmp_path):
    """Positive control, first."""
    p = elf(tmp_path, "busybox-arm64", machine=EM_AARCH64)
    proc = rig_eval(f'rig_require_arm64_static "{p}" && echo ACCEPTED')
    assert proc.returncode == 0, proc.stderr
    assert "ACCEPTED" in proc.stdout


def test_a_busybox_for_the_wrong_architecture_is_refused(tmp_path):
    """The failure this prevents is four faults wearing the same console.

    An x86-64 busybox gets as far as "Run /init as init process" and then
    the machine says nothing at all -- which is also what a gzipped kernel,
    a wrong DTB, a dead console and a watchdog reset look like.
    """
    p = elf(tmp_path, "busybox-x86", machine=EM_X86_64)
    proc = rig_eval(f'rig_require_arm64_static "{p}"')
    assert proc.returncode != 0
    assert "not an arm64 binary" in proc.stderr
    assert "183" in proc.stderr, "the message does not say what it wanted"


def test_a_dynamically_linked_busybox_is_refused(tmp_path):
    """The initramfs has no loader and no libc, so PT_INTERP is fatal."""
    p = elf(tmp_path, "busybox-dyn", machine=EM_AARCH64, interp=True)
    proc = rig_eval(f'rig_require_arm64_static "{p}"')
    assert proc.returncode != 0
    assert "dynamically linked" in proc.stderr


def test_something_that_is_not_an_elf_at_all_is_refused(tmp_path):
    p = tmp_path / "script"
    # Longer than the 64-byte header, so this exercises the MAGIC check
    # rather than the length check that guards od(1) above it.
    p.write_text("#!/bin/sh\n" + "# a shell script, not a binary\n" * 8)
    assert p.stat().st_size > 64
    proc = rig_eval(f'rig_require_arm64_static "{p}"')
    assert proc.returncode != 0
    assert "not an ELF binary" in proc.stderr


def test_a_busybox_too_short_to_have_a_header_is_refused_clearly(tmp_path):
    """The od(1) guard again, on the other checker."""
    p = tmp_path / "stub"
    p.write_text("#!/bin/sh\n")
    proc = rig_eval(f'rig_require_arm64_static "{p}"')
    assert proc.returncode != 0
    assert "too short" in proc.stderr
    assert "cannot skip past end" not in proc.stderr


# --- 8. the probes report, and the rig waits for the report ---------------

def test_every_probe_ends_with_the_marker_the_sampler_waits_for():
    """`poweroff -f` does not stop -M raspi3b: measured, the guest printed
    its DONE marker at 4.15s and then sat in rcu stalls to the 180s cap.

    So the sampler kills the emulator on the marker, and a probe that never
    prints one costs the full backstop. The marker is emitted by the init
    wrapper after the probe body, which is what makes that true for every
    probe including one that fails half way.
    """
    text = RIG.read_text()
    assert 'printf \'echo "OTP-RIG-DONE %s"\\n\' "$probe"' in text, (
        "the init wrapper no longer emits the DONE marker unconditionally "
        "after the probe body")
    assert 'grep -acF "OTP-RIG-DONE $probe"' in text, (
        "the sampler no longer looks for the marker with grep -c; `grep -q` "
        "here returns 141 under pipefail against a file qemu is still writing")


def test_a_probe_that_never_reports_is_a_failure_not_a_pass():
    """The silent-pass question: what does the rig do with no evidence?

    A rig that exits 0 having captured an empty console is worse than one
    that crashes, because the reader concludes the probe answered.
    """
    text = RIG.read_text()
    boot = text[text.index("rig_boot() {"):]
    assert "NEVER REPORTED" in boot
    assert re.search(r'if \[ -z "\$stopped" \]; then', boot), (
        "rig_boot no longer distinguishes a run that reported from one that "
        "did not")


def test_the_rig_has_no_ci_wiring():
    """Issue #22, explicitly: "No CI wiring. This is a debugging tool."

    It is still shellchecked -- that is linting, not running -- but nothing
    may invoke it per commit. A rig in CI would put a network fetch and an
    emulated boot on every push, which is the cost this exists to remove.
    """
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text()
        for line in text.splitlines():
            if "img-local-rig.sh" not in line:
                continue
            assert "shellcheck" in text, (
                f"{workflow.name} references the rig outside a shellcheck job")
            assert not re.search(r'^\s*(run:|\s+)\./harness/img-local-rig\.sh',
                                 line), (
                f"{workflow.name} appears to RUN the rig: {line.strip()!r}")

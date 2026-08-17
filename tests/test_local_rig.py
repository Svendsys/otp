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

  3. IT DOES NOT CHARGE FOR WHAT IT IS NOT DOING. The corollary of 2, and
     the one that was wrong until this file was fixed: --plan boots
     nothing, so it must not demand an emulator before it will say what it
     would run. A refusal on the path that only prints is not a
     precondition, it is an obstacle -- and it made six tests here fail on
     a runner for a reason that was never about the rig.

Every negative here has a positive control built from the same fixture: a
test that only ever asserts a refusal cannot tell "refused for the right
reason" from "refused for any reason at all", and this repository has
already shipped one guard that could not pass.
"""
import os
import re
import shlex
import shutil
import struct
import subprocess
import time
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
#
# AGAINST TIER 3'S ARGV, NOT AGAINST TIER 3'S PROSE. img-boot.sh is 1375
# lines and most of them are argument about the invocation rather than the
# invocation, so a substring search over the file agrees just as happily
# with the flag the script no longer passes. Measured, with the checks
# below in their previous form: `-M` matched the log() line "Booting $phase
# under -M raspi3b ...", `-m` matched a comment, `-display` and
# `-no-reboot` matched any of a dozen mentions -- and deleting `-no-reboot`
# from the real command line left the whole suite at 863 passed, as did
# reversing its two -serial lines. So the one block the shell actually runs
# is parsed, the way img_boot_append() already reads the one -append.

def img_boot_append() -> str:
    """The kernel command line img-boot.sh hands the emulator, as written."""
    text = IMG_BOOT.read_text()
    m = re.search(r'^\s*-append "([^"]*)"', text, re.M)
    assert m, f"{IMG_BOOT} no longer passes a quoted -append"
    return m.group(1)


def img_boot_qemu_block() -> str:
    """The ONE backgrounded qemu invocation img-boot.sh runs, as written.

    Exactly one, for the reason tests/mutation_gate.py refuses a `find`
    that matches twice: which of two blocks got read would otherwise
    depend on the file's history rather than on anybody's decision.
    """
    blocks = re.findall(
        r'^[ \t]*timeout\b[^\n]*\bqemu-system-aarch64[ \t]*\\\n'
        r'(?:[^\n]*\\\n)*'
        r'[^\n]*&[ \t]*$',
        IMG_BOOT.read_text(), re.M)
    assert len(blocks) == 1, (
        f"{IMG_BOOT} has {len(blocks)} backgrounded qemu-system-aarch64 "
        f"invocations continued over several lines, and this file compares "
        f"the rig against exactly one. It cannot choose between two.")
    return blocks[0]


def img_boot_argv() -> list:
    """That block as tokens, split the way the shell would split it.

    shlex rather than a regex per flag, because the point is to read the
    ARGUMENTS -- including the ones whose value is a quoted string with
    spaces in it. `${INITRD:+-initrd "$INITRD"}` survives as two
    odd-looking tokens, which is harmless: nothing here asks about -initrd.
    """
    return shlex.split(img_boot_qemu_block().replace("\\\n", " "), posix=True)


def test_the_parsed_block_really_is_the_command_line_tier_3_runs():
    """The positive control on the parser, without which everything in
    this section rots into agreement with a string nobody runs.

    Three independent anchors: the block is the one the shell backgrounds,
    it starts with the bounded `timeout` wrapper, and the -append inside it
    is the same one img_boot_append() finds by a completely different
    route. A regex that drifted onto some other line fails all three.
    """
    block = img_boot_qemu_block()
    assert block.rstrip().endswith("&"), block
    argv = img_boot_argv()
    assert argv[0] == "timeout", argv[:3]
    assert "qemu-system-aarch64" in argv, argv
    assert argv_value(argv, "-append") == img_boot_append(), (
        "the -append inside the parsed block is not the one "
        "img_boot_append() reads out of the file, so one of the two is "
        "looking at the wrong line")


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
    """-M, -m, and the display/reboot flags, held against tier 3's argv."""
    argv = rig_argv()
    img = img_boot_argv()
    assert argv[0] == "qemu-system-aarch64"
    for flag, why in (("-M", "the emulated board"), ("-m", "the memory size")):
        assert flag in img, (
            f"{IMG_BOOT}'s qemu invocation no longer passes {flag} ({why})")
        rig_value = argv_value(argv, flag)
        img_value = argv_value(img, flag)
        assert rig_value == img_value, (
            f"the rig passes {flag} {rig_value} and img-boot.sh passes "
            f"{flag} {img_value}. Findings do not transfer between "
            f"different machines."
        )
    # -no-reboot in particular: without it a guest that resets loops, and
    # runs 3 and 4 of issue #17 were both misread reboot loops. In the
    # ARGV on both sides -- the word appears a dozen times in img-boot.sh's
    # prose, and asking the file whether it contains it is how deleting it
    # from the real command line left this test green.
    for flag in ("-display", "-no-reboot"):
        assert flag in argv, f"the rig no longer passes {flag}"
        assert flag in img, (
            f"{IMG_BOOT}'s qemu invocation no longer passes {flag}. The "
            f"word may well still be in the file; that is what made this "
            f"check unable to fail.")


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


#: The mapping six runs of issue #17 turned on, recorded where a reader
#: meets it: in the FILE NAMES. Whatever lands in console.log came out of
#: the PL011 (ttyAMA1 under this DTB, where the probe and the real console
#: talk) and whatever lands in console-uart1.log came out of the mini-UART,
#: where the earlycon bootconsole lives and nothing else does. Both scripts
#: must capture them in that order or the file each script points its
#: reader at is the other one -- which is precisely the stubborn 0 bytes
#: that went unread for six runs.
UART_ORDER = ["console.log", "console-uart1.log"]


def serial_files(argv, resolve=lambda v: v) -> list:
    """The basenames each -serial writes to, in the order they are passed."""
    out = []
    for flag, value in zip(argv, argv[1:]):
        if flag != "-serial":
            continue
        assert value.startswith("file:"), value
        out.append(os.path.basename(resolve(value[len("file:"):])))
    return out


def img_boot_console_vars() -> dict:
    """img-boot.sh's `local console=` / `local console2=`, as paths.

    The variable names carry no meaning and the file names carry all of
    it, so the -serial values have to be resolved through the assignments
    before the order means anything.
    """
    found = dict(re.findall(r'^\s*local (console2?)="(\$dir/[^"]+)"',
                            IMG_BOOT.read_text(), re.M))
    assert set(found) == {"console", "console2"}, (
        f"{IMG_BOOT} no longer names its two console files in the shape "
        f"this test resolves: found {found}")
    return found


def test_tier_3_captures_the_pl011_into_console_log():
    """img-boot.sh's half of the mapping, read from its qemu invocation.

    This used to be checked nowhere at all: the test below asserted the
    arguments it had just passed in and never opened img-boot.sh.
    Measured -- swapping img-boot.sh's two -serial lines left the whole
    suite at 863 passed.
    """
    cvars = img_boot_console_vars()
    got = serial_files(img_boot_argv(), lambda v: cvars[v.lstrip("$")])
    assert got == UART_ORDER, (
        f"img-boot.sh captures {got}. The first -serial is the PL011 and "
        f"the second is the mini-UART; reversed, every reader sent to "
        f"console.log for the real console gets bootconsole noise, and the "
        f"PL011's silence -- the actual clue -- lands in the file nobody "
        f"is told to read.")


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


def test_every_harness_script_the_readme_names_is_one_that_exists():
    """EVERY mention, not the first one.

    The narrower form of this -- "img-local-rig.sh appears somewhere in the
    README" -- went green the moment a second paragraph mentioned the rig,
    because renaming the pointer at the top of the section left the other
    mentions to satisfy it. A reader who follows a path that is not there
    concludes the tool was deleted, which for a debugging tool whose entire
    value is "existing and being current" is the same as deleting it.
    """
    named = set(re.findall(r'harness/([a-z0-9][a-z0-9.-]*\.(?:sh|py))',
                           README.read_text()))
    assert "img-local-rig.sh" in named, sorted(named)
    missing = sorted(n for n in named if not (REPO / "harness" / n).exists())
    assert not missing, (
        f"harness/README.md tells the reader to run {missing}, and "
        f"harness/ has no such file.")


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
    """A rig invocation that needs no network: the kernel version is pinned.

    Shared by the refusal tests and their positive controls, so the two
    differ in exactly the thing under test. Used by real-run tests as well
    as --plan ones: pinning the release is what lets a run get past the
    preflight and stop, immediately and offline, at the archive index it
    has not got.
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


#: Tools a Debian host may genuinely not have, which is why the rig has to
#: ASK for them rather than assume them. Not coreutils in general: `cat`
#: and `mv` are not going to be missing, and a list that included them
#: would be noise around the entries that matter. Every one of these,
#: absent, is a real configuration -- a runner with no
#: device-tree-compiler, a container with no kmod, a qemu built without
#: the Pi machines.
NOT_GUARANTEED = {
    "qemu-system-aarch64", "curl", "xz", "gzip", "cpio", "dpkg-deb",
    "fdtput", "fdtget", "od", "depmod", "mkfs.ext4", "truncate", "timeout",
}


def rig_host_code() -> str:
    """The rig's own shell, with the guest's and the prose removed.

    The `<<'PROBE'` heredocs run in the initramfs against busybox applets
    and say nothing at all about what the HOST needs -- a `dd` in there is
    not a host tool. Comments go too: this file argues with itself at
    length and names tools while doing it.
    """
    text = re.sub(r"cat <<'PROBE'\n.*?\nPROBE\n", "", RIG.read_text(),
                  flags=re.S)
    return re.sub(r'^[ \t]*#.*$', '', text, flags=re.M)


def test_every_host_tool_the_rig_runs_is_one_it_asked_for():
    """USED IS DECLARED, which is the direction that catches the real bug.

    The test above checks the other one -- that everything DECLARED is on
    the stub PATH -- and a tool the rig runs without declaring sails past
    it. fdtget did exactly that. The rig writes the board revision with
    fdtput and reads it back with fdtget, so on a host with no
    device-tree-compiler the readback returned nothing and the rig
    announced "$dtb does not carry linux,revision after the patch": the
    patch blamed for a tool that was never installed, which is precisely
    the misdirection the whole preflight exists to prevent. timeout and
    truncate had the same shape.
    """
    declared = set(rig_tools("RIG_BOOT_TOOLS") + rig_tools("RIG_COLDPLUG_TOOLS")
                   + rig_tools("RIG_RESOLVE_TOOLS"))
    code = rig_host_code()
    used = {t for t in NOT_GUARANTEED
            if re.search(rf'(?:^|[\t (|;&]|\$\()[ \t]*{re.escape(t)}[ \t]',
                         code, re.M)}
    assert used, "the tool scan matched nothing at all, so it proves nothing"
    undeclared = used - declared
    assert not undeclared, (
        f"the rig runs {sorted(undeclared)} and no RIG_*_TOOLS list asks "
        f"for them, so a host without them fails somewhere downstream with "
        f"a message about something else. Add each to the list belonging to "
        f"the path that runs it.")


def test_the_tool_scan_would_notice_an_undeclared_one():
    """Positive control for the scan, which is a regex and can rot silently.

    An undeclared tool is planted by removing one from the declaration
    rather than by adding one to the code, so this exercises the same
    comparison the test above makes on the same text.
    """
    declared = set(rig_tools("RIG_BOOT_TOOLS") + rig_tools("RIG_COLDPLUG_TOOLS")
                   + rig_tools("RIG_RESOLVE_TOOLS")) - {"fdtget"}
    code = rig_host_code()
    used = {t for t in NOT_GUARANTEED
            if re.search(rf'(?:^|[\t (|;&]|\$\()[ \t]*{re.escape(t)}[ \t]',
                         code, re.M)}
    assert "fdtget" in used, (
        "the scan cannot see fdtget in the rig's host code, so the test "
        "above could not have caught it going undeclared")
    assert used - declared == {"fdtget"}


def test_a_host_with_no_qemu_says_so(tmp_path):
    proc = preflight("rng", stub_path(tmp_path, omit=("qemu-system-aarch64",)))
    assert proc.returncode != 0
    assert "qemu-system-aarch64" in proc.stderr
    assert "missing host tools" in proc.stderr


@pytest.mark.skipif(shutil.which("qemu-system-aarch64") is None,
                    reason="THIS ONE NEVER RUNS IN CI, and that is a "
                           "standing fact rather than an accident of some "
                           "host: ci.yml installs qemu-system-x86 and only "
                           "that, in the `vm` job, which runs no pytest -- "
                           "so no job that collects this file has an "
                           "aarch64 emulator and this is skipped on every "
                           "push, permanently. Deliberate: putting qemu in "
                           "the fast tier is a 200MB install per commit for "
                           "a debugging tool with no CI wiring by design "
                           "(issue #22). It runs LOCALLY, which is where "
                           "the rig is used, and it is the only thing here "
                           "that can prove a REAL `-M help` listing matches "
                           "the pattern rig_have_raspi_machine greps for. "
                           "Everything else in this section runs everywhere "
                           "against a fake, including this one's positive "
                           "control test_a_qemu_with_the_raspi_machine_gets_"
                           "through. apt-get install qemu-system-arm")
def test_the_hosts_own_qemu_satisfies_the_preflight(tmp_path):
    """The one test here that wants the real emulator, and the one test
    here that CI never runs. See the skip reason.

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


def test_the_one_thing_the_plan_does_need_is_asked_for_where_it_is_used(
        tmp_path):
    """--plan reports the release it would boot, and unpinned that is a
    network fetch, so curl is a real precondition of THAT plan.

    Named as a missing tool rather than left to arrive as curl's own
    "could not fetch", which reads like the archive is down.
    """
    env = {"OTP_RIG_WORK": str(tmp_path / "work"),
           "PATH": stub_path(tmp_path, omit=("curl",), fake_qemu=RASPI_QEMU)}
    proc = run_rig(["--plan", "rng"], env=env)
    assert proc.returncode != 0
    assert "missing host tools" in proc.stderr
    assert "curl" in proc.stderr


def test_pinning_the_kernel_removes_even_that(tmp_path, offline):
    """Positive control, and the claim the README makes.

    Same host, same missing curl; the only difference is that nothing has
    to be resolved, so nothing is fetched and nothing is demanded.
    """
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, omit=("curl",), fake_qemu=RASPI_QEMU)
    proc = run_rig(["--plan", "rng"], env=env)
    assert proc.returncode == 0, proc.stderr
    assert "missing host tools" not in proc.stderr


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


# --- 8. the marker contract, run rather than quoted -----------------------
#
# ONE HANDSHAKE IS WHAT MAKES THIS RIG SECONDS-SCALE: the guest prints
# OTP-RIG-DONE, the sampler sees it and kills the emulator. `poweroff -f`
# does not stop -M raspi3b -- measured, a probe printed its own DONE marker
# at 4.15s guest and the machine then sat in rcu_preempt stalls until the
# 180s cap, exiting 124 -- so without the handshake the rig is a 300s tool
# and has no reason to exist rather than tier 3.
#
# Both halves of it used to be guarded by "the source contains this
# string", which proves the string is present and nothing whatever about
# the handshake. These run rig_boot against a script pretending to be qemu
# and time it, the way test_the_machine_check_survives_a_producer_that_
# keeps_writing runs the real check against a real 200KB listing.

FAKE_QEMU = r"""#!/bin/bash
# A stand-in for the emulator. Writes to the file named by the FIRST
# -serial -- which is what a guest whose console is the PL011 does -- and
# then sits still for far longer than any backstop these tests set, so
# that "the run ended" is always the sampler's doing and never the fake's.
first=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-serial" ] && [ -z "$first" ]; then first="${a#file:}"; fi
  prev="$a"
done
printf 'fake qemu speaking on %s\n' "$first" >> "$first"
__MARKER__
sleep 600
"""


def fake_qemu(marker):
    """FAKE_QEMU with one line: what the guest says, or nothing at all."""
    say = (f'printf "%s\\n" {shlex.quote(marker)} >> "$first"'
           if marker else '# this guest says nothing')
    return FAKE_QEMU.replace("__MARKER__", say)


def boot(tmp_path, probe, marker, *, backstop=25, wait=120):
    """Run the rig's own rig_boot against the fake emulator.

    rig_boot is the function under test and the only one: it builds the
    argv, samples the console, stops the emulator on the marker and turns
    all of that into an exit code. Everything before it -- the archive,
    the deb, the DTB, the initramfs -- is a different half of the rig and
    is tested elsewhere in this file.

    Returns the process, the wall clock it took, and the work dir, because
    two of the three properties here are about time. The process is None
    if `wait` ran out: a rig_boot that has not returned is a rig_boot that
    is waiting out its backstop, which is the failure some of these tests
    are looking for, and giving up on it early is what keeps the mutation
    gate's bill for that row down to the deadline rather than up to the
    backstop.
    """
    work = tmp_path / "work"
    work.mkdir()
    binned = tmp_path / "bin"
    binned.mkdir()
    q = binned / "qemu-system-aarch64"
    q.write_text(fake_qemu(marker))
    q.chmod(0o755)
    env = {"PATH": f"{binned}:{os.environ['PATH']}",
           "OTP_RIG_WORK": str(work),
           "OTP_RIG_TIMEOUT": str(backstop)}
    # `|| rc=$?`, not `;`: the rig runs under set -e and rig_boot restores
    # it before returning, so a bare call would take the sourcing shell
    # down with it and print nothing.
    started = time.monotonic()
    try:
        proc = rig_eval(f'rc=0; rig_boot {probe} /K /D /I "" || rc=$?; '
                        f'echo "RIG-BOOT-RC=$rc"', env=env, timeout=wait)
    except subprocess.TimeoutExpired:
        proc = None
    return proc, time.monotonic() - started, work


@pytest.fixture(scope="module")
def clean_boot(tmp_path_factory):
    """One successful fake boot, shared by the tests that need one.

    Module-scoped because it costs real seconds: the sampler's cadence is
    two seconds and it drains for two more, so a boot that stops on its
    marker cannot take less than about six however fast the fake is. The
    failure cases below each need their own, and pay for it.

    The backstop is 25s and the deadline 12s, which is what makes the
    failure cheap to observe: a sampler that has stopped stopping runs to
    the backstop, and there is no reason to watch it do so.
    """
    return boot(tmp_path_factory.mktemp("clean"), "rng",
                "OTP-RIG-DONE rng status=ok", backstop=25, wait=12)


def test_the_marker_stops_the_emulator_instead_of_waiting_out_the_backstop(
        clean_boot):
    """The handshake, timed.

    The fake sits still for 600s and the backstop is 25s, so finishing at
    all is the property and finishing quickly is the measurement. A
    sampler that has stopped watching for the marker pays the whole 25 --
    and then reports as a run that never reported, which is the second
    assertion here from the same evidence.
    """
    proc, seconds, _ = clean_boot
    assert proc is not None, (
        f"the marker was on the console and rig_boot had still not "
        f"returned {seconds:.0f}s later, with a 25s backstop under it. The "
        f"sampler is not stopping the emulator, which is the whole reason "
        f"this rig answers faster than tier 3.")
    assert "RIG-BOOT-RC=0" in proc.stdout, proc.stderr
    assert "reported after" in proc.stderr


def test_a_probe_that_never_reports_pays_the_backstop_and_says_so(tmp_path):
    """The other side, and the interesting case.

    A run whose guest never printed a marker is what a wedge looks like.
    It has to cost the backstop -- there is nothing else to wait for --
    and it has to be its OWN exit code, because "the machine never got
    there" and "the probe ran and found something wrong" are different
    problems and telling them apart is most of what this rig is for.
    """
    proc, seconds, _ = boot(tmp_path, "rng", "", backstop=6)
    assert "RIG-BOOT-RC=1" in proc.stdout, proc.stderr
    assert "NEVER REPORTED" in proc.stderr
    assert seconds >= 6, (
        f"rig_boot gave up after {seconds:.0f}s of a 6s backstop, so it "
        f"stopped on something other than the marker it never saw")


def test_the_rig_will_not_call_a_failed_probe_an_answer(tmp_path):
    """status=fail is a non-zero exit, and a different one.

    Measured before the marker carried a status: coldplug's body with no
    module disk printed "FAIL: could not mount the module disk at
    /dev/mmcblk0" and the rig exited 0, so `./harness/img-local-rig.sh
    coldplug-replay && echo clean` printed clean for a replay that mounted
    nothing. A probe body of `:` printed BEGIN and DONE and did the same.
    """
    proc, _, _ = boot(tmp_path, "rng", "OTP-RIG-DONE rng status=fail")
    assert "RIG-BOOT-RC=2" in proc.stdout, proc.stderr
    assert "REPORTED FAILURE" in proc.stderr
    # Not the wedge code: the guest reported, it just reported badly.
    assert "NEVER REPORTED" not in proc.stderr


def test_a_marker_with_no_status_at_all_is_not_a_pass(tmp_path):
    """Fail closed. A marker the rig cannot read a status out of stops the
    run -- it has to, or the run costs the backstop -- but it does not get
    to mean the probe did its work."""
    proc, _, _ = boot(tmp_path, "rng", "OTP-RIG-DONE rng")
    assert "RIG-BOOT-RC=2" in proc.stdout, proc.stderr


def test_the_rig_captures_both_uarts_in_tier_3s_order(tmp_path):
    """The rig's half of the mapping, from a run rather than from a call.

    rig_qemu_argv is not the whole story -- rig_boot decides which file it
    hands to which -serial, and which one it then samples and points the
    reader at. So this reads the argv rig_boot actually wrote, and checks
    that the line the fake spoke on the FIRST port landed in the file the
    rig points its reader at. It used to assert the arguments the test
    itself had just passed in.

    Its own short backstop, not the shared clean boot: this does not need
    the run to SUCCEED, only to have launched, and reversing the order is
    exactly the mutation that stops the sampler ever seeing a marker. Four
    seconds of fake emulator rather than a backstop spent proving a point
    the argv file already settles.
    """
    _, _, work = boot(tmp_path, "rng", "OTP-RIG-DONE rng status=ok",
                      backstop=4)
    argv = (work / "rng" / "qemu-argv.txt").read_text().splitlines()
    assert serial_files(argv) == UART_ORDER, serial_files(argv)
    first = (work / "rng" / "console.log").read_text()
    second = (work / "rng" / "console-uart1.log").read_text()
    assert "OTP-RIG-DONE" in first, first
    assert "OTP-RIG-DONE" not in second, second


# --- 9. the init the guest runs -------------------------------------------
#
# UNTIL NOW THIS WAS THE ONLY SHELL IN THE REPOSITORY NOTHING LOOKED AT.
# The probe bodies live in `<<'PROBE'` quoted heredocs, which shellcheck
# reads as data: measured, an unbalanced `if [ "$broken" ; then` inside the
# rng heredoc left shellcheck v0.9.0 at rc 0, this file's 48 tests green
# and the whole suite at 863 passed. What that typo does in real use is
# kill /init before the marker, so the sampler waits out the full 300s and
# the rig prints "NEVER REPORTED ... hung, reset, or never reached /init"
# -- the exact wedge signature the rig exists to DISAMBIGUATE, produced by
# a typo in the rig.
#
# So: `sh -n` over every probe's init, and then the init RUN, because
# parsing is not the interesting half. The marker's status is what the
# rig's exit code now means, and the only way to know a probe sets it when
# its work did not happen is to stop the work happening and look.

def init_script(probe, *, idle_seconds="3") -> str:
    """The init the rig writes into the initramfs, from the rig itself."""
    proc = rig_eval(f'rig_init_script {probe}',
                    env={"OTP_RIG_IDLE_SECONDS": idle_seconds})
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def sh_n(text):
    return subprocess.run(["sh", "-n"], input=text, capture_output=True,
                          text=True, timeout=30)


def test_every_probes_init_parses_as_shell():
    """The cheap check that was missing entirely."""
    for probe in declared_probes():
        proc = sh_n(init_script(probe))
        assert proc.returncode == 0, (
            f"the {probe} init does not parse, so /init dies before the "
            f"marker and the run reads as a wedge: {proc.stderr}")


def test_the_syntax_check_would_notice(tmp_path):
    """Positive control, and the whole of the evidence for the section.

    A `sh -n` that accepts anything is not a check. This is the exact
    breakage that was measured passing everything: shellcheck rc 0, 48
    tests green, 863 passed.
    """
    broken = init_script("rng").replace('echo "-- hwrng:"',
                                        'if [ "$broken" ; then')
    assert 'if [ "$broken" ; then' in broken, "the fixture patched nothing"
    assert sh_n(broken).returncode != 0


#: Commands the init runs that must not be allowed to touch the host these
#: tests run on. `mount` above all: the init mounts /proc, /sys and /dev,
#: and this is not the rig's guest. The rest either write outside the temp
#: dir -- `mkdir -p /lib`, `ln -sf ... /lib/modules` -- or load kernel
#: modules. Everything else is the host's own tool, which is what lets the
#: rng body genuinely run: /proc/sys/kernel/random and /dev/random are
#: Linux, not Raspberry Pi.
NEUTERED = ("mount", "mkdir", "ln", "modprobe", "depmod")

#: The idle loop at the bottom of every init is `while true; do sleep 5;
#: done`, and it is deliberate -- PID 1 exiting is a kernel panic that
#: scrolls the evidence off the console. On a host there is no PID 1 to
#: protect and something has to end the run, so the five-second sleep, and
#: only the five-second sleep, stops the shell. Every other sleep in the
#: probes asks for one second and is answered honestly.
SLEEP_STUB = """#!/bin/sh
if [ "$1" = "5" ]; then kill -TERM "$PPID"; exit 0; fi
__ONE__
"""


def run_init(tmp_path, probe, *, rc=None, stubs=None, real_sleep=True,
             moddir=None, idle_seconds="3", timeout=60):
    """Run the init the rig builds, under a host shell.

    The emulator is not what is under test here; the SCRIPT is, and it is
    the same script either way. `rc` overrides the exit status of named
    commands, which is the whole mechanism -- {"dd": 1} is a /dev/random
    that cannot be read, and {"mount": 1} is a guest with no /proc.
    `stubs` replaces a command outright, for the cases where WHAT it says
    is the thing under test.

    dash buffers its output, so this reads a file rather than a pipe:
    measured, a shell killed mid-loop still flushes everything it echoed.
    """
    rc = dict(rc or {})
    binned = tmp_path / "bin"
    binned.mkdir(parents=True)
    for name in NEUTERED:
        rc.setdefault(name, 0)
    for name, code in rc.items():
        stub = binned / name
        stub.write_text(f'#!/bin/sh\n[ {code} = 0 ] || '
                        f'echo "{name}: stubbed failure" >&2\nexit {code}\n')
        stub.chmod(0o755)
    for name, text in (stubs or {}).items():
        stub = binned / name
        stub.write_text(text)
        stub.chmod(0o755)
    sleeper = binned / "sleep"
    sleeper.write_text(SLEEP_STUB.replace(
        "__ONE__", 'exec /bin/sleep "$@"' if real_sleep else "exit 0"))
    sleeper.chmod(0o755)

    script = tmp_path / "init"
    script.write_text(init_script(probe, idle_seconds=idle_seconds))
    out = tmp_path / "init.out"
    env = dict(os.environ)
    env["PATH"] = f"{binned}:{env['PATH']}"
    # Both documented in the rig: the guest writes these two paths and a
    # host has neither to spare.
    env["RIG_FAILED"] = str(tmp_path / "rig-failed")
    env["RIG_MODDIR"] = str(moddir or (tmp_path / "moddir"))
    with out.open("w") as fh:
        proc = subprocess.Popen(["sh", str(script)], stdout=fh,
                                stderr=subprocess.STDOUT, env=env,
                                cwd=str(tmp_path))
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    return out.read_text(errors="replace")


def marker_status(text, probe):
    """The status on the DONE marker, or None if there is no marker."""
    m = re.search(rf'^OTP-RIG-DONE {re.escape(probe)}(?: status=(\S+))?$',
                  text, re.M)
    return None if m is None else (m.group(1) or "")


def test_every_probes_init_ends_with_a_marker_carrying_a_status(tmp_path):
    """Run, for every probe, with everything that could fail stubbed out.

    The wrapper's contract in one test: BEGIN, then the body, then the
    marker -- unconditionally, so a probe that dies half way still ends
    the run instead of hanging it -- and the marker says which of the two
    it was. Whether a given probe passes on a host is not the point and is
    not asserted; coldplug's answer depends on what the host has in
    /sys/devices.
    """
    for probe in declared_probes():
        text = run_init(tmp_path / probe, probe, real_sleep=False)
        assert f"OTP-RIG-BEGIN {probe}" in text, text
        status = marker_status(text, probe)
        assert status in ("ok", "fail"), (
            f"the {probe} init ended with {status!r}, not a marker the "
            f"sampler can read a verdict out of:\n{text}")


def test_a_probe_whose_work_happened_says_ok(tmp_path):
    """Positive control for every negative below, on the probe whose body
    runs honestly on a host: /proc/sys/kernel/random and /dev/random are
    Linux, and dd reading sixteen bytes is the real thing."""
    text = run_init(tmp_path, "rng")
    assert marker_status(text, "rng") == "ok", text
    assert "16 blocking bytes" in text, text


def test_the_rng_probe_will_not_report_a_read_that_did_not_happen(tmp_path):
    """THE ONE THIS SECTION EXISTS FOR.

    dd's exit code was never examined and its stderr went to /dev/null, so
    "16 blocking bytes: uptime ... -> ..." printed unconditionally with a
    plausible sub-second delta. Measured on the emulator: booting the
    verbatim probe with /dev/random deleted printed "16 blocking bytes:
    uptime 3.29 -> 3.31" and the rig exited 0.

    It does not stay in the rig either. img-boot.sh cites this measurement
    as the evidence for a hard gate on crng seeding and harness/README.md
    repeats the figures, so a probe that cannot tell a blocking read from
    a missing device is a pad printer being told its entropy is fine.
    """
    text = run_init(tmp_path, "rng", rc={"dd": 1})
    assert marker_status(text, "rng") == "fail", text
    assert "the blocking read did not happen" in text, text
    assert "16 blocking bytes" not in text, (
        "the probe still reported sixteen bytes it never read:\n" + text)


def test_a_mount_that_failed_is_not_swallowed(tmp_path):
    """All three mounts ended in `2>/dev/null` with their status unread.

    A guest with no /dev is the concrete path by which the rng probe above
    reports on entropy it never asked for, so the mounts have to be able
    to say they did not happen.
    """
    text = run_init(tmp_path, "rng", rc={"mount": 1})
    assert marker_status(text, "rng") == "fail", text
    assert "could not mount" in text, text


#: busybox's own wording, from a real guest: mounting devtmpfs over a /dev
#: the kernel already mounted returns 255 and says this.
EBUSY_MOUNT = ('#!/bin/sh\n'
               'echo "mount: mounting $2 on $3 failed: '
               'Device or resource busy" >&2\nexit 255\n')


def test_a_mount_that_was_already_done_is_tolerated(tmp_path):
    """The one failure the mounts may swallow, and only this one.

    A kernel built with CONFIG_DEVTMPFS_MOUNT mounts /dev before /init
    runs, and mounting it again over itself is EBUSY and no problem.
    Measured on this rig's kernel that does NOT happen -- /dev holds one
    node, `console`, and all three mounts return 0 -- which is exactly why
    the tolerance has to be this narrow. The control is the test above:
    the same non-zero exit with any other message is a failure.
    """
    text = run_init(tmp_path, "rng", stubs={"mount": EBUSY_MOUNT})
    assert marker_status(text, "rng") == "ok", text
    assert "already mounted" in text, text
    assert "could not mount" not in text, text


def test_the_idle_probe_will_not_report_time_it_did_not_spend(tmp_path):
    """"survived 45 seconds" was printed for completing 45 iterations of a
    loop, which is a different statement.

    A sleep that returns immediately finishes the loop in milliseconds and
    the probe reports surviving a minute it never spent -- and this is the
    probe that decides whether the watchdog fix from issue #17 is
    believed. Same shape as the rng probe's unexamined dd.
    """
    fast = run_init(tmp_path / "fast", "idle-survive", real_sleep=False)
    assert marker_status(fast, "idle-survive") == "fail", fast
    assert "Nothing waited, so" in fast, fast
    assert "survived 3 seconds with no reset" not in fast, fast


def test_the_idle_probe_reports_time_it_did_spend(tmp_path):
    """Positive control, same fixture, three real seconds."""
    slow = run_init(tmp_path / "slow", "idle-survive", real_sleep=True)
    assert marker_status(slow, "idle-survive") == "ok", slow
    assert "survived 3 seconds with no reset" in slow, slow


#: Fails the module disk and nothing else. A blanket `mount` failure would
#: also take out the init's own /proc, /sys and /dev -- which set the
#: status by themselves -- and the test below would then pass with the
#: probe's own failure path deleted. Measured: it did.
EXT4_MOUNT_FAILS = ('#!/bin/sh\n'
                    'case " $* " in *" ext4 "*)\n'
                    '  echo "mount: mounting /dev/mmcblk0 failed" >&2\n'
                    '  exit 1 ;;\nesac\nexit 0\n')


def test_a_coldplug_that_mounted_nothing_says_so(tmp_path):
    """The measured one. With no module disk the probe printed "FAIL:
    could not mount the module disk at /dev/mmcblk0" and the rig exited 0,
    so `./harness/img-local-rig.sh coldplug-replay && echo clean` printed
    clean for a replay that mounted nothing."""
    text = run_init(tmp_path, "coldplug-replay",
                    stubs={"mount": EXT4_MOUNT_FAILS}, real_sleep=False)
    assert "could not mount" in text and "on /proc" not in text, (
        "the init's own mounts failed too, so the status below could have "
        "come from anywhere:\n" + text)
    assert marker_status(text, "coldplug-replay") == "fail", text
    assert "could not mount the module disk" in text, text


def moddisk(tmp_path, release, *, lines=3):
    """A module tree with a modules.dep in it, where the guest looks."""
    d = tmp_path / "moddir" / "lib" / "modules" / release
    d.mkdir(parents=True)
    (d / "modules.dep").write_text("x:\n" * lines)
    return tmp_path / "moddir"


def test_a_coldplug_with_no_modules_dep_says_so(tmp_path):
    """depmod runs on the HOST, in rig_build_module_disk, because the deb
    ships no modules.dep at all. Without it modprobe resolves nothing and
    every probe below 'succeeds' without doing anything."""
    text = run_init(tmp_path, "coldplug-replay", real_sleep=False)
    assert marker_status(text, "coldplug-replay") == "fail", text
    assert "no modules.dep for" in text, text


def test_a_coldplug_that_replayed_nothing_is_not_a_clean_replay(tmp_path):
    """"coldplug replayed 0 modaliases, 0 of them non-zero" read as a pass.

    The old guard excluded zero on purpose -- `[ "$total" -gt 0 ] &&` --
    so the one case where the loop did no work at all was the one case it
    could not report. A count that agrees with itself while measuring
    nothing is the same defect as the rng probe's, and this probe already
    carries a paragraph about the last time it had it.
    """
    release = subprocess.run(["uname", "-r"], capture_output=True,
                             text=True).stdout.strip()
    text = run_init(tmp_path, "coldplug-replay", real_sleep=False,
                    moddir=moddisk(tmp_path, release),
                    rc={"find": 0})
    assert "replayed 0 modaliases" in text, (
        "the fixture did not reach the replay loop, so this test is about "
        "something else:\n" + text)
    assert marker_status(text, "coldplug-replay") == "fail", text
    assert "replayed NOTHING" in text, text


def test_a_coldplug_that_replayed_something_is(tmp_path):
    """Positive control for the two above: same fixture, a real find.

    Whether individual modprobes succeed is not the question -- "No such
    module" is a legitimate answer for an alias with no driver -- so the
    stub returns 0 and the probe should be satisfied.
    """
    release = subprocess.run(["uname", "-r"], capture_output=True,
                             text=True).stdout.strip()
    text = run_init(tmp_path, "coldplug-replay", real_sleep=False,
                    moddir=moddisk(tmp_path, release))
    assert re.search(r'replayed [1-9]\d* modaliases', text), text
    assert marker_status(text, "coldplug-replay") == "ok", text


def test_a_console_probe_that_cannot_read_proc_says_so(tmp_path):
    """"(none)" for /proc/consoles reads as a finding about consoles when
    it is /proc failing to mount, and this probe exists to answer exactly
    the question it then cannot answer."""
    text = run_init(tmp_path, "console-test", rc={"cat": 1})
    assert marker_status(text, "console-test") == "fail", text
    assert "/proc/consoles is unreadable" in text, text


def test_the_console_probe_is_happy_when_it_can_read_proc(tmp_path):
    """Positive control. Note what is NOT a failure here: "no serial tty
    nodes at all". Measured on a healthy guest -- devtmpfs mounts and
    populates /dev with 130-odd nodes and there is still no /dev/ttyAMA1,
    while /proc/consoles registers ttyAMA1 at 204:65. "Registered as a
    console, no character device" is the finding this probe exists to
    bring back, not a fault in the probe."""
    text = run_init(tmp_path, "console-test")
    assert marker_status(text, "console-test") == "ok", text


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

"""The local repro rig's preconditions, and its agreement with tier 3.

harness/img-local-rig.sh makes a tier-3 hypothesis cost seconds, and it has
no CI wiring (issue #22). These tests hold three properties: it boots the
same machine img-boot.sh boots (both scripts are parsed and compared); it
refuses to run, naming the cause, when a tool, kernel or busybox is missing
or wrong, since each of those otherwise produces a silent console; and
--plan, which boots nothing, demands none of the run's tools.

Every negative has a positive control built from the same fixture, so a
refusal is known to be for the right reason.
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

    The rig is one self-contained file with no library to import; sourcing
    it with OTP_RIG_LIB_ONLY=1 defines its functions and runs no main.
    """
    script = f'OTP_RIG_LIB_ONLY=1 . "{RIG}"\n{snippet}\n'
    full = dict(os.environ)
    # Never inherit a developer's own pin or work dir.
    for leak in ("OTP_RIG_KERNEL", "OTP_RIG_WORK", "OTP_RIG_BUSYBOX",
                 "OTP_RIG_IDLE_SECONDS", "OTP_RIG_TIMEOUT"):
        full.pop(leak, None)
    full.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=timeout, env=full)


# --- 1. the machine the rig boots is tier 3's machine ---------------------
#
# Compared against the one qemu block img-boot.sh runs, not a substring
# search of the file, which would match flags named only in comments and log
# lines.

def img_boot_append() -> str:
    """The kernel command line img-boot.sh hands the emulator, as written."""
    text = IMG_BOOT.read_text()
    m = re.search(r'^\s*-append "([^"]*)"', text, re.M)
    assert m, f"{IMG_BOOT} no longer passes a quoted -append"
    return m.group(1)


def img_boot_qemu_block() -> str:
    """The one backgrounded qemu invocation img-boot.sh runs, as written.
    Exactly one must exist, so which block is read is never arbitrary."""
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
    """That block as tokens, split with shlex so quoted values with spaces
    stay whole. `${INITRD:+-initrd "$INITRD"}` survives as two odd tokens,
    which nothing here asks about."""
    return shlex.split(img_boot_qemu_block().replace("\\\n", " "), posix=True)


def test_the_parsed_block_really_is_the_command_line_tier_3_runs():
    """Positive control on the parser: the block is the one the shell
    backgrounds, it starts with the `timeout` wrapper, and its -append is
    the one img_boot_append() finds by a different route."""
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
    """The argv the rig would use, obtained by calling the rig's own function,
    so it is what the function produces and not what the source appears to
    say."""
    proc = rig_eval('rig_qemu_argv /K /D /I /C0 /C1 /DRIVE')
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.splitlines()


def argv_value(argv, flag):
    return argv[argv.index(flag) + 1]


# What img-boot.sh passes and the rig deliberately does not, each with its
# reason. Any token img-boot.sh gains that is on neither side turns the test
# below red, forcing a decision about the rig.
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
    # otp.imgcheck=<phase> in the two probe boots and empty in the release
    # boot. The rig has no such unit either way.
    "$imgcheck_token": "wakes a unit that ships in the image; the rig has none",
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
    # Without -no-reboot a guest that resets loops. Checked in the argv on
    # both sides, not by searching the file.
    for flag in ("-display", "-no-reboot"):
        assert flag in argv, f"the rig no longer passes {flag}"
        assert flag in img, (
            f"{IMG_BOOT}'s qemu invocation no longer passes {flag}. The "
            f"word may well still be in the file; that is what made this "
            f"check unable to fail.")


def test_every_kernel_parameter_the_rig_passes_is_one_tier_3_passes():
    """The rig's kernel command line is a subset of img-boot.sh's: a token
    tier 3 lacks means a machine tier 3 does not boot, and findings would
    not transfer."""
    rig_tokens = set(argv_value(rig_argv(), "-append").split())
    img_tokens = set(img_boot_append().split())
    extra = rig_tokens - img_tokens
    assert not extra, (
        f"the rig passes kernel parameters img-boot.sh does not: "
        f"{sorted(extra)}. Either tier 3 needs them too, or the rig is "
        f"booting a machine its findings do not describe."
    )


def test_no_tier_3_parameter_is_dropped_without_a_stated_reason():
    """Every tier-3 kernel parameter is either passed by the rig or listed
    with a reason, so a parameter img-boot.sh gains forces a decision about
    the rig."""
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
    """Positive control for the excuse list: every entry names a parameter
    img-boot.sh really passes, so stale entries cannot hide drift."""
    img_tokens = set(img_boot_append().split())
    stale = set(DELIBERATELY_NOT_IN_THE_RIG) - img_tokens
    assert not stale, (
        f"DELIBERATELY_NOT_IN_THE_RIG explains {sorted(stale)}, which "
        f"img-boot.sh no longer passes."
    )


def test_the_rig_synthesises_the_board_revision_tier_3_synthesises():
    """Both scripts synthesise the same board revision (the firmware's job),
    or gpiozero behaves differently."""
    rig_rev = re.search(r'^RIG_BOARD_REVISION=(\S+)', RIG.read_text(), re.M)
    img_rev = re.search(r'^BOARD_REVISION=(\S+)', IMG_BOOT.read_text(), re.M)
    assert rig_rev and img_rev, "one of the scripts no longer sets a board revision"
    assert rig_rev.group(1) == img_rev.group(1), (
        f"the rig synthesises {rig_rev.group(1)} and img-boot.sh "
        f"{img_rev.group(1)}; these are different boards."
    )


#: console.log is the PL011 (ttyAMA1 under this DTB, the real console and the
#: probe) and console-uart1.log is the mini-UART, which carries only the
#: earlycon bootconsole. Both scripts must capture them in this order, or each
#: points its reader at the wrong file.
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
    """img-boot.sh's `local console=` / `local console2=`, as paths, so
    -serial values can be resolved to the file names that carry the
    meaning."""
    found = dict(re.findall(r'^\s*local (console2?)="(\$dir/[^"]+)"',
                            IMG_BOOT.read_text(), re.M))
    assert set(found) == {"console", "console2"}, (
        f"{IMG_BOOT} no longer names its two console files in the shape "
        f"this test resolves: found {found}")
    return found


def test_tier_3_captures_the_pl011_into_console_log():
    """img-boot.sh passes the PL011's -serial first, into console.log, as read
    from its real qemu invocation."""
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
    """Every harness script the README mentions exists, not just the first
    mention: a reader who follows a dead path concludes the tool was
    deleted."""
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

    Shared by refusal tests and their positive controls so the two differ
    only in the thing under test. A real run with it gets past the
    preflight and stops, offline, at the archive index it does not have.
    """
    return {"OTP_RIG_KERNEL": "6.12.96+rpt-rpi-v8",
            "OTP_RIG_WORK": str(tmp_path / "work")}


def test_an_unknown_probe_is_refused(offline):
    proc = run_rig(["--plan", "rng-typo"], env=offline)
    assert proc.returncode != 0
    assert "unknown probe" in proc.stderr
    # The refusal must list the real probes: a typo falling through to a
    # default probe would give a plausible console for the wrong question.
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
# A real run needs an emulator, a deb unpacker and a device tree compiler;
# --plan needs none of them. The refusals go through rig_preflight directly
# so each positive control asserts the preflight passed; the CLI tests in 4b
# keep main() calling it.

TOOLS = ["bash", "sh", "curl", "xz", "gzip", "cpio", "dpkg-deb", "fdtput",
         "fdtget", "od", "depmod", "mkfs.ext4", "grep", "sed", "awk", "tr",
         "cut", "find", "sort", "head", "cat", "ls", "wc", "mkdir", "rm",
         "cp", "mv", "chmod", "du", "truncate", "sleep", "kill", "timeout",
         "basename", "dirname", "ln", "mount", "printf", "uname", "stat"]

#: Tools the preflight only asks `command -v` about. A host without one gets a
#: stub, so a refusal test and its control differ only in the omission.
#: qemu-system-aarch64 is not here because the preflight executes it (`-M
#: help`); tests that need one pass fake_qemu.
PRESENCE_ONLY = {"curl", "xz", "cpio", "dpkg-deb", "fdtput", "fdtget",
                 "depmod", "mkfs.ext4", "mount", "truncate"}


def stub_path(tmp_path, *, omit=(), fake_qemu=None):
    """A PATH with every tool the rig needs except the omitted ones.

    The host's own tool is symlinked where there is one. Otherwise a
    PRESENCE_ONLY tool gets a stub and anything else is an error, never a
    silent gap.
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
    """rig_preflight for one probe, on a PATH we control. The function is
    called, not the CLI, so controls can assert that it passed."""
    full = {"PATH": path}
    full.update(env or {})
    return rig_eval(f'rig_preflight {probe} && echo PREFLIGHT-OK', env=full)


def test_the_stub_path_carries_every_tool_the_preflight_asks_for(tmp_path):
    """Control on the fixture: the stub PATH carries every tool the preflight
    asks for. Otherwise a newly demanded tool would make every positive
    control refuse for an untested reason."""
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


#: Tools a Debian host may really lack, which the rig must therefore ask for.
#: Not coreutils in general.
NOT_GUARANTEED = {
    "qemu-system-aarch64", "curl", "xz", "gzip", "cpio", "dpkg-deb",
    "fdtput", "fdtget", "od", "depmod", "mkfs.ext4", "truncate", "timeout",
}


def rig_host_code() -> str:
    """The rig's host-side shell: the `<<'PROBE'` heredocs (which run in the
    guest against busybox) and the comments are removed, since both name
    tools the host does not need."""
    text = re.sub(r"cat <<'PROBE'\n.*?\nPROBE\n", "", RIG.read_text(),
                  flags=re.S)
    return re.sub(r'^[ \t]*#.*$', '', text, flags=re.M)


def test_every_host_tool_the_rig_runs_is_one_it_asked_for():
    """Every host tool the rig runs is one its preflight declares. An
    undeclared tool that is missing fails later with a message blaming
    something else."""
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
    """Positive control for the scan, which is a regex: removing a tool from
    the declaration makes the same comparison report it."""
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
    """The real qemu binary is found and its real `-M help` output satisfies
    the machine check. The only test here that needs the emulator, so CI
    skips it; the rest use a script pretending to be qemu."""
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
    """A qemu built without the raspi machines is refused by name, before the
    failure can arrive from inside the emulator."""
    proc = preflight("rng", stub_path(tmp_path, fake_qemu=NO_RASPI_QEMU))
    assert proc.returncode != 0
    assert "raspi3b" in proc.stderr
    assert "no 'raspi3b' machine" in proc.stderr


def test_a_qemu_with_the_raspi_machine_gets_through(tmp_path):
    """Positive control: the same stub with the raspi machine line gets
    through. Also the control for test_a_host_with_no_qemu_says_so."""
    proc = preflight("rng", stub_path(tmp_path, fake_qemu=RASPI_QEMU))
    assert proc.returncode == 0, proc.stderr
    assert "PREFLIGHT-OK" in proc.stdout


# A `-M help` far larger than a pipe buffer with raspi3b on the first machine
# line, so a `grep -q` closes the pipe while the producer is still writing.
# The loop is bash arithmetic because `seq` is not on the stubbed PATH.
BIG_RASPI_QEMU = NO_RASPI_QEMU.replace(
    'echo "virt                 QEMU 8.2 ARM Virtual Machine"',
    'echo "raspi3b              Raspberry Pi 3B (revision 1.2)"\n'
    '  i=0\n'
    '  while [ "$i" -lt 20000 ]; do i=$((i+1)); echo "machine$i            filler"; done')


def test_the_big_listing_fixture_really_is_big(tmp_path):
    """Positive control on the fixture: the listing really exceeds a pipe
    buffer, or the test below could not fail."""
    path = stub_path(tmp_path, fake_qemu=BIG_RASPI_QEMU)
    proc = subprocess.run([str(Path(path) / "qemu-system-aarch64"), "-M", "help"],
                          capture_output=True, text=True, timeout=120,
                          env={"PATH": path})
    assert proc.returncode == 0, proc.stderr
    # Well past a 64KiB pipe buffer.
    assert len(proc.stdout) > 200_000, len(proc.stdout)
    assert proc.stdout.splitlines()[1].startswith("raspi3b")


def test_the_machine_check_survives_a_producer_that_keeps_writing(tmp_path):
    """The machine check reads `-M help` to the end. With `grep -q` the
    producer gets SIGPIPE, the pipeline returns 141 under pipefail, and a
    capable host is reported as lacking the machine."""
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
    """Positive control: probes that need no module disk do not demand depmod,
    so a host without kmod can still run rng."""
    proc = preflight("rng",
                     stub_path(tmp_path, omit=("depmod",), fake_qemu=RASPI_QEMU))
    assert proc.returncode == 0, proc.stderr
    assert "PREFLIGHT-OK" in proc.stdout


# --- 4b. the plan pays for none of it, and the run pays for all of it -----
#
# Section 4 calls rig_preflight directly; these two CLI tests keep main()
# calling it on the run and not on the printing path.

#: A host with no qemu and no device-tree-compiler.
NO_EMULATOR = ("qemu-system-aarch64", "fdtput", "fdtget")


def test_the_plan_prints_on_a_host_with_no_emulator_at_all(tmp_path, offline):
    """--plan prints on a host with no emulator: it only builds strings, and
    it is what someone runs to decide whether to install the emulator."""
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, omit=NO_EMULATOR)
    proc = run_rig(["--plan", "rng"], env=env)
    assert proc.returncode == 0, proc.stderr
    assert "missing host tools" not in proc.stderr
    # The full argv, held against the rig's own argv function, minus the
    # tokens that are work-dir paths.
    printed = [line.strip() for line in proc.stdout.splitlines()]
    reference = rig_eval('rig_qemu_argv /K /D /I /C0 /C1')
    assert reference.returncode == 0, reference.stderr
    for token in reference.stdout.splitlines():
        if token.startswith(("/", "file:")):
            continue
        assert token in printed, f"the plan printed no {token!r}"
    # And it did none of the work: no deb, no kernel, no initramfs.
    work = Path(env["OTP_RIG_WORK"])
    assert sorted(p.name for p in work.iterdir()) == []


def test_the_plan_says_the_same_thing_with_the_tools_and_without(tmp_path,
                                                                 offline):
    """The plan's output is identical with and without the tools: the argv is
    what the reader takes away."""
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
    """An unpinned --plan must fetch the release it reports, so curl is
    required there and is named as a missing tool, not left to surface as a
    fetch error."""
    env = {"OTP_RIG_WORK": str(tmp_path / "work"),
           "PATH": stub_path(tmp_path, omit=("curl",), fake_qemu=RASPI_QEMU)}
    proc = run_rig(["--plan", "rng"], env=env)
    assert proc.returncode != 0
    assert "missing host tools" in proc.stderr
    assert "curl" in proc.stderr


def test_pinning_the_kernel_removes_even_that(tmp_path, offline):
    """Positive control: with the kernel pinned nothing is fetched, so the
    same host without curl is not refused."""
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, omit=("curl",), fake_qemu=RASPI_QEMU)
    proc = run_rig(["--plan", "rng"], env=env)
    assert proc.returncode == 0, proc.stderr
    assert "missing host tools" not in proc.stderr


def test_a_real_run_is_refused_before_it_costs_anything(tmp_path, offline):
    """A real run is refused by the preflight before it downloads anything:
    every unmet precondition otherwise produces the same silent console."""
    env = dict(offline)
    env["PATH"] = stub_path(tmp_path, omit=("qemu-system-aarch64",))
    proc = run_rig(["rng"], env=env)
    assert proc.returncode != 0
    assert "missing host tools" in proc.stderr
    assert "qemu-system-aarch64" in proc.stderr
    # From the preflight, not from a later stage tripping over the same
    # absence.
    assert "no Filename for linux-image-" not in proc.stderr


def test_a_real_run_with_the_tools_present_gets_past_the_preflight(tmp_path,
                                                                   offline):
    """Positive control: with nothing omitted the same run gets past the
    preflight. It stops at the missing archive index, a message that can
    only come from after the preflight."""
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
    """The metapackage's Depends is followed to the versioned kernel, so the
    rig boots the kernel pi-gen would install with no version string kept
    here."""
    proc = resolve(tmp_path, PACKAGES)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "6.12.96+rpt-rpi-v8"


def test_a_moved_archive_fails_loudly_rather_than_resolving_to_nothing(tmp_path):
    """A Depends with no versioned image fails loudly: an empty release would
    fail steps later inside a URL and a modules path."""
    # The realistic rot is a metapackage pointing at another metapackage, so
    # the fixture keeps the `linux-image-` prefix and removes only the
    # version; otherwise the prefix alone would reject it.
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
    """A field is read from its own stanza. Bleeding into the next package
    would return its Filename and boot the wrong kernel."""
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
    """A 64-byte arm64 Linux boot-protocol header (magic at offset 56),
    synthesised so the fixture does not depend on an archive."""
    p = tmp_path / name
    p.write_bytes(b"\x00" * 56 + magic + b"\x00" * 4)
    return p


def test_a_real_arm64_image_is_accepted(tmp_path):
    """Positive control: the check has to be able to pass."""
    p = arm64_image(tmp_path)
    proc = rig_eval(f'rig_require_arm64_image "{p}" && echo ACCEPTED')
    assert proc.returncode == 0, proc.stderr
    assert "ACCEPTED" in proc.stdout


def test_the_gzipped_vmlinuz_from_the_deb_is_refused(tmp_path):
    """The gzipped boot/vmlinuz-* the archive ships is refused. On a real Pi
    the raspi-firmware postinst decompresses it; QEMU's -kernel given the
    gzip writes nothing to either UART."""
    import gzip
    import os as _os
    p = tmp_path / "vmlinuz"
    # Incompressible filler keeps the gzip longer than the 64-byte header, so
    # the magic check is what refuses it and not the size check.
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
    """A file too short to hold a header is refused by the rig with its own
    message. Otherwise od(1) fails to skip, exits 1, and `set -e` ends the
    function with od's complaint alone."""
    p = tmp_path / "short"
    p.write_bytes(b"\x1f\x8b\x08\x00short")
    proc = rig_eval(f'rig_require_arm64_image "{p}"')
    assert proc.returncode != 0
    assert "too short" in proc.stderr
    assert "cannot skip past end" not in proc.stderr


# --- 7. the wrong busybox -------------------------------------------------

def elf(tmp_path, name, *, machine, interp=False):
    """A minimal ELF64 header with the fields the rig reads (e_machine and the
    program headers for PT_INTERP), built byte by byte so no toolchain is
    needed."""
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
    """A busybox for the wrong architecture is refused. It would stop silently
    after "Run /init as init process", which is also what a gzipped kernel,
    a wrong DTB or a dead console look like."""
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
    # Longer than the 64-byte header, so the magic check is exercised and not
    # the length check.
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
# The guest prints OTP-RIG-DONE, the sampler sees it and kills the emulator;
# `poweroff -f` does not stop -M raspi3b, so without the handshake every run
# waits out the backstop. These tests run rig_boot against a script
# pretending to be qemu and time it.

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

    Returns the process, the wall clock it took, and the work dir. The
    process is None if `wait` ran out, meaning rig_boot is still waiting
    out its backstop.
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
    # `|| rc=$?`, not `;`: rig_boot restores set -e before returning, so a
    # bare failing call would end the sourcing shell and print nothing.
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

    Module-scoped because it costs about six seconds: the sampler polls
    every two seconds and drains for two more. The 25s backstop against a
    12s deadline keeps a sampler that has stopped stopping cheap to detect.
    """
    return boot(tmp_path_factory.mktemp("clean"), "rng",
                "OTP-RIG-DONE rng status=ok", backstop=25, wait=12)


def test_the_marker_stops_the_emulator_instead_of_waiting_out_the_backstop(
        clean_boot):
    """The marker stops the emulator well before the backstop. The fake sits
    for 600s and the backstop is 25s, so a sampler that ignores the marker
    pays the full 25 and reports a run that never reported."""
    proc, seconds, _ = clean_boot
    assert proc is not None, (
        f"the marker was on the console and rig_boot had still not "
        f"returned {seconds:.0f}s later, with a 25s backstop under it. The "
        f"sampler is not stopping the emulator, which is the whole reason "
        f"this rig answers faster than tier 3.")
    assert "RIG-BOOT-RC=0" in proc.stdout, proc.stderr
    assert "reported after" in proc.stderr


def test_a_probe_that_never_reports_pays_the_backstop_and_says_so(tmp_path):
    """A guest that never prints a marker costs the backstop and gets its own
    exit code, so that a wedge is distinguishable from a probe that ran and
    found something wrong."""
    proc, seconds, _ = boot(tmp_path, "rng", "", backstop=6)
    assert "RIG-BOOT-RC=1" in proc.stdout, proc.stderr
    assert "NEVER REPORTED" in proc.stderr
    assert seconds >= 6, (
        f"rig_boot gave up after {seconds:.0f}s of a 6s backstop, so it "
        f"stopped on something other than the marker it never saw")


def test_the_rig_will_not_call_a_failed_probe_an_answer(tmp_path):
    """status=fail on the marker is a non-zero exit, distinct from the wedge
    code, so `rig && echo clean` cannot print clean for a failed probe."""
    proc, _, _ = boot(tmp_path, "rng", "OTP-RIG-DONE rng status=fail")
    assert "RIG-BOOT-RC=2" in proc.stdout, proc.stderr
    assert "REPORTED FAILURE" in proc.stderr
    # Not the wedge code: the guest reported, and reported a failure.
    assert "NEVER REPORTED" not in proc.stderr


def test_a_marker_with_no_status_at_all_is_not_a_pass(tmp_path):
    """Fail closed: a marker with no readable status stops the run but does
    not count as a pass."""
    proc, _, _ = boot(tmp_path, "rng", "OTP-RIG-DONE rng")
    assert "RIG-BOOT-RC=2" in proc.stdout, proc.stderr


def test_the_rig_captures_both_uarts_in_tier_3s_order(tmp_path):
    """The rig captures both UARTs in tier 3's order, read from the argv
    rig_boot actually wrote: the line the fake spoke on the first port
    lands in the file the rig points its reader at.

    It uses its own short backstop because the run only has to launch, and
    a reversed order stops the sampler ever seeing the marker.
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
# The probe bodies live in quoted `<<'PROBE'` heredocs, which shellcheck
# treats as data, and a syntax error there kills /init before the marker and
# looks like a wedge. So each probe's init is checked with `sh -n` and then
# run, to see that the marker's status reports work that did not happen.

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
    """Every probe's generated init passes `sh -n`."""
    for probe in declared_probes():
        proc = sh_n(init_script(probe))
        assert proc.returncode == 0, (
            f"the {probe} init does not parse, so /init dies before the "
            f"marker and the run reads as a wedge: {proc.stderr}")


def test_the_syntax_check_would_notice(tmp_path):
    """Positive control: `sh -n` rejects an unbalanced `if` in a probe body,
    which shellcheck does not see."""
    broken = init_script("rng").replace('echo "-- hwrng:"',
                                        'if [ "$broken" ; then')
    assert 'if [ "$broken" ; then' in broken, "the fixture patched nothing"
    assert sh_n(broken).returncode != 0


#: Commands the init runs that must not touch the test host: `mount` (it
#: mounts /proc, /sys and /dev), writers outside the temp dir, and module
#: loaders. Everything else is the host's own tool, so the rng body really
#: runs.
NEUTERED = ("mount", "mkdir", "ln", "modprobe", "depmod")

#: Every init ends in `while true; do sleep 5; done` because PID 1 exiting is
#: a kernel panic. On a host something has to end the run, so the five-second
#: sleep alone stops the shell; other sleeps are honoured.
SLEEP_STUB = """#!/bin/sh
if [ "$1" = "5" ]; then kill -TERM "$PPID"; exit 0; fi
__ONE__
"""


def run_init(tmp_path, probe, *, rc=None, stubs=None, real_sleep=True,
             moddir=None, idle_seconds="3", timeout=60):
    """Run the init the rig builds, under a host shell.

    `rc` overrides the exit status of named commands ({"dd": 1} is an
    unreadable /dev/random, {"mount": 1} a guest with no /proc); `stubs`
    replaces a command outright. Output is read from a file, not a pipe,
    because dash buffers.
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
    # The guest writes these two paths; both are documented in the rig.
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
    """Every probe's init prints BEGIN, the body, then the marker with a
    status, unconditionally, so a probe that dies half way still ends the
    run. Whether a probe passes on a host is not asserted."""
    for probe in declared_probes():
        text = run_init(tmp_path / probe, probe, real_sleep=False)
        assert f"OTP-RIG-BEGIN {probe}" in text, text
        status = marker_status(text, probe)
        assert status in ("ok", "fail"), (
            f"the {probe} init ended with {status!r}, not a marker the "
            f"sampler can read a verdict out of:\n{text}")


def test_a_probe_whose_work_happened_says_ok(tmp_path):
    """Positive control for the negatives below: the rng body runs for real on
    a Linux host and its marker says ok."""
    text = run_init(tmp_path, "rng")
    assert marker_status(text, "rng") == "ok", text
    assert "16 blocking bytes" in text, text


def test_the_rng_probe_will_not_report_a_read_that_did_not_happen(tmp_path):
    """The rng probe reports failure when dd could not read /dev/random,
    instead of printing a plausible timing for a read that never happened.
    img-boot.sh and harness/README.md cite this probe as evidence for the
    crng gate."""
    text = run_init(tmp_path, "rng", rc={"dd": 1})
    assert marker_status(text, "rng") == "fail", text
    assert "the blocking read did not happen" in text, text
    assert "16 blocking bytes" not in text, (
        "the probe still reported sixteen bytes it never read:\n" + text)


def test_a_mount_that_failed_is_not_swallowed(tmp_path):
    """A failed mount sets the failure status: a guest with no /dev is how the
    rng probe would report on entropy it never read."""
    text = run_init(tmp_path, "rng", rc={"mount": 1})
    assert marker_status(text, "rng") == "fail", text
    assert "could not mount" in text, text


#: busybox's own wording when devtmpfs is mounted over an existing /dev (exit
#: 255).
EBUSY_MOUNT = ('#!/bin/sh\n'
               'echo "mount: mounting $2 on $3 failed: '
               'Device or resource busy" >&2\nexit 255\n')


def test_a_mount_that_was_already_done_is_tolerated(tmp_path):
    """The one mount failure tolerated is EBUSY from a /dev the kernel already
    mounted (CONFIG_DEVTMPFS_MOUNT). The test above is the control: the
    same exit status with any other message is a failure."""
    text = run_init(tmp_path, "rng", stubs={"mount": EBUSY_MOUNT})
    assert marker_status(text, "rng") == "ok", text
    assert "already mounted" in text, text
    assert "could not mount" not in text, text


def test_the_idle_probe_will_not_report_time_it_did_not_spend(tmp_path):
    """The idle probe measures elapsed time, not loop iterations: a sleep that
    returns immediately must not yield a claim of having survived the
    interval."""
    fast = run_init(tmp_path / "fast", "idle-survive", real_sleep=False)
    assert marker_status(fast, "idle-survive") == "fail", fast
    assert "Nothing waited, so" in fast, fast
    assert "survived 3 seconds with no reset" not in fast, fast


def test_the_idle_probe_reports_time_it_did_spend(tmp_path):
    """Positive control, same fixture, three real seconds."""
    slow = run_init(tmp_path / "slow", "idle-survive", real_sleep=True)
    assert marker_status(slow, "idle-survive") == "ok", slow
    assert "survived 3 seconds with no reset" in slow, slow


#: Fails the module disk mount and nothing else. A blanket `mount` failure
#: would also fail the init's own mounts, which set the status themselves and
#: would mask the probe's own failure path.
EXT4_MOUNT_FAILS = ('#!/bin/sh\n'
                    'case " $* " in *" ext4 "*)\n'
                    '  echo "mount: mounting /dev/mmcblk0 failed" >&2\n'
                    '  exit 1 ;;\nesac\nexit 0\n')


def test_a_coldplug_that_mounted_nothing_says_so(tmp_path):
    """A coldplug probe that could not mount the module disk reports failure,
    so the rig does not exit 0 for a replay that mounted nothing."""
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
    """A module tree with no modules.dep is a failure: depmod runs on the host
    because the deb ships none, and without it modprobe resolves nothing."""
    text = run_init(tmp_path, "coldplug-replay", real_sleep=False)
    assert marker_status(text, "coldplug-replay") == "fail", text
    assert "no modules.dep for" in text, text


def test_a_coldplug_that_replayed_nothing_is_not_a_clean_replay(tmp_path):
    """A coldplug that replayed zero modaliases is a failure, not a clean
    replay: a loop that did no work has measured nothing."""
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
    """Positive control for the two above: same fixture with a modalias to
    find. Individual modprobe results are not the question, so the stub
    returns 0."""
    release = subprocess.run(["uname", "-r"], capture_output=True,
                             text=True).stdout.strip()
    text = run_init(tmp_path, "coldplug-replay", real_sleep=False,
                    moddir=moddisk(tmp_path, release))
    assert re.search(r'replayed [1-9]\d* modaliases', text), text
    assert marker_status(text, "coldplug-replay") == "ok", text


def test_a_console_probe_that_cannot_read_proc_says_so(tmp_path):
    """A console probe that cannot read /proc/consoles reports failure, since
    "(none)" would read as a finding about consoles."""
    text = run_init(tmp_path, "console-test", rc={"cat": 1})
    assert marker_status(text, "console-test") == "fail", text
    assert "/proc/consoles is unreadable" in text, text


def test_the_console_probe_is_happy_when_it_can_read_proc(tmp_path):
    """Positive control. "No serial tty nodes at all" is not a failure: a
    healthy guest registers ttyAMA1 in /proc/consoles with no /dev/ttyAMA1,
    and that is the finding the probe reports."""
    text = run_init(tmp_path, "console-test")
    assert marker_status(text, "console-test") == "ok", text


def test_the_rig_has_no_ci_wiring():
    """Nothing in CI invokes the rig (issue #22); it is only shellchecked.
    Running it would put a network fetch and an emulated boot on every
    push."""
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

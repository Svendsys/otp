"""The read-only root overlay: what install.sh does, and what proves it.

Issue #9. Nothing enabled the overlay -- install.sh printed advice, the
pi-gen stage printed the same advice, and the image booted read-write -- so
the property the whole design rests on, that pulling the plug erases the
session, was true only of units whose owner had run a command they were told
about in a paragraph of output.

The advice was also broken on trixie, which is why install.sh does not call
raspi-config. On bookworm and later `enable_overlayfs` installs Debian's
overlayroot package, whose initramfs script moves the root aside with
`mount --move`; the mount(8) an initramfs-tools initrd actually contains is
klibc's, which prints `mount: invalid option --` and takes init down with
it. That is the panic recorded in issue #9's first comment, and it is a
property of the package rather than of the guest it was first seen in.

These tests RUN the shipped blocks rather than pattern-matching them, for
the reason test_service_unit.py gives: the first version of the group guard
matched the text of a `for` header and never executed the body. Where a
block writes to absolute paths that a test has no business touching, it is
sliced at the boundary where its behaviour is still parameterised -- the
boot directory and the command-line file are variables, and the verification
that decides whether provisioning fails is entirely inside that boundary.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "device" / "install.sh"
IMG_BOOT = REPO / "harness" / "img-boot.sh"
GUEST_CHECK = REPO / "harness" / "img-guest-check.sh"
IMGCHECK_UNIT = REPO / "device" / "systemd" / "otp-unit-imgcheck.service"


def slice_between(text: str, first: str, last: str) -> str:
    """The shipped lines from `first` up to and including `last`."""
    start = text.index(first)
    end = text.index(last, start) + len(last)
    return text[start:end]


def run_block(block: str, tmp_path, *, env=None, preamble=""):
    """Run a slice of install.sh with a stub PATH and a fake boot directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # systemctl is stubbed rather than allowed through: the block disables a
    # first-boot service, and a test must not touch the machine it runs on.
    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").chmod(0o755)
    runner = tmp_path / "run.sh"
    runner.write_text(
        "set -euo pipefail\n"
        "log() { printf 'LOG %s\\n' \"$*\"; }\n"
        + preamble + "\n" + block + "\n"
    )
    return subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=60,
        env={**os.environ, **(env or {}),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )


# --- the kernel command line ---------------------------------------------

CMDLINE_BLOCK = ('    if ! grep -q "boot=overlay" "$CMDLINE_TXT"; then',
                 '    systemctl disable rpi-resize.service 2>/dev/null || true')

# pi-gen's own stage1 cmdline.txt, with export-image's PARTUUID substituted,
# which is what install.sh actually finds in the chroot.
PI_GEN_CMDLINE = ("console=serial0,115200 console=tty1 "
                  "root=PARTUUID=1a2b3c4d-02 rootfstype=ext4 fsck.repair=yes "
                  "rootwait resize\n")


def cmdline_after(tmp_path, before: str) -> str:
    boot = tmp_path / "boot"
    boot.mkdir(exist_ok=True)
    (boot / "cmdline.txt").write_text(before)
    block = slice_between(INSTALL.read_text(), *CMDLINE_BLOCK)
    proc = run_block(block, tmp_path,
                     preamble=f'CMDLINE_TXT="{boot}/cmdline.txt"')
    assert proc.returncode == 0, proc.stderr
    return (boot / "cmdline.txt").read_text()


def test_the_overlay_token_is_added_to_the_command_line(tmp_path):
    after = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    assert after.startswith("boot=overlay "), after
    # Everything else survives. Losing root= here would be an unbootable
    # card, and the token is prepended precisely so nothing else moves.
    assert "root=PARTUUID=1a2b3c4d-02" in after
    assert "rootfstype=ext4" in after


def test_adding_the_token_twice_does_not_add_it_twice(tmp_path):
    # install.sh says "safe to rerun" at the top of the file, and the
    # documented iteration loop is edit, rerun, reboot.
    once = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    twice = cmdline_after(tmp_path, once)
    assert twice.count("boot=overlay") == 1, twice


def test_the_first_boot_resize_token_is_removed(tmp_path):
    """
    An online resize cannot grow a filesystem mounted read-only as the
    overlay's lower layer, and a first-boot service that fails and reboots
    is a loop rather than a message. The appliance has nothing to grow.
    """
    after = cmdline_after(tmp_path, PI_GEN_CMDLINE)
    assert " resize" not in after and not after.endswith("resize\n"), after
    # Not a blanket deletion of the substring: fsck.repair and rootwait are
    # neighbours here and a sloppier sed would have taken part of one.
    assert "fsck.repair=yes" in after
    assert "rootwait" in after


# --- building the initramfs the overlay lives in --------------------------

def kernels_guard_block() -> str:
    """The empty-kernel-list guard, ending at its own `fi`.

    The end is computed rather than quoted for the same reason the build
    loop's is: an edit inside the block has to surface as a behavioural
    failure, not as a slice that stopped matching.
    """
    text = INSTALL.read_text()
    start = text.index('    if [ -z "$KERNELS" ]; then')
    return text[start:text.index("\n    fi", start) + len("\n    fi")]


def initramfs_build_block() -> str:
    """The per-kernel build loop, sliced without its trailing flag.

    The end anchor deliberately stops at `done` rather than quoting the
    `update-initramfs` line, so a change to that line shows up as a failed
    assertion about what was RUN rather than as a slice that no longer
    matches.
    """
    text = INSTALL.read_text()
    start = text.index("    for kern in $KERNELS; do")
    return text[start:text.index("\n    done", start) + len("\n    done")]


def run_initramfs_build(tmp_path, kernels):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    record = tmp_path / "argv.txt"
    (bin_dir / "update-initramfs").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {record}\n')
    (bin_dir / "update-initramfs").chmod(0o755)
    proc = run_block(initramfs_build_block(), tmp_path,
                     preamble=f'KERNELS="{kernels}"')
    return proc, (record.read_text() if record.exists() else "")


def test_the_initramfs_is_created_and_not_merely_updated(tmp_path):
    """
    `-c`, and the shipped comment names the exact defect: pi-gen sets
    update_initramfs=no in update-initramfs.conf so kernel installs build
    nothing, and update-initramfs honours that setting on its UPDATE path
    only. `-u` therefore prints "Not updating initramfs." and exits 0,
    leaving the overlay script out of an initramfs that already exists, has
    the right name and is the right size -- the firmware loads it,
    boot=overlay is read by nothing, and the unit comes up on a writable
    root. Nothing was red for that edit until this test.
    """
    kernels = "6.12.96+rpt-rpi-v8 6.12.96+rpt-rpi-2712"
    proc, argv = run_initramfs_build(tmp_path, kernels)
    assert proc.returncode == 0, proc.stderr
    # Every installed kernel, not just one: the image carries a second one
    # for the Pi 5, and an initramfs missing for the kernel that boots is
    # the same read-write root by another route.
    assert argv.split() and argv.splitlines() == [
        f"-c -k {kern}" for kern in kernels.split()], argv


def test_no_kernel_under_lib_modules_stops_provisioning(tmp_path):
    """
    In pi-gen's chroot `uname -r` is the BUILD HOST's kernel, for which
    there are no modules and no initramfs can be built -- so the kernel list
    is read out of /lib/modules, and an empty list means the overlay cannot
    be enabled at all. Building nothing and carrying on leaves the later
    verification to catch it, which is one guard rather than two.

    KERNELS is set here rather than discovered: the discovery loop reads
    /lib/modules on the machine running the test, and what it finds there
    is not this repository's business.
    """
    proc = run_block(kernels_guard_block(), tmp_path, preamble='KERNELS=""')
    assert proc.returncode != 0, proc.stdout
    assert "no kernel found" in proc.stderr, proc.stderr


def test_a_kernel_list_passes_the_guard(tmp_path):
    # The positive control: a guard that stopped everything would satisfy
    # the test above.
    proc = run_block(kernels_guard_block(), tmp_path,
                     preamble='KERNELS=" 6.12.96+rpt-rpi-v8"')
    assert proc.returncode == 0, proc.stderr


# --- the verification that decides whether provisioning fails -------------

VERIFY_BLOCK = ('    OVERLAY_INITRAMFS=""',
                '    log "  overlay initramfs: $OVERLAY_INITRAMFS"')


# A REAL-SIZED listing, with scripts/overlay early in it and a long tail
# after. Both details are load-bearing. install.sh runs under pipefail, and
# the first version of the check piped lsinitramfs into `grep -q`: grep
# closes the pipe on its first match, the producer dies of SIGPIPE, and the
# pipeline returns 141 -- so the check failed on exactly the initramfs that
# should pass. A three-line listing never reproduces it, because the producer
# is finished before grep can exit. Measured with the piped form and this
# fixture: NO MATCH, rc 141.
GOOD_LISTING = ("scripts/local\nscripts/overlay\n"
                + "".join(f"usr/lib/modules/6.12.96/kernel/drivers/x{i}.ko\n"
                          for i in range(20000)))
NO_OVERLAY_LISTING = GOOD_LISTING.replace("scripts/overlay\n", "")


def run_verification(tmp_path, *, initramfs_names=("initramfs8",),
                     listing=GOOD_LISTING,
                     cmdline="boot=overlay console=tty1\n"):
    boot = tmp_path / "boot"
    boot.mkdir(exist_ok=True)
    for name in initramfs_names:
        (boot / name).write_bytes(b"\x1f\x8b not really a cpio archive")
    (boot / "cmdline.txt").write_text(cmdline)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    # The real lsinitramfs unpacks a compressed cpio; what matters here is
    # what install.sh does with the listing, so the listing is the stub.
    (bin_dir / "lsinitramfs").write_text(
        "#!/bin/sh\ncat %s\n" % (tmp_path / "listing.txt"))
    (bin_dir / "lsinitramfs").chmod(0o755)
    (tmp_path / "listing.txt").write_text(listing)
    block = slice_between(INSTALL.read_text(), *VERIFY_BLOCK)
    return run_block(block, tmp_path,
                     preamble=f'BOOT_DIR="{boot}"\n'
                              f'CMDLINE_TXT="{boot}/cmdline.txt"')


def test_a_good_initramfs_passes_the_verification(tmp_path):
    # The positive control. Everything below asserts that provisioning
    # STOPS, and "it stopped" is also what a broken rig produces.
    proc = run_verification(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "initramfs8" in proc.stdout, proc.stdout


def test_no_initramfs_at_all_fails_the_install(tmp_path):
    proc = run_verification(tmp_path, initramfs_names=())
    assert proc.returncode != 0
    assert "scripts/overlay" in proc.stderr, proc.stderr


def test_an_initramfs_without_the_overlay_script_fails_the_install(tmp_path):
    """
    The silent case, and the reason this check reads the archive rather than
    stat()ing the file. An initramfs built before the overlay script existed
    is the right size, has the right name, and is loaded by the firmware --
    and boot=overlay is then read by nothing, so the unit comes up on a
    writable root and says nothing about it.
    """
    proc = run_verification(tmp_path, listing=NO_OVERLAY_LISTING)
    assert proc.returncode != 0
    assert "would come up on a writable root" in proc.stderr, proc.stderr


def test_the_command_line_token_going_missing_fails_the_install(tmp_path):
    proc = run_verification(tmp_path, cmdline="console=tty1\n")
    assert proc.returncode != 0
    assert "boot=overlay is not in" in proc.stderr, proc.stderr


# --- what the initramfs script itself does -------------------------------

def overlay_script() -> str:
    text = INSTALL.read_text()
    return slice_between(text, "# Mount the root filesystem read-only under",
                         "OVERLAY\n")


def test_the_lower_layer_is_mounted_read_only():
    # The line that makes the card read-only for the life of the boot. An
    # overlay whose lower layer is writable is a slower way of writing to
    # the SD card, not a reset-on-power-cycle appliance.
    lower = [line for line in overlay_script().splitlines()
             if "/lower" in line and "mount " in line]
    assert lower, "nothing in the overlay script mounts a lower layer"
    assert all(" -r " in line for line in lower), lower


def test_a_failed_overlay_panics_rather_than_booting_writable():
    """
    The direction of the failure is a safety decision, so it is asserted.

    A unit that quietly came up without the overlay would keep on the card
    everything the session wrote -- the one outcome this mechanism exists to
    prevent -- and would look identical from the outside. Panicking is loud;
    tier 3 catches it before a release.
    """
    script = overlay_script()
    assert "panic \"Failed to assemble the root overlay.\"" in script
    assert "panic \"Failed to mount the overlay's upper layer in RAM.\"" in script


def test_install_sh_no_longer_tells_the_operator_to_run_raspi_config():
    """
    Not tidiness. On trixie that command installs overlayroot, whose
    initramfs script calls `mount --move` -- an option klibc's mount, the
    one in the initrd, does not have. Following the old advice is how a
    provisioned unit stops booting.
    """
    text = INSTALL.read_text()
    instructions = [line for line in text.splitlines()
                    if "enable_overlayfs" in line and not line.lstrip().startswith("#")]
    assert not instructions, instructions


# --- the harness end ------------------------------------------------------

def test_the_harness_copies_the_overlay_token_and_never_invents_one(tmp_path):
    """
    Adding boot=overlay to -append when the image's own cmdline.txt does not
    carry it would make tier 3 prove its own command line rather than the
    artifact. An image built without the overlay has to boot without it here
    too, and fail.
    """
    block = slice_between(IMG_BOOT.read_text(), 'OVERLAY_TOKENS=""', "esac")
    for cmdline, expected in (
        ("boot=overlay console=tty1 root=PARTUUID=1a2b-02", "boot=overlay"),
        ("console=tty1 root=PARTUUID=1a2b-02", ""),
        # Not a substring match: a token that merely contains the word is
        # not the token.
        ("console=tty1 noboot=overlayfs", ""),
    ):
        runner = tmp_path / "tokens.sh"
        runner.write_text(f'IMG_CMDLINE="{cmdline}"\n{block}\nprintf "%s" "$OVERLAY_TOKENS"\n')
        proc = subprocess.run(["bash", str(runner)], capture_output=True,
                              text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == expected, f"{cmdline!r} -> {proc.stdout!r}"


def test_the_harness_hands_the_initramfs_to_qemu():
    # Without -initrd the kernel boots with no initramfs, boot=overlay is
    # read by nothing, and every overlay check would fail for a reason that
    # is the harness's fault rather than the image's.
    assert '${INITRD:+-initrd "$INITRD"}' in IMG_BOOT.read_text()


def test_the_guest_probe_cannot_run_on_a_flashed_unit():
    """
    The probe ships in the image, so what keeps it off a real appliance is
    this one condition. systemd matches a bare word against both the word
    and the left hand side of an assignment, so otp.imgcheck=boot1 starts
    it and a device command line that says nothing about it does not.
    """
    unit = IMGCHECK_UNIT.read_text()
    assert "ConditionKernelCommandLine=otp.imgcheck" in unit
    # And the harness is the only thing that supplies it.
    assert "otp.imgcheck=$phase" in IMG_BOOT.read_text()


# --- the build refuses to ship an image it cannot probe ------------------

PROBE_INSTALL_BLOCK = ('if [ -f "$REPO_DIR/harness/img-guest-check.sh" ]; then',
                       'would have nothing to report with."\nfi')

STAGE_RUN = REPO / "image" / "stage-otpunit" / "01-otpunit" / "00-run.sh"
STAGE_COPY_BLOCK = ("for item in otpunit codewords device harness",
                    'Run image/render-manual.sh before building." >&2\nfi')


def run_probe_install(tmp_path, *, repo_dir, image_build):
    """Run install.sh's probe-install block against a substitute REPO_DIR.

    `install` is shadowed by a shell function rather than stubbed on PATH:
    the block installs into /etc/systemd/system and /opt/otp-unit, and a
    test has no business writing to either.
    """
    block = slice_between(INSTALL.read_text(), *PROBE_INSTALL_BLOCK)
    return run_block(block, tmp_path,
                     preamble=("install() { printf 'INSTALL %s\\n' \"$*\"; }\n"
                               f'REPO_DIR="{repo_dir}"\n'
                               f'PREFIX="{tmp_path}/opt"\n'
                               f"IMAGE_BUILD={image_build}"))


def test_an_image_build_without_the_probe_fails(tmp_path):
    """
    Every other overlay precondition exits 1 under --image-build; this one
    printed a NOTE and carried on, so a build could ship an image whose
    overlay nothing can observe. Deleting the install line survived the
    whole fast suite, and the miss only appeared after a pi-gen build and
    two emulated boots -- as a guest that never reported, which reads as a
    boot failure rather than a packaging one.
    """
    empty = tmp_path / "repo"
    (empty / "harness").mkdir(parents=True)
    proc = run_probe_install(tmp_path, repo_dir=empty, image_build=1)
    assert proc.returncode != 0, proc.stdout
    assert "nothing to report on the" in proc.stderr, proc.stderr


def test_a_hand_provisioned_machine_without_the_probe_only_says_so(tmp_path):
    # Not an image build: someone running install.sh from a partial copy on
    # a Pi gets a working unit and a note, which is what they had before.
    empty = tmp_path / "repo"
    (empty / "harness").mkdir(parents=True)
    proc = run_probe_install(tmp_path, repo_dir=empty, image_build=0)
    assert proc.returncode == 0, proc.stderr
    assert "NOTE" in proc.stdout, proc.stdout


def test_the_probe_is_installed_when_it_is_there(tmp_path):
    # The positive control, against the real repository.
    proc = run_probe_install(tmp_path, repo_dir=REPO, image_build=1)
    assert proc.returncode == 0, proc.stderr
    assert "img-guest-check.sh" in proc.stdout, proc.stdout
    assert "otp-unit-imgcheck.service" in proc.stdout, proc.stdout


def run_stage_copy(tmp_path, *, present):
    """Run the pi-gen stage's copy loop over a synthetic repository."""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    for item in present:
        if item.endswith(".py") or item.endswith(".md"):
            (src / item).write_text("x")
        else:
            (src / item).mkdir(exist_ok=True)
    rootfs = tmp_path / "rootfs"
    (rootfs / "tmp" / "otp-src").mkdir(parents=True, exist_ok=True)
    block = slice_between(STAGE_RUN.read_text(), *STAGE_COPY_BLOCK)
    runner = tmp_path / "stage.sh"
    runner.write_text(
        f'set -e\nREPO_SRC="{src}"\nROOTFS_DIR="{rootfs}"\n'
        'STAGING=/tmp/otp-src\n' + block)
    return subprocess.run(["bash", str(runner)], capture_output=True,
                          text=True, timeout=60)


ALL_ITEMS = ("otpunit", "codewords", "device", "harness",
             "otp_generator.py", "otp.md", "assets")


def test_the_stage_copies_everything_install_sh_reads(tmp_path):
    proc = run_stage_copy(tmp_path, present=ALL_ITEMS)
    assert proc.returncode == 0, proc.stderr
    for item in ALL_ITEMS:
        assert (tmp_path / "rootfs" / "tmp" / "otp-src" / item).exists(), item


def test_a_missing_item_stops_the_stage_instead_of_being_skipped(tmp_path):
    """
    The copy was `if [ -e ]`, so dropping a name from the list -- or from
    the repository -- was a silent no-op. `harness` is the one that did not
    even fail in the chroot: install.sh printed a NOTE and built an image
    whose overlay nothing could probe.
    """
    for missing in ALL_ITEMS:
        if missing == "assets":
            continue
        present = [i for i in ALL_ITEMS if i != missing]
        proc = run_stage_copy(tmp_path / missing, present=present)
        assert proc.returncode != 0, f"{missing} missing was accepted"
        assert missing in proc.stderr, proc.stderr


def test_the_assets_directory_stays_optional(tmp_path):
    # install.sh guards its own use of the manual PDFs, and an image
    # without them boots and prints pads. The warning is the whole penalty.
    present = [i for i in ALL_ITEMS if i != "assets"]
    proc = run_stage_copy(tmp_path, present=present)
    assert proc.returncode == 0, proc.stderr
    assert "manual PDFs missing" in proc.stderr, proc.stderr


# --- the probe refuses to run on anything but a harness boot -------------

PHASE_GUARD_BLOCK = ('PHASE=""', "\nesac")

# What a provisioned unit actually boots with: install.sh prepends
# boot=overlay and strips `resize`, and nothing anywhere adds otp.imgcheck.
DEVICE_CMDLINE = ("boot=overlay console=serial0,115200 console=tty1 "
                  "root=PARTUUID=1a2b3c4d-02 rootfstype=ext4 fsck.repair=yes "
                  "rootwait\n")


def run_phase_guard(tmp_path, cmdline, *, cwd=None):
    """Run the shipped phase parse and guard against a substituted cmdline.

    The path is rewritten rather than the file stubbed, so the test still
    describes the shipped bytes: everything else in the block -- the `set
    -f`, the token match, the case -- is the code that ships.
    """
    block = slice_between(GUEST_CHECK.read_text(), *PHASE_GUARD_BLOCK)
    assert block.count("/proc/cmdline") == 1, block
    fake = tmp_path / "cmdline"
    fake.write_text(cmdline)
    runner = tmp_path / "phase.sh"
    runner.write_text(
        "set -uo pipefail\n"
        "BOOTDIR=/boot/firmware\n"
        + block.replace("/proc/cmdline", str(fake))
        + '\nprintf "REACHED-THE-CHECKS phase=%s\\n" "$PHASE"\n')
    return subprocess.run(["bash", str(runner)], capture_output=True,
                          text=True, timeout=30, cwd=str(cwd or tmp_path))


def test_the_probe_is_inert_on_a_command_line_without_the_token(tmp_path):
    """
    The HIGH one. This script ships 0755 in /opt/otp-unit on every unit and
    its service is enabled unconditionally, and what it does is WRITE: a
    sentinel to /, and pages=137 through config.save() into
    /boot/firmware/otp-unit.conf, which is outside the overlay and therefore
    permanent. The version before this guard read no token, set
    PHASE=unknown and ran every check anyway -- run against a realistic
    device command line it took an operator's page count from 500 to 137.
    """
    proc = run_phase_guard(tmp_path, DEVICE_CMDLINE)
    assert proc.returncode != 0, proc.stdout
    assert "REACHED-THE-CHECKS" not in proc.stdout, proc.stdout
    assert "refusing to run" in proc.stderr, proc.stderr


def test_the_probe_runs_for_the_two_phases_the_harness_supplies(tmp_path):
    # The positive control: everything else here asserts a refusal, and a
    # script that refused everything would satisfy all of them.
    for phase in ("boot1", "boot2"):
        proc = run_phase_guard(
            tmp_path, f"{DEVICE_CMDLINE.strip()} otp.imgcheck={phase}\n")
        assert proc.returncode == 0, proc.stderr
        assert f"REACHED-THE-CHECKS phase={phase}" in proc.stdout, proc.stdout


def test_a_phase_that_is_not_one_of_the_two_is_refused(tmp_path):
    # `otp.imgcheck` bare is the interesting one: systemd's
    # ConditionKernelCommandLine matches a bare word as well as an
    # assignment, so that token starts the unit and names no phase.
    for token in ("otp.imgcheck", "otp.imgcheck=", "otp.imgcheck=boot3",
                  "otp.imgcheck=unknown"):
        proc = run_phase_guard(tmp_path, f"{DEVICE_CMDLINE.strip()} {token}\n")
        assert proc.returncode != 0, token
        assert "REACHED-THE-CHECKS" not in proc.stdout, token


def test_the_command_line_is_split_but_not_glob_expanded(tmp_path):
    """
    The unit runs as root with a working directory of /, and the token loop
    reads an unquoted command substitution. With pathname expansion left on,
    a bare `*` in the command line is replaced by the listing of the current
    directory before the case sees it -- so a FILE whose name begins
    otp.imgcheck= chooses the phase, and the probe starts writing.
    """
    root = tmp_path / "fake-root"
    root.mkdir()
    (root / "otp.imgcheck=boot2").write_text("")
    (root / "vmlinuz").write_text("")
    proc = run_phase_guard(tmp_path, "console=tty1 * rootwait\n", cwd=root)
    assert proc.returncode != 0, proc.stdout
    assert "REACHED-THE-CHECKS" not in proc.stdout, proc.stdout
    assert "vmlinuz" not in proc.stderr, proc.stderr


def test_the_refusal_comes_before_anything_the_probe_writes():
    # Position, not behaviour: a guard that refuses AFTER the first write
    # has already rewritten the operator's settings.
    text = GUEST_CHECK.read_text()
    guard = text.index('case "$PHASE" in')
    for later, what in (("check root-is-overlay", "the first check"),
                        ('> "$SENTINEL"', "the sentinel write"),
                        ("config.save(saved)", "the config.save() into /boot")):
        assert guard < text.index(later), what


# --- the drop-in that keeps the first-boot wizard usable ------------------

# The end anchor is the heredoc's LAST line, not the delimiter's first
# appearance: `slice_between` takes the first match, and "DROPIN\n" first
# appears on the `cat <<DROPIN` line itself -- which cut the body off and
# wrote an empty drop-in that these tests then passed judgement on.
DROPIN_BLOCK = ("systemctl unmask userconfig.service", "StandardInput=null\nDROPIN")
DROPIN_DIR = "/etc/systemd/system/userconfig.service.d"


def run_dropin(tmp_path, *, boot_dir):
    """Write the shipped drop-in into a substituted /etc/systemd/system.

    The unit directory is rewritten rather than stubbed: a test has no
    business writing into the machine's real /etc/systemd/system, and
    everything that decides what the file SAYS -- the two conditions, the
    directory they name, StandardInput -- stays the shipped bytes.
    """
    etc = tmp_path / "etc"
    block = slice_between(INSTALL.read_text(), *DROPIN_BLOCK)
    assert block.count(DROPIN_DIR) == 2, block
    block = block.replace(DROPIN_DIR, f"{etc}/userconfig.service.d")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nBOOT_DIR="{boot_dir}"\n{block}'],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    written = etc / "userconfig.service.d" / "otp-appliance.conf"
    return written.read_text(), (tmp_path / "systemctl.log").read_text()


def test_the_wizard_runs_only_when_an_operator_seeded_it(tmp_path):
    """
    Both spellings, both as `|=`, or the unit is worse than before.

    A single ConditionPathExists is an AND with itself and would demand BOTH
    files; the pair with `|` is an OR group, which is what makes "either
    spelling of the seed" work. And the file has to be one an operator
    actually writes -- userconf-pi reads `userconf` first and `userconf.txt`
    second, and Raspberry Pi Imager writes the latter.
    """
    text, log = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "ConditionPathExists=|/boot/firmware/userconf\n" in text, text
    assert "ConditionPathExists=|/boot/firmware/userconf.txt\n" in text, text
    # Not masked. Masking is the fix that was tried and reverted: it stops the
    # prompt and silently discards every credential file too.
    assert "unmask userconfig.service" in log, log
    assert "mask userconfig.service" not in log.replace("unmask", ""), log


def test_the_wizard_cannot_reach_a_terminal(tmp_path):
    # Without this a malformed seed falls back to the interactive prompt and
    # holds multi-user.target open on a machine with no visible tty.
    text, _ = run_dropin(tmp_path, boot_dir="/boot/firmware")
    assert "StandardInput=null" in text, text


def test_the_condition_follows_the_boot_directory_that_exists(tmp_path):
    """
    install.sh falls back to BOOT_DIR=/boot when /boot/firmware is not a
    directory, and userconf-service's own get_fw_loc makes the same choice.
    Hardcoded, the pair named a path that cannot exist on that layout: the
    condition is false on every boot, the unit never runs, and the operator's
    credentials are ignored in silence -- the exact outcome masking the unit
    produced, arriving by a different route.
    """
    text, _ = run_dropin(tmp_path, boot_dir="/boot")
    assert "ConditionPathExists=|/boot/userconf\n" in text, text
    assert "ConditionPathExists=|/boot/userconf.txt\n" in text, text
    assert "/boot/firmware" not in text, text


# --- the two things that kept the wizard from ever running ----------------

# Run 31968966879 is the first run that seeded a real userconf.txt, and it
# found that the drop-in above had never been the deciding factor: the unit's
# start job never came up for execution at all. It was ordered behind
# cloud-init's config stage, which waits on network-online.target, which waits
# on systemd-networkd-wait-online.service -- TimeoutStartSec=infinity on a box
# whose links NetworkManager owns. Both boots ended with that job still
# running and multi-user.target never reached.

BOOT_FINISHES_BLOCK = ("systemctl mask systemd-networkd-wait-online.service",
                       ": > /etc/cloud/cloud-init.disabled")


def run_boot_finishes(tmp_path):
    """Run the shipped block that clears the two blockers, into a fake /etc.

    /etc/cloud is rewritten to a temporary directory; nothing else is, so
    the unit name masked and the kill switch's own filename stay the bytes
    that ship.
    """
    cloud = tmp_path / "cloud"
    block = slice_between(INSTALL.read_text(), *BOOT_FINISHES_BLOCK)
    assert block.count("/etc/cloud") == 2, block
    block = block.replace("/etc/cloud", str(cloud))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{block}"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    return (tmp_path / "systemctl.log").read_text(), cloud


def test_the_unbounded_network_wait_is_masked(tmp_path):
    """
    The job both boots of run 31968966879 ended on, in the console's own
    words: `systemd-networkd-wait-online.service/start running (2min 37s /
    no limit)`. "No limit" is the unit's TimeoutStartSec=infinity, and this
    appliance has no networkd configuration for it to ever be satisfied by.
    Everything ordered after network-online.target -- cloud-init's config
    stage, and through it the credential wizard -- waits for the life of
    the boot.
    """
    log, _ = run_boot_finishes(tmp_path)
    assert "mask systemd-networkd-wait-online.service" in log, log


def test_cloud_init_is_switched_off_on_an_air_gapped_printer(tmp_path):
    """
    The other end of the same chain, and worth doing on its own account.

    cloud-init is what pulls network-online.target into the transaction
    here; it spent 57 seconds of every boot finding no datasource; and it
    is a provisioning agent that takes user-data off the boot partition --
    the one partition an operator is told to write files on -- on a device
    that prints one-time pads and has no network by design. Its generator
    reads this file before it links cloud-init.target into
    multi-user.target, so cloud-config.service is not in the transaction at
    all and userconfig.service's `After=` on it becomes void.
    """
    _, cloud = run_boot_finishes(tmp_path)
    assert (cloud / "cloud-init.disabled").exists(), sorted(
        p.name for p in cloud.iterdir()) if cloud.exists() else "no /etc/cloud"


GETTY_BLOCK = ("install -d /etc/systemd/system/getty@tty1.service.d",
               "systemctl stop getty@tty1.service 2>/dev/null || true")
GETTY_DIR = "/etc/systemd/system/getty@tty1.service.d"


def run_getty_dropin(tmp_path):
    """Write the shipped getty drop-in into a substituted unit directory."""
    etc = tmp_path / "etc"
    block = slice_between(INSTALL.read_text(), *GETTY_BLOCK)
    assert block.count(GETTY_DIR) == 2, block
    block = block.replace(GETTY_DIR, f"{etc}/getty@tty1.service.d")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{block}"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    written = etc / "getty@tty1.service.d" / "otp-appliance.conf"
    log = tmp_path / "systemctl.log"
    return written.read_text(), (log.read_text() if log.exists() else "")


def test_no_login_prompt_can_start_on_the_front_panels_tty(tmp_path):
    """
    tty1 is the front panel, and the thing that starts a getty on it is the
    credential path itself: /usr/lib/userconf-pi/userconf ends in
    cancel-rename, which ends in `systemctl --no-block start getty@tty1` on
    every console-boot machine. A condition, evaluated at start time, is
    what keeps that from ever producing a prompt.

    THE SPELLING IS THE PROPERTY here, which is why this reads the exact
    directives rather than "a condition mentioning otp-unit". The form that
    shipped first --
    `ConditionPathExists=!/etc/systemd/system/otp-unit.service` -- keys on
    the unit FILE, and `systemctl mask` writes a symlink to /dev/null at
    exactly that path while `systemctl disable` leaves the file alone. Both
    therefore left the condition false and tty1 with no getty at all, on a
    machine whose panel was not going to run either. The truth table is in
    the test below; this is the always-runs half of it.
    """
    text, _ = run_getty_dropin(tmp_path)
    assert ("ConditionPathExists=|!/etc/systemd/system/multi-user.target"
            ".wants/otp-unit.service" in text), text
    assert ("ConditionFileNotEmpty=|!/etc/systemd/system/otp-unit.service"
            in text), text
    # The `|` on both is what makes them an OR. Un-prefixed, systemd ANDs
    # them and the prompt comes back only on a panel that is disabled AND
    # masked -- neither of the two states this exists for.
    assert "\nConditionPathExists=!" not in text, (
        "an un-prefixed condition is ANDed with the other one: " + text)
    assert text.lstrip().startswith("[Unit]"), text


GETTY_STATES = ("healthy", "masked", "disabled", "absent")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None,
                    reason="no systemd-analyze here to evaluate the "
                           "conditions with; the text assertions above "
                           "still run")
@pytest.mark.parametrize("state", GETTY_STATES)
def test_masking_or_disabling_the_panel_gives_tty1_its_login_back(
        tmp_path, state):
    """
    The four states a machine can be in, evaluated by real systemd.

    Only the healthy one may skip the getty. On the other three the panel is
    not going to take tty1, and a tty with neither a panel nor a login is a
    device an operator cannot reach -- the exact half-provisioned case the
    drop-in's own comment says the prompt exists for. The shipped form got
    two of the three wrong: `systemctl mask` REPLACES the unit file with a
    symlink to /dev/null and ConditionPathExists follows symlinks, and
    `systemctl disable` does not touch the unit file at all.

    The conditions are lifted out of the drop-in install.sh writes and only
    the /etc prefix is rewritten, so what systemd is handed here is the
    shipped directive with a different root.
    """
    text, _ = run_getty_dropin(tmp_path)
    etc = tmp_path / "sysroot" / "etc" / "systemd" / "system"
    (etc / "multi-user.target.wants").mkdir(parents=True)
    unit = etc / "otp-unit.service"
    wants = etc / "multi-user.target.wants" / "otp-unit.service"
    if state != "absent":
        unit.write_text("[Unit]\nDescription=OTP pad print unit\n")
        wants.symlink_to(unit)
    if state == "masked":
        unit.unlink()
        unit.symlink_to("/dev/null")
    if state == "disabled":
        wants.unlink()

    conditions = [ln.replace("/etc/systemd/system", str(etc))
                  for ln in text.splitlines() if ln.startswith("Condition")]
    assert len(conditions) == 2, text
    proc = subprocess.run(["systemd-analyze", "condition", *conditions],
                          capture_output=True, text=True, timeout=30)
    starts = proc.returncode == 0
    assert starts == (state != "healthy"), (
        f"with the panel {state}, systemd says the tty1 getty "
        f"{'starts' if starts else 'is skipped'}: {proc.stdout}{proc.stderr}")


def test_a_getty_already_running_on_tty1_is_stopped(tmp_path):
    """
    The half a condition cannot do, and tier 2 is where it was measured.

    `sudo ./device/install.sh` on a Pi that is already up meets a getty that
    started at boot, before any of this existed. otp-unit.service's
    Conflicts= used to stop it as a side effect of the unit starting; with
    the conflict gone and nothing in its place, the run went 11/15 with
    `getty1-stopped FAIL getty@tty1=active` and the unit `Started` then
    `Deactivated successfully` a second later -- the getty's Restart=always
    and TTYVHangup take /dev/tty1 back off the panel, whose Python exits.

    After the reload, not before it: the condition has to be in force before
    the getty is stopped, or anything that pokes it in between starts a new
    one.
    """
    _, log = run_getty_dropin(tmp_path)
    assert "stop getty@tty1.service" in log, log
    assert log.index("daemon-reload") < log.index("stop getty@tty1.service"), log


def test_the_getty_is_conditioned_rather_than_masked(tmp_path):
    """
    Masking looks equivalent and is not. `systemctl start` on a masked unit
    FAILS, cancel-rename's exit status is that command's, `userconf` hands
    it up, and userconf-service runs under `sh -e` -- so it dies before
    `rm`-ing the applied seed. Stock userconfig.service's Restart=on-failure
    then loops, printing "Failed with result" and "Scheduled restart job",
    two of the strings img-boot.sh fails a release on. A condition-skipped
    start returns success and does nothing.
    """
    _, log = run_getty_dropin(tmp_path)
    assert "mask getty@tty1" not in log, log


# --- the seeded userconf.txt path, as the guest sees it -------------------

# The stock unit is `StandardInput=tty`, `TTYPath=/dev/tty8` and
# `Restart=on-failure` (RPi-Distro/userconf-pi, debian/*.userconfig.service),
# and userconf-service goes INTERACTIVE whenever raspi-config's get_boot_cli
# says the machine boots to a console -- which this image does. That is the
# whiptail run 12's boot parked on. device/install.sh's drop-in is what stops
# it: the ConditionPathExists pair keeps the unit out of an unseeded boot
# entirely, and StandardInput=null turns a malformed seed into a fast failure
# instead of a prompt nobody can answer. These run the shipped block that
# checks all of that, against a systemd that says whatever the case needs.

HELPERS = ('check() {',
           'yesno() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }')

SYSTEMCTL_STUB = """#!/bin/sh
# Answers only what the probe asks, and says so loudly otherwise: a stub that
# silently returned "" for an unexpected question would make a check pass by
# accident, which is the failure this file exists to prevent.
case "$1 $2" in
    "show userconfig.service")
        case "$4" in
            ConditionTimestamp) printf '%s' "${UC_TS-}" ;;
            ConditionResult)    printf '%s' "${UC_COND-}" ;;
            Result)             printf '%s' "${UC_RESULT-}" ;;
            StandardInput)      printf '%s' "${UC_STDIN-}" ;;
            *) echo "stub: unexpected property $4" >&2; exit 64 ;;
        esac
        echo ;;
    "is-active userconfig.service")  echo "${UC_ACTIVE-inactive}" ;;
    "is-enabled userconfig.service") echo "${UC_ENABLED-enabled}" ;;
    "is-active otp-unit.service")    echo "${PANEL_ACTIVE-active}" ;;
    "is-active getty@tty1.service")  echo "${TTY1_ACTIVE-inactive}" ;;
    "is-enabled systemd-networkd-wait-online.service")
        echo "${NETWAIT_ENABLED-masked}" ;;
    # ONE queue for every question asked of it, because that is what the
    # machine has. A stub that answered the wizard's job count out of one
    # variable and the getty's out of another would let a test describe a
    # boot systemd cannot produce.
    "list-jobs --no-legend")         printf '%s' "${JOBS-}" ;;
    *) echo "stub: unexpected systemctl $*" >&2; exit 64 ;;
esac
"""

# The wizard's own log, which is the only account of it that survives the
# apply: cancel-rename disables the unit, systemd collects it, and every
# `systemctl show` after that describes a freshly loaded default. The
# default text is what run 31972140190's console recorded, verbatim.
JOURNALCTL_STUB = """#!/bin/sh
case "$*" in
    *userconfig.service*) printf '%s\\n' "${UC_JOURNAL-\
Aug 16 22:05:01 otp-unit systemd[1]: Starting userconfig.service - User configuration dialog...
Aug 16 22:06:11 otp-unit systemd[1]: Finished userconfig.service - User configuration dialog.}" ;;
    *) echo "stub: unexpected journalctl $*" >&2; exit 64 ;;
esac
"""

# What upstream does to a seed it will not accept: append the reason, rename
# it out of the way, and -- with no tty for the whiptail that comes next --
# die rather than prompt. The probe measures three things about this: that it
# ENDED, that userconf.txt is gone, and that the evidence is there.
USERCONF_SERVICE_STUB = """#!/bin/sh
cat "$BOOTDIR/userconf.txt" >> "$BOOTDIR/failed_userconf.txt"
rm -f "$BOOTDIR/userconf.txt"
echo "Entered username is invalid:"
exit 1
"""


def userconf_block() -> str:
    text = GUEST_CHECK.read_text()
    start = text.index("# --- the seeded userconf.txt path")
    return text[start:text.index("\nsync 2>/dev/null || true", start)]


def run_userconf(tmp_path, *, phase, service=USERCONF_SERVICE_STUB,
                 executable=True, shadow=None, env=None):
    """Run the shipped userconf block with a substituted systemd and shadow.

    Three absolute paths are rewritten rather than stubbed on the filesystem
    -- /etc/shadow, the userconf-service the experiment runs, and the boot
    directory -- for the reason run_phase_guard gives: everything else in the
    block, including both `check` calls' conditions and the wording they
    print, is the code that ships. The two waits are shortened, and only
    those: the 30s condition poll and the 60s bound on the experiment are
    real time in a real boot, and test_the_probe_is_given_longer_than_its_own
    _bounded_wait is what holds them against the unit's TimeoutStartSec.
    """
    bootdir = tmp_path / "firmware"
    bootdir.mkdir(parents=True, exist_ok=True)
    shadow_file = tmp_path / "shadow"
    shadow_file.write_text(
        "root:!:20000:0:99999:7:::\n"
        + (shadow if shadow is not None
           else "otp:$6$otpimgcheck$hashbytes:20000:0:99999:7:::\n"))
    svc = tmp_path / "userconf-service"
    if service is not None:
        svc.write_text(service)
        svc.chmod(0o755 if executable else 0o644)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(SYSTEMCTL_STUB)
    (bin_dir / "systemctl").chmod(0o755)
    (bin_dir / "journalctl").write_text(JOURNALCTL_STUB)
    (bin_dir / "journalctl").chmod(0o755)

    block = userconf_block()
    for original, replacement, why in (
        ("/etc/shadow", str(shadow_file), "the shadow file"),
        ("/usr/lib/userconf-pi/userconf-service", str(svc), "the service"),
        ("timeout -k 5 60", "timeout -k 1 2", "the experiment's bound"),
        ("sleep 2", "sleep 0", "the condition poll"),
    ):
        assert block.count(original) == 1, f"{why}: {original!r} in the block"
        block = block.replace(original, replacement)

    runner = tmp_path / "userconf.sh"
    runner.write_text(
        "set -uo pipefail\n"
        f'PHASE="{phase}"\nBOOTDIR="{bootdir}"\nPASS=0\nTOTAL=0\n'
        + slice_between(GUEST_CHECK.read_text(), *HELPERS) + "\n"
        + block + '\nprintf "TOTALS %s/%s\\n" "$PASS" "$TOTAL"\n')
    proc = subprocess.run(
        ["bash", str(runner)], capture_output=True, text=True, timeout=120,
        env={**os.environ, **(env or {}), "BOOTDIR": str(bootdir),
             "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    return proc, bootdir


HEALTHY_BOOT1 = {"UC_TS": "Sat 2026-08-15 10:00:00 UTC", "UC_COND": "yes",
                 "UC_RESULT": "success", "UC_ACTIVE": "inactive",
                 "UC_ENABLED": "enabled", "UC_STDIN": "null", "JOBS": "",
                 "NETWAIT_ENABLED": "masked", "PANEL_ACTIVE": "active",
                 "TTY1_ACTIVE": "inactive"}
HEALTHY_BOOT2 = {**HEALTHY_BOOT1, "UC_COND": "no", "UC_RESULT": ""}


def results(proc) -> dict:
    """The check lines the block printed, name -> PASS/FAIL."""
    return {name: state for name, state in
            re.findall(r"^OTP-CHECK \S+ (\S+) (PASS|FAIL)", proc.stdout, re.M)}


def test_a_seeded_first_boot_reports_applied_credentials_and_no_wizard(tmp_path):
    # The positive control for everything below: every other test here
    # asserts a FAIL, and a block that failed unconditionally would satisfy
    # all of them.
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "network-wait-cannot-hold-the-boot-open": "PASS",
        "userconf-seed-applied": "PASS",
        "userconf-seeded-boot-ran-no-wizard": "PASS",
        "front-panel-survives-the-credential-apply": "PASS"}, proc.stdout


def test_a_boot_still_waiting_on_the_network_fails(tmp_path):
    """
    Run 31968966879's shape, stated as a check.

    The wizard's job was queued behind network-online.target, which was
    queued behind a wait with no timeout, and from the unit's own fields
    that is indistinguishable from a skip. The queue is asked directly, in
    both boots, because the boot that suffers it is any boot.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "JOBS": "1 systemd-networkd-wait-online.service start running\n"})
    assert results(proc)["network-wait-cannot-hold-the-boot-open"] == "FAIL", \
        proc.stdout


def test_an_unmasked_network_wait_fails_even_with_an_empty_queue(tmp_path):
    """
    Both halves, because either alone is satisfied by the broken image.

    An empty queue at the moment the probe looks says nothing about the next
    boot: whatever pulls network-online.target in decides that, and on the
    image run 31968966879 booted it was cloud-init. The mask is what makes
    the answer the same every time, so it is read back off the machine.
    """
    for state in ("enabled", "disabled", "static"):
        proc, _ = run_userconf(tmp_path / state, phase="boot2",
                               env={**HEALTHY_BOOT2, "NETWAIT_ENABLED": state})
        assert results(proc)["network-wait-cannot-hold-the-boot-open"] == "FAIL", \
            proc.stdout


def test_a_credential_apply_that_took_the_front_panel_down_fails(tmp_path):
    """
    The defect this check exists for, with the credentials applied perfectly.

    cancel-rename starts a getty on tty1 at the end of every successful
    apply. While otp-unit.service carried Conflicts=getty@tty1.service that
    start stopped the panel, so a unit whose operator used the documented
    credential file printed nothing until it was power-cycled -- with every
    other check in this phase green, because the seed WAS applied.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "PANEL_ACTIVE": "inactive"})
    assert results(proc)["userconf-seed-applied"] == "PASS", proc.stdout
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout


def test_a_login_prompt_sharing_the_panels_tty_fails(tmp_path):
    """
    The other outcome of the same start, and the reason the check does not
    stop at "otp-unit is active". Without the Conflicts= that used to stop
    it, a getty that DOES start shares /dev/tty1 with a unit holding it
    through StandardInput=tty-force: output interleaves and keystrokes go to
    whichever grabbed them. A getty merely queued is the same thing a moment
    later, so the queue counts too.
    """
    proc, _ = run_userconf(tmp_path / "running", phase="boot1",
                           env={**HEALTHY_BOOT1, "TTY1_ACTIVE": "active"})
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout
    proc, _ = run_userconf(tmp_path / "queued", phase="boot1", env={
        **HEALTHY_BOOT1, "JOBS": "7 getty@tty1.service start waiting\n"})
    assert results(proc)["front-panel-survives-the-credential-apply"] == "FAIL", \
        proc.stdout


def test_a_wizard_still_holding_a_job_fails_the_first_boot(tmp_path):
    """
    Run 12's shape, stated as a check, with everything else looking healthy.

    A queued job for a unit that has not started yet reads as `inactive` in
    every other field -- and a job is the thing that actually held run 12's
    boot open, with multi-user.target waiting on a whiptail nobody could
    see. So the queue is asked directly rather than inferred from is-active.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "JOBS": "42 userconfig.service start waiting\n"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_parked_mid_start_fails_the_first_boot(tmp_path):
    # The other half of the same shape: the prompt is up, the unit has been
    # "activating" for as long as the boot has been waiting.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_ACTIVE": "activating", "UC_RESULT": "",
        "JOBS": "42 userconfig.service start running\n"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_seeded_boot_whose_wizard_was_skipped_fails(tmp_path):
    # The condition came out false with a seed sitting on the card: the
    # drop-in's paths and the file the operator wrote disagree, or the unit
    # was masked. The credentials are silently ignored either way, and the
    # log says so in systemd's words rather than the unit's fields.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_COND": "no",
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service"
                      " - User configuration dialog was skipped because of an"
                      " unmet condition check (ConditionPathExists=|/boot/"
                      "firmware/userconf)."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_seeded_boot_whose_wizard_never_ran_at_all_fails(tmp_path):
    """
    Nothing in the log, nothing in the queue, nothing wrong with the unit --
    which is what a masked or never-pulled-in wizard looks like from every
    other field, and what run 31968966879's boots looked like once their
    queued job is taken out of the picture. The operator's credentials are
    ignored in silence.

    An empty journal is the one thing a garbage-collected unit and a
    never-started one do NOT share: the collected one leaves its "Finished"
    line behind, which is the whole reason this check reads the log.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1",
                           env={**HEALTHY_BOOT1, "UC_JOURNAL": ""})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_that_failed_on_the_seed_fails_the_first_boot(tmp_path):
    # A malformed seed met at BOOT: userconf-service dies on the whiptail it
    # cannot draw, and Restart=on-failure prints the two strings img-boot.sh
    # fails a release on. The log carries both, so the check does not have to
    # ask a unit that may already have been collected.
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_RESULT": "exit-code", "UC_ACTIVE": "failed",
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service:"
                      " Failed with result 'exit-code'. Aug 16 22:05:02 otp-unit"
                      " systemd[1]: userconfig.service: Scheduled restart job,"
                      " restart counter is at 1."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_wizard_that_only_finished_after_a_restart_fails(tmp_path):
    """
    "Finished" on its own is not the whole answer, because stock
    userconfig.service carries Restart=on-failure: a boot that met a bad seed
    can fail, be restarted, and finish. That boot printed "Failed with result"
    and "Scheduled restart job" -- two of the strings img-boot.sh hard-fails a
    release on -- so the guest must not call it clean either.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1,
        "UC_JOURNAL": "Aug 16 22:05:01 otp-unit systemd[1]: userconfig.service:"
                      " Failed with result 'exit-code'. Aug 16 22:05:31 otp-unit"
                      " systemd[1]: userconfig.service: Scheduled restart job,"
                      " restart counter is at 1. Aug 16 22:06:11 otp-unit"
                      " systemd[1]: Finished userconfig.service - User"
                      " configuration dialog."})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "FAIL", proc.stdout


def test_a_finished_line_from_a_collected_unit_is_still_a_pass(tmp_path):
    """
    The measurement this check was rewritten around, stated as a test.

    A seeded apply ends in cancel-rename, which runs `systemctl disable
    userconfig` and a daemon-reload; the oneshot is inactive and unreferenced
    by then, so systemd garbage-collects it and every property a later
    `systemctl show` returns is a pristine default. Run 31972140190 measured
    exactly that -- `condition=no result=success is-active=inactive jobs=0`
    on a boot whose console says `Finished userconfig.service`, whose shadow
    entry carries the seeded salt and whose card came back with the seed
    consumed. Failing that boot would be the harness calling a correct
    machine broken.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env={
        **HEALTHY_BOOT1, "UC_TS": "", "UC_COND": "no", "UC_RESULT": "success"})
    assert results(proc)["userconf-seeded-boot-ran-no-wizard"] == "PASS", proc.stdout


def test_a_seed_deleted_without_being_applied_fails(tmp_path):
    """
    The gap the shadow check exists to close.

    From the host the file is gone, which is exactly what success looks like.
    A userconf-service that unlinked the seed without running chpasswd would
    leave the operator with an account whose password is pi-gen's random
    FIRST_USER_PASS -- a value nobody has -- and a green tier 3.
    """
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           shadow="otp:$6$rEaLsAlT$otherbytes:20000:0:99999:7:::\n")
    assert results(proc)["userconf-seed-applied"] == "FAIL", proc.stdout


def test_no_shadow_entry_at_all_fails(tmp_path):
    # The account renamed out from under the seed, or never created.
    proc, _ = run_userconf(tmp_path, phase="boot1", env=HEALTHY_BOOT1,
                           shadow="pi:$6$otpimgcheck$hashbytes:20000::::::\n")
    assert results(proc)["userconf-seed-applied"] == "FAIL", proc.stdout


def test_an_unseeded_second_boot_is_quiet_and_leaves_evidence(tmp_path):
    # The positive control for the boot2 half, and the healthy path: skipped
    # but enabled, no prompt possible, and the malformed seed quarantined.
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2)
    assert proc.returncode == 0, proc.stderr
    assert results(proc) == {
        "network-wait-cannot-hold-the-boot-open": "PASS",
        "userconf-unseeded-boot-skips-the-wizard": "PASS",
        "userconf-wizard-cannot-prompt": "PASS",
        "userconf-malformed-seed-fails-fast": "PASS"}, proc.stdout
    assert (bootdir / "failed_userconf.txt").exists()
    assert not (bootdir / "userconf.txt").exists()


def test_a_masked_wizard_is_quiet_for_the_wrong_reason(tmp_path):
    """
    Quiet is not the property. Masking userconfig.service also stops the
    prompt -- an earlier fix did exactly that -- and it silently discards
    every credential an operator ever writes to userconf.txt, because that
    unit is the file's only consumer. With no SSH and no network, the tty2
    recovery getty is then a login nobody can pass.
    """
    for state in ("masked", "disabled"):
        proc, _ = run_userconf(tmp_path / state, phase="boot2",
                               env={**HEALTHY_BOOT2, "UC_ENABLED": state})
        assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", \
            proc.stdout


def test_a_condition_systemd_never_evaluated_is_not_a_skip(tmp_path):
    # An empty ConditionTimestamp is "systemd has not looked at this unit",
    # which is what a probe that ran too early sees -- and it is
    # indistinguishable from a skip in every other field.
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_TS": ""})
    assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", proc.stdout


def test_a_second_boot_whose_condition_is_still_true_fails(tmp_path):
    """
    The property this check is NAMED for, which nothing was testing.

    Every other boot2 fixture leaves the condition false, so the
    `condition = no` clause could be DELETED and the whole suite stayed
    green -- the check went on being read as "the wizard was skipped" while
    only ever measuring "the unit is enabled, evaluated, and not running".
    Those do not imply it: a unit whose condition came out TRUE is one
    systemd let start, and on a oneshot that has already finished it is
    `inactive` with an evaluation timestamp and `enabled`, exactly like a
    skip.

    On the device a true condition here means the seed OUTLIVED the boot
    that was supposed to consume it -- the delete failed, or the operator
    wrote the file again -- so the wizard is armed on this boot and on every
    boot after it, and a credential line is still sitting on a FAT partition
    anybody can read in any card reader.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_COND": "yes"})
    assert results(proc)["userconf-unseeded-boot-skips-the-wizard"] == "FAIL", proc.stdout
    # And the reader is told which half went wrong.
    assert "condition=yes" in proc.stdout, proc.stdout


def test_a_wizard_that_can_still_reach_a_tty_fails(tmp_path):
    # The drop-in gone, or overridden by another one: the stock unit's
    # StandardInput=tty is back and a malformed seed is a hang again.
    proc, _ = run_userconf(tmp_path, phase="boot2",
                           env={**HEALTHY_BOOT2, "UC_STDIN": "tty"})
    assert results(proc)["userconf-wizard-cannot-prompt"] == "FAIL", proc.stdout


def hang_stub(*, ignore_term: bool) -> str:
    """A userconf-service that quarantines the seed and then never returns.

    The quarantine comes FIRST, which is the real order: upstream renames the
    bad seed and only then reaches the whiptail. Every other clause of the
    check therefore passes, so what these fixtures measure is the stopwatch
    and nothing else.

    `exec >/dev/null` before the block, so that the `sleep` this process
    leaves behind when it is killed is not still holding the pipe the probe's
    command substitution is reading -- without it the ORPHAN sets the test's
    runtime, not the timeout, and the experiment measures the harness.
    """
    return ('#!/bin/sh\n'
            + ('trap "" TERM\n' if ignore_term else '')
            + 'cat "$BOOTDIR/userconf.txt" >> "$BOOTDIR/failed_userconf.txt"\n'
              'rm -f "$BOOTDIR/userconf.txt"\n'
              'exec >/dev/null 2>&1\n'
              'sleep 30\n')


def test_a_malformed_seed_that_hangs_fails_instead_of_timing_the_boot_out(tmp_path):
    """
    The failure this experiment exists for. A userconf-service that blocks --
    a whiptail that found a terminal after all, a read on a stdin that is not
    really closed -- holds multi-user.target open on a device forever. Here it
    is bounded and reported: the probe kills it and says rc=124.
    """
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                                 service=hang_stub(ignore_term=False))
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "rc=124" in proc.stdout, proc.stdout
    # And the probe carried on to report, rather than being the hang itself.
    assert "TOTALS" in proc.stdout, proc.stdout


def test_a_hang_that_ignores_the_term_signal_also_fails(tmp_path):
    """
    The hang the check was written for, and the one it used to PASS.

    124 is what `timeout` answers when the child took its SIGTERM and died.
    A child that IGNORES SIGTERM -- a whiptail with a handler, a shell with
    `trap "" TERM`, anything wedged in uninterruptible state -- survives it
    and has to be SIGKILLed by the `-k` escalation, and then timeout answers
    128+9 = 137 instead. Against the `!= 124` form of the gate that scored

        userconf-malformed-seed-fails-fast PASS rc=137

    on a service that never returned, with the detail line printing the
    "124 = still running" gloss beside a 137. The test above passed only
    because a bare `sleep` is reaped by the first signal.

    So the gate reads `-lt 124` now, and this is the fixture that tells the
    two apart. Both fixtures are kept: this one alone would be satisfied by a
    gate that only knew about 137.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service=hang_stub(ignore_term=True))
    assert "rc=137" in proc.stdout, (
        "the fixture did not reach the -k escalation, so this test is not "
        "measuring what it says: " + proc.stdout)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "TOTALS" in proc.stdout, proc.stdout


def test_the_detail_line_does_not_gloss_a_kill_as_a_timeout(tmp_path):
    """
    What the operator reads has to survive the same correction the gate did.
    The old line said "(124 = still running at the 60s bound)" unconditionally
    -- printed next to rc=137 it names the wrong number and the wrong signal,
    and the next person debugging a red run is told the child was politely
    asked when it was actually killed.
    """
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service=hang_stub(ignore_term=True))
    detail = next(ln for ln in proc.stdout.splitlines()
                  if "userconf-malformed-seed-fails-fast" in ln)
    assert "137" in detail, detail
    assert "124 = still running" not in detail, detail


def test_the_experiment_supplies_the_conditions_the_unit_would(tmp_path):
    """
    Stdin closed and TERM removed, which is what the drop-in plus a unit
    with no tty add up to. Run it with the probe's own stdin and whiptail
    may find a terminal after all -- and then the experiment measures a
    machine nobody ships instead of the appliance.
    """
    block = userconf_block()
    line = next(ln for ln in block.splitlines() if "USERCONF_SERVICE" in ln
                and "timeout" in ln)
    assert "< /dev/null" in line, line
    assert "env -u TERM" in line, line


def test_a_malformed_seed_that_leaves_no_evidence_fails(tmp_path):
    # It ended, and it took the operator's file with it. failed_userconf.txt
    # is the only thing that tells them why they have no login.
    proc, _ = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                           service='#!/bin/sh\nrm -f "$BOOTDIR/userconf.txt"\nexit 1\n')
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_a_seed_left_in_place_by_the_service_fails(tmp_path):
    # Quarantine means moved: a card that still holds the bad line prompts on
    # every boot from here on.
    proc, _ = run_userconf(
        tmp_path, phase="boot2", env=HEALTHY_BOOT2,
        service='#!/bin/sh\ncp "$BOOTDIR/userconf.txt" "$BOOTDIR/failed_userconf.txt"\nexit 1\n')
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_an_image_without_userconf_pi_fails_rather_than_passing_silently(tmp_path):
    """
    An absent userconf-service is not a pass.

    Nothing would have been written, nothing would have failed, and both
    "userconf.txt is gone" and "the boot did not hang" would be true -- of an
    image on which the documented credential mechanism does not exist at all.
    """
    proc, bootdir = run_userconf(tmp_path, phase="boot2", env=HEALTHY_BOOT2,
                                 service=None)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout
    assert "rc=none" in proc.stdout, proc.stdout
    # And nothing was planted on a card whose machine cannot consume it.
    assert not (bootdir / "userconf.txt").exists()

    # The other way it goes missing: present, and not runnable.
    proc, _ = run_userconf(tmp_path / "not-executable", phase="boot2",
                           env=HEALTHY_BOOT2, executable=False)
    assert results(proc)["userconf-malformed-seed-fails-fast"] == "FAIL", proc.stdout


def test_the_probe_is_given_longer_than_its_own_bounded_wait(tmp_path):
    """
    systemd's default TimeoutStartSec is 90s and the probe's own poll for
    otp-unit.service is 45 iterations of `sleep 2` -- 90s exactly, before
    cupsd -t and two Python starts under TCG. On the default, a slow but
    healthy boot loses its probe mid-poll and the phase reports nothing,
    which the harness fails for having no OTP-GUEST-DONE line. It fails red
    rather than green, so this is a flake risk and not a false pass, but the
    number is worth stating rather than inheriting.

    EVERY bounded wait, summed, not the first one. The probe grew a second
    poll and a `timeout` when the credential checks landed, and a test that
    kept reading only the unit poll would have gone on approving a 300s
    budget against 90s of a 180s worst case. Measured as this stands: 90s
    polling for otp-unit, 30s polling for systemd's verdict on the wizard,
    60s bounding the malformed-seed experiment.

    Both halves are read out of the shipped files: the bounds out of the
    probe, the backstop out of the workflow.
    """
    probe = GUEST_CHECK.read_text()
    # Each `for _ in $(seq 1 N) ... done` with the `sleep S` inside it.
    poll_bound = 0
    for loop in re.finditer(r"for _ in \$\(seq 1 (\d+)\); do(.*?)\n *done",
                            probe, re.S):
        interval = re.search(r"^\s*sleep (\d+)\s*$", loop.group(2), re.M)
        assert interval, f"a poll with no sleep in it: {loop.group(0)[:80]}"
        poll_bound += int(loop.group(1)) * int(interval.group(1))
    # Plus anything the probe runs under a stopwatch of its own.
    for bound in re.finditer(r"timeout -k \d+ (\d+)", probe):
        poll_bound += int(bound.group(1))
    assert poll_bound >= 90, "the probe's bounded waits went missing"

    unit = IMGCHECK_UNIT.read_text()
    match = re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M)
    assert match, "otp-unit-imgcheck.service has no explicit TimeoutStartSec"
    timeout = int(match.group(1))
    assert timeout > poll_bound, (
        f"TimeoutStartSec={timeout}s does not clear the probe's own "
        f"{poll_bound}s poll, so a slow boot loses its report")

    workflow = (REPO / ".github" / "workflows" / "image.yml").read_text()
    backstop = int(re.search(r"OTP_IMG_TIMEOUT:\s*(\d+)", workflow).group(1))
    assert timeout < backstop, (
        f"TimeoutStartSec={timeout}s is not inside the {backstop}s per-boot "
        f"backstop, so a wedged probe eats the run instead of being killed")


def test_the_probe_checks_the_sentinel_before_it_writes_one():
    """
    Ordering is the whole test in boot2: reading the sentinel after writing
    it would report a survival every time, on a card that discards
    perfectly. The read is at the top of the script for that reason.
    """
    text = GUEST_CHECK.read_text()
    read_at = text.index('if [ -e "$SENTINEL" ]; then')
    write_at = text.index("> \"$SENTINEL\" 2>/dev/null")
    assert read_at < write_at, (
        "the sentinel is written before it is read back, so "
        "root-writes-discarded-by-the-power-cycle can never fail")


def test_the_probe_states_two_presences_before_it_asserts_an_absence():
    """
    "The file is gone" is satisfied by a boot1 that never ran, by a path
    that does not exist and by a rig that cannot write to / at all. Both
    controls are reported before it, with the same fixture: a sentinel
    written this boot and read back, and the setting boot1 persisted read
    back through config.load().
    """
    text = GUEST_CHECK.read_text()
    absence = text.index("check root-writes-discarded-by-the-power-cycle")
    for control in ("check root-write-lands-and-is-readable",
                    "check settings-survive-the-power-cycle"):
        assert text.index(control) < absence, control

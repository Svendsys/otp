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
import subprocess
from pathlib import Path


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

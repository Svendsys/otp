"""What a tag is allowed to publish.

`image.yml` builds the image, uploads it as an artifact, boots it (tier 3),
and on a tag attaches it to the release. The ORDER of the last two is the
whole of this file: for a while the attachment happened first, so a tagged
image that could not boot shipped anyway, to be written to a card and
handed to somebody.

The ordering is invisible in review -- two steps moved a hundred lines
apart in a 350-line workflow -- and nothing in CI would notice it moving
back, because the only run that would demonstrate it is a tag run with a
broken image, which is the run nobody wants to have.

THE FIRST VERSION OF THIS FILE WAS A CHECK THAT COULD BARELY FAIL. It
matched the gate's condition by substring, so `success() && startsWith(..)`
becoming `success() || startsWith(..)` -- one character, and a change that
publishes a release tagged `master` on every green push -- left all seven
assertions green. Nine mutations survived it in total: a duplicate attach
step, a renamed one, `|| true` on the boot script, a deleted `files:`. The
assertions below are written against those nine.

WHAT THIS CANNOT DO is prove GitHub honours the gate. It asserts the
workflow says the right thing. That a step whose `if` names no status
function carries an implicit `success()`, and that an intervening
`always()` step does not clear the job's failure state, are the runner's
semantics -- confirmed against actions/runner's StepsRunner, which updates
job status only inside `if (step.Result == TaskResult.Failed)` -- and are
not reproducible here.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "image.yml"

BOOT = "Boot the image"
ATTACH = "Attach to the release"
UPLOAD = "Upload"

#: The gate, exactly. Pinned as a whole expression rather than probed for
#: substrings, because the structure is the part that matters: `&&` -> `||`
#: contains every token a substring check looks for and inverts the meaning.
GATE = "success() && startsWith(github.ref, 'refs/tags/')"

#: Anything in a `run:` that stops a failing command from failing the step.
SWALLOWS_FAILURE = ("|| true", "|| :", "set +e", "continue-on-error")


def workflow() -> dict:
    # `on:` parses as the boolean True under YAML 1.1, which is harmless
    # here but startling if you go looking for it.
    return yaml.safe_load(WORKFLOW.read_text())


def build_steps() -> list:
    """
    The steps of the job that builds and boots the image.

    Named `build`, not `image`. `image` is the job branch protection
    requires: it always runs, reports on every pull request, and does
    nothing but read the other two's results. Everything these tests are
    about -- the boot, the artifact, the release attachment -- lives in
    `build`, which is conditional. See test_the_required_check_always_runs.
    """
    return workflow()["jobs"]["build"]["steps"]


def index_of(steps, name) -> int:
    matches = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert matches, (
        f"no step named {name!r} in image.yml; the steps are: "
        + ", ".join(repr(s.get("name")) for s in steps))
    # Uniqueness is load-bearing. A SECOND step of the same name appended
    # at the end with `always()` would leave a first-match lookup happily
    # asserting the gated one while the ungated duplicate ships the image.
    assert len(matches) == 1, f"{name!r} appears {len(matches)} times"
    return matches[0]


def step_named(steps, name) -> dict:
    return steps[index_of(steps, name)]


# A step publishes a release if it USES a release action or if it SHELLS
# OUT to one. Matching only on `uses:` missed the second kind entirely: a
# `run: gh release upload "$TAG" image/deploy/*.img.xz` placed before the
# boot is a publisher by any measure and left all eight tests green.
PUBLISHING_ACTIONS = ("gh-release", "create-release", "upload-release")
PUBLISHING_COMMANDS = ("gh release create", "gh release upload",
                       "hub release", "/releases/assets", "/releases }}",
                       "softprops/action-gh-release")


def release_publishers(paths=None) -> list:
    """
    Every step in EVERY workflow file that can create a release or attach
    an asset to one, by what it uses or runs rather than what it is called.

    Name-based lookup cannot see a second publisher under another name --
    `Publish the release asset`, placed before the boot, was invisible to
    the first version of this file and shipped the image ungated. Scanning
    only image.yml has the same hole one level up: ci.yml runs on every
    push to master and has no boot gate at all, so a publisher added there
    would never meet one.
    """
    found = []
    for path in sorted(paths if paths is not None else WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for job_name, job in (document.get("jobs") or {}).items():
            for position, step in enumerate(job.get("steps", [])):
                uses = str(step.get("uses", ""))
                runs = str(step.get("run", ""))
                if (any(a in uses for a in PUBLISHING_ACTIONS)
                        or any(c in runs for c in PUBLISHING_COMMANDS)):
                    found.append((f"{path.name}:{job_name}", position, step))
    return found


# --- the ordering ---------------------------------------------------------


def test_the_release_asset_is_attached_after_the_boot():
    """The point of the file."""
    steps = build_steps()
    assert index_of(steps, ATTACH) > index_of(steps, BOOT), (
        "the release attachment runs before the boot gate, so a tag whose "
        "image cannot boot would ship it anyway")


def test_there_is_exactly_one_thing_that_can_publish_a_release():
    publishers = release_publishers()
    assert len(publishers) == 1, (
        "more than one step can create a release; every one of them is a "
        f"way past the boot gate: {[(j, s.get('name')) for j, _, s in publishers]}")
    job, _, step = publishers[0]
    assert job == "image.yml:build", (
        f"the publisher moved to {job!r}. That may be fine, but the ordering "
        f"assertions here only see image.yml's `build` job -- update them "
        f"deliberately rather than letting this pass. A publisher in ci.yml "
        f"in particular meets no boot gate at all, and one in image.yml's "
        f"`image` job would meet none either: that job is the always-runs "
        f"reporter and does not build anything.")
    assert step.get("name") == ATTACH, step.get("name")


def test_the_plain_artifact_is_still_uploaded_before_the_boot():
    """
    The other half, and it must NOT move. Debugging a red boot needs the
    image that failed, so the artifact is uploaded unconditionally and
    early. Only the release asset is gated.
    """
    steps = build_steps()
    assert index_of(steps, UPLOAD) < index_of(steps, BOOT)
    upload = step_named(steps, UPLOAD)
    assert "if" not in upload, \
        "the artifact upload became conditional; a red boot now loses its evidence"
    assert upload["with"]["if-no-files-found"] == "error", \
        "an upload that finds no image must fail rather than shrug"


# --- the gate itself ------------------------------------------------------


def test_the_gate_is_exactly_the_expression_it_should_be():
    """
    Pinned whole. Probing for `success()` and `refs/tags/` separately is
    what let `&&` -> `||` through: that mutation keeps both tokens, drops
    the gate entirely, and publishes a release named after whatever branch
    was pushed. `!startsWith(...)` inverts it and keeps them too.
    """
    condition = " ".join(str(step_named(build_steps(), ATTACH)["if"]).split())
    assert condition == GATE, (
        f"the release gate now reads {condition!r}, not {GATE!r}. If the "
        f"change is deliberate, work out what it does on a green push to "
        f"master before updating this line.")


def test_the_boot_step_can_still_fail_the_job():
    """
    The gate is worth nothing if the thing it gates on cannot go red, and
    `continue-on-error` is not the cheapest way to break that -- appending
    `|| true` to the script invocation is one token and leaves every
    structural property of the step intact.
    """
    boot = step_named(build_steps(), BOOT)
    assert not boot.get("continue-on-error"), boot
    assert "if" not in boot, \
        "the boot step became conditional; it may no longer gate anything"
    script = boot["run"]
    assert "./harness/img-boot.sh" in script, \
        "the boot step no longer runs the boot harness"
    for swallow in SWALLOWS_FAILURE:
        assert swallow not in script, (
            f"the boot step's run block contains {swallow!r}, so a failed "
            f"boot would report success and the release would ship")


# --- what actually gets attached -----------------------------------------


def test_the_asset_is_the_image():
    """
    `fail_on_unmatched_files` defaults to false, so a tag run with no
    `files:` at all publishes a release carrying the caveat note and no
    image, entirely green.
    """
    attach = step_named(build_steps(), ATTACH)
    assert "softprops/action-gh-release@" in attach["uses"], attach["uses"]
    assert attach["with"]["files"] == "image/deploy/*.img.xz", attach["with"]


def test_the_note_is_appended_so_it_cannot_eat_the_release_notes():
    """
    The action's update path is `body = workflowBody || existingReleaseBody`
    unless append_body is set. Publishing a release through the GitHub UI
    creates the release AND the tag, and the tag push is what starts this
    workflow -- so the release already exists when the step runs, making
    the update path the normal case rather than the exotic one.

    Without append_body the caveat paragraph therefore REPLACES whatever
    changelog the maintainer wrote. An earlier version of this test pinned
    append_body out, on the reasoning that only the create path mattered,
    and so held the defect in place.
    """
    attach = step_named(build_steps(), ATTACH)["with"]
    assert attach.get("append_body") is True, \
        ("without append_body the note REPLACES the maintainer's release "
         "notes on the UI-publish path; see the comment in image.yml")
    body = attach["body"]
    assert "harness/img-boot.sh" in body, \
        "the note must point at the file that states the gate's limits"
    assert "otp-unit.service" in body
    # Folded, not literal: release bodies render hard line breaks, and a
    # block scalar would ship this at whatever width the YAML is wrapped to.
    assert "\n" not in body.strip(), \
        "the note carries hard line breaks and will render ragged"


def test_the_release_notes_two_boot_claim_is_backed_by_the_harness():
    """
    The note tells whoever downloads a tagged image that it was booted
    twice, that a file written to `/` in the first boot was gone in the
    second, and that a setting written to `/boot/firmware` was not. Nothing
    in this workflow makes any of that true: `OTP_IMG_PHASES` decides which
    boots happen, and the harness is what reads it.

    So the claim rests on two things, and both are asserted here. The
    harness must refuse a phase list without boot2 -- a one-boot run used to
    pass the whole gate and still print the two-boot conclusion -- and this
    workflow must not set the variable to something that drops a boot. A
    run halved to save CI minutes has to go red rather than ship a release
    body that says what did not happen.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "twice" in body, body
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    assert "leaves out:$PHASES_MISSING" in harness, \
        ("img-boot.sh no longer refuses a phase list that drops a boot, so "
         "the release note's two-boot claim is no longer backed by anything")
    phases = (step_named(build_steps(), BOOT).get("env") or {}).get("OTP_IMG_PHASES")
    if phases is not None:
        for boot in ("boot1", "boot2"):
            assert boot in str(phases).split(), \
                (f"the boot step pins OTP_IMG_PHASES={phases!r}, which does "
                 f"not run {boot}, but the release note claims both")


def test_the_release_notes_credential_claim_is_backed_by_the_harness():
    """
    The note tells whoever downloads a tagged image that a seeded
    userconf.txt was applied and consumed, that the second boot stayed quiet
    without the unit being disabled, and that a malformed seed failed fast.
    None of that is made true by this workflow: the harness demands those
    checks by name, and if it stops demanding them the sentence is a
    statement about nothing.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "userconf" in body, body
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    for name in ("userconf-seed-applied", "userconf-seeded-boot-ran-no-wizard",
                 "userconf-unseeded-boot-skips-the-wizard",
                 "userconf-malformed-seed-fails-fast",
                 # And the three the note's STRONGEST clause rests on. It no
                 # longer says the password lasts one boot; it says it is
                 # still in force after the power cycle, which is a claim
                 # about a machine rather than about a mechanism. Drop any of
                 # these from the gate and the sentence attached to a tag is
                 # a statement about nothing -- the worse direction, because
                 # the old wording at least understated.
                 "credential-recorded-outside-the-overlay",
                 "credential-recorded-for-the-next-boot",
                 "credential-survives-the-power-cycle",
                 # And the one the note's USERNAME sentence rests on. A note
                 # that promises the credential without saying the rename
                 # does not survive sends an operator to a tty2 prompt to
                 # type a name this machine does not have -- with no network
                 # and no sshd behind it, which is the whole recovery path.
                 "credential-recovers-a-store-naming-another-account"):
        assert name in harness, (
            f"the release note claims the credential path was checked, but "
            f"img-boot.sh no longer requires {name}")
    # THE CAVEAT ITSELF, in the body. The check above says the harness proves
    # it; this says the reader is told. `userconf-pi` renames the UID-1000
    # account to whatever a seed names and the rename dies with the overlay,
    # so the account is `otp` on every boot after the one that applied it.
    assert "USERNAME does not survive" in body, (
        "the release note promises a persistent login without saying the "
        "username reverts: an operator would type a name that is not there")
    assert "Log in as `otp`" in body, body
    # AND IT SAYS WHAT KEEPING IT COSTS. A note that tells a reader their
    # password now persists, without telling them it persists as an
    # offline-crackable hash on a partition any card reader can mount, has
    # told them the half that sounds like good news. This is the one place
    # this project's claims reach someone who cannot check them.
    for warning in ("world-readable", "offline"):
        assert warning in body, (
            f"the release note announces a persistent login without the word "
            f"{warning!r}: the exposure is part of the claim, not a footnote")
    # And the harness has to plant one, or the seeded branch is untested and
    # every check above it passes on an empty boot partition. The COPY, not
    # the name: the log line next to it says ::userconf.txt as well.
    assert re.search(r"^mcopy[^\n]*::userconf\.txt", harness, re.M), \
        "nothing seeds a userconf.txt, so the note's first clause is empty"


def test_the_release_notes_boot_completion_claim_is_backed_by_the_harness():
    """
    Two sentences that only exist because run 31968966879 disproved them.

    Neither boot in that run finished: an unbounded
    systemd-networkd-wait-online held network-online.target, which held
    cloud-init's config stage, which held the credential wizard -- and the
    apply the wizard performs ends by starting a getty on the front panel's
    tty. Both are checks on the booted image now, and a note that says so
    has to be backed by a harness that still demands them by name.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "front panel" in body, body
    assert "network" in body, body
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    for name in ("front-panel-survives-the-credential-apply",
                 "network-wait-cannot-hold-the-boot-open"):
        assert name in harness, (
            f"the release note claims the boot finished with its panel "
            f"intact, but img-boot.sh no longer requires {name}")


def test_the_release_notes_identity_claim_is_backed_by_the_harness():
    """
    The newest sentence in the note, and the one most easily left standing
    after the thing behind it stops being checked.

    It says the second boot came up as the same machine as the first. Three
    named guest checks make that true -- the machine-id in use matches the
    copy kept OUTSIDE the overlay, boot1 really recorded that id where boot2
    could read it, and boot2's id is identical to it -- and without all three
    the sentence is a claim about nothing on a device that prints key
    material. The fourth name is the host-side half, which owes nothing to a
    check running inside the guest: systemd itself said the second boot was
    not a first boot.

    AND IT MAY NOT CLAIM A STABLE SSH FINGERPRINT. It used to, because this
    unit used to copy its host keys onto the FAT partition. `ENABLE_SSH=0`
    means the image ships with `ssh.service` disabled and never runs sshd, so
    that was a fingerprint nobody could be shown, bought with private keys on
    a partition every local account can read. A release note is the one place
    this project's claims reach someone who cannot check them, and a
    withdrawn feature it still advertises is the worst version of that.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "machine-id" in body, body
    # The exposure is part of the claim, not a footnote: the boot partition
    # is vfat mounted with `defaults`, so everything on it is 0755 root:root,
    # and the note is the only place a person who flashes this image is told.
    assert "otp-identity" in body, body
    assert "world-readable" in body, body
    assert "fingerprint" not in body.lower(), (
        "the release note still advertises a stable SSH host key "
        "fingerprint; the image disables sshd and keeps no host key outside "
        "the overlay: " + body)
    assert "copy of the host keys" not in body.lower(), body
    # And it says so rather than going quiet, because "the identity is kept
    # on the FAT partition" invites exactly the wrong guess about what else
    # is up there.
    assert "no SSH host key is written outside the overlay" in body, body
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    for name in ("machine-id-persisted-outside-the-overlay",
                 "machine-id-recorded-for-the-next-boot",
                 "machine-id-identical-across-the-power-cycle",
                 "second-boot-is-not-a-first-boot"):
        assert name in harness, (
            f"the release note claims this unit keeps its identity across a "
            f"power cycle, but img-boot.sh no longer requires {name}")


# --- what counts as a change to the image --------------------------------
#
# The probe runs INSIDE the image. install.sh installs
# harness/img-guest-check.sh into /opt/otp-unit and enables a unit that runs
# it on every tier-3 boot, so editing a check edits the artifact -- and both
# of the mechanisms that decide whether to rebuild it were written before
# that was true.

PROBE = "harness/img-guest-check.sh"


def test_the_probe_is_really_part_of_the_image():
    # The premise of the two tests below, stated rather than assumed: if
    # install.sh stops installing it, they are guarding nothing.
    assert PROBE in (REPO / "device" / "install.sh").read_text()


def test_a_change_to_the_probe_runs_the_image_job():
    """
    The filter decides whether the expensive job runs at all. The probe was
    not in it, so a pull request that changed only a guest check ran five
    fast jobs, none of which can execute that file, and reported green.

    The shipped regex is applied to real paths here rather than searched for
    as a substring: `harness/img-boot\\.sh$` contains the word harness and
    matches none of this.
    """
    filter_run = None
    for step in workflow()["jobs"]["changes"]["steps"]:
        if "grep -qE" in str(step.get("run", "")):
            filter_run = str(step["run"])
    assert filter_run, "the changes job no longer filters with grep -qE"
    pattern = re.search(r"grep -qE \\\s*\n\s*'([^']+)'", filter_run)
    assert pattern, filter_run
    matcher = re.compile(pattern.group(1))
    for path in (PROBE, "harness/img-boot.sh", "device/install.sh",
                 "image/build.sh", ".github/workflows/image.yml"):
        assert matcher.search(path), f"{path} does not trigger the image job"
    # And the filter still filters: a docs-only change must not pay for a
    # pi-gen build.
    for path in ("README.md", "tests/test_img_verdict.py", "harness/README.md"):
        assert not matcher.search(path), f"{path} needlessly triggers a build"


def test_the_cache_key_covers_the_probe_the_image_ships():
    """
    A key that ignores the probe restores an image built before the edit and
    boots yesterday's guest checks. That does not fail honestly: the harness
    demands each named check by name, so the run goes red for checks the
    guest never emitted -- which reads as a broken image rather than a stale
    cache, and the first place anyone looks is the boot.
    """
    for step in build_steps():
        if str(step.get("uses", "")).startswith("actions/cache/restore"):
            key = str(step["with"]["key"])
            break
    else:
        raise AssertionError("no cache restore step in the build job")
    assert PROBE in key, key
    # The things already in it stay in it: an image built before otpunit
    # changed is the wrong image to boot even though pi-gen would not care.
    for pattern in ("image/**", "device/**", "otpunit/**", "codewords/**"):
        assert pattern in key, key


def test_the_gate_never_fires_on_a_branch_or_a_pull_request():
    """
    `tag_name` defaults to `github.ref_name`, so a gate that let a branch
    through would create a release literally called `master`.
    """
    condition = str(step_named(build_steps(), ATTACH)["if"])
    assert re.search(r"startsWith\(\s*github\.ref\s*,\s*'refs/tags/'\s*\)",
                     condition), condition
    assert "!" not in condition, f"the tag test is negated: {condition!r}"


# --- the required check, which exists to always report -------------------
#
# Branch protection requires a check called `image`. It used to be the
# build job itself, path-filtered at the trigger, so a pull request that
# touched none of those paths never ran it -- and a required check that
# never reports does not pass by default, it blocks forever. Five PRs sat
# behind that. These hold the replacement to the shape that fixes it.


def test_the_required_check_is_not_filtered_out_of_existing():
    on = workflow()[True]
    assert "paths" not in (on["pull_request"] or {}), (
        "image.yml filters its pull_request trigger again. Whatever the "
        "filter says, the effect is that PRs outside it never run this "
        "workflow, the required `image` check never reports, and they "
        "cannot be merged at all. Filter the WORK (see the changes job), "
        "never the report.")


def test_the_required_check_always_runs():
    gate = workflow()["jobs"]["image"]
    assert gate["if"] == "always()", (
        f"the `image` job's condition is {gate.get('if')!r}. Without "
        f"always() a skipped `build` skips this too, and a skipped "
        f"required check never reports -- which is the original bug.")
    assert set(gate["needs"]) == {"changes", "build"}, gate["needs"]
    assert "steps" in gate and len(gate["steps"]) == 1


def test_the_expensive_job_is_the_conditional_one():
    build = workflow()["jobs"]["build"]
    assert build["needs"] == "changes"
    assert build["if"] == "needs.changes.outputs.image == 'true'"
    # The cost is why any of this exists: a docs-only PR must not pay
    # for a pi-gen build. If this job stops being conditional the gate
    # still works, but every PR in the repository gets slower.
    assert build["runs-on"] == "ubuntu-24.04-arm"


def test_the_gate_cannot_pass_by_falling_off_the_end():
    """
    Every branch of the verdict either exits non-zero or says why not.

    A gate whose default is success is not a gate. `set -e` plus an
    explicit exit on each path is what keeps an unforeseen combination
    -- a new job result string, say -- from reading as approval.
    """
    run = workflow()["jobs"]["image"]["steps"][0]["run"]
    assert "set -euo pipefail" in run
    assert run.count("exit 1") >= 3, (
        "the verdict has fewer failure exits than it has ways to fail")
    for swallow in SWALLOWS_FAILURE:
        assert swallow not in run, (
            f"the verdict swallows failure with {swallow!r}")

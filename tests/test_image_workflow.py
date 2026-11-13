"""What a tag is allowed to publish.

`image.yml` builds the image, uploads it as an artifact, boots it (tier 3)
and, on a tag, attaches it to the release. The attachment must come after
the boot, or a tagged image that cannot boot ships anyway. These tests
assert what the workflow says; that GitHub honours the gate (an `if` naming
no status function implies `success()`, and an intervening `always()` step
does not clear the job's failure) is the runner's semantics and is not
reproducible here.
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "image.yml"

BOOT = "Boot the image"
ATTACH = "Attach to the release"
UPLOAD = "Upload"

#: The gate, pinned as a whole expression: `&&` -> `||` keeps every token a
#: substring check looks for and inverts the meaning.
GATE = "success() && startsWith(github.ref, 'refs/tags/')"

#: Anything in a `run:` that stops a failing command from failing the step.
SWALLOWS_FAILURE = ("|| true", "|| :", "set +e", "continue-on-error")


def workflow() -> dict:
    # `on:` parses as the boolean True under YAML 1.1.
    return yaml.safe_load(WORKFLOW.read_text())


def build_steps() -> list:
    """The steps of the job that builds and boots the image.

    That job is `build`, which is conditional. `image` is the
    always-running job branch protection requires; it only reads the
    others' results.
    """
    return workflow()["jobs"]["build"]["steps"]


def index_of(steps, name) -> int:
    matches = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert matches, (
        f"no step named {name!r} in image.yml; the steps are: "
        + ", ".join(repr(s.get("name")) for s in steps))
    # Uniqueness matters: a first-match lookup would miss an ungated
    # duplicate step of the same name.
    assert len(matches) == 1, f"{name!r} appears {len(matches)} times"
    return matches[0]


def step_named(steps, name) -> dict:
    return steps[index_of(steps, name)]


# A step publishes a release if it uses a release action or shells out to
# one.
PUBLISHING_ACTIONS = ("gh-release", "create-release", "upload-release")
PUBLISHING_COMMANDS = ("gh release create", "gh release upload",
                       "hub release", "/releases/assets", "/releases }}",
                       "softprops/action-gh-release")


def release_publishers(paths=None) -> list:
    """Every step in every workflow file that can create a release or attach
    an asset to one, found by what it uses or runs, not by its name. Other
    workflows are scanned too because they have no boot gate at all."""
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
    """The release asset is attached only after the boot has passed."""
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
    """The plain artifact is uploaded unconditionally and before the boot:
    debugging a red boot needs the image that failed."""
    steps = build_steps()
    assert index_of(steps, UPLOAD) < index_of(steps, BOOT)
    upload = step_named(steps, UPLOAD)
    assert "if" not in upload, \
        "the artifact upload became conditional; a red boot now loses its evidence"
    assert upload["with"]["if-no-files-found"] == "error", \
        "an upload that finds no image must fail rather than shrug"


EVIDENCE = "Upload the boot evidence"


def phase_evidence_files() -> set:
    """Every file img-boot.sh leaves in a phase's directory, read off the
    harness so that a new file cannot be missed by a hand-written list."""
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    found = set(re.findall(r'\$(?:dir|WORK/\$phase)/([A-Za-z0-9._-]+)',
                           harness))
    assert found, "img-boot.sh no longer writes anything into a phase directory"
    return found


def test_every_file_a_phase_leaves_behind_travels_as_evidence():
    """Every file a phase writes is matched by an evidence-upload glob. The
    listings and digests are the basis of the card checks, and a FAIL there
    cannot be diagnosed without them."""
    step = step_named(build_steps(), EVIDENCE)
    globs = [line.strip() for line in step["with"]["path"].splitlines()
             if line.strip()]
    # The phase directory is one level under $WORK; only globs with that
    # level can match a per-phase file.
    per_phase = [Path(g).name for g in globs
                 if "/otp-img/*/" in g.replace("\\", "/")]
    assert per_phase, f"no glob reaches into a phase directory: {globs}"
    for name in sorted(phase_evidence_files()):
        assert any(fnmatch(name, pattern) for pattern in per_phase), (
            f"img-boot.sh writes $WORK/<phase>/{name} and no path in the "
            f"{EVIDENCE!r} step collects it, so the run that needed it "
            f"cannot be diagnosed after the fact. Globs: {per_phase}")


#: The jobs whose steps must install mtools, and why each one needs it.
MTOOLS_JOBS = {
    "test": ("tests/test_img_verdict.py builds a real FAT partition and runs "
             "img-boot.sh's own fat_listing/fat_digest_of/fat_digests/"
             "strip_probe_droppings against it; without mtools those tests "
             "SKIP and the card functions go back to being covered by "
             "nothing"),
    "mutation": ("several fast-tier rows break those functions and name "
                 "those tests; mutation_gate.py refuses a row whose named "
                 "tests skipped, so the rows fail the job rather than pass "
                 "it"),
}


def test_the_jobs_that_need_mtools_install_mtools():
    """The jobs whose tests need mtools install it. Those tests skip without
    it, and in CI a skip is indistinguishable from a pass."""
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    for job, why in MTOOLS_JOBS.items():
        assert job in ci["jobs"], f"ci.yml no longer has a {job!r} job"
        runs = " ".join(step.get("run", "") for step in ci["jobs"][job]["steps"])
        assert re.search(r"apt-get install[^\n]*\bmtools\b", runs), (
            f"ci.yml's {job!r} job does not install mtools. {why}.")


def test_the_boot_evidence_upload_still_carries_the_verdict():
    """The evidence upload carries verdict.txt: the digest it holds is
    truncated on purpose (it covers a password hash), so the verdict has to
    be retrievable."""
    step = step_named(build_steps(), EVIDENCE)
    globs = step["with"]["path"]
    assert "verdict.txt" in globs, globs
    assert step.get("if") == "failure()", (
        "the boot evidence upload is the diagnosis path and img-boot.sh's "
        "comments say so; changing when it runs changes what those comments "
        "claim")


# --- the gate itself ------------------------------------------------------


def test_the_gate_is_exactly_the_expression_it_should_be():
    """The gate is exactly the pinned expression. Checking for `success()` and
    `refs/tags/` separately would pass `&&` -> `||` and `!startsWith(...)`."""
    condition = " ".join(str(step_named(build_steps(), ATTACH)["if"]).split())
    assert condition == GATE, (
        f"the release gate now reads {condition!r}, not {GATE!r}. If the "
        f"change is deliberate, work out what it does on a green push to "
        f"master before updating this line.")


def test_the_boot_step_can_still_fail_the_job():
    """The boot step can still fail the job: nothing in it (`|| true`,
    `continue-on-error`) swallows a failure, or the gate gates on nothing."""
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
    """The attached asset is the image. `fail_on_unmatched_files` defaults to
    false, so a missing `files:` would publish a release with no image."""
    attach = step_named(build_steps(), ATTACH)
    assert "softprops/action-gh-release@" in attach["uses"], attach["uses"]
    assert attach["with"]["files"] == "image/deploy/*.img.xz", attach["with"]


def test_the_note_is_appended_so_it_cannot_eat_the_release_notes():
    """The note is appended to the release body. Without append_body the
    action replaces an existing body, and a release created in the GitHub
    UI already exists (with the maintainer's notes) when this step runs."""
    attach = step_named(build_steps(), ATTACH)["with"]
    assert attach.get("append_body") is True, \
        ("without append_body the note REPLACES the maintainer's release "
         "notes on the UI-publish path; see the comment in image.yml")
    body = attach["body"]
    assert "harness/img-boot.sh" in body, \
        "the note must point at the file that states the gate's limits"
    assert "otp-unit.service" in body
    # Folded, not literal: release bodies render hard line breaks.
    assert "\n" not in body.strip(), \
        "the note carries hard line breaks and will render ragged"


# How many boots the note may claim, as English words. Derived from the
# harness so the note and the phase count cannot drift apart.
BOOT_COUNT_WORDS = {1: "once", 2: "twice", 3: "three times",
                    4: "four times", 5: "five times"}


def demanded_boots() -> int:
    """How many boots img-boot.sh refuses to run without."""
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    phases = re.search(r"^for want in ([a-z0-9 ]+); do$", harness, re.M)
    assert phases, "img-boot.sh no longer names the phases it demands"
    return len(phases.group(1).split())


def test_the_release_notes_boot_count_is_the_one_the_harness_demands():
    """The note's boot count is the one the harness demands.

    The harness must refuse a phase list that drops a boot, the workflow
    must not set `OTP_IMG_PHASES` to one, and the note must contain exactly
    one count word: the harness's.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    boots = demanded_boots()
    assert boots in BOOT_COUNT_WORDS, (
        f"the harness now demands {boots} boots and this file has no word "
        f"for that; add one rather than dropping the check")
    said = sorted(word for word in BOOT_COUNT_WORDS.values() if word in body)
    assert said == [BOOT_COUNT_WORDS[boots]], (
        f"img-boot.sh demands {boots} boots, so the release note has to say "
        f"{BOOT_COUNT_WORDS[boots]!r} and nothing else; it says {said}")
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    assert "leaves out:$PHASES_MISSING" in harness, \
        ("img-boot.sh no longer refuses a phase list that drops a boot, so "
         "the release note's boot-count claim is no longer backed by anything")
    phases = (step_named(build_steps(), BOOT).get("env") or {}).get("OTP_IMG_PHASES")
    if phases is not None:
        for boot in ("boot1", "boot2", "release"):
            assert boot in str(phases).split(), \
                (f"the boot step pins OTP_IMG_PHASES={phases!r}, which does "
                 f"not run {boot}, but the release note claims all three")


def test_the_job_cap_clears_every_boots_backstop():
    """The job cap exceeds the worst case of every boot's own timeout.

    A job cap firing is a cancellation, which skips the verdict and the
    evidence upload. The bound is derived from the harness's boot count and
    the workflow's per-boot backstop plus the `timeout -k 30` kill grace.
    `boots == 3` is also asserted outright, so adding a phase forces a look
    at every other place that states the count.
    """
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    boots = demanded_boots()
    assert boots == 3, (
        f"img-boot.sh now demands {boots} boots. The sums below re-derive "
        f"themselves, but the boot count is stated by hand in the release "
        f"note, in harness/README.md and in this workflow's budget summary, "
        f"and none of those move on their own -- check them, then move this "
        f"number")

    backstop = int((step_named(build_steps(), BOOT)["env"])["OTP_IMG_TIMEOUT"])
    grace = int(re.search(r"timeout -k (\d+) \"\$TIMEOUT\"", harness).group(1))
    cap = int(workflow()["jobs"]["build"]["timeout-minutes"])
    worst_boots = boots * (backstop + grace) / 60
    assert cap > worst_boots, (
        f"timeout-minutes: {cap} does not even cover {boots} boots at "
        f"{backstop}s + {grace}s of kill grace ({worst_boots:.1f} min), so a "
        f"run that lost every boot would be cancelled instead of reported")
    # The rest of the job has to fit too: twice the pi-gen cache-miss time
    # (6m58s) and twice everything else (1m19s).
    assert cap >= worst_boots + 2 * (6 + 58 / 60) + 2 * (1 + 19 / 60), (
        f"timeout-minutes: {cap} leaves no room for a slow pi-gen on top of "
        f"{worst_boots:.1f} minutes of worst-case boots")


def test_the_release_notes_inert_probe_claim_is_backed_by_the_harness():
    """The note's claim that the shipped test probe is inert is backed by the
    harness.

    img-guest-check.sh ships on every unit, runs as root and writes to
    /boot/firmware. It is safe only because of
    `ConditionKernelCommandLine=otp.imgcheck` and the release boot, which
    checks that systemd skipped the unit, the probe printed nothing and its
    records stayed off the card.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "img-guest-check.sh" in body, body
    assert "otp.imgcheck" in body, body
    assert "third boot" in body, body
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    for name in ("imgcheck-unit-considered-and-skipped",
                 "guest-probe-silent",
                 "guest-probe-journal-tag-absent",
                 "probe-droppings-stayed-off-the-card",
                 "identity-store-unchanged"):
        assert name in harness, (
            f"the release note says the shipped probe stayed inert and "
            f"img-boot.sh no longer has a {name} clause to say it with")
    # And the phase that runs them is one the harness refuses to skip.
    assert "for want in boot1 boot2 release; do" in harness, harness


def test_the_release_notes_credential_claim_is_backed_by_the_harness():
    """The note's credential claims (a seeded userconf.txt applied and
    consumed, a quiet second boot, a malformed seed failing fast) are
    backed by checks the harness demands by name."""
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "userconf" in body, body
    harness = (REPO / "harness" / "img-boot.sh").read_text()
    for name in ("userconf-seed-applied", "userconf-seeded-boot-ran-no-wizard",
                 "userconf-unseeded-boot-skips-the-wizard",
                 "userconf-malformed-seed-fails-fast",
                 # The checks behind the claim that the password is still in
                 # force after a power cycle.
                 "credential-recorded-outside-the-overlay",
                 "credential-recorded-for-the-next-boot",
                 "credential-survives-the-power-cycle",
                 # The check behind the note's username sentence: the rename
                 # does not survive, and the operator must be told which name
                 # to type.
                 "credential-recovers-a-store-naming-another-account"):
        assert name in harness, (
            f"the release note claims the credential path was checked, but "
            f"img-boot.sh no longer requires {name}")
    # The caveat itself must be in the body: `userconf-pi` renames the
    # UID-1000 account and the rename dies with the overlay, so the account
    # is `otp` on every later boot.
    assert "USERNAME does not survive" in body, (
        "the release note promises a persistent login without saying the "
        "username reverts: an operator would type a name that is not there")
    assert "Log in as `otp`" in body, body
    # The note also says what persistence costs: the hash sits on a partition
    # any card reader can mount.
    for warning in ("world-readable", "offline"):
        assert warning in body, (
            f"the release note announces a persistent login without the word "
            f"{warning!r}: the exposure is part of the claim, not a footnote")
    # The harness has to plant a userconf.txt, or the seeded branch is
    # untested. The copy is matched, not the name, which a log line also
    # contains.
    assert re.search(r"^mcopy[^\n]*::userconf\.txt", harness, re.M), \
        "nothing seeds a userconf.txt, so the note's first clause is empty"


def test_the_release_notes_boot_completion_claim_is_backed_by_the_harness():
    """The note's claims that both boots ran to completion (network-online not
    blocking, the getty on the panel's tty started) are backed by checks
    the harness demands by name."""
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
    """The note's claim that the second boot is the same machine as the first
    is backed by the named machine-id checks and by the host-side check
    that systemd did not treat it as a first boot.

    The note must not claim a stable SSH fingerprint: `ENABLE_SSH=0` means
    sshd never runs and no host key is kept outside the overlay.
    """
    body = step_named(build_steps(), ATTACH)["with"]["body"]
    assert "machine-id" in body, body
    # The exposure is part of the claim: the boot partition is vfat mounted
    # with `defaults`, so everything on it is world-readable.
    assert "otp-identity" in body, body
    assert "world-readable" in body, body
    assert "fingerprint" not in body.lower(), (
        "the release note still advertises a stable SSH host key "
        "fingerprint; the image disables sshd and keeps no host key outside "
        "the overlay: " + body)
    assert "copy of the host keys" not in body.lower(), body
    # Stated explicitly, so nobody assumes host keys are kept on the FAT
    # partition too.
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
# The probe runs inside the image: install.sh installs
# harness/img-guest-check.sh into /opt/otp-unit, so editing a check edits the
# artifact.

PROBE = "harness/img-guest-check.sh"


def test_the_probe_is_really_part_of_the_image():
    # The premise of the two tests below: if install.sh stops installing the
    # probe, they guard nothing.
    assert PROBE in (REPO / "device" / "install.sh").read_text()


def test_a_change_to_the_probe_runs_the_image_job():
    """A change to the probe triggers the image job; no other job can execute
    that file. The shipped regex is applied to real paths, not searched for
    as a substring."""
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
    # And a docs-only change must not pay for a pi-gen build.
    for path in ("README.md", "tests/test_img_verdict.py", "harness/README.md"):
        assert not matcher.search(path), f"{path} needlessly triggers a build"


def test_the_cache_key_covers_the_probe_the_image_ships():
    """The cache key covers the probe. Otherwise a stale image boots old guest
    checks and the run goes red for checks the guest never emitted, which
    reads as a broken image."""
    for step in build_steps():
        if str(step.get("uses", "")).startswith("actions/cache/restore"):
            key = str(step["with"]["key"])
            break
    else:
        raise AssertionError("no cache restore step in the build job")
    assert PROBE in key, key
    # Existing inputs stay in the key: an image built before otpunit changed
    # is the wrong image to boot.
    for pattern in ("image/**", "device/**", "otpunit/**", "codewords/**"):
        assert pattern in key, key


def test_the_gate_never_fires_on_a_branch_or_a_pull_request():
    """The gate never fires on a branch or pull request: `tag_name` defaults
    to `github.ref_name`, so it would create a release called `master`."""
    condition = str(step_named(build_steps(), ATTACH)["if"])
    assert re.search(r"startsWith\(\s*github\.ref\s*,\s*'refs/tags/'\s*\)",
                     condition), condition
    assert "!" not in condition, f"the tag test is negated: {condition!r}"


# --- the required check, which exists to always report -------------------
#
# Branch protection requires a check called `image`. A required check that
# never reports blocks forever, so it must run on every pull request.


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
    # The build job stays conditional so a docs-only PR does not pay for a
    # pi-gen build.
    assert build["runs-on"] == "ubuntu-24.04-arm"


def test_the_gate_cannot_pass_by_falling_off_the_end():
    """Every branch of the verdict either exits non-zero or says why not, so
    an unforeseen job result cannot read as approval."""
    run = workflow()["jobs"]["image"]["steps"][0]["run"]
    assert "set -euo pipefail" in run
    assert run.count("exit 1") >= 3, (
        "the verdict has fewer failure exits than it has ways to fail")
    for swallow in SWALLOWS_FAILURE:
        assert swallow not in run, (
            f"the verdict swallows failure with {swallow!r}")

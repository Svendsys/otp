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
WORKFLOW = REPO / ".github" / "workflows" / "image.yml"

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


def image_steps() -> list:
    return workflow()["jobs"]["image"]["steps"]


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


def release_publishers() -> list:
    """
    Every step in the whole file that can create a release, by what it
    USES rather than what it is called.

    Name-based lookup cannot see a second publisher under another name --
    `Publish the release asset`, placed before the boot, was invisible to
    the first version of this file and shipped the image ungated.
    """
    found = []
    for job_name, job in workflow()["jobs"].items():
        for position, step in enumerate(job.get("steps", [])):
            uses = str(step.get("uses", ""))
            if "gh-release" in uses or "create-release" in uses:
                found.append((job_name, position, step))
    return found


# --- the ordering ---------------------------------------------------------


def test_the_release_asset_is_attached_after_the_boot():
    """The point of the file."""
    steps = image_steps()
    assert index_of(steps, ATTACH) > index_of(steps, BOOT), (
        "the release attachment runs before the boot gate, so a tag whose "
        "image cannot boot would ship it anyway")


def test_there_is_exactly_one_thing_that_can_publish_a_release():
    publishers = release_publishers()
    assert len(publishers) == 1, (
        "more than one step can create a release; every one of them is a "
        f"way past the boot gate: {[(j, s.get('name')) for j, _, s in publishers]}")
    job, _, step = publishers[0]
    assert job == "image", (
        f"the publisher moved to the {job!r} job. That may be fine, but the "
        f"ordering assertions here only see the `image` job -- update them "
        f"deliberately rather than letting this pass.")
    assert step.get("name") == ATTACH, step.get("name")


def test_the_plain_artifact_is_still_uploaded_before_the_boot():
    """
    The other half, and it must NOT move. Debugging a red boot needs the
    image that failed, so the artifact is uploaded unconditionally and
    early. Only the release asset is gated.
    """
    steps = image_steps()
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
    condition = " ".join(str(step_named(image_steps(), ATTACH)["if"]).split())
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
    boot = step_named(image_steps(), BOOT)
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
    attach = step_named(image_steps(), ATTACH)
    assert "softprops/action-gh-release@" in attach["uses"], attach["uses"]
    assert attach["with"]["files"] == "image/deploy/*.img.xz", attach["with"]


def test_the_note_is_set_rather_than_appended():
    """
    `append_body` is consulted only on the action's UPDATE path; the create
    path ignores it. So on a first tag run it does nothing, and on a re-run
    or a workflow_dispatch against the same tag it would append the same
    paragraph again. Plain `body` is idempotent.
    """
    attach = step_named(image_steps(), ATTACH)["with"]
    assert "append_body" not in attach, \
        "append_body duplicates the note on any re-run; see the comment in image.yml"
    body = attach["body"]
    assert "harness/img-boot.sh" in body, \
        "the note must point at the file that states the gate's limits"
    assert "otp-unit.service" in body
    # Folded, not literal: release bodies render hard line breaks, and a
    # block scalar would ship this at whatever width the YAML is wrapped to.
    assert "\n" not in body.strip(), \
        "the note carries hard line breaks and will render ragged"


def test_the_gate_never_fires_on_a_branch_or_a_pull_request():
    """
    `tag_name` defaults to `github.ref_name`, so a gate that let a branch
    through would create a release literally called `master`.
    """
    condition = str(step_named(image_steps(), ATTACH)["if"])
    assert re.search(r"startsWith\(\s*github\.ref\s*,\s*'refs/tags/'\s*\)",
                     condition), condition
    assert "!" not in condition, f"the tag test is negated: {condition!r}"

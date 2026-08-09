"""What a tag is allowed to publish.

`image.yml` builds the image, uploads it as an artifact, boots it (tier 3),
and on a tag attaches it to the release. The ORDER of the last two is the
whole of this file: for a while the attachment happened first, so a tagged
image that could not boot shipped anyway, to be written to a card and
handed to somebody.

The ordering is invisible in review -- two steps moved a hundred lines
apart in a 350-line workflow -- and nothing in CI would notice it moving
back, because the only run that would demonstrate it is a tag run with a
broken image, which is the run nobody wants to have. So it is asserted
here instead, from the YAML rather than from a copy of it.

WHAT THIS CANNOT DO is prove that GitHub honours the rule. It asserts the
workflow says the right thing; the runner's semantics -- that a step whose
`if` names no status function carries an implicit `success()`, and that an
`always()` step earlier in the job does not clear the failure state -- are
GitHub's, documented, and not reproducible here. That is why the step
writes `success() &&` out in full rather than relying on the implicit
form: a reader can check it without knowing the rule, and so can
test_the_gate_is_not_written_in_the_implicit_form below.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "image.yml"

BOOT = "Boot the image"
ATTACH = "Attach to the release"
UPLOAD = "Upload"

# Anything that would let a step run after an earlier one failed. A gate
# carrying one of these is not a gate.
ESCAPES = ("always()", "failure()", "cancelled()")


@pytest.fixture(scope="module")
def steps() -> list:
    # `on:` is parsed by PyYAML as the boolean True (YAML 1.1), which is
    # harmless here but startling if you go looking for it.
    document = yaml.safe_load(WORKFLOW.read_text())
    return document["jobs"]["image"]["steps"]


def index_of(steps, name) -> int:
    for position, step in enumerate(steps):
        if step.get("name") == name:
            return position
    raise AssertionError(
        f"no step named {name!r} in image.yml; the steps are: "
        + ", ".join(repr(s.get("name")) for s in steps))


def step_named(steps, name) -> dict:
    return steps[index_of(steps, name)]


def test_the_release_asset_is_attached_after_the_boot(steps):
    """The point of the file."""
    assert index_of(steps, ATTACH) > index_of(steps, BOOT), (
        "the release attachment runs before the boot gate, so a tag whose "
        "image cannot boot would ship it anyway")


def test_the_plain_artifact_is_still_uploaded_before_the_boot(steps):
    """
    The other half, and it must NOT move. Debugging a red boot needs the
    image that failed, so the artifact is uploaded unconditionally and
    early. Only the release asset is gated. Moving both would fix the
    release and break the debugging.
    """
    assert index_of(steps, UPLOAD) < index_of(steps, BOOT)
    upload = step_named(steps, UPLOAD)
    assert "if" not in upload, \
        "the artifact upload became conditional; a red boot now loses its evidence"


def test_the_gate_cannot_run_after_a_failed_boot(steps):
    condition = step_named(steps, ATTACH)["if"]
    assert "refs/tags/" in condition, condition
    for escape in ESCAPES:
        assert escape not in condition, (
            f"the release attachment's `if` contains {escape}, which runs it "
            f"whatever the boot did")


def test_the_gate_is_not_written_in_the_implicit_form(steps):
    """
    `if: startsWith(...)` alone WOULD work -- GitHub adds success() when the
    condition names no status function. It is written out anyway, because
    this is the line that decides whether a broken image reaches a human
    and it should not rest on the reader recalling that rule.
    """
    assert "success()" in step_named(steps, ATTACH)["if"]


def test_the_boot_step_is_still_allowed_to_fail_the_job(steps):
    """
    The gate is worth nothing if the thing it gates on cannot go red.
    `continue-on-error: true` on the boot once made this job report success
    while tier 3 exited 1 -- the GitHub API called the step `success` -- and
    with the attachment now downstream, restoring it would silently
    re-open exactly the hole this file exists to close.
    """
    boot = step_named(steps, BOOT)
    assert not boot.get("continue-on-error"), boot
    assert "if" not in boot, \
        "the boot step became conditional; it may no longer gate anything"


def test_the_asset_carries_what_the_gate_does_not_prove(steps):
    """
    A green tier 3 says the image comes up and the unit starts. QEMU's
    raspi3b is a Pi with nothing plugged into it -- no OLED, no buttons, no
    printer -- and someone reading a release page has no way to know that
    unless the release says so.
    """
    attach = step_named(steps, ATTACH)["with"]
    assert attach.get("append_body") is True, \
        "body would REPLACE the release notes rather than add to them"
    body = attach["body"]
    assert "harness/img-boot.sh" in body, \
        "the note must point at the file that states the gate's limits"
    assert "otp-unit.service" in body


def test_every_step_this_file_names_exists(steps):
    # A rename would otherwise turn every assertion above into a lookup
    # that raises -- which is a red run, but a confusing one. This says it
    # once, plainly, and index_of's message names the steps it did find.
    for name in (BOOT, ATTACH, UPLOAD):
        index_of(steps, name)

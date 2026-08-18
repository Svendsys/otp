"""The mutation gate is itself a guard, so it has to be shown able to fail.

A gate that reports "all caught" whatever it is fed is the defect it exists
to catch, one level up -- and this repository has produced that exact shape
before: a rig whose CUPS half skipped for five separate reasons and left the
job green at "1 passed, 20 skipped".

So the runner is driven against a scratch repository built for the purpose,
with a guard, a test of that guard, and mutations whose outcome is known:

    caught       the guard is broken and the test notices          -> ok
    survivor     the edit changes nothing the tests can see        -> MISS
    rotted       the `find` string is no longer in the file        -> BROKEN
    already red  the named tests fail before any mutation          -> BROKEN

and the tree is checked, byte for byte, after each one -- including a file
with uncommitted edits in it, because restoring to HEAD instead of to what
was there is how a previous hand-run round destroyed an uncommitted fix.

The last test here is the cheap one that matters most day to day: every row
in the real tests/mutations.toml still applies to the real files. A `find`
that has drifted is a row that would silently stop mutating anything, so it
is a failure in the ordinary suite and not only in the gate job.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mutation_gate as gate                              # noqa: E402

FAST = gate.Tier(name="fast", description="scratch", pytest_args=())

# --- the scratch repository ----------------------------------------------
#
# A guard shaped like the ones this table really protects: it must accept
# ONLY the exact success line, because the machine's hostname is a substring
# of it. That is row 2 of issue #14, in eight lines.

GUARD = '''\
"""A verdict, in miniature."""


def unit_started(lines):
    # NOT `"otp-unit" in line`: the host is called otp-unit, so the login
    # prompt would match and a boot that never started the unit would pass.
    return any(line.strip() == "Started otp-unit.service" for line in lines)


def unused_helper():
    """Nothing tests this, which is the point of the survivor case."""
    return 17
'''

GUARD_TESTS = '''\
from verdict import unit_started


def test_the_success_line_is_accepted():
    assert unit_started(["Started otp-unit.service"])


def test_the_hostname_is_not_the_unit():
    assert not unit_started(["otp-unit login: "])
'''

ALREADY_RED = '''\
def test_this_one_is_broken_before_anything_is_mutated():
    assert False, "red on the unmutated tree"
'''

CAUGHT = ("tests/test_verdict.py::test_the_hostname_is_not_the_unit",)


def mutation(name="hostname", *, edits=None, tests=CAUGHT, tier="fast"):
    return gate.Mutation(
        name=name, tier=tier, guard="the scratch guard", tests=tests,
        edits=tuple(edits or [gate.Edit(
            path="verdict.py",
            find='line.strip() == "Started otp-unit.service"',
            replace='"otp-unit" in line')]))


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    """One repository for the whole module, so baselines are paid for once."""
    repo = tmp_path_factory.mktemp("scratch-repo")
    (repo / "verdict.py").write_text(GUARD)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_verdict.py").write_text(GUARD_TESTS)
    (tests / "test_already_red.py").write_text(ALREADY_RED)
    # conftest, not sys.path juggling: the nested pytest runs with this
    # directory as its rootdir and must import verdict.py from it.
    (repo / "conftest.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n")
    return repo


@pytest.fixture(scope="module")
def real(scratch):
    """A gate that really shells out to pytest, sharing one baseline cache."""
    return gate.Gate(repo=scratch, tiers={"fast": FAST})


def fake(result):
    """A runner that answers with a fixed TestRun instead of running pytest."""
    return lambda repo, tier, tests: result


def red(**kwargs):
    counts = {"failed": 1}
    counts.update(kwargs.pop("counts", {}))
    return gate.TestRun(returncode=1, counts=counts,
                        failed=("tests/test_verdict.py::test_x",), errors=(),
                        output="", seconds=0.0, **kwargs)


def green(**counts):
    return gate.TestRun(returncode=0, counts=counts or {"passed": 2},
                        failed=(), errors=(), output="", seconds=0.0)


# --- the two cases the whole thing rests on -------------------------------


class TestTheRunnerCanTellRedFromGreen:
    def test_a_mutation_the_tests_catch_is_reported_caught(self, real):
        outcome = real.apply_and_judge(mutation())
        assert outcome.ok, outcome.detail
        assert outcome.red == ("tests/test_verdict.py::test_the_hostname_is_not_the_unit",)

    def test_a_mutation_nothing_catches_is_reported_as_a_survivor(self, real):
        """The control. An edit inside a function no test calls changes the
        file and changes nothing the suite can see -- exactly the shape of a
        guard that has stopped discriminating, and the gate must say so
        rather than counting it."""
        outcome = real.apply_and_judge(mutation(
            "dead-code",
            edits=[gate.Edit(path="verdict.py", find="return 17",
                             replace="return 18")]))
        assert not outcome.ok
        assert outcome.verdict == "SURVIVED", outcome.detail
        assert "stayed green" in outcome.detail


# --- rot must be loud -----------------------------------------------------


class TestAMutationThatCannotBeApplied:
    """The failure mode that would turn this gate into a no-op: a `find`
    that no longer matches, quietly mutating nothing and quietly passing."""

    def test_a_find_that_no_longer_matches_is_a_failure_not_a_skip(self, real):
        outcome = real.apply_and_judge(mutation(
            "moved",
            edits=[gate.Edit(path="verdict.py",
                             find="a line that was refactored away",
                             replace="something")]))
        assert outcome.verdict == "BROKEN"
        assert "matches 0 time(s)" in outcome.detail

    def test_a_find_that_matches_twice_is_a_failure(self, real, scratch):
        """Which of the two got mutated would depend on the file's history."""
        outcome = real.apply_and_judge(mutation(
            "ambiguous",
            edits=[gate.Edit(path="verdict.py", find="return",
                             replace="return not")]))
        assert outcome.verdict == "BROKEN"
        assert "matches 2 time(s)" in outcome.detail

    def test_a_file_that_is_gone_is_a_failure(self, real):
        outcome = real.apply_and_judge(mutation(
            "renamed", edits=[gate.Edit(path="harness/gone.sh", find="x",
                                        replace="y")]))
        assert outcome.verdict == "BROKEN"
        assert "does not exist" in outcome.detail

    def test_a_broken_row_is_not_counted_as_caught(self, real):
        outcomes = real.run([mutation(), mutation(
            "moved", edits=[gate.Edit(path="verdict.py", find="not here",
                                      replace="x")])])
        assert [o.ok for o in outcomes] == [True, False]


# --- the anti-tautology checks -------------------------------------------


class TestTheBaseline:
    """A test that is already red catches every mutation, including the ones
    it cannot see. So the named tests must pass BEFORE the mutation."""

    def test_tests_that_are_already_failing_are_a_broken_row(self, real):
        outcome = real.apply_and_judge(mutation(
            "already-red",
            tests=("tests/test_already_red.py::test_this_one_is_broken_before_anything_is_mutated",)))
        assert outcome.verdict == "BROKEN"
        assert "ALREADY failing" in outcome.detail

    def test_a_node_id_that_does_not_exist_is_a_broken_row(self, real):
        outcome = real.apply_and_judge(mutation(
            "typo", tests=("tests/test_verdict.py::test_no_such_test",)))
        assert outcome.verdict == "BROKEN"
        assert "collected no tests" in outcome.detail

    def test_all_skipped_is_not_a_usable_baseline(self, scratch):
        """The hardware tier on a machine with no cupsd. A skipped test
        cannot go red, and a gate that reads that as proof is the "1 passed,
        20 skipped" green job all over again."""
        one = gate.Gate(repo=scratch, tiers={"fast": FAST},
                        runner=fake(green(passed=1, skipped=3)))
        outcome = one.apply_and_judge(mutation())
        assert outcome.verdict == "BROKEN"
        assert "SKIPPED" in outcome.detail

    def test_a_baseline_that_hangs_is_a_broken_row(self, scratch):
        timed_out = gate.TestRun(returncode=gate.TIMED_OUT, counts={},
                                 failed=(), errors=(), output="", seconds=300)
        one = gate.Gate(repo=scratch, tiers={"fast": FAST},
                        runner=fake(timed_out))
        assert "did not finish" in one.apply_and_judge(mutation()).detail

    def test_a_hang_under_mutation_is_not_read_as_a_red_test(self, scratch):
        """A mutated run can hang rather than fail -- one here sat for ten
        minutes before being killed from outside. A hang is not a check
        firing, and counting it as one would credit the guard for a wedge."""
        answers = iter([green(passed=2),
                        gate.TestRun(returncode=gate.TIMED_OUT, counts={},
                                     failed=(), errors=(), output="",
                                     seconds=300)])
        one = gate.Gate(repo=scratch, tiers={"fast": FAST},
                        runner=lambda *_: next(answers))
        outcome = one.apply_and_judge(mutation())
        assert outcome.verdict == "BROKEN"
        assert "did not finish" in outcome.detail

    def test_red_with_no_failed_test_is_not_a_catch(self, scratch):
        """rc=2 with an ERROR is the mutation breaking collection, not a
        guard noticing. Counting it would let a syntax error stand in for
        coverage."""
        answers = iter([green(passed=2),
                        gate.TestRun(returncode=2, counts={"error": 1},
                                     failed=(), errors=("tests/test_x.py",),
                                     output="", seconds=0.1)])
        one = gate.Gate(repo=scratch, tiers={"fast": FAST},
                        runner=lambda *_: next(answers))
        outcome = one.apply_and_judge(mutation())
        assert outcome.verdict == "BROKEN"
        assert "no FAILED test" in outcome.detail


# --- putting the tree back ------------------------------------------------


class TestTheTreeIsPutBack:
    """`git checkout` between mutations destroyed an uncommitted fix once,
    and `git stash push` + `git stash drop` does the same thing one step
    removed -- measured, it reverts the file to HEAD and takes the working
    copy's edits into the dropped stash. So the restore is byte-for-byte
    from what was read before the mutation."""

    def test_a_caught_mutation_leaves_the_file_exactly_as_it_was(self, real, scratch):
        before = (scratch / "verdict.py").read_bytes()
        real.apply_and_judge(mutation())
        assert (scratch / "verdict.py").read_bytes() == before

    def test_uncommitted_work_in_the_mutated_file_survives(self, scratch):
        """The case that matters and the one the git-based restores lose."""
        target = scratch / "verdict.py"
        original = target.read_text()
        wip = original.replace("def unused_helper():",
                               "def unused_helper():  # my uncommitted fix")
        target.write_text(wip)
        try:
            one = gate.Gate(repo=scratch, tiers={"fast": FAST},
                            runner=fake(red()))
            one._baselines[("fast", CAUGHT)] = ""      # skip the baseline run
            outcome = one.apply_and_judge(mutation())
            assert outcome.ok
            assert target.read_text() == wip, \
                "the restore reverted to HEAD and ate the uncommitted edit"
        finally:
            target.write_text(original)

    def test_the_tree_is_restored_even_when_the_test_run_explodes(self, scratch):
        def explode(repo, tier, tests):
            raise RuntimeError("pytest could not start")

        before = (scratch / "verdict.py").read_bytes()
        one = gate.Gate(repo=scratch, tiers={"fast": FAST}, runner=explode)
        one._baselines[("fast", CAUGHT)] = ""
        with pytest.raises(RuntimeError):
            one.apply_and_judge(mutation())
        assert (scratch / "verdict.py").read_bytes() == before

    def test_a_same_length_mutation_does_not_outlive_itself_in_the_cache(
            self, scratch):
        """
        Putting the BYTES back is not the same as putting the tree back.

        CPython decides a `__pycache__` entry is still valid by comparing
        the source's mtime in whole seconds and its size, and nothing else.
        A mutation whose replacement is the same length as what it replaced
        -- a swapped pair of lines -- changes neither, so a mutated run
        that finishes inside a second leaves a cache entry the RESTORED
        file matches exactly, and the interpreter goes on running the
        mutation out of a tree that `git diff --exit-code` calls clean.

        Found the hard way: after a fast-tier run in which every row was
        reported caught and the tree was verified clean, the next ordinary
        `pytest` in the same checkout failed on the swapped-line mutation
        from `panel-replacement-runs-before-anything-can-find-it`.

        The mutation here is written and restored inside one call, which is
        well inside one second, and it is length-preserving by
        construction: `return 17` for `return 18`.
        """
        target = scratch / "verdict.py"
        cached = pathlib.Path(importlib.util.cache_from_source(str(target)))
        cached.parent.mkdir(parents=True, exist_ok=True)
        original = target.read_bytes()
        stamp = target.stat().st_mtime

        # A cache entry that claims to be for the file as it stands now,
        # exactly as a mutated run leaves behind. The contents do not
        # matter -- what is asserted is whether it is still believed.
        cached.write_bytes(b"the mutated bytecode")
        os.utime(cached, (stamp, stamp))

        one = gate.Gate(repo=scratch, tiers={"fast": FAST}, runner=fake(red()))
        one._baselines[("fast", CAUGHT)] = ""
        one.apply_and_judge(mutation("same-length", edits=[
            gate.Edit(path="verdict.py", find="return 17", replace="return 18")]))

        assert target.read_bytes() == original, "the source was not restored"
        assert not cached.exists(), (
            "the restored file still has its cache entry, and the source's "
            "(mtime, size) are what they were -- so every later run in this "
            "checkout executes the mutation out of a tree that git calls "
            "clean")

    def test_every_file_of_a_multi_edit_mutation_is_restored(self, scratch):
        before = {name: (scratch / name).read_bytes()
                  for name in ("verdict.py", "tests/test_verdict.py")}
        one = gate.Gate(repo=scratch, tiers={"fast": FAST}, runner=fake(red()))
        one._baselines[("fast", CAUGHT)] = ""
        one.apply_and_judge(mutation("two-files", edits=[
            gate.Edit(path="verdict.py", find="return 17", replace="return 18"),
            gate.Edit(path="tests/test_verdict.py", find="def test_the_success_line_is_accepted",
                      replace="def test_renamed_by_the_mutation")]))
        assert {name: (scratch / name).read_bytes() for name in before} == before


# --- what CI actually depends on -----------------------------------------


class TestTheExitCode:
    """CI reads one thing: whether the process returned 0."""

    def write_table(self, repo, rows):
        table = repo / "mutations.toml"
        table.write_text("[tiers.fast]\ndescription = \"scratch\"\n\n" + rows)
        return table

    CAUGHT_ROW = '''
[[mutation]]
name = "hostname"
tier = "fast"
guard = "only the exact success line"
tests = ["tests/test_verdict.py::test_the_hostname_is_not_the_unit"]
[[mutation.edit]]
file = "verdict.py"
find = 'line.strip() == "Started otp-unit.service"'
replace = '"otp-unit" in line'
'''

    SURVIVOR_ROW = '''
[[mutation]]
name = "dead-code"
tier = "fast"
guard = "nothing calls this"
tests = ["tests/test_verdict.py::test_the_hostname_is_not_the_unit"]
[[mutation.edit]]
file = "verdict.py"
find = "return 17"
replace = "return 18"
'''

    def test_a_table_whose_mutations_are_all_caught_exits_zero(self, scratch, capsys):
        table = self.write_table(scratch, self.CAUGHT_ROW)
        code = gate.main(["--tier", "fast", "--table", str(table),
                          "--repo", str(scratch)])
        assert code == 0, capsys.readouterr().out

    def test_one_survivor_fails_the_run(self, scratch, capsys):
        table = self.write_table(scratch, self.CAUGHT_ROW + self.SURVIVOR_ROW)
        code = gate.main(["--tier", "fast", "--table", str(table),
                          "--repo", str(scratch)])
        assert code == 1
        assert "dead-code" in capsys.readouterr().err

    def test_a_tier_that_was_not_run_is_named_in_the_summary(self, scratch, capsys):
        """A partial run must not read as a complete one.

        The row here is deliberately one that cannot be applied, so this
        costs no pytest launch: it exercises the summary and the loud-rot
        path through main() at the same time.
        """
        table = self.write_table(
            scratch,
            '[tiers.hardware]\ndescription = "needs a cupsd"\n'
            + self.CAUGHT_ROW.replace('find = \'line.strip() == "Started otp-unit.service"\'',
                                      "find = 'a line nobody wrote'"))
        code = gate.main(["--tier", "fast", "--table", str(table),
                          "--repo", str(scratch)])
        captured = capsys.readouterr()
        assert code == 1, "a row that could not be applied passed the gate"
        assert "matches 0 time(s)" in captured.err
        assert "'hardware' was NOT run" in captured.out
        assert "needs a cupsd" in captured.out


# --- the table itself -----------------------------------------------------


class TestTheTableIsWellFormed:
    @pytest.mark.parametrize("row,why", [
        ('[[mutation]]\nname="a"\ntier="nope"\nguard="g"\ntests=["t"]\n'
         '[[mutation.edit]]\nfile="verdict.py"\nfind="x"\nreplace="y"\n',
         "not one of"),
        ('[[mutation]]\nname="a"\ntier="fast"\nguard="g"\ntests=[]\n'
         '[[mutation.edit]]\nfile="verdict.py"\nfind="x"\nreplace="y"\n',
         "no tests named"),
        ('[[mutation]]\nname="a"\ntier="fast"\nguard="g"\ntests=["t"]\n'
         '[[mutation.edit]]\nfile="verdict.py"\nfind="x"\nreplace="x"\n',
         "identical"),
        ('[[mutation]]\nname="a"\ntier="fast"\nguard="g"\ntests=["t"]\n'
         '[[mutation.edit]]\nfile="verdict.py"\nfind=""\nreplace="y"\n',
         "empty `find`"),
        ('[[mutation]]\nname="a"\ntier="fast"\nguard="g"\ntests=["t"]\n',
         "has no 'edit'"),
        ('[[mutation]]\nname="a"\ntier="fast"\nguard="g"\ntests=["t"]\n'
         '[[mutation.edit]]\nfile="verdict.py"\nfind="x"\nreplace="y"\n'
         '[[mutation]]\nname="a"\ntier="fast"\nguard="g"\ntests=["t"]\n'
         '[[mutation.edit]]\nfile="verdict.py"\nfind="x"\nreplace="y"\n',
         "two mutations are called"),
    ])
    def test_a_malformed_row_is_refused_loudly(self, tmp_path, row, why):
        table = tmp_path / "mutations.toml"
        table.write_text('[tiers.fast]\ndescription = "s"\n\n' + row)
        with pytest.raises(ValueError, match=why):
            gate.load(table)

    def test_the_shipped_table_loads(self):
        tiers, mutations = gate.load()
        assert mutations, "the shipped table is empty"
        assert set(tiers) >= {"fast", "hardware"}

    def test_every_row_in_the_shipped_table_still_applies(self):
        """The cheap half of the gate, run by the ordinary suite.

        A `find` that no longer matches its file is a row that mutates
        nothing. The gate job fails on it too, but this costs a hundredth of
        a second and catches it on the PR that renamed the guard.
        """
        _, mutations = gate.load()
        rotted = {m.name: gate.applies(m) for m in mutations if gate.applies(m)}
        assert not rotted, (
            f"mutations.toml no longer matches the files it mutates: "
            f"{rotted}. Re-derive the `find` strings by hand and confirm "
            f"the named tests still go red -- do not just make it match.")

    def test_every_named_test_file_exists(self):
        """A node id pointing at a deleted file collects nothing, which the
        gate calls BROKEN -- but only when it runs. This is the same check
        for the price of a stat."""
        _, mutations = gate.load()
        missing = {m.name: node for m in mutations for node in m.tests
                   if not (gate.REPO / node.split("::")[0]).is_file()}
        assert not missing, missing


# --- reading pytest's answer ---------------------------------------------


class TestParsingPytestOutput:
    """The verdict rests entirely on this, and -q output is a text format."""

    def test_failures_are_read_with_their_node_ids(self):
        counts, failed, errors = gate.parse_pytest(
            "..F\nFAILED tests/test_a.py::test_b - AssertionError: nope\n"
            "1 failed, 2 passed in 0.42s\n")
        assert counts == {"failed": 1, "passed": 2}
        assert failed == ("tests/test_a.py::test_b",)
        assert errors == ()

    def test_errors_are_not_mistaken_for_failures(self):
        counts, failed, errors = gate.parse_pytest(
            "ERROR tests/test_a.py - ImportError: no module named verdict\n"
            "1 error in 0.10s\n")
        assert failed == ()
        assert errors == ("tests/test_a.py",)
        assert counts == {"error": 1}

    def test_a_deselected_count_is_not_a_pass(self):
        counts, _, _ = gate.parse_pytest("3 passed, 21 deselected in 1.0s\n")
        assert counts == {"passed": 3, "deselected": 21}

    def test_counts_come_from_pytest_and_not_from_a_test_s_own_output(self):
        """A test that prints a pytest summary of its own -- this file does,
        it drives nested runs -- must not be able to rewrite the verdict."""
        counts, failed, _ = gate.parse_pytest(
            "captured stdout: === 4 passed in 0.10s\n"
            "FAILED tests/test_a.py::test_b - assert '2 passed in 1.0s'\n"
            "1 failed in 0.42s\n")
        assert counts == {"failed": 1}
        assert failed == ("tests/test_a.py::test_b",)

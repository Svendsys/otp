#!/usr/bin/env python3
"""Break each load-bearing guard on purpose and require the suite to notice.

WHY THIS EXISTS. Five checks in this repository have been found unable to
fail, one per audit round: a tier-3 boot offset aimed at a zero-filled gap,
a tier-3 verdict grepping for the image's own hostname, a tier-2 spool check
that was really a systemd invariant, a truncated-run gate satisfied by a
guest killed after two checks, and a group guard that matched the TEXT of a
`for` header and never ran the body. Each was found by hand, mutated by
hand, and confirmed by hand -- and the next one was found the same way, a
round later. The recurring defect is not any of them individually: it is
that a check's ability to fail is UNVERIFIED BY DEFAULT, and five rounds of
reading did not establish it. See issue #14.

So the hand-run round becomes a table and a runner. `mutations.toml` says,
for each guard, "this exists to catch X; here is X, and here are the tests
that must go red for it". This runs them.

WHAT IT REFUSES TO DO QUIETLY. Every way this gate could rot into a no-op is
a loud failure rather than a skip:

  - a `find` string that no longer matches the file (the guard moved, or was
    rewritten) fails the run. A mutation that cannot be applied proves
    nothing, and a gate that shrugs at that is the exact defect class it
    exists to fix.
  - a `find` that matches more than the declared number of times fails too:
    which occurrence got mutated would then depend on the file's history.
  - the named tests must be GREEN before the mutation. A test that is
    already red catches everything, including mutations it cannot see.
  - the named tests must actually RUN. All-skipped is not proof; on a
    machine with no cupsd the hardware tier says so instead of passing.
  - a tier that was not selected is named in the summary with its mutation
    count, so a partial run cannot read as a complete one.

RESTORING THE TREE. Not `git checkout`: a previous hand-run round used it
between mutations and destroyed an uncommitted fix in the same tree. Not
`git stash push` + `git stash drop` either, which the issue suggested and
which was measured here before choosing:

    $ printf 'committed line\nMY UNCOMMITTED FIX\n' > f.txt   # dirty tree
    $ sed -i s/committed/MUTATED/ f.txt                       # the mutation
    $ git stash push -- f.txt && git stash drop
    $ cat f.txt
    committed line

The developer's uncommitted work went with the mutation -- recoverable from
the dropped stash object, but gone from the worktree. That is `git checkout`
one step removed, and this runner is meant to be safe to run on a tree with
work in progress in it, because a gate people are afraid to run locally gets
run in CI only.

What it does instead: read the file's exact bytes before mutating, write
those bytes back afterwards in a `finally`, and verify the readback matches.
`git stash create` is taken once at the start as a crash net -- it writes a
recoverable snapshot commit of the whole dirty tree and, unlike `stash
push`, does not touch the worktree to do it.

USAGE

    python3 tests/mutation_gate.py --tier fast        # ~20s, per PR
    python3 tests/mutation_gate.py --tier hardware    # needs root + cupsd
    python3 tests/mutation_gate.py --tier all
    python3 tests/mutation_gate.py --list

There is deliberately no default tier. The hardware tier needs a real cupsd
and the fast tier does not, so any default would be either a run that fails
on most machines or a run that silently covers less than the table.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import signal
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TABLE = Path(__file__).resolve().parent / "mutations.toml"

#: How the mutated run is invoked. `--tb=no -rfE` because the tracebacks are
#: not the evidence here -- WHICH tests went red is -- and `-q` keeps a run
#: of twenty mutations readable. `-p no:cacheprovider` so a gate run does not
#: leave .pytest_cache pointing at a tree of failures that never existed.
PYTEST_ARGS = ("-q", "--no-header", "--tb=no", "-rfE", "-p", "no:cacheprovider")

#: pytest's exit codes, as far as this cares. Both of these mean "no test
#: ran", and telling them apart from a red test is the whole job: a node id
#: pointing at a file that is gone exits 5, and one naming a test that has
#: been renamed inside a file that still exists exits 4 with `ERROR: not
#: found:` -- measured, because 4 is documented as "usage error" and reads
#: like a problem with this runner's arguments rather than with the table.
NO_TESTS_COLLECTED = 5
USAGE_ERROR = 4

#: Not one of pytest's, and deliberately outside its range: the run never
#: finished, so there is no exit code to report.
TIMED_OUT = -9

_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)")
#: pytest's counts line, which always ends "in 0.42s". Pinned to that shape
#: so the counts come from pytest's own summary and not from a test's output
#: -- this runner's tests capture nested pytest runs, and a line of theirs
#: saying "3 passed" would otherwise be read as the verdict.
_SUMMARY = re.compile(r"\d+ (?:passed|failed|errors?|skipped|deselected"
                      r"|xfailed|xpassed).*\bin \d+\.\d+s")


class Destroyed(Exception):
    """The tree could not be put back. Nothing else matters after this."""


@dataclass(frozen=True)
class Edit:
    path: str           # repo-relative
    find: str
    replace: str
    occurrences: int = 1


@dataclass(frozen=True)
class Mutation:
    """One break, applied as one or more exact edits.

    More than one edit per row is not a convenience. A property can be
    defended twice, and then no single edit can turn the suite red -- the
    first run of this gate found exactly that case (tier2-crlf-not-stripped:
    the CR strip AND the `grep -oE` extraction both keep the trailing \\r out
    of the counts comparison, so removing either alone left all 17 tier-2
    tests green). Writing that as two edits states the redundancy instead of
    hiding it behind a dropped row.
    """
    name: str
    tier: str
    guard: str          # WHY the guard exists -- what it is there to catch
    tests: tuple[str, ...]
    edits: tuple[Edit, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(edit.path for edit in self.edits))


@dataclass(frozen=True)
class Tier:
    name: str
    description: str
    pytest_args: tuple[str, ...] = ()
    #: Wall clock for ONE mutated run. A mutated run can hang rather than
    #: fail: the first hardware-tier run here sat for ten minutes and had to
    #: be killed from outside, which is also how the missing signal handling
    #: below was found. The cause turned out to be contention for /tmp with
    #: another checkout rather than the mutation, but a gate that can only be
    #: ended from outside is a runner burning for six hours with nothing to
    #: report -- the failure ci.yml's own timeouts were written for.
    timeout: float = 300.0


@dataclass
class TestRun:
    returncode: int
    counts: dict
    failed: tuple
    errors: tuple
    output: str
    seconds: float

    @property
    def green(self) -> bool:
        return self.returncode == 0

    @property
    def summary(self) -> str:
        return ", ".join(f"{n} {word}" for word, n in sorted(self.counts.items()))


@dataclass
class Outcome:
    mutation: Mutation
    verdict: str        # caught | SURVIVED | BROKEN
    detail: str = ""
    red: tuple = ()
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict == "caught"


# --- the table ------------------------------------------------------------


def load(table_path: Path = TABLE) -> tuple[dict, list]:
    """Parse and validate mutations.toml.

    Every validation here is a loud failure rather than a dropped row. A
    table that silently ignores a malformed entry is a gate with a hole in
    it that nobody can see from the outside -- which is what this whole
    mechanism exists to stop.
    """
    document = tomllib.loads(table_path.read_text())

    tiers = {}
    for name, body in (document.get("tiers") or {}).items():
        tiers[name] = Tier(name=name,
                           description=body.get("description", ""),
                           pytest_args=tuple(body.get("pytest_args", ())),
                           timeout=float(body.get("timeout", 300.0)))
    if not tiers:
        raise ValueError(f"{table_path} defines no [tiers]")

    mutations = []
    seen = set()
    for index, entry in enumerate(document.get("mutation") or ()):
        where = f"{table_path.name} entry {index}"
        for key in ("name", "tier", "guard", "tests", "edit"):
            if key not in entry:
                raise ValueError(f"{where} has no {key!r}")
        name = entry["name"]
        if name in seen:
            raise ValueError(f"two mutations are called {name!r}")
        seen.add(name)
        if entry["tier"] not in tiers:
            raise ValueError(
                f"{name}: tier {entry['tier']!r} is not one of {sorted(tiers)}")
        if not entry["tests"]:
            raise ValueError(
                f"{name}: no tests named. A mutation with nothing required "
                f"to go red is a row that always passes.")
        edits = []
        for edit in entry["edit"]:
            for key in ("file", "find", "replace"):
                if key not in edit:
                    raise ValueError(f"{name}: an edit has no {key!r}")
            if not edit["find"]:
                raise ValueError(f"{name}: an empty `find` matches everywhere")
            if edit["find"] == edit["replace"]:
                raise ValueError(
                    f"{name}: find and replace are identical, so this edit "
                    f"changes nothing and can never be caught")
            edits.append(Edit(path=edit["file"], find=edit["find"],
                              replace=edit["replace"],
                              occurrences=int(edit.get("occurrences", 1))))
        if not edits:
            raise ValueError(f"{name}: no edits, so nothing is mutated")
        mutations.append(Mutation(
            name=name, tier=entry["tier"], guard=entry["guard"],
            tests=tuple(entry["tests"]), edits=tuple(edits)))
    if not mutations:
        raise ValueError(f"{table_path} defines no mutations")
    return tiers, mutations


def applies(mutation: Mutation, repo: Path = REPO) -> str:
    """"" if the mutation still applies cleanly, else why not.

    Split out from the runner because it costs nothing: the fast suite calls
    it on every row (tests/test_mutation_gate.py), so a table that has rotted
    against a renamed guard is caught by `pytest -q` and not only by the
    gate job.
    """
    for edit in mutation.edits:
        target = repo / edit.path
        if not target.is_file():
            return f"{edit.path} does not exist"
        # Bytes, not text. Reading and rewriting through str translates line
        # endings, so mutating a CRLF file would silently rewrite every line
        # in it as well -- a much bigger edit than the row describes, on a
        # file the tests then judge.
        found = target.read_bytes().count(edit.find.encode())
        if found != edit.occurrences:
            return (f"`find` matches {found} time(s) in {edit.path}, "
                    f"expected {edit.occurrences}: "
                    f"{edit.find.splitlines()[0][:60]!r}...")
    return ""


# --- running the tests ----------------------------------------------------


def parse_pytest(output: str) -> tuple[dict, tuple, tuple]:
    """Counts, FAILED node ids and ERROR node ids, from -q -rfE output."""
    lines = output.splitlines()
    summaries = [line for line in lines if _SUMMARY.search(line)]
    counts = {}
    # The LAST summary line: with -rfE the short summary comes first, and
    # stderr (a warning from an import, say) can land after it.
    for line in summaries[-1:]:
        for number, word in _COUNT.findall(line):
            counts[word.rstrip("s") if word.startswith("error") else word] = int(number)
    failed = tuple(line.split(" ", 1)[1].split(" - ")[0].strip()
                   for line in output.splitlines() if line.startswith("FAILED "))
    errors = tuple(line.split(" ", 1)[1].split(" - ")[0].strip()
                   for line in output.splitlines() if line.startswith("ERROR "))
    return counts, failed, errors


def run_pytest(repo: Path, tier: Tier, tests) -> TestRun:
    started = time.monotonic()
    command = [sys.executable, "-m", "pytest", *PYTEST_ARGS,
               *tier.pytest_args, *tests]
    try:
        proc = subprocess.run(command, cwd=repo, capture_output=True,
                              text=True, timeout=tier.timeout)
    except subprocess.TimeoutExpired as expired:
        return TestRun(returncode=TIMED_OUT, counts={}, failed=(), errors=(),
                       output=(expired.stdout or b"").decode(errors="replace")
                       if isinstance(expired.stdout, bytes)
                       else (expired.stdout or ""),
                       seconds=time.monotonic() - started)
    output = proc.stdout + proc.stderr
    counts, failed, errors = parse_pytest(output)
    return TestRun(returncode=proc.returncode, counts=counts, failed=failed,
                   errors=errors, output=output,
                   seconds=time.monotonic() - started)


# --- the gate -------------------------------------------------------------


@dataclass
class Gate:
    repo: Path = REPO
    tiers: dict = field(default_factory=dict)
    runner: callable = run_pytest
    log: callable = print
    _baselines: dict = field(default_factory=dict)

    def baseline(self, tier: Tier, tests: tuple) -> str:
        """"" if the named tests pass on the unmutated tree, else why not.

        Cached per (tier, tests): most rows in the table share a test file
        with a neighbour, and the baseline is a third of the runtime.

        This is the anti-tautology check. Without it a test file that is
        broken, deselected by a marker, or renamed out from under the table
        would "catch" every mutation aimed at it, and the gate would be
        green for the same reason the guards it audits were green.
        """
        key = (tier.name, tests)
        if key not in self._baselines:
            result = self.runner(self.repo, tier, tests)
            self._baselines[key] = self._judge_baseline(result)
        return self._baselines[key]

    @staticmethod
    def _judge_baseline(result: TestRun) -> str:
        if result.returncode == TIMED_OUT:
            return (f"the named tests did not finish in {result.seconds:.0f}s "
                    f"on the UNMUTATED tree; raise the tier's timeout or fix "
                    f"the tests before believing anything this gate says")
        if result.returncode in (NO_TESTS_COLLECTED, USAGE_ERROR):
            last = [line for line in result.output.splitlines() if line.strip()]
            return (f"pytest collected no tests (rc={result.returncode}) -- "
                    f"the node ids in the table no longer exist, or the "
                    f"tier's pytest args deselect them. pytest said: "
                    f"{last[-1] if last else '(nothing)'}")
        if not result.green:
            return (f"the named tests are ALREADY failing before any "
                    f"mutation ({result.summary}); a red test catches "
                    f"everything, including what it cannot see")
        if result.counts.get("skipped"):
            return (f"{result.counts['skipped']} of the named tests SKIPPED "
                    f"({result.summary}); a skipped test cannot go red, so "
                    f"this tier proves nothing on this machine")
        if not result.counts.get("passed"):
            return f"no test actually passed ({result.summary})"
        return ""

    def apply_and_judge(self, mutation: Mutation) -> Outcome:
        """Mutate, run the named tests, restore. The tree is put back even
        when the test run explodes; failing to put it back raises."""
        problem = applies(mutation, self.repo)
        if problem:
            return Outcome(mutation, "BROKEN", problem)

        tier = self.tiers[mutation.tier]
        problem = self.baseline(tier, mutation.tests)
        if problem:
            return Outcome(mutation, "BROKEN", problem)

        # Every file's pre-mutation bytes, read before anything is written,
        # so the restore puts back what was there rather than what HEAD says
        # was there. That difference is the developer's uncommitted work.
        originals = {edit.path: (self.repo / edit.path).read_bytes()
                     for edit in mutation.edits}
        started = time.monotonic()
        try:
            for edit in mutation.edits:
                target = self.repo / edit.path
                self._write(target, target.read_bytes().replace(
                    edit.find.encode(), edit.replace.encode()))
            result = self.runner(self.repo, tier, mutation.tests)
        finally:
            for path, original in originals.items():
                self._restore(self.repo / path, original)
        elapsed = time.monotonic() - started

        if result.returncode == TIMED_OUT:
            # A hang is not a red test. It might mean the mutation wedged
            # something, which is worth knowing, but it is not evidence that
            # any check noticed -- and it must not be counted as one.
            return Outcome(
                mutation, "BROKEN",
                f"the mutated run did not finish in {tier.timeout:.0f}s. A "
                f"hang is not proof a guard fired; name a narrower test, or "
                f"raise this tier's timeout if the hang IS the detection.",
                seconds=elapsed)
        if result.returncode in (NO_TESTS_COLLECTED, USAGE_ERROR):
            return Outcome(mutation, "BROKEN",
                           f"the mutation made pytest collect nothing "
                           f"(rc={result.returncode})", seconds=elapsed)
        if result.green:
            return Outcome(
                mutation, "SURVIVED",
                f"the guard was broken and the suite stayed green "
                f"({result.summary})", seconds=elapsed)
        if not result.failed:
            # Red for a reason that is not a test noticing: a collection
            # error, or an internal pytest failure. Not proof of anything.
            return Outcome(
                mutation, "BROKEN",
                f"the run went red with no FAILED test (rc={result.returncode}, "
                f"{result.summary}); errors: {', '.join(result.errors) or 'none'}",
                seconds=elapsed)
        return Outcome(mutation, "caught", "", red=result.failed, seconds=elapsed)

    @staticmethod
    def _write(target: Path, data: bytes) -> None:
        """
        Write `data`, and make sure the interpreter will actually read it.

        CPython decides whether a `__pycache__` entry is still good by
        comparing the source's mtime IN WHOLE SECONDS and its size --
        nothing about its contents. A mutation whose `replace` is the same
        length as its `find`, which a swapped pair of lines is, changes
        neither; and a mutated run that finishes inside a second leaves a
        cache entry the RESTORED file matches exactly. Python then goes on
        executing the mutation, in a tree `git diff` calls clean, for every
        run in that checkout until something else touches the file.

        Measured here, restoring
        panel-replacement-runs-before-anything-can-find-it -- which swaps
        two lines and so preserves the length. The row was reported caught,
        `git diff --exit-code` was clean, and the next ordinary pytest run
        in the same checkout failed on the mutated behaviour:

            pyc records mtime 1787071573 size 41074
            src has      mtime 1787071573 size 41074

        It runs the other way too, and that way is worse: a mutation
        written within a second of the original's cache entry can be
        executed AS THE ORIGINAL, and the row then survives for a reason
        that has nothing to do with the guard it is meant to test -- a
        green line in a report, which is the one thing this whole mechanism
        exists to refuse.

        So the cache entry goes with every write, in both directions.
        Deleting it is enough: the next import compiles what is there.
        """
        target.write_bytes(data)
        if target.suffix != ".py":
            return
        try:
            cached = Path(importlib.util.cache_from_source(str(target)))
        except (NotImplementedError, ValueError):    # pragma: no cover
            return                                   # no cache to poison
        try:
            cached.unlink()
        except OSError:
            pass                                     # never written, or gone

    @classmethod
    def _restore(cls, target: Path, original: bytes) -> None:
        cls._write(target, original)
        if target.read_bytes() != original:
            raise Destroyed(
                f"{target} could not be restored to its pre-mutation bytes. "
                f"Recover with the stash snapshot printed at the start of "
                f"this run.")

    def snapshot(self) -> str:
        """A recoverable copy of any uncommitted work, without touching it.

        `git stash create` writes the stash commit and stops there -- no
        worktree change, no stash list entry, nothing to pop. It is empty on
        a clean tree, which is the common case in CI.
        """
        try:
            proc = subprocess.run(["git", "stash", "create", "mutation gate"],
                                  cwd=self.repo, capture_output=True, text=True)
        except OSError as exc:                        # no git on PATH
            return f"(no snapshot: {exc})"
        sha = proc.stdout.strip()
        if proc.returncode != 0:
            return f"(no snapshot: git said {proc.stderr.strip()!r})"
        if not sha:
            return "(clean tree, nothing to snapshot)"
        return sha

    def run(self, mutations) -> list:
        outcomes = []
        for mutation in mutations:
            outcome = self.apply_and_judge(mutation)
            outcomes.append(outcome)
            self.log(self.line(outcome))
        return outcomes

    @staticmethod
    def line(outcome: Outcome) -> str:
        mark = {"caught": "  ok  ", "SURVIVED": " MISS ", "BROKEN": "BROKEN"}
        detail = outcome.detail
        if outcome.ok:
            detail = f"{len(outcome.red)} red: " + ", ".join(
                node.split("::", 1)[-1] for node in outcome.red)
        return (f"[{mark[outcome.verdict]}] {outcome.mutation.name:<44} "
                f"{outcome.seconds:5.1f}s  {detail}")


# --- the command line -----------------------------------------------------


def select(mutations, tiers_wanted, names):
    chosen = [m for m in mutations
              if ("all" in tiers_wanted or m.tier in tiers_wanted)
              and (not names or m.name in names)]
    return chosen


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Break each load-bearing guard and require a red test.")
    parser.add_argument("--tier", action="append", default=[],
                        help="which tier to run; repeatable, or 'all'")
    parser.add_argument("--name", action="append", default=[],
                        help="run only these mutations, by name")
    parser.add_argument("--table", type=Path, default=TABLE)
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--list", action="store_true",
                        help="print the table and exit")
    args = parser.parse_args(argv)

    tiers, mutations = load(args.table)

    if args.list:
        for tier in sorted(tiers.values(), key=lambda t: t.name):
            rows = [m for m in mutations if m.tier == tier.name]
            print(f"\n=== tier {tier.name} ({len(rows)} mutations) "
                  f"-- {tier.description}")
            for mutation in rows:
                print(f"  {mutation.name:<46} {', '.join(mutation.files)}")
        return 0

    if not args.tier:
        parser.error("choose a tier: --tier fast, --tier hardware, --tier all "
                     f"(known tiers: {', '.join(sorted(tiers))})")
    unknown = [t for t in args.tier if t != "all" and t not in tiers]
    if unknown:
        parser.error(f"unknown tier(s) {unknown}; known: {sorted(tiers)}")

    chosen = select(mutations, set(args.tier), set(args.name))
    if not chosen:
        print("no mutations selected -- check --tier/--name", file=sys.stderr)
        return 1

    # A mutated tree left behind is the worst thing this program can do, and
    # it has done it once: killed by SIGTERM at a ten-minute cap, the default
    # disposition terminated the process without unwinding, and
    # device/install.sh stayed mutated in the worktree. `finally` only runs
    # if something unwinds, so the signal has to become an exception.
    def unwind(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, unwind)
        except (OSError, ValueError, AttributeError):    # not on this platform
            pass

    gate = Gate(repo=args.repo.resolve(), tiers=tiers)
    print(f"=== mutation gate: {len(chosen)} mutation(s), "
          f"tier(s) {', '.join(sorted(set(args.tier)))}")
    print(f"    uncommitted work snapshot: {gate.snapshot()}")
    print("    (recover with: git stash apply <sha>)")
    started = time.monotonic()
    try:
        outcomes = gate.run(chosen)
    except KeyboardInterrupt as interrupted:
        # The restore already happened, in apply_and_judge's finally.
        print(f"\ninterrupted ({interrupted}); the tree has been put back",
              file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started

    bad = [o for o in outcomes if not o.ok]
    print(f"\n=== {len(outcomes) - len(bad)}/{len(outcomes)} caught "
          f"in {elapsed:.0f}s")

    # Name what was NOT run, with its size. A gate that covers half the table
    # must not read like one that covers all of it.
    skipped_tiers = {t.name: sum(1 for m in mutations if m.tier == t.name)
                     for t in tiers.values()
                     if "all" not in args.tier and t.name not in args.tier}
    for name, count in sorted(skipped_tiers.items()):
        print(f"    tier {name!r} was NOT run in this invocation "
              f"({count} mutations): {tiers[name].description}")
    if args.name:
        print(f"    --name limited this run to {len(chosen)} of "
              f"{len(mutations)} mutations")

    for outcome in bad:
        print(f"\n{outcome.verdict}: {outcome.mutation.name}", file=sys.stderr)
        print(f"  guard: {outcome.mutation.guard}", file=sys.stderr)
        print(f"  files: {', '.join(outcome.mutation.files)}", file=sys.stderr)
        print(f"  tests: {', '.join(outcome.mutation.tests)}", file=sys.stderr)
        print(f"  {outcome.detail}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Break each load-bearing guard on purpose and require the suite to notice.

`mutations.toml` says, for each guard, what it exists to catch, the edit
that breaks it, and the tests that must go red. This applies each edit,
runs those tests and restores the file (issue #14).

Anything that would turn the gate into a no-op is a failure, not a skip: a
`find` that does not match the declared number of times, named tests that
are not green before the mutation, named tests that do not run, and a tier
that was not selected is named in the summary with its mutation count.

Files are restored from the exact bytes read before mutating, written back
in a `finally` and verified, so uncommitted work survives; `git checkout`
and `git stash push` would both revert it to HEAD. `git stash create` is
taken once at the start as a crash net and does not touch the worktree.

USAGE

    python3 tests/mutation_gate.py --tier fast        # ~20s, per PR
    python3 tests/mutation_gate.py --tier hardware    # needs root + cupsd
    python3 tests/mutation_gate.py --tier all
    python3 tests/mutation_gate.py --list

There is no default tier: the hardware tier needs a real cupsd, so any
default would either fail on most machines or cover less than the table.
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

#: How the mutated run is invoked. Which tests went red is the evidence, not
#: the tracebacks; `-p no:cacheprovider` keeps a gate run from leaving a
#: .pytest_cache full of failures that never existed.
PYTEST_ARGS = ("-q", "--no-header", "--tb=no", "-rfE", "-p", "no:cacheprovider")

#: pytest exit codes that mean "no test ran": 5 for a node id whose file is
#: gone, 4 (`ERROR: not found:`) for a test renamed inside a file that exists.
NO_TESTS_COLLECTED = 5
USAGE_ERROR = 4

#: Not a pytest code: the run never finished.
TIMED_OUT = -9

_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed)")
#: pytest's counts line, which always ends "in 0.42s". Pinned to that shape so
#: a captured nested run's "3 passed" is not read as the verdict.
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

    A property defended twice cannot be turned red by a single edit, so a
    row may carry several edits that remove every defence at once.
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
    #: Wall clock for one mutated run, which can hang rather than fail.
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
    """Parse and validate mutations.toml. A malformed entry is a loud failure,
    never a dropped row."""
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

    Cheap enough that the fast suite calls it on every row
    (tests/test_mutation_gate.py).
    """
    for edit in mutation.edits:
        target = repo / edit.path
        if not target.is_file():
            return f"{edit.path} does not exist"
        # Bytes, not text: rewriting through str translates line endings,
        # which would change every line of a CRLF file.
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
    # The last summary line: with -rfE the short summary comes first, and
    # stderr can land after it.
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

        Cached per (tier, tests). Without this check a test file that is
        broken, deselected or renamed would "catch" every mutation aimed at
        it.
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

        # Pre-mutation bytes, read before anything is written, so the restore
        # keeps the developer's uncommitted work rather than what HEAD has.
        originals = {edit.path: (self.repo / edit.path).read_bytes()
                     for edit in mutation.edits}
        started = time.monotonic()
        shadowed = ""
        try:
            for edit in mutation.edits:
                target = self.repo / edit.path
                self._write(target, target.read_bytes().replace(
                    edit.find.encode(), edit.replace.encode()))
            # Asked before the run: a stale cache entry makes the subprocess
            # execute the original, and afterwards that is indistinguishable
            # from a missing guard. Reported as BROKEN, not SURVIVED, because
            # the row was not measured.
            shadowed = self._shadowed_by_cache(mutation)
            result = (None if shadowed
                      else self.runner(self.repo, tier, mutation.tests))
        finally:
            for path, original in originals.items():
                self._restore(self.repo / path, original)
        elapsed = time.monotonic() - started

        if shadowed:
            return Outcome(mutation, "BROKEN", shadowed, seconds=elapsed)

        if result.returncode == TIMED_OUT:
            # A hang is not a red test and is not evidence that any check
            # noticed.
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
            # Red without a failed test: a collection error or an internal
            # pytest failure, not a check noticing.
            return Outcome(
                mutation, "BROKEN",
                f"the run went red with no FAILED test (rc={result.returncode}, "
                f"{result.summary}); errors: {', '.join(result.errors) or 'none'}",
                seconds=elapsed)
        return Outcome(mutation, "caught", "", red=result.failed, seconds=elapsed)

    def _shadowed_by_cache(self, mutation: Mutation) -> str:
        """Name any mutated file the interpreter might read out of the cache.

        `_write` drops the `__pycache__` entry, but the directory can be
        unwritable or a second process can recompile the file. This checks,
        once the mutation is on disk and before anything runs, that the
        entry is really gone.
        """
        for edit in mutation.edits:
            cached = self._cache_entry(self.repo / edit.path)
            if cached is not None and cached.exists():
                return (f"{edit.path} was mutated with {cached} still in "
                        f"place, and CPython compares only the source's "
                        f"mtime in whole seconds and its size -- so this "
                        f"run may have executed the ORIGINAL and the "
                        f"verdict would mean nothing. Delete it and re-run.")
        return ""

    @staticmethod
    def _cache_entry(target: Path):
        """The `__pycache__` file CPython would believe for `target`."""
        if target.suffix != ".py":
            return None                              # nothing compiles it
        try:
            return Path(importlib.util.cache_from_source(str(target)))
        except (NotImplementedError, ValueError):    # pragma: no cover
            return None                              # no cache to poison

    @classmethod
    def _write(cls, target: Path, data: bytes) -> None:
        """Write `data` and drop the file's `__pycache__` entry.

        CPython validates a cache entry by the source's mtime in whole
        seconds and its size, not its contents. A same-length mutation
        written and restored within a second changes neither, so without
        this the interpreter could keep executing the mutation after the
        restore, or execute the original during the mutated run. Deleting
        the entry is enough: the next import recompiles.
        """
        target.write_bytes(data)
        cached = cls._cache_entry(target)
        if cached is None:
            return
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

        `git stash create` writes the stash commit only: no worktree change
        and no stash list entry. It is empty on a clean tree.
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

    # `finally` only runs if something unwinds, and the default disposition
    # of these signals terminates without unwinding, leaving a mutated tree.
    # So the signal becomes an exception.
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

    # Name what was not run, with its size, so a partial run cannot read as a
    # complete one.
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

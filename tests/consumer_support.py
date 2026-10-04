"""Disposable consumers, tool runners and SIMULATED verdicts for the acceptance tests.

Everything here is test code. The consumer shapes under tests/consumers are tiny stand-ins with no product
source or data. Fixture verdicts are SIMULATED attestations written by the tests: they exercise the verdict
boundary and never stand for independent review of anything.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tests.support import NOW, ROOT, SHARED_REPOSITORY, GitTestCase, out, shared_snapshot, snapshot_source

CONSUMERS = ROOT / "tests" / "consumers"
PIN = "7022c81efb48d8b5eb15498ce6043a3bd74b694c"
PSTACK_TREE = "975600f2f90dc6f755d58cccdccee27f950edcd2"
NATIVE = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[platform.system()]  # to build fixture criteria
PYTHON = Path(sys.executable).stem  # each test puts this interpreter's folder first on PATH
IMPLEMENTATION = {"build-and-run": ([PYTHON, "-I", "-B", "tools/build_and_run.py"], True)}
RESEARCH = {"synthetic-slope": ([PYTHON, "-I", "-B", "analysis/check_slope.py"], False)}


def probe(mode: str, *args: str, required: bool = True) -> tuple[list[str], bool]:
    return [PYTHON, "-I", "-B", "probe.py", mode, *args], required


def selector(repository: str, checks: dict, environments, shared_commit: str, applicability="lifecycle") -> str:
    """A consumer selector whose criteria require every check on every listed OS (labels name their OS)."""
    return json.dumps({
        "format": 1, "repository": repository, "applicability": applicability,
        "shared": {"repository": SHARED_REPOSITORY, "commit": shared_commit},
        "checks": {name: {"argv": argv, "environments": list(environments), "evidence_required": required}
                   for name, (argv, required) in checks.items()},
        "environments": {label: {"os": label} for label in environments}}, indent=2) + "\n"


def shape_files(shape: str) -> dict[str, bytes]:
    folder = CONSUMERS / shape
    return {path.relative_to(folder).as_posix(): path.read_bytes() for path in sorted(folder.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts}


class ConsumerTestCase(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")

    def consumer(self, shape: str, checks: dict, environments=(NATIVE,), repository: str = "example/app",
                 extra: dict | None = None) -> tuple[Path, Path, str]:
        """Bootstrap a disposable lifecycle consumer; returns its target, source checkout and dev (C = B)."""
        source = self.checkout(f"{shape} source")
        files = {**shape_files(shape), "VERSION": "26.1.0\n", "README.md": f"# {shape} fixture\n",
                 "multi-repo-stack.json": selector(repository, checks, environments, shared_snapshot()[1]),
                 **(extra or {})}
        commit = self.commit(source, files, f"Prepare the {shape} fixture")
        target = self.bare(f"{shape} target.git")
        self.bootstrap(target, source, commit, name=f"{shape} bootstrap op")
        return target, source, commit

    def pstack_clone(self, name: str) -> Path:
        """A task-owned clean checkout of the pin (the selected checkout itself is never modified)."""
        clone = self.tmp / name
        out("clone", "--quiet", "--shared", "--no-checkout", "--config", "core.autocrlf=false", self.pstack(), clone)
        out("-C", clone, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", PIN)
        return clone

    def tool_copy(self, name: str) -> Path:
        """A task-owned clean checkout of the shared source under test, at the commit fixtures select."""
        copy = self.tmp / name
        out("clone", "--quiet", "--config", "core.autocrlf=false", snapshot_source(), copy)
        return copy

    def tool(self, *args, checkout: Path | None = None, cwd: Path | None = None) -> tuple[int, dict]:
        """Run the CLI from a clean checkout of the shared source under test (the tool consumers select)."""
        proc = subprocess.run([sys.executable, "-I", "-B", str((checkout or shared_snapshot()[0]) / "mrs"),
                               *map(str, args), "--json"], capture_output=True, cwd=cwd)
        try:
            return proc.returncode, json.loads(proc.stdout.decode("utf-8"))
        except ValueError:
            self.fail(f"no JSON report (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}")

    def collect(self, target: Path, name: str, *, pstack: Path | None = None, checkout: Path | None = None,
                cwd: Path | None = None) -> tuple[int, dict, Path]:
        record = self.tmp / "records" / f"{name}.json"
        record.parent.mkdir(exist_ok=True)
        code, report = self.tool("collect", "--repository", target, "--out", record,
                                 "--pstack", pstack or self.pstack(), checkout=checkout, cwd=cwd)
        return code, report, record

    def write_json(self, name: str, value) -> Path:
        path = self.tmp / "inputs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return path

    def assess(self, target: Path, records, attestation: dict | None = None, reuse: list | None = None,
               name: str = "assessment") -> tuple[int, dict]:
        args = ["assess", "--repository", target, "--pstack", self.pstack()]
        for record in records:
            args += ["--record", record]
        if attestation is not None:
            args += ["--attestation", self.write_json(f"{name} verdict.json", attestation)]
        if reuse is not None:
            args += ["--reuse", self.write_json(f"{name} reuse.json", reuse)]
        return self.tool(*args)

    def verdict(self, record: Path, **changes) -> dict:
        """A SIMULATED PASS attestation bound to the record's candidate (test fixture; no review happened)."""
        facts = json.loads(record.read_text(encoding="utf-8"))
        verdict = {
            "format": "multi-repo-stack/attestation/1", "repository": facts["repository"],
            "candidate": facts["candidate"], "version": facts["version"],
            "criteria": {"identity": facts["criteria"], "previous": None,
                         "assessment": "SIMULATED: first fixture release, compared with the fixture's own intent"},
            "verifier": {"relationship": "non-author",
                         "statement": "SIMULATED fixture attestation written by test code; no review took place",
                         "harness": "SIMULATED: unittest fixture, no agent session"},
            "behavior": "SIMULATED: no behavior was inspected", "evidence": "SIMULATED: the fixture's own record",
            "reuse": [], "limitations": [], "verdict": "PASS"}
        verdict.update(changes)
        return verdict

    def checked_prepare(self, target: Path, name: str, candidate: str, records, attestation: dict | None,
                        reuse: list | None = None, now: datetime = NOW) -> dict:
        """The kernel's checked path, run from the clean shared snapshot like a releaser's selected tool.
        Returns the operation, or {"refused": code, "detail": ...}."""
        code = ("import json, sys; from datetime import datetime; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); from mrs import transactions; a = json.loads(sys.argv[2]); "
                "read = lambda p: Path(p).read_bytes() if p else None\n"
                "try:\n"
                "    print(json.dumps(transactions.prepare_accepted_release(remote=Path(a['target']), "
                "work=Path(a['work']), candidate=a['candidate'], records=[read(p) for p in a['records']], "
                "attestation=read(a['attestation']), proposals=read(a['reuse']), pstack=Path(a['pstack']), "
                "identity=transactions.Identity('Fixture Releaser', 'fixture-releaser@example.invalid'), "
                "now=datetime.fromisoformat(a['now']))))\n"
                "except transactions.Refused as exc:\n"
                "    print(json.dumps({'refused': exc.code, 'detail': exc.detail}))\n")
        payload = {"target": str(target), "work": str(self.tmp / name), "candidate": candidate,
                   "records": [str(record) for record in records], "pstack": str(self.pstack()),
                   "attestation": str(self.write_json(f"{name} verdict.json", attestation)) if attestation else None,
                   "reuse": str(self.write_json(f"{name} reuse.json", reuse)) if reuse is not None else None,
                   "now": now.isoformat()}
        proc = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(shared_snapshot()[0]), json.dumps(payload)],
                              capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return json.loads(proc.stdout.decode("utf-8"))

    def checked(self, call: str, work: Path, now: datetime = NOW) -> str:
        """transactions.apply or reconcile of a checked operation, run by the clean shared snapshot that prepared
        it (an operation is applied only by the tool that prepared it). Returns the outcome's status."""
        code = ("import sys; from datetime import datetime; sys.path.insert(0, sys.argv[1]); "
                "from mrs import transactions; w = sys.argv[3]; "
                "print((transactions.apply(w, now=datetime.fromisoformat(sys.argv[4])) if sys.argv[2] == 'apply' "
                "else transactions.reconcile(w)).status)")
        proc = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(shared_snapshot()[0]), call, str(work),
                               now.isoformat()], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8").strip()

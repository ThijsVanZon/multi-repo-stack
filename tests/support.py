"""Disposable Git fixtures for the Slice 1 tests.

Everything here is test code. Simulated acceptance and the disposable-fixture destination marker
are created only here; the tool has no switch that produces either.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from mrs import git as mrs_git
from mrs import transactions
from mrs.transactions import FIXTURE_MARKER, Identity

ROOT = Path(__file__).resolve().parents[1]
SHARED_REPOSITORY = "ThijsVanZon/multi-repo-stack"
PSTACK_ENV = "MRS_PSTACK_CHECKOUT"
DEV, MAIN = "refs/heads/dev", "refs/heads/main"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
RELEASER = Identity("Fixture Releaser", "fixture-releaser@example.invalid")

# Hostile ambient settings for every test: anything not suppressed would publish extra refs or repositories.
# Every Git process a test starts (helpers, the kernel, CLI and hook children) may use only the file
# transport, set by this test-owned configuration and by the environment setUp itself installs, so an
# adversarial redirection can never reach the network.
AMBIENT_GLOBAL_CONFIG = """[push]
\tfollowTags = true
\trecurseSubmodules = on-demand
[protocol]
\tallow = never
[protocol "file"]
\tallow = always
[init]
\tdefaultBranch = trunk
"""


def remove_tree(path: Path) -> None:
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def simulated_acceptance() -> dict:
    """SIMULATED fixture acceptance. It exercises object storage and ref transactions only."""
    return {
        "criteria": {"identity": "SIMULATED-fixture-criteria", "note": "test fixture, not acceptance criteria"},
        "results": [{"check": "SIMULATED-fixture-check", "environment": "fixture", "outcome": "pass",
                     "simulated": True}],
        "verdict": {"kind": "SIMULATED", "statement": "Synthetic fixture attestation; no independent review."},
    }


def selector(repository: str, applicability: str, shared_commit: str) -> str:
    return json.dumps({"format": 1, "repository": repository, "applicability": applicability,
                       "shared": {"repository": SHARED_REPOSITORY, "commit": shared_commit}}, indent=2) + "\n"


def git(*args, cwd=None, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run([mrs_git.executable(), *map(str, args)], cwd=cwd, input=input, capture_output=True)
    if check and proc.returncode:
        raise AssertionError(f"git {' '.join(map(str, args))} failed ({proc.returncode}): "
                             f"{proc.stderr.decode('utf-8', 'replace')}")
    return proc


def out(*args, **kwargs) -> str:
    return git(*args, **kwargs).stdout.decode("utf-8").strip()


def refs(repo: Path) -> dict[str, str]:
    """Direct ref listing of a local repository, independent of the tool's observation code."""
    text = out("-C", repo, "for-each-ref", "--format=%(refname) %(objectname)")
    return dict(line.split(" ", 1) for line in text.splitlines())


def has_object(repo: Path, oid: str) -> bool:
    return git("-C", repo, "cat-file", "-e", oid, check=False).returncode == 0


def receiver_capabilities(repo: Path) -> list[bytes]:
    """Capabilities receive-pack advertises for `repo`, read from its own advertisement, independent of the tool."""
    first = git("receive-pack", "--advertise-refs", repo).stdout.split(b"\n", 1)[0]
    return first.split(b"\0", 1)[1].split()


def tree_digest(path: Path) -> dict[str, str]:
    """Hash of every file below `path`, including .git internals."""
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def forge_tag(repo: Path, name: str, candidate: str, next_commit: str, next_version: str, *,
              opened: str = "2026-10-03", repository: str = "example/app", header: str | None = None) -> str:
    """Hand-build a contract-shaped release tag with plumbing only (test code, independent of the tool)."""
    receipt = {"acceptance": simulated_acceptance(), "candidate": candidate, "format": "multi-repo-stack/receipt/1",
               "next": {"commit": next_commit, "opened": opened, "version": next_version},
               "repository": repository, "version": name}
    header = header or (f"object {candidate}\ntype commit\ntag {name}\n"
                        "tagger Fixture Forger <fixture-forger@example.invalid> 1790000000 +0000\n")
    raw = (header.encode("utf-8") + b"\nmulti-repo-stack release receipt v1\n"
           + json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n")
    return out("-C", repo, "hash-object", "-t", "tag", "-w", "--literally", "--stdin", input=raw)


def wait_for(marker: Path, timeout: float = 120) -> None:
    """Deterministic barrier: block until a hook announces it reached its point (timeout is only a failsafe)."""
    deadline = time.monotonic() + timeout
    while not marker.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"barrier {marker.name} was never reached")
        time.sleep(0.02)


def start_kernel(code: str, *args: str, cwd: Path | None = None) -> subprocess.Popen:
    """Run kernel code in its own process tree so a test can kill it mid-operation."""
    prelude = "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from mrs import transactions; "
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen([sys.executable, "-I", "-B", "-c", prelude + code, str(ROOT), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
                            start_new_session=os.name != "nt", cwd=cwd)


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run([shutil.which("taskkill"), "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.communicate()


_SNAPSHOT: dict = {}


def _source_files() -> list[str]:
    probe = git("-C", ROOT, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode == 0 and os.path.samefile(probe.stdout.decode("utf-8").strip(), ROOT):
        listing = git("-C", ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout
        return sorted({name for name in listing.decode("utf-8").split("\0") if name and (ROOT / name).is_file()})
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts)


def shared_snapshot() -> tuple[Path, str]:
    """Commit the prepared source's current files in a fresh repository and return a clean checkout and
    its commit: the actual source under test, pinned at runtime rather than embedded in itself."""
    if not _SNAPSHOT:
        base = Path(tempfile.mkdtemp(prefix="mrs shared snapshot ü "))
        if not os.environ.get("MRS_KEEP_FIXTURES"):
            atexit.register(remove_tree, base)
        source = base / "source"
        out("init", "--quiet", source)
        for name in _source_files():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())
        out("-C", source, "-c", "core.autocrlf=false", "add", "-A")
        out("-C", source, "-c", "core.autocrlf=false", "commit", "--quiet", "-m", "Runtime snapshot of the source")
        checkout = base / "shared checkout"
        out("clone", "--quiet", "--config", "core.autocrlf=false", source, checkout)
        _SNAPSHOT.update(source=source, checkout=checkout, commit=out("-C", source, "rev-parse", "HEAD"))
    return _SNAPSHOT["checkout"], _SNAPSHOT["commit"]


def snapshot_source() -> Path:
    shared_snapshot()
    return _SNAPSHOT["source"]


class GitTestCase(unittest.TestCase):
    """Each test gets its own temporary tree and a test-owned global Git configuration."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mrs fixture ü "))
        if not os.environ.get("MRS_KEEP_FIXTURES"):
            self.addCleanup(remove_tree, self.tmp)
        config = self.tmp / "ambient global.gitconfig"
        config.write_text(AMBIENT_GLOBAL_CONFIG, encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=str(config), GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_NAME="Fixture Author", GIT_AUTHOR_EMAIL="fixture-author@example.invalid",
                   GIT_COMMITTER_NAME="Fixture Author", GIT_COMMITTER_EMAIL="fixture-author@example.invalid")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._dev_clones: dict[Path, Path] = {}

    # --- repositories ---------------------------------------------------------------------------

    def bare(self, name: str, fixture: bool = True) -> Path:
        path = self.tmp / name
        out("init", "--quiet", "--bare", "--template=", path)
        if fixture:
            (path / FIXTURE_MARKER).write_text("task-owned disposable test fixture\n", encoding="utf-8")
        return path

    def checkout(self, name: str) -> Path:
        path = self.tmp / name
        out("init", "--quiet", path)
        return path

    def commit(self, repo: Path, files: dict[str, str | bytes | None], message: str) -> str:
        for name, content in files.items():
            target = repo / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        out("-C", repo, "add", "-A")
        out("-C", repo, "commit", "--quiet", "-m", message)
        return out("-C", repo, "rev-parse", "HEAD")

    def lifecycle_source(self, name: str = "app source", version: str = "26.1.0",
                         repository: str = "example/app", files: dict | None = None) -> tuple[Path, str]:
        repo = self.checkout(name)
        content = {"VERSION": f"{version}\n", "multi-repo-stack.json": selector(repository, "lifecycle",
                                                                                 shared_snapshot()[1]),
                   "README.md": "# Example app\n", "src/app.py": "print('example')\n"}
        content.update(files or {})
        return repo, self.commit(repo, content, "Prepare example app")

    def hook(self, repo: Path, name: str, body: str) -> None:
        hooks = repo / "hooks"
        hooks.mkdir(exist_ok=True)
        path = hooks / name
        path.write_bytes(("#!/bin/sh\n" + body).encode("utf-8"))
        path.chmod(0o755)

    def receiver_config(self, key: str, value: str) -> None:
        """Configure fixture receivers through the test-owned global file. Git on macOS reads configuration while
        parsing receive-pack's non-ASCII repository path (every fixture path is one), before it enters that
        repository, and keeps that cache: settings in the target's own config file are never seen there."""
        out("config", "--global", key, value)

    # --- lifecycle steps through the kernel -----------------------------------------------------

    def bootstrap(self, target: Path, source: Path, commit: str, name: str = "bootstrap op") -> Path:
        work = self.tmp / name
        transactions.prepare_bootstrap(source=source, commit=commit, remote=target, work=work)
        outcome = transactions.apply(work)
        self.assertEqual(outcome.status, "APPLIED", outcome)
        return work

    def bootstrapped(self, version: str = "26.1.0", files: dict | None = None) -> tuple[Path, Path, str]:
        target = self.bare("target.git")
        source, commit = self.lifecycle_source(version=version, files=files)
        self.bootstrap(target, source, commit)
        return target, source, commit

    def integrate(self, target: Path, files: dict, message: str) -> str:
        """Ordinary task integration onto dev with plain Git, as another contributor would."""
        clone = self._dev_clones.get(target)
        if clone is None:
            clone = self.tmp / f"dev clone {len(self._dev_clones)}"
            out("clone", "--quiet", "--branch", "dev", target, clone)
            self._dev_clones[target] = clone
        else:
            out("-C", clone, "pull", "--quiet", "--ff-only", "origin", "dev")
        commit = self.commit(clone, files, message)
        out("-C", clone, "push", "--quiet", "origin", "HEAD:refs/heads/dev")
        return commit

    def prepare(self, target: Path, name: str = "release op", now: datetime = NOW,
                candidate: str | None = None) -> tuple[Path, dict]:
        work = self.tmp / name
        operation = transactions.prepare_release(
            remote=target, work=work, candidate=candidate or refs(target)[DEV],
            acceptance=simulated_acceptance(), identity=RELEASER, now=now)
        return work, operation

    def release(self, target: Path, name: str = "release op", now: datetime = NOW) -> tuple[Path, dict]:
        work, operation = self.prepare(target, name, now)
        outcome = transactions.apply(work, now=now)
        self.assertEqual(outcome.status, "APPLIED", outcome)
        return work, operation

    def pstack(self) -> Path:
        value = os.environ.get(PSTACK_ENV)
        if not value or not Path(value).is_dir():
            self.fail(f"set {PSTACK_ENV} to a clean checkout of the pinned cursor/plugins commit; "
                      "the selection gate cannot be skipped")
        return Path(value)

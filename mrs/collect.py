"""Exact-C check collection. A record is check evidence about one commit, never acceptance.

The candidate C is the observed dev of a lifecycle repository, N is C's VERSION, and the checks are the
criteria C itself declares. Each (check, environment) pair whose environment requires this native OS runs
as a literal argv without a shell, in its own fresh checkout of exactly C, with a fresh runner-owned
evidence path outside that checkout. Tracked bytes, modes and links are verified before and after every
command, and no command runs while a tracked link resolves outside its checkout; the selected shared and
pstack sources are verified before and after the whole run. Records hold no local paths.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, git, sources, state
from .acceptance import MAX_EVIDENCE_BYTES, RECORD_FORMAT

EVIDENCE_ENV = "MRS_EVIDENCE_FILE"
_EVIDENCE_NAME = "evidence.txt"
# Highest-precedence attributes in each isolated checkout: no line-ending, filter, ident or encoding
# conversion stands between C's blobs and the files the checks read.
_EXACT = "* -text -filter -ident -working-tree-encoding\n"


class Refused(Exception):
    pass


def remove_tree(path) -> None:
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    shutil.rmtree(path, onerror=retry) if sys.version_info < (3, 12) else shutil.rmtree(path, onexc=retry)


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def runner() -> dict:
    """The actual execution OS (WSL is linux, and is marked) and the collector's own runtime."""
    system = platform.system()
    native = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(system, system.lower())
    return {"os": native, "wsl": native == "linux" and "microsoft" in platform.release().lower(),
            "platform": platform.platform(), "python": platform.python_version(), "git": git.version()}


def collect(repository: str, pstack: Path | None) -> tuple[dict, dict[str, bytes], bool]:
    """Run the applicable checks of the observed dev's exact commit. Returns the public-safe record, each
    pair's exact combined output (private: it may name local paths) and whether a source change during the
    run invalidated every result."""
    scratch = Path(tempfile.mkdtemp(prefix="mrs-collect-"))
    try:
        return _collect(scratch, repository, pstack)
    except (config.ConfigError, sources.SourceError, state.Redirected) as exc:
        raise Refused(str(exc)) from None
    finally:
        try:
            remove_tree(scratch)
        except OSError as exc:
            print(f"warning: collection scratch was not removed: {exc}", file=sys.stderr)


def _collect(scratch: Path, repository: str, pstack: Path | None) -> tuple[dict, dict[str, bytes], bool]:
    store = scratch / "store.git"
    git.check(["init", "--quiet", "--bare", "--template=", str(store)])
    observed = state.observe(store, repository)  # dev, its selector and VERSION, read from fetched objects
    at = _utc()
    candidate = observed.dev
    if candidate is None:
        raise Refused(f"{repository} has no dev, so there is no integrated candidate to check")
    if observed.applicability != "lifecycle" or observed.active is None:
        raise Refused(f"dev {candidate} is not a lifecycle candidate: "
                      + ("; ".join(observed.problems) or f"applicability {observed.applicability!r}"))
    selection = config.at_commit(store, candidate)
    if selection.checks is None:
        raise Refused(f"{candidate} declares no lifecycle checks in {config.FILENAME}")
    shared, pinned = sources.selected(selection, candidate, pstack)
    host = runner()
    pairs = [(name, label) for name, check in sorted(selection.checks.items()) for label in check.environments]
    applicable = [pair for pair in pairs if selection.environments[pair[1]] == host["os"]]
    if not applicable:
        raise Refused(f"no declared check environment requires {host['os']}, the OS running this collection")
    entries = _entries(store, candidate)
    print(f"collecting {candidate} ({observed.active}) with shared {shared['commit']} and pstack {pinned['commit']} "
          f"tree {pinned['tree']}: {len(applicable)} pair(s) on {host['os']}", file=sys.stderr)
    results, outputs = [], {}
    for index, (name, label) in enumerate(applicable):
        result, output = _run(scratch / str(index), store, candidate, entries, name, label, selection.checks[name])
        results.append(result)
        outputs[f"{name}.{label}"] = output
    try:
        changed = None if sources.selected(selection, candidate, pstack) == (shared, pinned) else "identities differ"
    except (config.ConfigError, sources.SourceError) as exc:
        changed = str(exc)
    if changed:
        for result in results:
            result.update(outcome="invalid", reason=f"the selected shared or pstack source changed during the run: "
                                                    f"{changed}")
    return {
        "format": RECORD_FORMAT, "repository": selection.repository, "candidate": candidate,
        "version": str(observed.active), "criteria": selection.criteria, "observed": {"ref": state.DEV, "at": at},
        "shared": shared, "pstack": pinned, "runner": host,
        "checkout": {"bytes": "exact", "executable_bits": "not represented" if os.name == "nt" else "verified"},
        "results": results,
        "unmet": [{"check": name, "environment": label, "os": selection.environments[label]}
                  for name, label in pairs if (name, label) not in applicable],
    }, outputs, bool(changed)


def _entries(store: Path, commit: str) -> list[tuple[str, str, str]]:
    """C's tracked entries, refusing those an isolated checkout here cannot reproduce exactly."""
    entries = []
    for item in git.check(["-C", str(store), "ls-tree", "-r", "-z", "--full-tree", commit]).stdout.split(b"\0"):
        if not item:
            continue
        meta, raw = item.split(b"\t", 1)
        mode, _, oid = meta.decode("ascii").split(" ")
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused(f"tracked path {raw!r} is not UTF-8") from None
        if mode == "160000":
            raise Refused(f"{name} is a submodule; an isolated checkout of C does not contain its content")
        if mode == "120000" and os.name == "nt":
            raise Refused(f"{name} is a symbolic link, which a checkout on this platform does not reproduce exactly")
        if mode not in ("100644", "100755", "120000") or "\n" in name:
            raise Refused(f"tracked entry {name!r} (mode {mode}) is unsupported")
        entries.append((name, mode, oid))
    return entries


def _mismatches(checkout: Path, store: Path, entries) -> list[str]:
    """Tracked paths whose bytes, executable bit (where the filesystem has one) or link target differ from C."""
    problems, regular = [], []
    for name, mode, oid in entries:
        path = checkout / name
        try:
            info = os.lstat(path)
        except OSError:
            problems.append(f"{name}: missing")
            continue
        if mode == "120000":
            target = git.check(["-C", str(store), "cat-file", "blob", oid]).stdout
            if not stat.S_ISLNK(info.st_mode) or os.fsencode(os.readlink(path)) != target:
                problems.append(f"{name}: link differs")
        elif not stat.S_ISREG(info.st_mode):
            problems.append(f"{name}: not a regular file")
        else:
            regular.append((name, oid))
            if os.name != "nt" and bool(info.st_mode & stat.S_IXUSR) != (mode == "100755"):
                problems.append(f"{name}: executable bit differs")
    if regular:
        hashed = git.check(["-C", str(checkout), "hash-object", "--no-filters", "--stdin-paths"],
                           input="".join(f"{name}\n" for name, _ in regular).encode("utf-8")).out.split("\n")
        problems += [f"{name}: content differs" for (name, oid), got in zip(regular, hashed) if got != oid]
    return problems


def _escaping_links(checkout: Path, entries) -> list[str]:
    """Tracked links that resolve, through any chain of links, outside the isolated checkout: a check writing
    through one would change files elsewhere, such as the caller's work, instead of disposable outputs.
    Resolution is the filesystem's own. Windows checkouts have no links (_entries refuses them)."""
    root = os.path.realpath(checkout)
    escaping = []
    for name, mode, _ in entries:
        if mode == "120000":
            resolved = os.path.realpath(checkout / name)
            if resolved != root and not resolved.startswith(root + os.sep):
                escaping.append(f"{name} -> {os.readlink(checkout / name)}")
    return escaping


def _materialize(checkout: Path, store: Path, commit: str) -> None:
    git.check(["init", "--quiet", "--template=", str(checkout)])
    (checkout / ".git" / "info").mkdir()
    (checkout / ".git" / "info" / "attributes").write_text(_EXACT, encoding="ascii")
    git.check(["-C", str(checkout), "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--", str(store),
               f"{commit}:refs/mrs/candidate"], env=git.local_only(store))
    git.check(["-C", str(checkout), "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", commit])


def resolve(command: str) -> str:
    """The native executable that `command` names on PATH, never searched in the current directory.
    Windows batch launchers are refused: they always run through cmd.exe."""
    extensions = [""]
    if os.name == "nt":
        known = [ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower().split(";") if ext]
        extensions = [""] if os.path.splitext(command)[1].lower() in known else known
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        for ext in extensions:
            path = os.path.join(folder, command + ext)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                suffix = os.path.splitext(path)[1].lower()
                if os.name == "nt" and suffix in (".bat", ".cmd"):
                    raise Refused(f"{command!r} resolves to the batch launcher {os.path.basename(path)}, which "
                                  "runs through cmd.exe; batch launchers are unsupported")
                if os.name == "nt" and suffix not in (".exe", ".com"):
                    raise Refused(f"{command!r} resolves to {os.path.basename(path)}, not a native executable")
                return os.path.abspath(path)
    raise Refused(f"prerequisite missing: {command!r} is not found on PATH")


def _run(folder: Path, store: Path, candidate: str, entries, name: str, label: str,
         check: config.Check) -> tuple[dict, bytes]:
    checkout, evidence = folder / "checkout", folder / "evidence"
    evidence.mkdir(parents=True)
    owned = os.path.realpath(evidence)  # where the evidence must still resolve after the command
    try:
        _materialize(checkout, store, candidate)
    except git.GitError as exc:
        raise Refused(f"{candidate} cannot be checked out here: {exc}") from None
    before = _mismatches(checkout, store, entries)
    if before:
        raise Refused(f"a checkout of {candidate} here is not exact: {before[:5]}")
    escaping = _escaping_links(checkout, entries)
    if escaping:
        raise Refused(f"{candidate} has tracked links that resolve outside its isolated checkout: {escaping[:5]}; "
                      "only relative links inside C are supported, so no check ran")
    result = {"check": name, "environment": label, "argv": list(check.argv), "executable": None,
              "started": None, "finished": None, "exit": None, "output": None, "evidence": None,
              "outcome": "invalid", "reason": None}
    env = {key: value for key, value in os.environ.items() if key not in git.SCRUBBED_ENV}
    env[EVIDENCE_ENV] = str(evidence / _EVIDENCE_NAME)
    try:
        executable = resolve(check.argv[0])
        with open(executable, "rb") as handle:
            result["executable"] = {"name": os.path.basename(executable),
                                    "sha256": hashlib.file_digest(handle, "sha256").hexdigest()}
        result["started"] = _utc()
        proc = subprocess.run(list(check.argv), executable=executable, cwd=checkout, env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except (Refused, OSError) as exc:  # a missing or unsupported prerequisite: recorded, not run
        result["reason"] = str(exc)
        return result, b""
    result.update(finished=_utc(), exit=proc.returncode,
                  output={"bytes": len(proc.stdout), "sha256": hashlib.sha256(proc.stdout).hexdigest()})
    result["evidence"], problem = _evidence(evidence, owned, check.evidence_required)
    changed = _mismatches(checkout, store, entries)
    if changed:
        result["reason"] = f"the check changed tracked source of C: {changed[:5]}"
    elif problem:
        result["reason"] = problem
    elif proc.returncode:
        result.update(outcome="fail", reason=f"exit status {proc.returncode}")
    else:
        result["outcome"] = "pass"
    return result, proc.stdout


def _evidence(folder: Path, owned: str, required: bool) -> tuple[dict | None, str | None]:
    """The check's evidence file: a regular, unlinked, runner-owned UTF-8 file of at most 64 KiB at exactly
    the path it was given, retained as exact bytes. Absence is recorded; when required it is a failure.
    Rejected evidence is not retained (None) and the result is invalid."""
    path = folder / _EVIDENCE_NAME
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"present": False}, "required evidence file was not written" if required else None
    if os.path.realpath(path) != os.path.join(owned, _EVIDENCE_NAME):
        return None, "evidence path resolves outside its runner-owned folder"
    if not stat.S_ISREG(info.st_mode):
        return None, "evidence path is not a regular file (a link or another kind of entry)"
    if info.st_nlink != 1:
        return None, "evidence file is hard-linked"
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return None, "evidence file is not owned by the runner"
    with open(path, "rb") as handle:
        data = handle.read(MAX_EVIDENCE_BYTES + 1)
    if len(data) > MAX_EVIDENCE_BYTES:
        return None, f"evidence file exceeds {MAX_EVIDENCE_BYTES} bytes"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "evidence file is not UTF-8"
    return {"present": True, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "text": text}, None

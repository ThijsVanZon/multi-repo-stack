"""Native Git invocation: resolved executable, argv only, never a shell."""

from __future__ import annotations

import contextlib
import functools
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

OID = re.compile(r"[0-9a-f]{40,}")

# Variables that would silently redirect commands to another repository or index, inject the
# command-line configuration of a parent Git process (e.g. when run from a hook), make `git config`
# read other configuration than every other command (GIT_CONFIG), or rewrite ancestry (grafts).
SCRUBBED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH", "GIT_PREFIX", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG", "GIT_GRAFT_FILE",
)


class GitError(Exception):
    pass


def resolve(command: str) -> str:
    """The native executable that `command` names on an absolute PATH entry; the current directory and relative
    entries are never searched. Windows batch launchers are refused: they always run through cmd.exe."""
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
                    raise GitError(f"{command!r} resolves to the batch launcher {os.path.basename(path)}, which "
                                   "runs through cmd.exe; batch launchers are unsupported")
                if os.name == "nt" and suffix not in (".exe", ".com"):
                    raise GitError(f"{command!r} resolves to {os.path.basename(path)}, not a native executable")
                return os.path.abspath(path)
    raise GitError(f"prerequisite missing: {command!r} is not found on PATH")


@functools.lru_cache(maxsize=None)
def executable() -> str:
    return resolve("git")


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8").strip()

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace").strip()


def run(args: list[str], *, cwd: Path | str | None = None, input: bytes | None = None,
        env: dict[str, str] | None = None) -> Result:
    full_env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    # Replace refs would let a commit ID stand for different content; lifecycle facts use real objects.
    full_env.update({"LC_ALL": "C", "LANGUAGE": "C", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
                     "GIT_NO_REPLACE_OBJECTS": "1"})
    if env:
        full_env.update(env)
    proc = subprocess.run([executable(), *args], cwd=cwd, input=input, capture_output=True, env=full_env)
    return Result(proc.returncode, proc.stdout, proc.stderr)


def remove_tree(path) -> None:
    def retry(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    shutil.rmtree(path, onerror=retry) if sys.version_info < (3, 12) else shutil.rmtree(path, onexc=retry)


@contextlib.contextmanager
def scratch_store(prefix: str):
    """A new private bare repository in a new temporary folder, removed afterwards; a removal failure is only
    reported, so it never replaces the result."""
    scratch = tempfile.mkdtemp(prefix=prefix)
    try:
        store = Path(scratch) / "store.git"
        check(["init", "--quiet", "--bare", "--template=", str(store)])
        yield store
    finally:
        try:
            remove_tree(scratch)
        except OSError as exc:
            print(f"warning: scratch {scratch} was not removed: {exc}", file=sys.stderr)


def scheme(location: str | Path) -> str | None:
    """The URL scheme of a repository location, or None for a local path (a drive letter is not a scheme)."""
    found = urllib.parse.urlsplit(str(location)).scheme
    return found.lower() if len(found) > 1 else None


def transport_only(location: str | Path) -> dict[str, str]:
    """Environment for a transport to `location`: Git itself, and every Git process it starts, may then use only
    that location's own transport (file for a local path), and never one the caller's environment already
    forbids, so a rewritten location fails before any connection."""
    own = scheme(location) or "file"
    ambient = os.environ.get("GIT_ALLOW_PROTOCOL")
    return {"GIT_ALLOW_PROTOCOL": own if ambient is None or own in ambient.split(":") else "none"}


def check(args: list[str], **kwargs) -> Result:
    result = run(args, **kwargs)
    if not result.ok:
        raise GitError(f"git {' '.join(args[:3])} failed (exit {result.returncode}): {result.err}")
    return result


def version() -> str:
    return check(["--version"]).out


def object_type(repo: Path, oid: str) -> str | None:
    result = run(["-C", str(repo), "cat-file", "-t", oid])
    return result.out if result.ok else None


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = run(["-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant])
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise GitError(f"ancestry of {ancestor} and {descendant} is unknown: {result.err}")


def ls_tree(repo: Path, commit: str, *paths: str, recursive: bool = False) -> list[tuple[str, str, str, bytes]]:
    """(mode, type, oid, name) of each entry at commit, root-relative; names stay the bytes Git stores."""
    args = ["-C", str(repo), "ls-tree", "-z", "--full-tree", *(["-r"] if recursive else []), commit, "--", *paths]
    entries = []
    for record in filter(None, check(args).stdout.split(b"\0")):
        meta, name = record.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split(" ")
        entries.append((mode, kind, oid, name))
    return entries


def tree_entry(repo: Path, commit: str, path: str) -> tuple[str, str, str] | None:
    """(mode, type, oid) of one root-relative path at commit, or None when absent."""
    for mode, kind, oid, name in ls_tree(repo, commit, path):
        if name.decode("utf-8") == path:
            return mode, kind, oid
    return None


def read_file(repo: Path, commit: str, path: str) -> bytes | None:
    """Exact committed bytes of a regular file (no filters); None when absent."""
    entry = tree_entry(repo, commit, path)
    if entry is None:
        return None
    mode, kind, oid = entry
    if kind != "blob" or mode not in ("100644", "100755"):
        raise GitError(f"{path} at {commit} is not a regular file (mode {mode})")
    return check(["-C", str(repo), "cat-file", "blob", oid]).stdout

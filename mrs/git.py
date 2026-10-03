"""Native Git invocation: resolved executable, argv only, never a shell."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Variables that would silently redirect commands to another repository or index.
_REPOSITORY_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
    "GIT_QUARANTINE_PATH", "GIT_PREFIX",
)


class GitError(Exception):
    pass


@functools.lru_cache(maxsize=None)
def executable() -> str:
    path = shutil.which("git")
    if path is None:
        raise GitError("git executable not found on PATH")
    if Path(path).suffix.lower() in (".bat", ".cmd"):
        raise GitError(f"unsupported batch launcher for git: {path}")
    return os.path.abspath(path)


@dataclass(frozen=True)
class Result:
    args: tuple[str, ...]
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
    full_env = {k: v for k, v in os.environ.items() if k not in _REPOSITORY_ENV}
    full_env.update({"LC_ALL": "C", "LANGUAGE": "C", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    if env:
        full_env.update(env)
    proc = subprocess.run([executable(), *args], cwd=cwd, input=input, capture_output=True, env=full_env)
    return Result(tuple(args), proc.returncode, proc.stdout, proc.stderr)


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


def tree_entry(repo: Path, commit: str, path: str) -> tuple[str, str, str] | None:
    """(mode, type, oid) of one root-relative path at commit, or None when absent."""
    out = check(["-C", str(repo), "ls-tree", "-z", "--full-tree", commit, "--", path]).stdout
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        if name.decode("utf-8") == path:
            mode, kind, oid = meta.decode("ascii").split(" ")
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

"""Verification of the selected shared and pstack checkouts, and applicability routing."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from . import config, git

TOOL_ROOT = Path(__file__).resolve().parents[1]
CONTEXT_DOCS = ("docs/context.md",)
LIFECYCLE_DOCS = ("docs/lifecycle.md",)


class SourceError(Exception):
    pass


def _is_checkout_root(path: Path) -> bool:
    top = git.run(["-C", str(path), "rev-parse", "--show-toplevel"])
    return top.ok and os.path.samefile(top.out, path)


def verify_checkout(path: Path, commit: str, subtree: str | None = None, what: str = "checkout") -> str:
    """Return the verified tree ID after proving `path` is a clean checkout of exactly `commit`."""
    if not path.is_dir():
        raise SourceError(f"{what} {path} is missing")
    if not _is_checkout_root(path):
        raise SourceError(f"{what} {path} is not the root of a Git checkout; an installed or copied tree "
                          "cannot stand in for the pinned revision")
    head = git.run(["-C", str(path), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    if not head.ok or head.out != commit:
        raise SourceError(f"{what} {path} is at {head.out or 'no commit'}, but {commit} is selected")
    spec = ["--", subtree] if subtree else []
    status = git.run(["-C", str(path), "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                      "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all",
                      "--ignored=matching", *spec])
    if not status.ok or status.stdout:
        entries = [entry for entry in status.stdout.decode("utf-8", "replace").split("\0") if entry][:5]
        raise SourceError(f"{what} {path} has modified, untracked or ignored content: {entries or status.err}")
    listing = git.check(["-C", str(path), "ls-tree", "-r", "-z", "--full-tree", commit, *spec]).stdout
    names, expected = [], []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split(" ")
        if kind != "blob" or mode not in ("100644", "100755") or b"\n" in name:
            raise SourceError(f"{what} entry {name.decode('utf-8', 'replace')!r} (mode {mode}) is unsupported")
        names.append(name.decode("utf-8"))
        expected.append(oid)
    if names:
        # Byte-exact: checkout filters (core.autocrlf, clean drivers) must not stand between the pin and the bytes.
        hashed = git.check(["-C", str(path), "hash-object", "--no-filters", "--stdin-paths"],
                           input=("\n".join(names) + "\n").encode("utf-8")).out.split("\n")
        changed = [name for name, want, got in zip(names, expected, hashed) if want != got]
        if changed or len(hashed) != len(names):
            raise SourceError(f"{what} {path} content differs byte-for-byte from {commit}: {changed[:5]} "
                              "(clone with --config core.autocrlf=false if checkout filters converted it)")
    return git.check(["-C", str(path), "rev-parse", f"{commit}:{subtree}" if subtree else f"{commit}^{{tree}}"]).out


def tool_identity() -> dict:
    """The running tool's own checkout identity, recorded in each operation record."""
    if not _is_checkout_root(TOOL_ROOT):
        return {"commit": None, "clean": False}
    head = git.run(["-C", str(TOOL_ROOT), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"]).out or None
    try:
        verify_checkout(TOOL_ROOT, head or "", what="tool checkout")
        clean = True
    except SourceError:
        clean = False
    return {"commit": head, "clean": clean}


def preflight(consumer: Path, pstack: Path | None) -> dict:
    """Resolve the consumer's committed selection and verify every selected source it depends on."""
    consumer = Path(consumer).resolve()
    if not consumer.is_dir() or not _is_checkout_root(consumer):
        raise SourceError(f"consumer {consumer} is not the root of a Git checkout")
    head = git.run(["-C", str(consumer), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    if not head.ok:
        raise SourceError(f"consumer {consumer} has no commit")
    try:
        selection = config.at_commit(consumer, head.out)
    except (config.ConfigError, git.GitError) as exc:
        raise SourceError(str(exc)) from None
    pending = git.run(["-C", str(consumer), "--no-optional-locks", "status", "--porcelain", "--", config.FILENAME])
    if not pending.ok or pending.stdout:
        raise SourceError(f"{config.FILENAME} differs from the committed selection; the selector is ambiguous")
    if selection.shared is None:
        if not os.path.samefile(consumer, TOOL_ROOT):
            raise SourceError("this selector belongs to the shared project itself; run the tool from that checkout")
        # Editing this checkout is ordinary work, but a verified run executes only clean committed source;
        # its commit is recorded in the output (externally), never pinned inside the source itself.
        verify_checkout(TOOL_ROOT, head.out, what="shared checkout (this checkout, running tool)")
        shared = {"repository": selection.repository, "commit": head.out, "self": True, "clean": True}
        shared_selection = selection
    else:
        verify_checkout(TOOL_ROOT, selection.shared.commit, what="shared checkout (running tool)")
        shared_selection = config.at_commit(TOOL_ROOT, selection.shared.commit)
        if shared_selection.pstack is None or shared_selection.repository != selection.shared.repository:
            raise SourceError(f"commit {selection.shared.commit} is not the {selection.shared.repository} "
                              "shared source")
        shared = {"repository": selection.shared.repository, "commit": selection.shared.commit, "self": False,
                  "clean": True}
    pin = shared_selection.pstack
    if pstack is None:
        raise SourceError(f"supply --pstack: a clean checkout of {pin.repository} at {pin.commit}; installed "
                          "or floating copies are not accepted")
    tree = verify_checkout(Path(pstack).resolve(), pin.commit, pin.path, what="pstack checkout")
    docs = CONTEXT_DOCS + (LIFECYCLE_DOCS if selection.applicability == "lifecycle" else ())
    return {
        "consumer": {"repository": selection.repository, "applicability": selection.applicability,
                     "commit": head.out},
        "shared": shared,
        "pstack": {"repository": pin.repository, "commit": pin.commit, "path": pin.path, "tree": tree},
        "load": [str(TOOL_ROOT / doc) for doc in docs],
        "runtime": {"git": git.version(), "git_executable": git.executable(), "python": platform.python_version(),
                    "python_executable": sys.executable, "os": platform.platform()},
    }

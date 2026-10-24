"""Client delivery of the pinned pstack. After preflight, the client's tree is rebuilt from the pin's Git objects
into an immutable, content-addressed cache entry that the client loads by path, next to a generated handshake
plugin. Nothing is installed: no client, consumer or account file is written, because no client offers a
per-repository pinned install, and account- or organization-wide plugins would reach every repository."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from . import git, sources

# Each client's tree at the pin, loaded byte for byte; upstream generates the Claude Code tree from pstack/.
TREES = {"claude-code": "harness/pstack/claude-code", "cursor": None}  # None: the selector's own pstack path
MANIFESTS = {"claude-code": (".claude-plugin/plugin.json", {"name": "mrs-handshake"}),
             "cursor": (".cursor-plugin/plugin.json", {"name": "mrs-handshake", "skills": "./skills/"})}


def default_cache() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "multi-repo-stack"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "multi-repo-stack"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "multi-repo-stack"


def _tree(repo: Path, commit: str, path: str) -> dict[str, tuple[bytes, bool]]:
    """Exact committed bytes and executable bits under `path` at `commit`, read from Git objects only."""
    entries = []
    for mode, kind, oid, name in git.ls_tree(repo, commit, path, recursive=True):
        if kind != "blob" or mode not in ("100644", "100755"):
            raise sources.SourceError(f"pstack entry {name.decode('utf-8', 'replace')!r} (mode {mode}) is unsupported")
        entries.append((name.decode("utf-8")[len(path) + 1:], oid, mode == "100755"))
    if not entries:
        raise sources.SourceError(f"{path} is absent at pstack {commit}")
    batch = git.check(["-C", str(repo), "cat-file", "--batch"],
                      input="".join(f"{oid}\n" for _, oid, _ in entries).encode("ascii")).stdout
    files, at = {}, 0
    for name, oid, executable in entries:
        header_end = batch.index(b"\n", at)
        size = int(batch[at:header_end].split(b" ")[2])
        files[name] = (batch[header_end + 1:header_end + 1 + size], executable)
        at = header_end + 2 + size
    return files


def _digest(files: dict[str, bytes]) -> str:
    return hashlib.sha256("".join(f"{name}\0{hashlib.sha256(files[name]).hexdigest()}\n"
                                  for name in sorted(files)).encode("utf-8")).hexdigest()


def _read(entry: Path) -> dict[str, bytes]:
    files = {}
    for folder, dirs, names in os.walk(entry):
        for name in dirs + names:
            path = Path(folder) / name
            if path.is_symlink():
                raise sources.SourceError(f"cache entry {entry} holds a link: {path}")
            if path.is_file():
                files[path.relative_to(entry).as_posix()] = path.read_bytes()
    return files


def sync(consumer: Path, pstack: Path | None, client: str, cache: Path) -> dict:
    report = sources.preflight(consumer, pstack)
    shared, pin = report["shared"]["commit"], report["pstack"]
    cache = cache.resolve()
    for checkout in (sources.TOOL_ROOT, Path(consumer).resolve(), Path(pstack).resolve()):
        if cache == checkout or checkout in cache.parents:
            raise sources.SourceError(f"the cache {cache} must be outside the tool, consumer and pstack checkouts")
    handshake = f"MRS-HANDSHAKE client={client} multi-repo-stack={shared} pstack={pin['commit']}"
    manifest, fields = MANIFESTS[client]
    files = {f"pstack/{name}": content for name, content in
             _tree(Path(pstack).resolve(), pin["commit"], TREES[client] or pin["path"]).items()}
    skill = ("---\nname: mrs-handshake\ndescription: Report which multi-repo-stack pstack delivery this session "
             "loaded. Use when asked for the mrs handshake.\n---\n\nReply with exactly this line and nothing else:"
             f"\n\n{handshake}\n")
    files[f"mrs-handshake/{manifest}"] = ((json.dumps(fields) + "\n").encode("utf-8"), False)
    files["mrs-handshake/skills/mrs-handshake/SKILL.md"] = (skill.encode("utf-8"), False)
    digest = _digest({name: content for name, (content, _) in files.items()})
    entry, writes = cache / "pstack" / client / digest, []
    if not entry.exists():
        entry.parent.mkdir(parents=True, exist_ok=True)
        partial = Path(tempfile.mkdtemp(prefix=".partial-", dir=entry.parent))
        try:
            for name, (content, executable) in files.items():
                path = partial / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                if executable:
                    path.chmod(0o755)
            try:
                os.rename(partial, entry)  # entries appear complete or not at all, and are never rewritten
                writes.append(str(entry))
            except OSError:
                if not entry.is_dir():  # a concurrent sync may have placed the same entry first
                    raise
        finally:
            if partial.exists():
                git.remove_tree(partial)
    if _digest(_read(entry)) != digest:
        raise sources.SourceError(f"cache entry {entry} differs from its content digest; it was changed after it "
                                  "was written, and is never repaired here")
    return {"status": "STAGED", "client": client, "consumer": report["consumer"], "shared": report["shared"],
            "pstack": pin, "entry": str(entry), "writes": writes, "handshake": handshake,
            "plugin_paths": [str(entry / "pstack"), str(entry / "mrs-handshake")]}

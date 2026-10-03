"""The single tracked selector: identity, applicability and exact source pins."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import git

FILENAME = "multi-repo-stack.json"
APPLICABILITY = ("context", "lifecycle")

_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[a-z0-9]+(?:-[a-z0-9]+)*")
_OID = re.compile(r"[0-9a-f]{40,}")
_SUBTREE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    repository: str
    commit: str
    path: str | None = None


@dataclass(frozen=True)
class Selection:
    repository: str
    applicability: str
    shared: Pin | None  # consumers select the shared revision
    pstack: Pin | None  # only the shared project itself pins pstack


def _reject_duplicates(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ConfigError(f"duplicate keys in {FILENAME}: ambiguous selection")
    return dict(pairs)


def _object(value, keys: set[str], where: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigError(f"{where} must have exactly the keys {sorted(keys)}")
    return value


def _repository(value, where: str) -> str:
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ConfigError(f"{where} must be owner/lowercase-kebab-name, got {value!r}")
    return value


def _commit(value, where: str) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise ConfigError(f"{where} must be a full lowercase commit ID, not a branch or abbreviation: {value!r}")
    return value


def parse(data: bytes) -> Selection:
    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{FILENAME} is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{FILENAME} must be a JSON object")
    base = {"format", "repository", "applicability"}
    if set(raw) not in (base | {"shared"}, base | {"pstack"}):
        raise ConfigError(f"{FILENAME} keys must be {sorted(base)} plus exactly one of 'shared' or 'pstack'")
    if raw["format"] != 1:
        raise ConfigError(f"unsupported {FILENAME} format {raw['format']!r}")
    if raw["applicability"] not in APPLICABILITY:
        raise ConfigError(f"applicability must be one of {APPLICABILITY}, got {raw['applicability']!r}")
    shared = pstack = None
    if "shared" in raw:
        item = _object(raw["shared"], {"repository", "commit"}, "shared")
        shared = Pin(_repository(item["repository"], "shared.repository"), _commit(item["commit"], "shared.commit"))
    else:
        item = _object(raw["pstack"], {"repository", "commit", "path"}, "pstack")
        if not isinstance(item["path"], str) or not _SUBTREE.fullmatch(item["path"]) or ".." in item["path"].split("/"):
            raise ConfigError(f"pstack.path must be a relative subtree path, got {item['path']!r}")
        pstack = Pin(_repository(item["repository"], "pstack.repository"), _commit(item["commit"], "pstack.commit"),
                     item["path"])
    return Selection(_repository(raw["repository"], "repository"), raw["applicability"], shared, pstack)


def at_commit(repo: Path, commit: str) -> Selection:
    """Selection read from Git objects at `commit`, never from a working tree."""
    data = git.read_file(repo, commit, FILENAME)
    if data is None:
        raise ConfigError(f"{FILENAME} is absent at {commit}: not connected to multi-repo-stack")
    return parse(data)

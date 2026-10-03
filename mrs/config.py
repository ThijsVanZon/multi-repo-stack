"""The single tracked selector: identity, applicability, exact source pins and lifecycle check criteria."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import git

FILENAME = "multi-repo-stack.json"
APPLICABILITY = ("context", "lifecycle")
NATIVE_OS = ("windows", "linux", "macos")  # execution OS; WSL is linux

_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[a-z0-9]+(?:-[a-z0-9]+)*")
_OID = re.compile(r"[0-9a-f]{40,}")
_SUBTREE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_LABEL = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pin:
    repository: str
    commit: str
    path: str | None = None


@dataclass(frozen=True)
class Check:
    argv: tuple[str, ...]
    environments: tuple[str, ...]  # every one is a required (check, environment) pair
    evidence_required: bool


@dataclass(frozen=True)
class Selection:
    repository: str
    applicability: str
    shared: Pin | None  # consumers select the shared revision
    pstack: Pin | None  # only the shared project itself pins pstack
    checks: dict[str, Check] | None = None  # lifecycle acceptance criteria, when declared
    environments: dict[str, str] | None = None  # label -> native OS it requires
    criteria: str | None = None  # SHA-256 of the canonical checks and environments: the criteria identity


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
    base, criteria = {"format", "repository", "applicability"}, {"checks", "environments"}
    if set(raw) - criteria not in (base | {"shared"}, base | {"pstack"}) or 0 < len(set(raw) & criteria) < 2:
        raise ConfigError(f"{FILENAME} keys must be {sorted(base)} plus exactly one of 'shared' or 'pstack', "
                          "and lifecycle criteria declare both 'checks' and 'environments'")
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
    selection = Selection(_repository(raw["repository"], "repository"), raw["applicability"], shared, pstack)
    if "checks" not in raw:
        return selection
    if raw["applicability"] != "lifecycle":
        raise ConfigError("checks are lifecycle acceptance criteria; a context selector declares none")
    environments = _labelled(raw["environments"], "environments")
    for label, meaning in environments.items():
        _object(meaning, {"os"}, f"environments.{label}")
        if meaning["os"] not in NATIVE_OS:
            raise ConfigError(f"environments.{label}.os must be one of {NATIVE_OS}, got {meaning['os']!r}")
    checks = {}
    for name, item in _labelled(raw["checks"], "checks").items():
        where = f"checks.{name}"
        _object(item, {"argv", "environments", "evidence_required"}, where)
        argv = item["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
            raise ConfigError(f"{where}.argv must be a non-empty array of strings")
        if not argv[0] or any(sep in argv[0] for sep in "/\\"):
            raise ConfigError(f"{where}.argv[0] must be a command name found on PATH; run scripts through an "
                              f"explicit interpreter, got {argv[0]!r}")
        labels = item["environments"]
        if (not isinstance(labels, list) or not labels
                or not all(isinstance(label, str) and label in environments for label in labels)
                or len(set(labels)) != len(labels)):
            raise ConfigError(f"{where}.environments must be distinct labels declared in environments")
        if not isinstance(item["evidence_required"], bool):
            raise ConfigError(f"{where}.evidence_required must be true or false")
        checks[name] = Check(tuple(argv), tuple(labels), item["evidence_required"])
    unused = set(environments) - {label for check in checks.values() for label in check.environments}
    if unused:
        raise ConfigError(f"environments {sorted(unused)} are declared but required by no check")
    identity = hashlib.sha256(json.dumps({"checks": raw["checks"], "environments": raw["environments"]},
                                         sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return Selection(selection.repository, selection.applicability, shared, pstack, checks,
                     {label: meaning["os"] for label, meaning in environments.items()}, identity)


def _labelled(value, where: str) -> dict:
    if not isinstance(value, dict) or not value or not all(_LABEL.fullmatch(key) for key in value):
        raise ConfigError(f"{where} must be a non-empty object keyed by lowercase-kebab labels")
    return value


def at_commit(repo: Path, commit: str) -> Selection:
    """Selection read from Git objects at `commit`, never from a working tree."""
    data = git.read_file(repo, commit, FILENAME)
    if data is None:
        raise ConfigError(f"{FILENAME} is absent at {commit}: not connected to multi-repo-stack")
    return parse(data)

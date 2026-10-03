"""Read-only observation of a target and recognition of contract release state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import config, git, versions
from .versions import Version

MARKER = b"multi-repo-stack release receipt v1"
RECEIPT_FORMAT = "multi-repo-stack/receipt/1"
RECEIPT_KEYS = {"format", "repository", "version", "candidate", "next", "acceptance"}
MAX_RECEIPT_BYTES = 1 << 20
OPENED_LINE = "Next-line opening date (UTC): "
OBSERVED = "refs/mrs/observed/"
PROBE_REMOTE = "mrs-destination"
DEV, MAIN = "refs/heads/dev", "refs/heads/main"

_OID = re.compile(r"[0-9a-f]{40,}")


class Unknown(Exception):
    """Observation failed or required history is missing. Never equivalent to absence."""


class Redirected(Exception):
    """Git would observe or publish the requested destination somewhere else."""


class NotContract(Exception):
    """A version-shaped tag that is not a contract release tag (foreign)."""


class Malformed(Exception):
    """A tag claiming the receipt format whose bindings do not validate."""


def encode_receipt(receipt: dict) -> bytes:
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def check_destination(store: Path, remote: str) -> None:
    """Prove that Git fetches from and pushes to `remote` itself, before anything observes or publishes it.

    Git resolves the effective URLs from `store` with the configuration and environment its transports
    use there, so every configuration source, longest-match and empty url.<base>.(push)insteadOf prefixes
    are Git's own semantics rather than a re-implementation. A remote whose name is the destination
    replaces it on Windows. Configuration that Git cannot read is UNKNOWN, never "no redirection".
    """
    recorded = git.run(["config", "--file", str(Path(store) / "config"), "--replace-all",
                        f"remote.{PROBE_REMOTE}.url", remote])
    if not recorded.ok:
        raise Unknown(f"cannot record the destination in {store}: {recorded.err}")
    for direction, flags in (("fetch", []), ("push", ["--push"])):
        resolved = git.run(["-C", str(store), "remote", "get-url", *flags, "--all", PROBE_REMOTE])
        if not resolved.ok:
            raise Unknown(f"Git configuration could not be resolved for {remote}: {resolved.err}")
        urls = resolved.stdout.decode("utf-8", "replace").splitlines()
        if urls != [remote]:
            raise Redirected(f"Git configuration resolves the {direction} location of {remote} to {urls}")
    named = git.run(["-C", str(store), "config", "-z", "--name-only", "--get-regexp", r"^remote\."])
    if named.returncode not in (0, 1):  # 1: no remote is configured at all
        raise Unknown(f"Git configuration could not be read: {named.err}")
    for key in named.stdout.decode("utf-8", "replace").split("\0"):
        if key[len("remote."):].rpartition(".")[0] == remote:
            raise Redirected(f"configured remote {key} is named like the destination and would replace it")


def snapshot(store: Path, remote: str) -> dict[str, str]:
    """Complete ref listing of `remote` from one advertisement."""
    check_destination(store, remote)
    if Path(remote).is_dir():
        hidden = git.run(["-C", remote, "config", "--get-regexp", r"^(transfer|uploadpack|receive)\.hiderefs$"])
        if hidden.returncode not in (0, 1):  # 1: none configured
            raise Unknown(f"configuration of {remote} could not be read: {hidden.err}")
        if hidden.ok:
            raise Unknown(f"hidden refs are configured for {remote}; absence cannot be observed")
    result = git.run(["-C", str(store), "ls-remote", "--", remote], env=git.local_only(remote))
    if not result.ok:
        raise Unknown(f"ls-remote failed (exit {result.returncode}): {result.err}")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        raise Unknown("ls-remote output is not UTF-8") from None
    refs: dict[str, str] = {}
    for line in text.splitlines():
        oid, sep, name = line.partition("\t")
        if not sep or not _OID.fullmatch(oid) or not name:
            raise Unknown(f"unparseable ls-remote line: {line!r}")
        if not oid.strip("0"):
            raise Unknown(f"broken ref reported as a null object ID: {name}")
        if name == "HEAD" or name.endswith("^{}"):
            continue
        if not name.startswith("refs/") or name in refs:
            raise Unknown(f"unexpected ls-remote entry: {line!r}")
        refs[name] = oid
    return refs


def fetch(store: Path, remote: str, refs: dict[str, str]) -> None:
    """Bring dev/main/tag objects into `store` and confirm they match the snapshot."""
    try:
        stale = git.check(["-C", str(store), "for-each-ref", "--format=%(refname)", OBSERVED]).out.splitlines()
        if stale:
            git.check(["-C", str(store), "update-ref", "--stdin"],
                      input="".join(f"delete {ref}\n" for ref in stale).encode("utf-8"))
    except git.GitError as exc:
        raise Unknown(f"observation store unusable: {exc}") from None
    wanted = [ref for ref in refs if ref in (DEV, MAIN) or ref.startswith("refs/tags/")]
    if not wanted:
        return
    result = git.run(["-C", str(store), "-c", "gc.auto=0", "-c", "maintenance.auto=false", "fetch", "--quiet",
                      "--no-tags", "--no-write-fetch-head", "--recurse-submodules=no", "--", remote,
                      *[f"+{ref}:{OBSERVED}{ref[5:]}" for ref in wanted]], env=git.local_only(remote))
    if not result.ok:
        raise Unknown(f"fetch failed (exit {result.returncode}): {result.err}")
    for ref in wanted:
        got = git.run(["-C", str(store), "rev-parse", "--verify", "--quiet", OBSERVED + ref[5:]])
        if not got.ok or got.out != refs[ref]:
            raise Unknown(f"{ref} changed between listing and fetch; observe again")


def ancestor(store: Path, older: str, newer: str) -> bool:
    try:
        return git.is_ancestor(store, older, newer)
    except git.GitError as exc:
        raise Unknown(f"missing history: {exc}") from None


def _exact_commit(store: Path, value, what: str, width: int) -> str:
    """A full commit ID naming itself; names, abbreviations, IDs of another object format (`width` is
    the length of IDs in this repository) and other object types are malformed. A well-formed ID whose
    object is missing is UNKNOWN."""
    if not isinstance(value, str) or len(value) != width or not _OID.fullmatch(value):
        raise Malformed(f"{what} must be a full object ID of this repository ({width} hex digits), got {value!r}")
    found = git.object_type(store, value)
    if found is None:
        raise Unknown(f"missing history: {what} {value} is not available")
    resolved = git.run(["-C", str(store), "rev-parse", "--verify", "--quiet", "--end-of-options",
                        f"{value}^{{commit}}"])
    if found != "commit" or resolved.out != value:
        raise Malformed(f"{what} {value} is not exactly a commit")
    return value


def next_line_violation(store: Path, candidate: str, next_commit: str, version: Version,
                        opened: date) -> str | None:
    """Why `next_commit` is not C's direct child changing only VERSION, if it is not."""
    raw = git.check(["-C", str(store), "cat-file", "commit", next_commit]).stdout
    header, _, message = raw.partition(b"\n\n")
    parents = [line[7:].decode("ascii") for line in header.split(b"\n") if line.startswith(b"parent ")]
    if parents != [candidate]:
        return "next-line commit must have exactly one parent, the candidate"
    if f"{OPENED_LINE}{opened.isoformat()}" not in message.decode("utf-8", "replace").split("\n"):
        return f"next-line commit message does not record opening date {opened.isoformat()}"
    diff = git.check(["-C", str(store), "diff-tree", "-r", "-z", "--raw", "--no-renames", candidate,
                      next_commit]).stdout.split(b"\0")
    if len(diff) != 3 or diff[1] != b"VERSION" or diff[2] != b"":
        return "next-line commit must change only VERSION"
    old_mode, new_mode, _, _, status = diff[0].decode("ascii").lstrip(":").split(" ")
    if status != "M" or old_mode != "100644" or new_mode != "100644":
        return "next-line commit must modify VERSION in place as a regular file"
    try:
        found = versions.parse_file(git.read_file(store, next_commit, "VERSION") or b"")
    except versions.VersionError as exc:
        return f"next-line VERSION is invalid: {exc}"
    if found != version:
        return f"next-line VERSION is {found}, expected {version}"
    return None


@dataclass(frozen=True)
class Release:
    version: Version
    tag: str
    candidate: str
    next_commit: str
    next_version: Version
    repository: str


def _strict_json(data: bytes) -> dict:
    def no_duplicates(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise Malformed("duplicate keys in receipt")
        return dict(pairs)
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Malformed(f"receipt is not canonical ASCII JSON: {exc}") from None
    if not isinstance(value, dict) or encode_receipt(value) != data:
        raise Malformed("receipt is not in canonical serialization")
    return value


def _keys(value, keys: set[str], what: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise Malformed(f"{what} must have exactly the keys {sorted(keys)}")
    return value


def contract_release(store: Path, name: str, oid: str) -> Release:
    """Validate a canonical version-named tag as a contract release tag."""
    kind = git.object_type(store, oid)
    if kind is None:
        raise Unknown(f"missing history: tag {name} object {oid} is not available")
    if kind != "tag":
        raise NotContract(f"tag {name} is not an annotated tag")
    read = git.run(["-C", str(store), "cat-file", "tag", oid])
    if not read.ok:
        raise Unknown(f"tag {name} object {oid} cannot be read: {read.err}")
    raw = read.stdout
    head, _, message = raw.partition(b"\n\n")
    if not message.startswith(MARKER + b"\n"):
        raise NotContract(f"annotated tag {name} carries no multi-repo-stack receipt")
    body = message[len(MARKER) + 1:]
    if not body.endswith(b"\n") or b"\n" in body[:-1]:
        raise Malformed(f"tag {name}: receipt must be one canonical JSON line with nothing after it")
    data = body[:-1]
    if len(data) > MAX_RECEIPT_BYTES:
        raise Malformed(f"tag {name}: receipt exceeds {MAX_RECEIPT_BYTES} bytes")
    receipt = _keys(_strict_json(data), RECEIPT_KEYS, f"tag {name}: receipt")
    lines = head.decode("utf-8", "replace").split("\n")
    if [line.split(" ", 1)[0] for line in lines] != ["object", "type", "tag", "tagger"]:
        raise Malformed(f"tag {name}: header must be exactly one object, type, tag and tagger line")
    headers = dict(line.split(" ", 1) for line in lines)
    if receipt["format"] != RECEIPT_FORMAT:
        raise Malformed(f"tag {name}: unknown receipt format {receipt['format']!r}")
    if receipt["version"] != name or headers["tag"] != name:
        raise Malformed(f"tag {name}: tag name, tag header and receipt version disagree")
    try:
        version = versions.parse(name)
        candidate = _exact_commit(store, receipt["candidate"], f"tag {name}: candidate", len(oid))
        peeled = git.run(["-C", str(store), "rev-parse", "--verify", "--quiet", f"{oid}^{{commit}}"]).out
        if headers["object"] != candidate or headers["type"] != "commit" or peeled != candidate:
            raise Malformed(f"tag {name}: must directly target the receipt's candidate commit")
        if versions.parse_file(git.read_file(store, candidate, "VERSION") or b"") != version:
            raise Malformed(f"tag {name}: candidate VERSION is not {name}")
        selection = config.at_commit(store, candidate)
        if selection.applicability != "lifecycle" or selection.repository != receipt["repository"]:
            raise Malformed(f"tag {name}: receipt repository/applicability do not match the candidate's selector")
        _keys(receipt["acceptance"], {"criteria", "results", "verdict"}, f"tag {name}: acceptance")
        nxt = _keys(receipt["next"], {"commit", "version", "opened"}, f"tag {name}: next")
        opened = date.fromisoformat(nxt["opened"])
        next_version = versions.parse(nxt["version"])
        if opened.isoformat() != nxt["opened"] or next_version != versions.next_line(version, opened):
            raise Malformed(f"tag {name}: next line {next_version} does not follow from {name} opened {opened}")
        next_commit = _exact_commit(store, nxt["commit"], f"tag {name}: next-line commit", len(oid))
        problem = next_line_violation(store, candidate, next_commit, next_version, opened)
        if problem:
            raise Malformed(f"tag {name}: {problem}")
    except (versions.VersionError, config.ConfigError, ValueError, TypeError, git.GitError) as exc:
        raise Malformed(f"tag {name}: {exc}") from None
    return Release(version, oid, candidate, next_commit, next_version, receipt["repository"])


@dataclass
class State:
    refs: dict[str, str]
    kind: str  # EMPTY, LIFECYCLE or UNSUPPORTED
    problems: list[str] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    repository: str | None = None
    applicability: str | None = None
    active: Version | None = None
    ignored_tags: list[str] = field(default_factory=list)

    @property
    def dev(self) -> str | None:
        return self.refs.get(DEV)

    @property
    def main(self) -> str | None:
        return self.refs.get(MAIN)


def analyze(store: Path, refs: dict[str, str]) -> State:
    """Apply the core's release-history predicates to fetched objects."""
    if not refs:
        return State(refs, "EMPTY")
    state = State(refs, "UNSUPPORTED")
    problems = state.problems
    dev, main = refs.get(DEV), refs.get(MAIN)
    if dev is None:
        problems.append("dev is absent but the target is not empty")
    else:
        try:
            selection = config.at_commit(store, dev)
            state.repository, state.applicability = selection.repository, selection.applicability
            if selection.applicability != "lifecycle":
                problems.append(f"applicability at dev is {selection.applicability!r}, not 'lifecycle'")
        except (config.ConfigError, git.GitError) as exc:
            problems.append(f"selector at dev: {exc}")
        try:
            state.active = versions.parse_file(git.read_file(store, dev, "VERSION") or b"")
        except (versions.VersionError, git.GitError) as exc:
            problems.append(f"VERSION at dev: {exc}")
    for ref, oid in sorted(refs.items()):
        if not ref.startswith("refs/tags/"):
            continue
        name = ref[len("refs/tags/"):]
        if not versions.is_version_shaped(name):
            state.ignored_tags.append(name)
            continue
        try:
            state.releases.append(contract_release(store, name, oid))
        except NotContract as exc:
            problems.append(f"foreign version-shaped tag blocks promotion: {exc}")
        except Malformed as exc:
            problems.append(f"malformed contract receipt blocks mutation: {exc}")
    state.releases.sort(key=lambda release: release.version)
    previous = None
    for release in state.releases:
        if state.repository is not None and release.repository != state.repository:
            problems.append(f"release {release.version} binds {release.repository}, dev selects {state.repository}")
        if previous is not None:
            if release.version != previous.next_version:
                problems.append(f"release {release.version} does not continue line {previous.next_version}")
            if not ancestor(store, previous.next_commit, release.candidate):
                problems.append(f"release {release.version} does not descend from {previous.version}'s next line")
        previous = release
    if previous is None:
        if main is not None:
            problems.append("main exists without any contract release (e.g. an MVP main)")
    else:
        if main != previous.candidate:
            problems.append(f"main is not the latest contract release {previous.version} ({previous.candidate})")
        if dev is not None and not ancestor(store, previous.next_commit, dev):
            problems.append(f"dev does not descend from {previous.version}'s next-line commit")
        if state.active is not None and state.active != previous.next_version:
            problems.append(f"dev VERSION is {state.active}, expected open line {previous.next_version}")
    if not problems:
        state.kind = "LIFECYCLE"
    return state


def observe(store: Path, remote: str) -> State:
    refs = snapshot(store, remote)
    fetch(store, remote, refs)
    return analyze(store, refs)

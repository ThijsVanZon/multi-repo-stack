"""Guarded dev-only bootstrap and atomic main/tag/dev release: preparation, inspection, explicit application and
read-only reconciliation of one saved operation.

A destination is an absolute path to the root of a local bare repository, or an https, ssh or file URL. Git itself
must resolve it to exactly that location before every observation and push, and every Git process these
functions start may use only the destination's own transport.

A real target is changed only through the checked path: bootstrap first verifies the sources B selects, a release
carries a SUFFICIENT assessment of exact C (prepare_accepted_release), and both run only from the clean shared
checkout that those sources select. A disposable fixture (a local bare repository containing FIXTURE_MARKER, which
only test code creates) also admits the low-level prepare_release, which records an unjudged payload for the
transaction tests, and a tool checkout that is being edited.

Preparation writes only its own operation folder: an operation record and a private store holding the prepared
objects and a binding of kind, destination, tool and the expected values observed at preparation (an absent main
included). Apply, push and reconciliation load a record only when it is exactly what its store prepared and the
running tool is the one that prepared it. Nothing is repaired or regenerated.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import acceptance as acceptance_module
from . import config, git, sources, state, versions
from .state import DEV, MAIN

OPERATION_FORMAT = "multi-repo-stack/operation/1"
FIXTURE_MARKER = "mrs-disposable-fixture"
OPERATION_FILE = "operation.json"
ATTEMPTS_FILE = "attempts.log"
STORE = "repo.git"
BINDING = "refs/mrs/op/binding"
_BOUND = ("kind", "remote", "tool", "expected")
_SCHEMES = ("https", "ssh", "file")


class Refused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


@dataclass(frozen=True)
class Outcome:
    """apply: APPLIED, NOOP, REFUSED, MIXED or UNKNOWN.
    reconcile: COMPLETED, NOT_APPLIED, DIVERGED, MIXED or UNKNOWN."""
    status: str
    reason: str
    detail: str
    refs: dict[str, str] | None = None


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _destination(remote) -> str:
    """The one explicit destination. Relative paths, remote names and scp-like addresses are ambiguous, and a URL
    carrying a password would write a credential into the operation record."""
    remote = str(remote) if isinstance(remote, (str, Path)) else ""
    if not remote or any(char in remote for char in "\0\r\n"):
        raise Refused("destination", f"unusable destination {remote!r}")
    scheme = git.scheme(remote)
    if scheme is not None:
        try:
            parts = urllib.parse.urlsplit(remote)
            usable = (scheme in _SCHEMES and not parts.query and not parts.fragment and parts.password is None
                      and (scheme == "file" or bool(parts.hostname)))
        except ValueError:
            usable = False
        if not usable:
            raise Refused("destination", f"{remote} is not an https, ssh or file URL of one repository, without "
                                         "a password")
    local = state.local_path(remote)
    if local is None:
        return remote
    if not local.is_absolute():
        raise Refused("destination", "a local destination must be an absolute path or a file:// URL")
    probe = git.run(["-C", str(local), "rev-parse", "--is-bare-repository", "--absolute-git-dir"])
    lines = probe.out.splitlines() if probe.ok else []
    if len(lines) != 2 or lines[0] != "true" or not os.path.samefile(lines[1], local):
        raise Refused("destination", f"{remote} is not the root of a bare repository")
    return remote


def is_fixture(destination: str) -> bool:
    """A disposable test fixture: a local bare repository path (not a URL) containing FIXTURE_MARKER."""
    return git.scheme(destination) is None and (Path(destination) / FIXTURE_MARKER).is_file()


def _tool(destination: str) -> dict:
    """The running tool's identity, recorded with an operation. A real target needs a clean committed checkout."""
    identity = sources.tool_identity()
    if not identity["clean"] and not is_fixture(destination):
        raise Refused("tool", f"a real target is changed only from a clean committed checkout of the shared tool; "
                              f"the running tool {sources.TOOL_ROOT} is {identity}")
    return identity


def _resolves_to_itself(store: Path, destination: str) -> None:
    """The single effective destination, checked before every observation and push of an operation
    (inspection applies the same check through state.snapshot). Unreadable configuration is Unknown."""
    try:
        state.check_destination(store, destination)
    except state.Redirected as exc:
        raise Refused("destination", str(exc)) from None


def _new_store(work: Path) -> Path:
    if not work.is_absolute():
        raise Refused("work", "the operation directory must be an absolute path")
    if work.exists() and any(work.iterdir()):
        raise Refused("work", f"{work} is not empty")
    probe = work
    while not probe.exists():
        probe = probe.parent
    if git.run(["-C", str(probe), "rev-parse", "--git-dir"]).ok:
        raise Refused("work", f"{work} is inside a Git repository; keep operation scratch outside user work")
    work.mkdir(parents=True, exist_ok=True)
    store = work / STORE
    git.check(["init", "--quiet", "--bare", "--template=", str(store)])
    return store


def _bound(operation: dict) -> bytes:
    return state.encode_receipt({key: operation[key] for key in _BOUND})


def _write(work: Path, operation: dict) -> None:
    store = work / STORE
    blob = git.check(["-C", str(store), "hash-object", "-w", "--no-filters", "--stdin"], input=_bound(operation)).out
    git.check(["-C", str(store), "update-ref", BINDING, blob, ""])
    with open(work / OPERATION_FILE, "xb") as handle:
        handle.write(json.dumps(operation, indent=2, sort_keys=True).encode("utf-8") + b"\n")


_RECORD_KEYS = {
    "bootstrap": {"format", "kind", "repository", "remote", "version", "expected", "updates", "tool"},
    "release": {"format", "kind", "repository", "remote", "version", "candidate", "tag", "next", "expected",
                "updates", "receipt_sha256", "acceptance_sha256", "prepared_at", "tool"},
}


def load(work: Path) -> dict:
    """The operation record, refused unless it is exactly the operation its store prepared, for the destination,
    by the tool and with the expected values that the store bound, and the running tool is that same tool. Every
    inspection, apply, push and reconciliation loads through here; a record is never repaired."""
    path, store = Path(work) / OPERATION_FILE, Path(work) / STORE
    try:
        operation = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise Refused("operation", f"cannot read the operation record {path}: {exc}") from None
    kind = operation.get("kind") if isinstance(operation, dict) else None
    if not isinstance(kind, str) or operation.get("format") != OPERATION_FORMAT or set(operation) != _RECORD_KEYS.get(kind):
        raise Refused("operation", f"{path} is not a complete {OPERATION_FORMAT} record")
    try:
        facts, updates, leases, judged = _prepared(store, operation)
        bound = git.run(["-C", str(store), "cat-file", "blob", BINDING])
    except (state.NotContract, state.Malformed, state.Unknown, config.ConfigError, versions.VersionError,
            git.GitError, KeyError, TypeError, ValueError) as exc:
        raise Refused("operation", f"{path} does not match the objects prepared in its store: {exc}") from None
    for key, value in facts.items():
        if operation[key] != value:
            raise Refused("operation", f"{path}: {key} is {operation[key]!r}, but the prepared objects give {value!r}")
    if operation["updates"] != updates or operation["expected"] != leases:
        raise Refused("operation", f"{path} must name exactly the updates {updates} with leases {leases}")
    if not bound.ok or bound.stdout != _bound(operation):
        raise Refused("operation", f"{path} does not name the kind, destination, tool and expected values that its "
                                   "store bound at preparation")
    destination = _destination(operation["remote"])
    running = sources.tool_identity()
    if running != operation["tool"]:
        raise Refused("tool", f"the operation was prepared by the tool at {operation['tool']}, but the running tool "
                              f"is {running}; inspect, apply and reconcile it with the tool that prepared it")
    if not is_fixture(destination):
        _tool(destination)
        if kind == "release" and judged.get("format") != acceptance_module.ACCEPTANCE_FORMAT:
            raise Refused("acceptance", f"{path}: a real target is released only with an assessed acceptance")
    return operation


def _prepared(store: Path, operation: dict) -> tuple[dict, dict, dict, dict]:
    """The record facts, ref updates and leases that the objects prepared in `store` allow, and the acceptance
    the receipt carries: bootstrap creates only dev at B; a release moves main P->C, creates tag N at T and
    moves dev C->D."""
    if operation["kind"] == "bootstrap":
        commit = _prepared_object(store, "refs/mrs/op/bootstrap^{commit}")
        facts = {"repository": config.at_commit(store, commit).repository,
                 "version": str(versions.parse_file(git.read_file(store, commit, "VERSION") or b""))}
        return facts, {DEV: commit}, {DEV: None}, {}
    tag = _prepared_object(store, "refs/mrs/op/tag^{tag}")
    release = state.contract_release(store, operation["version"], tag)  # the existing C/T/D invariants
    receipt = json.loads(release.receipt)
    facts = {key: receipt[key] for key in ("repository", "version", "candidate", "next")}
    facts.update(tag=tag, receipt_sha256=hashlib.sha256(release.receipt).hexdigest(),
                 acceptance_sha256=hashlib.sha256(state.encode_receipt(receipt["acceptance"])).hexdigest())
    previous = operation["expected"].get(MAIN) if isinstance(operation["expected"], dict) else None
    if previous is not None and not state.ancestor(
            store, state.exact_commit(store, previous, "expected main", len(tag)), release.candidate):
        raise Refused("operation", f"expected main {previous} is not an ancestor of the candidate")
    tag_ref = f"refs/tags/{release.version}"
    return (facts, {MAIN: release.candidate, tag_ref: tag, DEV: release.next_commit},
            {MAIN: previous, tag_ref: None, DEV: release.candidate}, receipt["acceptance"])


def _prepared_object(store: Path, rev: str) -> str:
    found = git.run(["-C", str(store), "rev-parse", "--verify", "--quiet", rev])
    if not found.ok:
        raise Refused("operation", f"the operation store {store} holds no prepared {rev}")
    return found.out


def _identity_env(identity: Identity, when: datetime) -> dict[str, str]:
    for value in (identity.name, identity.email):
        if not value or any(char in value for char in "<>\n\0"):
            raise Refused("identity", f"unusable identity value {value!r}")
    stamp = f"{int(when.timestamp())} +0000"
    return {"GIT_AUTHOR_NAME": identity.name, "GIT_AUTHOR_EMAIL": identity.email, "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": identity.name, "GIT_COMMITTER_EMAIL": identity.email, "GIT_COMMITTER_DATE": stamp}


# --- preparation -----------------------------------------------------------------------------

def prepare_bootstrap(*, source: Path, commit: str, remote: str | Path, work: Path,
                      pstack: Path | None = None) -> dict:
    """Prepare creation of dev at exact `commit` (B) from a local source repository. For a real target the
    running tool and `pstack` must be the clean sources B selects."""
    destination = _destination(remote)
    if git.scheme(source) is not None or not Path(source).is_absolute():
        raise Refused("source", f"the source of B must be an absolute path to a local repository, not {source}")
    if not is_fixture(destination):  # before any scratch: the running tool and pstack are the sources B selects
        found = git.run(["-C", str(source), "rev-parse", "--verify", "--quiet", "--end-of-options",
                         f"{commit}^{{commit}}"])
        if not found.ok or found.out != commit:
            raise Refused("source", f"exact commit {commit} is not available from {source}")
        try:
            sources.selected(config.at_commit(Path(source), commit), commit, pstack)
        except (config.ConfigError, git.GitError) as exc:
            raise Refused("candidate", str(exc)) from None
        except sources.SourceError as exc:
            raise Refused("source", str(exc)) from None
        _tool(destination)
    store = _new_store(Path(work))
    _resolves_to_itself(store, destination)
    fetched = git.run(["-C", str(store), "-c", "gc.auto=0", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                       "--recurse-submodules=no", "--", str(source), f"{commit}:refs/mrs/op/bootstrap"],
                      env=git.transport_only(source))
    got = git.run(["-C", str(store), "rev-parse", "--verify", "--quiet", "refs/mrs/op/bootstrap^{commit}"])
    if not fetched.ok or got.out != commit:
        raise Refused("source", f"exact commit {commit} is not available from {source}")
    try:
        selection = config.at_commit(store, commit)
        if selection.applicability != "lifecycle":
            raise Refused("applicability", f"{selection.repository} selects {selection.applicability!r}; "
                                           "lifecycle mutation is refused")
        version = versions.parse_file(git.read_file(store, commit, "VERSION") or b"")
    except (config.ConfigError, versions.VersionError, git.GitError) as exc:
        raise Refused("candidate", str(exc)) from None
    operation = {"format": OPERATION_FORMAT, "kind": "bootstrap", "repository": selection.repository,
                 "remote": destination, "version": str(version), "expected": {DEV: None}, "updates": {DEV: commit},
                 "tool": _tool(destination)}
    current = _classify(store, destination, operation)
    if current.status == "UNKNOWN":
        raise state.Unknown(current.detail)
    if current.status not in ("NOT_APPLIED", "COMPLETED"):
        raise Refused(current.reason, current.detail)
    _write(Path(work), operation)
    return operation


def _next_line_commit(store: Path, candidate: str, current: versions.Version, following: versions.Version,
                      opened: date, env: dict[str, str]) -> str:
    blob = git.check(["-C", str(store), "hash-object", "-w", "--no-filters", "--stdin"],
                     input=versions.file_bytes(following)).out
    entries = []
    for record in git.check(["-C", str(store), "ls-tree", "-z", candidate]).stdout.split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        if name == b"VERSION":
            if not meta.startswith(b"100644 blob "):
                raise Refused("version", "VERSION must be a regular non-executable file")
            record = b"100644 blob " + blob.encode("ascii") + b"\tVERSION"
        entries.append(record + b"\0")
    tree = git.check(["-C", str(store), "mktree", "-z", "--missing"], input=b"".join(entries)).out
    message = f"Open line {following} after release {current}\n\n{state.OPENED_LINE}{opened.isoformat()}\n"
    return git.check(["-C", str(store), "-c", "i18n.commitEncoding=UTF-8", "commit-tree", "--no-gpg-sign", tree,
                      "-p", candidate, "-F", "-"], input=message.encode("utf-8"), env=env).out


def prepare_release(*, remote: str | Path, work: Path, candidate: str, acceptance: dict, identity: Identity,
                    now: datetime | None = None) -> dict:
    """Prepare the atomic promotion of exact integrated `candidate` (C) with receipt tag T and next line D.
    Low-level fixture path: `acceptance` is recorded, not judged, and cannot claim the assessed format;
    prepare_accepted_release is the checked path, and the only one for a real target."""
    if not is_fixture(_destination(remote)):
        raise Refused("acceptance", "an unjudged acceptance payload is only for disposable fixtures; a real target "
                                    "is released only from a SUFFICIENT assessment (prepare_accepted_release)")
    if not isinstance(acceptance, dict) or set(acceptance) != {"criteria", "results", "verdict"}:
        raise Refused("acceptance", "acceptance must contain exactly criteria, results and verdict")
    return _prepare_release(remote, work, candidate, lambda store, observed: acceptance, identity, now)


def prepare_accepted_release(*, remote: str | Path, work: Path, candidate: str, records: list[bytes],
                             attestation: bytes | None, proposals: bytes | None = None, pstack: Path | None,
                             identity: Identity, now: datetime | None = None) -> dict:
    """The checked path: the receipt carries only a SUFFICIENT assessment of exactly the observed dev C, made
    from collected records and a non-author verdict by the sources C selects."""
    def assessed(store: Path, observed: state.State) -> dict:
        result = acceptance_module.assess(store, observed, candidate, records, attestation, proposals, pstack)
        if result.status != acceptance_module.SUFFICIENT:
            raise Refused("acceptance", f"{result.status}: " + "; ".join(result.problems + result.missing))
        return result.payload
    return _prepare_release(remote, work, candidate, assessed, identity, now)


def _prepare_release(remote: str | Path, work: Path, candidate: str, payload, identity: Identity,
                     now: datetime | None) -> dict:
    now = _utc(now)
    env = _identity_env(identity, now)
    destination = _destination(remote)
    tool = _tool(destination)
    store = _new_store(Path(work))
    _resolves_to_itself(store, destination)
    observed = state.observe(store, destination)
    if observed.applicability not in (None, "lifecycle"):
        raise Refused("applicability", f"{observed.repository} selects {observed.applicability!r}; "
                                       "lifecycle mutation is refused")
    if observed.kind != "LIFECYCLE":
        raise Refused("unsupported-state", "; ".join(observed.problems) or "target is empty; bootstrap first")
    if observed.dev != candidate:
        raise Refused("stale", f"dev is {observed.dev}, not the accepted candidate {candidate}")
    current = observed.active
    tag_ref = f"refs/tags/{current}"
    if tag_ref in observed.refs:
        raise Refused("conflict", f"{tag_ref} already exists")
    opened = now.date()
    try:
        following = versions.next_line(current, opened)
    except versions.VersionError as exc:
        raise Refused("version", str(exc)) from None
    acceptance = payload(store, observed)
    next_commit = _next_line_commit(store, candidate, current, following, opened, env)
    receipt = state.encode_receipt({
        "format": state.RECEIPT_FORMAT, "repository": observed.repository, "version": str(current),
        "candidate": candidate, "acceptance": acceptance,
        "next": {"commit": next_commit, "version": str(following), "opened": opened.isoformat()}})
    if len(receipt) > state.MAX_RECEIPT_BYTES:
        raise Refused("receipt", f"receipt is {len(receipt)} bytes; the limit is {state.MAX_RECEIPT_BYTES}")
    tagger = f"{identity.name} <{identity.email}> {int(now.timestamp())} +0000"
    tag = git.check(["-C", str(store), "mktag"], input=(
        f"object {candidate}\ntype commit\ntag {current}\ntagger {tagger}\n\n".encode("utf-8")
        + state.MARKER + b"\n" + receipt + b"\n")).out
    git.check(["-C", str(store), "update-ref", "refs/mrs/op/tag", tag])
    git.check(["-C", str(store), "update-ref", "refs/mrs/op/next", next_commit])
    state.contract_release(store, str(current), tag)  # prepared objects must validate as observers will
    operation = {"format": OPERATION_FORMAT, "kind": "release", "repository": observed.repository,
                 "remote": destination, "version": str(current), "candidate": candidate, "tag": tag,
                 "next": {"commit": next_commit, "version": str(following), "opened": opened.isoformat()},
                 "expected": {MAIN: observed.main, tag_ref: None, DEV: candidate},
                 "updates": {MAIN: candidate, tag_ref: tag, DEV: next_commit},
                 "receipt_sha256": hashlib.sha256(receipt).hexdigest(),
                 "acceptance_sha256": hashlib.sha256(state.encode_receipt(acceptance)).hexdigest(),
                 "prepared_at": now.isoformat(), "tool": tool}
    _write(Path(work), operation)
    return operation


# --- observation and classification ----------------------------------------------------------

def _classify(store: Path, destination: str, operation: dict) -> Outcome:
    try:
        refs = state.snapshot(store, destination)
        state.fetch(store, destination, refs)
        if operation["kind"] == "bootstrap":
            return _classify_bootstrap(store, operation, refs)
        return _classify_release(store, operation, refs)
    except (state.Unknown, git.GitError) as exc:
        return Outcome("UNKNOWN", "observation", str(exc))
    except state.Redirected as exc:  # configuration changed after the operation's own destination check
        return Outcome("UNKNOWN", "destination", str(exc))


def _classify_bootstrap(store: Path, operation: dict, refs: dict[str, str]) -> Outcome:
    prepared, dev = operation["updates"][DEV], refs.get(DEV)
    if dev is not None and state.ancestor(store, prepared, dev):
        observed = state.analyze(store, refs)
        if observed.kind != "LIFECYCLE":
            return Outcome("MIXED", "mixed", f"dev contains {prepared} but the state is unsupported: "
                                             + "; ".join(observed.problems), refs)
        return Outcome("COMPLETED", "completed", f"dev is {prepared} or descends from it", refs)
    if not refs:
        return Outcome("NOT_APPLIED", "ready", "target is empty", refs)
    if dev is None:
        return Outcome("DIVERGED", "nonempty", f"target is not empty: {', '.join(sorted(refs))}", refs)
    return Outcome("DIVERGED", "conflict", f"dev is {dev}, which does not contain {prepared}", refs)


def _classify_release(store: Path, operation: dict, refs: dict[str, str]) -> Outcome:
    tag_ref = f"refs/tags/{operation['version']}"
    candidate, tag, next_commit = operation["candidate"], operation["tag"], operation["next"]["commit"]
    observed = state.analyze(store, refs)
    if observed.kind == "LIFECYCLE" and any(release.tag == tag for release in observed.releases):
        return Outcome("COMPLETED", "completed", f"{tag_ref} is the prepared receipt tag and history is consistent",
                       refs)
    seen_tag, seen_main, seen_dev = refs.get(tag_ref), refs.get(MAIN), refs.get(DEV)
    if seen_tag == tag or (seen_dev is not None and state.ancestor(store, next_commit, seen_dev)):
        problems = "; ".join(observed.problems) or f"main={seen_main} tag={seen_tag} dev={seen_dev}"
        return Outcome("MIXED", "mixed", f"prepared objects are published but the state is inconsistent: {problems}",
                       refs)
    if seen_tag is None and seen_main == operation["expected"][MAIN] and seen_dev == candidate:
        if observed.kind != "LIFECYCLE":
            return Outcome("DIVERGED", "unsupported-state", "; ".join(observed.problems), refs)
        return Outcome("NOT_APPLIED", "ready", "every expected value holds", refs)
    return Outcome("DIVERGED", "conflict" if seen_tag is not None else "stale",
                   f"expected main={operation['expected'][MAIN]} tag=absent dev={candidate}; "
                   f"observed main={seen_main} tag={seen_tag} dev={seen_dev}", refs)


# --- application -----------------------------------------------------------------------------

def _shown(destination: str) -> str:
    """The destination as Git reports it after a push: a URL without its user information, a path as given."""
    if git.scheme(destination) is None:
        return destination
    head, separator, rest = destination.partition("://")
    authority, slash, path = rest.partition("/")
    return head + separator + authority.rpartition("@")[2] + slash + path


def _reported_elsewhere(reported: str, destination: str) -> bool:
    if git.scheme(destination) is not None:  # a URL is compared as text, never resolved as a local path
        return reported != _shown(destination)
    try:
        return not os.path.samefile(reported, destination)
    except OSError:
        return True


def _push_result(result: git.Result, operation: dict, destination: str) -> str:
    """Classify a porcelain push. The caller always re-observes; this only names the reported cause."""
    refs = set(operation["updates"])
    expected_absent = {ref for ref, old in operation["expected"].items() if old is None}
    statuses, reported = {}, None
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if line.startswith("To "):
            reported = line[3:]
        elif len(parts) == 3 and len(parts[0]) == 1:
            statuses[parts[1].rpartition(":")[2]] = (parts[0], parts[2])
    if reported is not None and _reported_elsewhere(reported, destination):
        return "destination-mismatch"
    summaries = " ".join(summary for _, summary in statuses.values())
    if result.ok and set(statuses) == refs and all(flag in "*+ " for flag, _ in statuses.values()):
        return "success"
    if "(stale info)" in summaries:
        return "stale"
    if "hook declined" in summaries:
        return "policy"
    # The server found an expected-absent ref already created (Git 2.52 names this in the porcelain summary).
    if any(ref in expected_absent and summary == "[remote rejected] (reference already exists)"
           for ref, (_, summary) in statuses.items()):
        return "stale"
    server_update_failed = any(text in summaries for text in ("transaction failed", "failed to update ref"))
    if server_update_failed and (b"but expected" in result.stderr or b"reference already exists" in result.stderr):
        return "stale"
    if "rejected]" in summaries:
        return "rejected"
    if not statuses and b"does not support --atomic push" in result.stderr:
        return "atomic-unsupported"
    return "unknown"


def push_command(operation: dict) -> list[str]:
    """The Git arguments of the one guarded push, run in the operation's store: explicit leases on every
    updated ref, full refspecs, one destination and no ambient extras."""
    args = ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", "--no-force-if-includes"]
    if operation["kind"] == "release":
        args.append("--atomic")
    args += [f"--force-with-lease={ref}:{old or ''}" for ref, old in operation["expected"].items()]
    return args + ["--", operation["remote"], *[f"{new}:{ref}" for ref, new in operation["updates"].items()]]


def _ready(store: Path, destination: str, operation: dict, now: datetime) -> Outcome:
    """The target's state as apply reports it; NOT_APPLIED only when a push may be attempted, which for a release
    also needs its next line to have been opened in the current UTC year. The leases, not this observation, guard
    the push against any change after it."""
    before = _classify(store, destination, operation)
    if before.status == "NOT_APPLIED" and operation["kind"] == "release":
        opened = date.fromisoformat(operation["next"]["opened"])
        if now.year != opened.year:
            return Outcome("REFUSED", "year-changed", f"line {operation['next']['version']} was opened for "
                           f"{opened.year} and the operation is not applied; prepare it again", before.refs)
    return before


def _attempt(work: Path, store: Path, operation: dict, destination: str) -> str:
    """One push with explicit leases and no ambient extras. No fallback, no retry."""
    _resolves_to_itself(store, destination)
    with open(work / ATTEMPTS_FILE, "a", encoding="utf-8") as log:
        log.write(f"{datetime.now(timezone.utc).isoformat()} push attempted\n")
    result = git.run(["-C", str(store), *push_command(operation)], env=git.transport_only(destination))
    return _push_result(result, operation, destination)


def push(work: Path, *, now: datetime | None = None) -> str:
    """The guarded primitive. A real target is pushed only in the state in which apply pushes; otherwise this raises
    Refused, or state.Unknown. A disposable fixture is pushed in any state, so tests can show the leases reject."""
    work = Path(work)
    operation, store = load(work), work / STORE
    destination = _destination(operation["remote"])
    _resolves_to_itself(store, destination)
    if not is_fixture(destination):
        before = _ready(store, destination, operation, _utc(now))
        if before.status == "UNKNOWN":
            raise state.Unknown(before.detail)
        if before.status != "NOT_APPLIED":
            raise Refused(before.reason, before.detail)
    return _attempt(work, store, operation, destination)


def reconcile(work: Path) -> Outcome:
    """Read-only comparison of the prepared operation with the target's current state."""
    work = Path(work)
    operation, store = load(work), work / STORE
    destination = _destination(operation["remote"])
    try:
        _resolves_to_itself(store, destination)
    except state.Unknown as exc:
        return Outcome("UNKNOWN", "destination", str(exc))
    return _classify(store, destination, operation)


def apply(work: Path, *, now: datetime | None = None) -> Outcome:
    work = Path(work)
    now = _utc(now)
    store = work / STORE
    try:
        operation = load(work)
        destination = _destination(operation["remote"])
        _resolves_to_itself(store, destination)
    except Refused as exc:
        return Outcome("REFUSED", exc.code, exc.detail)
    except state.Unknown as exc:
        return Outcome("UNKNOWN", "destination", str(exc))
    before = _ready(store, destination, operation, now)
    if before.status == "COMPLETED":
        return Outcome("NOOP", "completed", before.detail, before.refs)
    if before.status != "NOT_APPLIED":
        status = before.status if before.status in ("MIXED", "UNKNOWN") else "REFUSED"
        return Outcome(status, before.reason, before.detail, before.refs)
    try:
        pushed = _attempt(work, store, operation, destination)
    except Refused as exc:
        return Outcome("REFUSED", exc.code, exc.detail, before.refs)
    except state.Unknown as exc:
        return Outcome("UNKNOWN", "destination", str(exc), before.refs)
    if pushed == "destination-mismatch":
        return Outcome("UNKNOWN", pushed, "Git reported a push location other than the destination; inspect both",
                       before.refs)
    after = _classify(store, destination, operation)
    if after.status == "COMPLETED":
        note = "" if pushed == "success" else f" (push reported {pushed}; reconciliation found completion)"
        return Outcome("APPLIED", "applied" if pushed == "success" else pushed, after.detail + note, after.refs)
    if pushed == "success" or after.status in ("MIXED", "UNKNOWN"):
        status = "MIXED" if after.status == "MIXED" else "UNKNOWN"
        return Outcome(status, after.reason, f"push reported {pushed}; {after.detail}", after.refs)
    return Outcome("REFUSED", pushed, f"{after.status.lower()}: {after.detail}", after.refs)

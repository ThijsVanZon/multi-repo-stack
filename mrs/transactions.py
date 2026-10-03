"""Guarded dev-only bootstrap and atomic main/tag/dev release, with read-only reconciliation.

Slice 1 boundary: these functions mutate only disposable fixture destinations (an absolute
path to a local bare repository containing FIXTURE_MARKER). There is no command-line entry
for them and no acceptance gate yet; the acceptance payload is recorded, not judged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, git, sources, state, versions
from .state import DEV, MAIN

OPERATION_FORMAT = "multi-repo-stack/operation/1"
FIXTURE_MARKER = "mrs-disposable-fixture"
OPERATION_FILE = "operation.json"
ATTEMPTS_FILE = "attempts.log"
STORE = "repo.git"


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


def _destination(remote: str | Path) -> str:
    path = Path(remote)
    if not path.is_absolute():
        raise Refused("destination", "Slice 1 transactions accept only an absolute path to a local bare repository")
    if not (path / FIXTURE_MARKER).is_file():
        raise Refused("destination", f"{path} is not marked as a disposable fixture ({FIXTURE_MARKER}); real "
                                     "targets require the acceptance gate, which Slice 1 does not implement")
    probe = git.run(["-C", str(path), "rev-parse", "--is-bare-repository", "--absolute-git-dir"])
    lines = probe.out.splitlines() if probe.ok else []
    if len(lines) != 2 or lines[0] != "true" or not os.path.samefile(lines[1], path):
        raise Refused("destination", f"{path} is not the root of a bare repository")
    return str(path)


def _check_rewrites(store: Path, destination: str) -> None:
    """Refuse ambient configuration that would send observation or the push somewhere else."""
    forms = {destination, Path(destination).as_posix()}
    rules = git.run(["-C", str(store), "config", "-z", "--get-regexp", r"^(url\..*\.(push)?insteadof|remote\..*)$"])
    for record in rules.stdout.decode("utf-8", "replace").split("\0") if rules.ok else []:
        key, _, value = record.partition("\n")
        rewrites = key.startswith("url.") and value and any(form.startswith(value) for form in forms)
        if rewrites or any(key.startswith(f"remote.{form}.") for form in forms):
            raise Refused("destination", f"ambient {key} would change where the destination resolves")


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


def _write(work: Path, operation: dict) -> None:
    with open(work / OPERATION_FILE, "xb") as handle:
        handle.write(json.dumps(operation, indent=2, sort_keys=True).encode("utf-8") + b"\n")


def load(work: Path) -> dict:
    operation = json.loads((Path(work) / OPERATION_FILE).read_bytes())
    if operation.get("format") != OPERATION_FORMAT:
        raise Refused("operation", f"{work} does not hold a {OPERATION_FORMAT} record")
    return operation


def _identity_env(identity: Identity, when: datetime) -> dict[str, str]:
    for value in (identity.name, identity.email):
        if not value or any(char in value for char in "<>\n\0"):
            raise Refused("identity", f"unusable identity value {value!r}")
    stamp = f"{int(when.timestamp())} +0000"
    return {"GIT_AUTHOR_NAME": identity.name, "GIT_AUTHOR_EMAIL": identity.email, "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": identity.name, "GIT_COMMITTER_EMAIL": identity.email, "GIT_COMMITTER_DATE": stamp}


# --- preparation -----------------------------------------------------------------------------

def prepare_bootstrap(*, source: Path, commit: str, remote: str | Path, work: Path) -> dict:
    """Prepare creation of dev at exact `commit` (B) from a local source repository."""
    destination = _destination(remote)
    store = _new_store(Path(work))
    _check_rewrites(store, destination)
    fetched = git.run(["-C", str(store), "-c", "gc.auto=0", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                       "--recurse-submodules=no", "--", str(source), f"{commit}:refs/mrs/op/bootstrap"])
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
                 "tool": sources.tool_identity()}
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
    """Prepare the atomic promotion of exact integrated `candidate` (C) with receipt tag T and next line D."""
    now = _utc(now)
    env = _identity_env(identity, now)
    if not isinstance(acceptance, dict) or set(acceptance) != {"criteria", "results", "verdict"}:
        raise Refused("acceptance", "acceptance must contain exactly criteria, results and verdict")
    destination = _destination(remote)
    store = _new_store(Path(work))
    _check_rewrites(store, destination)
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
                 "receipt_sha256": hashlib.sha256(receipt).hexdigest(), "prepared_at": now.isoformat(),
                 "tool": sources.tool_identity()}
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


def _damaged(store: Path, operation: dict) -> str | None:
    for ref, new in operation["updates"].items():
        kind = "tag" if ref.startswith("refs/tags/") else "commit"
        if git.object_type(store, new) != kind:
            return f"prepared {kind} {new} for {ref} is missing from {store}"
    if operation["kind"] == "release":
        try:
            state.contract_release(store, operation["version"], operation["tag"])
        except (state.NotContract, state.Malformed, state.Unknown) as exc:
            return f"prepared tag no longer validates: {exc}"
    return None


# --- application -----------------------------------------------------------------------------

def _same_path(reported: str, destination: str) -> bool:
    try:
        return os.path.samefile(reported, destination)
    except OSError:
        return False


def _push_result(result: git.Result, refs: set[str], destination: str) -> str:
    """Classify a porcelain push. The caller always re-observes; this only names the reported cause."""
    statuses, reported = {}, None
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if line.startswith("To "):
            reported = line[3:]
        elif len(parts) == 3 and len(parts[0]) == 1:
            statuses[parts[1].rpartition(":")[2]] = (parts[0], parts[2])
    if reported is not None and not _same_path(reported, destination):
        return "destination-mismatch"
    summaries = " ".join(summary for _, summary in statuses.values())
    if result.ok and set(statuses) == refs and all(flag in "*+ " for flag, _ in statuses.values()):
        return "success"
    if "(stale info)" in summaries:
        return "stale"
    if "hook declined" in summaries:
        return "policy"
    server_update_failed = any(text in summaries for text in ("transaction failed", "failed to update ref"))
    if server_update_failed and (b"but expected" in result.stderr or b"reference already exists" in result.stderr):
        return "stale"
    if "rejected]" in summaries:
        return "rejected"
    if not statuses and b"does not support --atomic push" in result.stderr:
        return "atomic-unsupported"
    return "unknown"


def push(work: Path) -> str:
    """The guarded primitive: one push with explicit leases and no ambient extras. No fallback, no retry."""
    work = Path(work)
    operation, store = load(work), work / STORE
    destination = _destination(operation["remote"])
    _check_rewrites(store, destination)
    with open(work / ATTEMPTS_FILE, "a", encoding="utf-8") as log:
        log.write(f"{datetime.now(timezone.utc).isoformat()} push attempted\n")
    args = ["-C", str(store), "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
            "--no-force-if-includes"]
    if operation["kind"] == "release":
        args.append("--atomic")
    args += [f"--force-with-lease={ref}:{old or ''}" for ref, old in operation["expected"].items()]
    args += ["--", destination, *[f"{new}:{ref}" for ref, new in operation["updates"].items()]]
    return _push_result(git.run(args), set(operation["updates"]), destination)


def reconcile(work: Path) -> Outcome:
    """Read-only comparison of the prepared operation with the target's current state."""
    work = Path(work)
    operation = load(work)
    return _classify(work / STORE, _destination(operation["remote"]), operation)


def apply(work: Path, *, now: datetime | None = None) -> Outcome:
    work = Path(work)
    now = _utc(now)
    operation, store = load(work), work / STORE
    try:
        destination = _destination(operation["remote"])
        _check_rewrites(store, destination)
    except Refused as exc:
        return Outcome("REFUSED", exc.code, exc.detail)
    damage = _damaged(store, operation)
    if damage:
        return Outcome("REFUSED", "operation", damage)
    before = _classify(store, destination, operation)
    if before.status == "COMPLETED":
        return Outcome("NOOP", "completed", before.detail, before.refs)
    if before.status != "NOT_APPLIED":
        status = before.status if before.status in ("MIXED", "UNKNOWN") else "REFUSED"
        return Outcome(status, before.reason, before.detail, before.refs)
    if operation["kind"] == "release":
        opened = date.fromisoformat(operation["next"]["opened"])
        if now.year != opened.year:
            return Outcome("REFUSED", "year-changed", f"line {operation['next']['version']} was opened for "
                           f"{opened.year} and the operation is not applied; prepare it again", before.refs)
    try:
        pushed = push(work)
    except Refused as exc:
        return Outcome("REFUSED", exc.code, exc.detail, before.refs)
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

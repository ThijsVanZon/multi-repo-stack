"""Notes-only GitHub Release pages for a release whose Git transaction is complete. `prepare` reads Git only and
seals one create in a new folder; page.json's SHA-256 is the digest a grant names. `apply` loads only the granted,
unchanged operation of the running tool, claims its one attempt exclusively and sends the validated request once,
never again. `reconcile` only reads. Readbacks compare with the notes validated at load. Nothing here writes Git.

Requests use this module's own transport: one new direct connection per request to the bound origin, targets from
four fixed templates, TLS verified, no redirect, retry, reuse or proxy. The credential is `gh auth token`, held in
memory and sent only to that origin.
"""

from __future__ import annotations

import functools
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import collect, git, sources, state, transactions

API, API_HOST, API_VERSION = "https://api.github.com", "github.com", "2022-11-28"
TIMEOUT = 60  # seconds, for connecting and for each read
MAX_BODY = 16 << 20
MAX_PAGES = 100
_LOOPBACK = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")  # tests' stand-in; library only
_TOKEN = re.compile(rb"([\x21-\x7e]+)\r?\n?")
_NEXT = re.compile(r';\s*rel="?next"?', re.IGNORECASE)
_GITHUB = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")
_SLUG = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REQUEST_ID = re.compile(r"[0-9A-Fa-f:]{1,64}")
_SECRET = re.compile(r"(?i)\b(?:gh[opsur]_|github_pat_)\w+|[a-z][a-z0-9+.-]*://[^/\s@]*@|\b(?:authorization\b"
                     r"[\s:=]*(?:(?:bearer|token|basic)\b)?|bearer\b|token\b)[\s:=]+\S+")
FORMAT = "multi-repo-stack/page/1"
RECORD, REQUEST, NOTES, CLAIM, RESULT = "page.json", "request.json", "notes.md", "claim.json", "result.json"
FIXED = {"draft": False, "prerelease": False, "generate_release_notes": False}
_KEYS = {"format", "repository", "api", "api_version", "destination", "tag", "tag_object", "candidate", "title",
         "notes_sha256", "request_sha256", "principal", "fixed", "tool", "prepared_at"}


class Stop(Exception):
    """A result that ends a command early: REFUSED, DIVERGED or UNKNOWN, with a reason and detail."""

    def __init__(self, status: str, reason: str, detail: str):
        super().__init__(f"{status} {reason}: {detail}")
        self.status, self.reason, self.detail = status, reason, detail


# --- transport ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    status: int | None  # None: no complete answer
    headers: dict[str, str]  # lowercased names
    body: bytes
    failure: str | None  # the transport failure's exception class, never its message

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def _origin(api: str) -> tuple[type, str, int]:
    if api == API:
        return http.client.HTTPSConnection, "api.github.com", 443
    loopback = _LOOPBACK.fullmatch(api)
    if loopback and int(loopback.group(1)) < 65536:
        return http.client.HTTPConnection, "127.0.0.1", int(loopback.group(1))
    raise Stop("REFUSED", "api", f"the API must be {API}")


def call(api: str, token: str, method: str, target: str, body: bytes | None = None) -> Answer:
    """One request on one new direct connection to the bound origin, then close it. Never follows, retries or
    proxies; a transport failure is reported by its class only."""
    kind, host, port = _origin(api)
    options = {"context": ssl.create_default_context()} if kind is http.client.HTTPSConnection else {}
    connection = kind(host, port, timeout=TIMEOUT, **options)
    try:
        connection.putrequest(method, target, skip_accept_encoding=True)
        for name, value in (("Accept", "application/vnd.github+json"), ("X-GitHub-Api-Version", API_VERSION),
                            ("Authorization", f"Bearer {token}"), ("User-Agent", "multi-repo-stack")):
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read(MAX_BODY + 1)
        if response.length and len(data) <= MAX_BODY:  # fewer bytes arrived than Content-Length declared
            raise http.client.IncompleteRead(data, response.length)
        return Answer(response.status, {k.lower(): v for k, v in response.getheaders()}, data, None)
    except (OSError, http.client.HTTPException) as exc:
        return Answer(None, {}, b"", type(exc).__name__)
    finally:
        connection.close()


def credential() -> str:
    """The operator's github.com token from `gh auth token`; refused unless it is one header-safe line. Neither
    the token nor gh's output is ever repeated in a result."""
    gh = shutil.which("gh")
    if gh is None or Path(gh).suffix.lower() in (".bat", ".cmd"):
        raise Stop("UNKNOWN", "credential", "gh is not on PATH; the token comes from `gh auth token`")
    try:
        got = subprocess.run([gh, "auth", "token", "--hostname", API_HOST], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Stop("UNKNOWN", "credential", f"gh auth token could not run ({type(exc).__name__})") from None
    found = _TOKEN.fullmatch(got.stdout) if got.returncode == 0 else None
    if found is None:
        raise Stop("UNKNOWN", "credential", f"gh auth token --hostname {API_HOST} gave no usable token "
                                            f"(exit {got.returncode})")
    return found.group(1).decode("ascii")


def _strict(data: bytes):
    def unique(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("duplicate keys")
        return dict(pairs)

    def constant(name):
        raise ValueError(f"{name} is not JSON")
    text = data.decode("utf-8")
    if text.startswith("﻿"):
        raise ValueError("byte order mark")
    value = json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    json.dumps(value, ensure_ascii=False).encode("utf-8")  # UnicodeEncodeError: a lone surrogate is not text
    return value


def _shown(answer: Answer) -> str:
    return f"failed ({answer.failure})" if answer.failure else f"answered {answer.status}"


def read(api: str, token: str, target: str, what: str):
    """A qualified read (status 200, identity encoding, strict JSON within the size limit), or Stop UNKNOWN."""
    answer = call(api, token, "GET", target)
    problem = (None if answer.status == 200 else _shown(answer))
    if problem is None and (answer.header("Content-Encoding") or len(answer.body) > MAX_BODY):
        problem = "answered with an encoded or oversized body"
    if problem is None:
        try:
            return _strict(answer.body), answer
        except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and UnicodeEncodeError included
            problem = f"answered a body that is not strict JSON ({type(exc).__name__})"
    raise Stop("UNKNOWN", what, f"GET {target} {problem}")


def listing(api: str, token: str, slug: str) -> list[dict]:
    """Every release entry, read page by page from the fixed template. `rel="next"` only says another page exists;
    its URL is never requested."""
    entries: list[dict] = []
    for number in range(1, MAX_PAGES + 1):
        target = f"/repos/{slug}/releases?per_page=100&page={number}"
        page, answer = read(api, token, target, "listing")
        more = bool(_NEXT.search(answer.header("Link") or ""))
        if (not isinstance(page, list) or not all(isinstance(item, dict) for item in page) or len(page) > 100
                or (more and len(page) < 100)):
            raise Stop("UNKNOWN", "listing", f"page {number} of the release listing is not a consistent page")
        entries += page
        if not more:
            ids = [json.dumps(item.get("id")) for item in entries]
            if len(ids) != len(set(ids)):
                raise Stop("UNKNOWN", "listing", "a release appears twice; the listing changed while it was read")
            return entries
    raise Stop("UNKNOWN", "listing", f"the listing has more than {MAX_PAGES} pages")


# --- preparation and load --------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _exclusive(path: Path, data: bytes) -> None:
    """Create `path` only if it is absent (FileExistsError otherwise), then make its bytes and entry durable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.name != "nt":  # the folder entry too; Windows cannot open a folder for fsync
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


def _slug(api: str, destination: str) -> str | None:
    """The GitHub repository a real-API destination names, or None for the loopback stand-in, which is only for a
    local Git destination. Anything else is refused."""
    if api == API:
        found = _GITHUB.fullmatch(destination)
        if found is None or found.group(2).strip(".") == "":
            raise Stop("REFUSED", "repository", "the Git destination must be https://github.com/<owner>/<name>")
        return f"{found.group(1)}/{found.group(2)}"
    _origin(api)
    if git.scheme(destination) not in (None, "file"):
        raise Stop("REFUSED", "api", "a loopback API is only for a local Git destination")
    return None


def _request(tag: str, candidate: str, title: str, body: str) -> bytes:
    request = {"tag_name": tag, "target_commitish": candidate, "name": title, "body": body, **FIXED}
    return (json.dumps(request, indent=2, ensure_ascii=True) + "\n").encode("ascii")


def prepare(*, destination: str, tag: str, title: str, notes: Path, principal: str, work: Path,
            api: str = API) -> tuple[str, dict]:
    """Seal one create of tag N's page into the new folder `work`; returns the operation digest and record."""
    if not title or any(char in title for char in "\r\n\0"):
        raise Stop("REFUSED", "title", "the title must be one non-empty line")
    if not _LOGIN.fullmatch(principal):
        raise Stop("REFUSED", "principal", "--principal must be a GitHub login")
    data = Path(notes).read_bytes()
    try:
        body = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Stop("REFUSED", "notes", "the notes are not UTF-8") from None
    if not body or "\r" in body or "\0" in body:
        raise Stop("REFUSED", "notes", "the notes must be non-empty, LF-only and without NUL")
    slug = _slug(api, destination)
    try:
        destination = transactions._destination(destination)
        tool = transactions._tool(destination)
        store = transactions._new_store(Path(work))
        state.check_destination(store, destination)
        observed = state.observe(store, destination)
    except transactions.Refused as exc:
        raise Stop("REFUSED", exc.code, exc.detail) from None
    except state.Redirected as exc:
        raise Stop("REFUSED", "destination", str(exc)) from None
    except (state.Unknown, git.GitError) as exc:
        raise Stop("UNKNOWN", "git", str(exc)) from None
    if observed.kind != "LIFECYCLE":
        raise Stop("REFUSED", "git-state", "; ".join(observed.problems) or f"the target is {observed.kind}")
    found = [release for release in observed.releases if str(release.version) == tag]
    if not found:
        raise Stop("REFUSED", "release", f"{tag} is not a contract release on the target")
    slug = slug or observed.repository
    if observed.repository.casefold() != slug.casefold():
        raise Stop("REFUSED", "repository", f"the release belongs to {observed.repository}, not {slug}")
    request = _request(tag, found[0].candidate, title, body)
    record = {"format": FORMAT, "repository": slug, "api": api, "api_version": API_VERSION,
              "destination": destination, "tag": tag, "tag_object": found[0].tag, "candidate": found[0].candidate,
              "title": title, "notes_sha256": _sha(data), "request_sha256": _sha(request), "principal": principal,
              "fixed": FIXED, "tool": tool, "prepared_at": _now()}
    _exclusive(Path(work) / NOTES, data)
    _exclusive(Path(work) / REQUEST, request)
    _exclusive(Path(work) / RECORD, _json(record))
    return _sha(_json(record)), record


def load(work: Path, granted: str | None = None) -> tuple[dict, bytes, bytes]:
    """The operation, its validated request bytes and its validated notes. Refused, never repaired, unless
    page.json is the granted operation (when a grant is given) in its exact format, its routing is consistent,
    every file has its bound bytes, the sealed store holds T as N's release of C and the running tool prepared
    it. The request bytes returned are the ones sent; the notes are the only readback reference."""
    work = Path(work)
    try:
        raw, request, notes = ((work / name).read_bytes() for name in (RECORD, REQUEST, NOTES))
        record = json.loads(raw)
        body = notes.decode("utf-8")
    except (OSError, ValueError) as exc:
        raise Stop("REFUSED", "record", f"the prepared operation cannot be read: {exc}") from None
    if granted is not None and granted != _sha(raw):
        raise Stop("REFUSED", "grant", f"the grant names operation {granted}, but {RECORD} is {_sha(raw)}")
    if (not isinstance(record, dict) or set(record) != _KEYS or _json(record) != raw or record["format"] != FORMAT
            or record["fixed"] != FIXED or record["api_version"] != API_VERSION
            or not all(isinstance(record[key], str) for key in _KEYS - {"fixed", "tool"})):
        raise Stop("REFUSED", "record", f"{RECORD} is not an exact {FORMAT} record")
    slug = _slug(record["api"], record["destination"])
    if not _SLUG.fullmatch(record["repository"]) or (slug or record["repository"]).casefold() != \
            record["repository"].casefold():
        raise Stop("REFUSED", "record", f"{RECORD} routes {record['repository']} inconsistently")
    if (_sha(notes) != record["notes_sha256"] or _sha(request) != record["request_sha256"]
            or request != _request(record["tag"], record["candidate"], record["title"], body)):
        raise Stop("REFUSED", "record", "the notes or request no longer match the operation")
    try:
        sealed = state.contract_release(work / transactions.STORE, record["tag"], record["tag_object"])
    except (state.Unknown, state.NotContract, state.Malformed, git.GitError) as exc:
        raise Stop("REFUSED", "record", f"the sealed store does not hold the prepared release: {exc}") from None
    if (sealed.candidate, sealed.repository.casefold()) != (record["candidate"], record["repository"].casefold()):
        raise Stop("REFUSED", "record", "the sealed release tag does not name the prepared candidate and repository")
    running = sources.tool_identity()
    if running != record["tool"]:
        raise Stop("REFUSED", "tool", f"prepared by the tool at {record['tool']}, but the running tool is {running}")
    return record, request, notes


# --- classification -------------------------------------------------------------------------------

def _tag(record: dict) -> None:
    """Tag N, listed from a private temporary store that is removed before returning."""
    scratch = tempfile.mkdtemp(prefix="mrs-page-")
    try:
        store = Path(scratch) / "observe.git"
        git.check(["init", "--quiet", "--bare", "--template=", str(store)])
        seen = state.snapshot(store, record["destination"]).get(f"refs/tags/{record['tag']}")
    except (OSError, state.Unknown, state.Redirected, git.GitError) as exc:
        raise Stop("UNKNOWN", "git", str(exc)) from None
    finally:
        collect.remove_tree(scratch)
    if seen != record["tag_object"]:
        raise Stop("DIVERGED", "tag", f"refs/tags/{record['tag']} is {seen or 'absent'}, not the release tag "
                                      f"{record['tag_object']}; a page never creates, moves or repairs a tag")


def _complete(entry: dict) -> bool:
    return (type(entry.get("id")) is int and entry["id"] > 0 and type(entry.get("draft")) is bool
            and type(entry.get("prerelease")) is bool and isinstance(entry.get("assets"), list)
            and all(isinstance(entry.get(key), str) for key in ("name", "body", "html_url")))


def _difference(stored: bytes, sealed: bytes) -> str:
    if stored.startswith(sealed):
        return f"+{len(stored) - len(sealed)} appended bytes"
    if sealed.startswith(stored):
        return f"its last {len(sealed) - len(stored)} bytes trimmed"
    return f"{len(stored)} bytes unlike the sealed {len(sealed)}"


def classify(record: dict, notes: bytes, get_token) -> dict:
    """COMPLETED or NOT_APPLIED; every other row raises Stop. Only tag N and N's page decide, compared with the
    notes validated at load."""
    api, slug, tag = record["api"], record["repository"], record["tag"]
    _tag(record)
    token = get_token()
    repo, _ = read(api, token, f"/repos/{slug}", "provider")
    if not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str) or not isinstance(
            repo.get("html_url"), str):
        raise Stop("UNKNOWN", "provider", "the repository read has no full_name and html_url")
    if repo["full_name"].casefold() != slug.casefold():
        raise Stop("DIVERGED", "repository", f"the provider names {repo['full_name']}, not {slug}")
    permissions = repo.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("push") is not True:
        raise Stop("UNKNOWN", "visibility", "this access has no push permission, so drafts may be hidden")
    mine = [entry for entry in listing(api, token, slug) if entry.get("tag_name") == tag]
    if not all(_complete(entry) for entry in mine):
        raise Stop("UNKNOWN", "listing", f"an entry for {tag} is not a complete release record")
    if not mine:
        user, answer = read(api, token, "/user", "visibility")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or login.casefold() != record["principal"].casefold():
            raise Stop("UNKNOWN", "visibility", f"no page for {tag} is listed, but this access is not the bound "
                                                f"principal {record['principal']} (it is {login!r})")
        scopes = [scope.strip() for scope in (answer.header("X-OAuth-Scopes") or "").split(",")]
        if "repo" not in scopes:
            raise Stop("UNKNOWN", "visibility", f"no page for {tag} is listed, but this access does not report "
                                                "the repo scope, so drafts may be hidden")
        return {"status": "NOT_APPLIED", "reason": "absent", "detail": f"no page or draft for {tag}"}
    if len(mine) > 1:
        raise Stop("DIVERGED", "duplicate", f"{len(mine)} pages or drafts name {tag}: {[e['id'] for e in mine]}")
    page = mine[0]
    if page["draft"]:
        raise Stop("DIVERGED", "draft", f"a draft for {tag} exists (id {page['id']}); resolve it explicitly")
    stored = page["body"].encode("utf-8")
    differences = [name for name, same in (
        ("name", page["name"] == record["title"]), ("prerelease", page["prerelease"] is False),
        ("assets", page["assets"] == []),
        ("html_url", page["html_url"].startswith(repo["html_url"] + "/releases/"))) if not same]
    if stored != notes:
        differences.append("body: " + _difference(stored, notes))
    if differences:
        raise Stop("DIVERGED", "page", f"the page for {tag} (id {page['id']}) differs: " + "; ".join(differences))
    return {"status": "COMPLETED", "reason": "matching", "detail": f"published page {page['id']} matches",
            "page": {"id": page["id"], "html_url": page["html_url"]}}


def attempt(work: Path) -> str:
    if not (Path(work) / CLAIM).exists():
        return "none"
    return "claimed-with-result" if (Path(work) / RESULT).exists() else "claimed-without-result"


# --- the one create -------------------------------------------------------------------------------

def _message(answer: Answer, token: str) -> str:
    """GitHub's error message for the printed report only: one line, credential-shaped text redacted."""
    try:
        text = _strict(answer.body).get("message")
    except (ValueError, RecursionError, AttributeError):
        return ""
    if not isinstance(text, str) or not text.strip():
        return ""
    return _SECRET.sub("[redacted]", text.replace(token, "[redacted]").splitlines()[0])[:200]


def apply(work: Path, granted: str) -> dict:
    record, request, notes = load(work, granted)
    get_token = functools.cache(credential)
    before = classify(record, notes, get_token)
    if before["status"] == "COMPLETED":
        return {**before, "status": "NOOP"}
    try:
        _exclusive(Path(work) / CLAIM, _json({"operation_sha256": granted, "at": _now()}))
    except FileExistsError:
        raise Stop("REFUSED", "attempted", "this operation was already claimed (an incomplete claim counts too); "
                                           "reconcile it. Another create needs a new preparation and grant") from None
    sent = call(record["api"], get_token(), "POST", f"/repos/{record['repository']}/releases", request)
    request_id = sent.header("X-GitHub-Request-Id")
    try:
        _exclusive(Path(work) / RESULT, _json({
            "status": sent.status, "request_id": request_id if _REQUEST_ID.fullmatch(request_id or "") else None,
            "transport": sent.failure or "answered", "at": _now()}))
        unrecorded = ""
    except OSError as exc:
        unrecorded = f" ({RESULT} not written: {type(exc).__name__})"
    message = _message(sent, get_token())
    answered = f"the create {_shown(sent)}" + (f" ({message})" if message else "") + unrecorded
    try:
        after = classify(record, notes, get_token)
    except Stop as exc:
        raise Stop(exc.status, exc.reason, f"{answered}; {exc.detail}") from None
    if after["status"] == "COMPLETED":
        return {**after, "status": "APPLIED", "detail": f"{answered}; {after['detail']}"}
    if sent.status is not None and 400 <= sent.status < 500:
        return {"status": "REFUSED", "reason": f"http-{sent.status}", "detail": f"{answered}; no page exists"}
    return {"status": "UNKNOWN", "reason": "unconfirmed", "detail": f"{answered}; no page is observed. Reconcile "
                                                                  "later; never resend"}


def reconcile(work: Path) -> dict:
    record, _, notes = load(work)
    return classify(record, notes, functools.cache(credential))

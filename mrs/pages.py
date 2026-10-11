"""Notes-only GitHub Release pages for a release whose Git transaction is complete: preparation, one guarded
create and read-only reconciliation of one sealed operation.

Provider requests go through this module's own transport: one new direct connection per request to the bound
origin only, request targets built from four fixed templates, TLS verification always on, no redirect followed,
no retry, no connection reuse and no proxy. The credential is the operator's own `gh auth token`, held in memory
and sent only to that origin; nothing here writes it anywhere.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

API, API_HOST, API_VERSION = "https://api.github.com", "github.com", "2022-11-28"
TIMEOUT = 60  # seconds, for connecting and for each read
MAX_BODY = 16 << 20
MAX_PAGES = 100
_LOOPBACK = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")  # tests' stand-in; library only
_TOKEN = re.compile(rb"([\x21-\x7e]+)\r?\n?")
_NEXT = re.compile(r';\s*rel="?next"?', re.IGNORECASE)


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
        got = subprocess.run([gh, "auth", "token", "--hostname", API_HOST], capture_output=True, timeout=TIMEOUT)
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
    return json.loads(text, object_pairs_hook=unique, parse_constant=constant)


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
        except ValueError as exc:  # UnicodeDecodeError included
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

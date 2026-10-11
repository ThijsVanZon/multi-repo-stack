"""A loopback stand-in for the GitHub REST endpoints `mrs page` uses, and the gh environment that reaches it (test
code; it models only what is written here, never GitHub itself).

It serves `GET /user`, `GET /repos/<slug>`, the paginated `GET /repos/<slug>/releases` and
`POST /repos/<slug>/releases` from scripted state, and records every request on the wire: method, path, query,
headers and exact body bytes (chunked bodies decoded). `post` scripts the create's answer, `raw` replaces any
answer with exact bytes, and `on_listing` and `on_post` run a scripted action, such as a Git change on the
disposable destination, when that request arrives.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Answers to the create. "-stored" variants store the page before answering; "drop" closes without an answer,
# "stall" never answers and a redirect points back at the same path. Only "201" and "-stored" store a page.
POSTS = ("201", "422", "500", "500-stored", "drop", "drop-stored", "stall", "301", "302", "307", "308")


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    query: str
    headers: dict[str, str]  # lowercased names
    body: bytes


class StandIn:
    def __init__(self, slug: str = "example/app", login: str = "fixture-owner", scopes: str | None = "repo, workflow",
                 push: bool = True, page_size: int = 100) -> None:
        self.slug, self.login, self.scopes, self.push, self.page_size = slug, login, scopes, push, page_size
        self.full_name = slug
        self.releases: list[dict] = []
        self.post = "201"
        self.raw: dict[str, bytes] = {}  # "GET /path?query" or "POST /path" -> exact response bytes
        self.next_origin: str | None = None  # serve rel="next" links on another origin
        self.on_listing = self.on_post = None
        self.wire: list[Request] = []
        self._released = threading.Event()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.stand_in = self
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._released.set()
        self._server.shutdown()
        self._server.server_close()

    def page(self, tag: str, body: str, **changes) -> dict:
        """A stored release as the listing shows it."""
        record = {"id": 1000 + len(self.releases), "tag_name": tag, "name": f"example/app {tag}", "body": body,
                  "draft": False, "prerelease": False, "assets": [],
                  "html_url": f"https://github.com/{self.slug}/releases/tag/{tag}", **changes}
        self.releases.append(record)
        return record

    def posts(self) -> list[Request]:
        return [request for request in self.wire if request.method != "GET"]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):  # trailers, then the blank line
                        pass
                    return data
                data += self.rfile.read(size + 2)[:size]
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, status: int, value=None, headers: dict | None = None) -> None:
        data = b"" if value is None else json.dumps(value).encode("utf-8")
        self.send_response(status)
        for name, text in {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(data)),
                           "X-GitHub-Request-Id": "C0DE:5EED:0001", **(headers or {})}.items():
            self.send_header(name, text)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        stand_in: StandIn = self.server.stand_in
        parts = urllib.parse.urlsplit(self.path)
        request = Request(self.command, parts.path, parts.query, {k.lower(): v for k, v in self.headers.items()},
                          self._body())
        stand_in.wire.append(request)
        key = f"{self.command} {parts.path}" + (f"?{parts.query}" if parts.query else "")
        releases = f"/repos/{stand_in.slug}/releases"
        if self.command == "GET" and parts.path == releases and stand_in.on_listing:
            stand_in.on_listing()
        if self.command == "POST" and parts.path == releases and stand_in.on_post:
            stand_in.on_post()
        if key in stand_in.raw:
            self.wfile.write(stand_in.raw[key])
            self.close_connection = True
        elif self.command == "GET" and parts.path == "/user":
            self._send(200, {"login": stand_in.login},
                       {} if stand_in.scopes is None else {"X-OAuth-Scopes": stand_in.scopes})
        elif self.command == "GET" and parts.path == f"/repos/{stand_in.slug}":
            self._send(200, {"full_name": stand_in.full_name, "html_url": f"https://github.com/{stand_in.full_name}",
                             "permissions": {"admin": stand_in.push, "push": stand_in.push, "pull": True}})
        elif self.command == "GET" and parts.path == releases:
            number = int(urllib.parse.parse_qs(parts.query).get("page", ["1"])[0])
            size = stand_in.page_size
            pages = max(1, -(-len(stand_in.releases) // size))
            headers = {}
            if number < pages:
                origin = stand_in.next_origin or stand_in.base
                headers["Link"] = f'<{origin}{releases}?per_page=100&page={number + 1}>; rel="next"'
            self._send(200, stand_in.releases[(number - 1) * size:number * size], headers)
        elif self.command == "POST" and parts.path == releases:
            self._create(stand_in, request.body)
        else:
            self._send(404, {"message": "Not Found"})

    def _create(self, stand_in: StandIn, body: bytes) -> None:
        mode = stand_in.post
        if mode in ("201", "500-stored", "drop-stored"):
            sent = json.loads(body)
            stored = stand_in.page(sent["tag_name"], sent["body"], name=sent["name"], draft=sent["draft"],
                                   prerelease=sent["prerelease"])
        if mode == "201":
            self._send(201, stored)
        elif mode == "422":
            self._send(422, {"message": "Validation Failed"})
        elif mode in ("500", "500-stored"):
            self._send(500, {"message": "Server Error"})
        elif mode in ("drop", "drop-stored"):
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
        elif mode == "stall":
            stand_in._released.wait()
            self.close_connection = True
        else:
            self._send(int(mode), {"message": "Moved"}, {"Location": f"{stand_in.base}{self.path}"})

    do_GET = do_POST = do_PATCH = do_DELETE = _handle


class DeadProxy:
    """The proxy every gh request is pointed at: it accepts and closes, so nothing it receives goes anywhere,
    and it counts every connection, so a test can show that no request left loopback."""

    def __init__(self) -> None:
        self.connections = 0
        self._socket = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            self.connections += 1
            connection.close()

    def close(self) -> None:
        self._socket.close()


DUMMY_TOKEN = "gho_fixtureDummyTokenNotACredential0000000"


def gh_environment(config: Path, proxy: str) -> dict[str, str]:
    """The environment gh runs with in page tests: an empty configuration, a dummy token, every proxy variable
    pointed at the dead proxy and no ambient GitHub host or credential."""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("GH_", "GITHUB_")) and key.upper() not in
           ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    env.update(GH_CONFIG_DIR=str(config), GH_TOKEN=DUMMY_TOKEN, GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
               HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy)
    return env

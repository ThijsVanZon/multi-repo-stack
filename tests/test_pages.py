import json
import os
import shutil
import socket
import tempfile
from pathlib import Path
from unittest import mock

from mrs import pages
from mrs.pages import Stop
from tests.page_stand_in import (DROP, STALL, SYNTHETIC_TOKEN, DeadProxy, StandIn, answer, gh_environment,
                                 redirect)
from tests.support import GitTestCase

SLUG = "example/app"
LISTING = f"/repos/{SLUG}/releases?per_page=100&page="


def create_body(tag: str = "26.1.0", notes: str = "Notes \u00fc \u2014\n\nend\n") -> bytes:
    """A create request shaped as prepare serializes it."""
    return (json.dumps({"tag_name": tag, "target_commitish": "c" * 40, "name": f"app {tag}", "body": notes,
                        "draft": False, "prerelease": False, "generate_release_notes": False,
                        "make_latest": "false"}, indent=2, ensure_ascii=True) + "\n").encode("ascii")


class PageTestCase(GitTestCase):
    """A loopback stand-in, a second stand-in playing another origin and a dead proxy behind every proxy
    variable; gh, with an empty configuration and a synthetic token, is the credential source. No request may
    reach the second stand-in or the dead proxy unless a test says so."""

    other_allowed = False

    def setUp(self) -> None:
        super().setUp()
        if shutil.which("gh") is None:
            self.fail("gh must be on PATH: it is the credential source of the page tests, which cannot be skipped")
        self.stand_in, self.other, self.proxy = StandIn(), StandIn(), DeadProxy()
        for closing in (self.stand_in, self.other, self.proxy):
            self.addCleanup(closing.close)
        config = self.tmp / "empty gh config"
        config.mkdir()
        env = mock.patch.dict(os.environ, gh_environment(config, self.proxy.url), clear=True)
        env.start()
        self.addCleanup(env.stop)
        timeout = mock.patch.object(pages, "TIMEOUT", 2)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.addCleanup(self._nothing_left_loopback)

    def _nothing_left_loopback(self) -> None:
        self.assertEqual(self.proxy.connections, 0, "a request went to a proxy")
        if not self.other_allowed:
            self.assertEqual(self.other.connections, 0, "a request reached another origin")

    def one_request_per_connection(self, stand_in: StandIn, calls: int) -> None:
        self.assertEqual(stand_in.connections, calls)
        self.assertEqual(len(stand_in.wire), calls)
        self.assertEqual(len({request.connection for request in stand_in.wire}), calls)

    def releases(self, count: int, start: int = 0) -> None:
        for number in range(start, start + count):
            self.stand_in.page(f"0.{number}.0", f"notes {number}\n")


class TransportQualificationTests(PageTestCase):
    """§6.1 (rev 3): the standard-library transport, qualified offline against loopback stand-ins."""

    def test_binding_sends_exact_requests_only_to_the_bound_origin(self):
        """§6.1 item 1, §4.2 origin binding and headers: each request template reaches the stand-in with exactly
        its method, target and headers; ambient GH_HOST and proxy variables change nothing."""
        os.environ.update(GH_HOST=self.other.base.removeprefix("http://"), http_proxy=self.proxy.url,
                          https_proxy=self.proxy.url)
        self.releases(1)
        body = create_body()
        for target in ("/user", f"/repos/{SLUG}", f"{LISTING}1"):
            self.assertEqual(pages.call(self.stand_in.base, SYNTHETIC_TOKEN, "GET", target).status, 200)
        self.assertEqual(pages.call(self.stand_in.base, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases",
                                    body).status, 201)

        host = self.stand_in.base.removeprefix("http://")
        common = {"host": host, "accept": "application/vnd.github+json", "x-github-api-version": "2022-11-28",
                  "authorization": f"Bearer {SYNTHETIC_TOKEN}", "user-agent": "multi-repo-stack"}
        self.assertEqual([(r.method, r.path, r.query, r.headers) for r in self.stand_in.wire], [
            ("GET", "/user", "", common), ("GET", f"/repos/{SLUG}", "", common),
            ("GET", f"/repos/{SLUG}/releases", "per_page=100&page=1", common),
            ("POST", f"/repos/{SLUG}/releases", "",
             {**common, "content-type": "application/json", "content-length": str(len(body))})])
        self.one_request_per_connection(self.stand_in, 4)
        with self.assertRaises(Stop) as refused:
            pages.call("http://localhost:1234", SYNTHETIC_TOKEN, "GET", "/user")
        self.assertEqual((refused.exception.status, refused.exception.reason), ("REFUSED", "api"))

    def test_credential_comes_only_from_gh_and_never_leaves_for_elsewhere(self):
        """§6.1 item 2, §4.2 credential: gh's token arrives as the bearer header; no gh, no token or an unsafe
        token is UNKNOWN (credential) before any request, and the token is never repeated."""
        token = pages.credential()
        self.assertEqual(token, SYNTHETIC_TOKEN)
        pages.read(self.stand_in.base, token, "/user", "visibility")
        self.assertEqual(self.stand_in.wire[0].headers["authorization"], f"Bearer {SYNTHETIC_TOKEN}")

        unsafe = {"space": "gho_with space", "CR": "gho_with\rcarriage", "LF": "gho_with\nline",
                  "empty": "", "absent": None}
        for name, value in unsafe.items():
            with self.subTest(name):
                env = {**os.environ, "GH_TOKEN": value} if value is not None else {
                    k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
                with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(Stop) as stopped:
                    pages.credential()
                self.assertEqual((stopped.exception.status, stopped.exception.reason), ("UNKNOWN", "credential"))
                self.assertNotIn("gho_with", str(stopped.exception))
        empty = self.tmp / "no gh here"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}), self.assertRaises(Stop) as stopped:
            pages.credential()
        self.assertEqual((stopped.exception.status, stopped.exception.reason), ("UNKNOWN", "credential"))
        self.assertEqual(len(self.stand_in.wire), 1)

    def test_redirects_are_never_followed(self):
        """§6.1 item 3, §4.2 no redirects, §4.3: a 3xx to any read makes it unqualified with no request to its
        Location; a 3xx to the create is its recorded answer after exactly one POST."""
        self.releases(150)
        reads = [("/user", "visibility"), (f"/repos/{SLUG}", "provider"), (f"{LISTING}1", "listing")]
        calls = 0
        for code in (301, 302, 303, 307, 308):
            for origin in (self.other.base, self.stand_in.base):
                for target, what in reads:
                    with self.subTest(code=code, target=target, origin=origin):
                        self.stand_in.raw = {f"GET {target}": redirect(code, origin + target)}
                        with self.assertRaises(Stop) as stopped:
                            pages.read(self.stand_in.base, SYNTHETIC_TOKEN, target, what)
                        calls += 1
                        self.assertEqual((stopped.exception.status, stopped.exception.reason), ("UNKNOWN", what))
                        self.assertIn(f"answered {code}", stopped.exception.detail)
                with self.subTest(code=code, page=2, origin=origin):
                    self.stand_in.raw = {f"GET {LISTING}2": redirect(code, f"{origin}{LISTING}2")}
                    with self.assertRaises(Stop) as stopped:
                        pages.listing(self.stand_in.base, SYNTHETIC_TOKEN, SLUG)
                    calls += 2
                    self.assertEqual(stopped.exception.reason, "listing")
                with self.subTest(code=code, create=True, origin=origin):
                    self.stand_in.raw, self.stand_in.post = {}, str(code)
                    self.stand_in.redirect_to = origin + f"/repos/{SLUG}/releases"
                    sent = pages.call(self.stand_in.base, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases", b"{}")
                    calls += 1
                    self.assertEqual((sent.status, sent.failure), (code, None))
        self.one_request_per_connection(self.stand_in, calls)
        self.assertEqual(len(self.stand_in.posts()), 10)

    def test_pagination_reads_only_template_pages(self):
        """§6.1 item 4, §4.3 complete listing: pages come from the fixed template; a rel="next" naming another
        origin is never requested; an inconsistent listing is UNKNOWN (listing)."""
        self.releases(250)
        self.stand_in.next_origin = self.other.base
        entries = pages.listing(self.stand_in.base, SYNTHETIC_TOKEN, SLUG)
        self.assertEqual([entry["tag_name"] for entry in entries], [f"0.{n}.0" for n in range(250)])
        self.assertEqual([r.query for r in self.stand_in.wire], [f"per_page=100&page={n}" for n in (1, 2, 3)])
        self.one_request_per_connection(self.stand_in, 3)

        page = [{"id": n, "tag_name": f"0.{n}.0"} for n in range(100)]
        cases = {
            "over 100 entries": {f"GET {LISTING}1": answer("200 OK", json.dumps(page + [{"id": 100}]).encode())},
            "next on a short page": {f"GET {LISTING}1": answer("200 OK", json.dumps(page[:99]).encode(),
                                                               'Link: <x>; rel="next"')},
            "an id twice": {f"GET {LISTING}1": answer("200 OK", json.dumps(page).encode(), 'Link: <x>; rel="next"'),
                            f"GET {LISTING}2": answer("200 OK", json.dumps(page[:1]).encode())},
            "a page that is not a list": {f"GET {LISTING}1": answer("200 OK", b'{"id": 1}')},
            "an entry that is not an object": {f"GET {LISTING}1": answer("200 OK", b"[1]")},
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.stand_in.raw = raw
                with self.assertRaises(Stop) as stopped:
                    pages.listing(self.stand_in.base, SYNTHETIC_TOKEN, SLUG)
                self.assertEqual((stopped.exception.status, stopped.exception.reason), ("UNKNOWN", "listing"))
        self.stand_in.raw = {}
        self.releases(pages.MAX_PAGES * 100 + 1 - 250, start=250)
        before = len(self.stand_in.wire)
        with self.assertRaises(Stop) as stopped:
            pages.listing(self.stand_in.base, SYNTHETIC_TOKEN, SLUG)
        self.assertIn(f"more than {pages.MAX_PAGES} pages", stopped.exception.detail)
        self.assertEqual(len(self.stand_in.wire) - before, pages.MAX_PAGES)

    def test_transport_failures_are_unqualified_and_never_retried(self):
        """§6.1 item 5, §4.2 no retries, §4.3: each failure makes a read unqualified, never absence, and puts
        exactly one request on the wire; the create's failure is reported by its class only."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
        refused = pages.call(dead, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases", b"{}")
        self.assertEqual((refused.status, refused.failure), (None, "ConnectionRefusedError"))
        with self.assertRaises(Stop):
            pages.read(dead, SYNTHETIC_TOKEN, "/user", "visibility")

        big = answer("200 OK", b"[" + b" " * pages.MAX_BODY + b"]")
        failures = {"dropped": DROP, "stalled": STALL, "malformed status": b"HTTP/1.1 2OO OK\r\n\r\n",
                    "not JSON": answer("200 OK", b"[{]"), "NaN": answer("200 OK", b"[NaN]"),
                    "duplicate keys": answer("200 OK", b'{"login": "a", "login": "b"}'),
                    "BOM": answer("200 OK", b"\xef\xbb\xbf[]"), "trailing data": answer("200 OK", b"[] []"),
                    "encoded": answer("200 OK", b"[]", "Content-Encoding: gzip"), "oversized": big}
        calls = 0
        for name, raw in failures.items():
            with self.subTest(name):
                self.stand_in.raw = {"GET /user": raw}
                with self.assertRaises(Stop) as stopped:
                    pages.read(self.stand_in.base, SYNTHETIC_TOKEN, "/user", "visibility")
                self.assertEqual((stopped.exception.status, stopped.exception.reason), ("UNKNOWN", "visibility"))
                calls += 1
        expected = {"dropped": ("RemoteDisconnected", None), "stalled": ("TimeoutError", None),
                    "malformed status": ("BadStatusLine", None)}
        for name, (failure, status) in expected.items():
            with self.subTest(create=name):
                self.stand_in.raw = {f"POST /repos/{SLUG}/releases": failures[name]}
                sent = pages.call(self.stand_in.base, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases", b"{}")
                self.assertEqual((sent.failure, sent.status), (failure, status))
                calls += 1
        self.one_request_per_connection(self.stand_in, calls)
        self.assertEqual(len(self.stand_in.posts()), 3)

    def test_create_body_arrives_byte_exact(self):
        """§6.1 item 6, §4.5 step 4: the stand-in receives exactly the request bytes, with their length."""
        body = create_body()
        sent = pages.call(self.stand_in.base, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases", body)
        self.assertEqual(sent.status, 201)
        [post] = self.stand_in.posts()
        self.assertEqual(post.body, body)
        self.assertEqual((post.headers["content-length"], post.headers["content-type"]),
                         (str(len(body)), "application/json"))
        self.assertNotIn("transfer-encoding", post.headers)

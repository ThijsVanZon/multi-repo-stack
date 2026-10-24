import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

from mrs import pages
from mrs.pages import Stop
from tests.page_stand_in import (DROP, STALL, SYNTHETIC_TOKEN, DeadProxy, StandIn, answer, gh_environment,
                                 redirect)
from tests.support import ROOT, GitTestCase, kill_tree, out, refs, shared_snapshot, start_kernel

SLUG = "example/app"
LISTING = f"/repos/{SLUG}/releases?per_page=100&page="


def create_body(tag: str = "26.1.0", notes: str = "Notes \u00fc \u2014\n\nend\n") -> bytes:
    """A create request shaped as prepare serializes it."""
    return (json.dumps({"tag_name": tag, "target_commitish": "c" * 40, "name": f"app {tag}", "body": notes,
                        "draft": False, "prerelease": False, "generate_release_notes": False},
                       indent=2, ensure_ascii=True) + "\n").encode("ascii")


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
        for request in self.stand_in.wire + self.other.wire:
            self.assertIn(request.method, ("GET", "POST"), "only reads and the one create are ever sent")
            self.assertFalse(request.path.endswith("/releases/latest"), "latest is never read")

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

    def test_gh_in_the_working_directory_or_a_relative_path_entry_is_never_run(self):
        """§4.2 credential: only absolute PATH entries name gh; a gh planted in the working directory (Windows
        searches it implicitly) or behind a relative entry such as `.` never supplies the token."""
        planted = self.tmp / "planted"
        planted.mkdir()
        fake = planted / ("gh.exe" if os.name == "nt" else "gh")
        fake.write_bytes(b"#!/bin/sh\necho gho_PlantedFixtureToken\n")
        fake.chmod(0o755)
        cwd = os.getcwd()
        os.chdir(planted)
        try:
            for name, path in (("working directory", os.environ["PATH"]),
                               ("relative entry", os.curdir + os.pathsep + os.environ["PATH"])):
                with self.subTest(name), mock.patch.dict(os.environ, {"PATH": path}):
                    self.assertEqual(pages.credential(), SYNTHETIC_TOKEN)
        finally:
            os.chdir(cwd)

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
        exactly one request on the wire; the create's failure is reported by its class only. A body cut short of
        its Content-Length, one over the size limit (though valid JSON) and JSON nested beyond the recursion limit
        are unqualified too."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
        refused = pages.call(dead, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases", b"{}")
        # Windows retries a refused loopback connect, so there the shortened timeout may end it first.
        self.assertEqual(refused.status, None)
        self.assertIn(refused.failure, ("ConnectionRefusedError", "TimeoutError"))
        with self.assertRaises(Stop):
            pages.read(dead, SYNTHETIC_TOKEN, "/user", "visibility")

        big = answer("200 OK", b'"' + b"a" * (pages.MAX_BODY - 1) + b'"')  # valid JSON, MAX_BODY + 1 bytes
        failures = {"dropped": DROP, "stalled": STALL, "malformed status": b"HTTP/1.1 2OO OK\r\n\r\n",
                    "not JSON": answer("200 OK", b"[{]"), "NaN": answer("200 OK", b"[NaN]"),
                    "duplicate keys": answer("200 OK", b'{"login": "a", "login": "b"}'),
                    "BOM": answer("200 OK", b"\xef\xbb\xbf[]"), "trailing data": answer("200 OK", b"[] []"),
                    "encoded": answer("200 OK", b"[]", "Content-Encoding: gzip"), "oversized": big,
                    "cut short": b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n[]",
                    "nested too deep": answer("200 OK", b"[" * 200000)}
        calls = 0
        for name, raw in failures.items():
            with self.subTest(name):
                self.stand_in.raw = {"GET /user": raw}
                with self.assertRaises(Stop) as stopped:
                    pages.read(self.stand_in.base, SYNTHETIC_TOKEN, "/user", "visibility")
                self.assertEqual((stopped.exception.status, stopped.exception.reason), ("UNKNOWN", "visibility"))
                calls += 1
        self.stand_in.raw = {"GET /user": answer("200 OK", b'"' + b"a" * (pages.MAX_BODY - 2) + b'"')}
        self.assertEqual(len(pages.read(self.stand_in.base, SYNTHETIC_TOKEN, "/user", "visibility")[1].body),
                         pages.MAX_BODY, "a complete body of exactly the limit qualifies")
        calls += 1
        expected = {"dropped": ("RemoteDisconnected", None), "stalled": ("TimeoutError", None),
                    "malformed status": ("BadStatusLine", None), "cut short": ("IncompleteRead", None)}
        for name, (failure, status) in expected.items():
            with self.subTest(create=name):
                self.stand_in.raw = {f"POST /repos/{SLUG}/releases": failures[name]}
                sent = pages.call(self.stand_in.base, SYNTHETIC_TOKEN, "POST", f"/repos/{SLUG}/releases", b"{}")
                self.assertEqual((sent.failure, sent.status), (failure, status))
                calls += 1
        self.one_request_per_connection(self.stand_in, calls)
        self.assertEqual(len(self.stand_in.posts()), 4)

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


TAG, TITLE, PRINCIPAL = "26.1.0", "example/app 26.1.0", "fixture-owner"
NOTES = "Notes ü —\n\nend\n".encode("utf-8")
CREATE = f"/repos/{SLUG}/releases"


class PageScenarioTestCase(PageTestCase):
    """A disposable target with contract release N, and notes for its page. The target's refs must be unchanged
    after every scenario unless it names a scripted provider side effect."""

    refs_change = False

    def setUp(self) -> None:
        super().setUp()
        self.target, _, _ = self.bootstrapped()
        self.release(self.target)
        self.before = refs(self.target)
        self.addCleanup(lambda: self.refs_change or self.assertEqual(refs(self.target), self.before))
        self.notes = self.tmp / "notes.md"
        self.notes.write_bytes(NOTES)

    def prepared(self, name: str = "page op", **changes) -> tuple[Path, str]:
        work = self.tmp / name
        digest, _ = pages.prepare(**{"destination": str(self.target), "tag": TAG, "title": TITLE,
                                     "notes": self.notes, "principal": PRINCIPAL, "work": work,
                                     "api": self.stand_in.base, **changes})
        return work, digest

    @staticmethod
    def outcome(function, *args) -> dict:
        try:
            return function(*args)
        except Stop as exc:
            return {"status": exc.status, "reason": exc.reason, "detail": exc.detail}

    def assertOutcome(self, report: dict, status: str, reason: str | None = None) -> None:
        self.assertEqual((report["status"], report["reason"] if reason else None), (status, reason), report)

    def published(self, body: str = NOTES.decode("utf-8"), **changes) -> dict:
        return self.stand_in.page(TAG, body, **changes)

    def mrs(self, *args) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "mrs"), "page", *map(str, args)],
                              capture_output=True)
        return proc.returncode, proc.stdout.decode("utf-8") + proc.stderr.decode("utf-8")

    def applying(self, work: Path, digest: str):
        """An apply through the CLI in its own process tree, so a test can run two at once or kill one."""
        return start_kernel("from mrs import cli; sys.exit(cli.main(sys.argv[2:]))", "page", "apply", "--work",
                            str(work), "--operation-sha256", digest, "--json")


class PagePreparationTests(PageScenarioTestCase):
    def test_prepare_seals_one_exact_request_and_sends_nothing(self):
        """§4.1: prepare reads Git only and writes the exact notes, the request in fixed key order (ASCII-escaped,
        one LF), page.json whose SHA-256 is the digest, and the sealed store; no provider request."""
        work, digest = self.prepared()
        record = json.loads((work / "page.json").read_bytes())
        self.assertEqual(digest, pages._sha((work / "page.json").read_bytes()))
        self.assertEqual((work / "notes.md").read_bytes(), NOTES)
        self.assertEqual((work / "request.json").read_bytes(), (json.dumps(
            {"tag_name": TAG, "target_commitish": record["candidate"], "name": TITLE, "body": NOTES.decode("utf-8"),
             "draft": False, "prerelease": False, "generate_release_notes": False},
            indent=2, ensure_ascii=True) + "\n").encode("ascii"))
        self.assertEqual(record["tag_object"], self.before[f"refs/tags/{TAG}"])
        self.assertEqual((record["repository"], record["principal"], record["api_version"]),
                         (SLUG, PRINCIPAL, "2022-11-28"))
        self.assertEqual(sorted(p.name for p in work.iterdir()), ["notes.md", "page.json", "repo.git", "request.json"])
        self.assertEqual(self.stand_in.wire, [])

    def test_prepare_refuses_what_it_cannot_bind(self):
        """§4.1 prepare and §5 routing: each input that cannot be bound exactly is refused before any request."""
        sink = self.bare("sink.git")
        checkout = self.checkout("a checkout")
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "file").write_text("x", encoding="utf-8")
        cases = {
            "not a contract release": ({"tag": "26.2.0"}, "release"),
            "unreadable destination": ({"destination": str(self.tmp / "missing.git")}, "destination"),
            "redirected destination": ({}, "destination"),
            "notes not UTF-8": (b"\xff\n", "notes"), "notes with CR": (b"a\r\n", "notes"),
            "notes with NUL": (b"a\0\n", "notes"), "empty notes": (b"", "notes"),
            "multi-line title": ({"title": "one\ntwo"}, "title"),
            "local destination on the real API": ({"api": pages.API}, "repository"),
            "non-GitHub destination on the real API": ({"api": pages.API, "destination": "https://gitlab.com/a/b"},
                                                       "repository"),
            "non-empty work folder": ({"work": busy}, "work"),
            "work folder inside a checkout": ({"work": checkout / "op"}, "work"),
            "loopback API with a GitHub destination": ({"destination": f"https://github.com/{SLUG}"}, "api"),
            "another API": ({"api": "http://localhost:1234"}, "api"),
        }
        for number, (name, (change, reason)) in enumerate(cases.items()):
            with self.subTest(name):
                if isinstance(change, bytes):
                    self.notes.write_bytes(change)
                    change = {}
                if name == "redirected destination":
                    out("config", "--global", f"url.{sink}.insteadOf", str(self.target))
                try:
                    with self.assertRaises(Stop) as stopped:
                        self.prepared(f"op {number}", **change)
                finally:
                    self.notes.write_bytes(NOTES)
                    out("config", "--global", "--unset-all", f"url.{sink}.insteadOf", check=False)
                self.assertEqual((stopped.exception.status, stopped.exception.reason), ("REFUSED", reason))
        self.assertEqual((self.stand_in.wire, refs(sink)), ([], {}))


class PageBindingTests(PageScenarioTestCase):
    def test_only_the_granted_unchanged_operation_loads(self):
        """§4.1 load: a wrong digest, or page.json, request.json or notes.md changed after prepare, is refused by
        apply and reconcile before any request; nothing is repaired."""
        work, digest = self.prepared()
        self.assertOutcome(self.outcome(pages.apply, work, "0" * 64), "REFUSED", "grant")
        record = json.loads((work / "page.json").read_bytes())
        notes = NOTES + b"edited\n"
        request = (work / "request.json").read_bytes().replace(b"end\\n", b"end\\nedited\\n")
        edits = {
            "page.json title": {"page.json": pages._json({**record, "title": "Another title"})},
            "page.json key order": {"page.json": json.dumps(dict(reversed(record.items())), indent=2).encode() + b"\n"},
            "page.json repository": {"page.json": pages._json({**record, "repository": "other/app"})},
            "page.json routed elsewhere": {"page.json": pages._json(
                {**record, "api": pages.API, "destination": "https://github.com/other/app"})},
            "page.json tag object": {"page.json": pages._json({**record, "tag_object": record["candidate"]})},
            "request.json": {"request.json": (work / "request.json").read_bytes().replace(b"false", b"true")},
            "notes.md": {"notes.md": notes},
            "notes.md and request.json together": {"notes.md": notes, "request.json": request},
        }
        for name, files in edits.items():
            with self.subTest(name):
                original = {file: (work / file).read_bytes() for file in files}
                for file, data in files.items():
                    (work / file).write_bytes(data)
                try:
                    granted = pages._sha((work / "page.json").read_bytes())
                    self.assertOutcome(self.outcome(pages.apply, work, granted), "REFUSED", "record")
                    self.assertOutcome(self.outcome(pages.reconcile, work), "REFUSED", "record")
                finally:
                    for file, data in original.items():
                        (work / file).write_bytes(data)
        self.assertEqual((self.stand_in.wire, pages.attempt(work)), ([], "none"))
        self.assertOutcome(pages.apply(work, digest), "APPLIED")

    def test_the_validated_request_and_notes_are_the_ones_used(self):
        """§4.1 load: request.json swapped during apply does not change the bytes sent; notes.md changed after
        load does not change the readback reference, so a page matching the changed notes is DIVERGED."""
        work, digest = self.prepared()
        sealed = (work / "request.json").read_bytes()
        swapped = sealed.replace(b"end", b"END")
        self.stand_in.on_listing = lambda: (work / "request.json").write_bytes(swapped)
        self.assertOutcome(pages.apply(work, digest), "APPLIED")
        self.assertEqual([request.body for request in self.stand_in.posts()], [sealed])

        work, digest = self.prepared("second op")
        footer = NOTES + b"\n-- \nfooter\n"
        self.stand_in.releases = [self.stand_in.page(TAG, footer.decode("utf-8"))]
        self.stand_in.on_listing = lambda: (work / "notes.md").write_bytes(footer)
        for run in (lambda: pages.apply(work, digest), lambda: pages.reconcile(work)):
            (work / "notes.md").write_bytes(NOTES)
            report = self.outcome(run)
            self.assertOutcome(report, "DIVERGED", "page")
            self.assertIn(f"+{len(footer) - len(NOTES)} appended bytes", report["detail"])
        self.assertEqual(len(self.stand_in.posts()), 1)

    def test_another_running_tool_is_refused(self):
        """§4.1 load: an operation prepared by another tool checkout is refused by apply and reconcile."""
        snapshot, _ = shared_snapshot()
        work = self.tmp / "page op"
        code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from mrs import pages; "
                "print(pages.prepare(destination=sys.argv[2], tag=sys.argv[3], title=sys.argv[4], "
                "notes=Path(sys.argv[5]), principal=sys.argv[6], work=Path(sys.argv[7]), api=sys.argv[8])[0])")
        proc = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(snapshot), str(self.target), TAG, TITLE,
                               str(self.notes), PRINCIPAL, str(work), self.stand_in.base], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        digest = proc.stdout.decode("ascii").strip()
        self.assertOutcome(self.outcome(pages.apply, work, digest), "REFUSED", "tool")
        self.assertOutcome(self.outcome(pages.reconcile, work), "REFUSED", "tool")
        self.assertEqual(self.stand_in.wire, [])


class PageClassificationTests(PageScenarioTestCase):
    def assertNoCreate(self, work: Path, digest: str, status: str, reason: str) -> dict:
        """apply and reconcile both classify as expected; apply claims nothing and sends no POST."""
        report = self.outcome(pages.apply, work, digest)
        self.assertOutcome(report, status, reason)
        self.assertOutcome(self.outcome(pages.reconcile, work), status, reason)
        self.assertEqual((self.stand_in.posts(), pages.attempt(work)), ([], "none"))
        return report

    def test_absence_creates_only_with_the_bound_principals_full_visibility(self):
        """§4.2 visibility, §4.4 "No entry for N": the wrong login, no repo scope or scope header, no push, an
        unreadable /user or no usable credential is UNKNOWN with zero POSTs; then the same operation applies."""
        work, digest = self.prepared()
        cases = {
            "wrong login": ("login", "someone-else", "visibility"),
            "no repo scope": ("scopes", "workflow, read:org", "visibility"),
            "no scope header": ("scopes", None, "visibility"),
            "no push": ("push", False, "visibility"),
            "unreadable /user": ("raw", {"GET /user": answer("500 Internal Server Error", b"{}")}, "visibility"),
            "no credential": ("raw", {}, "credential"),
        }
        for name, (attribute, value, reason) in cases.items():
            with self.subTest(name):
                saved = getattr(self.stand_in, attribute)
                setattr(self.stand_in, attribute, value)
                env = {k: v for k, v in os.environ.items() if not (name == "no credential" and k == "GH_TOKEN")}
                with mock.patch.dict(os.environ, env, clear=True):
                    sent = len(self.stand_in.wire)
                    self.assertNoCreate(work, digest, "UNKNOWN", reason)
                    if name == "no credential":
                        self.assertEqual(len(self.stand_in.wire), sent, "no request without a credential")
                setattr(self.stand_in, attribute, saved)
        self.assertOutcome(pages.apply(work, digest), "APPLIED")

    def test_only_qualified_reads_count_and_never_as_absence(self):
        """§4.3: duplicate keys, NaN, a BOM, an unreadable second page, an incomplete entry for N, a non-integer
        status or a redirect on any read is UNKNOWN with zero POSTs and no request to the Location; a matching
        page on the second page is COMPLETED."""
        work, digest = self.prepared()
        self.releases(150)
        repo, first, second = f"GET /repos/{SLUG}", f"GET {LISTING}1", f"GET {LISTING}2"
        cases = {
            "duplicate keys": ({repo: answer("200 OK", b'{"full_name": "other/app", "full_name": "example/app", '
                                             b'"html_url": "https://github.com/example/app", "permissions": '
                                             b'{"push": true}}')}, "provider"),
            "NaN": ({first: answer("200 OK", b"[NaN]")}, "listing"),
            "BOM": ({"GET /user": answer("200 OK", b'\xef\xbb\xbf{"login": "fixture-owner"}',
                                         "X-OAuth-Scopes: repo")}, "visibility"),
            "unreadable second page": ({second: answer("502 Bad Gateway", b"{}")}, "listing"),
            "non-integer status": ({repo: b"HTTP/1.1 2OO OK\r\nContent-Length: 2\r\n\r\n{}"}, "provider"),
            "incomplete entry for N": ("incomplete", "listing"),
        }
        for code in (301, 302, 303, 307, 308):
            for key, reason in ((repo, "provider"), (first, "listing"), (second, "listing"),
                                ("GET /user", "visibility")):
                target = key.split(" ", 1)[1]
                cases[f"{code} on {target}"] = ({key: redirect(code, self.other.base + target)}, reason)
        for name, (raw, reason) in cases.items():
            with self.subTest(name):
                if raw == "incomplete":
                    entry, raw = self.published(id="1"), {}
                self.stand_in.raw = raw
                report = self.assertNoCreate(work, digest, "UNKNOWN", reason)
                self.assertNotIn("absent", report["reason"])
                self.stand_in.raw = {}
                if name == "incomplete entry for N":
                    self.stand_in.releases.remove(entry)
        self.published()
        self.assertEqual(len(self.stand_in.releases), 151)  # N's page is on page 2
        self.assertOutcome(pages.reconcile(work), "COMPLETED")
        self.assertOutcome(pages.apply(work, digest), "NOOP")
        self.assertEqual(self.stand_in.posts(), [])

    def test_a_page_that_is_not_the_sealed_one_is_diverged_and_never_edited(self):
        """§4.4: a draft, a duplicate, a changed body (the difference named), name, prerelease, assets or a page
        URL in another repository, another repository's name, and tag N moved or deleted before apply are
        DIVERGED; a redirected destination is UNKNOWN (git). Each sends zero POSTs."""
        work, digest = self.prepared()
        body = NOTES.decode("utf-8")
        cases = {
            "draft": ({"draft": True}, "draft", "draft"), "duplicate": ({}, "duplicate", "2 pages"),
            "appended footer": ({"body": body + "\n-- \nfooter\n"}, "page", "+12 appended bytes"),
            "trimmed final LF": ({"body": body[:-1]}, "page", "last 1 bytes trimmed"),
            "different name": ({"name": "example/app 26.1.1"}, "page", "name"),
            "prerelease": ({"prerelease": True}, "page", "prerelease"),
            "assets": ({"assets": [{"id": 1}]}, "page", "assets"),
            "page in another repository": ({"html_url": "https://github.com/other/app/releases/tag/26.1.0"}, "page",
                                           "html_url"),
        }
        for name, (changes, reason, named) in cases.items():
            with self.subTest(name):
                self.stand_in.releases = []
                self.published(**changes)
                if name == "duplicate":
                    self.published()
                self.assertIn(named, self.assertNoCreate(work, digest, "DIVERGED", reason)["detail"])
        self.stand_in.releases = []
        with self.subTest("another repository"):
            self.stand_in.full_name = "other/app"
            self.assertNoCreate(work, digest, "DIVERGED", "repository")
            self.stand_in.full_name = SLUG
        sink = self.bare("sink.git")
        with self.subTest("redirected destination"):
            out("config", "--global", f"url.{sink}.insteadOf", str(self.target))
            try:
                self.assertNoCreate(work, digest, "UNKNOWN", "git")
            finally:
                out("config", "--global", "--unset-all", f"url.{sink}.insteadOf")
        tag, candidate = self.before[f"refs/tags/{TAG}"], json.loads((work / "page.json").read_bytes())["candidate"]
        for name, change in (("tag moved", ["update-ref", f"refs/tags/{TAG}", candidate]),
                             ("tag deleted", ["update-ref", "-d", f"refs/tags/{TAG}"])):
            with self.subTest(name):
                out("-C", self.target, *change)
                try:
                    self.assertNoCreate(work, digest, "DIVERGED", "tag")
                finally:
                    out("-C", self.target, "update-ref", f"refs/tags/{TAG}", tag)
        self.assertEqual(self.stand_in.wire and {r.method for r in self.stand_in.wire}, {"GET"})


class PageCreateTests(PageScenarioTestCase):
    def creates(self) -> list:
        return [request for request in self.stand_in.posts() if request.path == CREATE]

    def test_201_applies_once_and_a_second_apply_is_noop(self):
        """§4.5: the validated request is sent once, without make_latest; result.json keeps exactly its fields;
        a second apply is NOOP and reconcile COMPLETED, with no further POST."""
        work, digest = self.prepared()
        report = pages.apply(work, digest)
        self.assertOutcome(report, "APPLIED", "matching")
        self.assertEqual(report["page"], {"id": 1000, "html_url": f"https://github.com/{SLUG}/releases/tag/{TAG}"})
        [post] = self.creates()
        self.assertEqual(post.body, (work / "request.json").read_bytes())
        self.assertNotIn("make_latest", json.loads(post.body))
        result = json.loads((work / "result.json").read_bytes())
        self.assertEqual(set(result), {"status", "request_id", "transport", "at"})
        self.assertEqual((result["status"], result["request_id"], result["transport"]),
                         (201, "C0DE:5EED:0001", "answered"))
        self.assertOutcome(pages.apply(work, digest), "NOOP")
        self.assertOutcome(pages.reconcile(work), "COMPLETED")
        self.assertEqual((len(self.creates()), pages.attempt(work)), (1, "claimed-with-result"))

    def test_every_uncertain_create_is_sent_exactly_once(self):
        """§4.5 steps 4-7: 422 is REFUSED; 5xx after storing is APPLIED; 5xx, a dropped connection, a timeout
        or a 3xx (not followed) without a stored page is UNKNOWN; a drop after storing whose readback fails is
        UNKNOWN and reconciles COMPLETED. Each puts exactly one POST on the wire, and a later reconcile and apply
        add none."""
        self.stand_in.redirect_to = self.other.base + CREATE
        failing_readback = {f"GET /repos/{SLUG}": answer("503 Service Unavailable", b"{}")}
        cases = {"422": ("REFUSED", "http-422", 422, "answered", "NOT_APPLIED"),
                 "500-stored": ("APPLIED", "matching", 500, "answered", "COMPLETED"),
                 "500": ("UNKNOWN", "unconfirmed", 500, "answered", "NOT_APPLIED"),
                 "drop-stored": ("UNKNOWN", "provider", None, "RemoteDisconnected", "COMPLETED"),
                 "drop": ("UNKNOWN", "unconfirmed", None, "RemoteDisconnected", "NOT_APPLIED"),
                 "stall": ("UNKNOWN", "unconfirmed", None, "TimeoutError", "NOT_APPLIED"),
                 **{code: ("UNKNOWN", "unconfirmed", int(code), "answered", "NOT_APPLIED")
                    for code in ("301", "302", "303", "307", "308")}}
        for mode, (status, reason, code, transport, later) in cases.items():
            with self.subTest(mode):
                self.stand_in.releases, self.stand_in.wire, self.stand_in.post = [], [], mode
                if mode == "drop-stored":
                    self.stand_in.on_post = lambda: self.stand_in.raw.update(failing_readback)
                work, digest = self.prepared(f"op {mode}")
                self.assertOutcome(self.outcome(pages.apply, work, digest), status, reason)
                self.stand_in.raw, self.stand_in.on_post = {}, None
                result = json.loads((work / "result.json").read_bytes())
                self.assertEqual((result["status"], result["transport"]), (code, transport))
                self.assertOutcome(pages.reconcile(work), later)
                again = self.outcome(pages.apply, work, digest)
                self.assertOutcome(again, "NOOP") if later == "COMPLETED" else \
                    self.assertOutcome(again, "REFUSED", "attempted")
                self.assertEqual(len(self.creates()), 1)
                self.assertEqual(pages.attempt(work), "claimed-with-result")
        self.assertEqual(self.other.connections, 0)

    def test_a_claim_spends_the_operation_even_when_interrupted(self):
        """§4.5 steps 3 and 7: a pre-existing claim (also an empty one, as from a crash before the send) is
        REFUSED (attempted) with no POST; an apply killed after its send leaves the claim without a result,
        and neither a later apply nor reconcile sends again."""
        for name, data in (("empty claim", b""), ("claim without send", b'{"operation_sha256": "x"}\n')):
            with self.subTest(name):
                work, digest = self.prepared(f"op {name}")
                (work / "claim.json").write_bytes(data)
                self.assertOutcome(self.outcome(pages.apply, work, digest), "REFUSED", "attempted")
                self.assertEqual(pages.attempt(work), "claimed-without-result")
                self.assertEqual(self.stand_in.posts(), [])
        work, digest = self.prepared("op killed")
        sent = threading.Event()
        self.stand_in.post, self.stand_in.on_post = "stall", sent.set
        proc = self.applying(work, digest)
        try:
            self.assertTrue(sent.wait(120), "the create never arrived")
        finally:
            kill_tree(proc)
        self.assertEqual(pages.attempt(work), "claimed-without-result")
        self.assertOutcome(self.outcome(pages.apply, work, digest), "REFUSED", "attempted")
        self.assertOutcome(pages.reconcile(work), "NOT_APPLIED")
        self.assertEqual(len(self.creates()), 1)

    def test_concurrent_applies_send_one_create(self):
        """§4.5 step 3: two applies that both classify NOT_APPLIED race for the exclusive claim; exactly one
        POST is sent and the other apply sends nothing."""
        work, digest = self.prepared()
        barrier, listings = threading.Barrier(2, timeout=120), []

        def both_classified():
            listings.append(1)
            if len(listings) <= 2:
                barrier.wait()
        self.stand_in.on_listing = both_classified
        runs = [self.applying(work, digest) for _ in range(2)]
        reports = [json.loads(proc.communicate(timeout=300)[0]) for proc in runs]
        self.assertEqual(sorted(report["status"] for report in reports)[0], "APPLIED", reports)
        self.assertIn(sorted(report["status"] for report in reports)[1], ("NOOP", "REFUSED"), reports)
        self.assertEqual(len(self.creates()), 1)

    def test_nothing_sensitive_is_persisted_or_printed(self):
        """§4.5 step 5, §4.2 credential: a 422 message carrying synthetic credentials, the synthetic token itself
        and a credential-shaped request id appear neither in any file of the operation folder nor in the printed
        report; result.json keeps exactly its fields, with the unsafe request id as null."""
        secrets = ["ghp_" + "A" * 36, "fixtureBearerSecret", "fixture-user:fixture-pass", SYNTHETIC_TOKEN,
                   "ghp_" + "B" * 36]
        self.stand_in.post, self.stand_in.request_id = "422", f"token {secrets[4]}"
        self.stand_in.refusal = (f"Bad credentials {secrets[0]}; Authorization: Bearer {secrets[1]}; "
                                 f"see https://{secrets[2]}@example.invalid/x with token {secrets[3]}\nsecond line")
        work, digest = self.prepared()
        code, printed = self.mrs("apply", "--work", work, "--operation-sha256", digest)
        self.assertEqual(code, 3, printed)
        self.assertIn("REFUSED (http-422)", printed)
        self.assertIn("Bad credentials [redacted]", printed)
        printed += self.mrs("reconcile", "--work", work, "--json")[1]
        stored = b"".join(path.read_bytes() for path in work.rglob("*") if path.is_file())
        for secret in secrets + ["second line"]:
            self.assertNotIn(secret, printed)
            self.assertNotIn(secret.encode("ascii"), stored)
        result = json.loads((work / "result.json").read_bytes())
        self.assertEqual(set(result), {"status", "request_id", "transport", "at"})
        self.assertIsNone(result["request_id"], "a request id that is not [0-9A-Fa-f:]{1,64} is never kept")


class PageMalformedAnswerTests(PageScenarioTestCase):
    def test_malformed_provider_answers_are_unknown_also_after_the_create(self):
        """§4.3, §5 exit codes: a listing nested beyond the recursion limit, or N's page body holding a lone
        surrogate, is UNKNOWN (listing) with exit 4, never a crash: in reconcile and in apply's readback after its
        one POST, which no later command resends."""
        nested = {f"GET {LISTING}1": answer("200 OK", b"[" * 200000)}
        cases = {"nested": lambda: self.stand_in.raw.update(nested),
                 "lone surrogate": lambda: self.published(body="\ud800 notes")}
        for name, malformed in cases.items():
            with self.subTest(name):
                self.stand_in.releases, self.stand_in.raw, self.stand_in.wire = [], {}, []
                work, digest = self.prepared(f"op {name}")

                def run(*args) -> tuple[int, dict]:
                    code, printed = self.mrs(*args, "--work", work, "--json")
                    return code, json.loads(printed)
                malformed()
                code, report = run("reconcile")
                self.assertEqual((code, report["status"], report["reason"]), (4, "UNKNOWN", "listing"), report)
                self.stand_in.releases, self.stand_in.raw = [], {}
                self.stand_in.on_post = malformed
                code, report = run("apply", "--operation-sha256", digest)
                self.stand_in.on_post = None
                self.assertEqual((code, report["status"], report["reason"], report["attempt"]),
                                 (4, "UNKNOWN", "listing", "claimed-with-result"), report)
                self.assertEqual(run("reconcile")[0], 4)
                self.stand_in.raw = {}
                self.stand_in.releases = [r for r in self.stand_in.releases if "\ud800" not in r["body"]]
                self.assertEqual(run("apply", "--operation-sha256", digest)[1]["status"], "NOOP")
                self.assertEqual(len(self.stand_in.posts()), 1)


class PageRaceAndReadOnlyTests(PageScenarioTestCase):
    def test_tag_disappearance_race_is_reported_never_repaired(self):
        """§4.6: tag N deleted after apply's tag read and before the POST, and recreated by the provider as a
        lightweight tag at C (or not at all), is DIVERGED (tag) after exactly one POST; nothing repairs the tag
        or resends, and reconcile keeps reporting DIVERGED (tag)."""
        self.refs_change = True
        name = f"refs/tags/{TAG}"
        for recreated in (True, False):
            with self.subTest(recreated=recreated):
                out("-C", self.target, "update-ref", name, self.before[name])
                self.stand_in.releases, self.stand_in.wire = [], []
                work, digest = self.prepared(f"op {recreated}")
                candidate = json.loads((work / "page.json").read_bytes())["candidate"]
                self.stand_in.on_listing = lambda: (out("-C", self.target, "update-ref", "-d", name),
                                                    setattr(self.stand_in, "on_listing", None))
                self.stand_in.on_post = (lambda: out("-C", self.target, "update-ref", name, candidate)) \
                    if recreated else None
                self.assertOutcome(self.outcome(pages.apply, work, digest), "DIVERGED", "tag")
                self.assertOutcome(self.outcome(pages.reconcile, work), "DIVERGED", "tag")
                self.assertEqual(len(self.stand_in.posts()), 1)
                expected = {**self.before, name: candidate} if recreated else \
                    {key: value for key, value in self.before.items() if key != name}
                self.assertEqual(refs(self.target), expected)

    def test_reconcile_only_reads(self):
        """§4.7: across the absent, completed, diverged, unknown and claimed states, reconcile sends only GETs and
        leaves the target's refs, the whole operation folder (repo.git included: names, bytes, modification
        times) and the temporary folder unchanged."""
        work, digest = self.prepared()
        temporary = Path(tempfile.gettempdir())

        def folder() -> dict:
            return {path.relative_to(work).as_posix(): (path.is_file() and path.read_bytes(),
                                                        path.stat().st_mtime_ns) for path in work.rglob("*")}

        def unknown():
            self.stand_in.releases, self.stand_in.push = [], False

        def claimed():
            self.stand_in.push = True
            self.assertOutcome(pages.apply(work, digest), "APPLIED")
        for name, setting, status in (("absent", lambda: None, "NOT_APPLIED"),
                                      ("diverged", lambda: self.published(draft=True), "DIVERGED"),
                                      ("unknown", unknown, "UNKNOWN"), ("completed and claimed", claimed, "COMPLETED")):
            with self.subTest(name):
                setting()
                before = (refs(self.target), folder(), sorted(temporary.glob("mrs-page-*")), len(self.stand_in.wire))
                self.assertOutcome(self.outcome(pages.reconcile, work), status)
                self.assertEqual((refs(self.target), folder(), sorted(temporary.glob("mrs-page-*"))), before[:3])
                self.assertEqual({r.method for r in self.stand_in.wire[before[3]:]}, {"GET"})
        self.assertEqual(len(self.stand_in.posts()), 1)


class PageCommandTests(PageScenarioTestCase):
    def test_the_commands_report_each_outcome_with_its_exit_code(self):
        """§5 interface and P5: page prepare refuses a non-GitHub destination (3); reconcile is NOT_APPLIED (1),
        UNKNOWN (4), DIVERGED (3); apply under a wrong grant is REFUSED (3), then APPLIED (0) and NOOP (0) with
        ambient GH_HOST at the second stand-in, which receives nothing; reconcile is COMPLETED (0)."""
        os.environ["GH_HOST"] = self.other.base.removeprefix("http://")
        code, printed = self.mrs("prepare", "--repository", self.target, "--tag", TAG, "--title", TITLE, "--notes",
                                 self.notes, "--principal", PRINCIPAL, "--work", self.tmp / "cli op", "--json")
        self.assertEqual((code, json.loads(printed)["reason"]), (3, "repository"), printed)
        work, digest = self.prepared()

        def run(*args) -> tuple[int, dict]:
            code, printed = self.mrs(*args, "--work", work, "--json")
            return code, json.loads(printed)
        self.assertEqual(run("reconcile")[0], 1)
        self.stand_in.push = False
        self.assertEqual(run("reconcile")[0], 4)
        self.stand_in.push = True
        self.published(draft=True)
        self.assertEqual(run("reconcile")[0], 3)
        self.stand_in.releases = []
        self.assertEqual(run("apply", "--operation-sha256", "0" * 64)[0], 3)
        code, report = run("apply", "--operation-sha256", digest)
        self.assertEqual((code, report["status"], report["attempt"]), (0, "APPLIED", "claimed-with-result"))
        self.assertEqual(run("apply", "--operation-sha256", digest)[1]["status"], "NOOP")
        code, printed = self.mrs("reconcile", "--work", work)
        self.assertEqual(code, 0, printed)
        self.assertIn(f"page      1000 https://github.com/{SLUG}/releases/tag/{TAG}", printed)
        self.assertEqual(len(self.stand_in.posts()), 1)

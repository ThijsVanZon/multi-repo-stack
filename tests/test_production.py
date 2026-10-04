import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mrs import transactions
from mrs.transactions import Refused
from tests.consumer_support import IMPLEMENTATION, NATIVE, ConsumerTestCase, selector, shape_files
from tests.support import (AMBIENT_GLOBAL_CONFIG, DEV, MAIN, RELEASER, git, has_object, kill_tree, out, refs,
                           receiver_capabilities, shared_snapshot, simulated_acceptance, tree_digest, wait_for)

TAG = "refs/tags/26.1.0"
IDENTITY = "Fixture Releaser <fixture-releaser@example.invalid>"
COMPETING_DEV = """( unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  old=$(git rev-parse refs/heads/dev)
  new=$(echo "competing integration" | git commit-tree "$old^{tree}" -p "$old")
  git update-ref refs/heads/dev "$new" "$old" && echo "$new" > fixture-competitor )
exit 0
"""


def file_url(path: Path) -> str:
    posix = path.as_posix()
    return "file://" + ("" if posix.startswith("/") else "/") + posix


class RealTargetTestCase(ConsumerTestCase):
    """The checked production path through the public commands: unmarked task-owned bare repositories (real
    targets, not disposable-fixture destinations), changed only by `mrs operation apply` run from the clean shared
    snapshot that the consumers select. Their verdicts are SIMULATED (consumer_support): no real acceptance."""

    def prepare_bootstrap(self, target, source: Path, commit: str, name: str) -> tuple[int, dict]:
        return self.tool("prepare", "bootstrap", "--repository", target, "--source", source, "--commit", commit,
                         "--pstack", self.pstack(), "--work", self.tmp / name)

    def operation(self, verb: str, name: str, checkout: Path | None = None) -> tuple[int, dict]:
        return self.tool("operation", verb, "--work", self.tmp / name, checkout=checkout)

    def applied(self, name: str) -> str:
        code, report = self.operation("apply", name)
        self.assertEqual((code, report["status"]), (0, "APPLIED"), report)
        return report["reason"]

    def real_consumer(self, name: str = "app") -> tuple[Path, Path, str]:
        source = self.checkout(f"{name} source")
        b = self.commit(source, {**shape_files("implementation"), "VERSION": "26.1.0\n", "README.md": "# app\n",
                                 "multi-repo-stack.json": selector("example/app", IMPLEMENTATION, (NATIVE,),
                                                                   shared_snapshot()[1])}, "Prepare the app")
        target = self.bare(f"{name} target.git", fixture=False)
        self.assertEqual(self.prepare_bootstrap(target, source, b, f"{name} bootstrap op")[0], 0)
        self.applied(f"{name} bootstrap op")
        return target, source, b

    def prepare_release(self, target, name: str, candidate: str, records, verdict: dict | None) -> tuple[int, dict]:
        args = ["prepare", "release", "--repository", target, "--candidate", candidate, "--pstack", self.pstack(),
                "--identity", IDENTITY, "--work", self.tmp / name]
        for record in records:
            args += ["--record", record]
        if verdict is not None:
            args += ["--attestation", self.write_json(f"{name} verdict.json", verdict)]
        return self.tool(*args)

    def accepted(self, target, name: str) -> tuple[Path, dict]:
        """A record collected on exact dev and a SIMULATED PASS verdict for it."""
        _, report, record = self.collect(target, name)
        self.assertEqual(report["status"], "PASSED", report)
        return record, self.verdict(record)

    def mirror(self, target: Path, name: str) -> Path:
        copy = self.tmp / name
        out("clone", "--quiet", "--mirror", target, copy)
        out("-C", copy, "remote", "remove", "origin")
        return copy


class ProductionBootstrapTests(RealTargetTestCase):
    def test_bootstrap_is_prepared_inspected_applied_and_reconciled_through_the_commands(self):
        """Bootstrap: on an unmarked empty target, preparation publishes nothing, inspection shows the exact guarded
        push, apply creates only dev at exact B, and reconciliation and a repeat keep it; the dirty caller is
        unchanged. A file:// URL target behaves the same; nonempty and unreadable targets refuse or stay UNKNOWN."""
        source, b = self.lifecycle_source(name="caller source")
        out("-C", source, "checkout", "--quiet", "-b", "26.1.0/local-task")
        (source / "README.md").write_text("# staged change\n", encoding="utf-8")
        out("-C", source, "add", "README.md")
        (source / "untracked ü.txt").write_text("untracked\n", encoding="utf-8")
        caller = tree_digest(source)
        for label, target, remote in (("path", self.bare("path target.git", fixture=False), None),
                                      ("file URL", self.bare("url target.git", fixture=False), "url")):
            with self.subTest(label):
                remote = file_url(target) if remote else target
                code, prepared = self.prepare_bootstrap(remote, source, b, f"{label} op")
                self.assertEqual((code, prepared["status"]), (0, "PREPARED"), prepared)
                self.assertEqual(refs(target), {}, "preparation must not publish")
                code, inspected = self.operation("inspect", f"{label} op")
                self.assertEqual((code, inspected["status"]), (0, "VALID"))
                self.assertEqual(inspected["push"][3:], [
                    "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", "--no-force-if-includes",
                    f"--force-with-lease={DEV}:", "--", str(remote), f"{b}:{DEV}"])
                self.assertEqual(self.operation("reconcile", f"{label} op")[1]["status"], "NOT_APPLIED")

                self.assertEqual(self.applied(f"{label} op"), "applied")

                self.assertEqual(refs(target), {DEV: b})
                self.assertEqual(self.operation("reconcile", f"{label} op")[1]["status"], "COMPLETED")
                self.assertEqual(self.operation("apply", f"{label} op")[1]["status"], "NOOP")
                self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(tree_digest(source), caller)

        nonempty = self.bare("nonempty.git", fixture=False)
        out("-C", source, "push", "--quiet", "--no-follow-tags", nonempty, f"{b}:refs/heads/readme")
        corrupt = self.bare("corrupt.git", fixture=False)
        (corrupt / "packed-refs").write_text("garbage line\n", encoding="utf-8")
        for name, target, expected in (
                ("nonempty", nonempty, (3, "REFUSED")), ("unreadable refs", corrupt, (4, "UNKNOWN")),
                # an https target is observed only with its own transport; this test allows file only, so Git
                # refuses the transport before any connection or name lookup
                ("https URL", "https://example.invalid/owner/app.git", (4, "UNKNOWN"))):
            with self.subTest(name):
                code, report = self.prepare_bootstrap(target, source, b, f"{name} op")
                self.assertEqual((code, report["status"]), expected, report)
                self.assertFalse((self.tmp / f"{name} op" / "operation.json").exists())
        self.assertIn("transport 'https' not allowed", report["reason"])
        self.assertEqual(refs(nonempty), {"refs/heads/readme": b})
        self.assertFalse((corrupt / "refs" / "heads" / "dev").exists())

    def test_a_real_bootstrap_publishes_only_dev_despite_ambient_tags_and_submodules(self):
        """Ref allowlist: with ambient follow-tags and on-demand submodule pushing, an unrelated reachable annotated
        tag in the operation store and an unpublished submodule commit in B, only dev changes on the target and the
        separate submodule repository is untouched."""
        sub_remote = self.bare("submodule remote.git", fixture=False)
        seed = self.checkout("submodule seed")
        self.commit(seed, {"lib.txt": "v1\n"}, "Published submodule commit")
        out("-C", seed, "push", "--quiet", sub_remote, "HEAD:refs/heads/main")
        out("-C", sub_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        source, _ = self.lifecycle_source(name="app with submodule")
        out("-C", source, "tag", "-a", "unrelated-annotated", "-m", "unrelated reachable tag", "HEAD")
        out("-C", source, "-c", "protocol.file.allow=always", "submodule", "--quiet", "add", sub_remote, "lib")
        unpublished = self.commit(source / "lib", {"lib.txt": "v2\n"}, "Unpublished submodule commit")
        b = self.commit(source, {}, "Pin the unpublished submodule commit")
        target, sub_before = self.bare("target.git", fixture=False), refs(sub_remote)
        self.assertEqual(self.prepare_bootstrap(target, source, b, "op")[0], 0)
        out("-C", self.tmp / "op" / "repo.git", "fetch", "--quiet", "--no-tags", source,
            "refs/tags/unrelated-annotated:refs/tags/unrelated-annotated")

        self.applied("op")

        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(refs(sub_remote), sub_before)
        self.assertFalse(has_object(sub_remote, unpublished))

    def test_a_direct_library_push_refuses_a_bootstrap_whose_target_is_no_longer_empty(self):
        """Direct library calls: with the saved bootstrap unchanged, a main created on the target before application
        makes apply and a direct push by the preparing tool refuse it as nonempty, with no push attempted and the
        target unchanged. Once the target is empty again, the same direct push creates only dev at B."""
        source, b = self.lifecycle_source(name="bootstrap source")
        target = self.bare("target.git", fixture=False)
        self.assertEqual(self.prepare_bootstrap(target, source, b, "op")[0], 0)
        out("-C", target, "fetch", "--quiet", "--no-tags", source, b)
        out("-C", target, "update-ref", MAIN, b)

        code, report = self.operation("apply", "op")
        pushed = self.checked("push", self.tmp / "op")

        self.assertEqual((code, report["status"], report["reason"], pushed),
                         (3, "REFUSED", "nonempty", "Refused: nonempty"))
        self.assertFalse((self.tmp / "op" / "attempts.log").exists(), "no push may be attempted")
        self.assertEqual(refs(target), {MAIN: b})
        out("-C", target, "update-ref", "-d", MAIN, b)
        self.assertEqual(self.checked("push", self.tmp / "op"), "success")
        self.assertEqual(refs(target), {DEV: b})

    def test_malformed_or_password_destinations_are_refused_without_repeating_them(self):
        """Destinations: a URL that cannot be parsed, and https, ssh and file URLs carrying a dummy password, are
        refused as destinations by both preparations, with a clear reason that does not repeat the password and no
        traceback. No operation folder is written and the source is unchanged."""
        source, b = self.lifecycle_source(name="input source")
        caller = tree_digest(source)
        secret = "DUMMY_NOT_A_SECRET"
        urls = {"unparseable": "https://[unbalanced/owner/app.git",
                "https password": f"https://user:{secret}@example.invalid/owner/app.git",
                "ssh password": f"ssh://git:{secret}@example.invalid/owner/app.git",
                "file password": f"file://user:{secret}@localhost/srv/app.git",
                "password and newline": f"https://user:{secret}@example.invalid/owner/app.git\n"}
        for name, url in urls.items():
            for kind, args in (("bootstrap", ["--source", source, "--commit", b]),
                               ("release", ["--candidate", b, "--identity", IDENTITY])):
                with self.subTest(name, kind=kind):
                    work = self.tmp / f"{name} {kind} op"
                    proc = subprocess.run([sys.executable, "-I", "-B", str(shared_snapshot()[0] / "mrs"), "prepare",
                                           kind, "--repository", url, *map(str, args), "--pstack", str(self.pstack()),
                                           "--work", str(work), "--json"], capture_output=True)
                    report = json.loads(proc.stdout.decode("utf-8"))
                    self.assertEqual((proc.returncode, report["status"], proc.stderr), (3, "REFUSED", b""), report)
                    self.assertTrue(report["reason"].startswith("destination: the destination is "), report)
                    self.assertNotIn(secret.encode(), proc.stdout + proc.stderr)
                    self.assertFalse(work.exists())
        self.assertEqual(tree_digest(source), caller)


class ProductionReleaseTests(RealTargetTestCase):
    def test_first_and_later_releases_through_the_commands_keep_exact_identities_and_history(self):
        """First release / Later release / Retries: on a file:// URL target, a SUFFICIENT assessment of exact C
        prepares T and D without publishing; apply moves main to C, creates T directly on C and moves dev to D,
        which changes only VERSION and records today's UTC opening date. Repeats are no-ops before and after dev
        advances and after 26.2.0 is released; the 26.1.0 tag never changes."""
        target, _, _ = self.real_consumer()
        url = file_url(target)
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(url, "first")
        code, prepared = self.prepare_release(url, "first op", c, [record], verdict)
        self.assertEqual((code, prepared["status"]), (0, "PREPARED"), prepared)
        operation = prepared["operation"]
        t, d = operation["tag"], operation["next"]["commit"]
        self.assertEqual(refs(target), {DEV: c}, "preparation must not publish")
        self.assertEqual(self.operation("inspect", "first op")[1]["push"][-4:],
                         [url, f"{d}:{DEV}", f"{c}:{MAIN}", f"{t}:{TAG}"])  # as saved: sorted by ref
        today = datetime.now(timezone.utc).date()

        self.assertEqual(self.applied("first op"), "applied")

        self.assertEqual(refs(target), {MAIN: c, TAG: t, DEV: d})
        self.assertEqual(out("-C", target, "cat-file", "-t", t), "tag")
        self.assertEqual(out("-C", target, "rev-parse", f"{t}^{{commit}}"), c)
        self.assertIn(f"object {c}\ntype commit\ntag 26.1.0\n", out("-C", target, "cat-file", "tag", t))
        self.assertEqual(out("-C", target, "rev-list", "--parents", "-n", "1", d), f"{d} {c}")
        self.assertEqual(out("-C", target, "diff-tree", "-r", "--name-status", "--no-commit-id", c, d), "M\tVERSION")
        expected = "26.2.0" if today.year == 2026 else f"{today.year % 100}.1.0"
        self.assertEqual(git("-C", target, "cat-file", "blob", f"{d}:VERSION").stdout, f"{expected}\n".encode())
        self.assertIn(f"Next-line opening date (UTC): {today.isoformat()}", out("-C", target, "log", "-1",
                                                                                 "--format=%B", d))
        self.assertEqual(self.operation("reconcile", "first op")[1]["status"], "COMPLETED")
        self.assertEqual(self.operation("apply", "first op")[1]["status"], "NOOP")

        c2 = self.integrate(target, {"docs/next.md": "next-line task\n"}, "Integrate a next-line task")
        self.assertEqual(self.operation("apply", "first op")[1]["status"], "NOOP")
        record2 = self.accepted(url, "second")[0]
        criteria = json.loads(record2.read_text(encoding="utf-8"))["criteria"]
        verdict2 = self.verdict(record2, criteria={"identity": criteria, "previous": criteria,
                                                   "assessment": "SIMULATED: criteria unchanged since 26.1.0"})
        code, second = self.prepare_release(url, "second op", c2, [record2], verdict2)
        self.assertEqual(code, 0, second)
        self.applied("second op")

        t2, d2 = second["operation"]["tag"], second["operation"]["next"]["commit"]
        self.assertEqual(refs(target), {MAIN: c2, TAG: t, f"refs/tags/{expected}": t2, DEV: d2})
        self.assertEqual(self.operation("apply", "first op")[1]["status"], "NOOP")
        code, inspected = self.tool("inspect", "--remote", url)
        self.assertEqual((code, inspected["kind"]), (0, "LIFECYCLE"))
        self.assertEqual([release["tag"] for release in inspected["releases"]], [t, t2])

    def test_release_preparation_needs_a_SUFFICIENT_assessment_of_exact_C_even_through_the_library(self):
        """Collection versus acceptance: without a verdict, with a FAIL verdict, or for a C that is not the
        observed dev, nothing is prepared. The low-level unjudged kernel refuses a real target, and the checked
        library path refuses a running tool that is not the clean shared commit C selects; nothing is published."""
        target, _, b = self.real_consumer()
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "record")
        cases = {"no verdict": (c, None, "acceptance: INCOMPLETE: no non-author verdict on exact C"),
                 "FAIL verdict": (c, self.verdict(record, verdict="FAIL"),
                                  "acceptance: REFUSED: the non-author verdict is 'FAIL', not PASS"),
                 "not the observed dev": (b, verdict, f"stale: dev is {c}, not the accepted candidate {b}")}
        for name, (candidate, attestation, reason) in cases.items():
            with self.subTest(name):
                code, report = self.prepare_release(target, name, candidate, [record], attestation)
                self.assertEqual((code, report["status"]), (3, "REFUSED"))
                self.assertTrue(report["reason"].startswith(reason), report["reason"])
                self.assertFalse((self.tmp / name / "operation.json").exists())
        with self.assertRaises(Refused) as caught:
            transactions.prepare_release(remote=target, work=self.tmp / "unjudged op", candidate=c,
                                         acceptance=simulated_acceptance(), identity=RELEASER)
        self.assertEqual(caught.exception.code, "acceptance")
        with self.assertRaises(Refused) as caught:  # this process runs the working source, not the selected tool
            transactions.prepare_accepted_release(
                remote=target, work=self.tmp / "unselected op", candidate=c, records=[record.read_bytes()],
                attestation=json.dumps(verdict).encode("utf-8"), pstack=self.pstack(), identity=RELEASER)
        self.assertIn(caught.exception.code, ("acceptance", "tool"))
        self.assertFalse((self.tmp / "unjudged op").exists())
        self.assertEqual(refs(target), {DEV: c})

    def test_saved_operation_bindings_and_store_corruption_are_refused_before_any_push(self):
        """Saved operations: another destination, preparing tool, next line or acceptance identity in the record,
        a missing binding, a store ref naming another object, a missing prepared object, another or a dirty running
        tool (also through direct library calls), and a moved target are each refused by apply and reconcile before
        any push; neither the target nor another repository changes. The untouched operation then applies."""
        target, _, _ = self.real_consumer()
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "record")
        operation = self.prepare_release(target, "op", c, [record], verdict)[1]["operation"]
        work = self.tmp / "op"
        store, saved = work / "repo.git", (work / "operation.json").read_bytes()
        other = self.mirror(target, "other target.git")
        d = operation["next"]["commit"]
        loose = store / "objects" / d[:2] / d[2:]
        binding = out("-C", store, "rev-parse", transactions.BINDING)
        newer = self.tool_copy("newer tool")
        self.commit(newer, {"NOTES.md": "a later tool commit\n"}, "Later tool commit")
        dirty = self.tool_copy("dirty tool")
        (dirty / "mrs" / "shadow.py").write_text("SHADOW = True\n", encoding="utf-8")

        def record_change(change):
            def edit():
                changed = json.loads(saved)
                change(changed)
                (work / "operation.json").write_bytes(json.dumps(changed).encode("utf-8"))
            return edit, lambda: (work / "operation.json").write_bytes(saved)

        moved = Path(str(target) + " moved")
        cases = {
            "record names another target": (*record_change(lambda op: op.update(remote=str(other))), None, "operation"),
            "record names another preparing tool": (
                *record_change(lambda op: op["tool"].update(commit="0" * 40)), None, "operation"),
            "record changes the next line": (
                *record_change(lambda op: op["next"].update(version="26.3.0")), None, "operation"),
            "record changes the acceptance identity": (
                *record_change(lambda op: op.update(acceptance_sha256="0" * 64)), None, "operation"),
            "store binding removed": (lambda: out("-C", store, "update-ref", "-d", transactions.BINDING),
                                      lambda: out("-C", store, "update-ref", transactions.BINDING, binding), None,
                                      "operation"),
            "store tag ref names D": (lambda: out("-C", store, "update-ref", "refs/mrs/op/tag", d),
                                      lambda: out("-C", store, "update-ref", "refs/mrs/op/tag", operation["tag"]),
                                      None, "operation"),
            "prepared D missing from the store": (lambda: os.replace(loose, work / "D object"),
                                                  lambda: os.replace(work / "D object", loose), None, "operation"),
            "running tool is a later commit": (lambda: None, lambda: None, newer, "tool"),
            "running tool is dirty": (lambda: None, lambda: None, dirty, "tool"),
            "target moved away": (lambda: os.replace(target, moved), lambda: os.replace(moved, target), None,
                                  "destination"),
        }
        for name, (change, restore, tool, reason) in cases.items():
            with self.subTest(name):
                change()
                try:
                    for verb in ("apply", "reconcile"):
                        code, report = self.operation(verb, "op", checkout=tool)
                        self.assertEqual((code, report["status"], report["reason"]), (3, "REFUSED", reason), report)
                finally:
                    restore()
                self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
                self.assertEqual((refs(target), refs(other)), ({DEV: c}, {DEV: c}))
        with self.subTest("direct library calls from this process, which runs another tool"):
            outcome = transactions.apply(work)
            self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "tool"), outcome)
            for call in (transactions.push, transactions.reconcile):
                with self.assertRaises(Refused) as caught:
                    call(work)
                self.assertEqual(caught.exception.code, "tool")
            self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
            self.assertEqual(refs(target), {DEV: c})

        self.applied("op")
        self.assertEqual(refs(target), {MAIN: c, TAG: operation["tag"], DEV: d})
        self.assertEqual(refs(other), {DEV: c})

    def test_a_changed_main_lease_is_refused_before_any_push_and_the_prepared_one_applies(self):
        """Saved leases: the store binds the main observed at preparation. A first release whose absent main is
        changed to ancestor B, even with a competing main at B, and a later release whose P is changed to absence
        or to ancestor B, are refused by inspect, apply, reconcile and a direct push by the preparing tool, with no
        push attempted and the target unchanged. The unchanged operations apply, and repeats keep T and D."""
        target, _, b = self.real_consumer()

        def refused(name, lease, competing_main=None):
            record = self.tmp / name / "operation.json"
            saved = record.read_bytes()
            changed = json.loads(saved)
            changed["expected"][MAIN] = lease
            record.write_text(json.dumps(changed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if competing_main:
                out("-C", target, "update-ref", MAIN, competing_main)
            before = refs(target)
            try:
                for verb in ("inspect", "apply", "reconcile"):
                    code, report = self.operation(verb, name)
                    self.assertEqual((code, report["status"], report["reason"]), (3, "REFUSED", "operation"), report)
                self.assertEqual(self.checked("push", self.tmp / name), "Refused: operation")
                self.assertEqual(refs(target), before)
            finally:
                record.write_bytes(saved)
                if competing_main:
                    out("-C", target, "update-ref", "-d", MAIN, competing_main)
            self.assertFalse((self.tmp / name / "attempts.log").exists(), "no push may be attempted")

        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "first")
        first = self.prepare_release(target, "first op", c, [record], verdict)[1]["operation"]
        self.assertIsNone(first["expected"][MAIN])
        with self.subTest("first release: absent main changed to ancestor B, with a competing main at B"):
            refused("first op", b, competing_main=b)
        self.applied("first op")
        self.assertEqual(refs(target), {MAIN: c, TAG: first["tag"], DEV: first["next"]["commit"]})

        c2 = self.integrate(target, {"docs/next.md": "next-line task\n"}, "Integrate a next-line task")
        record2 = self.accepted(target, "second")[0]
        criteria = json.loads(record2.read_text(encoding="utf-8"))["criteria"]
        verdict2 = self.verdict(record2, criteria={"identity": criteria, "previous": criteria,
                                                   "assessment": "SIMULATED: criteria unchanged since 26.1.0"})
        second = self.prepare_release(target, "second op", c2, [record2], verdict2)[1]["operation"]
        self.assertEqual(second["expected"][MAIN], c)
        for label, lease in (("absence", None), ("ancestor B", b)):
            with self.subTest(f"later release: P changed to {label}"):
                refused("second op", lease)
        self.applied("second op")

        released = {MAIN: c2, TAG: first["tag"], f"refs/tags/{second['version']}": second["tag"],
                    DEV: second["next"]["commit"]}
        self.assertEqual(refs(target), released)
        for name in ("first op", "second op"):
            self.assertEqual(self.operation("apply", name)[1]["status"], "NOOP")
            self.assertEqual(self.operation("reconcile", name)[1]["status"], "COMPLETED")
        self.assertEqual(refs(target), released)

    def test_a_direct_library_push_keeps_the_apply_preconditions_and_the_leases(self):
        """Direct library calls: with the saved release unchanged, a clock in a later year than the next line's
        opening makes apply and a direct push by the preparing tool refuse it, with no push attempted and the
        target unchanged. In the prepared state the direct push publishes main, T and D once; a competing dev after
        the advertisement is still rejected by the leases themselves; the completed release stays a NOOP for apply
        in that later year, and a direct push of it is refused without an attempt."""
        target, _, _ = self.real_consumer()
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "record")
        race = self.mirror(target, "race target.git")
        operation = self.prepare_release(target, "op", c, [record], verdict)[1]["operation"]
        self.assertEqual(self.prepare_release(race, "race op", c, [record], verdict)[0], 0)
        work = self.tmp / "op"
        opened = datetime.fromisoformat(operation["next"]["opened"]).replace(hour=12, tzinfo=timezone.utc)
        later = opened.replace(year=opened.year + 1, month=1, day=1)

        self.assertEqual((self.checked("apply", work, later), self.checked("push", work, later)),
                         ("REFUSED", "Refused: year-changed"))
        self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
        self.assertEqual(refs(target), {DEV: c})

        self.hook(race, "pre-receive", COMPETING_DEV)
        self.assertEqual(self.checked("push", self.tmp / "race op", opened), "stale")
        competitor = (race / "fixture-competitor").read_text(encoding="utf-8").strip()
        self.assertEqual(refs(race), {DEV: competitor})

        self.assertEqual(self.checked("push", work, opened), "success")
        released = {MAIN: c, TAG: operation["tag"], DEV: operation["next"]["commit"]}
        self.assertEqual(refs(target), released)
        self.assertEqual((self.checked("apply", work, later), self.checked("push", work, later)),
                         ("NOOP", "Refused: completed"))
        self.assertEqual(refs(target), released)
        for name in ("op", "race op"):
            self.assertEqual(len((self.tmp / name / "attempts.log").read_text(encoding="utf-8").splitlines()), 1)

    def test_redirected_or_unreadable_destination_configuration_publishes_nowhere(self):
        """Single effective destination: after preparation, a pushInsteadOf rewrite of the target refuses apply and
        reconciliation, and configuration Git cannot read is UNKNOWN; neither the target nor the would-be sink
        changes. With the configuration restored the same operation applies."""
        target, _, _ = self.real_consumer()
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "record")
        self.assertEqual(self.prepare_release(target, "op", c, [record], verdict)[0], 0)
        sink = self.bare("sink.git", fixture=False)
        config = self.tmp / "ambient global.gitconfig"

        def unreadable():
            with open(config, "a", encoding="utf-8") as handle:
                handle.write('[url "unreadable"]\n\tpushInsteadOf\n')  # Git rejects a rewrite without a value

        for name, setting, expected in (
                ("pushInsteadOf", lambda: out("config", "--global", f"url.{sink}.pushInsteadOf", str(target)),
                 (3, "REFUSED", "destination")),
                ("unreadable", unreadable, (4, "UNKNOWN", "destination"))):
            with self.subTest(name):
                setting()
                try:
                    code, report = self.operation("apply", "op")
                    self.assertEqual((code, report["status"], report["reason"]), expected, report)
                    self.assertEqual(self.operation("reconcile", "op")[1]["status"], expected[1])
                finally:
                    config.write_text(AMBIENT_GLOBAL_CONFIG, encoding="utf-8")
                self.assertFalse((self.tmp / "op" / "attempts.log").exists(), "no push may be attempted")
                self.assertEqual((refs(target), refs(sink)), ({DEV: c}, {}))
        self.applied("op")
        self.assertEqual(refs(sink), {})

    def test_races_rejections_and_missing_atomic_support_leave_the_target_and_competitors_intact(self):
        """Real races / Atomic rejection / Conflicts: after preparation, a competing dev commit (before
        observation or after the advertisement), a new main or a conflicting tag rejects the release; a policy
        rejection of any one ref publishes none; a receiver without atomic support refuses without fallback. One
        push at most, never a retry; competing work survives."""
        target, _, _ = self.real_consumer()
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "record")
        rival = self.checkout("rival")
        self.commit(rival, {"rival.txt": "x\n"}, "rival")

        def rejecting(ref):
            return lambda m: self.hook(m, "update", f'[ "$1" = "{ref}" ] && exit 1\nexit 0\n')

        cases = {
            "competing dev after preparation": (
                lambda m: self.integrate(m, {"rival.py": "RIVAL = 1\n"}, "Competing task"), ("REFUSED", "stale")),
            "competing dev after advertisement": (lambda m: self.hook(m, "pre-receive", COMPETING_DEV),
                                                  ("REFUSED", "stale")),
            "main created after preparation": (
                lambda m: out("-C", rival, "push", "--quiet", m, f"HEAD:{MAIN}"), ("REFUSED", "stale")),
            "conflicting tag after preparation": (
                lambda m: out("-C", rival, "push", "--quiet", m, f"HEAD:{TAG}"), ("REFUSED", "conflict")),
            "policy rejects main": (rejecting(MAIN), ("REFUSED", "policy")),
            "policy rejects the tag": (rejecting(TAG), ("REFUSED", "policy")),
            "policy rejects dev": (rejecting(DEV), ("REFUSED", "policy")),
            "receiver without atomic support": (
                lambda m: self.receiver_config("receive.advertiseAtomic", "false"),
                ("REFUSED", "atomic-unsupported")),
        }
        for index, (name, (perturb, expected)) in enumerate(cases.items()):
            with self.subTest(name):
                mirror = self.mirror(target, f"mirror {index}.git")
                self.assertEqual(self.prepare_release(mirror, f"op {index}", c, [record], verdict)[0], 0)
                perturb(mirror)
                before = refs(mirror)

                code, report = self.operation("apply", f"op {index}")

                self.assertEqual((report["status"], report["reason"]), expected, report)
                attempts = self.tmp / f"op {index}" / "attempts.log"
                self.assertLessEqual(len(attempts.read_text(encoding="utf-8").splitlines())
                                     if attempts.exists() else 0, 1)
                after = refs(mirror)
                if (mirror / "fixture-competitor").exists():
                    before[DEV] = (mirror / "fixture-competitor").read_text(encoding="utf-8").strip()
                self.assertEqual(after, before, "no release ref changed and competing work survived")
        self.assertNotIn(b"atomic", receiver_capabilities(target))
        out("config", "--global", "--unset", "receive.advertiseAtomic")
        self.assertIn(b"atomic", receiver_capabilities(target), "control: the receiver advertises atomic again")

    def test_lost_response_is_reconciled_read_only_and_the_retry_keeps_exact_T_and_D(self):
        """Retries and uncertainty: the receiver applies all three refs, then the applying command is killed before
        any response. Read-only reconciliation finds the exact prepared T and D, and a retry is a no-op."""
        target, _, _ = self.real_consumer()
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        record, verdict = self.accepted(target, "record")
        operation = self.prepare_release(target, "op", c, [record], verdict)[1]["operation"]
        self.hook(target, "post-receive", """cat >/dev/null
: > fixture-updated
i=0
while [ ! -f fixture-release ] && [ $i -lt 2400 ]; do sleep 0.05; i=$((i+1)); done
exit 0
""")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen([sys.executable, "-I", "-B", str(shared_snapshot()[0] / "mrs"), "operation", "apply",
                                 "--work", str(self.tmp / "op")], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags, start_new_session=os.name != "nt")
        wait_for(target / "fixture-updated")
        kill_tree(proc)
        (target / "fixture-release").write_text("", encoding="utf-8")
        self.assertNotEqual(proc.returncode, 0)
        prepared = {MAIN: c, TAG: operation["tag"], DEV: operation["next"]["commit"]}

        code, reconciled = self.operation("reconcile", "op")

        self.assertEqual((code, reconciled["status"], reconciled["refs"]), (0, "COMPLETED", prepared))
        self.assertEqual(self.operation("apply", "op")[1]["status"], "NOOP")
        self.assertEqual(refs(target), prepared)
        self.assertEqual(len((self.tmp / "op" / "attempts.log").read_text(encoding="utf-8").splitlines()), 1)

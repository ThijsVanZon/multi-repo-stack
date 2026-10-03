import json
from datetime import datetime, timezone
from unittest import mock

from mrs import git as mrs_git
from mrs import transactions
from mrs.transactions import Refused
from tests.support import DEV, MAIN, NOW, GitTestCase, refs

TAG = "refs/tags/26.1.0"


class OperationRecordTests(GitTestCase):
    """A saved operation is applied, pushed or reconciled only as its store prepared it."""

    def assert_refused_before_any_push(self, work, target, change, now=NOW):
        record = work / "operation.json"
        prepared = record.read_bytes()
        operation = json.loads(prepared)
        change(operation)
        record.write_bytes(json.dumps(operation).encode("utf-8"))
        before = refs(target)
        try:
            with mock.patch.object(mrs_git, "run", wraps=mrs_git.run) as run:
                outcome = transactions.apply(work, now=now)
                self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "operation"), outcome)
                for call in (transactions.push, transactions.reconcile):
                    with self.assertRaises(Refused) as caught:
                        call(work)
                    self.assertEqual(caught.exception.code, "operation")
            self.assertFalse([c.args[0] for c in run.call_args_list if "push" in c.args[0]],
                             "no push may be attempted")
            self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
            self.assertEqual(refs(target), before)
        finally:
            record.write_bytes(prepared)

    def test_bootstrap_record_must_create_exactly_dev_at_prepared_B(self):
        """Local bootstrap / Publication scope: a changed bootstrap record (extra main, null or missing dev,
        a non-absent lease, another commit or different facts) is refused before any push; the unchanged
        record still bootstraps dev only and reconciles with the same identities."""
        target = self.bare("target.git")
        source, earlier = self.lifecycle_source()
        b = self.commit(source, {"README.md": "# Example app, revised\n"}, "Revise example app")
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        prepared = (work / "operation.json").read_bytes()
        changes = {
            "main added to updates": lambda op: op["updates"].update({MAIN: b}),
            "updates is null": lambda op: op.update(updates=None),
            "dev update and lease missing": lambda op: op.update(updates={}, expected={}),
            "extra lease": lambda op: op["expected"].update({MAIN: None}),
            "dev lease not absent": lambda op: op["expected"].update({DEV: b}),
            "dev names the prepared commit's parent": lambda op: op["updates"].update({DEV: earlier}),
            "dev names an abbreviation": lambda op: op["updates"].update({DEV: b[:12]}),
            "version differs from B": lambda op: op.update(version="26.2.0"),
            "remote is not a path": lambda op: op.update(remote=None),
        }
        for label, change in changes.items():
            with self.subTest(label):
                self.assert_refused_before_any_push(work, target, change)

        outcome = transactions.apply(work)
        self.assertEqual((outcome.status, outcome.reason), ("APPLIED", "applied"))
        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(transactions.reconcile(work).status, "COMPLETED")
        self.assertEqual((work / "operation.json").read_bytes(), prepared)

    def test_release_record_must_publish_exactly_main_C_tag_T_dev_D(self):
        """First transaction / Publication scope: a changed release record (extra or missing refs, dev not
        moved to D, wrong leases, facts that differ from the receipt, null fields) is refused before any
        push; the unchanged record still applies, reconciles and repeats with the same C/T/D."""
        target, _, b = self.bootstrapped()
        self.integrate(target, {"src/feature.py": "FEATURE = 1\n"}, "Integrate feature task")
        work, operation = self.prepare(target)
        c, t, d = operation["candidate"], operation["tag"], operation["next"]["commit"]
        prepared = (work / "operation.json").read_bytes()
        changes = {
            "unintended branch added": lambda op: op["updates"].update({"refs/heads/unintended": c}),
            "dev update is C instead of D": lambda op: op["updates"].update({DEV: c}),
            "dev update and lease missing": lambda op: (op["updates"].pop(DEV), op["expected"].pop(DEV)),
            "main update is D": lambda op: op["updates"].update({MAIN: d}),
            "updates is null": lambda op: op.update(updates=None),
            "tag lease not absent": lambda op: op["expected"].update({TAG: t}),
            "dev lease is B, not C": lambda op: op["expected"].update({DEV: b}),
            "main lease is not an ancestor of C": lambda op: op["expected"].update({MAIN: d}),
            "main lease is an abbreviation": lambda op: op["expected"].update({MAIN: b[:12]}),
            "expected is a list": lambda op: op.update(expected=[]),
            "tag names C": lambda op: op.update(tag=c),
            "candidate differs from the receipt": lambda op: op.update(candidate=b),
            "next is null": lambda op: op.update(next=None),
            "receipt hash differs": lambda op: op.update(receipt_sha256="0" * 64),
        }
        for label, change in changes.items():
            with self.subTest(label):
                self.assert_refused_before_any_push(work, target, change)
        with self.subTest("opening date moved into the year of execution"):
            later = datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc)
            self.assert_refused_before_any_push(
                work, target, lambda op: op["next"].update(opened="2027-01-02"), now=later)

        outcome = transactions.apply(work, now=NOW)
        self.assertEqual((outcome.status, outcome.reason), ("APPLIED", "applied"))
        self.assertEqual(refs(target), {MAIN: c, TAG: t, DEV: d})
        self.assertEqual(transactions.reconcile(work).status, "COMPLETED")
        self.assertEqual(transactions.apply(work, now=NOW).status, "NOOP")
        self.assertEqual(refs(target), {MAIN: c, TAG: t, DEV: d})
        self.assertEqual((work / "operation.json").read_bytes(), prepared)
        self.assertEqual(len((work / "attempts.log").read_text(encoding="utf-8").splitlines()), 1)

import hashlib
import json
import unittest

from mrs import transactions
from mrs.transactions import Refused
from tests.consumer_support import IMPLEMENTATION, NATIVE, RESEARCH, ConsumerTestCase, probe
from tests.support import DEV, MAIN, NOW, RELEASER, git, out, refs

TAG = "refs/tags/26.1.0"


def receipt(repo, tag: str) -> dict:
    message = git("-C", repo, "cat-file", "tag", tag).stdout.partition(b"\n\n")[2]
    marker, line, rest = message.split(b"\n", 2)
    assert (marker, rest) == (b"multi-repo-stack release receipt v1", b""), message[:80]
    return json.loads(line)


class CheckedReleaseTests(ConsumerTestCase):
    """The checked path: collected records and a SIMULATED fixture verdict, assessed by the tool C selects,
    into the existing fixture-only release kernel. Unlike the low-level transaction tests, whose receipts
    carry an unjudged SIMULATED payload, these receipts carry an assessment that observers validate again."""

    def test_assessed_receipt_round_trips_and_the_release_retries_exactly(self):
        """Transaction continuity: the assessed acceptance survives the annotated-tag round trip byte-exact,
        the release publishes C/T/D atomically without changing C, and a retry keeps T, D and the record."""
        target, _, _ = self.consumer("implementation", IMPLEMENTATION)
        c = self.integrate(target, {"docs/notes.md": "integrated task\n"}, "Integrate a task")
        _, _, record = self.collect(target, "implementation")
        verdict = self.verdict(record)
        code, assessment = self.assess(target, [record], verdict)
        self.assertEqual((code, assessment["status"]), (0, "SUFFICIENT"), assessment)

        operation = self.checked_prepare(target, "release op", c, [record], verdict)
        work = self.tmp / "release op"
        saved = (work / "operation.json").read_bytes()
        outcome = transactions.apply(work, now=NOW)

        self.assertEqual(outcome.status, "APPLIED", outcome)
        tag, d = operation["tag"], operation["next"]["commit"]
        self.assertEqual(refs(target), {MAIN: c, TAG: tag, DEV: d})
        clone = self.tmp / "verifier clone.git"
        out("clone", "--quiet", "--bare", target, clone)
        kept = receipt(clone, tag)
        self.assertEqual(kept["acceptance"], assessment["acceptance"])
        self.assertEqual(kept["acceptance"]["results"], [json.loads(record.read_text(encoding="utf-8"))])
        evidence = kept["acceptance"]["results"][0]["results"][0]["evidence"]
        self.assertEqual(hashlib.sha256(evidence["text"].encode("utf-8")).hexdigest(), evidence["sha256"])
        self.assertEqual(out("-C", clone, "rev-parse", f"{tag}^{{commit}}"), c)
        self.assertEqual(out("-C", clone, "diff-tree", "-r", "--name-only", "--no-commit-id", c, d), "VERSION")
        code, inspected = self.tool("inspect", "--remote", clone)
        self.assertEqual((code, inspected["kind"], inspected["problems"]), (0, "LIFECYCLE", []))
        self.assertEqual([release["tag"] for release in inspected["releases"]], [tag])

        self.assertEqual(transactions.reconcile(work).status, "COMPLETED")
        self.assertEqual(transactions.apply(work, now=NOW).status, "NOOP")
        self.assertEqual(refs(target), {MAIN: c, TAG: tag, DEV: d})
        self.assertEqual((work / "operation.json").read_bytes(), saved)

    def test_insufficient_assessments_prepare_nothing_and_the_kernel_payload_cannot_claim_assessment(self):
        """Collection versus acceptance: passing records without a verdict, or with a FAIL verdict, prepare no
        operation; the low-level kernel refuses a payload claiming the assessed format."""
        target, _, c = self.consumer("research", RESEARCH)
        _, _, record = self.collect(target, "research")
        cases = {"no verdict": (None, "INCOMPLETE: no non-author verdict on exact C"),
                 "FAIL verdict": (self.verdict(record, verdict="FAIL"),
                                  "REFUSED: the non-author verdict is 'FAIL', not PASS")}
        for name, (verdict, detail) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.checked_prepare(target, name, c, [record], verdict),
                                 {"refused": "acceptance", "detail": detail})
                self.assertFalse((self.tmp / name / "operation.json").exists())
        with self.assertRaises(Refused) as caught:
            transactions.prepare_release(remote=target, work=self.tmp / "low level op", candidate=c, identity=RELEASER,
                                         acceptance={"format": "multi-repo-stack/acceptance/1", "criteria": {},
                                                     "results": [], "verdict": {}}, now=NOW)
        self.assertEqual(caught.exception.code, "acceptance")
        self.assertEqual(refs(target), {DEV: c})

    def test_receipt_over_the_frozen_limit_is_refused_before_anything_is_published(self):
        """Evidence integrity: six checks of exactly 64 KiB evidence each pass and assess as SUFFICIENT, but
        their serialized receipt exceeds 1 MiB, so preparation refuses rather than truncate or drop evidence."""
        target, _, c = self.consumer("probe", {f"large-{n}": probe("astral") for n in range(1, 7)})
        _, report, record = self.collect(target, "large evidence")
        self.assertEqual(report["status"], "PASSED")
        verdict = self.verdict(record)
        self.assertEqual(self.assess(target, [record], verdict)[1]["status"], "SUFFICIENT")

        operation = self.checked_prepare(target, "large op", c, [record], verdict)

        self.assertEqual(operation["refused"], "receipt", operation)
        self.assertTrue(operation["detail"].endswith("bytes; the limit is 1048576"), operation)
        self.assertEqual(refs(target), {DEV: c})

    def test_criteria_changes_are_judged_against_the_previous_release_and_tampered_receipts_block(self):
        """Verdict / Later release / Conflicts: after a checked 26.1.0, weakened criteria at 26.2.0 need a
        verdict bound to the previous release's criteria identity; then 26.2.0 releases and 26.1.0 stays
        intact. A receipt edited after assessment is malformed and blocks mutation."""
        target, _, b = self.consumer("implementation", IMPLEMENTATION)
        _, _, first = self.collect(target, "first")
        operation = self.checked_prepare(target, "first op", b, [first], self.verdict(first))
        self.assertEqual(transactions.apply(self.tmp / "first op", now=NOW).status, "APPLIED")
        selector = json.loads(out("-C", target, "show", f"{refs(target)[DEV]}:multi-repo-stack.json"))
        selector["checks"]["build-and-run"]["evidence_required"] = False
        c2 = self.integrate(target, {"multi-repo-stack.json": json.dumps(selector, indent=2) + "\n"},
                            "Stop requiring build evidence")
        _, _, second = self.collect(target, "second")
        previous, current = (json.loads(path.read_text(encoding="utf-8"))["criteria"] for path in (first, second))
        self.assertNotEqual(previous, current)

        code, assessment = self.assess(target, [second], self.verdict(second), name="unexplained")
        self.assertEqual((code, assessment["status"]), (3, "REFUSED"))
        self.assertIn(f"the verdict compares with criteria None, but the previous release has '{previous}'",
                      assessment["problems"])
        explained = self.verdict(second, criteria={
            "identity": current, "previous": previous,
            "assessment": "SIMULATED: build evidence is no longer required; judged acceptable for this fixture"})
        code, assessment = self.assess(target, [second], explained, name="explained")
        self.assertEqual((code, assessment["acceptance"]["criteria"]), (0, {"identity": current, "previous": previous}))
        later = self.checked_prepare(target, "second op", c2, [second], explained)
        self.assertEqual(transactions.apply(self.tmp / "second op", now=NOW).status, "APPLIED")
        self.assertEqual(refs(target), {MAIN: c2, TAG: operation["tag"], "refs/tags/26.2.0": later["tag"],
                                        DEV: later["next"]["commit"]})

        forgeries = {
            "the verdict turned to FAIL": (lambda r: r["acceptance"]["verdict"].update(verdict="FAIL"),
                                           "the non-author verdict is 'FAIL', not PASS"),
            "a record relabelled to another OS": (
                lambda r: r["acceptance"]["results"][0]["runner"].update(os="linux" if NATIVE != "linux" else "macos"),
                f"build-and-run/{NATIVE} requires {NATIVE}, but this record's runner observed"),
        }
        for name, (change, problem) in forgeries.items():
            with self.subTest(name):
                mirror = self.tmp / f"{name}.git"
                out("clone", "--quiet", "--mirror", target, mirror)
                raw = git("-C", mirror, "cat-file", "tag", operation["tag"]).stdout
                header = raw.partition(b"\n\n")[0]
                forged = receipt(mirror, operation["tag"])
                change(forged)
                body = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode("ascii")
                tag = out("-C", mirror, "hash-object", "-t", "tag", "-w", "--stdin",
                          input=header + b"\n\nmulti-repo-stack release receipt v1\n" + body + b"\n")
                out("-C", mirror, "update-ref", TAG, tag)
                code, inspected = self.tool("inspect", "--remote", mirror)
                self.assertEqual(inspected["kind"], "UNSUPPORTED")
                self.assertTrue(any(found.startswith("malformed contract receipt blocks mutation: tag 26.1.0: "
                                                     "assessed acceptance does not hold: ") and problem in found
                                    for found in inspected["problems"]), inspected["problems"])


if __name__ == "__main__":
    unittest.main()

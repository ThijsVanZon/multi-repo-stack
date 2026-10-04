import copy
import hashlib
import json
import unittest

from mrs import transactions
from mrs.transactions import FIXTURE_MARKER, Refused
from tests.consumer_support import IMPLEMENTATION, NATIVE, PIN, RESEARCH, ConsumerTestCase, probe, shape_files
from tests.support import DEV, MAIN, NOW, RELEASER, git, out, refs

TAG = "refs/tags/26.1.0"


def receipt(repo, tag: str) -> dict:
    message = git("-C", repo, "cat-file", "tag", tag).stdout.partition(b"\n\n")[2]
    marker, line, rest = message.split(b"\n", 2)
    assert (marker, rest) == (b"multi-repo-stack release receipt v1", b""), message[:80]
    return json.loads(line)


def forge(repo, tag: str, change) -> None:
    """Point tag `tag`'s name in `repo` at a copy of it whose receipt `change` edits (test code: a forgery)."""
    header = git("-C", repo, "cat-file", "tag", tag).stdout.partition(b"\n\n")[0]
    edited = receipt(repo, tag)
    change(edited)
    body = json.dumps(edited, sort_keys=True, separators=(",", ":")).encode("ascii")
    forged = out("-C", repo, "hash-object", "-t", "tag", "-w", "--stdin",
                 input=header + b"\n\nmulti-repo-stack release receipt v1\n" + body + b"\n")
    out("-C", repo, "update-ref", f"refs/tags/{edited['version']}", forged)


def stripped(record: dict) -> None:
    """Remove every execution fact from a collected record, keeping its bindings and passing outcome."""
    record.update(runner={"os": record["runner"]["os"]}, checkout=None, observed=None)
    for result in record["results"]:
        result.update(executable=None, started=None, finished=None, output=None)


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
        self.assertEqual(self.checked("apply", work), "APPLIED")
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

        self.assertEqual(self.checked("reconcile", work), "COMPLETED")
        self.assertEqual(self.checked("apply", work), "NOOP")
        self.assertEqual(refs(target), {MAIN: c, TAG: tag, DEV: d})
        self.assertEqual((work / "operation.json").read_bytes(), saved)

    def test_insufficient_assessments_prepare_nothing_and_the_kernel_payload_cannot_claim_assessment(self):
        """Collection versus acceptance: passing records without a verdict, with a FAIL verdict, or stripped of
        their execution facts (with a PASS verdict) prepare no operation and publish nothing; the low-level
        kernel refuses a payload claiming the assessed format. The real record and verdict then release."""
        target, _, c = self.consumer("research", RESEARCH)
        _, _, record = self.collect(target, "research")
        bare = json.loads(record.read_text(encoding="utf-8"))
        stripped(bare)
        cases = {"no verdict": ([record], None, "INCOMPLETE: no non-author verdict on exact C"),
                 "FAIL verdict": ([record], self.verdict(record, verdict="FAIL"),
                                  "REFUSED: the non-author verdict is 'FAIL', not PASS"),
                 "no execution facts": ([self.write_json("stripped.json", bare)], self.verdict(record),
                                        "REFUSED: record 1: C's observation as dev at a UTC time is missing; record 1: "
                                        "the runner does not identify its actual OS, platform, Python and Git; ")}
        for name, (records, verdict, detail) in cases.items():
            with self.subTest(name):
                refused = self.checked_prepare(target, name, c, records, verdict)
                self.assertEqual(refused["refused"], "acceptance", refused)
                self.assertTrue(refused["detail"].startswith(detail), refused["detail"])
                self.assertFalse((self.tmp / name / "operation.json").exists())
        with self.assertRaises(Refused) as caught:
            transactions.prepare_release(remote=target, work=self.tmp / "low level op", candidate=c, identity=RELEASER,
                                         acceptance={"format": "multi-repo-stack/acceptance/1", "criteria": {},
                                                     "results": [], "verdict": {}}, now=NOW)
        self.assertEqual(caught.exception.code, "acceptance")
        self.assertEqual(refs(target), {DEV: c})

        operation = self.checked_prepare(target, "control op", c, [record], self.verdict(record))
        self.assertEqual(self.checked("apply", self.tmp / "control op"), "APPLIED")
        self.assertEqual(refs(target), {MAIN: c, TAG: operation["tag"], DEV: operation["next"]["commit"]})

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
        verdict bound to the previous release's criteria identity; then 26.2.0 releases, 26.1.0 stays intact
        and observers recognize the history. A receipt edited after assessment is malformed and blocks the
        next checked release: a FAIL verdict, a relabelled OS, removed execution facts, no pstack identity, an
        invented predecessor at the first release, or a later release claiming to have none."""
        target, _, b = self.consumer("implementation", IMPLEMENTATION)
        _, _, first = self.collect(target, "first")
        operation = self.checked_prepare(target, "first op", b, [first], self.verdict(first))
        self.assertEqual(self.checked("apply", self.tmp / "first op"), "APPLIED")
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
        self.assertEqual(self.checked("apply", self.tmp / "second op"), "APPLIED")
        self.assertEqual(refs(target), {MAIN: c2, TAG: operation["tag"], "refs/tags/26.2.0": later["tag"],
                                        DEV: later["next"]["commit"]})

        code, inspected = self.tool("inspect", "--remote", target)
        self.assertEqual((code, inspected["kind"], inspected["problems"]), (0, "LIFECYCLE", []))

        def predecessor(value):
            def change(r):
                r["acceptance"]["criteria"]["previous"] = value
                r["acceptance"]["verdict"]["criteria"]["previous"] = value
            return change

        def each_record(**values):
            return lambda r: [record.update(values) for record in r["acceptance"]["results"]]

        held = "assessed acceptance does not hold: "
        forgeries = {
            "the verdict turned to FAIL": (operation, lambda r: r["acceptance"]["verdict"].update(verdict="FAIL"),
                                           held + "the non-author verdict is 'FAIL', not PASS"),
            "a record relabelled to another OS": (
                operation,
                lambda r: r["acceptance"]["results"][0]["runner"].update(os="linux" if NATIVE != "linux" else "macos"),
                f"build-and-run/{NATIVE} requires {NATIVE}, but this record's runner observed"),
            "execution facts removed": (operation, lambda r: stripped(r["acceptance"]["results"][0]),
                                        held + "C's observation as dev at a UTC time is missing"),
            "no pstack identity": (operation, each_record(pstack=None), held + "the pstack source is not identified"),
            "records naming different pstack sources": (
                operation, lambda r: r["acceptance"]["results"].append(copy.deepcopy(r["acceptance"]["results"][0]) | {
                    "pstack": dict(r["acceptance"]["results"][0]["pstack"], tree="4" * 40)}),
                held + "the records name different pstack sources"),
            "an invented predecessor at the first release": (
                operation, predecessor("9" * 64),
                f"its acceptance judged criteria changes against '{'9' * 64}', but no contract release precedes it "
                "(None: a first release is judged against the frozen intent)"),
            "a later release claiming to have none": (
                later, predecessor(None),
                f"its acceptance judged criteria changes against None, but the previous contract release 26.1.0 has "
                f"'{previous}'"),
        }
        for name, (prepared, change, problem) in forgeries.items():
            with self.subTest(name):
                mirror = self.tmp / f"{name}.git"
                out("clone", "--quiet", "--mirror", target, mirror)
                (mirror / FIXTURE_MARKER).write_text("task-owned disposable test fixture\n", encoding="utf-8")
                forge(mirror, prepared["tag"], change)
                code, inspected = self.tool("inspect", "--remote", mirror)
                self.assertEqual(inspected["kind"], "UNSUPPORTED")
                self.assertTrue(any(found.startswith(f"malformed contract receipt blocks mutation: tag "
                                                     f"{prepared['version']}: ") and problem in found
                                    for found in inspected["problems"]), inspected["problems"])
                blocked = self.checked_prepare(mirror, f"{name} op", refs(mirror)[DEV], [second], explained)
                self.assertEqual(blocked["refused"], "unsupported-state", blocked)

    def test_a_receipt_must_name_the_pstack_source_that_C_itself_pins(self):
        """Conflicts / Connection: when C pins pstack itself, as the shared project does, a receipt whose records
        name another pstack commit is malformed; the matching pin is the recognized control. Receipt-parser
        fixture: the low-level kernel released C, and the assessed acceptance placed in its receipt is SIMULATED
        (a real collected record and a fixture verdict, rebound to this C)."""
        consumer, _, _ = self.consumer("research", RESEARCH)
        _, _, collected = self.collect(consumer, "research")
        record = json.loads(collected.read_text(encoding="utf-8"))
        criteria = json.loads(out("-C", consumer, "show", f"{record['candidate']}:multi-repo-stack.json"))
        own = {"format": 1, "repository": "example/tool", "applicability": "lifecycle",
               "pstack": {"repository": "cursor/plugins", "commit": PIN, "path": "pstack"},
               "checks": criteria["checks"], "environments": criteria["environments"]}
        source = self.checkout("self-selected source")
        c = self.commit(source, {**shape_files("research"), "VERSION": "26.1.0\n",
                                 "multi-repo-stack.json": json.dumps(own, indent=2) + "\n"}, "Pin pstack itself")
        target = self.bare("self-selected target.git")
        self.bootstrap(target, source, c, name="self-selected bootstrap")
        work, operation = self.prepare(target, "self-selected op", candidate=c)
        self.assertEqual(transactions.apply(work, now=NOW).status, "APPLIED")
        record.update(repository="example/tool", candidate=c,
                      shared={"repository": "example/tool", "commit": c, "self": True, "clean": True})
        assessed = {"format": "multi-repo-stack/acceptance/1",
                    "criteria": {"identity": record["criteria"], "previous": None}, "results": [record],
                    "verdict": self.verdict(collected, repository="example/tool", candidate=c)}
        for name, commit in (("matching pin", PIN), ("another pstack commit", "3" * 40)):
            with self.subTest(name):
                mirror = self.tmp / f"{name}.git"
                out("clone", "--quiet", "--mirror", target, mirror)
                acceptance = copy.deepcopy(assessed)
                acceptance["results"][0]["pstack"]["commit"] = commit
                forge(mirror, operation["tag"], lambda r: r.update(acceptance=acceptance))
                code, inspected = self.tool("inspect", "--remote", mirror)
                if commit == PIN:
                    self.assertEqual((code, inspected["kind"], inspected["problems"]), (0, "LIFECYCLE", []))
                else:
                    self.assertEqual(inspected["kind"], "UNSUPPORTED")
                    self.assertIn("malformed contract receipt blocks mutation: tag 26.1.0: assessed acceptance does "
                                  f"not hold: a record's pstack source is not cursor/plugins@{PIN} (pstack), which C "
                                  "pins", inspected["problems"])


if __name__ == "__main__":
    unittest.main()

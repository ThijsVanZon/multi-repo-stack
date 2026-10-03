import json
import unittest

from tests.consumer_support import IMPLEMENTATION, NATIVE, RESEARCH, ConsumerTestCase
from tests.support import DEV, out, refs

OTHERS = [label for label in ("windows", "linux", "macos") if label != NATIVE]


class AssessmentTests(ConsumerTestCase):
    """Edited copies of real records and verdicts are SIMULATED inputs: they exercise the assessor's refusals,
    and never stand for an executed platform or an independent review."""

    def edited(self, record_path, name: str, change):
        """A SIMULATED record: a copy of a real one with `change` applied."""
        record = json.loads(record_path.read_text(encoding="utf-8"))
        change(record)
        return self.write_json(f"{name}.json", record)

    def test_every_required_pair_needs_a_passing_record_from_its_own_OS(self):
        """Coverage and OS: other platforms stay unmet (INCOMPLETE) until exercised; failed, skipped,
        conflicting, mislabelled, re-commanded and unknown results and a bare pass claim are REFUSED; a
        consistent duplicate is accepted as more evidence."""
        target, _, _ = self.consumer("implementation", IMPLEMENTATION, environments=(NATIVE, *OTHERS))
        _, report, record = self.collect(target, "three platforms")
        self.assertEqual(report["record"]["unmet"], [
            {"check": "build-and-run", "environment": label, "os": label} for label in OTHERS])
        verdict = self.verdict(record)
        _, second, again = self.collect(target, "duplicate")

        code, assessment = self.assess(target, [record, again], verdict)
        self.assertEqual((code, assessment["status"], assessment["problems"]), (1, "INCOMPLETE", []))
        self.assertEqual(assessment["missing"], [f"no passing record for build-and-run/{label} on {label}"
                                                 for label in OTHERS])

        def result(field, value):
            def change(record):
                record["results"][0][field] = value
            return change

        def runner_os(value):
            def change(record):
                record["runner"]["os"] = value
            return change

        pair = f"build-and-run/{NATIVE}"
        refusals = {
            "failed": ([self.edited(record, "failed", result("outcome", "fail"))],
                       f"{pair} did not pass: 'fail' (None)"),
            "skipped": ([self.edited(record, "skipped", result("outcome", "skipped"))],
                        f"{pair} did not pass: 'skipped' (None)"),
            "conflicting duplicate": ([record, self.edited(record, "conflict", result("outcome", "fail"))],
                                      f"{pair} did not pass: 'fail' (None)"),
            "Windows-style label on another OS's runner": (
                [self.edited(record, "other os", runner_os(OTHERS[0]))],
                f"{pair} requires {NATIVE}, but this record's runner observed '{OTHERS[0]}'"),
            "label pasted onto this OS's evidence": (
                [self.edited(record, "relabelled", result("environment", OTHERS[0]))],
                f"build-and-run/{OTHERS[0]} requires {OTHERS[0]}, but this record's runner observed '{NATIVE}'"),
            "another command": ([self.edited(record, "argv", result("argv", ["python", "-c", "pass"]))],
                                f"{pair} ran ['python', '-c', 'pass'], not the candidate's declared command"),
            "undeclared check": ([self.edited(record, "undeclared", result("check", "lint"))],
                                 f"lint/{NATIVE} is not a check/environment pair that the candidate's criteria "
                                 "require"),
            "pass with a failing exit": ([self.edited(record, "exit", result("exit", 1))],
                                         f"{pair} is recorded as passing with exit status 1"),
            "edited evidence": ([self.edited(record, "evidence", lambda r: r["results"][0]["evidence"].update(
                text="all good\n"))], f"{pair}: evidence bytes do not match their recorded size and SHA-256"),
            "bare pass claim": ([self.write_json("bare.json", {"passed": True})],
                                "record 1: not a complete multi-repo-stack/checks/1 record"),
        }
        for name, (records, problem) in refusals.items():
            with self.subTest(name):
                code, assessment = self.assess(target, records, verdict, name=name)
                self.assertEqual((code, assessment["status"]), (3, "REFUSED"), assessment)
                self.assertTrue(any(problem in found for found in assessment["problems"]), assessment["problems"])
                self.assertIsNone(assessment["acceptance"])

    def test_records_must_bind_this_C_N_criteria_and_the_selected_sources(self):
        """Candidate identity / Source changes: a record is refused when its repository, version, criteria
        identity or shared/pstack identity differs from what exact C selects; the unedited control is
        SUFFICIENT with a SIMULATED verdict."""
        target, _, c = self.consumer("research", RESEARCH)
        _, _, record = self.collect(target, "research")
        verdict = self.verdict(record)
        code, control = self.assess(target, [record], verdict)
        self.assertEqual((code, control["status"]), (0, "SUFFICIENT"), control)
        self.assertEqual(control["acceptance"]["results"], [json.loads(record.read_text(encoding="utf-8"))])

        edits = {
            "repository": lambda r: r.update(repository="example/other"),
            "version": lambda r: r.update(version="26.2.0"),
            "criteria": lambda r: r.update(criteria="0" * 64),
            "shared": lambda r: r["shared"].update(commit="1" * 40),
            "pstack": lambda r: r["pstack"].update(tree="2" * 40),
        }
        for key, change in edits.items():
            with self.subTest(key):
                code, assessment = self.assess(target, [self.edited(record, key, change)], verdict, name=key)
                self.assertEqual((code, assessment["status"]), (3, "REFUSED"))
                self.assertTrue(assessment["problems"][0].startswith(f"record 1: {key} is "), assessment["problems"])
        self.assertEqual(refs(target), {DEV: c})

    def test_only_a_PASS_verdict_from_a_declared_non_author_bound_to_exact_C_establishes_eligibility(self):
        """Verdict and reuse: wrong-C, PR-merge, abbreviated, self, unbound, bare, FAIL, unresolved-limitation,
        unexplained-criteria, wrong-criteria and wrong-previous verdicts are refused; an accepted limitation
        with the otherwise valid verdict is SUFFICIENT."""
        target, _, c = self.consumer("research", RESEARCH)
        _, _, record = self.collect(target, "research")
        side = out("-C", target, "commit-tree", f"{c}^{{tree}}", "-p", c, "-m", "PR head", input=b"")
        merge = out("-C", target, "commit-tree", f"{c}^{{tree}}", "-p", c, "-p", side, "-m", "Synthetic PR merge")
        good = self.verdict(record)
        criteria = good["criteria"]
        invalid = {
            "PR synthetic merge": (self.verdict(record, candidate=merge),
                                   f"the verdict inspected candidate '{merge}', not '{c}'"),
            "abbreviated candidate": (self.verdict(record, candidate=c[:12]),
                                      f"the verdict inspected candidate '{c[:12]}', not '{c}'"),
            "wrong version": (self.verdict(record, version="26.2.0"),
                              "the verdict inspected version '26.2.0', not '26.1.0'"),
            "self verdict": (self.verdict(record, verifier=dict(good["verifier"], relationship="author")),
                             "a verdict from 'author' is not a non-author verdict"),
            "delegate verdict": (self.verdict(record, verifier=dict(good["verifier"], relationship="author-helper")),
                                 "a verdict from 'author-helper' is not a non-author verdict"),
            "undescribed verifier": (self.verdict(record, verifier=dict(good["verifier"], statement=" ")),
                                     "does not describe its verifier's non-authorship and actual harness"),
            "unbound generic review": ({"verdict": "PASS", "statement": "Looks good to me."},
                                       "the verdict is not a complete multi-repo-stack/attestation/1"),
            "bare passed flag": ({"passed": True}, "the verdict is not a complete multi-repo-stack/attestation/1"),
            "FAIL": (self.verdict(record, verdict="FAIL"), "the non-author verdict is 'FAIL', not PASS"),
            "unresolved limitation": (self.verdict(record, limitations=[
                {"statement": "macOS was not exercised", "resolution": "unresolved"}]),
                "unresolved limitation: macOS was not exercised"),
            "unexplained criteria": (self.verdict(record, criteria=dict(criteria, assessment="")),
                                     "does not address criteria changes"),
            "other criteria": (self.verdict(record, criteria=dict(criteria, identity="3" * 64)),
                               f"the verdict addresses criteria '{'3' * 64}', not '{criteria['identity']}'"),
            "invented previous release": (self.verdict(record, criteria=dict(criteria, previous="4" * 64)),
                                          f"the verdict compares with criteria '{'4' * 64}', but the previous "
                                          "release has None (a first release compares with the frozen intent)"),
            "missing behavior": ({key: value for key, value in good.items() if key != "behavior"},
                                 "the verdict is not a complete multi-repo-stack/attestation/1"),
        }
        for name, (verdict, problem) in invalid.items():
            with self.subTest(name):
                code, assessment = self.assess(target, [record], verdict, name=name)
                self.assertEqual((code, assessment["status"]), (3, "REFUSED"), assessment)
                self.assertTrue(any(problem in found for found in assessment["problems"]), assessment["problems"])

        limitation = "SIMULATED: fixture platforms other than this one are not required"
        accepted = self.verdict(record, limitations=[{"statement": limitation, "resolution": "accepted"}])
        code, assessment = self.assess(target, [record], accepted, name="accepted limitation")
        self.assertEqual((code, assessment["status"]), (0, "SUFFICIENT"), assessment)
        self.assertEqual(assessment["acceptance"]["verdict"], accepted)

    def test_reused_observations_need_the_verdict_and_never_replace_runtime_records(self):
        """Verdict and reuse: an owner-proposed reused observation is undecided without a verdict, refused
        when the verdict omits it, decides another or the proposal claims owner approval; accepted or rejected
        by the verdict it is recorded with its true older source; it never stands in for a runtime record."""
        target, _, b = self.consumer("research", RESEARCH)
        c = self.integrate(target, {"NOTES.md": "integrated after the observation\n"}, "Integrate a task")
        _, _, record = self.collect(target, "research")
        observation = {"id": "agents-md-fresh-entry", "source_commit": b,
                       "harness": "SIMULATED: fixture agent harness", "scope": "fresh AGENTS.md discovery"}
        proposals = [observation]

        def decided(decision, item=observation):
            return [{"observation": item, "decision": decision, "rationale": "SIMULATED: AGENTS.md is unchanged"}]

        code, assessment = self.assess(target, [record], None, proposals)
        self.assertEqual((code, assessment["status"]), (1, "INCOMPLETE"))
        self.assertEqual(assessment["missing"],
                         ["no non-author verdict on exact C; 1 proposed reused observation(s) undecided"])
        refusals = {
            "undecided": (self.verdict(record), proposals,
                          "proposed reuse 'agents-md-fresh-entry' is not decided by the verdict"),
            "another observation": (self.verdict(record, reuse=decided("accepted", dict(observation, id="other"))),
                                    proposals, "the verdict decides reuse 'other' that was not proposed"),
            "owner approval claimed": (self.verdict(record), [dict(observation, approved_by="owner")],
                                       "must name its id, exact source commit, harness and scope"),
        }
        for name, (verdict, offered, problem) in refusals.items():
            with self.subTest(name):
                code, assessment = self.assess(target, [record], verdict, offered, name=name)
                self.assertEqual((code, assessment["status"]), (3, "REFUSED"), assessment)
                self.assertTrue(any(problem in found for found in assessment["problems"]), assessment["problems"])
        for decision in ("accepted", "rejected"):
            with self.subTest(decision):
                code, assessment = self.assess(target, [record], self.verdict(record, reuse=decided(decision)),
                                               proposals, name=decision)
                self.assertEqual((code, assessment["status"]), (0, "SUFFICIENT"), assessment)
                [kept] = assessment["acceptance"]["verdict"]["reuse"]
                self.assertEqual((kept["decision"], kept["observation"]["source_commit"]), (decision, b))
                self.assertNotEqual(b, c, "the reused observation keeps its older identity, not C")
        code, assessment = self.assess(target, [], self.verdict(record, reuse=decided("accepted")), proposals,
                                       name="reuse without records")
        self.assertEqual((code, assessment["status"]), (1, "INCOMPLETE"))
        self.assertEqual(assessment["missing"], [f"no passing record for synthetic-slope/{NATIVE} on {NATIVE}"])


if __name__ == "__main__":
    unittest.main()

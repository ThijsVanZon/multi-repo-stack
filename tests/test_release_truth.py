from mrs import state, transactions
from mrs.transactions import Refused
from tests.support import DEV, MAIN, NOW, GitTestCase, forge_tag, out, refs, selector, shared_snapshot

OPENED = "Next-line opening date (UTC): 2026-10-03"


class ForgedReceiptTests(GitTestCase):
    """Contract release tags are validated from retained objects; a matching name or peel is not enough."""

    def forged_state(self, mutate: str) -> tuple:
        target, _, c = self.bootstrapped()
        clone = self.tmp / "forger clone"
        out("clone", "--quiet", "--branch", "dev", target, clone)
        files = {"VERSION": "26.2.0\n"}
        if mutate == "extra file in D":
            files["README.md"] = "# also changed\n"
        if mutate == "wrong candidate VERSION":
            c = self.commit(clone, {"VERSION": "26.5.0\n"}, "Candidate with another VERSION")
        d = self.commit(clone, files, "Open line 26.2.0\n\n" + ("" if mutate == "no opening date in D" else OPENED))
        if mutate == "D with two parents":
            out("-C", clone, "checkout", "--quiet", "--detach", c)
            side = self.commit(clone, {"side.txt": "x\n"}, "Side line")
            out("-C", clone, "checkout", "--quiet", "--detach", d)
            out("-C", clone, "merge", "--quiet", "--no-ff", "-m", f"Merge side\n\n{OPENED}", side)
            d = out("-C", clone, "rev-parse", "HEAD")
        kwargs = {}
        if mutate == "receipt names another repository":
            kwargs["repository"] = "example/other"
        if mutate == "duplicate object header":
            unrelated = self.commit(self.checkout("unrelated"), {"x": "x\n"}, "unrelated")
            out("-C", clone, "fetch", "--quiet", self.tmp / "unrelated", unrelated)
            kwargs["header"] = (f"object {unrelated}\ntype commit\ntag 26.1.0\n"
                                f"tagger F <f@example.invalid> 1790000000 +0000\nobject {c}\n")
        next_commit = d[:12] if mutate == "abbreviated next commit" else d
        tag = forge_tag(clone, "26.1.0", c, next_commit, "26.2.0", **kwargs)
        out("-C", clone, "push", "--quiet", "--no-follow-tags", "--force", "origin",
            f"{c}:refs/heads/main", f"{tag}:refs/tags/26.1.0", f"{d}:refs/heads/dev")
        return target, d

    def test_hand_built_valid_receipt_is_accepted_as_control(self):
        """Control: the unmutated forgery validates, so each variant below fails for its own defect."""
        target, d = self.forged_state("none")
        _, operation = self.prepare(target)
        self.assertEqual((operation["version"], operation["candidate"]), ("26.2.0", d))

    def test_malformed_contract_receipts_block_mutation(self):
        """Continued history / Conflicts: each forged receipt defect blocks mutation as malformed."""
        cases = {
            "extra file in D": "must change only VERSION",
            "D with two parents": "exactly one parent",
            "no opening date in D": "does not record opening date",
            "wrong candidate VERSION": "candidate VERSION is not 26.1.0",
            "receipt names another repository": "repository/applicability do not match",
            "duplicate object header": "header must be exactly one object, type, tag and tagger line",
            "abbreviated next commit": "must be a full object ID",
        }
        for mutate, message in cases.items():
            with self.subTest(mutate):
                self.setUp()
                target, _ = self.forged_state(mutate)
                before = refs(target)
                with self.assertRaises(Refused) as caught:
                    self.prepare(target)
                self.assertEqual(caught.exception.code, "unsupported-state")
                self.assertIn("malformed contract receipt", caught.exception.detail)
                self.assertIn(message, caught.exception.detail)
                self.assertEqual(refs(target), before)


class ReleaseHistoryPredicateTests(GitTestCase):
    """The latest contract release fixes main; contract releases chain; dev continues the open line."""

    def released(self):
        target, _, b = self.bootstrapped()
        c = self.integrate(target, {"src/feature.py": "FEATURE = 1\n"}, "Feature")
        work, operation = self.release(target)
        return target, b, c, work, operation

    def assert_refused(self, target, message):
        before = refs(target)
        with self.assertRaises(Refused) as caught:
            self.prepare(target, name="refused op")
        self.assertEqual(caught.exception.code, "unsupported-state")
        self.assertIn(message, caught.exception.detail)
        self.assertEqual(refs(target), before)

    def test_main_rewound_after_release_is_refused(self):
        """Continued history: main must equal the latest contract release candidate."""
        target, b, _, _, _ = self.released()
        out("-C", self._dev_clones[target], "push", "--quiet", "--force", "origin", f"{b}:refs/heads/main")
        self.assert_refused(target, "main is not the latest contract release 26.1.0")

    def test_dev_replaced_by_unrelated_history_is_mixed_or_unknown(self):
        """dev must descend from the latest D: the operation's own store (which retains D) diagnoses MIXED;
        a fresh observer cannot reach D at all and reports UNKNOWN, never absence."""
        target, _, _, work, _ = self.released()
        orphan = self.checkout("unrelated dev")
        commit = self.commit(orphan, {"VERSION": "26.2.0\n", "multi-repo-stack.json": selector(
            "example/app", "lifecycle", shared_snapshot()[1])}, "Unrelated line")
        out("-C", orphan, "push", "--quiet", "--force", target, f"{commit}:refs/heads/dev")
        replaced = refs(target)
        outcome = transactions.reconcile(work)
        self.assertEqual(outcome.status, "MIXED")
        self.assertIn("dev does not descend from 26.1.0's next-line commit", outcome.detail)
        self.assertEqual(transactions.apply(work, now=NOW).status, "MIXED")
        with self.assertRaises(state.Unknown):
            self.prepare(target, name="fresh op")
        self.assertEqual(refs(target), replaced)

    def test_version_gap_between_contract_releases_is_refused(self):
        """Conflicts: contract releases must form a consistent forward version chain."""
        target, _, _, _, operation = self.released()
        clone = self._dev_clones[target]
        out("-C", clone, "pull", "--quiet", "--ff-only", "origin", "dev")
        c3 = self.commit(clone, {"VERSION": "26.3.0\n"}, "Skip a line")
        d3 = self.commit(clone, {"VERSION": "26.4.0\n"}, f"Open line 26.4.0\n\n{OPENED}")
        tag = forge_tag(clone, "26.3.0", c3, d3, "26.4.0")
        out("-C", clone, "push", "--quiet", "--no-follow-tags", "--force", "origin",
            f"{c3}:refs/heads/main", f"{tag}:refs/tags/26.3.0", f"{d3}:refs/heads/dev")
        self.assertEqual(refs(target)["refs/tags/26.1.0"], operation["tag"])
        self.assert_refused(target, "release 26.3.0 does not continue line 26.2.0")

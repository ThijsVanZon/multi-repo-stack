import hashlib
import json
import shutil
from datetime import datetime, timezone

from mrs import state, transactions
from mrs.transactions import Refused
from tests.support import (DEV, MAIN, NOW, GitTestCase, git, kill_tree, out, receiver_capabilities, refs,
                           start_kernel, wait_for)

TAG = "refs/tags/26.1.0"


def receipt_from_clone(test: GitTestCase, target, tag_ref: str) -> tuple[str, bytes, dict]:
    """Read a tag object from a fresh clone with plain plumbing, independent of the tool."""
    clone = test.tmp / f"fresh clone {len(list(test.tmp.iterdir()))}"
    out("clone", "--quiet", "--bare", target, clone)
    tag = out("-C", clone, "rev-parse", tag_ref)
    raw = git("-C", clone, "cat-file", "tag", tag).stdout
    head, _, message = raw.partition(b"\n\n")
    marker, _, body = message.partition(b"\n")
    test.assertEqual(marker, b"multi-repo-stack release receipt v1")
    return head.decode("utf-8"), body[:-1], json.loads(body)


class FirstTransactionTests(GitTestCase):
    def test_first_release_publishes_distinct_C_T_D_and_receipt_round_trips(self):
        """First transaction: main=C, annotated T directly targets C, dev=D with only the next VERSION."""
        target, _, b = self.bootstrapped()
        c = self.integrate(target, {"src/feature.py": "FEATURE = 1\n"}, "Integrate feature task")
        work, operation = self.prepare(target)
        self.assertEqual(refs(target), {DEV: c}, "preparation must not publish")

        outcome = transactions.apply(work, now=NOW)

        self.assertEqual((outcome.status, outcome.reason), ("APPLIED", "applied"))
        t, d = operation["tag"], operation["next"]["commit"]
        self.assertEqual(refs(target), {MAIN: c, TAG: t, DEV: d})
        self.assertEqual(len({c, t, d}), 3)
        self.assertEqual(out("-C", target, "cat-file", "-t", t), "tag")
        header, receipt_bytes, receipt = receipt_from_clone(self, target, TAG)
        self.assertIn(f"object {c}\ntype commit\ntag 26.1.0\n", header)
        self.assertEqual(out("-C", target, "rev-list", "--parents", "-n", "1", d), f"{d} {c}")
        self.assertEqual(out("-C", target, "diff-tree", "-r", "--name-status", "--no-commit-id", c, d), "M\tVERSION")
        self.assertEqual(git("-C", target, "cat-file", "blob", f"{d}:VERSION").stdout, b"26.2.0\n")
        self.assertIn("Next-line opening date (UTC): 2026-10-03", out("-C", target, "log", "-1", "--format=%B", d))
        self.assertEqual(hashlib.sha256(receipt_bytes).hexdigest(), operation["receipt_sha256"])
        self.assertEqual(receipt["repository"], "example/app")
        self.assertEqual((receipt["version"], receipt["candidate"]), ("26.1.0", c))
        self.assertEqual(receipt["next"], {"commit": d, "version": "26.2.0", "opened": "2026-10-03"})
        self.assertEqual(receipt["acceptance"]["verdict"]["kind"], "SIMULATED")
        self.assertEqual(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("ascii"), receipt_bytes)

    def test_next_line_follows_utc_calendar(self):
        """Time: a line released in a later UTC year opens YY.1.0 of that year."""
        target, _, _ = self.bootstrapped()
        _, operation = self.release(target, now=datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc))
        d = operation["next"]["commit"]
        self.assertEqual(git("-C", target, "cat-file", "blob", f"{d}:VERSION").stdout, b"27.1.0\n")

    def test_completed_operation_keeps_its_identity_across_new_year(self):
        """Time: a completed (or in-flight) operation is reconciled, not relabelled, in a later year."""
        target, _, _ = self.bootstrapped()
        work, operation = self.release(target, now=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc))
        before = refs(target)
        outcome = transactions.apply(work, now=datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc))
        self.assertEqual((outcome.status, outcome.reason), ("NOOP", "completed"))
        self.assertEqual(refs(target), before)
        self.assertEqual(git("-C", target, "cat-file", "blob", f"{operation['next']['commit']}:VERSION").stdout,
                         b"26.2.0\n")

    def test_unattempted_operation_from_another_year_must_be_prepared_again(self):
        """Time: an operation that is proven not applied keeps no identity across New Year."""
        target, _, b = self.bootstrapped()
        work, _ = self.prepare(target, now=datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc))
        outcome = transactions.apply(work, now=datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc))
        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "year-changed"))
        self.assertEqual(refs(target), {DEV: b})
        self.assertFalse((work / "attempts.log").exists())


class RaceTests(GitTestCase):
    def test_competing_dev_commit_after_observation_rejects_stale_operation(self):
        """Guarded races: a real competing dev commit after observation rejects the operation."""
        target, _, b = self.bootstrapped()
        work, _ = self.prepare(target)
        competitor = self.integrate(target, {"src/rival.py": "RIVAL = 1\n"}, "Competing task")

        outcome = transactions.apply(work, now=NOW)

        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "stale"))
        self.assertEqual(refs(target), {DEV: competitor})

    def test_release_leases_reject_and_unleased_control_would_overwrite(self):
        """Guarded races: the explicit leases themselves reject; the same refspecs forced without them win."""
        target, _, b = self.bootstrapped()
        work, operation = self.prepare(target)
        competitor = self.integrate(target, {"src/rival.py": "RIVAL = 1\n"}, "Competing task")
        control = self.tmp / "control target.git"
        shutil.copytree(target, control)

        self.assertEqual(transactions.push(work), "stale")
        self.assertEqual(refs(target), {DEV: competitor})

        updates = [f"{new}:{ref}" for ref, new in operation["updates"].items()]
        git("-C", work / "repo.git", "push", "--quiet", "--atomic", "--force", "--no-follow-tags", control, *updates)
        self.assertEqual(refs(control)[DEV], operation["next"]["commit"], "control: competitor would be lost")

    def test_update_after_advertisement_before_server_update_is_rejected(self):
        """Guarded races: dev advanced after advertisement, before the server ref update; competitor survives.
        Proves the server-side old-value check on a real dev change (local Git evidence)."""
        target, _, b = self.bootstrapped()
        work, operation = self.prepare(target)
        self.hook(target, "pre-receive", """cat > fixture-commands
( unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  old=$(git rev-parse refs/heads/dev)
  new=$(echo "competing integration" | git commit-tree "$old^{tree}" -p "$old")
  git update-ref refs/heads/dev "$new" "$old" && echo "$new" > fixture-competitor )
exit 0
""")

        outcome = transactions.apply(work, now=NOW)

        competitor = (target / "fixture-competitor").read_text(encoding="utf-8").strip()
        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "stale"))
        self.assertEqual(refs(target), {DEV: competitor})
        sent = [line.split() for line in (target / "fixture-commands").read_text(encoding="utf-8").splitlines()]
        self.assertIn([b, operation["next"]["commit"], DEV], sent, "client expected dev=C in the real update")

    def test_main_and_tag_leases_reject_in_the_guarded_push_itself(self):
        """Guarded races: main/tag created after observation are rejected by the push leases themselves."""
        for ref in (MAIN, TAG):
            with self.subTest(ref=ref):
                self.setUp()
                target, _, b = self.bootstrapped()
                work, _ = self.prepare(target)
                rival = self.checkout("rival")
                rival_commit = self.commit(rival, {"rival.txt": "x\n"}, "rival")
                out("-C", rival, "push", "--quiet", target, f"HEAD:{ref}")
                self.assertEqual(transactions.push(work), "stale")
                self.assertEqual(refs(target), {DEV: b, ref: rival_commit})

    def test_foreign_tag_appearing_after_preparation_blocks_application(self):
        """Continued history: state is re-validated at apply time, not only at preparation."""
        target, source, b = self.bootstrapped()
        work, _ = self.prepare(target)
        out("-C", source, "tag", "25.3.0", b)
        out("-C", source, "push", "--quiet", "--no-follow-tags", target, "refs/tags/25.3.0")
        before = refs(target)
        outcome = transactions.apply(work, now=NOW)
        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "unsupported-state"))
        self.assertIn("foreign version-shaped tag", outcome.detail)
        self.assertEqual(refs(target), before)
        self.assertFalse((work / "attempts.log").exists())

    def test_stale_main_and_conflicting_tag_reject(self):
        """Guarded races: a main or tag created after preparation rejects the whole release."""
        for ref in (MAIN, TAG):
            with self.subTest(ref=ref):
                self.setUp()
                target, _, b = self.bootstrapped()
                work, _ = self.prepare(target)
                rival = self.checkout("rival")
                rival_commit = self.commit(rival, {"rival.txt": "x\n"}, "rival")
                out("-C", rival, "push", "--quiet", target, f"HEAD:{ref}")
                outcome = transactions.apply(work, now=NOW)
                self.assertEqual(outcome.status, "REFUSED")
                self.assertEqual(outcome.reason, "conflict" if ref == TAG else "stale")
                self.assertEqual(refs(target), {DEV: b, ref: rival_commit})


class AtomicTests(GitTestCase):
    def test_rejecting_each_destination_publishes_none_of_the_others(self):
        """Atomic failure: a policy rejection of main, the tag or dev leaves every ref unchanged."""
        for rejected in (MAIN, TAG, DEV):
            with self.subTest(rejected=rejected):
                self.setUp()
                target, _, b = self.bootstrapped()
                work, _ = self.prepare(target)
                self.hook(target, "update", f'[ "$1" = "{rejected}" ] && {{ echo "fixture policy" >&2; exit 1; }}\n'
                                            "exit 0\n")
                outcome = transactions.apply(work, now=NOW)
                self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "policy"))
                self.assertEqual(refs(target), {DEV: b})

    def test_control_without_atomic_would_leave_a_released_main_and_tag(self):
        """Atomic failure control: the same rejected-dev fixture publishes main/tag when --atomic is absent."""
        target, _, b = self.bootstrapped()
        work, operation = self.prepare(target)
        self.hook(target, "update", f'[ "$1" = "{DEV}" ] && exit 1\nexit 0\n')
        updates = [f"{new}:{ref}" for ref, new in operation["updates"].items()]
        git("-C", work / "repo.git", "push", "--no-follow-tags", target, *updates, check=False)
        self.assertEqual(refs(target), {MAIN: b, TAG: operation["tag"], DEV: b})

    def test_atomic_unsupported_is_a_refusal_without_fallback(self):
        """Atomic failure: a receiver without atomic support refuses; no sequential fallback is tried."""
        target, _, b = self.bootstrapped()
        work, _ = self.prepare(target)
        self.assertIn(b"atomic", receiver_capabilities(target), "control: the receiver advertises atomic by default")
        self.receiver_config("receive.advertiseAtomic", "false")
        self.assertNotIn(b"atomic", receiver_capabilities(target), "the receiver no longer advertises atomic")

        outcome = transactions.apply(work, now=NOW)

        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "atomic-unsupported"))
        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(len((work / "attempts.log").read_text(encoding="utf-8").splitlines()), 1)


class RetryTests(GitTestCase):
    def test_identical_repeat_preserves_exact_T_D_and_refs(self):
        """Retry: repeating the exact operation is a successful no-op with the same objects."""
        target, _, _ = self.bootstrapped()
        work, operation = self.release(target)
        before = refs(target)
        outcome = transactions.apply(work, now=NOW)
        self.assertEqual((outcome.status, outcome.reason), ("NOOP", "completed"))
        self.assertEqual(refs(target), before)
        self.assertEqual(before[TAG], operation["tag"])

    def test_lost_successful_response_is_identified_by_read_only_reconciliation(self):
        """Retry and uncertainty: the server updates all refs, the applying process dies before any
        response; read-only reconciliation finds completion and a retry keeps exact T and D."""
        target, _, b = self.bootstrapped()
        work, operation = self.prepare(target)
        self.hook(target, "post-receive", """cat >/dev/null
: > fixture-updated
i=0
while [ ! -f fixture-release ] && [ $i -lt 2400 ]; do sleep 0.05; i=$((i+1)); done
exit 0
""")
        proc = start_kernel("from datetime import datetime; "
                            "transactions.apply(Path(sys.argv[2]), now=datetime.fromisoformat(sys.argv[3]))",
                            str(work), NOW.isoformat())
        wait_for(target / "fixture-updated")
        kill_tree(proc)
        (target / "fixture-release").write_text("", encoding="utf-8")
        self.assertNotEqual(proc.returncode, 0)

        self.assertEqual(transactions.reconcile(work).status, "COMPLETED")
        expected = {MAIN: b, TAG: operation["tag"], DEV: operation["next"]["commit"]}
        self.assertEqual(refs(target), expected)
        retry = transactions.apply(work, now=NOW)
        self.assertEqual((retry.status, retry.reason), ("NOOP", "completed"))
        self.assertEqual(refs(target), expected)

    def test_different_tag_object_peeling_to_C_is_not_completion(self):
        """Retry and uncertainty: another release of the same C with a different tag object is not this one."""
        target, _, b = self.bootstrapped()
        mine, _ = self.prepare(target, name="my op")
        other, other_operation = self.prepare(target, name="other op", now=NOW.replace(hour=13))
        self.assertEqual(transactions.apply(other, now=NOW).status, "APPLIED")
        after_other = refs(target)
        self.assertEqual(out("-C", target, "rev-parse", f"{TAG}^{{commit}}"), b)

        self.assertEqual(transactions.reconcile(mine).status, "DIVERGED")
        outcome = transactions.apply(mine, now=NOW)
        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "conflict"))
        self.assertEqual(refs(target), after_other)
        self.assertEqual(after_other[TAG], other_operation["tag"])

    def test_mixed_state_stops_without_repair(self):
        """Retry and uncertainty: a partially applied state (tag and main, dev unchanged) is MIXED."""
        target, _, b = self.bootstrapped()
        work, operation = self.prepare(target)
        git("-C", work / "repo.git", "push", "--quiet", "--no-follow-tags", target,
            f"{operation['tag']}:{TAG}", f"{b}:{MAIN}")
        mixed = refs(target)

        self.assertEqual(transactions.reconcile(work).status, "MIXED")
        outcome = transactions.apply(work, now=NOW)

        self.assertEqual(outcome.status, "MIXED")
        self.assertEqual(refs(target), mixed)
        self.assertFalse((work / "attempts.log").exists(), "no repair push")

    def test_server_applying_only_dev_yields_mixed_not_success(self):
        """Retry and uncertainty: a server that applies dev itself and then fails the push leaves MIXED."""
        target, _, b = self.bootstrapped()
        work, operation = self.prepare(target)
        self.receiver_config("receive.procReceiveRefs", DEV)
        (target / "applied-target").write_text(operation["next"]["commit"] + "\n", encoding="utf-8")
        self.hook(target, "proc-receive", f""": > fixture-proc-receive-ran
( unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  git update-ref {DEV} "$(cat applied-target)" {b} )
exit 1
""")
        outcome = transactions.apply(work, now=NOW)
        self.assertTrue((target / "fixture-proc-receive-ran").is_file(), "the receiver ran the proc-receive hook")
        self.assertEqual(outcome.status, "MIXED")
        self.assertEqual(refs(target), {DEV: operation["next"]["commit"]})


class HistoryTests(GitTestCase):
    def test_retry_after_dev_advances_and_second_release_keeps_history(self):
        """Continued history: historical retry never rewinds; a second release keeps the first tag."""
        target, _, c1 = self.bootstrapped()
        first, op1 = self.release(target, name="first op")
        advanced = self.integrate(target, {"src/next.py": "NEXT = 1\n"}, "Integrate next-line task")
        self.assertEqual(transactions.apply(first, now=NOW).status, "NOOP")
        self.assertEqual(refs(target)[DEV], advanced)

        _, op2 = self.release(target, name="second op")

        self.assertEqual(refs(target), {MAIN: advanced, TAG: op1["tag"], "refs/tags/26.2.0": op2["tag"],
                                        DEV: op2["next"]["commit"]})
        self.assertEqual(git("-C", target, "cat-file", "blob", f"{op2['next']['commit']}:VERSION").stdout,
                         b"26.3.0\n")
        self.assertEqual(transactions.apply(first, now=NOW).status, "NOOP")
        self.assertEqual(refs(target)[MAIN], advanced)

    def test_releases_are_ordered_numerically(self):
        """Conflicts: 26.10.0 follows 26.9.0 (lexicographic order would invert them)."""
        target, _, _ = self.bootstrapped(version="26.9.0")
        self.release(target, name="op 9")
        _, op10 = self.release(target, name="op 10")
        self.assertEqual(refs(target)[MAIN], op10["candidate"])
        _, op11 = self.prepare(target, name="op 11")
        self.assertEqual(op11["version"], "26.11.0")

    def test_mvp_main_without_contract_release_is_refused(self):
        """Continued history: an MVP main is not release truth."""
        target, _, b = self.bootstrapped()
        mvp = self.checkout("mvp")
        mvp_commit = self.commit(mvp, {"index.html": "<p>mvp</p>\n"}, "MVP")
        out("-C", mvp, "push", "--quiet", target, "HEAD:refs/heads/main")
        with self.assertRaises(Refused) as caught:
            self.prepare(target)
        self.assertEqual(caught.exception.code, "unsupported-state")
        self.assertIn("main exists without any contract release", caught.exception.detail)
        self.assertEqual(refs(target), {DEV: b, MAIN: mvp_commit})

    def test_foreign_and_malformed_version_shaped_tags_block_and_are_preserved(self):
        """Continued history: lightweight/annotated foreign version-shaped tags and malformed receipts block;
        tags with other names are ignored and preserved."""
        cases = {
            "lightweight": ["tag", "25.3.0"],
            "annotated without receipt": ["tag", "-a", "25.3.0", "-m", "hand-made release"],
            "malformed receipt": ["tag", "-a", "25.3.0", "-m", "multi-repo-stack release receipt v1\n{broken"],
        }
        for label, command in cases.items():
            with self.subTest(label):
                self.setUp()
                target, source, b = self.bootstrapped()
                out("-C", source, *command, b)
                out("-C", source, "push", "--quiet", "--no-follow-tags", target, "refs/tags/25.3.0")
                before = refs(target)
                with self.assertRaises(Refused) as caught:
                    self.prepare(target)
                self.assertEqual(caught.exception.code, "unsupported-state")
                self.assertIn("malformed" if "malformed" in label else "foreign", caught.exception.detail)
                self.assertEqual(refs(target), before)

        self.setUp()
        target, source, b = self.bootstrapped()
        out("-C", source, "tag", "-a", "v1-notes", "-m", "unrelated", b)
        out("-C", source, "push", "--quiet", "--no-follow-tags", target, "refs/tags/v1-notes")
        foreign = refs(target)["refs/tags/v1-notes"]
        self.release(target)
        self.assertEqual(refs(target)["refs/tags/v1-notes"], foreign)

    def test_observation_failure_during_release_is_unknown(self):
        """Observation: a fetch failure on an existing target is UNKNOWN, not absence or refusal."""
        target, _, b = self.bootstrapped()
        loose = target / "objects" / b[:2] / b[2:]
        self.assertTrue(loose.exists())
        loose.chmod(0o644)
        loose.unlink()
        with self.assertRaises(state.Unknown):
            self.prepare(target)

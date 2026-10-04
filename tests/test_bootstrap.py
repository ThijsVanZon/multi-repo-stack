import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

from mrs import state, transactions
from mrs.transactions import Refused
from tests.support import DEV, GitTestCase, git, out, refs

# A proc-receive hook speaking Git's pkt-line protocol: it records the commands it was sent, optionally
# lets a competitor create dev first, and reports `ng <ref> <reason>` for every command.
PROC_RECEIVE = r'''import os, subprocess, sys
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
def read():
    lines = []
    while True:
        size = int(stdin.read(4), 16)
        if size == 0:
            return lines
        lines.append(stdin.read(size - 4))
def write(*lines):
    stdout.write(b"".join(b"%04x" % (len(line) + 4) + line for line in lines) + b"0000")
    stdout.flush()
read()
write(b"version=1\0")
commands = [command.split() for command in read()]
with open("fixture-commands", "wb") as record:
    record.write(b"\n".join(b" ".join(command) for command in commands) + b"\n")
if sys.argv[2]:
    quarantine = ("GIT_QUARANTINE_PATH", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
    env = {key: value for key, value in os.environ.items() if key not in quarantine}
    subprocess.run(["git", "update-ref", "refs/heads/dev", sys.argv[2], ""], env=env, check=True)
write(*[b"ng " + command[2] + b" " + sys.argv[1].encode("ascii") for command in commands])
'''


class BootstrapTests(GitTestCase):
    def test_empty_target_receives_exact_B_on_dev_only_and_repeat_is_noop(self):
        """Local bootstrap: exact B at dev only; main and tags stay absent; exact repeat is a no-op."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        work = self.tmp / "bootstrap op"
        operation = transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        self.assertEqual(refs(target), {}, "preparation must not publish")
        self.assertEqual(operation["updates"], {DEV: b})

        outcome = transactions.apply(work)

        self.assertEqual((outcome.status, outcome.reason), ("APPLIED", "applied"))
        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(out("-C", target, "cat-file", "blob", f"{b}:VERSION"), "26.1.0")
        repeat = transactions.apply(work)
        self.assertEqual((repeat.status, repeat.reason), ("NOOP", "completed"))
        self.assertEqual(refs(target), {DEV: b})

    def test_existing_readme_target_refuses(self):
        """Local bootstrap: a README commit already makes the target nonempty."""
        target = self.bare("target.git")
        readme = self.checkout("provider readme")
        commit = self.commit(readme, {"README.md": "# created by the provider\n"}, "Initial commit")
        out("-C", readme, "push", "--quiet", target, "HEAD:refs/heads/main")
        source, b = self.lifecycle_source()

        with self.assertRaises(Refused) as caught:
            transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=self.tmp / "op")

        self.assertEqual(caught.exception.code, "nonempty")
        self.assertEqual(refs(target), {"refs/heads/main": commit})

    def test_stray_ref_target_refuses(self):
        """Local bootstrap: any stray ref makes the target nonempty, including non-branch refs."""
        for stray in ("refs/heads/feature/x", "refs/notes/commits", "refs/tags/experiment"):
            with self.subTest(stray=stray):
                target = self.bare(f"target {stray.replace('/', ' ')}.git")
                other = self.checkout(f"other {stray.replace('/', ' ')}")
                commit = self.commit(other, {"x.txt": "x\n"}, "stray")
                out("-C", other, "push", "--quiet", target, f"HEAD:{stray}")
                source, b = self.lifecycle_source(name=f"source {stray.replace('/', ' ')}")
                with self.assertRaises(Refused) as caught:
                    transactions.prepare_bootstrap(source=source, commit=b, remote=target,
                                                   work=self.tmp / f"op {stray.replace('/', ' ')}")
                self.assertEqual(caught.exception.code, "nonempty")
                self.assertEqual(refs(target), {stray: commit})

    def test_competing_dev_after_observation_refuses_and_survives(self):
        """Local bootstrap: expected-absent dev created by a competitor after preparation is refused."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        rival = self.checkout("rival")
        competitor = self.commit(rival, {"rival.txt": "first\n"}, "Competing initializer")
        out("-C", rival, "push", "--quiet", target, "HEAD:refs/heads/dev")

        outcome = transactions.apply(work)

        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "conflict"))
        self.assertEqual(refs(target), {DEV: competitor})

    def test_guarded_push_lease_rejects_and_unleased_control_would_overwrite(self):
        """Local bootstrap: the expected-absent lease itself rejects; a force push without it overwrites."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        rival = self.checkout("rival")
        competitor = self.commit(rival, {"rival.txt": "first\n"}, "Competing initializer")
        out("-C", rival, "push", "--quiet", target, "HEAD:refs/heads/dev")
        control = self.tmp / "control target.git"
        shutil.copytree(target, control)

        self.assertEqual(transactions.push(work), "stale")
        self.assertEqual(refs(target), {DEV: competitor})

        git("-C", work / "repo.git", "push", "--quiet", "--force", control, f"{b}:refs/heads/dev")
        self.assertEqual(refs(control), {DEV: b}, "control: without the lease the competitor would be lost")

    def test_competing_dev_after_advertisement_is_rejected_by_server(self):
        """Local bootstrap: a dev created after advertisement, before the server update, survives.
        Local Git server-side evidence, not GitHub policy proof."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        rival = self.checkout("rival")
        competitor = self.commit(rival, {"rival.txt": "first\n"}, "Competing initializer")
        out("-C", rival, "push", "--quiet", target, f"{competitor}:refs/fixture/competitor")
        out("-C", target, "update-ref", "-d", "refs/fixture/competitor")
        (target / "competitor").write_text(competitor + "\n", encoding="utf-8")
        self.hook(target, "pre-receive", """cat > fixture-commands
( unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  git update-ref refs/heads/dev "$(cat competitor)" "" )
exit 0
""")
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)

        outcome = transactions.apply(work)

        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "stale"))
        self.assertEqual(refs(target), {DEV: competitor})
        sent = (target / "fixture-commands").read_text(encoding="utf-8").split()
        self.assertEqual(sent, ["0" * len(b), b, "refs/heads/dev"], "client sent create-only expectation")

    def test_server_reported_create_conflict_is_stale_and_other_rejections_are_not(self):
        """Local bootstrap / portability: Git 2.52 reports a create-only lease that loses the server-side race
        as '[remote rejected] (reference already exists)'. A proc-receive hook reproduces that report while a
        real competitor takes dev: stale, competitor kept. Another server reason of the same shape stays
        'rejected'. Local Git evidence, not GitHub policy proof."""
        source, b = self.lifecycle_source()
        rival = self.checkout("rival")
        competitor = self.commit(rival, {"rival.txt": "first\n"}, "Competing initializer")
        cases = (("reference already exists", competitor, ("REFUSED", "stale"), {DEV: competitor}),
                 ("fixture policy refuses dev", "", ("REFUSED", "rejected"), {}))
        for reason, rival_dev, expected, final in cases:
            with self.subTest(reason):
                target = self.bare(f"target {reason}.git")
                out("-C", rival, "push", "--quiet", target, f"{competitor}:refs/fixture/competitor")
                out("-C", target, "update-ref", "-d", "refs/fixture/competitor")
                self.receiver_config("receive.procReceiveRefs", DEV)
                (target / "fixture-proc-receive.py").write_text(PROC_RECEIVE, encoding="utf-8")
                self.hook(target, "proc-receive", f'exec "{Path(sys.executable).as_posix()}" -I -B '
                                                  f'fixture-proc-receive.py "{reason}" "{rival_dev}"\n')
                work = self.tmp / f"op {reason}"
                transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)

                outcome = transactions.apply(work)

                self.assertTrue((target / "fixture-commands").is_file(), "the receiver ran the proc-receive hook")
                sent = (target / "fixture-commands").read_text(encoding="utf-8").split()
                self.assertEqual(sent, ["0" * len(b), b, DEV], "client sent create-only expectation")
                self.assertEqual((outcome.status, outcome.reason), expected, outcome)
                self.assertEqual(refs(target), final)

    def test_corrupt_operation_record_fails_clearly(self):
        """Retry and uncertainty: an unreadable or incomplete operation record is refused with a clear reason
        by apply, push and reconcile; nothing is pushed."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        record = work / "operation.json"
        complete = json.loads(record.read_bytes())
        incomplete = json.dumps({key: value for key, value in complete.items() if key != "updates"})
        for label, data in (("truncated", record.read_bytes()[:40]), ("not an object", b"[]\n"),
                            ("missing field", incomplete.encode("utf-8"))):
            with self.subTest(label):
                record.write_bytes(data)
                outcome = transactions.apply(work)
                self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "operation"))
                for call in (transactions.push, transactions.reconcile):
                    with self.assertRaises(Refused) as caught:
                        call(work)
                    self.assertEqual(caught.exception.code, "operation")
        self.assertEqual(refs(target), {})
        self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")

    def test_observation_failures_are_unknown_never_absence(self):
        """Local bootstrap: auth/transport/output/fetch failures stay UNKNOWN and never allow bootstrap."""
        source, b = self.lifecycle_source()
        corrupt = self.bare("corrupt refs.git")
        (corrupt / "packed-refs").write_text("garbage line\n", encoding="utf-8")
        broken = self.bare("broken ref.git")
        (broken / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (broken / "refs" / "heads" / "dev").write_text("not an object id\n", encoding="utf-8")
        missing = self.bare("missing object.git")
        (missing / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (missing / "refs" / "heads" / "dev").write_text("1" * len(b) + "\n", encoding="utf-8")
        for target in (corrupt, broken, missing):
            with self.subTest(target=target.name):
                with self.assertRaises(state.Unknown):
                    transactions.prepare_bootstrap(source=source, commit=b, remote=target,
                                                   work=self.tmp / f"op {target.name}")
        self.assertFalse((corrupt / "refs" / "heads" / "dev").exists())
        self.assertEqual((broken / "refs" / "heads" / "dev").read_text(encoding="utf-8"), "not an object id\n")

        target = self.bare("target.git")
        work = self.tmp / "op later corrupted"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        (target / "packed-refs").write_text("garbage line\n", encoding="utf-8")
        outcome = transactions.apply(work)
        self.assertEqual((outcome.status, outcome.reason), ("UNKNOWN", "observation"))
        self.assertFalse((target / "refs" / "heads" / "dev").exists())
        self.assertFalse((work / "attempts.log").exists(), "no push may be attempted on an unknown observation")

    def test_lost_or_garbled_response_is_reconciled_from_observation(self):
        """Retry and uncertainty: the server applies B but reports failure; the kernel reconciles to completion."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        self.receiver_config("receive.procReceiveRefs", "refs/heads/dev")
        (target / "applied-target").write_text(b + "\n", encoding="utf-8")
        self.hook(target, "proc-receive", """: > fixture-proc-receive-ran
( unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  git update-ref refs/heads/dev "$(cat applied-target)" "" )
exit 1
""")
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)

        outcome = transactions.apply(work)

        self.assertTrue((target / "fixture-proc-receive-ran").is_file(), "the receiver ran the proc-receive hook")
        self.assertEqual((outcome.status, outcome.reason), ("APPLIED", "rejected"))
        self.assertIn("reconciliation found completion", outcome.detail)
        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(len((work / "attempts.log").read_text(encoding="utf-8").splitlines()), 1)

    def test_ambient_destination_rewrites_are_refused_and_nothing_is_published(self):
        """Local bootstrap: insteadOf/pushInsteadOf or a remote named like the destination would redirect the
        push (paths with spaces included); preparation and application refuse, and the sink stays empty."""
        source, b = self.lifecycle_source()
        sink = self.bare("other sink with spaces.git", fixture=False)
        target = self.bare("target.git")
        for key in (f"url.{sink}.insteadOf", f"url.{sink}.pushInsteadOf", f"remote.{target}.pushurl"):
            with self.subTest(key=key.split(".")[-1]):
                value = str(target) if key.startswith("url.") else str(sink)
                out("config", "--global", key, value)
                with self.assertRaises(Refused) as caught:
                    transactions.prepare_bootstrap(source=source, commit=b, remote=target,
                                                   work=self.tmp / f"op {key.split('.')[-1]}")
                self.assertEqual(caught.exception.code, "destination")
                out("config", "--global", "--unset", key)
        work = self.tmp / "op prepared before rewrite"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        out("config", "--global", f"url.{sink}.pushInsteadOf", str(target))
        outcome = transactions.apply(work)
        self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "destination"))
        self.assertEqual((refs(target), refs(sink)), ({}, {}))

    def test_hidden_refs_make_absence_unobservable(self):
        """Observation: with hideRefs configured, an advertisement cannot prove absence; result is UNKNOWN."""
        target, source, b = self.bootstrapped()
        empty = self.bare("empty target.git")
        out("config", "--global", "uploadpack.hideRefs", "refs/")
        masking = self.tmp / "empty masking.gitconfig"
        masking.write_text("", encoding="utf-8")
        # GIT_CONFIG changes what `git config` reads but not what upload-pack hides, so it must not mask the check.
        for remote, env in ((target, {}), (empty, {}), (target, {"GIT_CONFIG": str(masking)})):
            with self.subTest(remote=remote.name, masked=bool(env)):
                with mock.patch.dict(os.environ, env), self.assertRaises(state.Unknown):
                    transactions.prepare_bootstrap(source=source, commit=b, remote=remote,
                                                   work=self.tmp / f"op {remote.name} {len(env)}")
        self.assertEqual(refs(empty), {})

    def test_bootstrap_retry_in_unsupported_state_is_mixed_not_noop(self):
        """Retry: B is on dev but the state is no longer a supported lifecycle state; no NOOP is reported."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        work = self.bootstrap(target, source, b)
        mvp = self.checkout("mvp")
        mvp_commit = self.commit(mvp, {"index.html": "<p>mvp</p>\n"}, "MVP")
        out("-C", mvp, "push", "--quiet", target, "HEAD:refs/heads/main")
        outcome = transactions.apply(work)
        self.assertEqual(outcome.status, "MIXED")
        self.assertIn("main exists without any contract release", outcome.detail)
        with self.assertRaises(Refused) as caught:
            transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=self.tmp / "op again")
        self.assertEqual(caught.exception.code, "mixed")
        self.assertEqual(refs(target), {DEV: b, "refs/heads/main": mvp_commit})

    def test_preparation_rejects_unusable_inputs(self):
        """Local bootstrap: non-fixture, relative or nested destinations and dirty work dirs are refused."""
        source, b = self.lifecycle_source()
        unmarked = self.bare("unmarked.git", fixture=False)
        cases = {
            "unmarked real-looking target": dict(remote=unmarked, work=self.tmp / "op1"),
            "relative destination": dict(remote="target.git", work=self.tmp / "op2"),
            "url destination": dict(remote="https://example.invalid/owner/repo.git", work=self.tmp / "op3"),
            "work inside caller checkout": dict(remote=self.bare("t.git"), work=source / "op"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                with self.assertRaises(Refused) as caught:
                    transactions.prepare_bootstrap(source=source, commit=b, **kwargs)
                self.assertIn(caught.exception.code, ("destination", "work"))
        self.assertEqual(refs(unmarked), {})
        self.assertFalse((source / "op").exists())
        with self.assertRaises(Refused) as caught:
            transactions.prepare_bootstrap(source=source, commit=b[:12], remote=self.bare("t2.git"),
                                           work=self.tmp / "op4")
        self.assertEqual(caught.exception.code, "source")
        self.assertFalse((self.tmp / "op1").exists(), "refused before any scratch was created")

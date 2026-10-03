import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from mrs import state, transactions
from mrs.transactions import Refused
from tests.support import (AMBIENT_GLOBAL_CONFIG, DEV, MAIN, NOW, GitTestCase, git, out, refs,
                           shared_snapshot)

EMPTY_PUSH_PREFIX = "empty pushInsteadOf prefix"
EMPTY_PREFIX = "empty insteadOf prefix"
MASKED = "global pushInsteadOf hidden from git config by GIT_CONFIG"
NAMED_MASKED = "global remote named like the destination, hidden from git config by GIT_CONFIG"


def inspect(remote) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, "-I", "-B", str(shared_snapshot()[0] / "mrs"), "inspect",
                           "--remote", str(remote), "--json"], capture_output=True)
    return proc.returncode, json.loads(proc.stdout.decode("utf-8"))


class DestinationTests(GitTestCase):
    """Single effective destination: Git's own resolution of the requested path is checked before any
    observation or push. On refusal the intended and the would-be repository both keep their refs."""

    def reset_global_config(self) -> None:
        (self.tmp / "ambient global.gitconfig").write_text(AMBIENT_GLOBAL_CONFIG, encoding="utf-8")

    def redirect(self, target: Path, mode: str, label: str) -> tuple[Path | None, dict]:
        """Install one redirection of `target`; return the unintended repository Git would publish to
        (None when no repository can exist there) and the environment the redirection needs."""
        if mode in (MASKED, NAMED_MASKED):
            sink = self.bare(f"unmarked sink {label}.git", fixture=False)
            if mode == MASKED:
                out("config", "--global", f"url.{sink}.pushInsteadOf", str(target))
            else:  # Git ignores remote names starting with "/" (POSIX paths); on Windows the push follows it
                out("config", "--global", f"remote.{target}.pushurl", str(sink))
            masking = self.tmp / f"empty masking {label}.gitconfig"
            masking.write_text("", encoding="utf-8")
            return (sink if mode == MASKED or os.name == "nt" else None), {"GIT_CONFIG": str(masking)}
        key = "pushInsteadOf" if mode == EMPTY_PUSH_PREFIX else "insteadOf"
        base = str(self.tmp / f"redirected {label}")
        out("config", "--global", f"url.{base}.{key}", "")  # an empty prefix matches every URL
        if os.name == "nt":  # base + "C:\..." is not a valid Windows path: no repository can receive it
            return None, {}
        return self.bare(base + str(target), fixture=False), {}

    def assert_redirection_is_real(self, store: Path, target: Path, sink: Path | None, env: dict, commit: str):
        """Control: a plain push from the same store under the same configuration lands in the sink."""
        if sink is None:
            return
        with mock.patch.dict(os.environ, env):
            git("-C", store, "push", "--quiet", "--no-follow-tags", target, f"{commit}:refs/heads/control")
        self.assertIn("refs/heads/control", refs(sink))
        self.assertNotIn("refs/heads/control", refs(target))

    def test_redirected_bootstrap_publishes_nowhere(self):
        """Local bootstrap: an empty rewrite prefix, or a global rewrite or remote that GIT_CONFIG hides from
        `git config`, refuses preparation, application and reconciliation before anything is observed or pushed."""
        source, b = self.lifecycle_source()
        for index, mode in enumerate((EMPTY_PUSH_PREFIX, EMPTY_PREFIX, MASKED, NAMED_MASKED)):
            with self.subTest(mode):
                self.reset_global_config()
                target = self.bare(f"target {index}.git")
                work = self.tmp / f"bootstrap op {index}"
                transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
                sink, env = self.redirect(target, mode, str(index))

                with mock.patch.dict(os.environ, env):
                    outcome = transactions.apply(work)
                    with self.assertRaises(Refused) as reconciled:
                        transactions.reconcile(work)
                    with self.assertRaises(Refused) as prepared:
                        transactions.prepare_bootstrap(source=source, commit=b, remote=target,
                                                       work=self.tmp / f"bootstrap op {index} again")

                self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "destination"))
                self.assertEqual((reconciled.exception.code, prepared.exception.code), ("destination", "destination"))
                self.assertEqual(refs(target), {})
                if sink is not None:
                    self.assertEqual(refs(sink), {})
                self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
                self.assert_redirection_is_real(work / "repo.git", target, sink, env, b)

    def test_redirected_release_publishes_nowhere(self):
        """Atomic release: the same redirections refuse preparation and application of main/tag/dev;
        the intended target keeps dev=B without main or tag and the unintended repository stays empty."""
        source, b = self.lifecycle_source()
        for index, mode in enumerate((EMPTY_PUSH_PREFIX, MASKED, NAMED_MASKED)):
            with self.subTest(mode):
                self.reset_global_config()
                target = self.bare(f"target {index}.git")
                self.bootstrap(target, source, b, name=f"bootstrap op {index}")
                work, _ = self.prepare(target, name=f"release op {index}")
                sink, env = self.redirect(target, mode, str(index))

                with mock.patch.dict(os.environ, env):
                    outcome = transactions.apply(work, now=NOW)
                    with self.assertRaises(Refused) as prepared:
                        self.prepare(target, name=f"release op {index} again")

                self.assertEqual((outcome.status, outcome.reason), ("REFUSED", "destination"))
                self.assertEqual(prepared.exception.code, "destination")
                self.assertEqual(refs(target), {DEV: b})
                if sink is not None:
                    self.assertEqual(refs(sink), {})
                self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
                self.assert_redirection_is_real(work / "repo.git", target, sink, env, b)

    def test_unreadable_configuration_is_unknown_and_publishes_nothing(self):
        """Observation: configuration Git cannot read is UNKNOWN, never 'no redirection'; neither a
        bootstrap nor a release is observed or pushed."""
        release_target, source, b = self.bootstrapped()
        release_work, _ = self.prepare(release_target)
        empty = self.bare("empty target.git")
        bootstrap_work = self.tmp / "pending bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=empty, work=bootstrap_work)
        with open(self.tmp / "ambient global.gitconfig", "a", encoding="utf-8") as config:
            config.write('[url "unreadable"]\n\tpushInsteadOf\n')  # Git rejects a rewrite without a value

        for work in (bootstrap_work, release_work):
            with self.subTest(work.name):
                self.assertEqual(transactions.apply(work, now=NOW).status, "UNKNOWN")
                self.assertEqual(transactions.reconcile(work).status, "UNKNOWN")
                self.assertFalse((work / "attempts.log").exists(), "no push may be attempted")
        with self.assertRaises(state.Unknown):
            transactions.prepare_bootstrap(source=source, commit=b, remote=empty, work=self.tmp / "op again")
        code, report = inspect(empty)
        self.assertEqual((code, report["kind"]), (4, "UNKNOWN"))
        self.assertEqual((refs(empty), refs(release_target)), ({}, {DEV: b}))

    def test_inspect_refuses_a_redirected_target_instead_of_reporting_it_empty(self):
        """Observation: inspect checks the same resolution; a target redirected to an empty repository is
        refused, not reported EMPTY. Without the redirection the requested repository's truth returns."""
        target = self.bare("target.git")
        source, b = self.lifecycle_source()
        out("-C", source, "push", "--quiet", "--no-follow-tags", target, f"{b}:{MAIN}")
        sink = self.bare("empty other.git", fixture=False)
        self.assertEqual(inspect(target)[1]["kind"], "UNSUPPORTED")
        masking = self.tmp / "empty masking.gitconfig"
        masking.write_text("", encoding="utf-8")
        cases = {
            "insteadOf": ((f"url.{sink}.insteadOf", str(target)), {}),
            "insteadOf hidden by GIT_CONFIG": ((f"url.{sink}.insteadOf", str(target)), {"GIT_CONFIG": str(masking)}),
            "empty insteadOf prefix": ((f"url.{self.tmp / 'redirected'}.insteadOf", ""), {}),
        }
        for label, ((key, value), env) in cases.items():
            with self.subTest(label):
                self.reset_global_config()
                out("config", "--global", key, value)
                with mock.patch.dict(os.environ, env):
                    code, report = inspect(target)
                self.assertEqual((code, report["kind"]), (3, "REFUSED"), report)
        self.reset_global_config()
        code, report = inspect(target)
        self.assertEqual((code, report["kind"], report["refs"]), (0, "UNSUPPORTED", {MAIN: b}))
        self.assertEqual((refs(target), refs(sink)), ({MAIN: b}, {}))

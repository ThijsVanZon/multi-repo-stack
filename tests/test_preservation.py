from mrs import transactions
from mrs.transactions import Refused
from tests.support import DEV, NOW, GitTestCase, git, out, refs, start_kernel, tree_digest


class CallerPreservationTests(GitTestCase):
    def dirty(self, checkout) -> None:
        """Staged, unstaged, untracked and ignored work, another branch and a local config entry."""
        out("-C", checkout, "checkout", "--quiet", "-b", "26.1.0/local-task")
        (checkout / ".git" / "info").mkdir(exist_ok=True)
        (checkout / ".git" / "info" / "exclude").write_text("*.log\n", encoding="utf-8")
        (checkout / "README.md").write_text("# staged change\n", encoding="utf-8")
        out("-C", checkout, "add", "README.md")
        (checkout / "src" / "app.py").write_text("print('unstaged change')\n", encoding="utf-8")
        (checkout / "notes ü untracked.txt").write_text("untracked\n", encoding="utf-8")
        (checkout / "debug.log").write_text("ignored\n", encoding="utf-8")
        out("-C", checkout, "config", "caller.marker", "kept")

    def assert_still_dirty(self, checkout) -> None:
        status = out("-C", checkout, "--no-optional-locks", "status", "--porcelain=v1", "--branch",
                     "--untracked-files=all", "--ignored").splitlines()
        self.assertEqual(status, ["## 26.1.0/local-task", "M  README.md", " M src/app.py",
                                  "?? \"notes \\303\\274 untracked.txt\"", "!! debug.log"])

    def test_bootstrap_from_dirty_caller_publishes_committed_B_and_preserves_caller(self):
        """Caller/source preservation: only committed B is published; the dirty caller is byte-identical
        after success and after a refusal."""
        source, b = self.lifecycle_source(name="caller checkout")
        self.dirty(source)
        before = tree_digest(source)
        target = self.bare("target.git")
        work = self.tmp / "bootstrap op"
        transactions.prepare_bootstrap(source=source, commit=b, remote=target, work=work)
        self.assertEqual(transactions.apply(work).status, "APPLIED")

        self.assertEqual(refs(target), {DEV: b})
        self.assertEqual(out("-C", target, "ls-tree", "-r", "--name-only", b).splitlines(),
                         ["README.md", "VERSION", "multi-repo-stack.json", "src/app.py"])
        self.assertEqual(git("-C", target, "cat-file", "blob", f"{b}:src/app.py").stdout, b"print('example')\n")
        self.assertEqual(tree_digest(source), before)

        with self.assertRaises(Refused):
            transactions.prepare_bootstrap(source=source, commit=b, remote=self.nonempty(), work=self.tmp / "op 2")
        self.assertEqual(tree_digest(source), before)
        self.assert_still_dirty(source)

    def nonempty(self):
        target = self.bare("nonempty.git")
        other = self.checkout("other")
        self.commit(other, {"x": "x\n"}, "x")
        out("-C", other, "push", "--quiet", target, "HEAD:refs/heads/main")
        return target

    def test_release_invoked_from_dirty_caller_preserves_it(self):
        """Caller/source preservation: a release run from a dirty caller checkout, successful and refused,
        leaves it byte-identical."""
        target, _, _ = self.bootstrapped()
        caller = self.tmp / "caller checkout"
        out("clone", "--quiet", "--branch", "dev", target, caller)
        self.dirty(caller)
        before = tree_digest(caller)
        candidate = refs(target)[DEV]
        code = ("from datetime import datetime; from tests.support import RELEASER, simulated_acceptance; "
                "w = Path(sys.argv[2]); "
                "transactions.prepare_release(remote=Path(sys.argv[3]), work=w, candidate=sys.argv[4], "
                "acceptance=simulated_acceptance(), identity=RELEASER, now=datetime.fromisoformat(sys.argv[5])); "
                "print(transactions.apply(w, now=datetime.fromisoformat(sys.argv[5])).status)")
        for attempt, expected in (("first", b"APPLIED"), ("second", b"")):
            proc = start_kernel(code, str(self.tmp / f"{attempt} op"), str(target), candidate,
                                NOW.isoformat(), cwd=caller)
            stdout, stderr = proc.communicate(timeout=300)
            self.assertEqual(stdout.strip(), expected, stderr.decode("utf-8", "replace"))
            if not expected:
                self.assertIn(b"Refused", stderr)
            self.assertEqual(tree_digest(caller), before)
        self.assert_still_dirty(caller)

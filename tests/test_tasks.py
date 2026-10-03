"""Task branches of the active line. Landing here is a fixture stand-in for pstack's PR landing (a squash
onto dev guarded by a lease on the checked dev), not a PR."""

from tests.consumer_support import NATIVE, RESEARCH, ConsumerTestCase
from tests.support import DEV, MAIN, out, refs, selector, shared_snapshot, tree_digest


class TaskTests(ConsumerTestCase):
    def caller(self, target, name: str = "caller checkout"):
        """A contributor's clone on its own branch, with staged, unstaged, untracked and ignored work and a
        local configuration entry."""
        clone = self.tmp / name
        out("clone", "--quiet", "--branch", "dev", target, clone)
        out("-C", clone, "switch", "--quiet", "-c", "local-work")
        (clone / ".git" / "info" / "exclude").write_text("*.log\n", encoding="utf-8")
        (clone / "README.md").write_text("# staged change\n", encoding="utf-8")
        out("-C", clone, "add", "README.md")
        (clone / "src" / "app.py").write_text("print('unstaged change')\n", encoding="utf-8")
        (clone / "notes ü untracked.txt").write_text("untracked\n", encoding="utf-8")
        (clone / "debug.log").write_text("ignored\n", encoding="utf-8")
        out("-C", clone, "config", "caller.marker", "kept")
        return clone

    def caller_state(self, clone) -> dict:
        """The caller's working files, HEAD, index and configuration, and its status (refs are compared apart)."""
        return {"files": {name: digest for name, digest in tree_digest(clone).items()
                          if not name.startswith(".git/") or name in (".git/HEAD", ".git/index", ".git/config")},
                "status": out("-C", clone, "--no-optional-locks", "status", "--porcelain=v1", "--branch",
                              "--untracked-files=all", "--ignored")}

    def task(self, *args):
        return self.tool("task", *map(str, args))

    def on_branch(self, repo, branch: str, start: str, files: dict, message: str = "Task work") -> str:
        out("-C", repo, "switch", "--quiet", "--force", "-c", branch, start)
        return self.commit(repo, files, message)

    def land(self, repo, target, task: str, checked_dev: str, message: str) -> str:
        """Squash `task` onto the dev that was checked, and publish it only while dev is still that commit."""
        out("-C", repo, "fetch", "--quiet", target, "refs/heads/dev")
        out("-C", repo, "switch", "--quiet", "--force", "--detach", checked_dev)
        out("-C", repo, "merge", "--quiet", "--squash", task)
        out("-C", repo, "commit", "--quiet", "-m", message)
        out("-C", repo, "push", "--quiet", "--no-follow-tags", f"--force-with-lease=refs/heads/dev:{checked_dev}",
            target, "HEAD:refs/heads/dev")
        return out("-C", repo, "rev-parse", "HEAD")

    def test_a_task_starts_at_the_current_dev_and_lands_after_a_fresh_check(self):
        """Tasks: correct prefix and root base from the target's current dev (not the caller's stale copy);
        caller branch, index, staged, unstaged, untracked and ignored files and configuration survive; existing
        branches are never moved; actual integration after a fresh check."""
        target, _, b = self.bootstrapped()
        clone = self.caller(target)
        current = self.integrate(target, {"src/other.py": "OTHER = 1\n"}, "Another task landed")
        before, before_refs = self.caller_state(clone), refs(clone)

        code, report = self.task("create", "--repository", target, "--checkout", clone, "--task", "add-greeting")

        self.assertEqual((code, report["status"], report["branch"], report["commit"]),
                         (0, "CREATED", "26.1.0/add-greeting", current))
        self.assertNotEqual(current, b)
        self.assertEqual(self.caller_state(clone), before)
        self.assertEqual(refs(clone), {**before_refs, "refs/heads/26.1.0/add-greeting": current})
        unchanged = tree_digest(clone)
        code, report = self.task("create", "--repository", target, "--checkout", clone, "--task", "add-greeting")
        self.assertEqual((code, report["status"]), (3, "REFUSED"))
        self.assertIn("already exists in", report["reason"])
        self.assertEqual(tree_digest(clone), unchanged)

        work = self.tmp / "task work"
        out("-C", clone, "worktree", "add", "--quiet", work, "26.1.0/add-greeting")
        head = self.commit(work, {"src/greeting.py": "GREETING = 'hello'\n"}, "Add a greeting")
        code, report = self.task("check", "--repository", target, "--checkout", clone,
                                 "--branch", "26.1.0/add-greeting")
        self.assertEqual((code, report["status"], report["problems"]), (0, "VALID", []))
        self.assertEqual((report["commit"], report["base"], report["base_commit"], report["dev"]),
                         (head, "dev", current, current))
        out("-C", work, "push", "--quiet", "--no-follow-tags", target, "26.1.0/add-greeting")
        other = self.caller(target, "other contributor")
        code, report = self.task("create", "--repository", target, "--checkout", other, "--task", "add-greeting")
        self.assertEqual((code, report["status"]), (3, "REFUSED"))
        self.assertIn("already exists on the target", report["reason"])

        code, report = self.task("check", "--repository", target, "--checkout", clone,
                                 "--branch", "26.1.0/add-greeting")
        self.assertEqual((code, report["status"]), (0, "VALID"), "a fresh check immediately before landing")
        landed = self.land(work, target, "26.1.0/add-greeting", report["dev"], "Add a greeting (26.1.0/add-greeting)")

        self.assertEqual(refs(target)[DEV], landed)
        self.assertEqual(out("-C", target, "show", f"{landed}:src/greeting.py"), "GREETING = 'hello'")
        self.assertEqual(out("-C", target, "show", f"{landed}:VERSION"), "26.1.0")
        code, state = self.tool("inspect", "--remote", target)
        self.assertEqual((code, state["kind"], state["active"]), (0, "LIFECYCLE", "26.1.0"))

    def test_names_bases_version_changes_and_unsupported_targets_are_refused(self):
        """Tasks: semantic-name and active-prefix validation, root base dev or a same-line stacked parent
        containing its current tip, ordinary VERSION change refused; context-only, empty, unsupported and
        unreadable targets refuse; a refusal leaves the caller byte-identical."""
        target, _, _ = self.bootstrapped()
        dev = refs(target)[DEV]
        author = self.tmp / "author"
        out("clone", "--quiet", "--branch", "dev", target, author)
        fine =self.on_branch(author, "26.1.0/fine-work", dev, {"src/fine.py": "FINE = 1\n"})
        parent = self.on_branch(author, "26.1.0/parent-work", dev, {"src/parent.py": "PARENT = 1\n"})
        out("-C", author, "push", "--quiet", "--no-follow-tags", target, "26.1.0/parent-work")
        self.on_branch(author, "26.1.0/child-work", parent, {"src/child.py": "CHILD = 1\n"})
        for name in ("26.2.0/fine-work", "feature/fine-work", "26.1.0/Fine_Work", "26.1.0/fine--work",
                     "26.1.0/9-lives", "26.1.0/fine/work"):
            out("-C", author, "branch", name, fine)
        self.on_branch(author, "26.1.0/bump-version", dev, {"VERSION": "26.2.0\n"})
        self.on_branch(author, "26.1.0/leave-lifecycle", dev,
                       {"multi-repo-stack.json": selector("example/app", "context", shared_snapshot()[1])})
        out("-C", author, "switch", "--quiet", "--orphan", "26.1.0/unrelated-work")
        self.commit(author, {"VERSION": "26.1.0\n", "README.md": "# Unrelated\n",
                             "multi-repo-stack.json": selector("example/app", "lifecycle", shared_snapshot()[1])},
                    "Unrelated history")

        def check(branch, base="dev"):
            return self.task("check", "--repository", target, "--checkout", author, "--branch", branch, "--base", base)

        for branch, base in (("26.1.0/fine-work", "dev"), ("26.1.0/child-work", "26.1.0/parent-work")):
            with self.subTest(f"control {branch} onto {base}"):
                self.assertEqual(check(branch, base)[1]["status"], "VALID")
        invalid = {
            ("26.2.0/fine-work", "dev"): "not a task branch of the active line",
            ("feature/fine-work", "dev"): "not a task branch of the active line",
            ("26.1.0/Fine_Work", "dev"): "lowercase kebab-case",
            ("26.1.0/fine--work", "dev"): "lowercase kebab-case",
            ("26.1.0/9-lives", "dev"): "starting with a letter",
            ("26.1.0/fine/work", "dev"): "lowercase kebab-case",
            ("26.1.0/bump-version", "dev"): "the task changes VERSION to 26.2.0",
            ("26.1.0/leave-lifecycle", "dev"): "no longer selects the lifecycle",
            ("26.1.0/unrelated-work", "dev"): "shares no history with dev",
            ("26.1.0/fine-work", "main"): "not 'main'",
            ("26.1.0/child-work", "26.2.0/parent-work"): "not '26.2.0/parent-work'",
            ("26.1.0/child-work", "26.1.0/missing-parent"): "does not exist on the target",
        }
        for (branch, base), message in invalid.items():
            with self.subTest(f"{branch} onto {base}"):
                code, report = check(branch, base)
                self.assertEqual((code, report["status"]), (1, "INVALID"), report)
                self.assertTrue(any(message in problem for problem in report["problems"]), report["problems"])
        out("-C", author, "switch", "--quiet", "--force", "26.1.0/parent-work")
        moved = self.commit(author, {"src/parent.py": "PARENT = 2\n"}, "Parent moves on")
        out("-C", author, "push", "--quiet", "--no-follow-tags", target, "26.1.0/parent-work")
        code, report = check("26.1.0/child-work", "26.1.0/parent-work")
        self.assertEqual((code, report["status"]), (1, "INVALID"))
        self.assertIn(f"does not contain {moved}, the current tip of its parent", report["problems"][0])

        clone = self.caller(target)
        unchanged = tree_digest(clone)
        for task in ("26.1.0/named-with-prefix", "Fix", "fix_it", "fix-", "9-lives"):
            with self.subTest(f"create {task!r}"):
                code, report = self.task("create", "--repository", target, "--checkout", clone, "--task", task)
                self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
        context = self.bare("context target.git")
        connected = self.checkout("context source")
        context_dev = self.commit(connected, {"multi-repo-stack.json": selector("example/existing", "context",
                                                                                 shared_snapshot()[1]),
                                              "README.md": "# Existing\n"}, "Connect as context")
        out("-C", connected, "push", "--quiet", "--no-follow-tags", context, f"{context_dev}:refs/heads/dev")
        mvp = self.bare("mvp target.git")
        out("-C", author, "push", "--quiet", "--no-follow-tags", mvp, f"{dev}:{DEV}", f"{dev}:{MAIN}")
        targets = {"context-only": (context, 3, "selects 'context'"), "empty": (self.bare("empty.git"), 3, "empty"),
                   "unsupported": (mvp, 3, "without any contract release"),
                   "unreadable": (self.tmp / "no such target", 4, "")}
        for label, (where, expected, message) in targets.items():
            for command in (["create", "--task", "fine-work"],
                            ["check", "--branch", "26.1.0/fine-work", "--commit", "HEAD"]):
                with self.subTest(f"{command[0]} against a {label} target"):
                    code, report = self.task(command[0], "--repository", where, "--checkout", clone, *command[1:])
                    self.assertEqual(code, expected, report)
                    self.assertIn(message, report["reason"])
        self.assertEqual(tree_digest(clone), unchanged)

    def test_a_line_advance_rejects_old_line_tasks_until_ported_explicitly(self):
        """Tasks: genuine line advancement, then old-line rejection (also of a silent rename) and an explicit
        native-Git port onto a new prefixed branch that keeps the old branch and names the ported work, with
        re-verification of the integrated result."""
        target, _, _ = self.consumer("research", RESEARCH)
        author = self.tmp / "author"
        out("clone", "--quiet", "--branch", "dev", target, author)
        code, report = self.task("create", "--repository", target, "--checkout", author, "--task", "document-model")
        self.assertEqual(code, 0, report)
        old_base = report["commit"]
        out("-C", author, "switch", "--quiet", "26.1.0/document-model")
        old = self.commit(author, {"analysis/NOTES.md": "The model is a least-squares slope.\n"}, "Document the model")
        self.assertEqual(self.task("check", "--repository", target, "--checkout", author,
                                   "--branch", "26.1.0/document-model")[1]["status"], "VALID")

        _, operation = self.release(target)
        opening = operation["next"]["commit"]
        out("-C", author, "branch", "26.2.0/document-model-renamed", old)
        for branch, messages in (("26.1.0/document-model", ["not a task branch", "changes VERSION to 26.1.0",
                                                            f"does not descend from {opening}"]),
                                 ("26.2.0/document-model-renamed", ["changes VERSION to 26.1.0",
                                                                    f"does not descend from {opening}"])):
            with self.subTest(branch):
                code, report = self.task("check", "--repository", target, "--checkout", author, "--branch", branch)
                self.assertEqual((code, report["status"], report["version"]), (1, "INVALID", "26.2.0"))
                for message in messages:
                    self.assertTrue(any(message in problem for problem in report["problems"]), report["problems"])

        code, report = self.task("create", "--repository", target, "--checkout", author, "--task", "document-model")
        self.assertEqual((code, report["branch"], report["commit"]), (0, "26.2.0/document-model", opening))
        out("-C", author, "switch", "--quiet", "26.2.0/document-model")
        out("-C", author, "cherry-pick", "-x", f"{old_base}..26.1.0/document-model")
        ported = out("-C", author, "rev-parse", "HEAD")
        self.assertIn(f"(cherry picked from commit {old})", out("-C", author, "log", "-1", "--format=%B"))
        self.assertEqual(out("-C", author, "diff", "--name-only", opening, ported), "analysis/NOTES.md")
        self.assertEqual(out("-C", author, "rev-parse", "26.1.0/document-model"), old, "the old branch is kept")
        code, report = self.task("check", "--repository", target, "--checkout", author,
                                 "--branch", "26.2.0/document-model")
        self.assertEqual((code, report["status"]), (0, "VALID"), report)
        landed = self.land(author, target, "26.2.0/document-model", report["dev"], "Document the model (ported)")

        code, report, record = self.collect(target, "after the port")
        self.assertEqual((code, report["status"]), (0, "PASSED"), report)
        self.assertEqual((report["record"]["candidate"], report["record"]["version"]), (landed, "26.2.0"))
        self.assertEqual([(r["check"], r["environment"], r["outcome"]) for r in report["record"]["results"]],
                         [("synthetic-slope", NATIVE, "pass")])
        code, state = self.tool("inspect", "--remote", target)
        self.assertEqual((state["kind"], state["active"], [r["version"] for r in state["releases"]]),
                         ("LIFECYCLE", "26.2.0", ["26.1.0"]))

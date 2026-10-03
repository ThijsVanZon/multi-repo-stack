"""Connecting an existing repository and upgrading a consumer's selection, by the procedure in docs/connect.md."""

import json
import os
from pathlib import Path

from mrs import transactions
from tests.consumer_support import RESEARCH, ConsumerTestCase
from tests.support import DEV, MAIN, ROOT, out, refs, selector, shared_snapshot, snapshot_source


def route_section() -> str:
    """The AGENTS.md section that docs/connect.md tells a consumer to add, exactly as documented."""
    lines = (ROOT / "docs" / "connect.md").read_text(encoding="utf-8").splitlines()
    start = lines.index("   ```markdown") + 1
    return "\n".join(line[3:] for line in lines[start:lines.index("   ```", start)]) + "\n"


class ConnectionTests(ConsumerTestCase):
    def test_an_existing_repository_connects_for_context_without_adopting_the_lifecycle(self):
        """Connection: an existing repository with its own instructions, ignore rules, source, branch, tag and
        version identity gains only a selector and an appended route; context entry loads no lifecycle rules,
        and lifecycle operations and release recognition refuse it."""
        repo = self.checkout("existing product")
        out("-C", repo, "symbolic-ref", "HEAD", "refs/heads/main")
        existing = {"README.md": "# Existing product\n", ".gitignore": "build/\n*.log\n",
                    "AGENTS.md": "# Agents\n\nRun `python -m unittest` before every commit.\n",
                    "CLAUDE.md": "Trunk is main. Use feature/<name> branches.\n", "VERSION": "1.4.2\n",
                    "pyproject.toml": '[project]\nname = "existing-product"\nversion = "1.4.2"\n',
                    "src/existing/__init__.py": "def total(items):\n    return sum(items)\n"}
        first = self.commit(repo, existing, "Existing product")
        before = self.commit(repo, {"src/existing/__init__.py": existing["src/existing/__init__.py"]
                                    + "\n\ndef mean(items):\n    return total(items) / len(items)\n"}, "Add mean")
        out("-C", repo, "tag", "-a", "v1.4.2", "-m", "Existing release", before)
        tag = out("-C", repo, "rev-parse", "refs/tags/v1.4.2")
        agents = (repo / "AGENTS.md").read_bytes()

        after = self.commit(repo, {"multi-repo-stack.json": selector("example/existing-product", "context",
                                                                     shared_snapshot()[1]),
                                   "AGENTS.md": agents.decode("utf-8") + "\n" + route_section()},
                            "Connect to multi-repo-stack for context")

        self.assertEqual(out("-C", repo, "diff", "--name-status", before, after).splitlines(),
                         ["M\tAGENTS.md", "A\tmulti-repo-stack.json"])
        self.assertTrue((repo / "AGENTS.md").read_bytes().startswith(agents))
        for name, content in existing.items():
            if name not in ("AGENTS.md", "src/existing/__init__.py"):
                self.assertEqual((repo / name).read_bytes(), content.encode("utf-8"), name)
        self.assertEqual(out("-C", repo, "rev-list", "--reverse", after), "\n".join([first, before, after]))
        self.assertEqual((out("-C", repo, "symbolic-ref", "HEAD"), out("-C", repo, "rev-parse", "refs/tags/v1.4.2")),
                         ("refs/heads/main", tag))
        code, report = self.tool("preflight", "--consumer", repo, "--pstack", self.pstack())
        self.assertEqual((code, report["consumer"]["applicability"]), (0, "context"), report)
        self.assertEqual([os.path.basename(path) for path in report["load"]], ["context.md"])

        mirror = self.bare("existing mirror.git")
        out("-C", repo, "push", "--quiet", "--no-follow-tags", mirror, f"{after}:{MAIN}", f"{after}:{DEV}",
            "refs/tags/v1.4.2")
        code, state = self.tool("inspect", "--remote", mirror)
        self.assertEqual((code, state["kind"], state["releases"], state["ignored_tags"]),
                         (0, "UNSUPPORTED", [], ["v1.4.2"]))
        clone = self.tmp / "existing clone"
        out("clone", "--quiet", "--branch", "main", mirror, clone)
        for command in (["task", "create", "--repository", mirror, "--checkout", clone, "--task", "add-median"],
                        ["collect", "--repository", mirror, "--pstack", self.pstack(), "--out", self.tmp / "r.json"]):
            with self.subTest(command[0]):
                code, report = self.tool(*command)
                self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
                self.assertIn("'context'", report["reason"])
        with self.assertRaises(transactions.Refused) as caught:
            transactions.prepare_bootstrap(source=repo, commit=after, remote=self.bare("empty.git"),
                                           work=self.tmp / "bootstrap op")
        self.assertEqual(caught.exception.code, "applicability")
        self.assertEqual(refs(mirror), {MAIN: after, DEV: after, "refs/tags/v1.4.2": tag})

    def test_an_explicit_upgrade_moves_context_and_tools_together_and_leaves_the_product_alone(self):
        """Upgrade: moving a shared branch changes nothing; one explicit selector change moves context, tool and
        pstack together; old checkouts and old entry records refuse; earlier check records do not carry over;
        the consumer's version and history are kept."""
        old_checkout, old = shared_snapshot()
        target, _, before = self.consumer("research", RESEARCH)
        entry = self.tmp / "entry before the upgrade.json"
        author = self.tmp / "author"
        out("clone", "--quiet", "--branch", "dev", target, author)
        code, report = self.tool("preflight", "--consumer", author, "--pstack", self.pstack())
        self.assertEqual(code, 0, report)
        entry.write_text(json.dumps(report), encoding="utf-8")
        code, report, old_record = self.collect(target, "before the upgrade")
        self.assertEqual(code, 0, report)

        upstream = self.tmp / "shared upstream.git"
        out("clone", "--quiet", "--bare", snapshot_source(), upstream)
        newer_pstack = self.pstack_clone("newer pstack checkout")
        pin = self.commit(newer_pstack, {"pstack/FIXTURE-UPGRADE.md": "Fixture-only newer pstack content.\n"},
                          "Fixture-only newer pstack revision")
        work = self.tmp / "shared work"
        out("clone", "--quiet", "--config", "core.autocrlf=false", upstream, work)
        out("-C", work, "switch", "--quiet", "--detach", old)
        shared_selector = json.loads((work / "multi-repo-stack.json").read_text(encoding="utf-8"))
        shared_selector["pstack"]["commit"] = pin
        context = (work / "docs" / "context.md").read_text(encoding="utf-8")
        marker = "- Fixture upgrade: this line exists only in the newer shared revision.\n"
        new = self.commit(work, {"multi-repo-stack.json": json.dumps(shared_selector, indent=2) + "\n",
                                 "docs/context.md": context + marker},
                          "Newer shared revision")
        out("-C", work, "push", "--quiet", "--no-follow-tags", upstream, f"{new}:refs/heads/main")
        new_checkout = self.tmp / "shared checkout new"
        out("clone", "--quiet", "--no-checkout", "--config", "core.autocrlf=false", upstream, new_checkout)
        out("-C", new_checkout, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", new)

        def preflight(checkout, pstack, *extra):
            return self.tool("preflight", "--consumer", author, "--pstack", pstack, *extra, checkout=checkout)

        code, report = preflight(old_checkout, self.pstack(), "--expect", entry)
        self.assertEqual((code, report["shared"]["commit"]), (0, old), "the moved shared branch changed nothing")
        code, report = preflight(new_checkout, newer_pstack)
        self.assertEqual((code, report["verdict"]), (3, "REFUSED"))
        self.assertIn(f"is at {new}, but {old} is selected", report["reason"])

        code, report = self.tool("task", "create", "--repository", target, "--checkout", author,
                                 "--task", "upgrade-shared-source")
        self.assertEqual(code, 0, report)
        out("-C", author, "switch", "--quiet", "26.1.0/upgrade-shared-source")
        consumer_selector = json.loads((author / "multi-repo-stack.json").read_text(encoding="utf-8"))
        consumer_selector["shared"]["commit"] = new
        self.commit(author, {"multi-repo-stack.json": json.dumps(consumer_selector, indent=2) + "\n"},
                    "Select the newer shared revision")
        code, report = self.tool("task", "check", "--repository", target, "--checkout", author,
                                 "--branch", "26.1.0/upgrade-shared-source")
        self.assertEqual((code, report["status"]), (0, "VALID"), report)
        out("-C", author, "switch", "--quiet", "--detach", before)
        out("-C", author, "merge", "--quiet", "--squash", "26.1.0/upgrade-shared-source")
        out("-C", author, "commit", "--quiet", "-m", "Select the newer shared revision (26.1.0/upgrade-shared-source)")
        out("-C", author, "push", "--quiet", "--no-follow-tags", f"--force-with-lease=refs/heads/dev:{before}",
            target, "HEAD:refs/heads/dev")
        upgraded = refs(target)[DEV]

        self.assertEqual(out("-C", author, "diff", "--name-only", before, upgraded), "multi-repo-stack.json")
        self.assertEqual(out("-C", author, "show", f"{upgraded}:VERSION"), "26.1.0")
        self.assertEqual(out("-C", author, "merge-base", before, upgraded), before)
        code, report = preflight(new_checkout, newer_pstack)
        self.assertEqual(code, 0, report)
        self.assertEqual((report["shared"]["commit"], report["pstack"]["commit"]), (new, pin))
        self.assertIn(marker, Path(report["load"][0]).read_text(encoding="utf-8"))
        refusals = {"old tool checkout": (preflight(old_checkout, self.pstack()), 3, f"is at {old}, but {new}"),
                    "old pstack checkout": (preflight(new_checkout, self.pstack()), 3, f"but {pin} is selected"),
                    "old entry record": (preflight(new_checkout, newer_pstack, "--expect", entry), 5, "shared")}
        for label, ((code, report), expected, message) in refusals.items():
            with self.subTest(label):
                self.assertEqual(code, expected, report)
                self.assertIn(message, report["reason"])

        code, report = self.tool("assess", "--repository", target, "--pstack", newer_pstack, "--record", old_record,
                                 checkout=new_checkout)
        self.assertEqual((code, report["status"]), (3, "REFUSED"))
        self.assertTrue(any(f"but the assessed candidate has '{upgraded}'" in problem
                            for problem in report["problems"]), report["problems"])
        code, report, _ = self.collect(target, "after the upgrade, old tool")
        self.assertEqual((code, report["status"]), (3, "REFUSED"))
        code, report, _ = self.collect(target, "after the upgrade", pstack=newer_pstack, checkout=new_checkout)
        self.assertEqual((code, report["status"]), (0, "PASSED"), report)
        record = report["record"]
        self.assertEqual((record["candidate"], record["version"], record["shared"]["commit"],
                          record["pstack"]["commit"]), (upgraded, "26.1.0", new, pin))

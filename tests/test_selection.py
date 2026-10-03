import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mrs import transactions
from mrs.transactions import Refused
from tests.support import (DEV, MAIN, NOW, SHARED_REPOSITORY, GitTestCase, out, refs, selector,
                           shared_snapshot, snapshot_source)

PIN = "7022c81efb48d8b5eb15498ce6043a3bd74b694c"
PSTACK_TREE = "975600f2f90dc6f755d58cccdccee27f950edcd2"


def run_tool(checkout, *args) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "-I", "-B", str(checkout / "mrs"), *map(str, args)], capture_output=True)
    return proc.returncode, proc.stdout.decode("utf-8") + proc.stderr.decode("utf-8")


class SelectionTests(GitTestCase):
    def consumer(self, name: str, applicability: str, commit: str | None = None) -> Path:
        repo = self.checkout(name)
        files = {"multi-repo-stack.json": selector("example/app", applicability, commit or shared_snapshot()[1]),
                 "README.md": "# Example app\n"}
        if applicability == "lifecycle":
            files["VERSION"] = "26.1.0\n"
        self.commit(repo, files, "Connect to multi-repo-stack")
        return repo

    def preflight(self, consumer, pstack, checkout=None, *extra) -> tuple[int, dict]:
        code, text = run_tool(checkout or shared_snapshot()[0], "preflight", "--consumer", consumer,
                              "--pstack", pstack, "--json", *extra)
        return code, json.loads(text)

    def pstack_clone(self, name: str) -> Path:
        clone = self.tmp / name
        out("clone", "--quiet", "--shared", "--no-checkout", "--config", "core.autocrlf=false", self.pstack(), clone)
        out("-C", clone, "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", PIN)
        return clone

    def test_lifecycle_consumer_resolves_exact_shared_and_pstack_bytes(self):
        """Entry and scope: a fresh lifecycle consumer resolves the runtime-pinned shared commit and the
        exact pstack bytes; neither checkout moves."""
        checkout, shared_commit = shared_snapshot()
        consumer = self.consumer("lifecycle consumer", "lifecycle")
        pstack_head = out("-C", self.pstack(), "rev-parse", "HEAD")

        code, report = self.preflight(consumer, self.pstack())

        self.assertEqual(code, 0, report)
        self.assertEqual(report["consumer"], {"repository": "example/app", "applicability": "lifecycle",
                                              "commit": out("-C", consumer, "rev-parse", "HEAD")})
        self.assertEqual(report["shared"], {"repository": SHARED_REPOSITORY, "commit": shared_commit,
                                            "self": False, "clean": True})
        self.assertEqual(report["pstack"], {"repository": "cursor/plugins", "commit": PIN, "path": "pstack",
                                            "tree": PSTACK_TREE})
        self.assertEqual([Path(path).resolve() for path in report["load"]],
                         [(checkout / "docs" / name).resolve() for name in ("context.md", "lifecycle.md")])
        self.assertEqual(out("-C", checkout, "rev-parse", "HEAD"), shared_commit)
        self.assertEqual(out("-C", self.pstack(), "rev-parse", "HEAD"), pstack_head)

    def test_shared_project_own_entry(self):
        """Entry and scope: the shared repository's own selector routes to both instruction files."""
        checkout, shared_commit = shared_snapshot()
        code, text = run_tool(checkout, "preflight", "--consumer", checkout, "--pstack", self.pstack())
        self.assertEqual(code, 0, text)
        self.assertIn("PREFLIGHT OK", text)
        self.assertIn(f"shared    {SHARED_REPOSITORY}  commit={shared_commit}  (this checkout)", text)
        self.assertIn(f"pstack    cursor/plugins  commit={PIN}  pstack tree={PSTACK_TREE}", text)

    def test_context_only_existing_consumer_keeps_its_rules_and_refuses_lifecycle_mutation(self):
        """Entry and scope: an existing-content repository connected as context keeps its files and rules,
        loads no lifecycle rules, and every lifecycle mutation refuses."""
        repo = self.checkout("existing product")
        original = {"README.md": "# Existing product\n", "CLAUDE.md": "Trunk is main. Use feature/<name> branches.\n",
                    "src/main.c": "int main(void) { return 0; }\n"}
        before = self.commit(repo, original, "Existing MVP")
        out("-C", repo, "branch", "--quiet", "dev")
        after = self.commit(repo, {"multi-repo-stack.json": selector("example/existing-product", "context",
                                                                      shared_snapshot()[1]),
                                   "AGENTS.md": "Run multi-repo-stack preflight; load only what it lists.\n"},
                            "Connect to shared context")
        self.assertEqual(out("-C", repo, "diff", "--name-status", before, after).splitlines(),
                         ["A\tAGENTS.md", "A\tmulti-repo-stack.json"])
        for name, content in original.items():
            self.assertEqual((repo / name).read_bytes(), content.encode("utf-8"))

        code, report = self.preflight(repo, self.pstack())
        self.assertEqual(code, 0, report)
        self.assertEqual(report["consumer"]["applicability"], "context")
        self.assertEqual([os.path.basename(path) for path in report["load"]], ["context.md"])

        empty = self.bare("empty target.git")
        with self.assertRaises(Refused) as caught:
            transactions.prepare_bootstrap(source=repo, commit=after, remote=empty, work=self.tmp / "op 1")
        self.assertEqual(caught.exception.code, "applicability")
        mirror = self.bare("context mirror.git")
        out("-C", repo, "push", "--quiet", "--no-follow-tags", mirror, f"{before}:{MAIN}", f"{after}:{DEV}")
        with self.assertRaises(Refused) as caught:
            transactions.prepare_release(remote=mirror, work=self.tmp / "op 2", candidate=after,
                                         acceptance={"criteria": {}, "results": [], "verdict": {}},
                                         identity=transactions.Identity("x", "x@example.invalid"), now=NOW)
        self.assertEqual(caught.exception.code, "applicability")
        self.assertEqual((refs(empty), refs(mirror)), ({}, {MAIN: before, DEV: after}))

    def test_wrong_dirty_missing_or_floating_pstack_refuses(self):
        """Caller/source preservation: missing, copied, moved or dirty pstack blocks dependent work."""
        consumer = self.consumer("consumer", "lifecycle")
        installed = self.tmp / "installed pstack copy"
        shutil.copytree(self.pstack() / "pstack", installed / "pstack")
        moved = self.pstack_clone("moved pstack")
        self.commit(moved, {"pstack/README.md": "local edit\n"}, "Newer local revision")
        dirty = self.pstack_clone("dirty pstack")
        (dirty / "pstack" / "README.md").write_bytes(b"dirty\n")
        shadow = self.pstack_clone("shadowing pstack")
        (shadow / "pstack" / "skills" / "unvetted").mkdir()
        (shadow / "pstack" / "skills" / "unvetted" / "SKILL.md").write_text("shadow\n", encoding="utf-8")
        hidden = self.pstack_clone("hidden edit pstack")
        out("-C", hidden, "update-index", "--assume-unchanged", "pstack/README.md")
        (hidden / "pstack" / "README.md").write_bytes(b"hidden edit\n")
        cases = {
            "missing": (self.tmp / "no such checkout", "is missing"),
            "installed copy": (installed, "not the root of a Git checkout"),
            "different revision": (moved, "but 7022c81efb48d8b5eb15498ce6043a3bd74b694c is selected"),
            "modified file": (dirty, "modified, untracked or ignored"),
            "untracked shadowing file": (shadow, "modified, untracked or ignored"),
            "edit hidden by assume-unchanged": (hidden, "content differs"),
        }
        for label, (pstack, message) in cases.items():
            with self.subTest(label):
                code, report = self.preflight(consumer, pstack)
                self.assertEqual((code, report["verdict"]), (3, "REFUSED"))
                self.assertIn(message, report["reason"])
        unrelated = self.pstack_clone("unrelated untracked pstack")
        (unrelated / "outside-pstack.txt").write_text("not in the pinned subtree\n", encoding="utf-8")
        self.assertEqual(self.preflight(consumer, unrelated)[0], 0)
        self.assertEqual(out("-C", moved, "rev-parse", "HEAD~1"), PIN, "refusal never switched the checkout")

    def test_wrong_or_dirty_shared_checkout_and_ambiguous_selectors_refuse(self):
        """Caller/source preservation: the running tool must be the clean selected shared commit, and the
        consumer selector must be unambiguous."""
        dirty_shared = self.tmp / "dirty shared checkout"
        out("clone", "--quiet", "--config", "core.autocrlf=false", snapshot_source(), dirty_shared)
        (dirty_shared / "mrs" / "extra.py").write_text("SHADOW = True\n", encoding="utf-8")
        code, report = self.preflight(self.consumer("consumer a", "lifecycle"), self.pstack(), dirty_shared)
        self.assertEqual(code, 3)
        self.assertIn("shared checkout (running tool)", report["reason"])

        wrong = self.consumer("consumer b", "lifecycle", commit="1" * 40)
        code, report = self.preflight(wrong, self.pstack())
        self.assertEqual(code, 3)
        self.assertIn("but 1111111111111111111111111111111111111111 is selected", report["reason"])

        edited = self.consumer("consumer c", "lifecycle")
        (edited / "multi-repo-stack.json").write_text(selector("example/app", "context", shared_snapshot()[1]),
                                                       encoding="utf-8")
        self.assertIn("ambiguous", self.preflight(edited, self.pstack())[1]["reason"])

        commit = shared_snapshot()[1]
        invalid = {
            "duplicate key": '{"format": 1, "repository": "example/app", "repository": "example/other", '
                             '"applicability": "context", "shared": {"repository": "%s", "commit": "%s"}}'
                             % (SHARED_REPOSITORY, commit),
            "branch name": selector("example/app", "context", "main"),
            "abbreviated commit": selector("example/app", "context", commit[:12]),
            "unknown scope": selector("example/app", "full", commit),
            "unknown key": selector("example/app", "context", commit).replace('"format"', '"extra": 1, "format"'),
        }
        for label, text in invalid.items():
            with self.subTest(label):
                repo = self.checkout(f"invalid {label}")
                self.commit(repo, {"multi-repo-stack.json": text}, "invalid selector")
                code, report = self.preflight(repo, self.pstack())
                self.assertEqual((code, report["verdict"]), (3, "REFUSED"))
        unconnected = self.checkout("unconnected")
        self.commit(unconnected, {"README.md": "x\n"}, "x")
        self.assertIn("not connected", self.preflight(unconnected, self.pstack())[1]["reason"])

    def test_change_during_a_run_invalidates_it(self):
        """Caller/source preservation: identities recorded at entry are re-verified; a change invalidates."""
        consumer = self.consumer("consumer", "lifecycle")
        pstack = self.pstack_clone("pstack in use")
        code, report = self.preflight(consumer, pstack)
        record = self.tmp / "entry selection.json"
        record.write_text(json.dumps(report), encoding="utf-8")
        self.commit(consumer, {"src/work.py": "WORK = 1\n"}, "Ordinary task work")
        self.assertEqual(self.preflight(consumer, pstack, None, "--expect", record)[0], 0)

        (pstack / "pstack" / "README.md").write_bytes(b"changed mid-run\n")
        code, report = self.preflight(consumer, pstack, None, "--expect", record)
        self.assertEqual((code, report["verdict"]), (5, "INVALIDATED"))
        out("-C", pstack, "checkout", "--quiet", "--", "pstack/README.md")
        self.commit(consumer, {"multi-repo-stack.json": selector("example/app", "context", shared_snapshot()[1])},
                    "Switch to context")
        code, report = self.preflight(consumer, pstack, None, "--expect", record)
        self.assertEqual((code, report["verdict"]), (5, "INVALIDATED"))
        self.assertIn("applicability", report["reason"])

    def test_cli_is_read_only_and_reports_unknown_separately(self):
        """Entry and scope / Observation: only read-only commands exist; an unreadable target is UNKNOWN."""
        checkout, _ = shared_snapshot()
        code, text = run_tool(checkout, "bootstrap")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice: 'bootstrap' (choose from 'preflight', 'inspect')", text)
        code, text = run_tool(checkout, "inspect", "--remote", self.tmp / "no such target", "--json")
        self.assertEqual((code, json.loads(text)["kind"]), (4, "UNKNOWN"))
        target, _, _ = self.bootstrapped()
        self.assertEqual(json.loads(run_tool(checkout, "inspect", "--remote", target, "--json")[1])["kind"],
                         "LIFECYCLE")
        self.release(target)
        code, text = run_tool(checkout, "inspect", "--remote", target, "--json")
        report = json.loads(text)
        self.assertEqual((code, report["kind"], report["active"]), (0, "LIFECYCLE", "26.2.0"))
        self.assertEqual([r["version"] for r in report["releases"]], ["26.1.0"])

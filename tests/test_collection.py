import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mrs import config
from tests.consumer_support import (IMPLEMENTATION, NATIVE, PIN, PSTACK_TREE, RESEARCH, ConsumerTestCase,
                                    probe, selector, shape_files)
from tests.support import DEV, SHARED_REPOSITORY, out, refs, shared_snapshot, tree_digest

EXACT = "line one\r\nline two € with no final newline".encode("utf-8")


def interpreter_sha256() -> str:
    with open(sys.executable, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def can_symlink() -> bool:
    with tempfile.TemporaryDirectory(prefix="mrs symlink probe ") as folder:
        try:
            os.symlink(os.path.join(folder, "target"), os.path.join(folder, "link"))
            return True
        except OSError:
            return False


def outcomes(record_path: Path) -> dict:
    record = json.loads(record_path.read_text(encoding="utf-8"))
    return {result["check"]: (result["outcome"], result["reason"]) for result in record["results"]}


class CollectionTests(ConsumerTestCase):
    def test_two_consumer_shapes_collect_exact_C_as_check_evidence_only(self):
        """Two consumer shapes / Candidate identity / Collection versus acceptance: each shape's own command
        runs on exact C; the record binds repository, C, N, criteria, sources, runner and executable, keeps
        the implementation evidence and the research evidence's optional absence, holds no local path, and
        stays check evidence: without a non-author verdict the assessment is INCOMPLETE."""
        built = ("built build/greet.pyz (", " bytes); it printed 'hello, fixture\\n'; as expected\n")
        converting = {".gitattributes": "* text eol=crlf\n"}  # an ordinary checkout here would rewrite C's bytes
        for shape, checks, name, evidence in (("implementation", IMPLEMENTATION, "build-and-run", built),
                                              ("research", RESEARCH, "synthetic-slope", None)):
            with self.subTest(shape):
                target, _, c = self.consumer(shape, checks, extra=converting if shape == "implementation" else None)
                before = refs(target)
                code, report, path = self.collect(target, shape)
                self.assertEqual((code, report["status"]), (0, "PASSED"), report)

                text = path.read_text(encoding="utf-8")
                record = json.loads(text)
                selector_at_c = json.loads(out("-C", target, "show", f"{c}:multi-repo-stack.json"))
                criteria = hashlib.sha256(json.dumps({key: selector_at_c[key] for key in ("checks", "environments")},
                                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                self.assertEqual({key: record[key] for key in ("repository", "candidate", "version", "criteria")},
                                 {"repository": "example/app", "candidate": c, "version": "26.1.0",
                                  "criteria": criteria})
                self.assertEqual(record["shared"], {"repository": SHARED_REPOSITORY, "commit": shared_snapshot()[1],
                                                    "self": False, "clean": True})
                self.assertEqual(record["pstack"], {"repository": "cursor/plugins", "commit": PIN, "path": "pstack",
                                                    "tree": PSTACK_TREE})
                self.assertEqual((record["runner"]["os"], record["unmet"]), (NATIVE, []))
                [result] = record["results"]
                self.assertEqual((result["check"], result["environment"], result["outcome"], result["exit"]),
                                 (name, NATIVE, "pass", 0))
                self.assertEqual(result["argv"], checks[name][0])
                self.assertEqual(result["executable"], {"name": os.path.basename(sys.executable),
                                                        "sha256": interpreter_sha256()})
                if evidence is None:
                    self.assertEqual(result["evidence"], {"present": False}, "optional absence is explicit")
                else:
                    kept = result["evidence"]["text"]
                    self.assertTrue(kept.startswith(evidence[0]) and kept.endswith(evidence[1]), kept)
                    self.assertEqual((result["evidence"]["bytes"], result["evidence"]["sha256"]),
                                     (len(kept.encode()), hashlib.sha256(kept.encode()).hexdigest()))
                log = path.with_name(f"{path.name}.{name}.{NATIVE}.log").read_bytes()
                self.assertEqual(result["output"], {"bytes": len(log), "sha256": hashlib.sha256(log).hexdigest()})
                for private in (str(self.tmp), os.path.expanduser("~"), str(Path(sys.executable).parent)):
                    self.assertNotIn(json.dumps(private)[1:-1], text, "records hold no local paths")
                self.assertEqual(refs(target), before, "collection never changes the repository")

                code, assessment = self.assess(target, [path])
                self.assertEqual((code, assessment["status"], assessment["acceptance"]), (1, "INCOMPLETE", None))
                self.assertEqual(assessment["missing"], ["no non-author verdict on exact C"])

    def test_research_error_fails_and_the_dirty_caller_is_neither_used_nor_changed(self):
        """Two consumer shapes / Source preservation: an estimator error integrated into C fails the research
        check although the caller's dirty checkout holds a fix; the caller's branch, index, staged, unstaged,
        untracked and ignored work and configuration survive a failing and a passing collection."""
        target, _, _ = self.consumer("research", RESEARCH)
        model = shape_files("research")["analysis/model.py"].decode("utf-8")
        broken = self.integrate(target, {"analysis/model.py": model.replace("sum((x - mx) ** 2 for x in xs)",
                                                                            "len(xs)")}, "Introduce an error")
        caller = self.tmp / "caller checkout"
        out("clone", "--quiet", "--branch", "dev", target, caller)
        out("-C", caller, "checkout", "--quiet", "-b", "26.1.0/local-task")
        (caller / ".git" / "info").mkdir(exist_ok=True)
        (caller / ".git" / "info" / "exclude").write_text("*.log\n", encoding="utf-8")
        (caller / "README.md").write_text("# staged change\n", encoding="utf-8")
        out("-C", caller, "add", "README.md")
        (caller / "analysis" / "model.py").write_text(model, encoding="utf-8")  # unstaged fix, never collected
        (caller / "notes ü untracked.txt").write_text("untracked\n", encoding="utf-8")
        (caller / "debug.log").write_text("ignored\n", encoding="utf-8")
        out("-C", caller, "config", "caller.marker", "kept")
        before = tree_digest(caller)

        code, report, path = self.collect(target, "broken", cwd=caller)
        self.assertEqual((code, report["status"]), (1, "NOT PASSED"), report)
        self.assertEqual(report["record"]["candidate"], broken)
        self.assertEqual(outcomes(path), {"synthetic-slope": ("fail", "exit status 1")})
        log = path.with_name(f"{path.name}.synthetic-slope.{NATIVE}.log").read_text(encoding="utf-8")
        self.assertIn("the synthetic series has slope 3.0", log)
        self.assertNotIn("estimated slope 3.0;", log)
        self.assertEqual(tree_digest(caller), before)

        fixed = self.integrate(target, {"analysis/model.py": model}, "Fix the estimator")
        code, report, path = self.collect(target, "fixed", cwd=caller)
        self.assertEqual((code, report["record"]["candidate"]), (0, fixed), report)
        self.assertEqual(tree_digest(caller), before)
        status = out("-C", caller, "--no-optional-locks", "status", "--porcelain=v1", "--branch",
                     "--untracked-files=all", "--ignored").splitlines()
        self.assertEqual(status, ["## 26.1.0/local-task", "M  README.md", " M analysis/model.py",
                                  "?? \"notes \\303\\274 untracked.txt\"", "!! debug.log"])

    def test_evidence_bounds_and_tracked_source_are_enforced_per_check(self):
        """Evidence integrity / Source preservation: exact bytes (CRLF, non-ASCII, no final newline) and the
        64 KiB limit are kept; oversized, non-UTF-8, missing required, directory, linked and escaping evidence
        is refused; optional absence passes; a zero-exit check that changes, deletes or re-modes tracked source
        is invalid, while untracked build outputs are allowed."""
        checks = {"exact-bytes": probe("exact"), "at-limit": probe("limit"), "oversized": probe("oversized"),
                  "not-utf8": probe("not-utf8"), "required-missing": probe("absent"),
                  "optional-absent": probe("absent", required=False), "directory": probe("directory"),
                  "hard-link": probe("hardlink"), "escaping-folder": probe("escape"),
                  "changes-source": probe("mutate"), "deletes-source": probe("delete"),
                  "build-outputs": probe("outputs"), "fails": probe("fail")}
        expected = {
            "exact-bytes": ("pass", None), "at-limit": ("pass", None), "optional-absent": ("pass", None),
            "build-outputs": ("pass", None), "fails": ("fail", "exit status 3"),
            "oversized": ("invalid", "evidence file exceeds 65536 bytes"),
            "not-utf8": ("invalid", "evidence file is not UTF-8"),
            "required-missing": ("invalid", "required evidence file was not written"),
            "directory": ("invalid", "evidence path is not a regular file (a link or another kind of entry)"),
            "hard-link": ("invalid", "evidence file is hard-linked"),
            "escaping-folder": ("invalid", "evidence path resolves outside its runner-owned folder"),
            "changes-source": ("invalid", "the check changed tracked source of C: ['data.txt: content differs']"),
            "deletes-source": ("invalid", "the check changed tracked source of C: ['data.txt: missing']"),
        }
        if can_symlink():
            checks["symbolic-link"] = probe("symlink")
            expected["symbolic-link"] = ("invalid", "evidence path resolves outside its runner-owned folder")
        if os.name != "nt":
            checks["re-modes-source"] = probe("chmod")
            expected["re-modes-source"] = (
                "invalid", "the check changed tracked source of C: ['data.txt: executable bit differs']")
        target, _, c = self.consumer("probe", checks)

        code, report, path = self.collect(target, "probes")

        self.assertEqual((code, report["status"]), (1, "NOT PASSED"))
        self.assertEqual(outcomes(path), expected)
        results = {result["check"]: result for result in report["record"]["results"]}
        self.assertEqual(results["exact-bytes"]["evidence"],
                         {"present": True, "bytes": len(EXACT), "sha256": hashlib.sha256(EXACT).hexdigest(),
                          "text": EXACT.decode("utf-8")})
        self.assertEqual(results["exact-bytes"]["evidence"]["text"].encode("utf-8"), EXACT)
        self.assertEqual(results["at-limit"]["evidence"]["text"], "x" * 65536)
        self.assertEqual(results["optional-absent"]["evidence"], {"present": False})
        self.assertEqual(results["required-missing"]["evidence"], {"present": False})
        self.assertIsNone(results["oversized"]["evidence"], "rejected evidence is not retained")
        self.assertEqual(out("-C", target, "rev-parse", DEV), c)

        code, assessment = self.assess(target, [path], self.verdict(path))
        self.assertEqual((code, assessment["status"]), (3, "REFUSED"))
        self.assertIn(f"record 1: oversized/{NATIVE} did not pass: 'invalid' (evidence file exceeds 65536 bytes)",
                      assessment["problems"])

    def test_literal_argv_native_executable_missing_prerequisite_and_batch_launchers(self):
        """Invocation: literal argv with spaces, Unicode, an empty argument and shell metacharacters reaches
        the check unchanged without a shell; the resolved native interpreter is identified by name and hash;
        a missing prerequisite and (on Windows) batch launchers are recorded as not run."""
        literal = ["two words", "ü € 日本", "&|<>^%PATH%$HOME`;'", '"quoted" \\ back\\', ""]
        checks = {"literal-argv": probe("argv", *literal), "missing-tool": (["mrs-no-such-tool-ü"], True)}
        expected = {"literal-argv": ("pass", None),
                    "missing-tool": ("invalid", "prerequisite missing: 'mrs-no-such-tool-ü' is not found on PATH")}
        if os.name == "nt":
            launchers = self.tmp / "launchers"
            launchers.mkdir()
            (launchers / "mrs-fixture-launcher.cmd").write_bytes(b"@echo off\r\nexit /b 0\r\n")
            os.environ["PATH"] = str(launchers) + os.pathsep + os.environ["PATH"]
            checks.update({"batch-by-name": (["mrs-fixture-launcher"], True),
                           "batch-explicit": (["mrs-fixture-launcher.cmd"], True)})
            reason = ("'{}' resolves to the batch launcher mrs-fixture-launcher.cmd, which runs through cmd.exe; "
                      "batch launchers are unsupported")
            expected.update({"batch-by-name": ("invalid", reason.format("mrs-fixture-launcher")),
                             "batch-explicit": ("invalid", reason.format("mrs-fixture-launcher.cmd"))})
        target, _, _ = self.consumer("probe", checks)

        code, report, path = self.collect(target, "invocation")

        self.assertEqual(code, 1)
        self.assertEqual(outcomes(path), expected)
        results = {result["check"]: result for result in report["record"]["results"]}
        self.assertEqual(json.loads(results["literal-argv"]["evidence"]["text"]), literal)
        self.assertEqual(results["literal-argv"]["executable"], {"name": os.path.basename(sys.executable),
                                                                 "sha256": interpreter_sha256()})
        for name in set(expected) - {"literal-argv"}:
            self.assertEqual((results[name]["executable"], results[name]["exit"], results[name]["started"]),
                             (None, None, None), f"{name} must not have run")

    def test_checks_see_only_their_isolated_checkout_even_from_a_hook_environment(self):
        """Source preservation / Invocation: when Git variables point at the caller's repository (as inside a
        Git hook), a check's own Git commands still see only the isolated checkout of C, and the caller's
        commit, files and index are unchanged."""
        target, _, c = self.consumer("probe", {"git-view": probe("head")})
        caller = self.tmp / "hook caller"
        out("clone", "--quiet", "--branch", "dev", target, caller)
        self.commit(caller, {"local.txt": "caller-only work\n"}, "Caller-only commit")
        (caller / "unstaged.txt").write_text("dirty\n", encoding="utf-8")
        before = tree_digest(caller)
        hook = {"GIT_DIR": str(caller / ".git"), "GIT_WORK_TREE": str(caller),
                "GIT_INDEX_FILE": str(caller / ".git" / "index")}
        with mock.patch.dict(os.environ, hook):
            code, report, _ = self.collect(target, "hook environment")
        self.assertEqual(code, 0, report)
        [result] = report["record"]["results"]
        self.assertEqual(result["evidence"]["text"], f"HEAD {c}; status lines 0\n")
        self.assertEqual(tree_digest(caller), before)

    def test_selected_sources_are_verified_before_and_after_the_run(self):
        """Source changes: missing, dirty or moved pstack and a dirty or moved shared tool refuse before any
        check runs; a selected pstack or tool checkout changing during the run invalidates every result."""
        target, _, _ = self.consumer("probe", {"touches": probe("touch")})
        dirty_pstack = self.pstack_clone("dirty pstack")
        (dirty_pstack / "pstack" / "README.md").write_bytes(b"dirty\n")
        moved_pstack = self.pstack_clone("moved pstack")
        self.commit(moved_pstack, {"pstack/README.md": "newer local revision\n"}, "Move the pstack checkout")
        dirty_tool = self.tool_copy("dirty tool")
        (dirty_tool / "untracked residue.txt").write_text("residue\n", encoding="utf-8")
        moved_tool = self.tool_copy("moved tool")
        self.commit(moved_tool, {"NOTES.md": "local edit\n"}, "Move the tool checkout")
        refusals = {
            "missing pstack": ({"pstack": self.tmp / "no such pstack"}, "is missing"),
            "dirty pstack": ({"pstack": dirty_pstack}, "has modified, untracked or ignored content"),
            "moved pstack": ({"pstack": moved_pstack}, f"but {PIN} is selected"),
            "dirty tool": ({"checkout": dirty_tool}, "has modified, untracked or ignored content"),
            "moved tool": ({"checkout": moved_tool}, f"but {shared_snapshot()[1]} is selected"),
        }
        for name, (where, reason) in refusals.items():
            with self.subTest(name):
                code, report, path = self.collect(target, name, **where)
                self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
                self.assertIn(reason, report["reason"])
                self.assertFalse(path.exists(), "no record without verified sources")

        for name in ("pstack", "tool"):
            with self.subTest(f"{name} changes during the run"):
                clean = self.pstack_clone("touched pstack") if name == "pstack" else self.tool_copy("touched tool")
                os.environ["FIXTURE_DIRTY"] = str(clean / "pstack" if name == "pstack" else clean)
                where = {"pstack": clean} if name == "pstack" else {"checkout": clean}
                code, report, path = self.collect(target, f"touched {name}", **where)
                self.assertEqual((code, report["status"]), (5, "INVALIDATED"), report)
                [(outcome, reason)] = outcomes(path).values()
                self.assertEqual(outcome, "invalid")
                self.assertTrue(reason.startswith("the selected shared or pstack source changed during the run: "),
                                reason)
                self.assertIn("has modified, untracked or ignored content", reason)

    def test_dev_advancing_during_checks_keeps_C_and_a_stale_release_preserves_the_competitor(self):
        """Integration moves / Candidate identity: a real dev advance during the run leaves the record on C;
        the competitor's commit has C's exact tree, yet C's evidence does not transfer to it; a checked
        release of C is refused as stale and publishes nothing."""
        target, _, c = self.consumer("probe", {"advances-dev": probe("advance")})
        os.environ["FIXTURE_TARGET"] = str(target)

        code, report, path = self.collect(target, "advancing")

        self.assertEqual((code, report["record"]["candidate"]), (0, c), report)
        competitor = refs(target)[DEV]
        self.assertNotEqual(competitor, c)
        self.assertEqual(out("-C", target, "rev-parse", f"{competitor}^{{tree}}"), out("-C", target, "rev-parse",
                                                                                       f"{c}^{{tree}}"))
        code, assessment = self.assess(target, [path], self.verdict(path))
        self.assertEqual((code, assessment["status"]), (3, "REFUSED"))
        self.assertIn(f"record 1: candidate is '{c}', but the assessed candidate has '{competitor}'",
                      assessment["problems"])
        operation = self.checked_prepare(target, "stale op", c, [path], self.verdict(path))
        self.assertEqual(operation["refused"], "stale", operation)
        self.assertEqual(refs(target), {DEV: competitor})

    def test_candidate_without_usable_lifecycle_criteria_is_refused(self):
        """Missing or malformed criteria: a context-only dev, a lifecycle dev without checks and criteria
        that require no check on this OS refuse collection; malformed declarations fail clearly."""
        context = self.bare("context target.git")
        source = self.checkout("context source")
        self.commit(source, {"multi-repo-stack.json": json.dumps({
            "format": 1, "repository": "example/app", "applicability": "context",
            "shared": {"repository": SHARED_REPOSITORY, "commit": shared_snapshot()[1]}}) + "\n"}, "Context only")
        out("-C", source, "push", "--quiet", "--no-follow-tags", context, "HEAD:refs/heads/dev")
        unchecked, _, _ = self.bootstrapped()
        elsewhere = {"windows": "linux", "linux": "macos", "macos": "windows"}[NATIVE]
        foreign, _, _ = self.consumer("research", RESEARCH, environments=(elsewhere,))
        for target, reason in ((context, "is not a lifecycle candidate"),
                               (unchecked, "declares no lifecycle checks"),
                               (foreign, f"no declared check environment requires {NATIVE}")):
            with self.subTest(reason):
                code, report, path = self.collect(target, reason[:12])
                self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
                self.assertIn(reason, report["reason"])

        valid = json.loads(selector("example/app", RESEARCH, [NATIVE], shared_snapshot()[1]))
        malformed = {
            "argv[0] must be a command name": ("checks", {"x": {"argv": ["./run"], "environments": [NATIVE],
                                                                "evidence_required": False}}),
            "must be distinct labels declared": ("checks", {"x": {"argv": ["python"], "environments": ["gpu"],
                                                                  "evidence_required": False}}),
            "must be one of ('windows', 'linux', 'macos')": ("environments", {NATIVE: {"os": "wsl"}}),
            "must be true or false": ("checks", {"x": {"argv": ["python"], "environments": [NATIVE],
                                                       "evidence_required": "yes"}}),
            "lowercase-kebab labels": ("checks", {"Upper": {"argv": ["python"], "environments": [NATIVE],
                                                            "evidence_required": False}}),
            "declare both 'checks' and 'environments'": ("environments", None),
            "a context selector declares none": ("applicability", "context"),
        }
        for reason, (key, value) in malformed.items():
            with self.subTest(reason):
                broken = dict(valid)
                if value is None:
                    del broken[key]
                else:
                    broken[key] = value
                with self.assertRaises(config.ConfigError) as caught:
                    config.parse(json.dumps(broken).encode("utf-8"))
                self.assertIn(reason, str(caught.exception))
        unused = dict(valid, environments={NATIVE: {"os": NATIVE}, "spare": {"os": NATIVE}})
        with self.assertRaisesRegex(config.ConfigError, r"declared but required by no check"):
            config.parse(json.dumps(unused).encode("utf-8"))

    def test_entries_an_isolated_checkout_cannot_reproduce_are_refused(self):
        """Source preservation / Portability: a submodule gitlink, and on Windows a symbolic link, cannot be
        reproduced exactly by an isolated checkout of C, so collection refuses instead of judging other bytes."""
        _, source, b = self.consumer("research", RESEARCH)

        def commit_index(message: str) -> str:  # entries exist only in the index, so no `add -A`
            out("-C", source, "commit", "--quiet", "-m", message)
            return out("-C", source, "rev-parse", "HEAD")

        out("-C", source, "update-index", "--add", "--cacheinfo", f"160000,{b},vendored")
        gitlinked = commit_index("Add a submodule gitlink")
        with_gitlink = self.bare("gitlink target.git")
        out("-C", source, "push", "--quiet", "--no-follow-tags", with_gitlink, f"{gitlinked}:refs/heads/dev")
        code, report, _ = self.collect(with_gitlink, "gitlink")
        self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
        self.assertIn("vendored is a submodule", report["reason"])

        out("-C", source, "update-index", "--force-remove", "vendored")
        blob = out("-C", source, "hash-object", "-w", "--stdin", input=b"analysis/model.py")
        out("-C", source, "update-index", "--add", "--cacheinfo", f"120000,{blob},model-link.py")
        linked = commit_index("Replace the gitlink with a relative symbolic link")
        with_link = self.bare("link target.git")
        out("-C", source, "push", "--quiet", "--no-follow-tags", with_link, f"{linked}:refs/heads/dev")
        code, report, _ = self.collect(with_link, "link")
        if os.name == "nt":
            self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
            self.assertIn("model-link.py is a symbolic link", report["reason"])
        else:
            self.assertEqual((code, report["status"]), (0, "PASSED"), report)

        out("-C", source, "update-index", "--force-remove", "model-link.py")
        for name, data in (("Case.txt", b"upper\n"), ("case.txt", b"lower\n")):
            blob = out("-C", source, "hash-object", "-w", "--stdin", input=data)
            out("-C", source, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
        colliding = commit_index("Add paths that differ only in case")
        with_case = self.bare("case target.git")
        out("-C", source, "push", "--quiet", "--no-follow-tags", with_case, f"{colliding}:refs/heads/dev")
        (self.tmp / "case probe").write_text("x", encoding="utf-8")
        code, report, _ = self.collect(with_case, "case")
        if (self.tmp / "CASE PROBE").exists():  # a case-insensitive filesystem keeps only one of the two files
            self.assertEqual((code, report["status"]), (3, "REFUSED"), report)
            self.assertIn("here is not exact", report["reason"])
        else:
            self.assertEqual((code, report["status"]), (0, "PASSED"), report)


if __name__ == "__main__":
    unittest.main()

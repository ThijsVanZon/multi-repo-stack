"""The pre-merge review workflow (.github/workflows/review.yml), by running its step against a stub gh."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import ROOT

HEAD = "c" * 40


def review_step() -> str:
    """The workflow's one `run: |` block, exactly as committed."""
    lines = (ROOT / ".github" / "workflows" / "review.yml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |") + 1
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        block.append(line[indent:])
    return "\n".join(block) + "\n"


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "the workflow step runs in bash on Linux")
class ReviewWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mrs review ü "))
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "step.sh").write_text(review_step(), encoding="utf-8")
        gh = self.tmp / "bin" / "gh"
        gh.parent.mkdir()
        gh.write_text('#!/bin/sh\ncase "$2" in */pulls/*) echo "$PR_HEAD" ;; *) echo "$@" >> "$STATUS_LOG" ;; esac\n',
                      encoding="utf-8")
        gh.chmod(0o755)

    def record(self, body: str) -> tuple[int, str]:
        """Run the step for a comment body on a PR whose head is HEAD; return its exit and the statuses set."""
        log = self.tmp / "statuses.log"
        log.write_text("", encoding="utf-8")
        env = {"PATH": f"{self.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}", "PR_HEAD": HEAD,
               "STATUS_LOG": str(log), "BODY": body, "PR": "16", "URL": "https://example.invalid/comment",
               "GITHUB_REPOSITORY": "example/app", "GH_TOKEN": "synthetic"}
        run = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(self.tmp / "step.sh")],
                             env=env, capture_output=True, text=True)
        return run.returncode, log.read_text(encoding="utf-8")

    def test_a_verdict_sets_the_status_only_on_the_exact_current_head(self):
        """Pre-merge review: `PASS <head>` sets success and `FAIL <head>` failure on that head, CRLF and notes
        below the first line included; a stale or malformed first line sets nothing."""
        status = f"api repos/example/app/statuses/{HEAD} -f state=%s -f context=pre-merge review"
        self.assertEqual(self.record(f"PASS {HEAD}")[0], 0)
        self.assertIn(status % "success", self.record(f"PASS {HEAD}\r\nNotes follow.")[1])
        self.assertIn(status % "failure", self.record(f"FAIL {HEAD}")[1])
        for body in (f"PASS {'b' * 40}", f"PASS {HEAD} extra", f"PASS {HEAD[:7]}", f"pass {HEAD}",
                     f"PASS {HEAD.upper()}", f"\nPASS {HEAD}"):
            self.assertEqual(self.record(body), (1, ""), body)

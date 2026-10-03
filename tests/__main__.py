"""The shared project's declared check: `python -I -B tests`, run from the repository root, runs the whole
suite. Under `mrs collect` (MRS_EVIDENCE_FILE set) it also writes a public-safe summary of what ran, failed
and was skipped, so a passing record shows its coverage rather than only an exit status."""
import os
import sys
import unittest

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.discover(os.path.join(root, "tests"), top_level_dir=root))
evidence = os.environ.get("MRS_EVIDENCE_FILE")
if evidence:
    lines = [f"tests run {result.testsRun}, failures {len(result.failures)}, errors {len(result.errors)}, "
             f"skipped {len(result.skipped)}"]
    lines += [f"skipped {test.id()}: {reason}" for test, reason in result.skipped]
    lines += [f"failed {test.id()}" for test, _ in result.failures + result.errors]
    with open(evidence, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
sys.exit(0 if result.wasSuccessful() else 1)

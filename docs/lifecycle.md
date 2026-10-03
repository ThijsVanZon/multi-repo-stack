# Lifecycle rules (`lifecycle` applicability only)

- `dev` carries the active unreleased line. `main` is the latest contract release, and is absent before the first one.
- Root `VERSION` holds `YY.RELEASE.PATCH`: a two-digit year, no `v` prefix and a single final newline. Task branches are `<VERSION>/<lowercase-kebab-task>` and target `dev`. Ordinary tasks never change `VERSION`. After the line advances, port old-line work explicitly to a correctly prefixed branch.
- **Bootstrap** creates only `refs/heads/dev`, at an exact prepared commit, on an empty target. It never adopts or overwrites a nonempty repository.
- **Acceptance** of the integrated commit C needs two things:
  - a passing record for every check/environment pair that C's own `multi-repo-stack.json` requires, each collected on an isolated checkout of exactly C by a runner on that environment's native OS;
  - a PASS verdict from a separately commissioned non-author, bound to exact C and its criteria.

  Passing checks alone never accept C. Neither does a verdict on another commit or on a PR merge.
- **Release** promotes the exact integrated commit C in one guarded atomic push, with explicit leases, `--no-follow-tags`, `--recurse-submodules=no` and no fallback:
  - `main` → C;
  - the annotated tag `N` (receipt embedded, directly targeting C);
  - `dev` → D, a direct child of C that changes only `VERSION`. In the same UTC year the next line is RELEASE+1; the first line opened in a later year is `YY.1.0`.
- A lost response stays UNKNOWN until read-only reconciliation. Retries reuse the exact prepared tag and D. A mixed state stops for a separately reviewed repair.
- These all block lifecycle mutation: foreign version-shaped tags, malformed receipts, a `main` without a contract release, and inconsistent or unrelated history.
- README.md records which of these mechanisms this revision implements.

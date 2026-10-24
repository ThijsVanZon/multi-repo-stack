# Lifecycle rules (`lifecycle` applicability only)

- `dev` carries the active unreleased line. `main` is the latest contract release, and is absent before the first one.
- **Protection.** On the forge, `dev` and `main` refuse deletion and non-fast-forward updates from everyone. Otherwise `dev` changes only by a squash-merged PR whose required checks pass, and `main` only by a release; only the release operator bypasses this, because a release pushes `main` and `dev` directly. Where the forge cannot enforce this, the consumer's `AGENTS.md` records the deviation. `docs/guide.md` gives the setup.
- Root `VERSION` holds `YY.RELEASE.PATCH`: a two-digit year, no `v` prefix and a single final newline. Task branches are `<VERSION>/<task>`, with `<task>` in lowercase kebab-case starting with a letter, and target `dev`. Ordinary tasks never change `VERSION`. After the line advances, port old-line work explicitly to a correctly prefixed branch.
- **Tasks.** pstack runs the PR workflow (opening a PR, babysitting, shipping); this tool adds only the lifecycle policy, and runs no forge command for tasks.
  - `mrs task create` creates `<active VERSION>/<task>` in your checkout, at the target's current `dev`, without checking it out. It never moves an existing branch, and changes nothing else in your checkout.
  - `mrs task check` validates a task against the target's current state: the active prefix, the name, the base, descent from the commit that opened the line, an unchanged `VERSION` and a lifecycle selector. A stacked task must contain its parent's current tip on the target.
  - A VALID result describes one observation. It locks nothing and accepts nothing. Run the check again immediately before landing; a green CI run from before `dev` moved is not that check.
  - Task PRs land in the order they were opened, lowest number first, and none is abandoned: work dropped from a PR is reverted or superseded by later commits in that PR. After each landing, the next one is brought onto the new `dev` before its check.
  - **Port** old-line work after a release: `mrs task create` makes the new-line branch, then `git cherry-pick -x <old base>..<old branch>` onto it records which commits were ported. Keep the old branch, check the new one and run the checks again. Never rename or retarget the old branch.
- **pstack under this lifecycle** (deliberate, narrow adaptations of the pinned pstack):
  - Trunk is `dev`, not `main`, because `main` moves only by release. Root PRs target `dev`; a stacked child targets its same-line parent task branch.
  - Branch names carry the active line, so a task's line is visible and checkable.
  - Shipping lands verified PRs on `dev` only. A release is a separate operation, never a PR into `main`. A task verdict that survives a rebase under Shipping's patch-id rule never carries release acceptance.
  - Before merge, the PR brings every standing document it affects (README.md, `docs/`, agent instructions) into line with the result, so they stay aligned with `dev`. Every squash merge sets its message explicitly, never GitHub's default: the subject is `<PR title> (#<N>)`, the body a short summary of the change, and `Co-Authored-By` the only trailer. No session links, attribution lines or AI footers. A landing is complete only once its task branch no longer exists on the remote.
  - Rewriting a task branch (rebase, amend) is pstack's business. `dev`, `main` and release tags are never rewritten.
- **Bootstrap** creates only `refs/heads/dev`, at an exact prepared commit, on an empty target. It never adopts or overwrites a nonempty repository.
- **Acceptance** of the integrated commit C needs two things:
  - a passing record for every check/environment pair that C's own `multi-repo-stack.json` requires, each collected on an isolated checkout of exactly C by a runner on that environment's native OS;
  - a PASS verdict from a separately commissioned non-author, neither the session that wrote the change nor whoever directs and approves it, bound to exact C and its criteria.

  Passing checks alone never accept C. Neither does a verdict on another commit or on a PR merge.
- **Release** promotes the exact integrated commit C in one guarded atomic push, with explicit leases, `--no-follow-tags`, `--recurse-submodules=no` and no fallback:
  - `main` → C;
  - the annotated tag `N` (receipt embedded, directly targeting C);
  - `dev` → D, a direct child of C that changes only `VERSION`. In the same UTC year the next line is RELEASE+1; the first line opened in a later year is `YY.1.0`.
- **Operations.** `mrs prepare bootstrap` and `mrs prepare release` write a new operation folder and publish nothing; a release needs a SUFFICIENT assessment of C and runs from the tool and pstack checkouts that C selects. `mrs operation inspect` shows the one push, `mrs operation apply` makes it, and `mrs operation reconcile` reads the result. Apply and reconcile with the same tool checkout and operation folder, and apply only under a grant for that exact operation.
- **Release pages** (optional, GitHub). After a release reconciles COMPLETED, `mrs page prepare` seals the notes, the one create request and the bound principal into one operation; `mrs page apply` sends it, under a grant naming its digest, at most once with the token from your gh, and `mrs page reconcile` reads. A page never changes Git and never reads latest. Absence counts only for the bound principal with the `repo` scope and push access, else it is UNKNOWN. UNKNOWN is reconciled by reading, never by resending; DIVERGED stops.
- A lost response stays UNKNOWN until read-only reconciliation. Retries reuse the exact prepared tag and D. A mixed state stops for a separately reviewed repair.
- These all block lifecycle mutation: foreign version-shaped tags, malformed receipts, a `main` without a contract release, and inconsistent or unrelated history.
- **Patches.** Releases with `PATCH` above 0 are unsupported. Ship an urgent fix as an ordinary task of the active line and release that line. A patch tag or `main` move made by hand leaves `mrs inspect` UNSUPPORTED and blocks every later lifecycle mutation until a separately reviewed repair.

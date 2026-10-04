# multi-repo-stack

A small shared substrate through which agents connect repositories to exact shared context, and through which lifecycle repositories bootstrap `dev`, accept exact integrated commits and promote releases with one guarded atomic Git transaction. Product truth stays in each product repository. Engineering and PR workflow come from canonical [pstack](https://github.com/cursor/plugins/tree/main/pstack), pinned by commit in `multi-repo-stack.json`.

## Status: production bootstrap and release application

This revision adds the public bootstrap and release operations: prepare, inspect, explicit apply and read-only reconcile. They build on task branches, connection and upgrades, exact-commit acceptance and the local Git transaction kernel. The target repository is active on `dev`. This revision is **not** release 26.1.0, and it has not been accepted.

Implemented and exercised by tests against disposable local repositories:

- **Source and applicability preflight** (`mrs preflight`). It reads the consumer's committed selector and verifies the selected shared checkout and the pinned pstack checkout: exact HEAD, no modified, untracked or ignored content, and content re-hashed byte-for-byte against the commit. Replace refs are ignored and checkout filters are not trusted; clone with `--config core.autocrlf=false` if your Git converts line endings. It prints the verified identities and the instruction files that apply: `context` loads `docs/context.md`; `lifecycle` also loads `docs/lifecycle.md`. `--expect` invalidates a run whose selection changed. When the shared project checks itself, the executing checkout must be clean and committed too: editing is ordinary work, but a dirty checkout is refused, and a new commit invalidates an earlier `--expect` record until a fresh preflight records it.
- **State observation** (`mrs inspect`). It separates absent refs from UNKNOWN; hidden-ref configuration, broken refs, missing history and Git configuration that cannot be read count as UNKNOWN. A target that Git's configuration would redirect elsewhere is refused rather than observed. It recognizes contract release tags (annotated, directly targeting C, embedding a canonical receipt that binds exact object IDs) and applies the release-history predicates.
- **Acceptance criteria** live in the lifecycle selector:
  - `checks` names argv arrays, the environment labels each one requires, and whether it must write evidence.
  - `environments` gives each label the native OS it requires: `windows`, `linux` or `macos` (WSL is linux).
  - The criteria identity is the SHA-256 of the canonical `checks` and `environments`, read from Git objects at the candidate.
  - Context selectors declare none.
- **Check collection** (`mrs collect`).
  - **Identity:** C is the repository's observed `dev`, and N is C's `VERSION`.
  - **Sources:** the selected shared and pstack checkouts are verified before any check runs and again after the run. A change during the run invalidates every result.
  - **Isolation:** every declared pair whose environment requires this OS runs in its own fresh checkout of exactly C, with no line-ending, filter or encoding conversion.
  - **Invocation:** each check runs as a literal argv without a shell. Its executable is resolved on PATH and identified by name and SHA-256; batch launchers are refused.
  - **Source checks:** tracked bytes, executable bits and links are verified before and after each command. A command that changes them is invalid even when it exits 0; untracked build outputs are allowed. If a tracked link resolves outside the checkout, directly or through other links, collection is refused before any check runs. Windows refuses tracked links altogether.
  - **Unmet pairs:** pairs for other OSes are listed as unmet.
  - **Record:** public-safe JSON kept outside C. Each pair's raw output goes to a private log next to it, bound by size and SHA-256.
  - A passing collection is check evidence, never acceptance.
- **Check evidence.** A check may write one UTF-8 file of at most 64 KiB to the path in `MRS_EVIDENCE_FILE`, outside its checkout. The record keeps the exact bytes and their SHA-256. Absent required evidence, a link, a replaced folder, another encoding or an oversized file makes the result invalid. Optional absence is recorded.
- **Assessment** (`mrs assess`).
  - **SUFFICIENT** needs a passing record from the right native OS for every required pair. Every record must bind this repository, C, N, the criteria identity and the selected shared and pstack sources.
  - A record counts only with the facts of an actual execution: C observed as `dev` at a UTC time, the runner's OS, platform, Python and Git, an exact checkout and the pstack source. Each passing result also needs its executable, its start and finish times and its output identity. A record without them, or with contradictory values, is refused.
  - It also needs a separately supplied verdict, described below.
  - **INCOMPLETE** means something is missing. **REFUSED** means something is failed, mislabelled, mixed or unbound.
  - These are content and binding checks for trusted operators. The tool cannot authenticate who wrote a verdict, or whether its statements are true.
- **Task branches** (`mrs task create`, `mrs task check`).
  - `create` makes `<active VERSION>/<task>` in the caller's local checkout, at the target's current `dev`. It refuses an existing branch, here or on the target, and changes nothing else in the checkout.
  - `check` validates a task against the target's current state: the active prefix, a lowercase kebab-case name, base `dev` or a same-line parent task that it contains, descent from the commit that opened the line, an unchanged `VERSION` and a lifecycle selector. A VALID result describes one observation; it locks and accepts nothing.
  - Both refuse context-only, empty, unsupported and redirected targets. pstack owns PRs, stacks, review and landing; `docs/lifecycle.md` describes the explicit port after a line advances.
- **Connection and upgrades** are documented procedures, not tools (`docs/connect.md`). An existing repository connects for context with a selector and an appended `AGENTS.md` route, and nothing else changes. An upgrade changes the one selector; old checkouts, old `--expect` records and old check records then refuse.
- **Bootstrap and release operations** (`mrs prepare`, `mrs operation`; `mrs/transactions.py`):
  - **Prepare** writes only a new operation folder outside any checkout: the operation record and a private store holding the prepared objects. It publishes nothing.
    - `prepare bootstrap` targets an observed empty repository: any ref, including a provider's README commit, refuses, and UNKNOWN is never empty. Apply then creates only `dev` at exact B, expecting it absent. A lease on `dev` cannot guard other refs, so you must be the target's sole initializer.
    - `prepare release` promotes the target's observed `dev` C, which you name. It needs a SUFFICIENT assessment of exact C, from the same records and verdict as `assess`, made by the tool and pstack checkouts that C selects. It prepares the receipt tag T on C, and D: C's direct child that changes only `VERSION` and records the UTC opening date.
  - **Inspect** validates the saved operation and shows the one push that apply would make. **Apply** observes the target again, then makes that push: explicit leases on every changed ref, full refspecs, `--atomic` for a release, `--no-follow-tags` and `--recurse-submodules=no`, with no fallback and no retry. A direct library push to a real target makes the same observation first and refuses wherever apply would, including a release whose next line was opened in an earlier year; only a disposable test fixture is pushed without it, so the tests can show that the leases themselves reject. **Reconcile** reads the target and reports COMPLETED, NOT_APPLIED, DIVERGED, MIXED or UNKNOWN. A lost response stays UNKNOWN until reconciliation. Repeating a completed operation is a no-op, also after later development and releases.
  - **Bindings.** A bootstrap may only create `dev` at B. A release may only move `main` from P (or absence) to C, create tag N at T and move `dev` from C to D, and its other facts must match T's validated receipt, including the identity of its acceptance. The store also binds the operation's kind, destination, expected values (including P or an absent `main`, as observed at preparation) and the identity of the tool that prepared it. Inspect, apply, the push and reconciliation, called through the commands or the library, refuse a record that differs from the prepared objects or that binding, and a running tool other than the one that prepared it. A real target also needs that tool to be a clean committed checkout. Nothing is repaired or regenerated: T and D keep their identities across retries.
  - **Destinations** are one absolute path to a local bare repository, or an https, ssh or file URL without a password. Relative paths, remote names, scp-like addresses and URLs that cannot be parsed are refused, and the reason does not repeat a refused destination, since it may carry a credential. Before every observation, fetch and push, Git itself must resolve the destination to exactly itself, and the transport may use only the destination's own protocol.
  - **Receipts.** Observers re-validate an assessed receipt whenever they read the tag. They apply the same record validation, against what C provides (its criteria, its shared pin and, when C pins pstack itself, that pin) and against the retained release history (the previous release's criteria, or none at a first release). An observer has neither the shared revision a consumer selects nor a pstack checkout. It therefore checks a consumer's pstack pin, and every pstack tree, only for form and for agreement across records.

**Hosted CI** (`.github/workflows/checks.yml`) has read-only permissions and runs on Linux, Windows and macOS with Python 3.11. Pull requests get task evidence for their head commit. Pushes to `dev` get an exact-commit record of the target's observed `dev`. See `docs/verification.md`.

Deliberate limits:

- Only `operation apply` changes a target, and only as prepared. `task create` adds one new branch, and the objects it needs, to the caller's local checkout; `prepare` writes only its new operation folder; nothing else writes outside its own scratch. Any destination rewrite, including an empty `insteadOf`/`pushInsteadOf` prefix in whichever configuration file holds it, or a remote named like the destination, is refused. Configuration that Git cannot read is UNKNOWN.
- Pushes come from the operation's own bare store, so submodule recursion has no submodule repository to act on. `--recurse-submodules=no` is defense in depth there, not a separately demonstrable guard.
- The low-level `prepare_release` records an unjudged payload for the transaction tests, and refuses one that claims the assessed format. It accepts only disposable fixtures: local bare repositories carrying the `mrs-disposable-fixture` marker, which only test code creates. Observers still recognize such receipts.
- The tool cannot see a provider's own rules, such as branch protection, rulesets or hidden refs. It reports the push and the state it observes afterwards; it neither bypasses nor detects provider policy in advance.
- The collector cannot run agent-integration or provider checks. They reach acceptance only through the non-author verdict, which accepts or rejects each reused observation.
- This is trusted-repository execution, not a sandbox: checks run with the collector's permissions and environment, minus Git variables that would redirect them to another repository. Refusing escaping tracked links keeps ordinary build output inside the checkout; a command can still write anywhere its permissions allow. Evidence ownership is checked on POSIX only.
- Not proved: how a provider such as GitHub handles the guarded release push (atomicity, lease rejection, races and protections). The tests use local Git repositories only.
- This revision was exercised by its author only on Windows 11 with Git 2.37.3.windows.1 and Python 3.11.0. Hosted CI records describe only the commits they name. Linux, macOS, agent-integration and provider gates remain open for this revision.

## Use

Requires Python 3.11 or newer and Git on PATH. Only the standard library is used. Run the tool from the selected checkout with an explicit interpreter, without relying on shebangs:

```text
python -I -B <shared checkout>/mrs preflight --consumer <consumer checkout> --pstack <cursor/plugins checkout at the pinned commit> [--json] [--expect <earlier --json output>]
python -I -B <shared checkout>/mrs inspect --remote <path or URL> [--json]
python -I -B <shared checkout>/mrs collect --repository <path or URL> --pstack <pstack checkout> --out <new record file> [--json]
python -I -B <shared checkout>/mrs assess --repository <path or URL> --pstack <pstack checkout> --record <record file> [--record ...] [--attestation <verdict file>] [--reuse <proposals file>] [--json]
python -I -B <shared checkout>/mrs task create --repository <path or URL> [--checkout <local checkout>] --task <task> [--json]
python -I -B <shared checkout>/mrs task check --repository <path or URL> [--checkout <local checkout>] --branch <task branch> [--commit <commit>] [--base dev|<parent task branch>] [--json]
python -I -B <shared checkout>/mrs prepare bootstrap --repository <path or URL> --source <local repository> --commit <B> --pstack <pstack checkout> --work <new folder> [--json]
python -I -B <shared checkout>/mrs prepare release --repository <path or URL> --candidate <C> --pstack <pstack checkout> --record <record file> [--record ...] --attestation <verdict file> [--reuse <proposals file>] --identity "<Name> <email>" --work <new folder> [--json]
python -I -B <shared checkout>/mrs operation inspect|apply|reconcile --work <operation folder> [--json]
```

Exit codes:

- `0`: OK, every applicable check passed, the assessment is SUFFICIENT, the task branch was created, the task is VALID, the operation was prepared, is valid, was applied or was already complete.
- `1`: a check did not pass, the assessment is INCOMPLETE, the task is INVALID, or the operation is not applied yet.
- `2`: usage error.
- `3`: refused, or the target diverged from the operation.
- `4`: unknown observation or outcome.
- `5`: the selection changed during a run.
- `6`: mixed: the target holds part of the operation; stop and have a repair reviewed separately.

Only `operation apply` writes to a target. `collect` writes only its record and logs, `task create` only its new branch into your checkout, and `prepare` only its new operation folder.

**The verdict file** is JSON in the `multi-repo-stack/attestation/1` format. Its keys:

- `repository`, `candidate` (the full commit ID of C) and `version`.
- `criteria`:
  - `identity`: C's criteria identity.
  - `previous`: the latest release's criteria identity, or `null` at a first release.
  - `assessment`: how criteria changes since then, or since the frozen intent, were judged.
- `verifier`:
  - `relationship`: must be `non-author`.
  - `statement`: why the verifier is a non-author.
  - `harness`: the actual harness, tools and substitutions.
- `behavior` and `evidence`: what was inspected.
- `limitations`: each one `accepted` or `unresolved`.
- `reuse`: each proposed reused observation, `accepted` or `rejected`, with a rationale. An observation names its `id`, exact `source_commit`, `harness` and `scope`.
- `verdict`: `PASS` or `FAIL`.

Tests and the recipe for this project's own records: see `docs/verification.md`.

## Layout

- `AGENTS.md` is the agent entrypoint. `docs/` holds the routed instructions, the verification recipe and the connection and upgrade procedures.
- `.github/workflows/checks.yml` is the prepared read-only CI.
- `multi-repo-stack.json` is the single selector: repository identity, applicability, the pinned canonical pstack, and this project's own check criteria.
- `VERSION` holds the active line, `26.1.0`.
- `mrs/` is the tool. `tests/` holds the disposable-fixture tests; `tests/consumers/` holds the tiny consumer shapes they build.

## License

MIT (see `LICENSE`).

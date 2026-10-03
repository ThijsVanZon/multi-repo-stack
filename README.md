# multi-repo-stack

A small shared substrate through which agents connect repositories to exact shared context, and through which lifecycle repositories bootstrap `dev`, accept exact integrated commits and promote releases with one guarded atomic Git transaction. Product truth stays in each product repository. Engineering and PR workflow come from canonical [pstack](https://github.com/cursor/plugins/tree/main/pstack), pinned by commit in `multi-repo-stack.json`.

## Status: Slice 2, the exact-commit acceptance proof

This revision adds exact-commit acceptance to the Slice 1 local Git transaction proof. It is **not** release 26.1.0. It is not activated anywhere, and it has not been accepted.

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
- **Transaction kernel** (`mrs/transactions.py`, library only):
  - **Bootstrap:** guarded dev-only bootstrap of an exact prepared commit.
  - **Release:** preparation of an exact release (receipt tag T on C, and D, the direct child that changes only `VERSION`), then one atomic `main`/tag/`dev` push with explicit leases, `--no-follow-tags` and `--recurse-submodules=no`.
  - **Checked release path:** `prepare_accepted_release` assesses the records and the verdict for exactly the observed `dev`, with the sources C selects. It builds the receipt only from a SUFFICIENT assessment. Observers re-validate an assessed receipt whenever they read the tag. They apply the same record validation, against what C provides (its criteria, its shared pin and, when C pins pstack itself, that pin) and against the retained release history (the previous release's criteria, or none at a first release). An observer has neither the shared revision a consumer selects nor a pstack checkout. It therefore checks a consumer's pstack pin, and every pstack tree, only for form and for agreement across records. The low-level `prepare_release` still records an unjudged payload for the transaction tests, and refuses one that claims the assessed format.
  - **Reconciliation:** read-only, giving COMPLETED, NOT_APPLIED, DIVERGED, MIXED or UNKNOWN.
  - **Saved operations:** apply, the push primitive and reconciliation refuse an operation record unless it is exactly what its operation repository prepared. A bootstrap may only create `dev` at B, expecting it absent. A release may only move `main` from P (or absence) to C, create tag N at T and move `dev` from C to D. Its other facts must match T's validated receipt. A changed or unusable record is refused before any push and is never repaired.

Deliberate limits:

- There is no mutation command. The kernel mutates only absolute local bare repositories carrying the `mrs-disposable-fixture` marker, which only test code creates. Before every observation and push, Git itself resolves where fetching from and pushing to the destination would go. Any rewrite, including an empty `insteadOf`/`pushInsteadOf` prefix in whichever configuration file holds it, or a remote named like the destination, is refused. Configuration that Git cannot read is UNKNOWN. The kernel's transports may use only Git's file protocol.
- The kernel pushes from its own bare operation repository, so submodule recursion has no submodule repository to act on. `--recurse-submodules=no` is defense in depth there, not a separately demonstrable guard.
- The low-level kernel's receipts carry an unjudged payload; the tests use SIMULATED acceptance there. Observers still recognize such receipts. Only the fixture kernel can create one.
- The collector cannot run agent-integration or provider checks. They reach acceptance only through the non-author verdict, which accepts or rejects each reused observation.
- This is trusted-repository execution, not a sandbox: checks run with the collector's permissions and environment, minus Git variables that would redirect them to another repository. Refusing escaping tracked links keeps ordinary build output inside the checkout; a command can still write anywhere its permissions allow. Evidence ownership is checked on POSIX only.
- Not implemented yet:
  - task-branch creation and validation;
  - hosted CI;
  - provider (GitHub) tests and application;
  - consumer setup and upgrade tooling.
- This revision was exercised by its author only on Windows 11 with Git 2.37.3.windows.1 and Python 3.11.0 and 3.12.1. Linux, macOS, agent-integration and provider gates remain open.

## Use

Requires Python 3.11 or newer and Git on PATH. Only the standard library is used. Run the tool from the selected checkout with an explicit interpreter, without relying on shebangs:

```text
python -I -B <shared checkout>/mrs preflight --consumer <consumer checkout> --pstack <cursor/plugins checkout at the pinned commit> [--json] [--expect <earlier --json output>]
python -I -B <shared checkout>/mrs inspect --remote <path or URL> [--json]
python -I -B <shared checkout>/mrs collect --repository <path or URL> --pstack <pstack checkout> --out <new record file> [--json]
python -I -B <shared checkout>/mrs assess --repository <path or URL> --pstack <pstack checkout> --record <record file> [--record ...] [--attestation <verdict file>] [--reuse <proposals file>] [--json]
```

Exit codes:

- `0`: OK, every applicable check passed, or the assessment is SUFFICIENT.
- `1`: a check did not pass, or the assessment is INCOMPLETE.
- `2`: usage error.
- `3`: refused.
- `4`: unknown observation.
- `5`: the selection changed during a run.

Collection and assessment are read-only toward the repository; `collect` writes only its record and logs.

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

- `AGENTS.md` is the agent entrypoint. `docs/` holds the routed instructions and the verification recipe.
- `multi-repo-stack.json` is the single selector: repository identity, applicability, the pinned canonical pstack, and this project's own check criteria.
- `VERSION` holds the active line, `26.1.0`.
- `mrs/` is the tool. `tests/` holds the disposable-fixture tests; `tests/consumers/` holds the tiny consumer shapes they build.

## License

MIT (see `LICENSE`). The copyright attribution is deliberately unresolved and must be confirmed before any publication.

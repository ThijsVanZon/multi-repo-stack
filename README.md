# multi-repo-stack

A small shared substrate through which agents connect repositories to exact shared context, and through which lifecycle repositories bootstrap `dev`, accept exact integrated commits and promote releases with one guarded atomic Git transaction. Product truth stays in each product repository. Engineering and PR workflow come from canonical [pstack](https://github.com/cursor/plugins/tree/main/pstack), pinned by commit in `multi-repo-stack.json`.

## Status: Slice 1, the local Git transaction proof

This revision is a bounded first increment. It is **not** release 26.1.0. It is not activated anywhere, and it has not been accepted.

Implemented and exercised by tests against disposable local repositories:

- **Source and applicability preflight** (`mrs preflight`). It reads the consumer's committed selector and verifies the selected shared checkout and the pinned pstack checkout: exact HEAD, no modified, untracked or ignored content, and content re-hashed byte-for-byte against the commit. Replace refs are ignored and checkout filters are not trusted; clone with `--config core.autocrlf=false` if your Git converts line endings. It prints the verified identities and the instruction files that apply: `context` loads `docs/context.md`; `lifecycle` also loads `docs/lifecycle.md`. `--expect` invalidates a run whose selection changed. When the shared project checks itself, the executing checkout must be clean and committed too: editing is ordinary work, but a dirty checkout is refused, and a new commit invalidates an earlier `--expect` record until a fresh preflight records it.
- **State observation** (`mrs inspect`). It separates absent refs from UNKNOWN; hidden-ref configuration, broken refs, missing history and Git configuration that cannot be read count as UNKNOWN. A target that Git's configuration would redirect elsewhere is refused rather than observed. It recognizes contract release tags (annotated, directly targeting C, embedding a canonical receipt that binds exact object IDs) and applies the release-history predicates.
- **Transaction kernel** (`mrs/transactions.py`, library only):
  - **Bootstrap:** guarded dev-only bootstrap of an exact prepared commit.
  - **Release:** preparation of an exact release (receipt tag T on C, and D, the direct child that changes only `VERSION`), then one atomic `main`/tag/`dev` push with explicit leases, `--no-follow-tags` and `--recurse-submodules=no`.
  - **Reconciliation:** read-only, giving COMPLETED, NOT_APPLIED, DIVERGED, MIXED or UNKNOWN.
  - **Saved operations:** apply, the push primitive and reconciliation refuse an operation record unless it is exactly what its operation repository prepared. A bootstrap may only create `dev` at B, expecting it absent. A release may only move `main` from P (or absence) to C, create tag N at T and move `dev` from C to D. Its other facts must match T's validated receipt. A changed or unusable record is refused before any push and is never repaired.

Deliberate Slice 1 limits:

- There is no mutation command. The kernel mutates only absolute local bare repositories carrying the `mrs-disposable-fixture` marker, which only test code creates. Before every observation and push, Git itself resolves where fetching from and pushing to the destination would go. Any rewrite, including an empty `insteadOf`/`pushInsteadOf` prefix in whichever configuration file holds it, or a remote named like the destination, is refused. Configuration that Git cannot read is UNKNOWN. The kernel's transports may use only Git's file protocol.
- The kernel pushes from its own bare operation repository, so submodule recursion has no submodule repository to act on. `--recurse-submodules=no` is defense in depth there, not a separately demonstrable guard.
- The acceptance payload in a receipt is recorded, not judged. The tests use SIMULATED acceptance.
- Not implemented yet:
  - the check runner and evidence collection;
  - acceptance assessment;
  - task-branch creation and validation;
  - hosted CI;
  - provider (GitHub) tests;
  - consumer setup and upgrade tooling.
- This revision was exercised by its author only on Windows 11 with Git 2.37.3.windows.1 and Python 3.11.0 and 3.12.1. Linux, macOS and agent-integration gates remain open.

## Use

Requires Python 3.11 or newer and Git on PATH. Only the standard library is used. Run the tool from the selected checkout with an explicit interpreter, without relying on shebangs:

```text
python -I -B <shared checkout>/mrs preflight --consumer <consumer checkout> --pstack <cursor/plugins checkout at the pinned commit> [--json] [--expect <earlier --json output>]
python -I -B <shared checkout>/mrs inspect --remote <path or URL> [--json]
```

Exit codes: `0` OK, `2` usage, `3` refused, `4` unknown observation, `5` selection changed during a run.

Tests: see `docs/verification.md`.

## Layout

- `AGENTS.md` is the agent entrypoint. `docs/` holds the routed instructions and the verification recipe.
- `multi-repo-stack.json` is the single selector: repository identity, applicability, and the pinned canonical pstack.
- `VERSION` holds the active line, `26.1.0`.
- `mrs/` is the tool. `tests/` holds the disposable-fixture tests.

## License

MIT (see `LICENSE`). The copyright attribution is deliberately unresolved and must be confirmed before any publication.

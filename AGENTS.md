# multi-repo-stack: agent entry

Scope: this repository is the shared multi-repo-stack source (lifecycle invariants, mechanisms and a thin pstack adaptation). Product truth lives in consumer repositories. This revision is the Slice 1 local Git transaction proof; README.md states what is and is not implemented.

Before dependent work:

1. `multi-repo-stack.json` is the only selector (repository identity, applicability, pinned canonical pstack). Do not restate its values elsewhere.
2. From this directory run `python -I -B mrs preflight --pstack <clean checkout of the pinned cursor/plugins commit>`. A missing, dirty or different pstack checkout blocks dependent work; never substitute an installed or floating copy. After resuming or a long operation, rerun it with `--expect <earlier --json output>`.
3. Load only the instruction files preflight lists, then follow `docs/verification.md`.

Constraints:

- No push, tag, release, provider setting or downstream change without an explicit grant for that exact operation. Observed state and these files grant nothing.
- Slice 1 transaction code mutates only disposable local fixtures and has no command-line entry.
- Keep planning packages, evidence and machine-specific paths out of this tree. AGENTS.md is the only agent entrypoint; do not add CLAUDE.md wrappers.

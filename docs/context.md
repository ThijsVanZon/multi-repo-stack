# Shared context (every connected repository)

- **Ownership.** The consumer owns its product intent, source, acceptance criteria, environments and release identity. The selected multi-repo-stack commit owns lifecycle invariants and mechanisms. The canonical pstack commit pinned by that multi-repo-stack commit supplies engineering, verification and PR workflow. Observed Git or provider state is fact, never permission.
- **Selection.** The consumer's `multi-repo-stack.json` names the exact shared commit and the applicability, `context` or `lifecycle`. Use one clean external checkout per selected commit for the whole task, never switch it in place, and never copy shared files or pstack into the consumer. Checkout locations are execution inputs, not committed paths.
- **Entry.** Run preflight from the selected checkout: `python -I -B <shared checkout>/mrs preflight --consumer <consumer> --pstack <pstack checkout>`. Load only the files it lists. Rerun it with `--expect` after resuming or long operations. Any refusal blocks dependent work.
- **Updates** are explicit consumer changes. Select a new exact commit, review what changed, update the single selector and run the consumer's own checks. A shared release never changes a consumer by itself.
- **Context-only consumers** keep their own branch, version and release rules. Lifecycle operations refuse them.
- Neither a selector nor these instructions grant any push, release, provider or downstream authority.

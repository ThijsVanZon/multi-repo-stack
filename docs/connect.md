# Connecting a repository and upgrading its selection

Both are ordinary changes to the consumer, made through its own workflow. For a lifecycle consumer, that means a task branch and a PR to `dev`. Nothing here installs, generates or copies shared files or pstack into the consumer. No step grants push, release or provider authority.

## Connect an existing repository for context

1. **Read the consumer first:** its README, instructions (AGENTS.md and any CLAUDE files), branches, version identity and any `multi-repo-stack.json`. If a `multi-repo-stack.json` already exists, or an existing instruction contradicts the route below, stop and reconcile it with the owner. Never overwrite either.
2. **Select exact sources.** Choose a shared commit by its full ID, never a branch name. Get a clean external checkout of it, and a clean checkout of the pstack commit that this shared revision pins. Their locations are execution inputs; never commit them.
3. **Add the selector** `multi-repo-stack.json`:

   ```json
   {
     "format": 1,
     "repository": "<owner>/<name>",
     "applicability": "context",
     "shared": {"repository": "ThijsVanZon/multi-repo-stack", "commit": "<full commit ID>"}
   }
   ```

   A context selector declares no checks. Connecting adds no `VERSION`, `dev`, `main` or release rules, and does not certify an existing `main` as a release.
4. **Route from `AGENTS.md`.** Create it if it is absent. Otherwise append this section and keep everything already there:

   ```markdown
   ## multi-repo-stack

   `multi-repo-stack.json` selects the exact shared multi-repo-stack commit and its applicability. Before dependent work, run `python -I -B <shared checkout>/mrs preflight --consumer . --pstack <pstack checkout>` and load only the files it lists. Both checkouts are clean checkouts of the selected commits, supplied with the task. Rerun preflight with `--expect <earlier --json output>` after resuming. A refusal blocks dependent work. The selection grants no push, release or provider authority.
   ```

   Keep existing CLAUDE files, and do not add CLAUDE wrappers. Claude Code 2.1.287 on Windows was observed to load an existing `CLAUDE.md` and then not load `AGENTS.md` at all. In such a repository, start each session by reading `AGENTS.md` explicitly. Routing to it from the existing CLAUDE file instead, for example with an `@AGENTS.md` import line, is the consumer owner's decision about its own instructions. Whichever path you use, observe that it loads in your harness.
5. **Run preflight** from the shared checkout. It must report `applicability=context` and list only `docs/context.md`.
6. **Commit only those two files**, through the consumer's own workflow. Its history, branches, tags, version and every other file stay as they were.

Lifecycle adoption of a repository with existing content is not this procedure. It needs a separately reviewed transition to a compatible state.

## Upgrade the shared selection

1. **Review the change.** Choose the new exact shared commit, and review what changed since the selected one, including its pstack pin.
2. **Use separate checkouts.** Get a separate clean checkout of the new commit, and of its pstack pin if that changed. Never move a checkout in place: other consumers, or work in progress, may still use it.
3. **Change only the selection.** In a task, change only `shared.commit` in `multi-repo-stack.json`, plus any thin integration the reviewed change requires. Lifecycle consumers can create the task with `mrs task create` and check it with `mrs task check`.
4. **Re-enter and re-check.** Run preflight from the new checkout. Earlier `--expect` records, and the old checkout, now refuse. Run the consumer's own checks with the new sources. Earlier check records bind the old commit and shared source, so they do not apply to the new `dev`; collect again.
5. **Leave the product version alone.** The consumer's `VERSION` and product versions stay unchanged. Updating the shared selection is not a product release.

A selector names a commit. Moving a shared branch, or publishing a shared release, therefore changes no consumer.

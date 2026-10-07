# Connecting a repository and upgrading its selection

Both are ordinary changes to the consumer, made through its own workflow. For a lifecycle consumer, that means a task branch and a PR to `dev`. Nothing here installs, generates or copies shared files or pstack into the consumer. No step grants push, release or provider authority.

## Connect an existing repository for context

1. **Read the consumer first:** its README, instructions (AGENTS.md and any CLAUDE files), branches, version identity and any `multi-repo-stack.json`. If a `multi-repo-stack.json` already exists, or an existing instruction contradicts the route below, stop and reconcile it with the owner. Never overwrite either.
2. **Select exact sources.** Choose a shared commit by its full ID, never a branch name. Normally select a release: `git rev-parse "<version>^{commit}"`, because the tag name alone resolves to the annotated tag object. Get a clean external checkout of it, and a clean checkout of the pstack commit that this shared revision pins. Their locations are execution inputs; never commit them.
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
5. **Commit only those two files** locally. The consumer's history, branches, tags, version and every other file stay as they were.
6. **Run preflight** from the shared checkout; it reads the committed selector. It must report `applicability=context` and list only `docs/context.md`. Then land the commit through the consumer's own workflow.

Lifecycle adoption of a repository with existing content is not this procedure. It needs a separately reviewed transition to a compatible state.

## Check for an update (manual, read-only)

Nothing notifies a consumer or changes its selection; this check is manual. To check on demand, fetch into a scratch bare repository (`git init --bare <scratch>`) outside every checkout, with `<selected>` the commit your selector names and `<N>` a release that `inspect` lists:

```text
python -I -B <shared checkout>/mrs inspect --remote <shared repository> --json
git -C <scratch> fetch --no-tags <shared repository> refs/tags/<N>:refs/tags/<N>
git -C <scratch> merge-base --is-ancestor <selected> "<N>^{commit}"
git -C <scratch> diff --exit-code --stat <selected> "<N>^{commit}" -- multi-repo-stack.json mrs <routed docs>
```

Use `releases` only when `kind` is LIFECYCLE. UNSUPPORTED also exits 0 (an older selected tool, or Git older than 2.30, can report a valid release that way) and UNKNOWN (exit 4) means not observed; neither means "no release", so review with the shared owner. A failed fetch cannot tell. `merge-base` exit 0 means the release contains your selection. `diff --exit-code` exits 0 when the release changes nothing you load or run, including the pstack pin, and 1 when it does. `<routed docs>` are the files preflight lists under `load`, relative to the shared checkout. Any other exit means these objects cannot tell, for example when your selection is not in N's history or is newer than N; never read missing output as "no change". If you selected a commit that is not a release, also fetch `refs/heads/dev`; `merge-base --is-ancestor "<N>^{commit}" <selected>` exit 0 means your selection already contains N. A finding authorizes nothing; an upgrade is the task below, and you may decline it.

The shared project can compare its pstack pin with `cursor/plugins` `main` the same way: fetch `refs/heads/main` into a scratch bare repository, then `git merge-base --is-ancestor <pin> FETCH_HEAD` (0: `main` contains the pin) and `git diff --exit-code --stat <pin> FETCH_HEAD -- pstack` (0: nothing changed; 1: changed). This is optional discovery, not a release gate: any other exit, or a failed fetch, is an unknown discovery result and says nothing about the clean pinned checkout that preflight verifies. The pin moves only through its own reviewed task.

## Upgrade the shared selection

1. **Review the change.** Choose the new exact shared commit, and review what changed since the selected one, including its pstack pin.
2. **Use separate checkouts.** Get a separate clean checkout of the new commit, and of its pstack pin if that changed. Never move a checkout in place: other consumers, or work in progress, may still use it.
3. **Change only the selection.** In a task, change only `shared.commit` in `multi-repo-stack.json`, plus any thin integration the reviewed change requires. Lifecycle consumers can create the task with `mrs task create` and check it with `mrs task check`.
4. **Re-enter and re-check.** Run preflight from the new checkout. Earlier `--expect` records, and the old checkout, now refuse. Run the consumer's own checks with the new sources. Earlier check records bind the old commit and shared source, so they do not apply to the new `dev`; collect again.
5. **Leave the product version alone.** The consumer's `VERSION` and product versions stay unchanged. Updating the shared selection is not a product release.

A selector names a commit. Moving a shared branch, or publishing a shared release, therefore changes no consumer.

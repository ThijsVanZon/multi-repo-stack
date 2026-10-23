# Setting up a product repository

How a consumer connects, bootstraps, checks, loads pstack, and upgrades. Each step is an ordinary change to the consumer, made through its own workflow. For a lifecycle consumer, that means a task branch and a PR to `dev`. Nothing here installs, generates or copies shared files or pstack into the consumer. No step grants push, release or provider authority. The rules live in `docs/context.md` and `docs/lifecycle.md`, and the README states what each command does.

## Get the exact sources

Every step runs from a clean external checkout of the selected shared commit, with a clean checkout of the pstack commit that this shared revision pins. Select a shared commit by its full ID, never a branch name. Normally select a release: `git rev-parse "<version>^{commit}"`, because the tag name alone resolves to the annotated tag object. Fetch each by full ID with `core.autocrlf=false`, and read the pstack pin from the shared checkout's selector rather than restating it (POSIX shell; use your interpreter's name for `python3`):

```sh
get() { git init -q "$1"; git -C "$1" config core.autocrlf false; git -C "$1" fetch -q --depth 1 "$2" "$3"; git -C "$1" checkout -q --detach FETCH_HEAD; }
get <shared> https://github.com/ThijsVanZon/multi-repo-stack <full commit ID>
get <pstack> $(python3 -I -B -c 'import json, sys; p = json.load(open(sys.argv[1], encoding="utf-8"))["pstack"]; print("https://github.com/" + p["repository"], p["commit"])' <shared>/multi-repo-stack.json)
```

Their locations are execution inputs; never commit them. Use one checkout per selected commit and never move it in place: other consumers, or work in progress, may still use it.

## Connect an existing repository for context

1. **Read the consumer first:** its README, instructions (AGENTS.md and any CLAUDE files), branches, version identity and any `multi-repo-stack.json`. If a `multi-repo-stack.json` already exists, or an existing instruction contradicts the route below, stop and reconcile it with the owner. Never overwrite either.
2. **Get the exact sources** as above.
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

## Bootstrap a new lifecycle repository

1. **Create the target empty.** No README, license or `.gitignore` commit: any ref refuses bootstrap, and UNKNOWN is never empty. You must be its sole initializer.
2. **Make B**, a single commit in a local repository, holding only:
   - `VERSION`: `<YY>.1.0` and a newline;
   - `multi-repo-stack.json` as above, with `"applicability": "lifecycle"`. `checks` and `environments` are declared both or neither; B may omit them, but collect and assess refuse without them, so a check must land in a task before the first release;
   - `AGENTS.md` with the route section above, plus the consumer's own rules.

   Product content lands afterwards through task PRs to `dev`.
3. **Apply** from the shared checkout. Only apply publishes, and only under a grant for that exact operation:

   ```text
   python -I -B <shared>/mrs preflight --consumer <B checkout> --pstack <pstack>
   python -I -B <shared>/mrs inspect --remote <target>
   python -I -B <shared>/mrs prepare bootstrap --repository <target> --source <B checkout> --commit <B> --pstack <pstack> --work <new folder>
   python -I -B <shared>/mrs operation inspect --work <folder>
   python -I -B <shared>/mrs operation apply --work <folder>
   python -I -B <shared>/mrs operation reconcile --work <folder>
   ```

   `inspect` must report EMPTY first, and LIFECYCLE with the active line afterwards.
4. **Provider settings**, which the tool cannot see: confirm the default branch is `dev` (GitHub makes the first pushed branch the default), then set up Protection as `docs/lifecycle.md` describes.
5. **Private targets.** The tool runs Git with prompts disabled, so the target needs a credential helper that answers without prompting, or an SSH key.

## Hosted checks for a lifecycle consumer

This project's `.github/workflows/checks.yml` checks itself and runs `mrs` from its own candidate, so it does not fit a consumer as is. A consumer's read-only workflow checks out the candidate, the shared commit its selector names, and the pstack commit that shared commit pins, all with `core.autocrlf=false`. Then:

- `preflight --consumer <candidate>`;
- on a pull request, `task check --checkout <candidate>`: `--checkout` defaults to the working directory. The candidate needs full history (`fetch-depth: 0`); a shallow one gives INVALID or UNKNOWN. Run the declared checks there as task evidence;
- on a push to `dev`, `collect`, and keep its record (see `docs/verification.md`).

On a private target, the tool's own Git needs credentials: for example, a global credential helper that answers with the job token. `persist-credentials` does not reach the tool's clones, and `insteadOf` rewrites are refused.

## Load pstack into Claude Code or Cursor

`mrs pstack sync` runs preflight, then rebuilds the client's tree at the pin from Git objects into an immutable cache entry named by its content digest, next to a generated `mrs-handshake` plugin. It writes only new cache entries (`--cache`, default the user cache folder), never client, consumer or account files, and the client loads the entry by path. Nothing is installed: no client offers a per-repository pinned install, and account- or organization-wide plugins reach every repository.

- **Claude Code** loads upstream's generated `harness/pstack/claude-code` tree. Start the CLI with the two `--plugin-dir` arguments sync prints. The desktop app's local sessions take no flags: put the `CLAUDE_CODE_PLUGIN_DIRS` value sync prints in a saved local environment (Claude Code 2.1.280 or later) as one unquoted `NAME=value` line, and start only this repository's sessions from it, since the environment is not bound to a repository. Reopened, the editor shows a long value as its start and end only; the saved value stays whole, so close it without saving. Either way the paths last for that session and install nothing. Skills are named `pstack:<folder>`, and most run only as commands. After the selection changes, sync again and start new sessions with the new paths.
- **Claude app and claude.ai.** Their Code tab drives a session on your machine through Remote Control: start the CLI session above with `--remote-control` too; a desktop app session is reachable already, so open it from the session list rather than starting a new session there. It keeps this delivery and needs your machine on. Chat, Cowork and Dispatch cannot load it.
- **Claude Code cloud sessions** (claude.ai/code, the Claude app) install nothing from the repository. A cloud environment can stage the delivery. Its setup script gets the sources as above, syncs with `--consumer` set to the shared checkout, which gives the same entry as your consumer, and links that entry to a fixed path:

  ```sh
  set -eu
  # "Get the exact sources", with <shared> = /opt/mrs/shared and <pstack> = /opt/mrs/pstack, then:
  entry=$(python3 -I -B /opt/mrs/shared/mrs pstack sync --consumer /opt/mrs/shared --pstack /opt/mrs/pstack --client claude-code --cache /opt/mrs/cache --json | python3 -I -B -c 'import json, sys; print(json.load(sys.stdin)["entry"])')
  ln -sfn "$entry" /opt/mrs/current
  ```

  The environment sets `CLAUDE_CODE_PLUGIN_DIRS` to `/opt/mrs/current/pstack:/opt/mrs/current/mrs-handshake`. The link is the container's own, made fresh for each session and shared with no other selection; a refused sync leaves none, so the handshake fails. The environment is yours, not the repository's, and restates the shared commit: choose it only for this repository's sessions.
- **Cursor** loads `pstack/` through a `workspaceOpen` hook that returns `pluginPaths`. With `--client cursor`, `--hook` prints exactly that and exits 0, or prints `{}` on a refusal, so the workspace opens without the delivery.
- **Handshake.** Have the agent invoke the `mrs-handshake` skill, and compare what the skill returns with the line sync printed in your terminal. Keep that line out of the session, since an agent could repeat it. A Skill call that returns the same line shows that this session loaded this delivery; another line, or no such skill, means it did not. It shows that one skill of this delivery loaded, not that every skill did, and it cannot rule out another pstack source delivered to the account or organization.

The Cursor hook is optional and installed by a person, never by the tool. It goes in the consumer's `.cursor/hooks.json`, and holds machine paths, so keep it out of the consumer's commits (for example with `.git/info/exclude`). Project hooks run only in a trusted workspace, from the project root, which `--consumer` defaults to. Use forward slashes in paths, and on Windows prefix the command with `& `, because Cursor runs it inside PowerShell:

```json
{"version": 1, "hooks": {"workspaceOpen": [{"command": "python -I -B \"<shared checkout>/mrs\" pstack sync --pstack \"<pstack checkout>\" --client cursor --hook"}]}}
```

## Check for an update (manual, read-only)

Nothing notifies a consumer or changes its selection; this check is manual. To check on demand, fetch into a scratch bare repository (`git init --bare <scratch>`) outside every checkout, with `<selected>` the commit your selector names and `<N>` a release that `inspect` lists:

```text
python -I -B <shared checkout>/mrs inspect --remote <shared repository> --json
git -C <scratch> fetch --no-tags <shared repository> refs/tags/<N>:refs/tags/<N>
git -C <scratch> merge-base --is-ancestor <selected> "<N>^{commit}"
git -C <scratch> diff --exit-code --stat <selected> "<N>^{commit}" -- multi-repo-stack.json mrs <routed docs>
```

Use `releases` only when `kind` is LIFECYCLE. UNSUPPORTED also exits 0 (an older selected tool, or Git older than 2.30, can report a valid release that way) and UNKNOWN (exit 4) means not observed; neither means "no release", so review with the shared owner. A failed fetch cannot tell. `merge-base` exit 0 means the release contains your selection. `diff --exit-code` exits 0 when the release changes nothing you load or run, including the pstack pin, and 1 when it does. `<routed docs>` are the files preflight lists under `load`, relative to the shared checkout. Any other exit means these objects cannot tell, for example when your selection is not in N's history or is newer than N; never read missing output as "no change". If you selected a commit that is not a release, also fetch `refs/heads/dev`; `merge-base --is-ancestor "<N>^{commit}" <selected>` exit 0 means your selection already contains N. A finding authorizes nothing; an upgrade is the task below, and you may decline it.

## Upgrade the shared selection

1. **Review the change.** Choose the new exact shared commit, and review what changed since the selected one, including its pstack pin.
2. **Use separate checkouts.** Get the exact sources of the new commit into new folders.
3. **Change only the selection.** In a task, change only `shared.commit` in `multi-repo-stack.json`, plus any thin integration the reviewed change requires. Lifecycle consumers can create the task with `mrs task create` and check it with `mrs task check`.
4. **Re-enter and re-check.** Run preflight from the new checkout. Earlier `--expect` records, and the old checkout, now refuse. Run the consumer's own checks with the new sources. Earlier check records bind the old commit and shared source, so they do not apply to the new `dev`; collect again.
5. **Update what restates the selection,** once the task lands: sync again and use the new paths in CLI sessions and in a desktop saved environment's `CLAUDE_CODE_PLUGIN_DIRS`; the shared commit in a cloud setup script; the checkout paths in a Cursor hook.
6. **Leave the product version alone.** The consumer's `VERSION` and product versions stay unchanged. Updating the shared selection is not a product release.

A selector names a commit. Moving a shared branch, or publishing a shared release, therefore changes no consumer.

# multi-repo-stack

A small shared substrate for agent work across repositories. Each repository commits a selector, `multi-repo-stack.json`, naming an exact commit of this project; that commit pins canonical [pstack](https://github.com/cursor/plugins/tree/main/pstack), which supplies engineering and PR workflow. A `context` repository gets the shared instructions. A `lifecycle` repository also gets `dev` and `main` lines, exact-commit acceptance, and bootstrap and releases applied as one guarded atomic Git push. Product truth stays in each product repository.

## What this revision implements

Everything below is exercised by tests against disposable local repositories. Hosted CI (`.github/workflows/checks.yml`, read-only) runs them on Linux, Windows and macOS with Python 3.11; see `docs/verification.md`. `dev` beyond the latest release is unreleased work on the line in `VERSION`. A release is an annotated version tag whose embedded receipt is its acceptance record.

- **Preflight** (`mrs preflight`) reads the consumer's committed selector and verifies the selected shared checkout and the pinned pstack checkout: exact HEAD, nothing modified, untracked or ignored, and content re-hashed byte for byte against the commit. Replace refs are ignored and checkout filters are not trusted, so clone with `--config core.autocrlf=false` if your Git converts line endings. It prints the verified identities and the files to load: `docs/context.md`, plus `docs/lifecycle.md` for `lifecycle`. `--expect` refuses a run whose selection changed. When this project checks itself, its own checkout must be clean and committed too, and a new commit invalidates earlier `--expect` records.
- **Observation** (`mrs inspect`) tells absent refs from UNKNOWN. Hidden refs, broken refs, missing history, unreadable Git configuration and unparseable URLs are UNKNOWN, and a target that Git would redirect is refused. It recognizes contract release tags (annotated, directly on C, embedding a canonical receipt bound to exact object IDs), re-validates each receipt and applies the release-history rules.
- **Connection and upgrades** are procedures, not tools (`docs/connect.md`). A selector and an `AGENTS.md` route connect a repository for context, and nothing else changes. An upgrade changes only the selector; old checkouts, `--expect` records and check records then refuse.
- **Tasks** (`mrs task create|check`). `create` adds `<active VERSION>/<task>` to your checkout at the target's current `dev`, and refuses a branch that exists here or on the target. `check` validates a task against the target's current state (rules in `docs/lifecycle.md`); a VALID result is one observation, not a lock or acceptance. Both refuse context-only, empty, unsupported and redirected targets. pstack owns PRs, stacks, review and landing.
- **Acceptance criteria** live in the lifecycle selector: `checks` gives each check's argv, the environment labels it needs and whether it must write evidence; `environments` gives each label's native OS (`windows`, `linux` or `macos`; WSL is linux). The criteria identity is the SHA-256 of both, read from Git objects at the candidate. Context selectors declare none.
- **Collection** (`mrs collect`) runs the checks for C, the target's observed `dev`, at N, C's `VERSION`. A passing collection is evidence, never acceptance.
  - The selected shared and pstack checkouts are verified before and after the run; a change invalidates every result.
  - Each pair for this OS runs in its own fresh checkout of exactly C, without line-ending, filter or encoding conversion, as a literal argv without a shell. Its executable is resolved on PATH and identified by name and SHA-256; batch launchers are refused. Pairs for other OSes are listed as unmet.
  - Tracked bytes, executable bits and links are verified before and after each command. A command that changes them is invalid even on exit 0; untracked build output is allowed. A tracked link that resolves outside the checkout refuses collection, and Windows refuses tracked links altogether.
  - The record is public-safe JSON kept outside C. Each pair's raw output goes to a private log beside it, bound by size and SHA-256.
  - A check may write one UTF-8 file of at most 64 KiB to `MRS_EVIDENCE_FILE`, outside its checkout, and the record keeps its exact bytes. Missing required evidence, a link, a replaced folder, another encoding or an oversized file invalidates the result; optional absence is recorded.
- **Assessment** (`mrs assess`) is SUFFICIENT only with a passing record from the right native OS for every required pair, and a non-author verdict (see "The verdict file"). Each record must bind this repository, C, N, the criteria identity and the selected sources, and carry the facts of a real execution: C observed as `dev` at a UTC time, the runner's OS, platform, Python and Git, the exact checkout and pstack source, and for each passing result its executable, start and finish times and output identity. Anything missing is INCOMPLETE; anything failed, mislabelled, mixed or unbound is REFUSED. These are content and binding checks for trusted operators: the tool cannot authenticate who wrote a verdict, or whether it is true.
- **Bootstrap and release** (`mrs prepare`, `mrs operation`).
  - `prepare` writes only a new operation folder outside every checkout, holding the record and a private store of the prepared objects. `prepare bootstrap` needs an observed empty target (any ref refuses, including a provider's README commit, and UNKNOWN is never empty) and prepares only `dev` at B. You must be the target's sole initializer, because a lease on `dev` cannot guard other refs. `prepare release` needs a SUFFICIENT assessment of the observed `dev` C that you name, made by the tool and pstack checkouts that C selects. It prepares the receipt tag T on C, and D: C's direct child that changes only `VERSION` and records the UTC opening date.
  - `operation inspect` shows the one push. `apply` observes the target again, then pushes with explicit leases on every changed ref, full refspecs, `--atomic` for a release, `--no-follow-tags` and `--recurse-submodules=no`, with no fallback and no retry. `reconcile` reads: COMPLETED, NOT_APPLIED, DIVERGED, MIXED or UNKNOWN. A lost response stays UNKNOWN until reconciled; repeating a completed operation is a no-op, also after later releases. A library push to a real target makes the same observation and refusals as apply, including for a next line opened in an earlier year; only disposable test fixtures skip it, so the tests can show that the leases themselves reject.
  - **Bindings.** A bootstrap may only create `dev` at B. A release may only move `main` from P (or absence) to C, create N at T and move `dev` from C to D, and its other facts must match T's validated receipt, including its acceptance identity. The store also binds the kind, the destination, the expected values observed at preparation and the identity of the preparing tool. Every step, by command or library, refuses a record that differs, or a different running tool; a real target also needs that tool to be a clean committed checkout. Nothing is repaired or regenerated: T and D keep their identities across retries.
  - **Destinations** are one absolute path to a local bare repository, or an https, ssh or file URL without a password. Relative paths, remote names, scp-like addresses and unparseable URLs are refused, without repeating the destination, since it may carry a credential. Before every observation, fetch and push, Git must resolve the destination to exactly itself, over its own protocol only.
  - **Receipts** are re-validated whenever a tag is read: against what C provides (its criteria, its shared pin and, when C pins pstack itself, that pin) and against the retained release history. An observer has neither a consumer's selected shared revision nor a pstack checkout, so it checks a consumer's pstack pin and every pstack tree only for form and for agreement across records.
- **Release pages** (`mrs page`), notes only, for a contract release N on `https://github.com/<owner>/<name>`. `page prepare` reads Git only and seals the notes, the one create request (`make_latest` "false", no assets, no generated notes) and the bound principal into a new operation folder; the SHA-256 of its `page.json` is the digest a grant names. `page apply` sends that request at most once over its own direct HTTPS transport to `api.github.com` (fixed request targets; no redirect, retry, proxy or connection reuse), with the token from `gh auth token --hostname github.com` held in memory only. Its exclusive claim spends the operation whatever the answer. `page reconcile` only reads. Only tag N and N's page decide COMPLETED, NOT_APPLIED, DIVERGED or UNKNOWN; absence counts only for the bound principal with the `repo` scope and push access, and any other read is UNKNOWN.
- **pstack delivery** (`mrs pstack sync`). After preflight, it rebuilds the client's tree at the pin from Git objects into an immutable cache entry named by its content digest: upstream's generated `harness/pstack/claude-code` for Claude Code, `pstack/` for Cursor. Beside it is a generated `mrs-handshake` plugin whose answer names the shared and pstack commits. An existing entry is re-verified, never rewritten. It prints the client's activation, documented in `docs/connect.md`: `claude --plugin-dir`, the same paths as `CLAUDE_CODE_PLUGIN_DIRS` for the Claude desktop app, or the `pluginPaths` of a Cursor `workspaceOpen` hook. The Claude app and claude.ai reach a delivery through Remote Control of such a session, or in a cloud session from a cloud environment that stages it.

## Limits

- **Writes.** Only `operation apply` changes a target's refs, and only as prepared; only `page apply` creates a Release page, and only its one. `collect` writes only its record and logs, `task create` only its new branch and the objects it needs into your checkout, `prepare` and `page prepare` only their new operation folder, and `pstack sync` only new cache entries. Any destination rewrite is refused, including an empty `insteadOf`/`pushInsteadOf` prefix in any configuration file or a remote named like the destination; configuration Git cannot read is UNKNOWN.
- Pushes come from the operation's own bare store, which has no submodule repository, so `--recurse-submodules=no` is defense in depth, not a separately demonstrable guard.
- The low-level `prepare_release` records an unjudged payload for the transaction tests and refuses one that claims the assessed format. It accepts only local bare repositories carrying the `mrs-disposable-fixture` marker, which only test code creates. Observers still recognize such receipts.
- The tool cannot see provider rules such as branch protection, rulesets or hidden refs. It reports the push and the state it observes afterwards, and neither bypasses nor predicts provider policy. Provider behavior (atomicity, lease rejection, races, protections) is established only as far as a release's verdict records it.
- The collector cannot run agent-integration or provider checks. They reach acceptance only through the non-author verdict, which accepts or rejects each reused observation.
- Collection is trusted-repository execution, not a sandbox. Checks run with the collector's permissions and environment, minus Git variables that would redirect them to another repository, and can write anywhere those permissions allow. Evidence ownership is checked on POSIX only.
- **Release pages: tag-disappearance race, unsolved.** If tag N is deleted between apply's classification and its create, GitHub may create a lightweight tag N at C. The tool reports DIVERGED (tag) afterwards and repairs nothing, but cannot prevent it. No real `page apply` is authorized until this is resolved or explicitly accepted.
- **Release pages: live behavior unproven.** The tests use loopback stand-ins only. Draft visibility on the `repo`-scope path, byte-exact bodies, `make_latest` "false" and GitHub's answers to redirects and pagination are unobserved; a redirect is UNKNOWN, never followed. TLS uses Python's default trust store and proxies are unsupported, so an unverifiable trust store or a proxied-only network gives UNKNOWN. The token gh prints is trusted beyond the login and scope checks. The listing is read page by page, so a release moving between pages can be missed; other state is sampled, not locked. The claim is a local file without a folder fsync on Windows, and an operator who deletes it and reruns is outside the grant. The tag read inherits Git's redirect behavior. Publishing can trigger release workflows, which the tool does not check.
- **pstack delivery is observed per client, not proven per session.** Recorded probes, all at the earlier pin `0b8e952` (pstack 0.15.26), loaded deliveries of this shape in Claude Code CLI on Windows and Linux, the Claude desktop app on Windows (also driven over Remote Control from the Claude iOS app and claude.ai), Claude Code cloud sessions from a staging environment (started from claude.ai and the iOS app) and Cursor 3.25.9 on Windows; other versions and OSes are unobserved. The handshake shows that one skill of the delivery loaded, not every skill, and plugins delivered to the account or organization can still coexist. A cloud environment is per person and chosen per session, not per repository, and restates the selection. Claude Chat, Cowork and Dispatch and Cursor Cloud Agents have no route and are unsupported.
- Evidence binds exact commits. A release's receipt holds its runtime records, with each runner's OS, Python and Git, and its verdict with the accepted limitations. Hosted CI records describe only the commits they name; a later commit is covered by none of it until its own release.

## Use

Requires Python 3.11 or newer and Git on PATH; `page apply` and `page reconcile` also need gh, as the credential source. Only the standard library is used. Run the tool from the selected checkout with an explicit interpreter, without relying on shebangs:

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
python -I -B <shared checkout>/mrs page prepare --repository https://github.com/<owner>/<name> --tag <N> --title "<title>" --notes <file> --principal <login> --work <new folder> [--json]
python -I -B <shared checkout>/mrs page apply --work <folder> --operation-sha256 <granted digest> [--json]
python -I -B <shared checkout>/mrs page reconcile --work <folder> [--json]
python -I -B <shared checkout>/mrs pstack sync --consumer <consumer checkout> --pstack <pstack checkout> --client claude-code|cursor [--cache <folder>] [--hook] [--json]
```

Exit codes:

- `0`: OK, every applicable check passed, the assessment is SUFFICIENT, the task branch was created, the task is VALID, the operation was prepared, is valid, was applied or was already complete, or the pstack delivery is staged (Cursor's `--hook` output, every result).
- `1`: a check did not pass, the assessment is INCOMPLETE, the task is INVALID, or the operation is not applied yet.
- `2`: usage error.
- `3`: refused, or the target diverged from the operation.
- `4`: unknown observation or outcome.
- `5`: the selection changed during a run.
- `6`: mixed: the target holds part of the operation; stop and have a repair reviewed separately.

Page commands use the same codes: `0` PREPARED, COMPLETED, APPLIED or NOOP; `1` NOT_APPLIED; `3` REFUSED or DIVERGED; `4` UNKNOWN.

**The verdict file** is JSON in the `multi-repo-stack/attestation/1` format. Its keys:

- `repository`, `candidate` (the full commit ID of C) and `version`.
- `criteria`: `identity` (C's criteria identity), `previous` (the latest release's criteria identity, or `null` at a first release) and `assessment` (how criteria changes since then, or since the frozen intent, were judged).
- `verifier`: `relationship` (must be `non-author`), `statement` (why the verifier is a non-author) and `harness` (the actual harness, tools and substitutions).
- `behavior` and `evidence`: what was inspected.
- `limitations`: each one `accepted` or `unresolved`.
- `reuse`: each proposed reused observation, `accepted` or `rejected`, with a rationale. An observation names its `id`, exact `source_commit`, `harness` and `scope`.
- `verdict`: `PASS` or `FAIL`.

Tests and the recipe for this project's own records: `docs/verification.md`.

## Layout

- `AGENTS.md` is the agent entrypoint. `docs/` holds the routed instructions, the verification recipe and the connection and upgrade procedures.
- `multi-repo-stack.json` is the single selector: repository identity, applicability, the pinned canonical pstack and this project's own check criteria. `VERSION` holds the active line.
- `mrs/` is the tool. `tests/` holds the disposable-fixture tests; `tests/consumers/` holds the tiny consumer shapes they build. `.github/workflows/checks.yml` is the read-only CI.

## License

MIT (see `LICENSE`).

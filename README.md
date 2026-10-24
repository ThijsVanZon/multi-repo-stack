# multi-repo-stack

A small shared substrate for agent work across repositories. Each repository commits a selector, `multi-repo-stack.json`, naming an exact commit of this project; that commit pins canonical [pstack](https://github.com/cursor/plugins/tree/main/pstack), which supplies engineering and PR workflow. A `context` repository gets the shared instructions. A `lifecycle` repository also gets `dev` and `main` lines, exact-commit acceptance, and bootstrap and releases applied as one guarded atomic Git push. Product truth stays in each product repository.

## What this revision implements

Everything below is exercised by tests against disposable local repositories. Hosted CI (`.github/workflows/checks.yml`, read-only) runs them on Linux, Windows and macOS with Python 3.11; see `docs/verification.md`. `dev` beyond the latest release is unreleased work on the line in `VERSION`. A release is an annotated version tag whose embedded receipt is its acceptance record. The rules are in `docs/lifecycle.md`, and each module's docstring in `mrs/` states its mechanism.

- **Preflight** (`mrs preflight`) verifies the selected shared checkout and the pinned pstack checkout against their commits: exact HEAD, nothing modified, untracked or ignored, content re-hashed byte for byte. Replace refs are ignored and checkout filters are not trusted, so clone with `--config core.autocrlf=false` if your Git converts line endings. It prints the verified identities and the files to load; `--expect` refuses a run whose selection changed.
- **Observation** (`mrs inspect`) tells absent refs from UNKNOWN, refuses a target that Git would redirect, and recognizes and re-validates contract release tags and their receipts.
- **Connection and upgrades** are procedures, not tools (`docs/guide.md`).
- **Tasks** (`mrs task create|check`) create a task branch of the active line and validate it against the target's current state.
- **Acceptance criteria** live in the lifecycle selector: `checks` gives each check's argv, the environment labels it needs and whether it must write evidence; `environments` gives each label's native OS (`windows`, `linux` or `macos`; WSL is linux). The criteria identity is the SHA-256 of both, read from Git objects at the candidate. Context selectors declare none.
- **Collection** (`mrs collect`) runs C's checks for this OS. The record is public-safe JSON kept outside C; each pair's raw output goes to a private log beside it, bound by size and SHA-256. Executables are resolved on PATH and identified by name and SHA-256; batch launchers are refused, and Windows refuses tracked links. A check may write one UTF-8 file of at most 64 KiB to `MRS_EVIDENCE_FILE`, and the record keeps its exact bytes.
- **Assessment** (`mrs assess`) judges records and the verdict (see "The verdict file") as SUFFICIENT, INCOMPLETE or REFUSED.
- **Bootstrap and release** (`mrs prepare`, `mrs operation`). `prepare` writes a new operation folder outside every checkout; `operation inspect` shows the one push, `apply` makes it and `reconcile` reads: COMPLETED, NOT_APPLIED, DIVERGED, MIXED or UNKNOWN. Destinations are one absolute path to a local bare repository, or an https, ssh or file URL without a password.
- **Release pages** (`mrs page`), notes only, for a contract release N on `https://github.com/<owner>/<name>`. The create request sets no `make_latest`, so GitHub's default marks the new page latest, even for an older release. The digest a grant names covers `prepared_at`, so a later prepare gives a new one: prepare once, from the approved notes file, and grant the digest of the folder that will be applied. Only tag N and N's page decide the outcome.
- **pstack delivery** (`mrs pstack sync`) stages the client's tree at the pin, beside a generated `mrs-handshake` plugin, and prints how the client loads it; `docs/guide.md` covers each client.

## Limits

- **Writes** are only those listed in `mrs/cli.py`. Any destination rewrite is refused, including an empty `insteadOf`/`pushInsteadOf` prefix in any configuration file or a remote named like the destination; configuration Git cannot read is UNKNOWN.
- Pushes come from the operation's own bare store, which has no submodule repository, so `--recurse-submodules=no` is defense in depth, not a separately demonstrable guard.
- The low-level `prepare_release` records an unjudged payload for the transaction tests and refuses one that claims the assessed format. It accepts only local bare repositories carrying the `mrs-disposable-fixture` marker, which only test code creates. Observers still recognize such receipts.
- The tool cannot see provider rules such as branch protection, rulesets or hidden refs. It reports the push and the state it observes afterwards, and neither bypasses nor predicts provider policy. Provider behavior (atomicity, lease rejection, races, protections) is established only as far as a release's verdict records it.
- Assessment makes content and binding checks for trusted operators: the tool cannot authenticate who wrote a verdict, or whether it is true.
- An observer re-validating a receipt has neither a consumer's selected shared revision nor a pstack checkout, so it checks a consumer's pstack pin and every pstack tree only for form and for agreement across records.
- The collector cannot run agent-integration or provider checks. They reach acceptance only through the pre-release review verdict.
- Collection is trusted-repository execution, not a sandbox. Checks run with the collector's permissions and environment, minus Git variables that would redirect them to another repository, and can write anywhere those permissions allow. Evidence ownership is checked on POSIX only.
- **Release pages: tag-disappearance race, unsolved.** If tag N is deleted between apply's classification and its create, GitHub may create a lightweight tag N at C. The tool reports DIVERGED (tag) afterwards and repairs nothing, but cannot prevent it. A grant for a real `page apply` must accept it explicitly.
- **Release pages: live behavior unproven.** The tests use loopback stand-ins only. The one real apply, the 26.2.0 page, read back its body byte-exact. Draft visibility on the `repo`-scope path, GitHub's default latest marking and its answers to redirects and pagination are unobserved; a redirect is UNKNOWN, never followed. TLS uses Python's default trust store and proxies are unsupported, so an unverifiable trust store or a proxied-only network gives UNKNOWN. The token gh prints is trusted beyond the login and scope checks. The listing is read page by page, so a release moving between pages can be missed; other state is sampled, not locked. The claim is a local file without a folder fsync on Windows, and an operator who deletes it and reruns is outside the grant. The tag read inherits Git's redirect behavior. Publishing can trigger release workflows, which the tool does not check.
- **pstack delivery is observed per client, not proven per session.** Recorded probes at pin `0b8e952` (pstack 0.15.26) loaded deliveries of this shape in Claude Code CLI on Windows and Linux, the Claude desktop app on Windows (also driven over Remote Control from the Claude iOS app and claude.ai), Claude Code cloud sessions from a staging environment (started from claude.ai and the iOS app) and Cursor 3.25.9 on Windows, and one at `9580c23` (pstack 0.15.32) in headless Claude Code CLI 2.1.310 on Linux; other versions and OSes are unobserved. The handshake shows that one skill of the delivery loaded, not every skill, and plugins delivered to the account or organization can still coexist. A cloud environment is per person and chosen per session, not per repository, and restates the selection. Claude Chat, Cowork and Dispatch and Cursor Cloud Agents have no route and are unsupported.
- The `pre-merge review` status (`.github/workflows/review.yml`) records that a collaborator posted `PASS <head>` for that exact head, not who reviewed it: every session here comments as the owner. Anyone with write access, or a workflow in a same-repo PR, can also set it. Release accounts bypass the rulesets that require it, so for sessions acting as one of them it is a signal, not a lock; the pre-release review checks every landing's PASS. Non-authorship rests on commissioning, as for the verdicts `assess` reads.
- Evidence binds exact commits. A release's receipt holds its runtime records, with each runner's OS, Python and Git, and its verdict with the accepted limitations. Hosted CI records describe only the commits they name; a later commit is covered by none of it until its own release.

## Use

Requires Python 3.11 or newer and Git on PATH; `page apply` and `page reconcile` also need gh, as the credential source. Git, gh and check commands are found only in absolute PATH entries, never in the current directory, and batch launchers are refused. Only the standard library is used. Run the tool from the selected checkout with an explicit interpreter, without relying on shebangs:

```text
python -I -B <shared checkout>/mrs preflight --consumer <consumer checkout> --pstack <cursor/plugins checkout at the pinned commit> [--json] [--expect <earlier --json output>]
python -I -B <shared checkout>/mrs inspect --remote <path or URL> [--json]
python -I -B <shared checkout>/mrs collect --repository <path or URL> --pstack <pstack checkout> --out <new record file> [--json]
python -I -B <shared checkout>/mrs assess --repository <path or URL> --pstack <pstack checkout> --record <record file> [--record ...] [--attestation <verdict file>] [--json]
python -I -B <shared checkout>/mrs task create --repository <path or URL> [--checkout <local checkout>] --task <task> [--json]
python -I -B <shared checkout>/mrs task check --repository <path or URL> [--checkout <local checkout>] --branch <task branch> [--commit <commit>] [--base dev|<parent task branch>] [--json]
python -I -B <shared checkout>/mrs prepare bootstrap --repository <path or URL> --source <local repository> --commit <B> --pstack <pstack checkout> --work <new folder> [--json]
python -I -B <shared checkout>/mrs prepare release --repository <path or URL> --candidate <C> --pstack <pstack checkout> --record <record file> [--record ...] --attestation <verdict file> --identity "<Name> <email>" --work <new folder> [--json]
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
- `reuse`: `[]` under the agent-integration gate in `docs/verification.md`, which the verdict applies and the tool does not enforce. Releases up to 26.2.0 recorded reused agent observations here, so the tool still checks each entry's form: `accepted` or `rejected` with a rationale, naming its `id`, exact `source_commit`, `harness` and `scope`.
- `verdict`: `PASS` or `FAIL`.

Tests and the recipe for this project's own records: `docs/verification.md`.

## Layout

- `AGENTS.md` is the agent entrypoint. `docs/` holds the routed instructions, the verification recipe and the consumer setup guide.
- `multi-repo-stack.json` is the single selector: repository identity, applicability, the pinned canonical pstack and this project's own check criteria. `VERSION` holds the active line.
- `mrs/` is the tool. `tests/` holds the disposable-fixture tests; `tests/consumers/` holds the tiny consumer shapes they build. `.github/workflows/checks.yml` is the read-only CI.

## License

MIT (see `LICENSE`).
